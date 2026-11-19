"""Native directory picker backend for DeepSpace.

Provides native OS directory chooser on macOS, Linux, and Windows.
"""

from __future__ import annotations

import asyncio
import logging
import platform
from typing import Any

from app.deepspace.services.host.directory_picker.seam import (
    DirectoryPicker,
    DirectoryPickerService,
    NativeCapability,
)

logger = logging.getLogger(__name__)


class _BoundNativeCapability(NativeCapability):
    """Capability adapter that binds the backend implementation."""

    def __init__(self, picker: NativeDirectoryPicker) -> None:
        self._picker = picker

    async def pick(self, signal: Any) -> str | None:
        return await self._picker.pick(signal)


class NativeDirectoryPicker(DirectoryPicker):
    """Native OS directory picker backend."""

    def __init__(self) -> None:
        self._platform = platform.system().lower()

    @property
    def capability(self) -> NativeCapability:
        return _BoundNativeCapability(self)

    async def pick(self, signal: Any) -> str | None:
        """
        Open native OS directory chooser and return chosen path or None on cancel.
        """
        if self._platform == "darwin":
            return await self._pick_macos(signal)
        elif self._platform == "linux":
            return await self._pick_linux(signal)
        elif self._platform == "windows":
            return await self._pick_windows(signal)
        else:
            raise RuntimeError(f"Unsupported platform: {self._platform}")

    async def _pick_macos(self, signal: Any) -> str | None:
        """Open macOS native directory chooser using osascript."""
        script = """
        tell application "System Events"
            activate
        end tell
        set chosenFolder to choose folder with prompt "Select Workspace Directory"
        return POSIX path of chosenFolder
        """

        proc = await asyncio.create_subprocess_exec(
            "osascript",
            "-l",
            "JavaScript",
            "-e",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Wire abort signal
        def abort_handler() -> None:
            proc.terminate()

        if hasattr(signal, "addEventListener"):
            signal.addEventListener("abort", abort_handler)

        try:
            stdout, stderr = await proc.communicate()
            if signal.aborted:
                return None
            if proc.returncode == 0:
                path = stdout.decode().strip()
                return path if path else None
            if proc.returncode == -128:  # User cancelled
                return None
            raise RuntimeError(f"osascript failed: {stderr.decode()}")
        finally:
            if hasattr(signal, "removeEventListener"):
                signal.removeEventListener("abort", abort_handler)

    async def _pick_linux(self, signal: Any) -> str | None:
        """Open Linux directory chooser using Zenity or KDialog."""
        # Try Zenity first
        for cmd in [
            ["zenity", "--file-selection", "--directory", "--title=Select Workspace Directory"],
            ["kdialog", "--getexistingdirectory", "--title", "Select Workspace Directory"],
        ]:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                def abort_handler(p: asyncio.subprocess.Process = proc) -> None:
                    p.terminate()

                if hasattr(signal, "addEventListener"):
                    signal.addEventListener("abort", abort_handler)

                try:
                    stdout, stderr = await proc.communicate()
                    if signal.aborted:
                        return None
                    if proc.returncode == 0:
                        path = stdout.decode().strip()
                        return path if path else None
                    if proc.returncode == 1:  # User cancelled
                        return None
                finally:
                    if hasattr(signal, "removeEventListener"):
                        signal.removeEventListener("abort", abort_handler)
            except FileNotFoundError:
                continue  # Try next tool

        raise RuntimeError("No directory chooser available (install zenity or kdialog)")

    async def _pick_windows(self, signal: Any) -> str | None:
        """Open Windows directory chooser using PowerShell and IFileOpenDialog."""
        # Use PowerShell to invoke IFileOpenDialog
        ps_script = """
        Add-Type -AssemblyName System.Windows.Forms
        $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
        $dialog.Description = "Select Workspace Directory"
        $dialog.ShowNewFolderButton = $true
        $result = $dialog.ShowDialog()
        if ($result -eq "OK") {
            Write-Output $dialog.SelectedPath
        }
        """

        proc = await asyncio.create_subprocess_exec(
            "powershell",
            "-NoProfile",
            "-Command",
            ps_script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        def abort_handler() -> None:
            proc.terminate()

        if hasattr(signal, "addEventListener"):
            signal.addEventListener("abort", abort_handler)

        try:
            stdout, stderr = await proc.communicate()
            if signal.aborted:
                return None
            if proc.returncode == 0:
                path = stdout.decode().strip()
                return path if path else None
            return None
        finally:
            if hasattr(signal, "removeEventListener"):
                signal.removeEventListener("abort", abort_handler)


def create_native_directory_picker_service() -> DirectoryPickerService:
    """Create and register the native directory picker service."""
    service = DirectoryPickerService()
    backend = NativeDirectoryPicker()
    _ = service.register_backend(backend)
    return service


__all__ = ["NativeDirectoryPicker", "create_native_directory_picker_service"]
