"""The per-turn changed-file record and the deliverables a conversation declares.

A coding agent works in turns: each turn reads the workspace, changes some
files, and answers.  The workspace diff tool answers "what changed right
now", but it compares against one baseline snapshot, so turn N's change list
is gone by turn N+1.  This module keeps that list per turn, so a later turn
and the user can still see which files a turn touched and how much their
content changed.

The same module holds the deliverables a turn declares: the files the agent
wrote that the user is meant to open.  A deliverable is a declaration, not a
copy; the file stays where it is in the workspace, and the record says which
conversation and turn it belongs to.

The store is process-wide and bounded: it keeps the last turns of a
conversation, the last declarations of a conversation, and a small cache of
file contents so the changed-line count is exact for the files the store has
seen before.  It is used from the event loop, so its methods are synchronous
and need no lock.
"""

from __future__ import annotations

import difflib
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.deepspace.services.workspace_root import WorkspacePathError, normalize_relative_path
from app.deepspace.services.workspace_snapshot import SnapshotFile, changed_paths

#: The largest number of turns the store keeps for one conversation.
MAX_TURNS_PER_CONVERSATION = 32

#: The largest number of declarations the store keeps for one conversation.
MAX_DELIVERABLES_PER_CONVERSATION = 64

#: The largest number of conversations the store keeps records for.
MAX_CONVERSATIONS = 32

#: The largest number of file contents the store caches for one conversation.
MAX_CACHED_FILES = 200

#: The largest number of characters the store caches for one conversation.
MAX_CACHED_CHARS = 500_000

#: The largest number of unchanged files the store caches in one turn.
MAX_CACHE_REFRESH_PER_RECORD = 100

#: The largest file the store diffs line by line.
MAX_DIFF_LINES = 5_000


class DeliverableError(ValueError):
    """A declaration named a path that is not inside the project workspace."""


