"""Workspace instruction loader for AGENTS.md/CLAUDE.md hierarchy.

Ported from NeoSIS @averqel/neosis-agent-instructions.
Discovers, loads, reconciles, and renders workspace instruction files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.auth.dependencies import AuthContext
from app.core.config import Settings
from app.deepspace.services.skill_catalog import (
    DEFAULT_CATALOG_MAX_CHARS,
    build_skill_catalog,
    skill_catalog_message,
)
from app.deepspace.services.skill_registry import skill_registry
from app.deepspace.services.workspace_root import (
    WorkspacePathError,
    WorkspaceUnavailableError,
    workspace_root_for,
)

#: Maximum bytes for rendered instructions injected into the prompt.
DEFAULT_MAX_BYTES = 32_000

#: Baseline instruction filenames in priority order (first found wins).
BASELINE_FILENAMES = ("AGENTS.md", "CLAUDE.md")

#: Nested instruction filenames (loaded after baseline).
NESTED_FILENAMES = ("AGENTS.md", "CLAUDE.md", "agents.md", "claude.md")


@dataclass(frozen=True, slots=True)
class InstructionFile:
    """One discovered instruction file."""

    path: Path
    relative_path: str
    content: str
    size: int
    is_baseline: bool


@dataclass(frozen=True, slots=True)
class LoadedInstructionSet:
    """Complete loaded instruction set for a workspace."""

    baseline: InstructionFile | None
    nested: tuple[InstructionFile, ...]
    project_root: Path


@dataclass(frozen=True, slots=True)
class InstructionVersionCache:
    """Version tracking for instruction files."""

    versions: dict[str, int] = field(default_factory=dict)

    def get_version(self, path: str) -> int:
        return self.versions.get(path, 0)

    def set_version(self, path: str, version: int) -> None:
        self.versions[path] = version

    def has_changed(self, path: str, current_version: int) -> bool:
        return self.get_version(path) != current_version


@dataclass(frozen=True, slots=True)
class InstructionContext:
    """Reconciled instruction context ready for rendering."""

    baseline_content: str | None
    nested_instructions: tuple[tuple[str, str], ...]  # (relative_path, content)
    project_root: str
    version_cache: InstructionVersionCache


def discover_baseline_files(workspace_root: Path) -> list[InstructionFile]:
    """Find baseline AGENTS.md/CLAUDE.md at workspace root."""
    files = []
    for name in BASELINE_FILENAMES:
        path = workspace_root / name
        if path.is_file():
            try:
                content = path.read_text(encoding="utf-8")
                files.append(
                    InstructionFile(
                        path=path,
                        relative_path=name,
                        content=content,
                        size=len(content),
                        is_baseline=True,
                    )
                )
            except OSError:
                pass
    return files


def discover_nested_files(workspace_root: Path, project_root: Path) -> list[InstructionFile]:
    """Find nested AGENTS.md/CLAUDE.md files under project root."""
    files = []
    for name in NESTED_FILENAMES:
        for path in project_root.rglob(name):
            if path.is_file():
                try:
                    relative = path.relative_to(workspace_root).as_posix()
                    content = path.read_text(encoding="utf-8")
                    files.append(
                        InstructionFile(
                            path=path,
                            relative_path=relative,
                            content=content,
                            size=len(content),
                            is_baseline=False,
                        )
                    )
                except (OSError, ValueError):
                    pass
    return files


def load_baseline_instructions(workspace_root: Path, project_root: Path) -> LoadedInstructionSet:
    """Load all instruction files for a workspace."""
    baseline_files = discover_baseline_files(workspace_root)
    baseline = baseline_files[0] if baseline_files else None
    nested = tuple(discover_nested_files(workspace_root, project_root))
    return LoadedInstructionSet(
        baseline=baseline,
        nested=nested,
        project_root=project_root,
    )


def reconcile_instruction_context(
    previous: InstructionContext | None,
    current: LoadedInstructionSet,
) -> InstructionContext:
    """Reconcile previous context with newly loaded instructions.

    Preserves version numbers from previous context for files that still exist
    and haven't changed. Increments version for files with changed content.
    """
    version_cache = InstructionVersionCache()

    if previous is not None:
        # Carry over versions for files that still exist
        current_paths = {f.relative_path for f in current.nested}
        if current.baseline:
            current_paths.add(current.baseline.relative_path)

        for path, version in previous.version_cache.versions.items():
            if path in current_paths:
                version_cache.set_version(path, version)

    # Check for changes in current files - only update version if content changed
    if current.baseline:
        baseline_version = hash(current.baseline.content)
        # Only update if we don't have a version for this path, or if content changed
        current_version = version_cache.get_version(current.baseline.relative_path)
        if current_version == 0 or current_version != baseline_version:
            version_cache.set_version(current.baseline.relative_path, baseline_version)

    for nested_file in current.nested:
        nested_version = hash(nested_file.content)
        current_version = version_cache.get_version(nested_file.relative_path)
        if current_version == 0 or current_version != nested_version:
            version_cache.set_version(nested_file.relative_path, nested_version)

    baseline_content = current.baseline.content if current.baseline else None
    nested_instructions = tuple((f.relative_path, f.content) for f in current.nested)

    return InstructionContext(
        baseline_content=baseline_content,
        nested_instructions=nested_instructions,
        project_root=str(current.project_root),
        version_cache=version_cache,
    )


def apply_instruction_version_updates(
    context: InstructionContext,
    changed_files: list[str],
) -> InstructionContext:
    """Update version cache for files that were modified by tools.

    Called after successful fs_write/fs_edit/fs_delete operations.
    """
    version_cache = InstructionVersionCache()
    for path, version in context.version_cache.versions.items():
        version_cache.set_version(path, version)

    for file_path in changed_files:
        # Increment version to mark as changed
        current_version = version_cache.get_version(file_path)
        version_cache.set_version(file_path, current_version + 1)

    return InstructionContext(
        baseline_content=context.baseline_content,
        nested_instructions=context.nested_instructions,
        project_root=context.project_root,
        version_cache=version_cache,
    )


def render_agent_instructions(
    context: InstructionContext,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> str:
    """Render instruction context into a single prompt block.

    Baseline instructions come first, then nested instructions sorted by path.
    Truncates to max_bytes with omission marker.
    """
    parts = []

    if context.baseline_content:
        parts.append(f"# Workspace Baseline Instructions ({context.project_root})")
        parts.append(context.baseline_content.strip())
        parts.append("")

    if context.nested_instructions:
        parts.append("# Nested Workspace Instructions")
        parts.append("")

        for rel_path, content in sorted(context.nested_instructions, key=lambda x: x[0]):
            parts.append(f"## {rel_path}")
            parts.append(content.strip())
            parts.append("")

    rendered = "\n".join(parts).strip()
    if not rendered:
        return ""

    if len(rendered.encode("utf-8")) > max_bytes:
        # Truncate from the end of nested instructions
        truncated = rendered[: max_bytes - 200]
        truncated = (
            truncated.rsplit("\n", 1)[0] + "\n\n[... instructions truncated to fit context ...]"
        )
        return truncated

    return rendered


def get_workspace_roots(
    settings: Settings,
    auth: AuthContext,
) -> tuple[Path, Path]:
    """Get workspace root and project root for the current auth context."""
    workspace_root = workspace_root_for(settings, auth.tenant_id, auth.user_id)
    project_root = workspace_root
    return workspace_root, project_root


async def prepare_instruction_message(
    settings: Settings,
    auth: AuthContext,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any] | None:
    """Prepare the agent-instructions system message for a turn.

    Returns None if instructions are disabled or no instructions found.
    """
    if not getattr(settings, "deepspace_workspace_instructions_enabled", True):
        return None

    try:
        workspace_root, project_root = get_workspace_roots(settings, auth)
        loaded = load_baseline_instructions(workspace_root, project_root)
        if not loaded.baseline and not loaded.nested:
            return None

        context = reconcile_instruction_context(None, loaded)
        rendered = render_agent_instructions(context, max_bytes)
        if not rendered:
            return None

        return {
            "role": "system",
            "content": rendered,
            "source": {"kind": "agent-instructions", "baseline": True},
        }
    except (WorkspacePathError, WorkspaceUnavailableError, OSError):
        return None


def get_instruction_context_for_tools(
    settings: Settings,
    auth: AuthContext,
) -> InstructionContext | None:
    """Get instruction context for tool-side version tracking."""
    try:
        workspace_root, project_root = get_workspace_roots(settings, auth)
        loaded = load_baseline_instructions(workspace_root, project_root)
        return reconcile_instruction_context(None, loaded)
    except (WorkspacePathError, WorkspaceUnavailableError, OSError):
        return None


def update_instruction_context_after_tool(
    context: InstructionContext | None,
    changed_paths: list[str],
) -> InstructionContext | None:
    """Update instruction context after file-modifying tool operations."""
    if context is None:
        return None
    return apply_instruction_version_updates(context, changed_paths)


def prepare_skill_catalog_message(
    settings: Settings,
    auth: AuthContext,
    *,
    max_chars: int | None = None,
    scope: Any = None,
    workspace_root: Path | None = None,
) -> dict[str, Any] | None:
    """Prepare the model-facing skill catalog for one turn.

    The catalog is rebuilt from the live registry every turn, so a skill
    created, edited, or removed since the last turn is reflected without any
    stored catalog to invalidate.  An empty complete catalog produces no
    message; an empty incomplete catalog still produces one, so a failed
    provider never looks like "no skills exist".

    Args:
        settings: The deployment settings in force.
        auth: The authenticated turn owner.
        max_chars: The rendered catalog budget, or the configured default.
        scope: The agent's skill-visibility scope, or None for the main agent.
        workspace_root: The workspace to read, when the caller resolved one.
            It wins over the account's default workspace.

    Returns:
        A system message in the turn's dynamic-instruction shape, or None when
        there is nothing useful to advertise.
    """
    if not bool(getattr(settings, "deepspace_skill_catalog_enabled", True)):
        return None
    try:
        if workspace_root is None:
            workspace_root, _project_root = get_workspace_roots(settings, auth)
        registry = skill_registry(settings, workspace_root)
        budget = (
            max_chars
            if max_chars is not None
            else int(
                getattr(settings, "deepspace_skill_catalog_max_chars", DEFAULT_CATALOG_MAX_CHARS)
            )
        )
        snapshot = build_skill_catalog(
            registry,
            max_chars=max(0, budget),
            for_admin=_is_admin(auth),
            scope=scope,
        )
        if not snapshot.entries and not snapshot.incomplete:
            return None
        return skill_catalog_message(snapshot)
    except (WorkspacePathError, WorkspaceUnavailableError, OSError):
        return None


def _is_admin(auth: AuthContext) -> bool:
    """Return whether the turn owner may see admin-only skills."""
    roles = {str(role).casefold() for role in getattr(auth, "roles", frozenset())}
    permissions = {str(item).casefold() for item in getattr(auth, "permissions", frozenset())}
    return bool(
        roles & {"admin", "owner", "superuser"} or permissions & {"*", "admin", "skills:admin"}
    )
