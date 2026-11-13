"""Ranked Python skill registry and safe filesystem discovery for DeepSpace."""

from __future__ import annotations

import builtins
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from app.core.averqel_home import averqel_agents_home, is_averqel_desktop_runtime
from app.core.config import Settings
from app.deepspace.services.bundled_skills import get_bundled_skills_manager
from app.deepspace.services.workspace_root import WorkspacePathError, workspace_root_for

logger = logging.getLogger(__name__)
_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
_STRICT_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_REGISTRIES: dict[tuple[str, ...], SkillRegistry] = {}
_REGISTRY_LOCK = threading.RLock()
_MAX_SKILL_BYTES = 160_000
_USER_SKILL_COMMAND = re.compile(r"^(?:/|@skill:)([a-z0-9]+(?:[-_][a-z0-9]+)*)(?:\s+([\s\S]*))?$")
_SKILL_WATCH_INTERVAL_SECONDS = 1.0
_SKILL_CACHE_TTL_SECONDS = 0.0
_SKILL_COLLECT_ATTEMPTS = 2
_RUNTIME_SKILL_RANK = 250


def is_valid_skill_name(name: str, *, strict: bool = False) -> bool:
    """Return whether a name follows the skill name contract.

    Args:
        name: The candidate skill name.
        strict: Require kebab case, rejecting underscores.  Strict naming is
            available for portable skill bundles; the default accepts the
            underscores that earlier DeepSpace skills already use.

    Returns:
        True when the name is valid.
    """
    pattern = _STRICT_SKILL_NAME if strict else _SKILL_NAME
    return bool(pattern.fullmatch(str(name or "")))


@dataclass(frozen=True, slots=True)
class SkillInvocationPolicy:
    """Whether the model or user may explicitly invoke a skill."""

    model_invocable: bool = True
    user_invocable: bool = True
    admin_only: bool = False


@dataclass(frozen=True, slots=True)
class SkillProviderObservation:
    """The outcome of one provider discovery attempt.

    A provider that fails is reported rather than hidden: the catalog then
    carries ``incomplete`` and the model is told not to trust an empty result.
    """

    provider: str
    ok: bool
    discovered: int = 0
    attempts: int = 1
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    """One resolved skill, with provenance and optional resource base."""

    name: str
    description: str
    when_to_use: str
    invocation: SkillInvocationPolicy
    source: str
    provider: str
    rank: int
    content: str
    path: Path | None = None
    resource_base: Path | None = None
    content_truncated: bool = False
    content_sha256: str = ""

    def __post_init__(self) -> None:
        """Fill in the content digest when a provider did not supply one."""
        if not self.content_sha256:
            object.__setattr__(
                self,
                "content_sha256",
                hashlib.sha256(self.content.encode("utf-8", errors="replace")).hexdigest(),
            )


@dataclass(frozen=True, slots=True)
class SkillRoot:
    """One ordered directory that contributes skills.

    ``skip_system`` keeps a user-level root from advertising dot-directories
    and vendored trees.  ``trusted`` marks a product-owned root whose content
    ships with the application rather than with the workspace.
    """

    path: Path
    source: str
    rank: int
    skip_system: bool = False
    trusted: bool = False


class SkillContentResolver(Protocol):
    """A source of skill document text and sizes.

    The default resolver reads the local filesystem.  A deployment that keeps
    a workspace on another host can supply a resolver instead of exposing the
    remote tree as local paths.
    """

    def read_text(self, path: Path, *, limit: int) -> str | None: ...

    def size(self, path: Path) -> int | None: ...


class LocalSkillContentResolver:
    """Read skill documents from the local filesystem."""

    def read_text(self, path: Path, *, limit: int) -> str | None:
        """Read one document, or return None when it is unavailable or large."""
        try:
            if path.stat().st_size > limit:
                return None
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def size(self, path: Path) -> int | None:
        """Return one document's size, or None when it is unavailable."""
        try:
            return path.stat().st_size
        except OSError:
            return None


