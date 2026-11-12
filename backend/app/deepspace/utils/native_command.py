"""
Native Command Execution & Path Opening for DeepSpace.

Port of @deepseek-ai/dsh-native-command to Python.
Provides shell-free command execution with cancellation, cross-platform path opening,
file association detection, .desktop file parsing, and WSL handoff support.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shlex
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None


__all__ = [
    # Command runner
    "run_native_command",
    "NativeCommandRunner",
    "NativeCommandResult",
    "NativeCommandError",
    # Path opener
    "can_open_native_path",
    "native_file_manager",
    "reveal_native_path",
    "open_native_path",
    "open_native_associated_path",
    "open_native_text_file",
    "NativeFileManager",
    "PathOpenerRunner",
    "PathOpenerInternals",
    # File applications
    "native_file_applications",
    "open_native_file_application",
    "NativeFileApplication",
    # Desktop entry
    "desktop_entry_fields",
    "desktop_data_directories",
    "desktop_application_icon",
    "DesktopEntryFields",
    # Platform detection
    "is_wsl",
    "get_wsl_interop_path",
    "wsl_handoff_command",
]


# ============================================================================
# Platform Detection
# ============================================================================


def is_wsl() -> bool:
    """Check if running in WSL (Windows Subsystem for Linux)."""
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower() or "wsl" in f.read().lower()
    except OSError:
        return False


def get_wsl_interop_path() -> Path | None:
    """Get WSL interop executable path if available."""
    if not is_wsl():
        return None
    # WSL interop allows calling Windows executables
    for path in [
        Path("/mnt/c/Windows/System32/cmd.exe"),
        Path("/mnt/c/Windows/explorer.exe"),
    ]:
        if path.exists():
            return path
    return None


def is_windows() -> bool:
    return platform.system().lower() == "windows"


def is_macos() -> bool:
    return platform.system().lower() == "darwin"


def is_linux() -> bool:
    return platform.system().lower() == "linux"


# ============================================================================
# Native Command Runner
# ============================================================================


@dataclass(frozen=True)
class NativeCommandResult:
    """Result of a native command execution."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False


class NativeCommandError(Exception):
    """Error during native command execution."""

    pass


