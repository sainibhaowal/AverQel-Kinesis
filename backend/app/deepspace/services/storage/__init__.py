"""Storage hub for DeepSpace — named backend registry and form mounting.

Python-native port of the AverQel KineSis storage package.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any, Literal, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class StorageError(Exception):
    """Storage layer error."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


UNIT_NAME_RE = r"^[a-z][a-z0-9_]*$"
UNIT_NAME_RE_COMPILED = re.compile(UNIT_NAME_RE)


class StorageBackend:
    async def close(self) -> None:
        raise NotImplementedError

    kv: Any = None


class KvFacet:
    async def open(self, descriptor: KvUnitDescriptor) -> KvUnit:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class KvUnitDescriptor:
    def __init__(
        self,
        name: str,
        version: int,
        tables: list[str],
        has_global: bool = False,
        layout: Literal["single", "per-record"] | None = None,
        compatible_versions: list[int] | None = None,
    ) -> None:
        self.name = name
        self.version = version
        self.tables = tables
        self.has_global = has_global
        self.layout = layout
        self.compatible_versions = compatible_versions or []

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "tables": self.tables,
            "hasGlobal": self.has_global,
            "layout": self.layout,
            "compatibleVersions": self.compatible_versions,
        }


class KvUnit:
    async def load_all(self) -> dict[str, Any]:
        raise NotImplementedError

    async def put_record(self, table: str, key: str, value: Any) -> None:
        raise NotImplementedError

    async def delete_record(self, table: str, key: str) -> None:
        raise NotImplementedError

    async def delete_records(self, table: str, keys: list[str]) -> None:
        raise NotImplementedError

    async def set_global(self, value: Any) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    async def backup_record(self, table: str, key: str) -> str:
        raise NotImplementedError


class BackendRegistry:
    def __init__(self) -> None:
        self._backends: dict[str, Any] = {}

    def register(self, name: str, backend: Any) -> Callable[[], None]:
        if name in self._backends:
            raise StorageError(
                "duplicate-backend", f"storage backend '{name}' is already registered"
            )
        self._backends[name] = backend

        def disposer() -> None:
            if self._backends.get(name) is backend:
                del self._backends[name]

        return disposer

    def get(self, name: str) -> Any:
        backend = self._backends.get(name)
        if backend is None:
            registered = ", ".join(self._backends.keys()) or "none"
            raise StorageError(
                "backend-not-found",
                f"storage backend '{name}' is not registered (registered: {registered})",
            )
        return backend

    def names(self) -> list[str]:
        return list(self._backends.keys())


class Storage:
    def __init__(self, ctx: Any | None = None) -> None:
        self.backend = BackendRegistry()
        self._forms: dict[str, Any] = {}
        self._ctx = ctx

    def mount(self, form: str, facility: Any) -> Callable[[], None]:
        if form in self._forms:
            raise StorageError("duplicate-mount", f"storage form '{form}' is already mounted")
        self._forms[form] = facility

        def disposer() -> None:
            if self._forms.get(form) is facility:
                del self._forms[form]

        return disposer

    def form(self, form: str) -> Any:
        if form not in self._forms:
            raise StorageError("form-not-mounted", f"storage form '{form}' is not mounted")
        return self._forms[form]

    @property
    def domain(self) -> Any:
        return self.form("domain")


def storage_backend_service_key(name: str) -> str:
    return f"storage.backend.{name}"


__all__ = [
    "StorageError",
    "UNIT_NAME_RE",
    "UNIT_NAME_RE_COMPILED",
    "StorageBackend",
    "KvFacet",
    "KvUnit",
    "KvUnitDescriptor",
    "BackendRegistry",
    "Storage",
    "storage_backend_service_key",
]
