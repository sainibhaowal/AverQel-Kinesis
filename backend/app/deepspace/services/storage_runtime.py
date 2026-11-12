"""Persistent application-owned storage hub for DeepSpace.

The domain facility is exposed to application components through
``runtime.storage.domain``. SQLite is the default durable KV backend; JSON is
also registered for components that explicitly route a domain to it.
Conversation and account records remain authoritative in the application DB.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.deepspace.services.storage import Storage
from app.deepspace.services.storage_domain import DomainFacility
from app.deepspace.services.storage_json import JsonStorageBackend
from app.deepspace.services.storage_sqlite import SqliteStorageBackend


class DeepSpaceStorageRuntime:
    """Own persistent storage backends and the mounted domain facility."""

    def __init__(self, home: Path) -> None:
        self.home = Path(home)
        json_root = self.home / "storage" / "deepspace-kv"
        sqlite_path = self.home / "db" / "deepspace-kv.sqlite3"
        json_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        sqlite_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        self.storage = Storage()
        self.backends = {
            "json": JsonStorageBackend(json_root),
            "sqlite": SqliteStorageBackend({"path": str(sqlite_path), "journalMode": "wal"}),
        }
        for name, backend in self.backends.items():
            self.storage.backend.register(name, backend)

        context = SimpleNamespace(storage=self.storage)
        self.domain = DomainFacility(context, {"backend": "sqlite"})
        self.storage.mount("domain", self.domain)
        self._closed = False
        self._close_lock = asyncio.Lock()

    async def close(self) -> None:
        """Close domains before their backends; safe to call more than once."""
        async with self._close_lock:
            if self._closed:
                return
            await self.domain.close_all()
            await asyncio.gather(
                *(backend.close() for backend in self.backends.values()),
                return_exceptions=False,
            )
            self._closed = True


__all__ = ["DeepSpaceStorageRuntime"]