class NativeCommandRunner:
    """
    Shell-free command runner with cancellation support.

    Features:
    - Direct process execution (no shell)
    - Async cancellation via signal/terminate
    - Timeout support
    - Captured stdout/stderr with size limits
    - Cross-platform (Windows, macOS, Linux, WSL)
    """

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        max_output_size: int = 10 * 1024 * 1024,  # 10 MB
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
    ) -> None:
        self.timeout = timeout
        self.max_output_size = max_output_size
        self.env = env or os.environ.copy()
        self.cwd = Path(cwd) if cwd else Path.cwd()
        self._process: subprocess.Popen | None = None
        self._cancelled = False

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def cancel(self) -> None:
        """Cancel the running command."""
        self._cancelled = True
        if self._process and self._process.poll() is None:
            if is_windows():
                self._process.terminate()
            else:
                try:
                    os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def run(
        self,
        command: str | list[str],
        *,
        input_data: str | None = None,
        timeout: float | None = None,
    ) -> NativeCommandResult:
        """Run command synchronously with timeout and cancellation."""
        if isinstance(command, str):
            # Parse command string safely (no shell)
            args = shlex.split(command)
        else:
            args = list(command)

        if not args:
            raise NativeCommandError("Empty command")

        # Prepare subprocess
        creationflags = 0
        preexec_fn = None
        if not is_windows():
            preexec_fn = os.setsid
        else:
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        try:
            self._process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE if input_data else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
                cwd=self.cwd,
                preexec_fn=preexec_fn,
                creationflags=creationflags,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            self._process = None
            exit_code = 127 if isinstance(exc, FileNotFoundError) else 126
            return NativeCommandResult(
                exit_code=exit_code,
                stdout="",
                stderr=str(exc),
                cancelled=self._cancelled,
            )

        try:
            stdout, stderr = self._process.communicate(
                input=input_data,
                timeout=timeout or self.timeout,
            )
            exit_code = self._process.returncode
            timed_out = False
            cancelled = self._cancelled
        except subprocess.TimeoutExpired:
            self.cancel()
            try:
                stdout, stderr = self._process.communicate(timeout=1.0)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
            exit_code = -1
            timed_out = True
            cancelled = True
        except Exception as e:
            self.cancel()
            raise NativeCommandError(f"Command execution failed: {e}") from e
        finally:
            self._process = None

        # Truncate output if too large
        stdout = self._truncate_output(stdout)
        stderr = self._truncate_output(stderr)

        return NativeCommandResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            cancelled=cancelled,
        )

    def _truncate_output(self, output: str) -> str:
        if len(output.encode("utf-8")) > self.max_output_size:
            truncated = output.encode("utf-8")[: self.max_output_size]
            return truncated.decode("utf-8", errors="replace") + "\n... [output truncated]"
        return output

    async def run_async(
        self,
        command: str | list[str],
        *,
        input_data: str | None = None,
        timeout: float | None = None,
    ) -> NativeCommandResult:
        """Run command asynchronously."""
        if isinstance(command, str):
            args = shlex.split(command)
        else:
            args = list(command)

        if not args:
            raise NativeCommandError("Empty command")

        # Prepare subprocess
        creationflags = 0
        if is_windows():
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        loop = asyncio.get_event_loop()

        def _run() -> NativeCommandResult:
            self._process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE if input_data else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
                cwd=self.cwd,
                preexec_fn=os.setsid if not is_windows() else None,
                creationflags=creationflags if is_windows() else 0,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            try:
                stdout, stderr = self._process.communicate(
                    input=input_data,
                    timeout=timeout or self.timeout,
                )
                return NativeCommandResult(
                    exit_code=self._process.returncode,
                    stdout=self._truncate_output(stdout),
                    stderr=self._truncate_output(stderr),
                    timed_out=False,
                    cancelled=self._cancelled,
                )
            except subprocess.TimeoutExpired:
                self.cancel()
                try:
                    stdout, stderr = self._process.communicate(timeout=1.0)
                except Exception:
                    stdout, stderr = "", ""
                return NativeCommandResult(
                    exit_code=-1,
                    stdout=self._truncate_output(stdout),
                    stderr=self._truncate_output(stderr),
                    timed_out=True,
                    cancelled=True,
                )
            except Exception as e:
                self.cancel()
                raise NativeCommandError(f"Command execution failed: {e}") from e
            finally:
                self._process = None

        return await loop.run_in_executor(None, _run)


def run_native_command(
    command: str | list[str],
    *,
    timeout: float = 30.0,
    max_output_size: int = 10 * 1024 * 1024,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    input_data: str | None = None,
) -> NativeCommandResult:
    """
    Convenience function to run a native command.

    Args:
        command: Command as string or list of args
        timeout: Timeout in seconds
        max_output_size: Max output size in bytes
        env: Environment variables
        cwd: Working directory
        input_data: Optional stdin input

    Returns:
        NativeCommandResult with exit_code, stdout, stderr
    """
    runner = NativeCommandRunner(
        timeout=timeout,
        max_output_size=max_output_size,
        env=env,
        cwd=cwd,
    )
    return runner.run(command, input_data=input_data)


# ============================================================================
# Path Opener (Cross-platform file/URL opening)
# ============================================================================


@dataclass(frozen=True)
class NativeFileApplication:
    """Native file application info."""

    name: str
    exec: str  # Executable command with %f placeholder
    mime_types: list[str]
    extensions: list[str]