class SkillProvider(Protocol):
    """A source of skill definitions."""

    name: str

    def discover(self) -> list[SkillDefinition]: ...


class SkillInvocationError(ValueError):
    """A slash command named a skill that forbids direct user invocation."""


def _unquote_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        if value[0] == '"':
            try:
                decoded = json.loads(value)
                return decoded if isinstance(decoded, str) else value[1:-1]
            except json.JSONDecodeError:
                return value[1:-1]
        return value[1:-1].replace("''", "'")
    return value


def parse_skill_document(
    raw: str,
    *,
    fallback_name: str,
    source: str,
    provider: str,
    rank: int,
    path: Path | None = None,
    resource_base: Path | None = None,
    strict_names: bool = False,
) -> SkillDefinition:
    """Parse the scalar YAML frontmatter fields used by the skill contract."""
    metadata: dict[str, str] = {}
    body = raw
    if raw.startswith("---\n") or raw.startswith("---\r\n"):
        lines = raw.splitlines()
        end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
        if end is None:
            raise ValueError("Skill frontmatter is not closed.")
        for line in lines[1:end]:
            key, separator, value = line.partition(":")
            if not separator:
                continue
            normalized = key.strip().replace("_", "-").casefold()
            if normalized in {
                "name",
                "description",
                "when-to-use",
                "model-invocable",
                "user-invocable",
                "admin-only",
            }:
                metadata[normalized] = _unquote_scalar(value)
        body = "\n".join(lines[end + 1 :]).lstrip("\n")

    name = metadata.get("name", fallback_name).strip()
    if not is_valid_skill_name(name, strict=strict_names):
        if strict_names:
            raise ValueError("Skill names must be lowercase words separated by hyphens.")
        raise ValueError("Skill names must be lowercase words separated by hyphens or underscores.")
    description = metadata.get("description", "").strip()
    if not description:
        description = next(
            (line.strip().lstrip("# ")[:300] for line in body.splitlines() if line.strip()),
            name,
        )
    model_flag = metadata.get("model-invocable", "true").casefold()
    user_flag = metadata.get("user-invocable", "true").casefold()
    admin_flag = metadata.get("admin-only", "false").casefold()
    if (
        model_flag not in {"true", "false"}
        or user_flag not in {"true", "false"}
        or admin_flag not in {"true", "false"}
    ):
        raise ValueError("Skill invocation flags must be true or false.")
    content = body[:40_000]
    return SkillDefinition(
        name=name,
        description=description[:500],
        when_to_use=metadata.get("when-to-use", "").strip()[:1_000],
        invocation=SkillInvocationPolicy(
            model_flag == "true",
            user_flag == "true",
            admin_flag == "true",
        ),
        source=source,
        provider=provider,
        rank=rank,
        content=content,
        path=path,
        resource_base=resource_base,
        content_truncated=len(body) > 40_000,
        content_sha256=hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest(),
    )


