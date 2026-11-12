"""JSON storage backend for DeepSpace.

Python-native port of the DeepSeek Harness storage-json package.
Provides JsonStorageBackend with single and per-record layouts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

from app.deepspace.services.storage import (
    UNIT_NAME_RE_COMPILED,
    KvUnit,
    KvUnitDescriptor,
    StorageBackend,
    StorageError,
)

logger = logging.getLogger(__name__)

SAFE_KEY_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


# === Format Functions ===


class UnitState:
    """In-memory authoritative state of one unit."""

    def __init__(self, version, global_val=None, tables=None):
        self.version = version
        self.global_val = global_val if global_val is not None else None
        self.tables = tables or {}


def serialize(name: str, state: UnitState) -> str:
    """Serialize a unit state to file content."""
    tables = {}
    for table, records in state.tables.items():
        tables[table] = dict(records) if isinstance(records, dict) else {}
    document = {
        "unit": {"name": name, "version": state.version},
        "global": state.global_val,
        "tables": tables,
    }
    return json.dumps(document, indent=2) + "\n"


def parse(text: str, descriptor: KvUnitDescriptor) -> UnitState:
    """Parse file content into unit state, validating shape and version."""
    try:
        document = json.loads(text)
    except json.JSONDecodeError as e:
        raise StorageError(
            "malformed-medium", f"unit '{descriptor.name}': file is not valid JSON"
        ) from e

    if not isinstance(document, dict):
        raise StorageError(
            "malformed-medium", f"unit '{descriptor.name}': file is not a JSON object"
        )

    unit = document.get("unit")
    if (
        not isinstance(unit, dict)
        or unit.get("name") != descriptor.name
        or not isinstance(unit.get("version"), int)
    ):
        raise StorageError(
            "malformed-medium", f"unit '{descriptor.name}': missing or foreign unit header"
        )

    version = unit["version"]
    if version != descriptor.version and version not in (descriptor.compatible_versions or []):
        raise StorageError(
            "version-mismatch",
            f"unit '{descriptor.name}': stored version {version} is not accepted by version {descriptor.version}",
        )

    tables_raw = document.get("tables")
    if not isinstance(tables_raw, dict):
        raise StorageError("malformed-medium", f"unit '{descriptor.name}': tables is not an object")

    tables = {}
    for table in descriptor.tables:
        records = tables_raw.get(table)
        if records is None:
            tables[table] = {}
            continue
        if not isinstance(records, dict):
            raise StorageError(
                "malformed-medium", f"unit '{descriptor.name}': table '{table}' is not an object"
            )
        tables[table] = dict(records)

    global_val = document.get("global")
    if global_val is None:
        global_val = None

    state = UnitState(version=version, global_val=global_val, tables=tables)
    return state


def serialize_record(version: int, value: Any) -> str:
    """Serialize one per-record document."""
    document = {"version": version, "record": value}
    return json.dumps(document, indent=2) + "\n"


def parse_record(text: str, versions: list[int]) -> Any:
    """Parse one per-record document; foreign docs read as absent (None)."""
    try:
        document = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(document, dict):
        return None
    stamped = document.get("version")
    if not isinstance(stamped, int) or stamped not in versions:
        return None
    return document.get("record")


# === Atomic Write ===


async def write_atomic(path: str, data: str) -> None:
    """Durably replace path with data."""
    target = Path(path)
    tmp = target.parent / f".{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(data, encoding="utf-8")
        # fsync
        with open(tmp, "r+b") as f:
            os.fsync(f.fileno())
        os.replace(tmp, target)
        # fsync directory
        await fsync_directory(str(target.parent))
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


async def fsync_directory(path: str) -> None:
    """Persist directory-entry changes on POSIX."""
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


# === Single Layout Unit ===


class SingleJsonUnit(KvUnit):
    """One opened JSON unit in single layout."""

    def __init__(self, descriptor, path, state, on_close):
        self.descriptor = descriptor
        self.path = path
        self.state = state
        self.on_close = on_close
        self._closed = False
        self._in_flight = set()

    def assert_open(self):
        if self._closed:
            raise StorageError("closed", f"unit '{self.descriptor.name}' is closed")

    async def load_all(self):
        self.assert_open()
        tables = {}
        for table, records in self.state.tables.items():
            tables[table] = dict(records)
        return {"tables": tables, "global": self.state.global_val}

    async def put_record(self, table, key, value):
        self.assert_open()
        records = self.state.tables.setdefault(table, {})
        had_key = key in records
        previous = records.get(key)
        records[key] = value
        try:
            await self._publish()
        except Exception:
            if had_key:
                records[key] = previous
            else:
                del records[key]
            raise

    async def delete_record(self, table, key):
        await self.delete_records(table, [key])

    async def delete_records(self, table, keys):
        self.assert_open()
        records = self.state.tables.setdefault(table, {})
        removed = {}
        for key in set(keys):
            if key in records:
                removed[key] = records.pop(key)
        if not removed:
            return
        try:
            await self._publish()
        except Exception:
            for key, val in removed.items():
                records[key] = val
            raise

    async def set_global(self, value):
        self.assert_open()
        if not self.descriptor.has_global:
            raise ValueError(f"unit '{self.descriptor.name}' does not declare a global slot")
        previous = self.state.global_val
        self.state.global_val = value
        try:
            await self._publish()
        except Exception:
            self.state.global_val = previous
            raise

    async def close(self):
        if self._closed:
            await asyncio.gather(*self._in_flight, return_exceptions=True)
            return
        self._closed = True
        await asyncio.gather(*self._in_flight, return_exceptions=True)
        self.on_close()

    async def backup_record(self, table, key):
        raise NotImplementedError("backup not supported in single layout")

    async def _publish(self):
        data = serialize(self.descriptor.name, self.state)
        write = write_atomic(self.path, data)
        self._in_flight.add(write)
        try:
            await write
        finally:
            self._in_flight.discard(write)


# === Per-Record Layout Unit ===


class PerRecordJsonUnit(KvUnit):
    """One opened JSON unit in per-record layout."""

    def __init__(self, descriptor, dir_path, on_close):
        self.descriptor = descriptor
        self.dir_path = dir_path
        self.on_close = on_close
        self._closed = False
        self._unusable = None
        self._in_flight = set()

    def assert_open(self):
        if self._closed:
            raise StorageError("closed", f"unit '{self.descriptor.name}' is closed")
        if self._unusable is not None:
            raise self._unusable

    async def load_all(self):
        self.assert_open()
        state = await self._load_state()
        tables = {}
        for table, records in state.tables.items():
            tables[table] = dict(records)
        return {"tables": tables, "global": state.global_val}

    async def _load_state(self):
        from pathlib import Path

        dir_path = Path(self.dir_path)
        versions = [self.descriptor.version] + (self.descriptor.compatible_versions or [])
        state = UnitState(
            version=self.descriptor.version,
            global_val=None,
            tables={t: {} for t in self.descriptor.tables},
        )

        # Check if directory exists
        if not dir_path.exists():
            # Try legacy bootstrap
            await self._bootstrap_legacy(dir_path, state)
            return state

        # Read per-record files
        has_new_state = False
        for table in self.descriptor.tables:
            table_dir = dir_path / table
            if table_dir.is_dir():
                has_new_state = True
                records = state.tables[table]
                for f in table_dir.iterdir():
                    if f.suffix == ".json" and f.is_file():
                        key = f.stem
                        if not SAFE_KEY_RE.match(key):
                            continue
                        try:
                            text = f.read_text(encoding="utf-8")
                            value = parse_record(text, versions)
                            if value is not None:
                                records[key] = value
                        except Exception:
                            pass

        # Global
        global_path = dir_path / "global.json"
        if global_path.is_file() and self.descriptor.has_global:
            has_new_state = True
            try:
                text = global_path.read_text(encoding="utf-8")
                value = parse_record(text, versions)
                if value is not None:
                    state.global_val = value
            except Exception:
                pass

        if not has_new_state:
            await self._bootstrap_legacy(dir_path, state)

        return state

    async def _bootstrap_legacy(self, dir_path, state):
        """Bootstrap from legacy whole-unit file."""
        legacy_path = Path(self.dir_path).parent / f"{self.descriptor.name}.json"
        if not legacy_path.is_file():
            return
        try:
            text = legacy_path.read_text(encoding="utf-8")
            document = json.loads(text)
            if not isinstance(document, dict):
                return
            unit = document.get("unit", {})
            if unit.get("name") != self.descriptor.name:
                return
            stamped = unit.get("version")
            versions = [self.descriptor.version] + (self.descriptor.compatible_versions or [])
            if not isinstance(stamped, int) or stamped not in versions:
                return
            tables_raw = document.get("tables")
            if not isinstance(tables_raw, dict):
                return
            for table, records in tables_raw.items():
                if table not in state.tables:
                    continue
                if not isinstance(records, dict):
                    continue
                target = state.tables[table]
                for key, value in records.items():
                    # Write per-record doc
                    record_dir = Path(self.dir_path) / table
                    record_dir.mkdir(parents=True, exist_ok=True)
                    record_path = record_dir / f"{key}.json"
                    await write_atomic(
                        str(record_path), serialize_record(self.descriptor.version, value)
                    )
                    target[key] = value
        except Exception:
            pass

    async def put_record(self, table, key, value):
        self.assert_open()
        if not SAFE_KEY_RE.match(key):
            raise ValueError(
                f"unit '{self.descriptor.name}': per-record key '{key}' is not path-safe"
            )
        table_dir = Path(self.dir_path) / table
        table_dir.mkdir(parents=True, exist_ok=True)
        record_path = table_dir / f"{key}.json"
        await self._tracked(
            write_atomic(str(record_path), serialize_record(self.descriptor.version, value))
        )

    async def delete_record(self, table, key):
        await self.delete_records(table, [key])

    async def delete_records(self, table, keys):
        self.assert_open()
        unique = list(set(keys))
        for key in unique:
            if not SAFE_KEY_RE.match(key):
                raise ValueError(
                    f"unit '{self.descriptor.name}': per-record key '{key}' is not path-safe"
                )

        dir_path = Path(self.dir_path) / table
        staged = []
        try:
            for key in unique:
                from_path = dir_path / f"{key}.json"
                to_path = Path(str(from_path) + ".deleting")
                if from_path.exists():
                    os.rename(from_path, to_path)
                    staged.append((from_path, to_path))
            if staged:
                await fsync_directory(str(dir_path))
        except Exception as failure:
            # Restore
            restore_failures = []
            for from_path, to_path in staged:
                try:
                    os.rename(to_path, from_path)
                except Exception as re:
                    restore_failures.append(re)
            if restore_failures:
                self._unusable = StorageError(
                    "unknown-state",
                    f"unit '{self.descriptor.name}' could not restore a failed batch delete",
                )
                raise self._unusable from failure
            raise

        # Cleanup staged files
        for _from_path, to_path in staged:
            try:
                to_path.unlink(missing_ok=True)
            except Exception:
                pass

    async def set_global(self, value):
        self.assert_open()
        if not self.descriptor.has_global:
            raise ValueError(f"unit '{self.descriptor.name}' does not declare a global slot")
        global_path = Path(self.dir_path) / "global.json"
        global_path.parent.mkdir(parents=True, exist_ok=True)
        await self._tracked(
            write_atomic(str(global_path), serialize_record(self.descriptor.version, value))
        )

    async def close(self):
        if self._closed:
            await asyncio.gather(*self._in_flight, return_exceptions=True)
            return
        self._closed = True
        await asyncio.gather(*self._in_flight, return_exceptions=True)
        self.on_close()

    async def backup_record(self, table, key):
        self.assert_open()
        if not SAFE_KEY_RE.match(key):
            raise ValueError(
                f"unit '{self.descriptor.name}': per-record key '{key}' is not path-safe"
            )
        from datetime import datetime

        path = Path(self.dir_path) / table / f"{key}.json"
        moved = f"{path}.bak.{datetime.now().strftime('%Y%m%d%H%M')}"
        os.rename(path, moved)
        await self._tracked(asyncio.coroutine(lambda: None)() if False else asyncio.sleep(0))
        return moved

    async def _tracked(self, coro):
        task = asyncio.ensure_future(coro)
        self._in_flight.add(task)
        try:
            await task
        finally:
            self._in_flight.discard(task)


# === JsonStorageBackend ===


class JsonStorageBackend(StorageBackend):
    """JSON storage backend."""

    def __init__(self, root):
        self.root = root
        self._open = {}
        self._opening = {}
        self._closed = False

    @property
    def kv(self):
        return self

    async def open(self, descriptor):
        if self._closed:
            raise StorageError("closed", "json backend is closed")
        if not UNIT_NAME_RE_COMPILED.match(descriptor.name):
            raise StorageError("malformed-medium", f"invalid unit name '{descriptor.name}'")
        for table in descriptor.tables:
            if not UNIT_NAME_RE_COMPILED.match(table):
                raise StorageError(
                    "malformed-medium", f"invalid table name '{table}' in unit '{descriptor.name}'"
                )

        if descriptor.name in self._open or descriptor.name in self._opening:
            raise ValueError(f"unit '{descriptor.name}' is already open")

        opening = self._open_unit(descriptor)
        self._opening[descriptor.name] = opening
        try:
            unit = await opening
            self._open[descriptor.name] = unit
            return unit
        finally:
            self._opening.pop(descriptor.name, None)

    async def _open_unit(self, descriptor):
        os.makedirs(self.root, exist_ok=True, mode=0o700)

        def on_close() -> None:
            self._open.pop(descriptor.name, None)

        if descriptor.layout == "per-record":
            unit = PerRecordJsonUnit(descriptor, os.path.join(self.root, descriptor.name), on_close)
            # Load initial state by calling load_all (which handles bootstrap)
            return unit
        else:
            # Single layout
            path = os.path.join(self.root, f"{descriptor.name}.json")
            text = None
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            except FileNotFoundError:
                pass

            if text is None:
                state = UnitState(
                    version=descriptor.version,
                    global_val=None,
                    tables={t: {} for t in descriptor.tables},
                )
            else:
                state = parse(text, descriptor)

            state.version = descriptor.version
            return SingleJsonUnit(descriptor, path, state, on_close)

    async def close(self):
        if not self._closed:
            self._closed = True
        await asyncio.gather(
            *[u.close() for u in list(self._opening.values())], return_exceptions=True
        )
        for unit in list(self._open.values()):
            await unit.close()


def apply(ctx, config):
    """Register the json backend on the storage hub."""
    root = config.get("root") if isinstance(config, dict) else getattr(config, "root", None)
    backend = JsonStorageBackend(root)
    storage = getattr(ctx, "storage", None)
    if storage is not None:
        disposer = storage.backend.register("json", backend)
        ctx.effect(lambda: disposer) if hasattr(ctx, "effect") else None
    return backend


__all__ = [
    "UnitState",
    "serialize",
    "parse",
    "serialize_record",
    "parse_record",
    "write_atomic",
    "fsync_directory",
    "SingleJsonUnit",
    "PerRecordJsonUnit",
    "JsonStorageBackend",
    "apply",
    "SAFE_KEY_RE",
]
