"""Content-addressed snapshots of a project workspace.

A snapshot records one hash per text file under a workspace root, so two
snapshots of the same tree say which files were added, removed, or changed
without keeping a copy of the content.  The diff tool uses the newest
snapshot as the "before" side when the workspace is not a git repository.

The store keeps the newest snapshot per tenant, user, and workspace root.
It is used from the event loop, so its methods are synchronous and need no
lock, and it bounds its memory by dropping the oldest snapshot once too many
workspaces are active.
"""

from __future__ import annotations

import difflib
import hashlib
import mimetypes
import os
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The directory names the walk never enters, in addition to the hidden ones.
SKIPPED_DIRECTORIES = frozenset(
    {
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "site-packages",
        "venv",
        "virtualenv",
    }
)

#: The number of bytes read to decide whether a file holds binary content.
BINARY_PROBE_BYTES = 8192

#: The note appended to a diff that did not fit its byte budget.
TRUNCATION_NOTICE = "\n... the diff was truncated; some changes are not shown ...\n"


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    """One text file in a workspace snapshot.

    Attributes:
        relative_path: The path relative to the workspace root, in POSIX form.
        content_hash: The lowercase hexadecimal SHA-256 digest of the bytes.
        size_bytes: The file's length in bytes.
    """

    relative_path: str
    content_hash: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    """One recorded state of a workspace.

    Attributes:
        id: The content address of the recorded file set.
        root: The workspace root the snapshot was taken in.
        created_at: The Unix timestamp of the moment the snapshot was taken.
        files: The recorded files, ordered by their root-relative path.
    """

    id: str
    root: str
    created_at: float
    files: tuple[SnapshotFile, ...]


def _skip_directory(name: str) -> bool:
    """Report whether the walk must not enter a directory.

    Args:
        name: The directory's name.

    Returns:
        True when the directory is hidden or holds build output rather than
        project sources.
    """
    return name.startswith(".") or name in SKIPPED_DIRECTORIES


def _is_within(root: Path, candidate: Path) -> bool:
    """Report whether a path lies inside the workspace root.

    Args:
        root: The resolved workspace root.
        candidate: The resolved path to test.

    Returns:
        True when the path is the root or lies under it.
    """
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _snapshot_file(path: Path, root: Path, max_bytes_per_file: int) -> SnapshotFile | None:
    """Return the snapshot entry for one file, or None when it is not one.

    A file that is too large, holds binary content, or cannot be read is not
    part of the snapshot.

    Args:
        path: The file to read.
        root: The resolved workspace root.
        max_bytes_per_file: The largest file the snapshot records.

    Returns:
        The snapshot entry, or None when the file is skipped.
    """
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if not _is_within(root, resolved):
        return None
    try:
        size_bytes = resolved.stat().st_size
    except OSError:
        return None
    if size_bytes > max_bytes_per_file:
        return None
    try:
        data = resolved.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:BINARY_PROBE_BYTES]:
        return None
    return SnapshotFile(
        relative_path=resolved.relative_to(root).as_posix(),
        content_hash=hashlib.sha256(data).hexdigest(),
        size_bytes=size_bytes,
    )


def collect_snapshot_files(
    root: Path,
    *,
    max_files: int = 2000,
    max_bytes_per_file: int = 512_000,
) -> list[SnapshotFile]:
    """Collect one snapshot entry per text file under a workspace root.

    The walk skips hidden directories, build and virtual-environment
    directories, files that are too large, and files that hold binary
    content.

    Args:
        root: The workspace root to walk.
        max_files: The largest number of files one collection records.
        max_bytes_per_file: The largest file one collection reads.

    Returns:
        The collected files, ordered by their root-relative path.
    """
    base = Path(root).resolve()
    collected: list[SnapshotFile] = []
    for directory, subdirectories, filenames in os.walk(base, topdown=True, followlinks=False):
        subdirectories[:] = sorted(name for name in subdirectories if not _skip_directory(name))
        for filename in sorted(filenames):
            if len(collected) >= max_files:
                return sorted(collected, key=lambda item: item.relative_path)
            entry = _snapshot_file(Path(directory) / filename, base, max_bytes_per_file)
            if entry is not None:
                collected.append(entry)
    return sorted(collected, key=lambda item: item.relative_path)


def changed_paths(before: Sequence[SnapshotFile], after: Sequence[SnapshotFile]) -> list[str]:
    """Return the paths that differ between two snapshot file sets.

    Args:
        before: The earlier snapshot's files.
        after: The later snapshot's files.

    Returns:
        The added, removed, and changed paths, ordered by path.
    """
    before_map = {item.relative_path: item for item in before}
    after_map = {item.relative_path: item for item in after}
    paths = {
        path
        for path, item in after_map.items()
        if path not in before_map or before_map[path].content_hash != item.content_hash
    }
    paths.update(path for path in before_map if path not in after_map)
    return sorted(paths)


