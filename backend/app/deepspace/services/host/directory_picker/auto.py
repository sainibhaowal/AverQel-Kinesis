"""Auto directory picker chooser for DeepSpace.

Resolves the host's situation once at boot and mounts the matching
native or browse backend.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil

from app.deepspace.services.host.directory_picker.browse import (
    create_browse_directory_picker_service,
)
from app.deepspace.services.host.directory_picker.native import (
    create_native_directory_picker_service,
)
from app.deepspace.services.host.directory_picker.seam import DirectoryPickerService

logger = logging.getLogger(__name__)


class DirectoryPickerAuto:
    """Adaptive chooser that resolves the host situation at boot."""

    def __init__(self) -> None:
        self._service: DirectoryPickerService | None = None
        self._backend_kind: str | None = None

    @property
    def service(self) -> DirectoryPickerService | None:
        return self._service

    @property
    def backend_kind(self) -> str | None:
        return self._backend_kind

    def resolve(self) -> DirectoryPickerService:
        """Resolve the backend at boot time."""
        kind = self._resolve_backend()
        self._backend_kind = kind

        if kind == "native":
            self._service = create_native_directory_picker_service()
        else:
            self._service = create_browse_directory_picker_service()

        logger.info(f"Directory picker resolved to: {kind}")
        return self._service

    def _resolve_backend(self) -> str:
        """Resolve the backend kind at boot time."""
        # Check for remote authority trust
        if self._has_remote_authority():
            logger.info("Remote authority detected, using browse backend")
            return "browse"

        # Check bind host
        if not self._is_loopback_bind():
            logger.info("Non-loopback bind, using browse backend")
            return "browse"

        # Check SSH launch
        if self._is_ssh_launch():
            logger.info("SSH launch detected, using browse backend")
            return "browse"

        # Platform-specific checks
        system = platform.system().lower()
        if system == "darwin" or system == "win32":
            return "native"

        if system == "linux":
            if self._has_linux_chooser() and self._has_display():
                return "native"

        return "browse"

    def _has_remote_authority(self) -> bool:
        """Check if there's a non-loopback trusted host."""
        # In a real implementation, this would check the webserver config
        # For now, return False
        return False

    def _is_loopback_bind(self) -> bool:
        """Check if the bind host is loopback."""
        # In a real implementation, this would check the webserver config
        # For now, assume loopback
        return True

    def _is_ssh_launch(self) -> bool:
        """Check if launched via SSH."""
        return bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))

    def _has_linux_chooser(self) -> bool:
        """Check if Linux has a chooser binary (zenity or kdialog)."""
        for binary in ("zenity", "kdialog"):
            if shutil.which(binary):
                return True
        return False

    def _has_display(self) -> bool:
        """Check if there's a display session."""
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def create_auto_directory_picker_service() -> DirectoryPickerService:
    """Create and resolve the auto directory picker service."""
    auto = DirectoryPickerAuto()
    return auto.resolve()


__all__ = ["DirectoryPickerAuto", "create_auto_directory_picker_service"]