class FilesystemSkillProvider:
    """Discover flat Markdown and directory skills from ordered trusted roots."""

    name = "filesystem"

    def __init__(
        self,
        roots: Sequence[SkillRoot | tuple[Path, str, int]],
        *,
        resolver: SkillContentResolver | None = None,
        strict_names: bool = False,
    ) -> None:
        self.roots = [_coerce_root(root) for root in roots]
        self.resolver = resolver or LocalSkillContentResolver()
        self.strict_names = strict_names

    def discover(self) -> list[SkillDefinition]:
        found: list[SkillDefinition] = []
        for skill_root in self.roots:
            configured_root = skill_root.path
            try:
                root = configured_root.expanduser().resolve(strict=True)
            except OSError:
                continue
            if not root.is_dir():
                continue
            try:
                entries = sorted(root.iterdir(), key=lambda entry: entry.name.casefold())
            except OSError:
                logger.warning("Could not scan skill root %s", root)
                continue
            for entry in entries:
                try:
                    if skill_root.skip_system and (
                        entry.name.startswith(".") or entry.name in {"node_modules", "__pycache__"}
                    ):
                        continue
                    if entry.is_symlink():
                        target = entry.resolve(strict=True)
                        if not target.is_relative_to(root):
                            continue
                    if entry.is_file() and entry.suffix.casefold() == ".md":
                        document = entry.resolve(strict=True)
                        fallback = entry.stem
                        resource_base: Path | None = document.parent
                    elif entry.is_dir():
                        skill_dir = entry.resolve(strict=True)
                        if not skill_dir.is_relative_to(root):
                            continue
                        skill_document: Path | None = None
                        for candidate in (
                            skill_dir / "SKILL.md",
                            skill_dir / "skill.md",
                            skill_dir / "README.md",
                        ):
                            if candidate.is_file():
                                skill_document = candidate
                                break
                        if skill_document is None:
                            continue
                        document = skill_document.resolve(strict=True)
                        if not document.is_relative_to(root):
                            continue
                        fallback = entry.name
                        resource_base = skill_dir
                    else:
                        continue
                    raw: str | None
                    if isinstance(self.resolver, LocalSkillContentResolver):
                        if document.stat().st_size > _MAX_SKILL_BYTES:
                            continue
                        raw = document.read_text(encoding="utf-8", errors="replace")
                    else:
                        raw = self.resolver.read_text(document, limit=_MAX_SKILL_BYTES)
                        resource_base = None
                    if raw is None:
                        continue
                    found.append(
                        parse_skill_document(
                            raw,
                            fallback_name=fallback,
                            source=skill_root.source,
                            provider=self.name,
                            rank=skill_root.rank,
                            path=document,
                            resource_base=resource_base,
                            strict_names=self.strict_names,
                        )
                    )
                except (OSError, ValueError, WorkspacePathError):
                    logger.debug("Ignoring invalid skill entry %s", entry, exc_info=True)
        return found


def _coerce_root(root: SkillRoot | tuple[Path, str, int]) -> SkillRoot:
    """Return one root in the structured form without changing tuple callers."""
    if isinstance(root, SkillRoot):
        return root
    path, source, rank = root
    return SkillRoot(path=path, source=source, rank=rank)


class BundledSkillProvider:
    """Expose enabled built-in skills without writing over workspace files."""

    name = "bundled"

    def __init__(self, settings: Settings) -> None:
        self.manager = get_bundled_skills_manager(settings)

    def discover(self) -> list[SkillDefinition]:
        definitions: list[SkillDefinition] = []
        entries_getter = getattr(self.manager, "get_bundled_skill_entries", None)
        if entries_getter is None:
            entries = {
                name: (content, None) for name, content in self.manager.get_bundled_skills().items()
            }
        else:
            entries = entries_getter()
        for name, (content, resource_base) in entries.items():
            try:
                definitions.append(
                    parse_skill_document(
                        content,
                        fallback_name=name,
                        source="bundled",
                        provider=self.name,
                        rank=600,
                        resource_base=resource_base,
                    )
                )
            except ValueError:
                logger.warning("Ignoring invalid bundled skill %s", name)
        return definitions


class RuntimeSkillProvider:
    """Publish skills registered at runtime, without a backing file.

    A plugin, workflow, or remote catalog can contribute a skill for one
    conversation.  The provider owns rank 250, between explicit project roots
    and user-level roots, so a workspace skill still wins over a runtime
    suggestion.
    """

    name = "runtime"

    def __init__(self) -> None:
        self._skills: dict[str, SkillDefinition] = {}
        self._lock = threading.RLock()

    def add(self, skill: SkillDefinition) -> Any:
        """Add or replace one runtime skill and return an idempotent remover."""
        key = skill.name.casefold()
        with self._lock:
            self._skills[key] = skill
        removed = False

        def remove() -> None:
            nonlocal removed
            with self._lock:
                if not removed:
                    removed = True
                    self._skills.pop(key, None)

        return remove

    def discover(self) -> list[SkillDefinition]:
        """Return a snapshot of the published runtime skills."""
        with self._lock:
            return list(self._skills.values())