def truncate_diff(text: str, max_bytes: int) -> tuple[str, bool]:
    """Cut a diff body at its byte budget and say that it was cut.

    The notice is part of the budget, so the result never grows past it.

    Args:
        text: The diff body.
        max_bytes: The largest result the caller accepts.

    Returns:
        The bounded text and whether it was cut.
    """
    if len(text) <= max_bytes:
        return text, False
    budget = max(0, max_bytes - len(TRUNCATION_NOTICE))
    head = text[:budget]
    cut = head.rfind("\n")
    if cut > 0:
        head = head[: cut + 1]
    return head + TRUNCATION_NOTICE, True


def _read_lines(path: Path) -> list[str]:
    """Read one file as the newline-terminated lines a diff expects.

    Args:
        path: The file to read.

    Returns:
        The file's lines, or an empty list when it cannot be read.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines(keepends=True)
    return [line if line.endswith("\n") else f"{line}\n" for line in lines]


def _file_section(
    relative_path: str,
    before: SnapshotFile | None,
    after: SnapshotFile | None,
    root: Path,
) -> str:
    """Return the unified-diff section for one changed file.

    A snapshot keeps a hash and a size, not the content, so a file whose
    content changed is reported with those two values instead of a line
    diff.  A file the snapshot did not have is shown in full, because its
    current content is the whole of the change.

    Args:
        relative_path: The changed file's path relative to the root.
        before: The file's entry in the earlier snapshot, when it had one.
        after: The file's entry in the later snapshot, when it has one.
        root: The workspace root the files live in.

    Returns:
        The unified-diff section for the file.
    """
    if before is None:
        assert after is not None
        lines = _read_lines(root / after.relative_path)
        return "".join(
            difflib.unified_diff(
                [],
                lines,
                fromfile="/dev/null",
                tofile=f"b/{after.relative_path}",
            )
        )
    if after is None:
        removed = f"- {relative_path} ({before.size_bytes} bytes)\n"
        return "".join(
            difflib.unified_diff(
                [removed],
                [],
                fromfile=f"a/{relative_path}",
                tofile="/dev/null",
            )
        )
    old = f"- {relative_path} ({before.size_bytes} bytes, sha256 {before.content_hash[:12]})\n"
    new = f"+ {relative_path} ({after.size_bytes} bytes, sha256 {after.content_hash[:12]})\n"
    return "".join(
        difflib.unified_diff(
            [old],
            [new],
            fromfile=f"a/{relative_path}",
            tofile=f"b/{relative_path}",
        )
    )


def diff_snapshots(
    before: Sequence[SnapshotFile],
    after: Sequence[SnapshotFile],
    *,
    root: Path,
    max_diff_bytes: int = 400_000,
) -> str:
    """Render the difference between two snapshot file sets.

    Args:
        before: The earlier snapshot's files.
        after: The later snapshot's files.
        root: The workspace root the files live in.
        max_diff_bytes: The largest result the caller accepts.

    Returns:
        The unified diff of the two file sets, cut at the byte budget with a
        notice that says it was cut.
    """
    base = Path(root).resolve()
    before_map = {item.relative_path: item for item in before}
    after_map = {item.relative_path: item for item in after}
    sections: list[str] = []
    for relative_path in changed_paths(before, after):
        sections.append(
            _file_section(
                relative_path,
                before_map.get(relative_path),
                after_map.get(relative_path),
                base,
            )
        )
    text, _truncated = truncate_diff("".join(sections), max_diff_bytes)
    return text


def _snapshot_id(files: Sequence[SnapshotFile]) -> str:
    """Return the content address of one snapshot's file set.

    Args:
        files: The snapshot's files, ordered by their root-relative path.

    Returns:
        The lowercase hexadecimal SHA-256 digest of the file set.
    """
    digest = hashlib.sha256()
    for item in files:
        digest.update(item.relative_path.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(item.content_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


class SnapshotStore:
    """The newest snapshot for each tenant, user, and workspace root.

    The store is used from the event loop, so its methods are synchronous
    and need no lock.
    """

    #: The largest number of workspaces the store keeps a snapshot for.
    MAX_ENTRIES = 64

    def __init__(self) -> None:
        """Create an empty store."""
        self._snapshots: dict[tuple[str, str, str], WorkspaceSnapshot] = {}

    def save(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        root: str,
        files: Sequence[SnapshotFile],
    ) -> WorkspaceSnapshot:
        """Record a snapshot as the newest one for one workspace.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            root: The workspace root the snapshot covers.
            files: The files the snapshot records.

        Returns:
            The recorded snapshot.
        """
        ordered = tuple(sorted(files, key=lambda item: item.relative_path))
        snapshot = WorkspaceSnapshot(
            id=_snapshot_id(ordered),
            root=str(root),
            created_at=time.time(),
            files=ordered,
        )
        self._snapshots[(str(tenant_id), str(user_id), str(root))] = snapshot
        self._evict()
        return snapshot

    def latest(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, root: str
    ) -> WorkspaceSnapshot | None:
        """Return the newest snapshot for one workspace.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            root: The workspace root the snapshot covers.

        Returns:
            The newest snapshot, or None when the workspace has none.
        """
        return self._snapshots.get((str(tenant_id), str(user_id), str(root)))

    def clear(self) -> None:
        """Drop every snapshot the store holds."""
        self._snapshots.clear()

    def _evict(self) -> None:
        """Drop the oldest snapshots until the store is within its bound."""
        while len(self._snapshots) > self.MAX_ENTRIES:
            oldest = min(self._snapshots, key=lambda key: self._snapshots[key].created_at)
            del self._snapshots[oldest]


#: The process-wide snapshot store.
_STORE = SnapshotStore()


def snapshot_store() -> SnapshotStore:
    """Return the process-wide snapshot store.

    Returns:
        The store every workspace snapshot is recorded in.
    """
    return _STORE


@dataclass(frozen=True, slots=True)
class WorkspaceStatEntry:
    """Lightweight stat record for any file (text or binary) inside a workspace."""

    relative_path: str
    size_bytes: int
    mtime_ns: int


_EXTENSION_MIME_OVERRIDES: dict[str, str] = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".ppm": "image/x-portable-pixmap",
    ".pgm": "image/x-portable-graymap",
    ".pbm": "image/x-portable-bitmap",
    ".pdf": "application/pdf",
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".cjs": "application/javascript",
    ".ts": "text/typescript",
    ".tsx": "text/tsx",
    ".jsx": "text/jsx",
    ".json": "application/json",
    ".md": "text/markdown",
    ".mdx": "text/markdown",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
    ".toml": "application/toml",
    ".xml": "application/xml",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".docm": "application/vnd.ms-word.document.macroenabled.12",
    ".dot": "application/msword",
    ".dotx": "application/vnd.openxmlformats-officedocument.wordprocessingml.template",
    ".dotm": "application/vnd.ms-word.template.macroenabled.12",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ott": "application/vnd.oasis.opendocument.text-template",
    ".odm": "application/vnd.oasis.opendocument.text-master",
    ".oth": "application/vnd.oasis.opendocument.text-web",
    ".rtf": "application/rtf",
    ".ppt": "application/vnd.ms-powerpoint",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".xlsm": "application/vnd.ms-excel.sheet.macroenabled.12",
    ".xlt": "application/vnd.ms-excel",
    ".xltx": "application/vnd.openxmlformats-officedocument.spreadsheetml.template",
    ".xltm": "application/vnd.ms-excel.template.macroenabled.12",
    ".xlm": "application/vnd.ms-excel",
    ".xla": "application/vnd.ms-excel",
    ".xlw": "application/vnd.ms-excel",
    ".xlsb": "application/vnd.ms-excel.sheet.binary.macroenabled.12",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".ots": "application/vnd.oasis.opendocument.spreadsheet-template",
    ".pptm": "application/vnd.ms-powerpoint.presentation.macroenabled.12",
    ".pot": "application/vnd.ms-powerpoint",
    ".potx": "application/vnd.openxmlformats-officedocument.presentationml.template",
    ".potm": "application/vnd.ms-powerpoint.template.macroenabled.12",
    ".pps": "application/vnd.ms-powerpoint",
    ".ppsx": "application/vnd.openxmlformats-officedocument.presentationml.slideshow",
    ".ppsm": "application/vnd.ms-powerpoint.slideshow.macroenabled.12",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".otp": "application/vnd.oasis.opendocument.presentation-template",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odg": "application/vnd.oasis.opendocument.graphics",
    ".otg": "application/vnd.oasis.opendocument.graphics-template",
    ".odc": "application/vnd.oasis.opendocument.chart",
    ".otc": "application/vnd.oasis.opendocument.chart-template",
    ".odf": "application/vnd.oasis.opendocument.formula",
    ".otf": "application/vnd.oasis.opendocument.formula-template",
    ".odi": "application/vnd.oasis.opendocument.image",
    ".oti": "application/vnd.oasis.opendocument.image-template",
    ".zip": "application/zip",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}


def mime_type_for_path(path: Path) -> str:
    """Return a normalized MIME type for a workspace file path."""
    suffix = path.suffix.lower()
    if suffix in _EXTENSION_MIME_OVERRIDES:
        return _EXTENSION_MIME_OVERRIDES[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def collect_workspace_stat_entries(
    root: Path,
    *,
    max_files: int = 4000,
) -> list[WorkspaceStatEntry]:
    """Collect lightweight stat metadata for every file (text or binary) under a root."""
    base = Path(root).resolve()
    if not base.is_dir():
        return []
    collected: list[WorkspaceStatEntry] = []
    for directory, subdirectories, filenames in os.walk(base, topdown=True, followlinks=False):
        subdirectories[:] = sorted(name for name in subdirectories if not _skip_directory(name))
        for filename in sorted(filenames):
            if len(collected) >= max_files:
                return sorted(collected, key=lambda item: item.relative_path)
            candidate = Path(directory) / filename
            try:
                resolved = candidate.resolve()
                if not _is_within(base, resolved):
                    continue
                stat_result = resolved.stat()
                if not resolved.is_file():
                    continue
                collected.append(
                    WorkspaceStatEntry(
                        relative_path=resolved.relative_to(base).as_posix(),
                        size_bytes=stat_result.st_size,
                        mtime_ns=stat_result.st_mtime_ns,
                    )
                )
            except OSError:
                continue
    return sorted(collected, key=lambda item: item.relative_path)


def _normalize_workspace_candidate(root: Path, raw_path: str) -> str | None:
    """Normalize a workspace path candidate and verify it resolves to a file inside root."""
    cleaned = str(raw_path or "").strip()
    if not cleaned:
        return None
    base = root.resolve()
    path_obj = Path(cleaned)
    try:
        resolved = path_obj.resolve() if path_obj.is_absolute() else (base / cleaned).resolve()
        if not _is_within(base, resolved) or not resolved.is_file():
            return None
        return resolved.relative_to(base).as_posix()
    except OSError:
        return None


def changed_file_entries(
    before: Sequence[WorkspaceStatEntry],
    after: Sequence[WorkspaceStatEntry],
    *,
    root: Path,
    presented_paths: Sequence[str] = (),
    touched_paths: Sequence[str] = (),
    max_files: int = 32,
) -> list[dict[str, Any]]:
    """Return metadata records for files created, modified, or presented during a turn."""
    base = Path(root).resolve()
    before_map = {item.relative_path: item for item in before}
    after_map = {item.relative_path: item for item in after}

    presented_set: set[str] = set()
    for raw in presented_paths:
        norm = _normalize_workspace_candidate(base, raw)
        if norm:
            presented_set.add(norm)

    touched_set: set[str] = set()
    for raw in touched_paths:
        norm = _normalize_workspace_candidate(base, raw)
        if norm:
            touched_set.add(norm)

    candidates: dict[str, str] = {}
    for rel_path, after_entry in after_map.items():
        if rel_path in presented_set:
            candidates[rel_path] = "presented"
        elif rel_path not in before_map:
            candidates[rel_path] = "created"
        else:
            prev = before_map[rel_path]
            if (
                after_entry.size_bytes != prev.size_bytes
                or after_entry.mtime_ns != prev.mtime_ns
                or rel_path in touched_set
            ):
                candidates[rel_path] = "modified"

    for rel_path in presented_set | touched_set:
        if rel_path not in candidates:
            candidates[rel_path] = (
                "presented"
                if rel_path in presented_set
                else ("created" if rel_path not in before_map else "modified")
            )

    priority = {"presented": 0, "created": 1, "modified": 2}
    ordered_paths = sorted(
        candidates,
        key=lambda rel: (priority.get(candidates[rel], 3), rel),
    )[:max_files]

    results: list[dict[str, Any]] = []
    for rel_path in ordered_paths:
        entry = after_map.get(rel_path)
        size_bytes = entry.size_bytes if entry is not None else 0
        if entry is None:
            try:
                size_bytes = (base / rel_path).stat().st_size
            except OSError:
                continue
        path_obj = Path(rel_path)
        results.append(
            {
                "path": rel_path,
                "name": path_obj.name,
                "extension": path_obj.suffix.lstrip(".").lower(),
                "mime_type": mime_type_for_path(path_obj),
                "size_bytes": size_bytes,
                "change_type": candidates[rel_path],
            }
        )
    return results


__all__ = [
    "BINARY_PROBE_BYTES",
    "SKIPPED_DIRECTORIES",
    "TRUNCATION_NOTICE",
    "SnapshotFile",
    "SnapshotStore",
    "WorkspaceSnapshot",
    "WorkspaceStatEntry",
    "changed_file_entries",
    "changed_paths",
    "collect_snapshot_files",
    "collect_workspace_stat_entries",
    "diff_snapshots",
    "mime_type_for_path",
    "snapshot_store",
    "truncate_diff",
]
