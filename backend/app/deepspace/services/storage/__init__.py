"""Storage hub for DeepSpace — named backend registry and form mounting.

Python-native port of the DeepSeek Harness storage package.
"""

from __future__ import annotations

import logging
import re
from typing import Any, TypeVar

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
    def close(self) -> Any:
        raise NotImplementedError

    kv: Any = None


class KvFacet:
    def open(self, descriptor: KvUnitDescriptor) -> Any:
        raise NotImplementedError

    def close(self) -> Any:
        raise NotImplementedError


class KvUnitDescriptor:
    def __init__(
        self, name, version, tables, has_global=False, layout=None, compatible_versions=None
    ):
        self.name = name
        self.version = version
        self.tables = tables
        self.has_global = has_global
        self.layout = layout
        self.compatible_versions = compatible_versions or []

    def to_dict(self):
        return {
            "name": self.name,
            "version": self.version,
            "tables": self.tables,
            "hasGlobal": self.has_global,
            "layout": self.layout,
            "compatibleVersions": self.compatible_versions,
        }


class KvUnit:
    def load_all(self):
        raise NotImplementedError

    def put_record(self, table, key, value):
        raise NotImplementedError

    def delete_record(self, table, key):
        raise NotImplementedError

    def delete_records(self, table, keys):
        raise NotImplementedError

    def set_global(self, value):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def backup_record(self, table, key):
        raise NotImplementedError


class BackendRegistry:
    def __init__(self):
        self._backends = {}

    def register(self, name, backend):
        if name in self._backends:
            raise StorageError(
                "duplicate-backend", f"storage backend '{name}' is already registered"
            )
        self._backends[name] = backend

        def disposer():
            if self._backends.get(name) is backend:
                del self._backends[name]

        return disposer

    def get(self, name):
        backend = self._backends.get(name)
        if backend is None:
            registered = ", ".join(self._backends.keys()) or "none"
            raise StorageError(
                "backend-not-found",
                f"storage backend '{name}' is not registered (registered: {registered})",
            )
        return backend

    def names(self):
        return list(self._backends.keys())


class Storage:
    def __init__(self, ctx=None):
        self.backend = BackendRegistry()
        self._forms = {}
        self._ctx = ctx

    def mount(self, form, facility):
        if form in self._forms:
            raise StorageError("duplicate-mount", f"storage form '{form}' is already mounted")
        self._forms[form] = facility

        def disposer():
            if self._forms.get(form) is facility:
                del self._forms[form]

        return disposer

    def form(self, form):
        if form not in self._forms:
            raise StorageError("form-not-mounted", f"storage form '{form}' is not mounted")
        return self._forms[form]

    @property
    def domain(self):
        return self.form("domain")


def storage_backend_service_key(name):
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
