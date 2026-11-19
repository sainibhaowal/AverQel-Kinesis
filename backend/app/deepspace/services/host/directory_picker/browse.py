"""Browse directory picker backend for DeepSpace.

Provides in-app directory listing and creation for remote clients.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from app.deepspace.services.host.directory_picker.seam import (
    BrowseCapability,
    DirectoryEntry,
    DirectoryListing,
    DirectoryPicker,
    DirectoryPickerError,
    DirectoryPickerErrorCode,
    DirectoryPickerService,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_ENTRIES = 1000


@dataclass
class BrowseConfig:
    max_entries: int = DEFAULT_MAX_ENTRIES


class _BoundBrowseCapability(BrowseCapability):
    """Capability adapter that binds the backend implementation."""

    def __init__(self, picker: BrowseDirectoryPicker) -> None:
        self._picker = picker

    async def list(self, path: str | None = None) -> DirectoryListing:
        return await self._picker.list(path)

    async def create_directory(self, path: str, name: str) -> DirectoryEntry:
        return await self._picker.create_directory(path, name)


class BrowseDirectoryPicker(DirectoryPicker):
    """In-app directory browser backend."""

    def __init__(self, config: BrowseConfig | None = None) -> None:
        self.config = config or BrowseConfig()

    @property
    def capability(self) -> BrowseCapability:
        return _BoundBrowseCapability(self)

    async def list(self, path: str | None = None) -> DirectoryListing:
        """List one directory level."""
        if path is None:
            path = str(Path.home())

        # Validate fully qualified path
        resolved_path = self._resolve_path(path)
        if resolved_path is None:
            raise DirectoryPickerError(DirectoryPickerErrorCode.DIRECTORY_UNREADABLE, path)

        # List directory with bounded window
        entries: list[DirectoryEntry] = []
        crumbs: list[DirectoryEntry] = []
        truncated = False

        try:
            # Build crumbs (ancestor chain)
            current = Path(path).resolve()
            while current != current.parent:
                crumbs.insert(
                    0, DirectoryEntry(path=str(current), hidden=current.name.startswith("."))
                )
                current = current.parent
            # Add root
            crumbs.insert(0, DirectoryEntry(path="/", hidden=False))

            # List directory with bounded window
            entries_list: list[os.DirEntry[str]] = []
            with os.scandir(resolved_path) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=True):
                        entries_list.append(entry)

            # Sort by name
            entries_list.sort(key=lambda e: e.name.lower())

            # Apply max_entries bound with truncation
            if len(entries_list) > self.config.max_entries:
                truncated = True
                entries_list = entries_list[: self.config.max_entries]

            for entry in entries_list:
                entries.append(
                    DirectoryEntry(
                        path=str(Path(resolved_path) / entry.name),
                        hidden=entry.name.startswith("."),
                    )
                )

            return DirectoryListing(
                entries=entries,
                crumbs=crumbs,
                truncated=truncated,
                home=str(Path.home()),
            )

        except PermissionError as exc:
            raise DirectoryPickerError(DirectoryPickerErrorCode.DIRECTORY_UNREADABLE, path) from exc
        except FileNotFoundError as exc:
            raise DirectoryPickerError(DirectoryPickerErrorCode.DIRECTORY_UNREADABLE, path) from exc
        except OSError as exc:
            raise DirectoryPickerError(
                DirectoryPickerErrorCode.DIRECTORY_UNREADABLE, path, str(exc)
            ) from exc

    async def create_directory(self, path: str, name: str) -> DirectoryEntry:
        """Create a child directory under an existing parent."""
        # Validate name
        if not name or name.strip() != name:
            raise DirectoryPickerError(
                DirectoryPickerErrorCode.DIRECTORY_CREATE_FAILED, path, "Invalid directory name"
            )
        if name in (".", "..") or "/" in name or "\\" in name:
            raise DirectoryPickerError(
                DirectoryPickerErrorCode.DIRECTORY_CREATE_FAILED, path, "Invalid directory name"
            )

        parent_path = self._resolve_path(path)
        if parent_path is None:
            raise DirectoryPickerError(DirectoryPickerErrorCode.DIRECTORY_UNREADABLE, path)

        new_path = Path(parent_path) / name

        try:
            new_path.mkdir(parents=False, exist_ok=False)
            return DirectoryEntry(path=str(new_path), hidden=name.startswith("."))
        except FileExistsError as exc:
            raise DirectoryPickerError(
                DirectoryPickerErrorCode.DIRECTORY_EXISTS, str(new_path)
            ) from exc
        except PermissionError as exc:
            raise DirectoryPickerError(
                DirectoryPickerErrorCode.DIRECTORY_CREATE_FAILED, str(new_path), "Permission denied"
            ) from exc
        except OSError as exc:
            raise DirectoryPickerError(
                DirectoryPickerErrorCode.DIRECTORY_CREATE_FAILED, str(new_path), str(exc)
            ) from exc

    def _resolve_path(self, path: str) -> str | None:
        """Resolve and validate a fully qualified path."""
        if not path:
            return None

        path_obj = Path(path)

        # Must be absolute
        if not path_obj.is_absolute():
            return None

        # On Windows, must be drive-qualified or UNC
        if sys.platform == "win32":
            if not (len(path) >= 3 and path[1] == ":" and path[2] in ("\\", "/")):
                if not (path.startswith("\\\\") or path.startswith("//")):
                    return None

        try:
            resolved = Path(path).resolve()
            return str(resolved)
        except (OSError, ValueError):
            return None


def create_browse_directory_picker_service(
    config: BrowseConfig | None = None,
) -> DirectoryPickerService:
    """Create and register the browse directory picker service."""
    service = DirectoryPickerService()
    backend = BrowseDirectoryPicker(config)
    _ = service.register_backend(backend)
    return service


__all__ = ["BrowseDirectoryPicker", "BrowseConfig", "create_browse_directory_picker_service"]
