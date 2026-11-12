"""The per-tenant project workspace that DeepSpace's file and shell tools use.

DeepSpace's Library is conversation-scoped: every file is a row in the
database and every read is authorized against one conversation.  The file
system tools and the local shell need a different storage, because a coding
agent works on a directory tree that outlives one conversation.  This module
provides that directory and enforces its boundary.

The boundary is the root itself.  A path is resolved against the root, then
checked again after symlink resolution, so neither ``..`` segments nor a
symlink pointing outside the root can reach a file the tenant does not own.

An account owns one default workspace and may own named workspaces beside
it. The standalone desktop app uses ``~/Kinesis`` itself as the default
workspace; shared deployments keep tenant and user directories below the
configured base. A user may instead attach any existing project folder.
Named workspace registrations live under app data on desktop without moving
or copying the selected project.
"""

from __future__ import annotations

import filecmp
import os
import shutil
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

#: The largest path a caller may submit, in characters.
MAX_PATH_LENGTH = 1024

#: The largest number of path segments one request may traverse.
MAX_PATH_SEGMENTS = 64

#: The identifier of the workspace every account starts with.
DEFAULT_WORKSPACE_ID = "default"

#: The directory name used for an account's named workspace catalogue.
NAMED_WORKSPACES_DIRNAME = ".workspaces"

#: The characters a workspace identifier may contain.
WORKSPACE_ID_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_.")

#: The largest workspace identifier, in characters.
MAX_WORKSPACE_ID_LENGTH = 64

_WORKSPACE_MIGRATION_LOCK = threading.RLock()


class WorkspacePathError(ValueError):
    """A submitted path is malformed or escapes the workspace root."""


class WorkspaceUnavailableError(RuntimeError):
    """The deployment has no writable project workspace configured."""


@dataclass(frozen=True, slots=True)
class ResolvedWorkspacePath:
    """One path resolved inside a workspace root."""

    #: The absolute path on disk.
    absolute: Path
    #: The path relative to the workspace root, in POSIX form.
    relative: str

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable projection of this path."""
        return {"path": self.relative, "absolute_path": str(self.absolute)}


def normalize_relative_path(raw: str) -> str:
    """Normalize a caller-supplied relative path.

    The result never starts with a separator, never contains a ``..`` segment,
    and never escapes the root.  An empty path is rejected because every
    tool needs a concrete file or directory.

    Args:
        raw: The path the model supplied.

    Returns:
        The normalized POSIX-style relative path.

    Raises:
        WorkspacePathError: When the path is empty, absolute, too long, too
            deep, or contains a parent-directory segment.
    """
    text = str(raw or "").strip()
    if not text:
        raise WorkspacePathError("A path is required.")
    if len(text) > MAX_PATH_LENGTH:
        raise WorkspacePathError(f"A path may not exceed {MAX_PATH_LENGTH} characters.")
    normalized = text.replace("\\", "/")
    if normalized.startswith("/"):
        raise WorkspacePathError(
            "An absolute path is not accepted; use a path inside the workspace."
        )
    if PurePosixPath(normalized).is_absolute():
        raise WorkspacePathError(
            "An absolute path is not accepted; use a path inside the workspace."
        )
    segments = [segment for segment in normalized.split("/") if segment not in {"", "."}]
    if not segments:
        raise WorkspacePathError("A path is required.")
    if len(segments) > MAX_PATH_SEGMENTS:
        raise WorkspacePathError(f"A path may not exceed {MAX_PATH_SEGMENTS} segments.")
    if any(segment == ".." for segment in segments):
        raise WorkspacePathError("A path may not contain '..'.")
    return "/".join(segments)


def is_workspace_id(value: str) -> bool:
    """Report whether a string is a well-formed workspace identifier.

    A workspace identifier names a directory under the account's root, so it
    may hold only characters that are safe as a directory name.

    Args:
        value: The identifier to check.

    Returns:
        True when the identifier may name a workspace.
    """
    text = str(value or "")
    if not text or len(text) > MAX_WORKSPACE_ID_LENGTH:
        return False
    return all(character in WORKSPACE_ID_CHARACTERS for character in text)


def _workspace_base(settings: Any) -> Path:
    """Return the configured workspace base or the local default root.

    Args:
        settings: The deployment settings in force.

    Returns:
        The configured base directory, ``~/Kinesis`` for the standalone
        desktop app, or the app-home workspace directory for other runtimes.

    Raises:
        WorkspaceUnavailableError: When no workspace base is configured.
    """
    base = str(getattr(settings, "deepspace_workspace_root", "") or "").strip()
    if base:
        from app.core.home_paths import expand_user_path

        return expand_user_path(base)
    from app.core.averqel_home import get_averqel_home, is_averqel_desktop_runtime

    if is_averqel_desktop_runtime():
        return Path.home() / "Kinesis"
    return get_averqel_home() / "workspaces"


def _legacy_desktop_workspace(settings: Any, relative: Path) -> Path | None:
    """Find the old desktop workspace location for a non-destructive copy."""
    if str(getattr(settings, "deepspace_workspace_root", "") or "").strip():
        return None
    from app.core.averqel_home import get_averqel_home, is_averqel_desktop_runtime

    if not is_averqel_desktop_runtime():
        return None
    legacy = get_averqel_home() / "workspaces" / relative
    return legacy if legacy != Path.home() / "Kinesis" / relative else None


def _desktop_local_workspace(settings: Any) -> bool:
    """Report whether this is the standalone desktop's implicit workspace."""
    if str(getattr(settings, "deepspace_workspace_root", "") or "").strip():
        return False
    from app.core.averqel_home import is_averqel_desktop_runtime

    return is_averqel_desktop_runtime()


