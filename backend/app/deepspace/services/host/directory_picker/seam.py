"""Directory Picker Seam for DeepSpace host.

Provides the service contract, capability vocabulary, and error codes
for native and browse backends.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class DirectoryPickerErrorCode(str, Enum):
    """Error codes for directory picker operations."""

    DIRECTORY_UNREADABLE = "directory-unreadable"
    DIRECTORY_EXISTS = "directory-exists"
    DIRECTORY_CREATE_FAILED = "directory-create-failed"


class DirectoryPickerError(Exception):
    """Typed error for directory picker operations."""

    def __init__(
        self, code: DirectoryPickerErrorCode, path: str, message: str | None = None
    ) -> None:
        self.code = code
        self.path = path
        self.message = message or f"{code.value}: {path}"
        super().__init__(self.message)


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    """A directory entry returned by the browse backend."""

    path: str
    hidden: bool = False


@dataclass(frozen=True, slots=True)
class DirectoryListing:
    """A directory listing returned by the browse backend."""

    entries: list[DirectoryEntry]
    crumbs: list[DirectoryEntry]  # Ancestor chain from root to listed directory
    truncated: bool = False  # True if listing was cut due to maxEntries
    home: str | None = None  # Home anchor path


class DirectoryPickerCapabilityKind(str, Enum):
    """Capability kinds for the directory picker."""

    NATIVE = "native"
    BROWSE = "browse"


class NativeCapability:
    """Native OS chooser capability."""

    kind: DirectoryPickerCapabilityKind = DirectoryPickerCapabilityKind.NATIVE

    async def pick(self, signal: Any) -> str | None:
        """Open native OS chooser and return chosen path or None on cancel."""
        raise NotImplementedError


class BrowseCapability:
    """In-app browser capability."""

    kind: DirectoryPickerCapabilityKind = DirectoryPickerCapabilityKind.BROWSE

    async def list(self, path: str | None = None) -> DirectoryListing:
        """List one directory level."""
        raise NotImplementedError

    async def create_directory(self, path: str, name: str) -> DirectoryEntry:
        """Create a child directory."""
        raise NotImplementedError


DirectoryPickerCapability = NativeCapability | BrowseCapability


class DirectoryPicker(ABC):
    """Abstract directory picker service."""

    @property
    @abstractmethod
    def capability(self) -> DirectoryPickerCapability:
        """Return the capability provided by this backend."""
        pass


class DirectoryPickerService:
    """
    Service that manages the directory picker backend.
    Only one backend can be registered at a time.
    """

    def __init__(self) -> None:
        self._backend: DirectoryPicker | None = None
        self._disposer: Callable[[], None] | None = None

    def register_backend(self, backend: DirectoryPicker) -> Callable[[], None]:
        """Register a backend. Returns a disposer."""
        if self._backend is not None:
            raise RuntimeError("Directory picker backend already registered")
        self._backend = backend

        def dispose() -> None:
            if self._backend is backend:
                self._backend = None

        self._disposer = dispose
        return dispose

    @property
    def backend(self) -> DirectoryPicker | None:
        return self._backend

    @property
    def capability(self) -> DirectoryPickerCapability | None:
        return self._backend.capability if self._backend else None

    def dispose(self) -> None:
        if self._disposer:
            self._disposer()
            self._disposer = None
        self._backend = None


__all__ = [
    "DirectoryPicker",
    "DirectoryPickerService",
    "DirectoryPickerCapability",
    "DirectoryPickerCapabilityKind",
    "NativeCapability",
    "BrowseCapability",
    "DirectoryEntry",
    "DirectoryListing",
    "DirectoryPickerError",
    "DirectoryPickerErrorCode",
]
