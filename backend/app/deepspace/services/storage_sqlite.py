"""SQLite storage backend for DeepSpace.

Python-native port of the DeepSeek Harness storage-sqlite package.
Provides SqliteStorageBackend with relational KV storage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3

from app.deepspace.services.storage import (
    UNIT_NAME_RE,
    UNIT_NAME_RE_COMPILED,
    KvUnit,
    StorageBackend,
    StorageError,
)

logger = logging.getLogger(__name__)

STORAGE_SQLITE_SCHEMA_VERSION = 1


def record_table_name(unit: str, table: str) -> str:
    """Physical table name for one unit table."""
    return f"u_{unit}_{table}"


def open_database(path: str, journal_mode: str = "wal") -> sqlite3.Connection:
    """Open and configure a SQLite database."""
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True, mode=0o700)
        # Create file with owner-only permissions if it doesn't exist
        if not os.path.exists(path):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)

    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA journal_mode = {journal_mode.upper()}")
        # Check user_version
        cursor = conn.execute("PRAGMA user_version")
        on_disk = cursor.fetchone()[0]
        if on_disk != 0 and on_disk != STORAGE_SQLITE_SCHEMA_VERSION:
            raise StorageError(
                "version-mismatch",
                f"storage database at '{path}' has schema version {on_disk}, incompatible with this build ({STORAGE_SQLITE_SCHEMA_VERSION})",
            )
        # Create units and unit_globals tables
        conn.execute("""
            CREATE TABLE IF NOT EXISTS units (
                name TEXT PRIMARY KEY,
                version INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS unit_globals (
                unit TEXT PRIMARY KEY REFERENCES units(name),
                value TEXT NOT NULL
            )
        """)
        if on_disk == 0:
            conn.execute(f"PRAGMA user_version = {STORAGE_SQLITE_SCHEMA_VERSION}")
        conn.commit()
        return conn
    except Exception:
        conn.close()
        raise


class SqliteKvUnit(KvUnit):
    """One opened SQLite KV unit."""

    def __init__(
        self, db, descriptor, stored_version, on_close, ensure_usable, on_rollback_failure
    ):
        self.db = db
        self.descriptor = descriptor
        self.on_close = on_close
        self._ensure_usable = ensure_usable
        self._on_rollback_failure = on_rollback_failure
        self._needs_version_stamp = stored_version != descriptor.version
        self._closed = False
        self._tables = {}

        for table in descriptor.tables:
            physical = record_table_name(descriptor.name, table)
            self.db.execute(f"""
                CREATE TABLE IF NOT EXISTS "{physical}" (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            self._tables[table] = {
                "upsert": None,  # prepared as needed
                "remove": None,
                "select_all": None,
            }

        self._global_upsert = None
        self._global_select = None
        if descriptor.has_global:
            self._global_upsert = "INSERT INTO unit_globals (unit, value) VALUES (?, ?) ON CONFLICT(unit) DO UPDATE SET value = excluded.value"
            self._global_select = "SELECT value FROM unit_globals WHERE unit = ?"

    def _statements_for(self, table):
        physical = record_table_name(self.descriptor.name, table)
        return physical

    def _settle(self, operation):
        try:
            self._ensure_open()
            return operation()
        except Exception as e:
            return _reject(e)

    def _ensure_open(self):
        if self._closed:
            raise StorageError("closed", f"unit '{self.descriptor.name}' is closed")
        self._ensure_usable()

    async def load_all(self):
        self._ensure_open()
        tables = {}
        for table in self.descriptor.tables:
            physical = record_table_name(self.descriptor.name, table)
            records = {}
            cursor = self.db.execute(f'SELECT key, value FROM "{physical}"')
            for row in cursor.fetchall():
                try:
                    records[row[0]] = json.loads(row[1])
                except json.JSONDecodeError as exc:
                    raise StorageError(
                        "malformed-medium",
                        f"kv unit '{self.descriptor.name}' holds unparsable JSON at table '{table}' key '{row[0]}'",
                    ) from exc
            tables[table] = records

        global_val = None
        if self.descriptor.has_global and self._global_select:
            cursor = self.db.execute(self._global_select, (self.descriptor.name,))
            row = cursor.fetchone()
            if row is not None:
                try:
                    global_val = json.loads(row[0])
                except json.JSONDecodeError as exc:
                    raise StorageError(
                        "malformed-medium",
                        f"kv unit '{self.descriptor.name}' holds unparsable JSON at global slot",
                    ) from exc

        return {"tables": tables, "global": global_val}

    async def put_record(self, table, key, value):
        self._ensure_open()
        physical = record_table_name(self.descriptor.name, table)
        text = json.dumps(value)
        self._write(
            lambda: self.db.execute(
                f'INSERT INTO "{physical}" (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                (key, text),
            ).rowcount,
            transactional=False,
        )

    async def delete_record(self, table, key):
        await self.delete_records(table, [key])

    async def delete_records(self, table, keys):
        self._ensure_open()
        if not keys:
            return
        physical = record_table_name(self.descriptor.name, table)
        unique = list(set(keys))
        self._write(
            lambda: sum(
                self.db.execute(f'DELETE FROM "{physical}" WHERE key = ?', (k,)).rowcount
                for k in unique
            ),
            transactional=True,
        )

    async def set_global(self, value):
        self._ensure_open()
        if not self.descriptor.has_global:
            raise ValueError(f"kv unit '{self.descriptor.name}' declared no global slot")
        text = json.dumps(value)
        self._write(
            lambda: self.db.execute(
                "INSERT INTO unit_globals (unit, value) VALUES (?, ?) ON CONFLICT(unit) DO UPDATE SET value = excluded.value",
                (self.descriptor.name, text),
            ).rowcount,
            transactional=False,
        )

    def _write(self, operation, transactional=False):
        if not self._needs_version_stamp and not transactional:
            operation()
            return

        self.db.execute("BEGIN IMMEDIATE")
        try:
            changed = operation() > 0
            if self._needs_version_stamp and changed:
                self.db.execute(
                    "UPDATE units SET version = ? WHERE name = ?",
                    (self.descriptor.version, self.descriptor.name),
                )
            self.db.commit()
            if changed:
                self._needs_version_stamp = False
        except Exception:
            if self.db.in_transaction:
                try:
                    self.db.rollback()
                except Exception as rollback_error:
                    failure = StorageError(
                        "unknown-state",
                        f"kv unit '{self.descriptor.name}' write and rollback failed",
                    )
                    self._on_rollback_failure(failure)
                    raise failure from rollback_error
            raise

    async def close(self):
        if not self._closed:
            self._closed = True
            self.on_close()
        return None

    async def backup_record(self, table, key):
        raise NotImplementedError("backup not supported in sqlite")


def _reject(exc):
    raise exc


class SqliteStorageBackend(StorageBackend):
    """SQLite storage backend."""

    def __init__(self, config):
        if isinstance(config, dict):
            path = config.get("path", ":memory:")
            journal_mode = config.get("journalMode", config.get("journal_mode", "wal"))
        else:
            path = getattr(config, "path", ":memory:")
            journal_mode = getattr(config, "journalMode", getattr(config, "journal_mode", "wal"))

        self._path = path
        self._journal_mode = journal_mode
        self._db = open_database(path, journal_mode)
        self._units = {}
        self._closing = None
        self._failure = None

    @property
    def kv(self):
        return self

    async def open(self, descriptor):
        if self._closing is not None:
            raise StorageError("closed", "sqlite storage backend is closed")
        if self._failure is not None:
            raise self._failure

        if not UNIT_NAME_RE_COMPILED.match(descriptor.name):
            raise ValueError(f"kv unit name '{descriptor.name}' violates {UNIT_NAME_RE}")
        for table in descriptor.tables:
            if not UNIT_NAME_RE_COMPILED.match(table):
                raise ValueError(
                    f"kv table name '{table}' in unit '{descriptor.name}' violates {UNIT_NAME_RE}"
                )

        if descriptor.name in self._units:
            raise ValueError(
                f"kv unit '{descriptor.name}' is already open (double-open is a caller bug)"
            )

        pending = self._materialize_unit(descriptor)
        self._units[descriptor.name] = pending
        try:
            unit = await pending
            return unit
        except Exception:
            self._units.pop(descriptor.name, None)
            raise

    async def _materialize_unit(self, descriptor):
        # Check existing version
        cursor = self._db.execute("SELECT version FROM units WHERE name = ?", (descriptor.name,))
        row = cursor.fetchone()
        if row is None:
            self._db.execute(
                "INSERT INTO units (name, version) VALUES (?, ?)",
                (descriptor.name, descriptor.version),
            )
            self._db.commit()
            stored_version = descriptor.version
        else:
            stored_version = row[0]
            if stored_version != descriptor.version and stored_version not in (
                descriptor.compatible_versions or []
            ):
                raise StorageError(
                    "version-mismatch",
                    f"kv unit '{descriptor.name}' is stamped version {stored_version} on the medium, incompatible with descriptor version {descriptor.version}",
                )

        for table in descriptor.tables:
            physical = record_table_name(descriptor.name, table)
            self._db.execute(f"""
                CREATE TABLE IF NOT EXISTS "{physical}" (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
        self._db.commit()

        def on_close() -> None:
            self._units.pop(descriptor.name, None)

        def ensure_usable() -> None:
            self._ensure_usable()

        def on_rollback(error: Exception) -> None:
            self._failure = error

        return SqliteKvUnit(
            self._db,
            descriptor,
            stored_version,
            on_close,
            ensure_usable,
            on_rollback,
        )

    def _ensure_usable(self):
        if self._failure is not None:
            raise self._failure

    async def close(self):
        if self._closing is not None:
            await self._closing
            return
        loop = asyncio.get_event_loop()
        self._closing = loop.create_future()
        try:
            for pending in list(self._units.values()):
                try:
                    unit = await pending
                    await unit.close()
                except Exception:
                    pass
            self._db.close()
            self._closing.set_result(None)
        except Exception as e:
            self._closing.set_exception(e)
            raise

    async def close_sync(self):
        await self.close()


def apply(ctx, config):
    """Register the sqlite backend on the storage hub."""
    if isinstance(config, dict):
        backend = SqliteStorageBackend(config)
    else:
        backend = SqliteStorageBackend(
            {
                "path": getattr(config, "path", ":memory:"),
                "journalMode": getattr(config, "journalMode", "wal"),
            }
        )
    storage = getattr(ctx, "storage", None)
    if storage is not None:
        storage.backend.register("sqlite", backend)
    return backend


__all__ = [
    "STORAGE_SQLITE_SCHEMA_VERSION",
    "record_table_name",
    "open_database",
    "SqliteKvUnit",
    "SqliteStorageBackend",
    "apply",
]