def _path_exists(path: Path) -> bool:
    """Include dangling symlinks when checking whether a path is occupied."""
    return path.exists() or path.is_symlink()


def _copy_tree_without_overwrite(source: Path, destination: Path) -> bool:
    """Copy missing paths and report whether every source item is preserved.

    Existing files are never replaced. Identical files count as already
    migrated; conflicts and symlinks stay in the source for recovery.
    """
    if source.is_symlink():
        # A relative symlink can change meaning when moved. Keep it at the
        # source so its target and security boundary are not altered.
        return False
    source_resolved = source.resolve()
    destination_resolved = destination.resolve()
    if source_resolved == destination_resolved:
        return True
    if source_resolved.is_relative_to(destination_resolved) or destination_resolved.is_relative_to(
        source_resolved
    ):
        # Avoid recursing into a destination nested inside the source, or
        # treating an existing source inside the workspace as legacy data.
        return False

    if source.is_dir():
        if _path_exists(destination):
            if destination.is_symlink() or not destination.is_dir():
                return False
        else:
            destination.mkdir(parents=True)
        complete = True
        for child in sorted(source.iterdir(), key=lambda item: item.name):
            complete = _copy_tree_without_overwrite(child, destination / child.name) and complete
        return complete

    if source.is_file():
        if _path_exists(destination):
            return (
                not destination.is_symlink()
                and destination.is_file()
                and filecmp.cmp(source, destination, shallow=False)
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return True

    # Do not try to copy sockets, devices, or other special filesystem entries.
    return False


def _move_missing_tree(source: Path, destination: Path) -> bool:
    """Move old workspace entries when safe; leave name conflicts untouched."""
    if not _path_exists(source):
        return True
    if source.is_symlink():
        # Moving a relative symlink would change its target. Preserve it in the
        # old directory instead of silently changing what it points to.
        return False
    if not _path_exists(destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(source, destination)
            return True
        except FileExistsError:
            # A concurrent request created the destination. Merge below.
            pass

    if destination.is_symlink():
        return False
    if source.is_dir() and destination.is_dir():
        complete = True
        for child in sorted(source.iterdir(), key=lambda item: item.name):
            complete = _move_missing_tree(child, destination / child.name) and complete
        if complete:
            source.rmdir()
        return complete
    return False


def _tree_has_symlinks(root: Path) -> bool:
    """Report whether moving this tree could change any relative symlink."""
    for current, directories, files in os.walk(root, followlinks=False):
        if any((Path(current) / name).is_symlink() for name in directories + files):
            return True
    return False


def _copy_nested_workspace_without_relinking(source: Path, destination: Path) -> None:
    """Copy ordinary files out of a nested old root while leaving links intact."""
    from app.core.averqel_home import get_averqel_home

    staging_parent = get_averqel_home() / "workspaces"
    staging_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".workspace-copy-", dir=staging_parent) as name:
        staged = Path(name) / "workspace"
        _copy_tree_without_overwrite(source, staged)
        _copy_tree_without_overwrite(staged, destination)


def _remove_empty_parents(path: Path, stop: Path) -> None:
    """Remove only empty migration directories up to, but not including, stop."""
    current = path
    while current != stop:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _desktop_migration_marker(
    tenant_id: uuid.UUID, user_id: uuid.UUID, migration_name: str
) -> Path:
    """Return the app-data marker that makes a desktop migration one-time."""
    from app.core.averqel_home import get_averqel_home

    return (
        get_averqel_home()
        / "workspaces"
        / ".migration-state"
        / str(tenant_id)
        / str(user_id)
        / migration_name
    )


def _record_desktop_migration(marker: Path) -> None:
    """Record an attempted migration without replacing an existing marker."""
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("complete-or-conflicts-preserved\n")
    except OSError:
        marker.unlink(missing_ok=True)
        raise


def _copy_legacy_desktop_default_once(
    settings: Any,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    root: Path,
    relative: Path,
) -> None:
    """Copy the previous app-data default once, retaining its original files."""
    legacy = _legacy_desktop_workspace(settings, relative)
    if legacy is None or not legacy.is_dir() or legacy.is_symlink():
        return
    marker = _desktop_migration_marker(tenant_id, user_id, "default-copy-v1")
    try:
        with _WORKSPACE_MIGRATION_LOCK:
            if _path_exists(marker):
                return
            _copy_tree_without_overwrite(legacy, root)
            _record_desktop_migration(marker)
    except OSError as exc:
        raise WorkspaceUnavailableError(
            "The previous desktop workspace could not be safely copied into ~/Kinesis."
        ) from exc


def _migrate_desktop_named_workspaces(
    settings: Any, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """Move old Kinesis catalogue entries into the desktop app-data catalogue."""
    if not _desktop_local_workspace(settings):
        return True
    from app.core.averqel_home import get_averqel_home

    base = _workspace_base(settings)
    old_root = base / str(tenant_id) / f"{user_id}{NAMED_WORKSPACES_DIRNAME}"
    app_data_root = get_averqel_home() / "workspaces" / str(tenant_id)
    new_root = app_data_root / f"{user_id}{NAMED_WORKSPACES_DIRNAME}"
    if not _path_exists(old_root):
        return True
    if not old_root.is_dir() or old_root.is_symlink():
        return False
    marker = _desktop_migration_marker(tenant_id, user_id, "named-catalog-v1")
    try:
        with _WORKSPACE_MIGRATION_LOCK:
            if _path_exists(marker):
                return not _path_exists(old_root)
            if _tree_has_symlinks(old_root):
                _record_desktop_migration(marker)
                return False
            app_data_root.mkdir(parents=True, exist_ok=True)
            complete = _copy_tree_without_overwrite(old_root, new_root)
            _record_desktop_migration(marker)
            if complete:
                shutil.rmtree(old_root)
                _remove_empty_parents(old_root.parent, base)
                return True
            return False
    except OSError as exc:
        raise WorkspaceUnavailableError(
            "The desktop workspace catalogue could not be safely moved into app data."
        ) from exc


def _ensure_workspace_directory(root: Path, legacy: Path | None = None) -> Path:
    """Create a workspace and copy legacy contents without replacing files."""
    try:
        with _WORKSPACE_MIGRATION_LOCK:
            root.mkdir(parents=True, exist_ok=True)
            if legacy is not None and legacy.is_dir() and not legacy.is_symlink():
                _copy_tree_without_overwrite(legacy, root)
        return root.resolve()
    except OSError as exc:
        raise WorkspaceUnavailableError(
            "The project workspace could not be created or safely copied for this account."
        ) from exc


def account_workspace_root(settings: Any, tenant_id: uuid.UUID, user_id: uuid.UUID) -> Path:
    """Return one account's default workspace.

    Shared deployments use ``<configured base>/<tenant id>/<user id>``. The
    standalone desktop's implicit default is ``~/Kinesis``. It migrates old
    desktop files into that directory without overwriting conflicts.

    Args:
        settings: The deployment settings in force.
        tenant_id: The owning tenant.
        user_id: The owning user.

    Returns:
        The absolute account root, created if it does not exist.

    Raises:
        WorkspaceUnavailableError: When no workspace base is configured.
    """
    relative = Path(str(tenant_id)) / str(user_id)
    base = _workspace_base(settings)
    if _desktop_local_workspace(settings):
        root = _ensure_workspace_directory(base)
        with _WORKSPACE_MIGRATION_LOCK:
            _copy_legacy_desktop_default_once(settings, tenant_id, user_id, root, relative)
            _migrate_desktop_named_workspaces(settings, tenant_id, user_id)
            old_account_root = base / relative
            if old_account_root.is_dir() and not old_account_root.is_symlink():
                try:
                    marker = _desktop_migration_marker(tenant_id, user_id, "default-flatten-v1")
                    if not _path_exists(marker):
                        if _tree_has_symlinks(old_account_root):
                            # Copy ordinary files where safe but keep the old
                            # tree intact so relative links retain their targets.
                            _copy_nested_workspace_without_relinking(old_account_root, root)
                        else:
                            for child in sorted(
                                old_account_root.iterdir(), key=lambda item: item.name
                            ):
                                _move_missing_tree(child, root / child.name)
                            _remove_empty_parents(old_account_root, base)
                        _record_desktop_migration(marker)
                except OSError as exc:
                    raise WorkspaceUnavailableError(
                        "The old desktop workspace could not be safely moved into ~/Kinesis."
                    ) from exc
        return root

    root = base / relative
    legacy = _legacy_desktop_workspace(settings, relative)
    return _ensure_workspace_directory(root, legacy)


def named_workspaces_root(settings: Any, tenant_id: uuid.UUID, user_id: uuid.UUID) -> Path:
    """Return the directory that holds one account's named workspaces.

    The named workspaces stay outside the account's default workspace, so a
    workspace never appears in another workspace's file tree and a
    conversation working in one cannot reach the others through the file tools.

    Args:
        settings: The deployment settings in force.
        tenant_id: The owning tenant.
        user_id: The owning user.

    Returns:
        The absolute directory of the named workspaces, created if it does
        not exist.

    Raises:
        WorkspaceUnavailableError: When no workspace base is configured.
    """
    relative = Path(str(tenant_id)) / f"{user_id}{NAMED_WORKSPACES_DIRNAME}"
    if _desktop_local_workspace(settings):
        from app.core.averqel_home import get_averqel_home

        root = get_averqel_home() / "workspaces" / relative
        if not _migrate_desktop_named_workspaces(settings, tenant_id, user_id):
            root = _workspace_base(settings) / relative
            if root.is_symlink():
                raise WorkspaceUnavailableError(
                    "The old desktop workspace catalogue is a symlink and was left untouched."
                )
        legacy = None
    else:
        root = _workspace_base(settings) / relative
        legacy = _legacy_desktop_workspace(settings, relative)
    return _ensure_workspace_directory(root, legacy)


def workspace_root_for(
    settings: Any,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    workspace_id: str | None = None,
) -> Path:
    """Return the project workspace root for one tenant and user.

    Standalone desktop defaults to ``~/Kinesis``. Shared deployments and
    other local runtimes keep the default at
    ``<configured base>/<tenant id>/<user id>``. Named workspace registrations
    are account-scoped and live outside the default workspace.

    Args:
        settings: The deployment settings in force.
        tenant_id: The owning tenant.
        user_id: The owning user.
        workspace_id: The workspace to resolve.  The default identifier, an
            empty identifier, and None all name the default workspace.

    Returns:
        The absolute workspace root, created if it does not exist.

    Raises:
        WorkspacePathError: When the workspace identifier is malformed.
        WorkspaceUnavailableError: When no workspace base is configured or
            the workspace could not be created.
    """
    root = account_workspace_root(settings, tenant_id, user_id)
    identifier = str(workspace_id or "").strip()
    if not identifier or identifier == DEFAULT_WORKSPACE_ID:
        return root
    if not is_workspace_id(identifier):
        raise WorkspacePathError(
            "A workspace identifier may hold only letters, digits, dashes, underscores, and dots."
        )
    named_root = named_workspaces_root(settings, tenant_id, user_id)
    named = named_root / identifier
    if not named.exists():
        # A registration whose directory was removed is reported as missing
        # rather than recreated, so the caller can tell the user the project
        # folder is gone instead of silently working in an empty directory.
        from app.deepspace.services.workspace_registry import WorkspaceRegistry

        registered = WorkspaceRegistry(settings, tenant_id, user_id).directory_for(identifier)
        if registered is None:
            raise WorkspaceUnavailableError(
                f"The workspace '{identifier}' is no longer registered."
            )
        named = registered

    # A custom project directory is never recreated: the workspace either
    # points at the directory the user chose or reports that it is missing.
    marker = named / ".deepspace-workspace.json"
    if marker.exists():
        try:
            import json

            data = json.loads(marker.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("path"):
                custom_path = Path(str(data["path"])).expanduser().resolve()
                if not custom_path.is_dir():
                    raise WorkspaceUnavailableError(
                        f"The project directory '{custom_path}' is missing."
                    )
                return custom_path
        except WorkspaceUnavailableError:
            raise
        except (OSError, ValueError):
            pass

    if not named.is_dir():
        raise WorkspaceUnavailableError(f"The workspace directory '{named}' is missing.")

    resolved = named.resolve()
    try:
        resolved.relative_to(named_root)
    except ValueError as exc:
        # A link out of the account's own directory would put the workspace
        # somewhere else, so it is refused.
        raise WorkspaceUnavailableError(
            "The project workspace could not be resolved for this account."
        ) from exc
    return resolved


def resolve_within_root(root: Path, raw: str) -> ResolvedWorkspacePath:
    """Resolve a caller-supplied path inside one workspace root.

    The path is normalized, joined to the root, and then checked again after
    symlink resolution.  The second check is what stops a symlink inside the
    workspace from pointing at a file outside it.

    Args:
        root: The workspace root that bounds the path.
        raw: The path the model supplied.

    Returns:
        The resolved path and its root-relative form.

    Raises:
        WorkspacePathError: When the path escapes the root.
    """
    relative = normalize_relative_path(raw)
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise WorkspacePathError(
            "The requested path resolves outside the project workspace."
        ) from exc
    return ResolvedWorkspacePath(absolute=candidate, relative=relative)


def is_within_root(root: Path, candidate: Path) -> bool:
    """Report whether an absolute path lies inside a workspace root.

    Args:
        root: The workspace root that bounds the path.
        candidate: The absolute path to test.

    Returns:
        True when the path is the root or lies under it.
    """
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def describe_workspace(root: Path) -> dict[str, Any]:
    """Describe a workspace root for a model-facing tool result.

    Args:
        root: The workspace root to describe.

    Returns:
        The root path and whether it currently exists.
    """
    return {
        "root": str(root),
        "exists": root.exists(),
        "writable": os.access(root, os.W_OK) if root.exists() else False,
    }


def remove_path(target: Path) -> None:
    """Delete a file or a directory tree inside the workspace.

    Args:
        target: The absolute path to remove.

    Raises:
        OSError: When the path cannot be removed.
    """
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    else:
        target.unlink(missing_ok=True)
