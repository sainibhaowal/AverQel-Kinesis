"""Open In App service for DeepSpace.

Provides a fixed catalog of editors, Git GUIs, terminals, and file managers
with per-platform detection, icon extraction, and launch capabilities.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OpenInAppConfig:
    """Configuration for Open In App service."""

    probe_timeout_ms: int = 10000
    icon_timeout_ms: int = 10000
    launch_watch_ms: int = 1000


@dataclass(frozen=True, slots=True)
class AppEntry:
    """An application entry in the catalog."""

    id: str
    name: str
    category: str  # editor, git-gui, terminal, file-manager
    platforms: list[str]  # macos, windows, linux
    launcher_sources: list[
        str
    ]  # fixed, app, xcode, cli, file, scan, app-paths, install-record, github-desktop, desktop
    icon_source: str | None = None


@dataclass(frozen=True, slots=True)
class VerifiedLauncher:
    """A verified launcher for an application."""

    app_id: str
    argv: list[str]
    fallback_argv: list[str] | None = None
    icon_path: str | None = None


# Fixed application catalog
OPEN_IN_APP_CATALOG: list[AppEntry] = [
    # Editors & IDEs
    AppEntry("cursor", "Cursor", "editor", ["macos", "linux", "windows"], ["app", "cli"], "cursor"),
    AppEntry(
        "vscode",
        "Visual Studio Code",
        "editor",
        ["macos", "linux", "windows"],
        ["app", "cli"],
        "vscode",
    ),
    AppEntry(
        "vscode-insiders",
        "VS Code Insiders",
        "editor",
        ["macos", "linux", "windows"],
        ["app", "cli"],
        "vscode-insiders",
    ),
    AppEntry(
        "windsurf", "Windsurf", "editor", ["macos", "linux", "windows"], ["app", "cli"], "windsurf"
    ),
    AppEntry("zed", "Zed", "editor", ["macos", "linux"], ["app", "cli"], "zed"),
    AppEntry(
        "sublime-text",
        "Sublime Text",
        "editor",
        ["macos", "linux", "windows"],
        ["app", "cli"],
        "sublime-text",
    ),
    AppEntry("xcode", "Xcode", "editor", ["macos"], ["xcode"], "xcode"),
    AppEntry(
        "android-studio",
        "Android Studio",
        "editor",
        ["macos", "linux", "windows"],
        ["app"],
        "android-studio",
    ),
    AppEntry(
        "intellij",
        "IntelliJ IDEA",
        "editor",
        ["macos", "linux", "windows"],
        ["app"],
        "intellij-idea",
    ),
    AppEntry("pycharm", "PyCharm", "editor", ["macos", "linux", "windows"], ["app"], "pycharm"),
    AppEntry("webstorm", "WebStorm", "editor", ["macos", "linux", "windows"], ["app"], "webstorm"),
    AppEntry("phpstorm", "PhpStorm", "editor", ["macos", "linux", "windows"], ["app"], "phpstorm"),
    AppEntry("goland", "GoLand", "editor", ["macos", "linux", "windows"], ["app"], "goland"),
    AppEntry("rider", "Rider", "editor", ["macos", "linux", "windows"], ["app"], "rider"),
    AppEntry(
        "rustrover", "RustRover", "editor", ["macos", "linux", "windows"], ["app"], "rustrover"
    ),
    # Git GUIs
    AppEntry("fork", "Fork", "git-gui", ["macos", "windows"], ["app"], "fork"),
    AppEntry("sourcetree", "Sourcetree", "git-gui", ["macos", "windows"], ["app"], "sourcetree"),
    AppEntry(
        "github-desktop",
        "GitHub Desktop",
        "git-gui",
        ["macos", "windows"],
        ["github-desktop"],
        "github-desktop",
    ),
    AppEntry("tower", "Tower", "git-gui", ["macos", "windows"], ["app"], "tower"),
    AppEntry(
        "gitkraken", "GitKraken", "git-gui", ["macos", "linux", "windows"], ["app"], "gitkraken"
    ),
    AppEntry("smartgit", "SmartGit", "git-gui", ["macos", "linux", "windows"], ["app"], "smartgit"),
    AppEntry(
        "sublime-merge",
        "Sublime Merge",
        "git-gui",
        ["macos", "linux", "windows"],
        ["app"],
        "sublime-merge",
    ),
    # Terminals
    AppEntry("ghostty", "Ghostty", "terminal", ["macos", "linux"], ["cli"], "ghostty"),
    AppEntry("warp", "Warp", "terminal", ["macos", "linux", "windows"], ["cli"], "warp"),
    AppEntry("iterm2", "iTerm2", "terminal", ["macos"], ["app"], "iterm2"),
    AppEntry("kitty", "kitty", "terminal", ["macos", "linux"], ["cli"], "kitty"),
    AppEntry("terminal", "Terminal", "terminal", ["macos"], ["app"], "terminal"),
    AppEntry(
        "windows-terminal",
        "Windows Terminal",
        "terminal",
        ["windows"],
        ["cli", "app-paths"],
        "windows-terminal",
    ),
    AppEntry("git-bash", "Git Bash", "terminal", ["windows"], ["app-paths"], "git-bash"),
    AppEntry("gnome-terminal", "GNOME Terminal", "terminal", ["linux"], ["cli"], "gnome-terminal"),
    AppEntry("konsole", "Konsole", "terminal", ["linux"], ["cli"], "konsole"),
    # File Managers
    AppEntry("finder", "Finder", "file-manager", ["macos"], ["file"], "finder"),
    AppEntry(
        "file-explorer", "File Explorer", "file-manager", ["windows"], ["file"], "file-explorer"
    ),
    AppEntry("xdg-open", "xdg-open", "file-manager", ["linux"], ["file"], "xdg-open"),
]


class OpenInAppService:
    """Open In App service with catalog resolution and launch capabilities."""

    def __init__(self, config: OpenInAppConfig | None = None) -> None:
        self.config = config or OpenInAppConfig()
        self._resolved_launchers: dict[str, VerifiedLauncher] = {}
        self._resolved = False
        self._system = platform.system().lower()

    def get_system(self) -> str:
        return self._system

    async def resolve(self) -> dict[str, VerifiedLauncher]:
        """Resolve the catalog against this host."""
        if self._resolved:
            return self._resolved_launchers

        self._resolved_launchers = {}
        current_platform = self._system

        for entry in OPEN_IN_APP_CATALOG:
            if current_platform not in entry.platforms:
                continue

            launcher = await self._resolve_entry(entry)
            if launcher:
                self._resolved_launchers[entry.id] = launcher

        self._resolved = True
        return self._resolved_launchers

    async def _resolve_entry(self, entry: AppEntry) -> VerifiedLauncher | None:
        """Resolve a single catalog entry."""
        for source in entry.launcher_sources:
            launcher = await self._resolve_source(entry, source)
            if launcher:
                return launcher
        return None

    async def _resolve_source(self, entry: AppEntry, source: str) -> VerifiedLauncher | None:
        """Resolve a single launcher source."""
        if source == "app":
            return await self._resolve_app(entry)
        elif source == "xcode":
            return await self._resolve_xcode(entry)
        elif source == "cli":
            return await self._resolve_cli(entry)
        elif source == "file":
            return await self._resolve_file(entry)
        elif source == "app-paths":
            return await self._resolve_app_paths(entry)
        elif source == "install-record":
            return await self._resolve_install_record(entry)
        elif source == "github-desktop":
            return await self._resolve_github_desktop(entry)
        elif source == "desktop":
            return await self._resolve_desktop(entry)
        elif source == "fixed":
            return await self._resolve_fixed(entry)
        return None

    async def _resolve_app(self, entry: AppEntry) -> VerifiedLauncher | None:
        """Resolve macOS .app bundle or Windows app."""
        if self._system == "darwin":
            for base in [Path("/Applications"), Path.home() / "Applications"]:
                for name in [f"{entry.name}.app", f"{entry.id}.app"]:
                    path = base / name
                    if path.exists():
                        return VerifiedLauncher(
                            app_id=entry.id,
                            argv=["open", "-a", str(path)],
                            icon_path=(
                                str(path / "Contents/Resources/AppIcon.icns")
                                if (path / "Contents/Resources/AppIcon.icns").exists()
                                else None
                            ),
                        )
        elif self._system == "windows":
            # Check App Paths registry
            pass
        return None

    async def _resolve_xcode(self, entry: AppEntry) -> VerifiedLauncher | None:
        """Resolve Xcode via xcode-select."""
        try:
            result = await asyncio.create_subprocess_exec(
                "xcode-select",
                "-p",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(result.communicate(), timeout=5)
            if result.returncode == 0:
                path = stdout.decode().strip()
                if path:
                    return VerifiedLauncher(
                        app_id=entry.id,
                        argv=["open", "-a", "Xcode"],
                        icon_path=(
                            str(Path(path) / "Applications/Xcode.app/Contents/Resources/Xcode.icns")
                            if Path(path).exists()
                            else None
                        ),
                    )
        except (TimeoutError, subprocess.SubprocessError):
            pass
        return None

    async def _resolve_cli(self, entry: AppEntry) -> VerifiedLauncher | None:
        """Resolve CLI command in PATH."""
        for cmd in [entry.id, entry.name.lower().replace(" ", "-")]:
            path = shutil.which(cmd)
            if path:
                return VerifiedLauncher(
                    app_id=entry.id,
                    argv=[path, "."],
                    icon_path=None,
                )
        return None

    async def _resolve_file(self, entry: AppEntry) -> VerifiedLauncher | None:
        """Resolve well-known install paths."""
        # Platform-specific well-known paths
        return None

    async def _resolve_app_paths(self, entry: AppEntry) -> VerifiedLauncher | None:
        """Resolve via Windows App Paths registry."""
        if self._system != "windows":
            return None
        # Implementation would use winreg
        return None

    async def _resolve_install_record(self, entry: AppEntry) -> VerifiedLauncher | None:
        """Resolve via Windows Uninstall records."""
        if self._system != "windows":
            return None
        return None

    async def _resolve_github_desktop(self, entry: AppEntry) -> VerifiedLauncher | None:
        """Resolve GitHub Desktop with its cli.js."""
        if self._system == "windows":
            # Check App Paths for GitHub Desktop
            pass
        elif self._system == "darwin":
            # Check /Applications/GitHub Desktop.app
            pass
        return None

    async def _resolve_desktop(self, entry: AppEntry) -> VerifiedLauncher | None:
        """Resolve via XDG desktop entry (Linux)."""
        if self._system != "linux":
            return None

        desktop_id = f"{entry.id}.desktop"
        for base in [Path("/usr/share/applications"), Path.home() / ".local/share/applications"]:
            desktop_file = base / desktop_id
            if desktop_file.exists():
                try:
                    content = desktop_file.read_text()
                    exec_line = None
                    try_exec = None
                    for line in content.splitlines():
                        if line.startswith("Exec="):
                            exec_line = line[5:].strip()
                        elif line.startswith("TryExec="):
                            try_exec = line[8:].strip()
                    argv = shlex.split(exec_line) if exec_line else None
                    if try_exec and shutil.which(try_exec):
                        icon = None
                        # Find icon
                        for size in ["128", "64", "48", "32"]:
                            icon_path = Path(
                                f"/usr/share/icons/hicolor/{size}x{size}/apps/{entry.id}.png"
                            )
                            if icon_path.exists():
                                icon = str(icon_path)
                                break
                        return VerifiedLauncher(
                            app_id=entry.id,
                            argv=argv or [],
                            fallback_argv=[try_exec] if try_exec else None,
                            icon_path=icon,
                        )
                except Exception:
                    pass
        return None

    async def _resolve_fixed(self, entry: AppEntry) -> VerifiedLauncher | None:
        """Fixed paths for well-known locations."""
        return None

    async def get_icon(self, app_id: str) -> bytes | None:
        """Get application icon as bytes."""
        launcher = self._resolved_launchers.get(app_id)
        if not launcher or not launcher.icon_path:
            return None

        icon_path = Path(launcher.icon_path)
        if not icon_path.exists():
            return None

        return icon_path.read_bytes()

    async def launch(self, app_id: str, path: str) -> bool:
        """Launch an application with a workspace path."""
        launcher = self._resolved_launchers.get(app_id)
        if not launcher:
            return False

        # Validate path
        path_obj = Path(path)
        if not path_obj.is_dir():
            return False

        # Build argv
        argv = launcher.argv + [str(path_obj.resolve())]

        try:
            # Launch detached with scrubbed environment
            env = os.environ.copy()
            # Remove sensitive vars
            for key in list(env.keys()):
                if key.startswith(("AWS_", "GITHUB_", "DOCKER_", "KUBE_")):
                    del env[key]

            proc = await asyncio.create_subprocess_exec(
                *argv,
                env=env,
                start_new_session=True,
            )

            # Watch for early failure
            try:
                await asyncio.wait_for(proc.wait(), timeout=self.config.launch_watch_ms / 1000)
                return proc.returncode == 0
            except TimeoutError:
                # Still running after watch window - consider launched
                return True
        except Exception:
            return False

    def get_catalog(self) -> list[dict]:
        """Get the resolved catalog for the browser."""
        apps = []
        for entry in OPEN_IN_APP_CATALOG:
            if self._system in entry.platforms:
                apps.append(
                    {
                        "id": entry.id,
                        "name": entry.name,
                        "category": entry.category,
                        "icon": (
                            f"/open-in-app/icon/{entry.id}"
                            if entry.id in self._resolved_launchers
                            else None
                        ),
                    }
                )
        return apps


def create_open_in_app_service(config: OpenInAppConfig | None = None) -> OpenInAppService:
    """Create the Open In App service."""
    return OpenInAppService(config)


__all__ = [
    "OpenInAppService",
    "OpenInAppConfig",
    "AppEntry",
    "VerifiedLauncher",
    "create_open_in_app_service",
    "OPEN_IN_APP_CATALOG",
]