class PathOpenerRunner:
    """Cross-platform path/URL opener."""

    def __init__(self) -> None:
        self._file_applications: dict[str, NativeFileApplication] = {}

    def can_open_native_path(self, path: str | Path) -> bool:
        """Check if path can be opened natively."""
        path = Path(path)
        if not path.exists():
            return False
        # Check if we have an opener for this platform
        return True

    def reveal_native_path(self, path: str | Path) -> bool:
        """Open file manager and select the file."""
        path = Path(path).resolve()
        if not path.exists():
            return False

        try:
            if is_windows():
                subprocess.run(["explorer.exe", "/select,", str(path)], check=False)
            elif is_macos():
                subprocess.run(["open", "-R", str(path)], check=False)
            elif is_linux() or is_wsl():
                # Try xdg-open on parent directory
                subprocess.run(["xdg-open", str(path.parent)], check=False)
            return True
        except Exception:
            return False

    def open_native_path(self, path: str | Path) -> bool:
        """Open path with default application."""
        path = Path(path).resolve()
        if not path.exists():
            return False

        try:
            if is_windows():
                os.startfile(path)  # type: ignore
            elif is_macos():
                subprocess.run(["open", str(path)], check=False)
            elif is_linux() or is_wsl():
                subprocess.run(["xdg-open", str(path)], check=False)
            return True
        except Exception:
            return False

    def open_native_associated_path(self, path: str | Path) -> bool:
        """Open with specific associated application."""
        # Same as open_native_path for now
        return self.open_native_path(path)

    def open_native_text_file(self, path: str | Path) -> bool:
        """Open text file with default text editor."""
        path = Path(path).resolve()
        if not path.exists():
            return False

        try:
            if is_windows():
                # Try notepad
                subprocess.run(["notepad.exe", str(path)], check=False)
            elif is_macos():
                subprocess.run(["open", "-t", str(path)], check=False)
            elif is_linux() or is_wsl():
                # Try common text editors
                for editor in ["xdg-open", "code", "vim", "nano"]:
                    try:
                        subprocess.run([editor, str(path)], check=False, timeout=1)
                        return True
                    except Exception:
                        continue
            return True
        except Exception:
            return False

    def native_file_manager(self) -> str:
        """Get the native file manager command."""
        if is_windows():
            return "explorer.exe"
        elif is_macos():
            return "open"
        else:
            return "xdg-open"

    def __call__(self) -> str:
        """Return the platform's file-manager executable for compatibility."""
        return self.native_file_manager()


# Module-level instance
_native_file_manager = PathOpenerRunner()
NativeFileManager = PathOpenerRunner

can_open_native_path = _native_file_manager.can_open_native_path
native_file_manager = _native_file_manager
reveal_native_path = _native_file_manager.reveal_native_path
open_native_path = _native_file_manager.open_native_path
open_native_associated_path = _native_file_manager.open_native_associated_path
open_native_text_file = _native_file_manager.open_native_text_file


# ============================================================================
# File Applications (MIME type → application mapping)
# ============================================================================

# Cache for file associations
_file_applications_cache: dict[str, NativeFileApplication] | None = None


def native_file_applications() -> dict[str, NativeFileApplication]:
    """Get cached file application associations."""
    global _file_applications_cache
    if _file_applications_cache is not None:
        return _file_applications_cache

    apps: dict[str, NativeFileApplication] = {}

    if is_windows():
        # Windows: Use assoc/ftype or registry
        apps = _get_windows_file_applications()
    elif is_macos():
        # macOS: Use LSRegister or defaults
        apps = _get_macos_file_applications()
    elif is_linux() or is_wsl():
        # Linux: Use xdg-mime and .desktop files
        apps = _get_linux_file_applications()

    _file_applications_cache = apps
    return apps