def _scope_allows(skill: SkillDefinition, scope: Any) -> bool:
    """Return whether one scope may see one resolved skill."""
    if scope is None:
        return True
    from app.deepspace.services.skill_scope import filter_skills_for_scope

    return bool(filter_skills_for_scope([skill], scope))


class SkillRegistry:
    """Merge provider catalogs, resolve ranked duplicates, and emit changes."""

    def __init__(
        self,
        providers: list[SkillProvider] | None = None,
        *,
        enabled_names: Iterable[str] | None = None,
        disabled_names: Iterable[str] | None = None,
        cache_ttl_seconds: float = 0.0,
    ) -> None:
        self._providers: list[SkillProvider] = list(providers or [])
        self._listeners: list[Any] = []
        self._last_revision: tuple[tuple[str, str, int, int, str], ...] | None = None
        self._lock = threading.RLock()
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._observations: tuple[SkillProviderObservation, ...] = ()
        self._cache: tuple[float, list[SkillDefinition]] | None = None
        self._cache_ttl = max(0.0, float(cache_ttl_seconds))
        self._enabled_names = (
            frozenset(str(name).casefold() for name in enabled_names)
            if enabled_names is not None
            else None
        )
        self._disabled_names = frozenset(str(name).casefold() for name in disabled_names or ())

    def register_provider(self, provider: SkillProvider) -> Any:
        """Register one provider and return an idempotent unregister callback."""
        with self._lock:
            if any(item.name == provider.name for item in self._providers):
                raise ValueError(f"Skill provider {provider.name!r} is already registered.")
            self._providers.append(provider)
        removed = False

        def unregister() -> None:
            nonlocal removed
            with self._lock:
                if not removed:
                    removed = True
                    self._providers = [item for item in self._providers if item is not provider]

        return unregister

    def subscribe(self, callback: Any) -> Any:
        """Watch catalog changes and return an idempotent unsubscribe callback.

        The first listener establishes a baseline and starts a daemon poller.
        The poller rescans skill roots and emits ``skills/change`` when their
        resolved catalog changes. It stops after the final listener leaves.
        """
        self.list()
        with self._lock:
            self._listeners.append(callback)
            self._watch_stop.clear()
            if self._watch_thread is None or not self._watch_thread.is_alive():
                self._watch_thread = threading.Thread(
                    target=self._watch_catalog,
                    name="deepspace-skill-catalog-watch",
                    daemon=True,
                )
                self._watch_thread.start()

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)
                if not self._listeners:
                    self._watch_stop.set()

        return unsubscribe

    def _watch_catalog(self) -> None:
        while True:
            if self._watch_stop.wait(_SKILL_WATCH_INTERVAL_SECONDS):
                with self._lock:
                    if self._listeners:
                        self._watch_stop.clear()
                        continue
                    if self._watch_thread is threading.current_thread():
                        self._watch_thread = None
                    return
            try:
                with self._lock:
                    self._list_locked(use_cache=False)
            except Exception:  # noqa: BLE001 - a failed scan must not kill later notifications.
                logger.exception("Skill catalog watcher failed to rescan providers")

    def list(self) -> builtins.list[SkillDefinition]:
        """Return the winning definition for each name, in stable name order."""
        with self._lock:
            return self._list_locked()

    def snapshot(
        self,
    ) -> tuple[builtins.list[SkillDefinition], tuple[SkillProviderObservation, ...]]:
        """Return the resolved catalog and each provider's last outcome.

        Returns:
            The winning definitions and one observation per configured
            provider, so a caller can tell an empty catalog from a failed
            provider.
        """
        with self._lock:
            return self._list_locked(), self._observations

    def invalidate(self) -> None:
        """Discard the collection cache after an out-of-band change."""
        with self._lock:
            self._cache = None

    def register_runtime_skill(self, skill: SkillDefinition) -> Any:
        """Publish one skill for this registry without writing a file.

        Args:
            skill: The resolved definition to publish.  Its provider name is
                normalized to ``runtime`` and its rank to 250.

        Returns:
            An idempotent callback that removes the skill again.
        """
        runtime = next(
            (item for item in self._providers if isinstance(item, RuntimeSkillProvider)),
            None,
        )
        with self._lock:
            if runtime is None:
                runtime = RuntimeSkillProvider()
                self._providers.append(runtime)
            normalized = SkillDefinition(
                name=skill.name,
                description=skill.description,
                when_to_use=skill.when_to_use,
                invocation=skill.invocation,
                source=skill.source or "runtime",
                provider="runtime",
                rank=_RUNTIME_SKILL_RANK,
                content=skill.content,
                path=skill.path,
                resource_base=skill.resource_base,
                content_truncated=skill.content_truncated,
                content_sha256=skill.content_sha256
                or hashlib.sha256(skill.content.encode("utf-8", errors="replace")).hexdigest(),
            )
            remove = runtime.add(normalized)
            self._cache = None
            return remove

    def list_for_scope(self, scope: Any) -> builtins.list[SkillDefinition]:
        """Return the catalog one scope may see.

        Args:
            scope: A ``SkillScope`` from ``skill_scope``.

        Returns:
            The visible definitions, inherited from the scope chain.
        """
        from app.deepspace.services.skill_scope import filter_skills_for_scope

        return filter_skills_for_scope(self.list(), scope)

    def _list_locked(self, *, use_cache: bool = True) -> builtins.list[SkillDefinition]:
        """Resolve the catalog while the registry lock is held."""
        now = time.monotonic()
        if use_cache and not self._listeners and self._cache is not None and now < self._cache[0]:
            return self._cache[1]
        candidates: list[SkillDefinition] = []
        observations: list[SkillProviderObservation] = []
        for provider in self._providers:
            attempts = 0
            discovered: list[SkillDefinition] = []
            last_error: Exception | None = None
            while attempts < _SKILL_COLLECT_ATTEMPTS:
                attempts += 1
                try:
                    discovered = list(provider.discover())
                    last_error = None
                    break
                except Exception as exc:  # noqa: BLE001 - one broken source does not hide others.
                    last_error = exc
                    logger.exception(
                        "Skill provider %s failed discovery (attempt %s)",
                        provider.name,
                        attempts,
                    )
            if last_error is not None:
                observations.append(
                    SkillProviderObservation(
                        provider=provider.name,
                        ok=False,
                        discovered=0,
                        attempts=attempts,
                        error=str(last_error),
                    )
                )
                continue
            observations.append(
                SkillProviderObservation(
                    provider=provider.name,
                    ok=True,
                    discovered=len(discovered),
                    attempts=attempts,
                )
            )
            candidates.extend(discovered)
        self._observations = tuple(observations)
        winners: dict[str, SkillDefinition] = {}
        for candidate in sorted(
            candidates, key=lambda item: (item.rank, item.provider, item.source)
        ):
            key = candidate.name.casefold()
            if self._enabled_names is not None and key not in self._enabled_names:
                continue
            if key in self._disabled_names:
                continue
            winners.setdefault(key, candidate)
        result = sorted(winners.values(), key=lambda item: (item.name.casefold(), item.name))
        revision_items: list[tuple[str, str, int, int, str]] = []
        for item in result:
            modified = size = 0
            content_hash = (
                item.content_sha256
                or hashlib.sha256(item.content.encode("utf-8", errors="replace")).hexdigest()
            )
            if item.path is not None:
                try:
                    raw = item.path.read_bytes()
                    file_info = item.path.stat()
                    modified, size = file_info.st_mtime_ns, file_info.st_size
                    content_hash = hashlib.sha256(raw).hexdigest()
                except OSError:
                    pass
            else:
                size = len(item.content)
            revision_items.append(
                (item.name, str(item.path or item.source), modified, size, content_hash)
            )
        revision = tuple(revision_items)
        if self._last_revision is not None and revision != self._last_revision:
            for listener in tuple(self._listeners):
                try:
                    listener("skills/change", revision)
                except Exception:  # noqa: BLE001 - observers must not break listing.
                    logger.exception("Skill catalog change listener failed")
        self._last_revision = revision
        if self._cache_ttl > 0:
            self._cache = (now + self._cache_ttl, result)
        else:
            self._cache = None
        return result

    def get(
        self,
        name: str,
        *,
        for_user: bool = False,
        for_admin: bool = False,
        scope: Any = None,
    ) -> SkillDefinition | None:
        """Resolve one skill while enforcing its invocation policy.

        Args:
            name: The skill name to resolve.
            for_user: Enforce direct user-invocation policy.
            for_admin: Allow admin-only skills.
            scope: The caller's skill-visibility scope, or None when the
                caller is the unrestricted main agent.

        Returns:
            The resolved definition, or None when it is not visible.
        """
        normalized = str(name or "").strip().casefold()
        for skill in self.list():
            if skill.name.casefold() != normalized:
                continue
            if not _scope_allows(skill, scope):
                return None
            if skill.invocation.admin_only and not for_admin:
                return None
            if for_user and not skill.invocation.user_invocable:
                return None
            if not for_user and not skill.invocation.model_invocable:
                return None
            return skill
        return None

    def read_resource(self, skill: SkillDefinition, relative_path: str) -> bytes:
        """Read one linked skill asset without allowing traversal or symlinks out."""
        if skill.resource_base is None:
            raise FileNotFoundError("This skill has no resource directory.")
        base = skill.resource_base.resolve(strict=True)
        target = (base / relative_path).resolve(strict=True)
        if not target.is_relative_to(base) or target.is_symlink() or not target.is_file():
            raise PermissionError("The requested skill resource is outside its resource directory.")
        if target.stat().st_size > _MAX_SKILL_BYTES:
            raise ValueError("The skill resource exceeds the read limit.")
        return target.read_bytes()


