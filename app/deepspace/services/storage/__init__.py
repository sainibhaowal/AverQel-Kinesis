"""Storage hub for DeepSpace — named backend registry and form mounting.

Python-native port of the DeepSeek Harness storage package.
Provides BackendRegistry, Storage service, and form mounting.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

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
    """Backend interface for storage media."""

    def close(self) -> Any:
        raise NotImplementedError

    kv: Any = None  # Optional KvFacet


class KvFacet:
    """Key-value operations facet."""

    def open(self, descriptor: "KvUnitDescriptor") -> Any:
        raise NotImplementedError

    def close(self) -> Any:
        raise NotImplementedError


class KvUnitDescriptor:
    """Static identity and shape of one KV unit."""

    def __init__(
        self,
        name: str,
        version: int,
        tables: list[str],
        has_global: bool = False,
        layout: str | None = None,
        compatible_versions: list[int] | None = None,
    ) -> None:
        self.name = name
        self.version = version
        self.tables = tables
        self.has_global = has_global
        self.layout = layout
        self.compatible_versions = compatible_versions or []

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "tables": self.tables,
            "hasGlobal": self.has_global,
            "layout": self.layout,
            "compatibleVersions": self.compatible_versions,
        }


class KvUnit:
    """One opened unit interface."""

    def load_all(self) -> dict:
        raise NotImplementedError

    def put_record(self, table: str, key: str, value: Any) -> Any:
        raise NotImplementedError

    def delete_record(self, table: str, key: str) -> Any:
        raise NotImplementedError

    def delete_records(self, table: str, keys: list[str]) -> Any:
        raise NotImplementedError

    def set_global(self, value: Any) -> Any:
        raise NotImplementedError

    def close(self) -> Any:
        raise NotImplementedError

    def backup_record(self, table: str, key: str) -> str:
        raise NotImplementedError


class BackendRegistry:
    """Named backend registry for storage hub."""

    def __init__(self) -> None:
        self._backends: dict[str, StorageBackend] = {}

    def register(self, name: str, backend: StorageBackend) -> Callable[[], None]:
        """Register a named backend. Returns disposer."""
        if name in self._backends:
            raise StorageError(
                "duplicate-backend", f"storage backend '{name}' is already registered"
            )
        self._backends[name] = backend

        def disposer() -> None:
            if self._backends.get(name) is backend:
                del self._backends[name]

        return disposer

    def get(self, name: str) -> StorageBackend:
        """Resolve a backend by name."""
        backend = self._backends.get(name)
        if backend is None:
            registered = ", ".join(self._backends.keys()) or "none"
            raise StorageError(
                "backend-not-found",
                f"storage backend '{name}' is not registered (registered: {registered})",
            )
        return backend

    def names(self) -> list[str]:
        """Registered backend names."""
        return list(self._backends.keys())


class Storage:
    """Storage hub service."""

    def __init__(self, ctx: Any = None) -> None:
        self.backend = BackendRegistry()
        self._forms: dict[str, Any] = {}
        self._ctx = ctx

    def mount(self, form: str, facility: Any) -> Callable[[], None]:
        """Mount a data-form facility on the hub. Returns disposer."""
        if form in self._forms:
            raise StorageError(
                "duplicate-mount", f"storage form '{form}' is already mounted"
            )
        self._forms[form] = facility

        def disposer() -> None:
            if self._forms.get(form) is facility:
                del self._forms[form]

        return disposer

    def form(self, form: str) -> Any:
        """Resolve a mounted data form."""
        if form not in self._forms:
            raise StorageError(
                "form-not-mounted", f"storage form '{form}' is not mounted"
            )
        return self._forms[form]

    @property
    def domain(self) -> Any:
        """Domain data form."""
        return self.form("domain")


def storage_backend_service_key(name: str) -> str:
    """Derive the lifecycle service key for a named backend."""
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
PYEOF
echo "written"
