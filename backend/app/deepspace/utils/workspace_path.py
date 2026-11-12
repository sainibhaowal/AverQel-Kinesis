"""
DeepSpace Workspace Path Utilities - Python-native implementation of dsh-util-workspace-path.

Provides browser-safe workspace path and display helpers:
- Path resolution and normalization
- Display abbreviations
- Session-scoped file addresses
- Media URLs
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

# ============================================================================
# Types
# ============================================================================


@dataclass(frozen=True, slots=True)
class FileAddress:
    """A file resource address in one of two scopes."""

    # Session-scoped: resolved against a Session's workspace root
    scope: Literal["session", "absolute"]
    path: str  # Absolute or workspace-relative `/`-separated path; empty for workspace root
    session_id: str | None = None


# ============================================================================
# Path Classification
# ============================================================================


def is_windows_style_path(value: str) -> bool:
    """Check if a path uses a Windows drive or UNC prefix."""
    return bool(re.match(r"^[A-Za-z]:[/\\]", value) or value.startswith("\\\\"))


def is_absolute_workspace_path(path: str) -> bool:
    """
    Check if a path is absolute in either spelling: POSIX (`/a/b`) or Windows drive/UNC.
    """
    return path.startswith("/") or is_windows_style_path(path)


def is_relative_workspace_path(path: str) -> bool:
    """Check if a path is workspace-relative (not absolute)."""
    return not is_absolute_workspace_path(path)


# ============================================================================
# Path Resolution
# ============================================================================


def resolve_workspace_path(cwd: str | None, path: str) -> str:
    """
    Resolve a workspace-relative path into the Host-facing spelling.

    Args:
        cwd: Session workspace root, when known.
        path: Absolute or workspace-relative path; leading `./` segments are dropped when joining.

    Returns:
        An absolute path when a workspace root is available, otherwise the original path.
    """
    if is_absolute_workspace_path(path):
        return path
    if not cwd:
        return path

    # Determine separator from cwd
    separator = "\\" if is_windows_style_path(cwd) and "\\" in cwd else "/"
    base = cwd.rstrip("/\\")
    relative = re.sub(r"^(?:\.?[/\\])+", "", path)
    return f"{base}{separator}{relative}"


def normalize_path(path: str) -> str:
    """Normalize a path: backslashes to forward slashes, collapse multiple separators."""
    normalized = path.replace("\\", "/")
    # Collapse multiple slashes
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def relativize_to_cwd(text: str, cwd: str | None) -> str:
    """
    Strip the workspace root from a workspace-rooted absolute path (display only).

    Args:
        text: The path to shorten.
        cwd: Session workspace root; absent or empty leaves the path unchanged.

    Returns:
        The path relative to the workspace root, or unchanged when it is not rooted there.
    """
    if not cwd:
        return text
    root = cwd.rstrip("/\\")
    normalized_text = text.replace("\\", "/")
    root_normalized = root.replace("\\", "/")
    if normalized_text == root_normalized:
        return ""
    if normalized_text.startswith(root_normalized + "/"):
        return normalized_text[len(root_normalized) + 1 :]
    return text


# ============================================================================
# Display Helpers
# ============================================================================


def abbreviate_home_path(path: str, home: str | None = None) -> str:
    """
    Abbreviate a POSIX home directory for display.

    Args:
        path: Absolute or already-short display path.
        home: Host account home; absent skips abbreviation.

    Returns:
        `~` or `~/...` for the POSIX home and its descendants, otherwise `path`.
    """
    if not home:
        return path
    if is_windows_style_path(path) or is_windows_style_path(home):
        return path

    root = home.rstrip("/")
    if not root or root == "/":
        return path

    normalized_path = path.rstrip("/")
    if normalized_path == root:
        return "~"
    if normalized_path.startswith(root + "/"):
        return "~" + normalized_path[len(root) :]
    return path


def workspace_title_of(path: str) -> str:
    """
    Read the final non-empty segment of a workspace path for display.

    Args:
        path: Workspace directory path using POSIX or Windows separators.

    Returns:
        The final segment, or an empty string for a separator-only path.
    """
    # Check if path ends with separator (indicating "separator-only" intent)
    ends_with_separator = path.endswith("/") or path.endswith("\\")
    trimmed = path.rstrip("/\\")
    if not trimmed:
        return ""
    separator = max(trimmed.rfind("/"), trimmed.rfind("\\"))
    return trimmed[separator + 1 :] if not ends_with_separator else ""


def path_parts_of(path: str) -> tuple[str, str]:
    """
    Split a path for display: directories through last separator, and final segment.

    Args:
        path: File or directory path using POSIX or Windows separators.

    Returns:
        (directory_prefix, final_segment) - both may be empty.
    """
    trimmed = path.rstrip("/\\")
    if not trimmed:
        return ("", path)
    cut = max(trimmed.rfind("/"), trimmed.rfind("\\")) + 1
    return (trimmed[:cut], trimmed[cut:])


# ============================================================================
# File Addressing (dsh-resource://file/...)
# ============================================================================

FILE_ADDRESS_PREFIX = "dsh-resource://file/"


def _encode_segment(segment: str) -> str:
    """Component-encode one id or path segment, keeping `:` literal for drive letters."""
    return urllib.parse.quote(segment).replace("%3A", ":")


def _encode_path(path: str) -> str:
    """Encode a `/`-separated path segment by segment."""
    return "/".join(_encode_segment(seg) for seg in path.split("/") if seg != "")


def _decode_segment(segment: str) -> str:
    """Component-decode one segment."""
    return urllib.parse.unquote(segment).replace(":", ":")


def _is_drive_segment(segment: str | None) -> bool:
    """Check if a decoded first path segment is a Windows drive (`C:`)."""
    return segment is not None and bool(re.match(r"^[A-Za-z]:$", segment))


def session_file_address(session_id: str, path: str) -> str:
    """
    Build the address of a file read through one Session.

    Args:
        session_id: The Session whose Host workspace resolves the path.
        path: Absolute or workspace-relative path; backslashes normalized to `/`,
              leading `./` prefixes dropped.

    Returns:
        The `dsh-resource://file/session/<sessionId>/<path>` address.
    """
    normalized = path.replace("\\", "/").replace(r"^(?:\.\/)+", "")
    return f"{FILE_ADDRESS_PREFIX}session/{_encode_segment(session_id)}/{_encode_path(normalized)}"


def absolute_file_address(path: str) -> str:
    """
    Build the address of a file by its absolute path.

    Args:
        path: Absolute path; backslashes normalized to `/` and the leading `/` dropped,
              except that a UNC path (`//server/share`) keeps one empty first segment.

    Returns:
        The `dsh-resource://file/absolute/<path>` address.
    """
    normalized = path.replace("\\", "/")
    unc = normalized.startswith("//")
    absolute = normalized.lstrip("/")
    return f"{FILE_ADDRESS_PREFIX}absolute/{'/' if unc else ''}{_encode_path(absolute)}"


def parse_file_address(address: str) -> FileAddress | None:
    """
    Parse a file address back into its parts without resolving `.` or `..`.

    Query and fragment suffixes are ignored; encoded path segments are decoded.

    Args:
        address: A candidate address.

    Returns:
        The parts, or None when the string is not a `dsh-resource://file/`
        URI in a known scope with a path, or a segment is not validly encoded.
    """
    try:
        if not address.startswith(FILE_ADDRESS_PREFIX):
            return None

        end = address.find("?")
        if end == -1:
            end = address.find("#")
        if end == -1:
            end = len(address)

        parts = address[len(FILE_ADDRESS_PREFIX) : end].split("/")
        if not parts:
            return None

        scope = parts[0]
        rest = parts[1:]

        if scope == "session":
            if not rest:
                return None
            session_id = rest[0]
            if not session_id:
                return None
            path_segments = rest[1:]
            path = "/".join(_decode_segment(seg) for seg in path_segments)
            return FileAddress(scope="session", session_id=session_id, path=path)

        if scope == "absolute":
            if not rest:
                return None
            # Handle UNC: empty first segment means //server/share
            unc = rest[0] == "" and len(rest) > 1
            segments = rest[1:] if unc else rest
            path_segments = [_decode_segment(seg) for seg in segments]
            if not path_segments or path_segments[0] == "":
                return None

            if unc:
                path = "//" + "/".join(path_segments)
            else:
                path = "/".join(path_segments)
                # Windows drive letter gets no leading slash
                if path_segments and re.match(r"^[A-Za-z]:$", path_segments[0]):
                    path = path  # Already has drive letter
                else:
                    path = "/" + path

            return FileAddress(scope="absolute", path=path)

        return None
    except Exception:
        return None


def file_address_for(session_id: str, cwd: str | None, path: str) -> str:
    """
    Address a file as seen by a Session.

    A relative path or absolute path inside the Session's workspace becomes a
    `session`-scoped address; an absolute path outside it keeps its absolute path.
    """
    normalized = path.replace("\\", "/")
    if not is_absolute_workspace_path(normalized):
        return session_file_address(session_id, normalized)

    if not cwd:
        return absolute_file_address(normalized)

    root = cwd.replace("\\", "/").rstrip("/")
    if normalized == root:
        return session_file_address(session_id, "")
    if normalized.startswith(root + "/"):
        return session_file_address(session_id, normalized[len(root) + 1 :])
    return absolute_file_address(normalized)


# ============================================================================
# Media URLs
# ============================================================================


def file_media_url(base: str, path: str) -> str | None:
    """
    Address a decoded absolute file path through the authenticated file route.

    Args:
        base: HTTP(S) application base including deployment prefix, or `dsh-app://app/`.
        path: Native file path; URL escapes in authored Markdown must already be decoded.

    Returns:
        File URL, or None for unsupported transports and non-absolute paths.
    """
    if not (
        base.startswith("http:") or base.startswith("https:") or base.startswith("dsh-app://app/")
    ):
        return None
    if not is_absolute_workspace_path(path):
        return None
    # Reject UNC paths and control characters
    if path.startswith("\\\\") or re.search(r"[\x00-\x1f\x7f]", path):
        return None

    encoded_path = urllib.parse.quote(path, safe="/")
    return f"{base.rstrip('/')}/api/file?path={encoded_path}"


# ============================================================================
# Workspace Root Resolution
# ============================================================================


class WorkspaceUnavailableError(Exception):
    """Raised when a workspace root cannot be determined."""

    pass


def workspace_root_for(
    settings: Any,
    tenant_id: str,
    user_id: str,
    workspace_id: str | None = None,
) -> Path:
    """
    Resolve the workspace root for a tenant/user.

    Args:
        settings: Application settings with deepspace_workspace_root.
        tenant_id: Tenant identifier.
        user_id: User identifier.
        workspace_id: Optional workspace identifier for named workspaces.

    Returns:
        Path to the workspace root.

    Raises:
        WorkspaceUnavailableError: When the workspace root cannot be determined.
    """
    # Check for explicit root
    explicit_root = getattr(settings, "deepspace_workspace_root", None)
    if explicit_root:
        return Path(explicit_root).expanduser().resolve()

    # Default to AverQel home
    averqel_home = getattr(settings, "averqel_home", None)
    if not averqel_home:
        raise WorkspaceUnavailableError("No workspace root configured")

    base = Path(averqel_home).expanduser().resolve()

    if workspace_id:
        return base / "workspaces" / tenant_id / user_id / workspace_id

    return base / "workspaces" / tenant_id / user_id


def account_workspace_root(
    settings: Any,
    tenant_id: str,
    user_id: str,
) -> Path:
    """Get the account-scoped workspace root."""
    return workspace_root_for(settings, tenant_id, user_id)


def named_workspaces_root(
    settings: Any,
    tenant_id: str,
    user_id: str,
) -> Path:
    """Get the root for named workspaces."""
    return workspace_root_for(settings, tenant_id, user_id, workspace_id="named")


def is_workspace_id(value: str) -> bool:
    """Check if a string is a valid workspace identifier."""
    return bool(re.match(r"^[A-Za-z0-9._@+-]+$", value))


# ============================================================================
# Workspace Registration
# ============================================================================

REGISTRY_FILENAME = ".registry.json"
REGISTRY_LOCK_FILENAME = ".registry.lock"
REGISTRY_VERSION = 2
WORKSPACE_MARKER_FILENAME = ".deepspace-workspace.json"
QUARANTINE_DIRNAME = ".deleted"
QUARANTINE_PREFIX = "deleted-"

_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def _get_process_lock(path: str) -> threading.RLock:
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(path)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[path] = lock
        return lock


def _fsync_directory(path: Path) -> None:
    """Flush directory entry after rename."""
    if os.name == "nt":
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """
    Write JSON atomically with cross-process locking.

    Args:
        path: Destination path.
        payload: Document to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")

    # Create lock file with exclusive create
    fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, f"{os.getpid()}\n".encode())
    finally:
        os.close(fd)

    try:
        # Write temp file
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_path, path)
        _fsync_directory(path.parent)
    finally:
        try:
            os.unlink(lock_path)
        except OSError:
            pass


# ============================================================================
# Exports
# ============================================================================

__all__ = [
    # Types
    "FileAddress",
    "WorkspaceUnavailableError",
    # Classification
    "is_absolute_workspace_path",
    "is_relative_workspace_path",
    "is_workspace_id",
    # Resolution
    "resolve_workspace_path",
    "normalize_path",
    "relativize_to_cwd",
    # Display
    "abbreviate_home_path",
    "workspace_title_of",
    "path_parts_of",
    # Addressing
    "session_file_address",
    "absolute_file_address",
    "file_address_for",
    "parse_file_address",
    # Media
    "file_media_url",
    # Workspace root
    "workspace_root_for",
    "account_workspace_root",
    "named_workspaces_root",
    "WorkspaceUnavailableError",
    # Registry
    "REGISTRY_FILENAME",
    "REGISTRY_LOCK_FILENAME",
    "REGISTRY_VERSION",
    "WORKSPACE_MARKER_FILENAME",
    "QUARANTINE_DIRNAME",
    "QUARANTINE_PREFIX",
    "atomic_write_json",
]