def _registry_key(
    settings: Settings, workspace_root: Path, include_shared: bool
) -> tuple[str, ...]:
    custom = str(getattr(settings, "deepspace_skill_custom_roots", "") or "")
    shared = str(averqel_agents_home()) if include_shared else ""
    return (
        str(workspace_root.resolve()),
        custom,
        shared,
        str(bool(getattr(settings, "deepspace_bundled_skills_enabled", True))),
        str(bool(getattr(settings, "deepspace_skill_strict_names", False))),
        str(getattr(settings, "deepspace_skill_enabled_names", "") or ""),
        str(getattr(settings, "deepspace_skill_disabled_names", "") or ""),
        str(getattr(settings, "deepspace_skill_remote_url", "") or ""),
        str(getattr(settings, "deepspace_skill_remote_public_key", "") or ""),
    )


def _name_set(value: Any) -> frozenset[str] | None:
    """Parse a comma or path-separated setting into a name set."""
    text = str(value or "").strip()
    if not text:
        return None
    names = {item.strip().casefold() for item in re.split(r"[,;\n]", text) if item.strip()}
    return frozenset(names) or None


def skill_registry(
    settings: Settings,
    workspace_root: Path,
    *,
    include_shared: bool | None = None,
) -> SkillRegistry:
    """Return the cached registry for one workspace and runtime configuration."""
    shared_enabled = is_averqel_desktop_runtime() if include_shared is None else include_shared
    key = _registry_key(settings, workspace_root, shared_enabled)
    with _REGISTRY_LOCK:
        registry = _REGISTRIES.get(key)
        if registry is not None:
            return registry
        roots: list[SkillRoot] = [
            SkillRoot(workspace_root / ".kinesis" / "skills", "project-kinesis", 100),
            SkillRoot(workspace_root / ".agents" / "skills", "project-agents", 200),
        ]
        custom_roots = str(getattr(settings, "deepspace_skill_custom_roots", "") or "")
        for raw in custom_roots.split(os.pathsep):
            value = raw.strip()
            if value:
                custom = Path(value).expanduser()
                if custom.is_absolute():
                    roots.append(SkillRoot(custom, "custom", 300))
        if shared_enabled:
            shared_home = averqel_agents_home()
            roots.extend(
                [
                    SkillRoot(
                        shared_home / ".kinesis" / "skills",
                        "user-kinesis",
                        400,
                        skip_system=True,
                    ),
                    SkillRoot(
                        shared_home / "skills",
                        "user-agents",
                        500,
                        skip_system=True,
                    ),
                ]
            )
        strict_names = bool(getattr(settings, "deepspace_skill_strict_names", False))
        providers: list[SkillProvider] = [FilesystemSkillProvider(roots, strict_names=strict_names)]
        if bool(getattr(settings, "deepspace_bundled_skills_enabled", True)):
            providers.append(BundledSkillProvider(settings))
        remote_url = str(getattr(settings, "deepspace_skill_remote_url", "") or "").strip()
        remote_key = str(getattr(settings, "deepspace_skill_remote_public_key", "") or "").strip()
        if remote_url and remote_key:
            from app.deepspace.services.skill_bundle import RemoteSkillProvider

            providers.append(RemoteSkillProvider(remote_url, remote_key))
        registry = SkillRegistry(
            providers,
            enabled_names=_name_set(getattr(settings, "deepspace_skill_enabled_names", "")),
            disabled_names=_name_set(getattr(settings, "deepspace_skill_disabled_names", "")) or (),
            cache_ttl_seconds=float(
                getattr(settings, "deepspace_skill_cache_ttl_seconds", 0.0) or 0.0
            ),
        )
        _REGISTRIES[key] = registry
        return registry


