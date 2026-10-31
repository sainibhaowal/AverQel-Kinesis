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
it. The standalone desktop app places its default workspace under its single
application data root; a user may instead attach any existing project folder.
Named workspace registrations live under the application data root without
moving or copying the selected project.
"""

from __future__ import annotations

import os
import shutil
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

#: The directory under an account's root that holds the named workspaces.
NAMED_WORKSPACES_DIRNAME = ".workspaces"

#: The characters a workspace identifier may contain.
WORKSPACE_ID_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_.")

#: The largest workspace identifier, in characters.
MAX_WORKSPACE_ID_LENGTH = 64


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
    """Return the directory that holds every account's workspaces.

    Args:
        settings: The deployment settings in force.

    Returns:
        The absolute base directory.

    Raises:
        WorkspaceUnavailableError: When no workspace base is configured.
    """
    base = str(getattr(settings, "deepspace_workspace_root", "") or "").strip()
    if base:
        return Path(base).expanduser()
    from app.core.averqel_home import get_averqel_home

    return get_averqel_home() / "workspaces"


def account_workspace_root(settings: Any, tenant_id: uuid.UUID, user_id: uuid.UUID) -> Path:
    """Return one account's default workspace.

    The root is ``<configured base>/<tenant id>/<user id>``.  It is the
    workspace a conversation works in when it names none, and it is the
    account's own directory, so one account's workspaces never reach another
    account's.

    Args:
        settings: The deployment settings in force.
        tenant_id: The owning tenant.
        user_id: The owning user.

    Returns:
        The absolute account root, created if it does not exist.

    Raises:
        WorkspaceUnavailableError: When no workspace base is configured.
    """
    root = _workspace_base(settings) / str(tenant_id) / str(user_id)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkspaceUnavailableError(
            "The project workspace could not be created for this account."
        ) from exc
    return root.resolve()


def named_workspaces_root(settings: Any, tenant_id: uuid.UUID, user_id: uuid.UUID) -> Path:
    """Return the directory that holds one account's named workspaces.

    The named workspaces sit beside the account's default workspace, not
    inside it, so a workspace never appears in another workspace's file
    tree and a conversation working in one cannot reach the others through
    the file tools.

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
    root = _workspace_base(settings) / str(tenant_id) / f"{user_id}{NAMED_WORKSPACES_DIRNAME}"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkspaceUnavailableError(
            "The project workspace could not be created for this account."
        ) from exc
    return root.resolve()


def workspace_root_for(
    settings: Any,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    workspace_id: str | None = None,
) -> Path:
    """Return the project workspace root for one tenant and user.

    The default workspace is ``<configured base>/<tenant id>/<user id>``.  A
    named workspace is a directory of its own beside it, at
    ``<configured base>/<tenant id>/<user id>workspaces/<workspace id>``.
    A deployment without a configured base has no workspace, and the file and
    shell tools report that rather than silently writing somewhere else.

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
    try:
        named.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkspaceUnavailableError(
            "The project workspace could not be created for this account."
        ) from exc

    # Check if a custom directory path was recorded for this workspace
    marker = named / ".deepspace-workspace.json"
    if marker.exists():
        try:
            import json

            data = json.loads(marker.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("path"):
                custom_path = Path(str(data["path"])).expanduser().resolve()
                custom_path.mkdir(parents=True, exist_ok=True)
                return custom_path
        except (OSError, ValueError):
            pass

    resolved = named.resolve()
    try:
        resolved.relative_to(named_root)
    except ValueError as exc:
        # A link out of the account's own directory would put the workspace
        # somewhere else, so it is refused.
        raise WorkspaceUnavailableError(
            "The project workspace could not be created for this account."
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
