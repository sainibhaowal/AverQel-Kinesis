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
import os
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

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


__all__ = [
    "BINARY_PROBE_BYTES",
    "SKIPPED_DIRECTORIES",
    "TRUNCATION_NOTICE",
    "SnapshotFile",
    "SnapshotStore",
    "WorkspaceSnapshot",
    "changed_paths",
    "collect_snapshot_files",
    "diff_snapshots",
    "snapshot_store",
    "truncate_diff",
]
