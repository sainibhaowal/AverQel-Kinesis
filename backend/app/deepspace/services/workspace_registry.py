"""The durable project registry behind DeepSpace's workspaces.

A workspace is a directory a conversation works in.  The directory alone
cannot carry the order a user arranged projects in, which session belongs to
which project, or the order of those sessions, so this module keeps one small
manifest beside the named workspace registrations and writes it atomically.

The manifest is a cache of user choices, not the only copy of the projects:
every registration directory keeps its marker, and a missing or corrupt
manifest is rebuilt from those markers.  Writes are serialized with a thread
lock and an advisory file lock, then committed with ``os.replace``, so a
crash leaves either the previous manifest or the new one, never a half-written
file.

Deleting a project never deletes user data.  The record leaves the manifest
in a tombstone first, then the registration directory moves into
``.deleted/``; a crash between those steps finishes on the next load.  A
custom project directory outside the registration root is never touched.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

from app.deepspace.services.workspace_root import (
    WorkspaceUnavailableError,
    account_workspace_root,
    is_workspace_id,
    named_workspaces_root,
)
from app.deepspace.utils.atomic_write import atomic_write_json as _atomic_write_json

#: The manifest that records order and session positions.
REGISTRY_FILENAME = ".registry.json"

#: The advisory lock that serializes writers across processes.
REGISTRY_LOCK_FILENAME = ".registry.lock"

#: The schema version this module writes.
REGISTRY_VERSION = 2

#: The marker every workspace registration directory carries.
WORKSPACE_MARKER_FILENAME = ".deepspace-workspace.json"

#: The directory deleted registrations move into instead of being destroyed.
QUARANTINE_DIRNAME = ".deleted"

#: The prefix a quarantined registration carries.
QUARANTINE_PREFIX = "deleted-"

#: A process-local lock per manifest path, so threads do not contend on flock.
_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


class WorkspaceRegistryError(RuntimeError):
    """The registry could not complete an operation."""


class UnknownWorkspaceRegistryError(WorkspaceRegistryError):
    """No registration carries the requested identifier."""


class WorkspaceNameConflictError(WorkspaceRegistryError):
    """Another registration already carries the requested name."""


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    """One durable workspace registration.

    Attributes:
        id: The stable identifier; it survives rename and path changes.
        directory: The registration directory name under the named root.
        name: The display name the user chose.
        path: A custom project directory, or None for the registration dir.
        created_at: The ISO timestamp the registration was created.
        updated_at: The ISO timestamp the registration last changed.
    """

    #: The stable identifier.
    id: str
    #: The registration directory name.
    directory: str
    #: The display name.
    name: str
    #: The custom project directory, when the user attached one.
    path: str | None
    #: When the registration was created.
    created_at: str
    #: When the registration last changed.
    updated_at: str

    def effective_path(self, named_root: Path) -> Path:
        """Return the directory the tools work in.

        Args:
            named_root: The account's named workspace root.

        Returns:
            The custom path when one is recorded, otherwise the registration
            directory itself.
        """
        if self.path:
            return Path(self.path).expanduser()
        return named_root / self.directory

    def status(self, named_root: Path) -> str:
        """Report whether the workspace directory still exists.

        Args:
            named_root: The account's named workspace root.

        Returns:
            ``"ok"`` when the directory exists, ``"missing-dir"`` otherwise.
        """
        target = self.effective_path(named_root)
        try:
            return "ok" if target.is_dir() else "missing-dir"
        except OSError:
            return "missing-dir"

    def as_manifest(self) -> dict[str, Any]:
        """Return the JSON projection stored in the manifest.

        Returns:
            The record fields.
        """
        return {
            "id": self.id,
            "directory": self.directory,
            "name": self.name,
            "path": self.path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class _RegistryState:
    """The in-memory form of the manifest."""

    records: dict[str, WorkspaceRecord]
    order: list[str]
    positions: dict[str, list[str]]
    tombstones: list[str]


def _now() -> str:
    """Return the current UTC time as an ISO timestamp."""
    return datetime.now(UTC).isoformat()


def _process_lock(path: Path) -> threading.RLock:
    """Return the process-local lock that guards one manifest path.

    Args:
        path: The manifest path.

    Returns:
        The lock for that path.
    """
    key = str(path)
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[key] = lock
        return lock


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Serialize writers to one manifest across processes.

    Args:
        path: The lock file path.

    Yields:
        None while the lock is held.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with _process_lock(path):
        handle = open(path, "a+b")  # noqa: SIM115 - held for the context
        posix_locking: Any = fcntl
        windows_locking: Any = msvcrt
        try:
            if posix_locking is not None:
                posix_locking.flock(handle.fileno(), posix_locking.LOCK_EX)
            elif windows_locking is not None:  # pragma: no cover - Windows
                handle.seek(0)
                windows_locking.locking(handle.fileno(), windows_locking.LK_LOCK, 1)
            yield
        finally:
            try:
                if posix_locking is not None:
                    posix_locking.flock(handle.fileno(), posix_locking.LOCK_UN)
                elif windows_locking is not None:  # pragma: no cover - Windows
                    handle.seek(0)
                    windows_locking.locking(handle.fileno(), windows_locking.LK_UNLCK, 1)
            finally:
                handle.close()


def _fsync_directory(path: Path) -> None:
    """Flush a directory entry after a rename, where the platform supports it."""
    if os.name == "nt":  # pragma: no cover - Windows
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


# Use enhanced atomic write with cross-process locking from utils
# The _atomic_write_json includes file locking, PID takeover, and Windows retry logic
def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write one JSON document so readers see the old or the new file.

    This enhanced version includes cross-process file locking with PID-based
    stale-lock takeover and Windows transient-error retries.

    Args:
        path: The destination.
        payload: The document to write.

    Raises:
        OSError: When the file could not be written or replaced.
    """
    _atomic_write_json(path, payload)