@dataclass(frozen=True, slots=True)
class ChangedFile:
    """One file whose content changed during a turn.

    Attributes:
        path: The path relative to the workspace root, in POSIX form.
        changed_lines: The number of lines the file's content changed by.
    """

    path: str
    changed_lines: int

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable projection of this change."""
        return {"path": self.path, "changed_lines": self.changed_lines}


@dataclass(frozen=True, slots=True)
class TurnChange:
    """The files one turn changed.

    Attributes:
        conversation_id: The conversation the turn belongs to.
        turn: The turn's number, as the caller numbered it.
        files: The files that changed, ordered by their path.
        recorded_at: The Unix timestamp of the moment the record was made.
    """

    conversation_id: uuid.UUID
    turn: int
    files: tuple[ChangedFile, ...]
    recorded_at: float

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable projection of this record."""
        return {
            "conversation_id": str(self.conversation_id),
            "turn": self.turn,
            "files": [item.as_dict() for item in self.files],
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True, slots=True)
class DeliverableRecord:
    """One file a turn declared as a deliverable.

    Attributes:
        path: The path relative to the workspace root, in POSIX form.
        conversation_id: The conversation the declaration belongs to.
        turn: The turn the declaration was made in.
        declared_at: The Unix timestamp of the moment it was declared.
    """

    path: str
    conversation_id: uuid.UUID
    turn: int
    declared_at: float

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable projection of this declaration."""
        return {
            "path": self.path,
            "conversation_id": str(self.conversation_id),
            "turn": self.turn,
            "declared_at": self.declared_at,
        }


def _changed_line_count(previous: str | None, current: str | None) -> int:
    """Count the lines one file's content changed by.

    Args:
        previous: The content the file held before the change, when the store
            knows it.
        current: The content the file holds after the change, when it still
            exists.

    Returns:
        The number of lines added plus the number of lines removed.  A file
        the store has never seen counts every line as added, and a file that
        was removed counts every line it held as removed.  A file too large
        to diff line by line reports the difference in its line count.
    """
    if previous is None:
        return len(current.splitlines()) if current is not None else 0
    if current is None:
        return len(previous.splitlines())
    previous_lines = previous.splitlines()
    current_lines = current.splitlines()
    if len(previous_lines) > MAX_DIFF_LINES or len(current_lines) > MAX_DIFF_LINES:
        return abs(len(current_lines) - len(previous_lines))
    matcher = difflib.SequenceMatcher(None, previous_lines, current_lines)
    changed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in {"replace", "delete"}:
            changed += i2 - i1
        if tag in {"replace", "insert"}:
            changed += j2 - j1
    return changed


def _read_text(root: Path, relative_path: str) -> str | None:
    """Read one workspace file as text.

    Args:
        root: The workspace root the file lives in.
        relative_path: The file's path relative to the root.

    Returns:
        The file's content, or None when it is not a readable text file
        inside the root.
    """
    try:
        base = root.resolve()
        target = (base / relative_path).resolve()
        target.relative_to(base)
    except (OSError, ValueError):
        return None
    try:
        if not target.is_file():
            return None
        return target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


class DeliverableStore:
    """The turn changes and the declarations of every conversation.

    The store is used from the event loop, so its methods are synchronous and
    need no lock.  It bounds its memory by dropping the oldest turns and
    declarations of a conversation, the least recently used file contents, and
    the oldest conversations when too many are active.
    """

    def __init__(self) -> None:
        """Create an empty store."""
        self._changes: dict[uuid.UUID, list[TurnChange]] = {}
        self._deliverables: dict[uuid.UUID, list[DeliverableRecord]] = {}
        self._contents: dict[uuid.UUID, dict[str, tuple[str, str]]] = {}
        self._current_turn: dict[uuid.UUID, int] = {}
        self._roots: dict[uuid.UUID, Path] = {}
        self._conversations: list[uuid.UUID] = []

    # ------------------------------------------------------------------
    # Recording a turn
    # ------------------------------------------------------------------

    def record_turn_changes(
        self,
        conversation_id: uuid.UUID,
        turn: int,
        before: Sequence[SnapshotFile],
        after: Sequence[SnapshotFile],
        *,
        root: Path,
    ) -> TurnChange:
        """Record the files one turn changed.

        The two snapshot file sets say which paths differ.  The changed-line
        count of each path is the number of lines its content gained and
        lost, counted against the content the store held the last time it
        recorded that file.  A file the store has never seen counts every
        line as added, because the store cannot know what it held before.

        Args:
            conversation_id: The conversation the turn belongs to.
            turn: The turn's number, as the caller numbered it.
            before: The workspace files as they were before the turn.
            after: The workspace files as they are after the turn.
            root: The workspace root the snapshots were taken in.

        Returns:
            The recorded turn.
        """
        self._touch(conversation_id)
        self._roots[conversation_id] = root
        after_map = {item.relative_path: item for item in after}
        cached = self._contents.setdefault(conversation_id, {})
        contents: dict[str, str | None] = {}
        files: list[ChangedFile] = []
        for path in changed_paths(before, after):
            previous = cached.get(path)
            previous_text = previous[1] if previous is not None else None
            current_text = _read_text(root, path) if path in after_map else None
            contents[path] = current_text
            files.append(
                ChangedFile(
                    path=path,
                    changed_lines=_changed_line_count(previous_text, current_text),
                )
            )
        record = TurnChange(
            conversation_id=conversation_id,
            turn=turn,
            files=tuple(files),
            recorded_at=time.time(),
        )
        records = self._changes.setdefault(conversation_id, [])
        records.append(record)
        while len(records) > MAX_TURNS_PER_CONVERSATION:
            records.pop(0)
        self._refresh_contents(conversation_id, cached, after, contents)
        self._current_turn[conversation_id] = max(self.current_turn(conversation_id), turn + 1)
        return record

    def _refresh_contents(
        self,
        conversation_id: uuid.UUID,
        cached: dict[str, tuple[str, str]],
        after: Sequence[SnapshotFile],
        contents: dict[str, str | None],
    ) -> None:
        """Bring the cached file contents up to the state the turn ended in.

        Args:
            conversation_id: The conversation whose cache is refreshed.
            cached: The conversation's cache.
            after: The workspace files as they are after the turn.
            contents: The contents already read for the files that changed.
        """
        after_map = {item.relative_path: item for item in after}
        for path in list(cached):
            if path not in after_map:
                del cached[path]
        for entry in after:
            path = entry.relative_path
            known = cached.get(path)
            if known is not None and known[0] == entry.content_hash:
                continue
            if path in contents:
                text = contents[path]
            else:
                continue
            if text is None:
                cached.pop(path, None)
                continue
            cached[path] = (entry.content_hash, text)
        self._cache_unchanged(conversation_id, cached, after)
        self._evict_contents(cached)

    def _cache_unchanged(
        self,
        conversation_id: uuid.UUID,
        cached: dict[str, tuple[str, str]],
        after: Sequence[SnapshotFile],
    ) -> None:
        """Cache the content of the files a turn left alone, a few at a time.

        A file the store has never seen counts every line as changed, because
        the store cannot know what it held before.  Caching the content of
        files that did not change makes the next change of one of them exact.
        The store reads a bounded number of them per turn, so a large
        workspace costs a bounded amount of extra work.

        Args:
            conversation_id: The conversation whose cache is filled.
            cached: The conversation's cache.
            after: The workspace files as they are after the turn.
        """
        root = self._root_for(conversation_id)
        read = 0
        for entry in after:
            if read >= MAX_CACHE_REFRESH_PER_RECORD:
                return
            known = cached.get(entry.relative_path)
            if known is not None and known[0] == entry.content_hash:
                continue
            text = _read_text(root, entry.relative_path)
            if text is None:
                continue
            cached[entry.relative_path] = (entry.content_hash, text)
            read += 1

    def _root_for(self, conversation_id: uuid.UUID) -> Path:
        """Return the workspace root one conversation's files live in.

        The store keeps no root of its own: the caller that records a turn
        names the root, and the cache is refreshed with it.

        Args:
            conversation_id: The conversation whose root is wanted.

        Returns:
            The root the caller last named, or the process working directory.
        """
        return self._roots.get(conversation_id, Path.cwd())

    def _evict_contents(self, cached: dict[str, tuple[str, str]]) -> None:
        """Drop the least recently used file contents until the cache fits.

        Args:
            cached: The conversation's cache.
        """
        characters = sum(len(text) for _hash, text in cached.values())
        while cached and (len(cached) > MAX_CACHED_FILES or characters > MAX_CACHED_CHARS):
            path, (_hash, text) = next(iter(cached.items()))
            del cached[path]
            characters -= len(text)

    # ------------------------------------------------------------------
    # Declaring a deliverable
    # ------------------------------------------------------------------

    def declare_deliverable(
        self, conversation_id: uuid.UUID, turn: int, path: str
    ) -> DeliverableRecord:
        """Record one file as a deliverable of a turn.

        Args:
            conversation_id: The conversation the declaration belongs to.
            turn: The turn the declaration is made in.
            path: The workspace-relative path of the file.

        Returns:
            The recorded declaration.

        Raises:
            DeliverableError: When the path is not a path inside the
                project workspace.
        """
        try:
            relative = normalize_relative_path(path)
        except WorkspacePathError as exc:
            raise DeliverableError(str(exc)) from exc
        self._touch(conversation_id)
        record = DeliverableRecord(
            path=relative,
            conversation_id=conversation_id,
            turn=turn,
            declared_at=time.time(),
        )
        records = self._deliverables.setdefault(conversation_id, [])
        for index, existing in enumerate(records):
            if existing.path == relative and existing.turn == turn:
                records[index] = record
                break
        else:
            records.append(record)
        while len(records) > MAX_DELIVERABLES_PER_CONVERSATION:
            records.pop(0)
        return record

    # ------------------------------------------------------------------
    # Reading the store
    # ------------------------------------------------------------------

    def turn_changes(self, conversation_id: uuid.UUID) -> tuple[TurnChange, ...]:
        """Return the recorded turns of one conversation, oldest first.

        Args:
            conversation_id: The conversation to read.

        Returns:
            The recorded turns.
        """
        return tuple(self._changes.get(conversation_id, ()))

    def deliverables(self, conversation_id: uuid.UUID) -> tuple[DeliverableRecord, ...]:
        """Return the declarations of one conversation, oldest first.

        Args:
            conversation_id: The conversation to read.

        Returns:
            The recorded declarations.
        """
        return tuple(self._deliverables.get(conversation_id, ()))

    def current_turn(self, conversation_id: uuid.UUID) -> int:
        """Return the turn one conversation is in.

        The turn after the last recorded one is the turn in progress, so a
        tool that declares a file during a turn attributes it to that turn.

        Args:
            conversation_id: The conversation to read.

        Returns:
            The turn in progress, or 1 when the conversation has no record.
        """
        return self._current_turn.get(conversation_id, 1)

    def set_current_turn(self, conversation_id: uuid.UUID, turn: int) -> None:
        """Set the turn one conversation is in.

        A caller that numbers its turns from zero sets the first turn here,
        so a declaration made in it is attributed to the right turn.

        Args:
            conversation_id: The conversation to set.
            turn: The turn in progress.
        """
        self._touch(conversation_id)
        self._current_turn[conversation_id] = turn

    def clear(self) -> None:
        """Drop every record the store holds."""
        self._changes.clear()
        self._deliverables.clear()
        self._contents.clear()
        self._current_turn.clear()
        self._roots.clear()
        self._conversations.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _touch(self, conversation_id: uuid.UUID) -> None:
        """Mark one conversation as the most recently used one.

        Args:
            conversation_id: The conversation to mark.
        """
        if conversation_id in self._conversations:
            self._conversations.remove(conversation_id)
        self._conversations.append(conversation_id)
        self._evict_conversations()

    def _evict_conversations(self) -> None:
        """Drop the oldest conversations until the store is within its bound."""
        while len(self._conversations) > MAX_CONVERSATIONS:
            oldest = self._conversations.pop(0)
            self._changes.pop(oldest, None)
            self._deliverables.pop(oldest, None)
            self._contents.pop(oldest, None)
            self._current_turn.pop(oldest, None)
            self._roots.pop(oldest, None)


#: The process-wide deliverable store.
_STORE = DeliverableStore()


def deliverable_store() -> DeliverableStore:
    """Return the process-wide deliverable store.

    Returns:
        The store every turn change and declaration is recorded in.
    """
    return _STORE


def record_turn_changes(
    conversation_id: uuid.UUID,
    turn: int,
    before: Sequence[SnapshotFile],
    after: Sequence[SnapshotFile],
    *,
    root: Path,
) -> TurnChange:
    """Record the files one turn changed.

    Args:
        conversation_id: The conversation the turn belongs to.
        turn: The turn's number, as the caller numbered it.
        before: The workspace files as they were before the turn.
        after: The workspace files as they are after the turn.
        root: The workspace root the snapshots were taken in.

    Returns:
        The recorded turn.
    """
    return _STORE.record_turn_changes(conversation_id, turn, before, after, root=root)


def declare_deliverable(conversation_id: uuid.UUID, turn: int, path: str) -> DeliverableRecord:
    """Record one file as a deliverable of a turn.

    Args:
        conversation_id: The conversation the declaration belongs to.
        turn: The turn the declaration is made in.
        path: The workspace-relative path of the file.

    Returns:
        The recorded declaration.

    Raises:
        DeliverableError: When the path is not a path inside the project
            workspace.
    """
    return _STORE.declare_deliverable(conversation_id, turn, path)


__all__ = [
    "MAX_CACHED_CHARS",
    "MAX_CACHED_FILES",
    "MAX_CACHE_REFRESH_PER_RECORD",
    "MAX_CONVERSATIONS",
    "MAX_DELIVERABLES_PER_CONVERSATION",
    "MAX_DIFF_LINES",
    "MAX_TURNS_PER_CONVERSATION",
    "ChangedFile",
    "DeliverableError",
    "DeliverableRecord",
    "DeliverableStore",
    "TurnChange",
    "declare_deliverable",
    "deliverable_store",
    "record_turn_changes",
]