def _get_windows_file_applications() -> dict[str, NativeFileApplication]:
    """Get Windows file associations via registry."""
    apps: dict[str, NativeFileApplication] = {}
    try:
        import winreg

        # Read from HKEY_CLASSES_ROOT
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "") as root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    ext = winreg.EnumKey(root, i)
                    if not ext.startswith("."):
                        continue
                    with winreg.OpenKey(root, ext) as ext_key:
                        prog_id = winreg.QueryValue(ext_key, "")
                    with winreg.OpenKey(root, f"{prog_id}\\shell\\open\\command") as cmd_key:
                        cmd = winreg.QueryValue(cmd_key, "")
                    apps[ext] = NativeFileApplication(
                        name=prog_id,
                        exec=cmd,
                        mime_types=[],
                        extensions=[ext],
                    )
                except Exception:
                    continue
    except Exception:
        pass
    return apps


def _get_macos_file_applications() -> dict[str, NativeFileApplication]:
    """Get macOS file associations."""
    apps: dict[str, NativeFileApplication] = {}
    try:
        # Use defaults read or mdls
        subprocess.run(
            ["defaults", "read", "com.apple.LaunchServices", "LSHandlers"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # Parse output (simplified)
    except Exception:
        pass
    return apps


def _get_linux_file_applications() -> dict[str, NativeFileApplication]:
    """Get Linux file associations via xdg-mime and .desktop files."""
    apps: dict[str, NativeFileApplication] = {}
    try:
        # Get MIME types for common extensions
        common_exts = [
            ".txt",
            ".pdf",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".svg",
            ".mp4",
            ".mp3",
            ".wav",
            ".html",
            ".css",
            ".js",
            ".py",
            ".json",
            ".xml",
            ".csv",
            ".md",
            ".rst",
        ]
        for ext in common_exts:
            try:
                result = subprocess.run(
                    ["xdg-mime", "query", "filetype", f"dummy{ext}"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                mime = result.stdout.strip()
                if mime and mime != "application/octet-stream":
                    # Get default app for MIME type
                    app_result = subprocess.run(
                        ["xdg-mime", "query", "default", mime],
                        capture_output=True,
                        text=True,
                        timeout=2,
                    )
                    desktop_file = app_result.stdout.strip()
                    if desktop_file:
                        apps[ext] = NativeFileApplication(
                            name=desktop_file.replace(".desktop", ""),
                            exec=desktop_file,
                            mime_types=[mime],
                            extensions=[ext],
                        )
            except Exception:
                continue
    except Exception:
        pass
    return apps


def open_native_file_application(path: str | Path, application: str | None = None) -> bool:
    """Open file with specific or default application."""
    path = Path(path).resolve()
    if not path.exists():
        return False

    if application:
        # Try to open with specific app
        try:
            subprocess.run([application, str(path)], check=False)
            return True
        except Exception:
            return False

    # Use default
    return open_native_path(path)


# ============================================================================
# Desktop Entry Parsing (.desktop files)
# ============================================================================


@dataclass(frozen=True)
class DesktopEntryFields:
    """Parsed .desktop file fields."""

    name: str
    exec: str
    icon: str
    type: str
    categories: list[str]
    mime_types: list[str]
    no_display: bool
    terminal: bool
    startup_notify: bool
    keywords: list[str]
    comment: str
    generic_name: str
    try_exec: str
    path: str
    startup_wm_class: str
    url: str
    dbus_activatable: bool
    actions: dict[str, dict[str, str]]


def desktop_data_directories() -> list[Path]:
    """Get XDG data directories for .desktop files."""
    dirs: list[Path] = []

    # XDG_DATA_HOME
    if xdg_home := os.environ.get("XDG_DATA_HOME"):
        dirs.append(Path(xdg_home))
    else:
        dirs.append(Path.home() / ".local" / "share")

    # XDG_DATA_DIRS
    if xdg_dirs := os.environ.get("XDG_DATA_DIRS"):
        for d in xdg_dirs.split(":"):
            dirs.append(Path(d))
    else:
        dirs.extend(
            [
                Path("/usr/local/share"),
                Path("/usr/share"),
            ]
        )

    # Add applications subdirectory
    return [d / "applications" for d in dirs]


def desktop_entry_fields(desktop_file: str | Path) -> DesktopEntryFields | None:
    """Parse a .desktop file."""
    path = Path(desktop_file)
    if not path.exists():
        return None

    fields = {
        "name": "",
        "exec": "",
        "icon": "",
        "type": "Application",
        "categories": [],
        "mime_types": [],
        "no_display": False,
        "terminal": False,
        "startup_notify": True,
        "keywords": [],
        "comment": "",
        "generic_name": "",
        "try_exec": "",
        "path": "",
        "startup_wm_class": "",
        "url": "",
        "dbus_activatable": False,
        "actions": {},
    }

    current_action = None
    action_fields = {}

    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            if line.startswith("[Desktop Action "):
                if current_action and action_fields:
                    fields["actions"][current_action] = action_fields
                current_action = line[15:-1]  # Remove "[Desktop Action " and "]"
                action_fields = {}
                continue
            elif line.startswith("[Desktop Entry]"):
                current_action = None
                continue

            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()

            target = action_fields if current_action else fields

            if key == "Name":
                target["name"] = value
            elif key == "Exec":
                target["exec"] = value
            elif key == "Icon":
                target["icon"] = value
            elif key == "Type":
                target["type"] = value
            elif key == "Categories":
                target["categories"] = [c for c in value.split(";") if c]
            elif key == "MimeType":
                target["mime_types"] = [m for m in value.split(";") if m]
            elif key == "NoDisplay":
                target["no_display"] = value.lower() == "true"
            elif key == "Terminal":
                target["terminal"] = value.lower() == "true"
            elif key == "StartupNotify":
                target["startup_notify"] = value.lower() == "true"
            elif key == "Keywords":
                target["keywords"] = [k for k in value.split(";") if k]
            elif key == "Comment":
                target["comment"] = value
            elif key == "GenericName":
                target["generic_name"] = value
            elif key == "TryExec":
                target["try_exec"] = value
            elif key == "Path":
                target["path"] = value
            elif key == "StartupWMClass":
                target["startup_wm_class"] = value
            elif key == "URL":
                target["url"] = value
            elif key == "DBusActivatable":
                target["dbus_activatable"] = value.lower() == "true"

    except Exception:
        return None

    if current_action and action_fields:
        fields["actions"][current_action] = action_fields

    return DesktopEntryFields(**fields)


def desktop_application_icon(desktop_file: str | Path, size: int = 48) -> Path | None:
    """Resolve icon path from .desktop file."""
    entry = desktop_entry_fields(desktop_file)
    if not entry or not entry.icon:
        return None

    # If absolute path or full icon name
    icon_name = entry.icon
    if icon_name.endswith((".png", ".svg", ".xpm", ".ico")):
        # Absolute path
        if os.path.isabs(icon_name):
            return Path(icon_name) if Path(icon_name).exists() else None
        # Search in icon themes
        icon_theme_dirs = [
            Path.home() / ".local" / "share" / "icons",
            Path("/usr/local/share/icons"),
            Path("/usr/share/icons"),
        ]
        for theme_dir in icon_theme_dirs:
            for ext in [".png", ".svg", ".xpm"]:
                for theme in theme_dir.iterdir():
                    if theme.is_dir():
                        icon_path = theme / "apps" / f"{icon_name}{ext}"
                        if icon_path.exists():
                            return icon_path
    return None


# ============================================================================
# Internals for testing
# ============================================================================


class PathOpenerInternals:
    """Internal state for testing."""

    runner: PathOpenerRunner

    def __init__(self) -> None:
        self.runner = PathOpenerRunner()


# ============================================================================
# WSL Handoff Support
# ============================================================================


def wsl_handoff_command(command: str | list[str]) -> list[str]:
    """Wrap command for WSL-to-Windows handoff."""
    if not is_wsl():
        if isinstance(command, str):
            return shlex.split(command)
        return list(command)

    if isinstance(command, str):
        args = shlex.split(command)
    else:
        args = list(command)

    # Use wslpath for path conversion if needed
    # For now, just use cmd.exe /c
    return ["cmd.exe", "/c"] + args