def expand_user_skill_invocation(
    prompt: str,
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    workspace_root: Path | None = None,
    for_admin: bool = False,
    scope: Any = None,
) -> str:
    """Expand a known ``/skill-name`` or ``@skill:name`` command into its instructions."""
    match = _USER_SKILL_COMMAND.fullmatch(str(prompt or "").strip())
    if match is None:
        return prompt
    root = workspace_root or workspace_root_for(settings, tenant_id, user_id)
    registry = skill_registry(settings, root)
    name = match.group(1)
    candidate: SkillDefinition | None = None
    for item in registry.list():
        if item.name.casefold() == name.casefold():
            candidate = item
            break
    if candidate is None or not _scope_allows(candidate, scope):
        return prompt
    if candidate.invocation.admin_only and not for_admin:
        return prompt
    if not candidate.invocation.user_invocable:
        raise SkillInvocationError(f"The skill {name!r} is not directly invocable by users.")
    request = (match.group(2) or "").strip()
    provenance = escape_user_skill_text(candidate.provider or candidate.source)
    return (
        f'<skill_content name="{escape_user_skill_text(candidate.name)}" '
        f'provider="{provenance}">\n'
        f"{candidate.content}\n"
        "</skill_content>\n\n"
        f"User-invoked skill: {candidate.name}\n"
        "Follow the skill instructions above for the user's request. Do not "
        "load the same skill again.\n\n"
        f"User request:\n{request or f'Use the {candidate.name} skill.'}"
    )


def escape_user_skill_text(value: str) -> str:
    """Escape one attribute value in the user-invocation wrapper."""
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


__all__ = [
    "BundledSkillProvider",
    "FilesystemSkillProvider",
    "LocalSkillContentResolver",
    "RuntimeSkillProvider",
    "SkillContentResolver",
    "SkillDefinition",
    "SkillInvocationError",
    "SkillInvocationPolicy",
    "SkillProviderObservation",
    "SkillRegistry",
    "SkillRoot",
    "expand_user_skill_invocation",
    "is_valid_skill_name",
    "parse_skill_document",
    "skill_registry",
]