def _legacy_atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    raise


def read_json_object(path: Path) -> dict[str, Any] | None:
    """Read one JSON object, returning None when it is absent or unreadable.

    Args:
        path: The file to read.

    Returns:
        The object, or None.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _clean_conversation_id(value: object) -> str:
    """Return a canonical conversation identifier.

    Args:
        value: The candidate identifier.

    Returns:
        The canonical UUID string.

    Raises:
        ValueError: When the value is not a UUID.
    """
    return str(uuid.UUID(str(value)))


def _parse_state(payload: dict[str, Any] | None) -> _RegistryState:
    """Build the in-memory state from a manifest document.

    Args:
        payload: The parsed manifest, or None.

    Returns:
        The state.  Unknown fields are ignored and malformed entries dropped.

    Raises:
        WorkspaceUnavailableError: When the manifest was written by a newer
            schema this build cannot safely rewrite.
    """
    empty = _RegistryState(records={}, order=[], positions={}, tombstones=[])
    if payload is None:
        return empty
    version = payload.get("version")
    if isinstance(version, int) and version > REGISTRY_VERSION:
        raise WorkspaceUnavailableError(
            "The workspace registry was written by a newer AverQel version."
        )
    records: dict[str, WorkspaceRecord] = {}
    raw_records = payload.get("workspaces")
    if isinstance(raw_records, list):
        for item in raw_records:
            if not isinstance(item, dict):
                continue
            identifier = str(item.get("id") or "")
            directory = str(item.get("directory") or identifier)
            if not is_workspace_id(identifier) or not is_workspace_id(directory):
                continue
            name = str(item.get("name") or identifier)
            path_value = item.get("path")
            path = str(path_value) if isinstance(path_value, str) and path_value else None
            created = str(item.get("created_at") or _now())
            updated = str(item.get("updated_at") or created)
            records[identifier] = WorkspaceRecord(
                id=identifier,
                directory=directory,
                name=name,
                path=path,
                created_at=created,
                updated_at=updated,
            )
    order: list[str] = []
    raw_order = payload.get("order")
    if isinstance(raw_order, list):
        for item in raw_order:
            identifier = str(item)
            if identifier in records and identifier not in order:
                order.append(identifier)
    for identifier in records:
        if identifier not in order:
            order.append(identifier)
    positions: dict[str, list[str]] = {}
    raw_positions = payload.get("positions")
    if isinstance(raw_positions, dict):
        for workspace_id, values in raw_positions.items():
            if not isinstance(values, list):
                continue
            cleaned: list[str] = []
            for value in values:
                try:
                    conversation_id = _clean_conversation_id(value)
                except (TypeError, ValueError):
                    continue
                if conversation_id not in cleaned:
                    cleaned.append(conversation_id)
            positions[str(workspace_id)] = cleaned
    tombstones: list[str] = []
    raw_tombstones = payload.get("tombstones")
    if isinstance(raw_tombstones, list):
        for item in raw_tombstones:
            identifier = str(item)
            if is_workspace_id(identifier) and identifier not in tombstones:
                tombstones.append(identifier)
    return _RegistryState(records=records, order=order, positions=positions, tombstones=tombstones)


class WorkspaceRegistry:
    """The ordered, durable project registry of one DeepSpace account."""

    def __init__(self, settings: Any, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """Create the registry for one account.

        Args:
            settings: The deployment settings in force.
            tenant_id: The owning tenant.
            user_id: The owning user.
        """
        self.settings = settings
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.named_root = named_workspaces_root(settings, tenant_id, user_id)
        self.path = self.named_root / REGISTRY_FILENAME
        self.lock_path = self.named_root / REGISTRY_LOCK_FILENAME

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def catalogue(self) -> list[WorkspaceRecord]:
        """Return the account's named workspaces in the user's order.

        Returns:
            The records; a registration whose directory disappeared stays in
            the list with a ``missing-dir`` status.

        Raises:
            WorkspaceUnavailableError: When the registry cannot be read.
        """
        with _file_lock(self.lock_path):
            state = self._load_locked()
        return self._ordered_records(state)

    def directory_for(self, identifier: str) -> Path | None:
        """Return the registration directory for one identifier.

        Args:
            identifier: The stable workspace identifier.

        Returns:
            The directory, or None when the account has no such registration.
        """
        with _file_lock(self.lock_path):
            state = self._load_locked()
        record = state.records.get(identifier)
        if record is None:
            return None
        return self.named_root / record.directory

    def session_order(self, workspace_id: str) -> list[str]:
        """Return the explicitly ordered conversations of one workspace.

        Args:
            workspace_id: The workspace identifier.

        Returns:
            The conversation identifiers in the order the user arranged.
        """
        with _file_lock(self.lock_path):
            state = self._load_locked()
        return list(state.positions.get(workspace_id, []))

    def default_root(self) -> Path:
        """Return the account's default workspace root.

        Returns:
            The default workspace directory.
        """
        return account_workspace_root(self.settings, self.tenant_id, self.user_id)

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def create(
        self, workspace_id: str, name: str, custom_path: Path | None = None
    ) -> WorkspaceRecord:
        """Create one registration.

        Args:
            workspace_id: The stable identifier; it also names the directory.
            name: The display name.
            custom_path: An attached project directory, or None.

        Returns:
            The new record.

        Raises:
            WorkspaceRegistryError: When the identifier is already registered
                or the registration directory cannot be created.
        """
        if not is_workspace_id(workspace_id):
            raise WorkspaceRegistryError(
                "That workspace identifier is not safe as a directory name."
            )
        with _file_lock(self.lock_path):
            state = self._load_locked()
            if workspace_id in state.records:
                raise WorkspaceRegistryError(f"A workspace named '{workspace_id}' already exists.")
            directory = self.named_root / workspace_id
            if directory.exists():
                raise WorkspaceRegistryError(
                    f"The workspace directory '{directory}' already exists."
                )
            try:
                directory.mkdir(parents=True)
            except OSError as exc:
                raise WorkspaceRegistryError("The workspace could not be created.") from exc
            created = _now()
            record = WorkspaceRecord(
                id=workspace_id,
                directory=workspace_id,
                name=name,
                path=str(custom_path) if custom_path is not None else None,
                created_at=created,
                updated_at=created,
            )
            try:
                self._write_marker(record)
                state.records[workspace_id] = record
                state.order.append(workspace_id)
                self._persist_locked(state)
            except OSError as exc:
                with contextlib.suppress(OSError):
                    (directory / WORKSPACE_MARKER_FILENAME).unlink()
                with contextlib.suppress(OSError):
                    directory.rmdir()
                raise WorkspaceRegistryError("The workspace could not be created.") from exc
            return record

    def rename(self, identifier: str, name: str) -> WorkspaceRecord:
        """Rename one registration without changing its identity.

        Args:
            identifier: The workspace identifier.
            name: The new display name.

        Returns:
            The updated record.

        Raises:
            UnknownWorkspaceRegistryError: When no registration carries it.
            WorkspaceNameConflictError: When another registration uses the
                name in any letter case.
        """
        with _file_lock(self.lock_path):
            state = self._load_locked()
            record = state.records.get(identifier)
            if record is None:
                raise UnknownWorkspaceRegistryError("No such workspace.")
            for other in state.records.values():
                if other.id != identifier and other.name.strip().lower() == name.strip().lower():
                    raise WorkspaceNameConflictError("A workspace with that name already exists.")
            updated = WorkspaceRecord(
                id=record.id,
                directory=record.directory,
                name=name,
                path=record.path,
                created_at=record.created_at,
                updated_at=_now(),
            )
            try:
                self._write_marker(updated)
            except OSError as exc:
                raise WorkspaceRegistryError("The workspace could not be renamed.") from exc
            state.records[identifier] = updated
            self._persist_locked(state)
            return updated

    def set_path(self, identifier: str, custom_path: Path | None) -> WorkspaceRecord:
        """Attach or detach the project directory of one registration.

        Args:
            identifier: The workspace identifier.
            custom_path: The new project directory, or None to use the
                registration directory.

        Returns:
            The updated record.

        Raises:
            UnknownWorkspaceRegistryError: When no registration carries it.
        """
        with _file_lock(self.lock_path):
            state = self._load_locked()
            record = state.records.get(identifier)
            if record is None:
                raise UnknownWorkspaceRegistryError("No such workspace.")
            updated = WorkspaceRecord(
                id=record.id,
                directory=record.directory,
                name=record.name,
                path=str(custom_path) if custom_path is not None else None,
                created_at=record.created_at,
                updated_at=_now(),
            )
            try:
                self._write_marker(updated)
            except OSError as exc:
                raise WorkspaceRegistryError("The workspace could not be updated.") from exc
            state.records[identifier] = updated
            self._persist_locked(state)
            return updated

    def move(self, identifier: str, before_id: str | None = None) -> list[str]:
        """Move one registration before another in the user's order.

        Args:
            identifier: The workspace to move.
            before_id: The workspace it should precede, or None to append.

        Returns:
            The new identifier order.

        Raises:
            UnknownWorkspaceRegistryError: When either identifier is absent.
        """
        with _file_lock(self.lock_path):
            state = self._load_locked()
            if identifier not in state.records:
                raise UnknownWorkspaceRegistryError("No such workspace.")
            if before_id is not None and before_id not in state.records:
                raise UnknownWorkspaceRegistryError("No such workspace.")
            if before_id == identifier:
                return [item for item in state.order if item in state.records]
            state.order = [item for item in state.order if item != identifier]
            if before_id is None:
                state.order.append(identifier)
            else:
                index = state.order.index(before_id)
                state.order.insert(index, identifier)
            self._persist_locked(state)
            return [item for item in state.order if item in state.records]

    def delete(self, identifier: str) -> bool:
        """Remove a registration without destroying user data.

        The record and its order entry commit first with a tombstone, then the
        registration directory moves into ``.deleted/``.  A crash after the
        first step is finished by the next load.

        Args:
            identifier: The workspace identifier.

        Returns:
            True when a registration was removed, False when none existed.
        """
        with _file_lock(self.lock_path):
            state = self._load_locked()
            record = state.records.get(identifier)
            if record is None:
                return False
            state.tombstones.append(identifier)
            state.records.pop(identifier, None)
            state.order = [item for item in state.order if item != identifier]
            state.positions.pop(identifier, None)
            self._persist_locked(state)
            self._quarantine_locked(identifier, record.directory)
            state.tombstones = [item for item in state.tombstones if item != identifier]
            self._persist_locked(state)
            return True

    def order_sessions(self, workspace_id: str, conversation_ids: list[str]) -> list[str]:
        """Replace the explicit session order of one workspace.

        Args:
            workspace_id: The workspace identifier.
            conversation_ids: The conversation identifiers in their new order.

        Returns:
            The stored order.
        """
        cleaned: list[str] = []
        for value in conversation_ids:
            conversation_id = _clean_conversation_id(value)
            if conversation_id not in cleaned:
                cleaned.append(conversation_id)
        with _file_lock(self.lock_path):
            state = self._load_locked()
            state.positions[workspace_id] = cleaned
            self._persist_locked(state)
        return cleaned

    def insert_session(
        self, workspace_id: str, conversation_id: str, before_id: str | None = None
    ) -> list[str]:
        """Move one conversation before another within a workspace.

        Args:
            workspace_id: The workspace identifier.
            conversation_id: The conversation to move.
            before_id: The conversation it should precede, or None to append.

        Returns:
            The stored order.
        """
        session_id = _clean_conversation_id(conversation_id)
        with _file_lock(self.lock_path):
            state = self._load_locked()
            ordered = [item for item in state.positions.get(workspace_id, []) if item != session_id]
            if before_id is None:
                ordered.append(session_id)
            else:
                target = _clean_conversation_id(before_id)
                if target in ordered:
                    ordered.insert(ordered.index(target), session_id)
                else:
                    ordered.append(session_id)
            state.positions[workspace_id] = ordered
            self._persist_locked(state)
            return ordered

    def prune_sessions(self, valid_conversation_ids: set[str]) -> None:
        """Drop positions for conversations that no longer exist.

        Args:
            valid_conversation_ids: The conversations that still exist.
        """
        with _file_lock(self.lock_path):
            state = self._load_locked()
            changed = False
            for workspace_id, values in list(state.positions.items()):
                kept = [value for value in values if value in valid_conversation_ids]
                if kept != values:
                    state.positions[workspace_id] = kept
                    changed = True
            if changed:
                self._persist_locked(state)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ordered_records(self, state: _RegistryState) -> list[WorkspaceRecord]:
        """Return records in the user's order, then any unlisted ones."""
        ordered = [state.records[item] for item in state.order if item in state.records]
        seen = {record.id for record in ordered}
        extras = sorted(
            (record for record in state.records.values() if record.id not in seen),
            key=lambda record: (record.name.lower(), record.id),
        )
        return [*ordered, *extras]

    def _load_locked(self) -> _RegistryState:
        """Read, repair, and reconcile the manifest.  The caller holds the lock."""
        state = self._read_manifest_locked()
        changed = self._recover_tombstones_locked(state)
        changed = self._reconcile_markers_locked(state) or changed
        if changed:
            self._persist_locked(state)
        return state

    def _read_manifest_locked(self) -> _RegistryState:
        """Read the manifest, quarantining a corrupt file and rebuilding it."""
        if not self.path.exists():
            return _parse_state(None)
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._quarantine_corrupt_manifest_locked()
            return _parse_state(None)
        if not isinstance(payload, dict):
            self._quarantine_corrupt_manifest_locked()
            return _parse_state(None)
        return _parse_state(payload)

    def _quarantine_corrupt_manifest_locked(self) -> None:
        """Move an unreadable manifest aside so it can be rebuilt."""
        target = (
            self.named_root / f"{REGISTRY_FILENAME}.corrupt-{int(datetime.now(UTC).timestamp())}"
        )
        with contextlib.suppress(OSError):
            os.replace(self.path, target)

    def _recover_tombstones_locked(self, state: _RegistryState) -> bool:
        """Finish deletions that committed their tombstone but not the move."""
        changed = False
        for identifier in list(state.tombstones):
            record = state.records.pop(identifier, None)
            directory = record.directory if record is not None else identifier
            state.order = [item for item in state.order if item != identifier]
            state.positions.pop(identifier, None)
            self._quarantine_locked(identifier, directory)
            state.tombstones.remove(identifier)
            changed = True
        return changed

    def _reconcile_markers_locked(self, state: _RegistryState) -> bool:
        """Import registration directories the manifest does not know yet."""
        changed = False
        try:
            entries = sorted(self.named_root.iterdir(), key=lambda item: item.name)
        except OSError:
            return False
        for entry in entries:
            if entry.name.startswith(".") or entry.is_symlink():
                continue
            try:
                if not entry.is_dir() or not is_workspace_id(entry.name):
                    continue
            except OSError:
                continue
            if entry.name in state.tombstones:
                continue
            meta = read_json_object(entry / WORKSPACE_MARKER_FILENAME) or {}
            identifier = str(meta.get("id") or entry.name)
            if not is_workspace_id(identifier):
                identifier = entry.name
            if identifier in state.tombstones:
                continue
            name = str(meta.get("name") or identifier)
            raw_path = meta.get("path")
            path = str(raw_path) if isinstance(raw_path, str) and raw_path else None
            existing = state.records.get(identifier)
            if existing is None:
                created = str(meta.get("created_at") or _now())
                state.records[identifier] = WorkspaceRecord(
                    id=identifier,
                    directory=entry.name,
                    name=name,
                    path=path,
                    created_at=created,
                    updated_at=str(meta.get("updated_at") or created),
                )
                state.order.append(identifier)
                changed = True
                continue
            if existing.directory != entry.name or existing.name != name or existing.path != path:
                state.records[identifier] = WorkspaceRecord(
                    id=existing.id,
                    directory=entry.name,
                    name=name,
                    path=path,
                    created_at=existing.created_at,
                    updated_at=_now(),
                )
                changed = True
        return changed

    def _persist_locked(self, state: _RegistryState) -> None:
        """Write the manifest.  The caller holds the lock."""
        payload = {
            "version": REGISTRY_VERSION,
            "workspaces": [record.as_manifest() for record in self._ordered_records(state)],
            "order": [item for item in state.order if item in state.records],
            "positions": {
                workspace_id: values for workspace_id, values in sorted(state.positions.items())
            },
            "tombstones": list(state.tombstones),
        }
        try:
            atomic_write_json(self.path, payload)
        except OSError as exc:
            raise WorkspaceUnavailableError("The workspace registry could not be written.") from exc

    def _write_marker(self, record: WorkspaceRecord) -> None:
        """Write the portable marker of one registration.

        Args:
            record: The record to write.

        Raises:
            OSError: When the marker could not be written.
        """
        directory = self.named_root / record.directory
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            directory / WORKSPACE_MARKER_FILENAME,
            {
                "version": REGISTRY_VERSION,
                "id": record.id,
                "name": record.name,
                "path": record.path,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
            },
        )

    def _quarantine_locked(self, identifier: str, directory_name: str) -> None:
        """Move a registration directory into the quarantine directory.

        Args:
            identifier: The workspace identifier.
            directory_name: The registration directory name.

        Raises:
            WorkspaceUnavailableError: When the directory could not be moved
                and the call is not recovering from a finished deletion.
        """
        source = self.named_root / directory_name
        try:
            if not source.exists() or source.is_symlink():
                return
        except OSError:
            return
        quarantine = self.named_root / QUARANTINE_DIRNAME
        try:
            quarantine.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
            target = quarantine / f"{QUARANTINE_PREFIX}{identifier}-{stamp}"
            suffix = 1
            while target.exists():
                target = quarantine / f"{QUARANTINE_PREFIX}{identifier}-{stamp}-{suffix}"
                suffix += 1
            os.replace(source, target)
        except OSError as exc:
            raise WorkspaceUnavailableError(
                "The workspace registration could not be moved aside safely."
            ) from exc


__all__ = [
    "QUARANTINE_DIRNAME",
    "REGISTRY_FILENAME",
    "REGISTRY_VERSION",
    "WORKSPACE_MARKER_FILENAME",
    "WorkspaceNameConflictError",
    "WorkspaceRecord",
    "WorkspaceRegistry",
    "WorkspaceRegistryError",
    "atomic_write_json",
    "read_json_object",
]
