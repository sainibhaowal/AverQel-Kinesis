"""Plugin Hot Module Reloading (HMR) and Configuration Editor for DeepSpace.

Allows plugins to be updated without restart, and provides a configuration editor UI.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.deepspace.services.plugins import (
    PluginManifest,
    PluginRegistry,
    deployment_plugin_directory,
    discover_package_plugins,
    discover_workspace_plugins,
)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PluginHMRConfig:
    """Plugin HMR configuration."""

    enabled: bool = False
    watch_interval_seconds: int = 5
    debounce_ms: int = 500
    reload_on_change: bool = True
    validate_on_load: bool = True
    allow_workspace_plugins: bool = True
    allow_deployment_plugins: bool = True
    config_editor_enabled: bool = True


# ──────────────────────────────────────────────────────────────────────
# Plugin Watcher
# ──────────────────────────────────────────────────────────────────────


def _hash_file(path: Path) -> str:
    """Return SHA256 hash of a file."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


@dataclass(frozen=True, slots=True)
class PluginFileState:
    """Tracked state of a plugin file."""

    path: Path
    file_hash: str
    last_modified: float
    last_checked: float

    @staticmethod
    def from_path(path: Path) -> PluginFileState:
        """Create state from a file path."""
        stat = path.stat()
        return PluginFileState(
            path=path,
            file_hash=_hash_file(path),
            last_modified=stat.st_mtime,
            last_checked=time.time(),
        )

    def has_changed(self) -> bool:
        """Check if the file has changed since last check."""
        try:
            stat = self.path.stat()
            if stat.st_mtime != self.last_modified:
                return True
            return _hash_file(self.path) != self.file_hash
        except OSError:
            return True  # File deleted or inaccessible


class PluginWatcher:
    """Watches plugin directories for changes and triggers reloads."""

    def __init__(self, config: PluginHMRConfig, settings: Settings, registry: PluginRegistry):
        self.config = config
        self.settings = settings
        self.registry = registry
        self._file_states: dict[Path, PluginFileState] = {}
        self._callbacks: list[Callable[[list[PluginManifest]], Any]] = []
        self._running = False
        self._task: asyncio.Task | None = None
        self._plugin_paths: dict[str, list[Path]] = {}  # plugin_id -> list of source files

    def add_callback(self, callback: Callable[[list[PluginManifest]], Any]) -> None:
        """Add a callback to be called when plugins change."""
        self._callbacks.append(callback)

    def remove_callback(self, callback: Callable[[list[PluginManifest]], Any]) -> None:
        """Remove a callback."""
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def _collect_plugin_files(self, manifest: PluginManifest) -> list[Path]:
        """Collect all source files for a plugin."""
        files = []
        manifest_path = Path(manifest.path) if manifest.path else None
        plugin_dir = manifest_path.parent if manifest_path else Path()

        # Add manifest file
        if manifest_path and manifest_path.exists():
            files.append(manifest_path)

        # Add Python source files
        for ext in (".py", ".json", ".yaml", ".yml", ".toml"):
            for src in plugin_dir.rglob(f"*{ext}"):
                # Skip __pycache__ and hidden dirs
                if "__pycache__" in src.parts or any(p.startswith(".") for p in src.parts):
                    continue
                if src.is_file():
                    files.append(src)

        return files

    async def start(self) -> None:
        """Start watching for plugin changes."""
        if self._running:
            return
        self._running = True
        await self._scan_all()
        self._task = asyncio.create_task(self._watch_loop())

    async def stop(self) -> None:
        """Stop watching."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _scan_all(self) -> list[PluginManifest]:
        """Scan all plugin directories and return discovered manifests."""
        manifests: list[PluginManifest] = []

        if self.config.allow_workspace_plugins:
            # Workspace plugins are per-conversation, skip global scan
            pass

        if self.config.allow_deployment_plugins:
            manifests.extend(discover_package_plugins())
            dep_dir = deployment_plugin_directory(self.settings)
            if dep_dir.exists():
                try:
                    manifests.extend(discover_workspace_plugins(dep_dir))
                except Exception:
                    pass

        # Track all plugin source files
        self._plugin_paths.clear()
        for manifest in manifests:
            self._plugin_paths[manifest.id] = self._collect_plugin_files(manifest)
            for src in self._plugin_paths[manifest.id]:
                if src not in self._file_states:
                    self._file_states[src] = PluginFileState.from_path(src)

        return manifests

    async def _watch_loop(self) -> None:
        """Main watch loop."""
        while self._running:
            try:
                await asyncio.sleep(self.config.watch_interval_seconds)
                if not self._running:
                    break
                await self._check_changes()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.error("Plugin watcher error", exc_info=True)

    async def _check_changes(self) -> None:
        """Check for plugin file changes and reload affected plugins."""
        if not self.config.reload_on_change:
            return

        changed_plugins: set[str] = set()
        current_files: set[Path] = set()

        # Check existing tracked files
        for path, state in list(self._file_states.items()):
            if not path.exists():
                # File deleted
                plugin_id = self._find_plugin_for_file(path)
                if plugin_id:
                    changed_plugins.add(plugin_id)
                del self._file_states[path]
            elif state.has_changed():
                # File modified
                plugin_id = self._find_plugin_for_file(path)
                if plugin_id:
                    changed_plugins.add(plugin_id)
                # Update state
                self._file_states[path] = PluginFileState.from_path(path)
            current_files.add(path)

        # Check for new files in tracked plugin directories
        for plugin_id, paths in self._plugin_paths.items():
            for path in paths:
                if path not in current_files and path.exists():
                    # New file added
                    changed_plugins.add(plugin_id)
                    self._file_states[path] = PluginFileState.from_path(path)
                    current_files.add(path)

        if changed_plugins:
            logger.info("Plugin changes detected, reloading: %s", ", ".join(changed_plugins))
            await self._reload_plugins(changed_plugins)

    def _find_plugin_for_file(self, path: Path) -> str | None:
        """Find which plugin a file belongs to."""
        for plugin_id, paths in self._plugin_paths.items():
            if path in paths:
                return plugin_id
            # Check if path is under any tracked plugin directory
            for tracked in paths:
                try:
                    path.relative_to(tracked.parent)
                    return plugin_id
                except ValueError:
                    pass
        return None

    async def _reload_plugins(self, plugin_ids: set[str]) -> None:
        """Reload specific plugins by re-importing their modules."""
        import importlib
        import sys

        # Reload the plugin modules
        for plugin_id in plugin_ids:
            plugin = self.registry.get(plugin_id)
            if not plugin:
                continue

            # Package handlers are cached by Python's import system. Evicting
            # their modules lets the next call observe changed source. A
            # workspace handler is loaded from its file for each call, so it
            # only needs import caches invalidated.
            module_name = (
                plugin.handler.partition(":")[0].strip()
                if plugin.handler and not plugin.handler_module
                else ""
            )
            importlib.invalidate_caches()

            # Reload the module and any submodules
            reloaded = set()
            for name, module in list(sys.modules.items()) if module_name else []:
                if name == module_name or name.startswith(module_name + "."):
                    try:
                        importlib.reload(module)
                        reloaded.add(name)
                    except Exception as e:
                        logger.warning("Failed to reload module %s: %s", name, e)

            if reloaded:
                logger.info("Reloaded modules for plugin %s: %s", plugin_id, ", ".join(reloaded))
        # Refresh the registry to pick up any new plugins
        self.registry.refresh(settings=self.settings)

        # Notify callbacks
        manifests = [
            manifest for pid in plugin_ids if (manifest := self.registry.get(pid)) is not None
        ]
        for callback in self._callbacks:
            try:
                await callback(manifests)
            except Exception:
                logger.error("Plugin change callback failed", exc_info=True)


# ──────────────────────────────────────────────────────────────────────
# Configuration Editor
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PluginConfigSchema:
    """Schema for a plugin's configuration."""

    plugin_id: str
    schema: dict[str, Any]  # JSON Schema
    defaults: dict[str, Any]
    ui_hints: dict[str, Any] = field(default_factory=dict)


class PluginConfigEditor:
    """Manages plugin configuration editing and validation."""

    def __init__(self, config: PluginHMRConfig, settings: Settings):
        self.config = config
        self.settings = settings
        self._schemas: dict[str, PluginConfigSchema] = {}

    def register_schema(self, schema: PluginConfigSchema) -> None:
        """Register a plugin's configuration schema."""
        self._schemas[schema.plugin_id] = schema

    def get_schema(self, plugin_id: str) -> PluginConfigSchema | None:
        """Get a plugin's configuration schema."""
        return self._schemas.get(plugin_id)

    def validate_config(self, plugin_id: str, config: dict[str, Any]) -> tuple[bool, list[str]]:
        """Validate a plugin configuration against its schema."""
        schema = self._schemas.get(plugin_id)
        if not schema:
            return True, []  # No schema = accept anything

        try:
            import jsonschema

            jsonschema.validate(config, schema.schema)
            return True, []
        except Exception as e:
            return False, [str(e)]

    def get_defaults(self, plugin_id: str) -> dict[str, Any]:
        """Get default configuration for a plugin."""
        schema = self._schemas.get(plugin_id)
        return schema.defaults if schema else {}

    def get_ui_hints(self, plugin_id: str) -> dict[str, Any]:
        """Get UI hints for a plugin's configuration."""
        schema = self._schemas.get(plugin_id)
        return schema.ui_hints if schema else {}

    def list_schemas(self) -> list[PluginConfigSchema]:
        """List all registered schemas."""
        return list(self._schemas.values())


# ──────────────────────────────────────────────────────────────────────
# Plugin HMR Service
# ──────────────────────────────────────────────────────────────────────


class PluginHMRService:
    """Main plugin HMR service combining watcher and editor."""

    def __init__(self, config: PluginHMRConfig, settings: Settings, registry: PluginRegistry):
        self.config = config
        self.settings = settings
        self.registry = registry
        self.watcher = PluginWatcher(config, settings, registry)
        self.editor = PluginConfigEditor(config, settings)
        self._initialized = False

    async def initialize(self) -> bool:
        """Initialize the HMR service."""
        if self._initialized:
            return True
        if not self.config.enabled:
            return False

        await self.watcher.start()
        self._initialized = True
        logger.info("Plugin HMR service initialized")
        return True

    async def shutdown(self) -> None:
        """Shutdown the HMR service."""
        await self.watcher.stop()
        self._initialized = False

    def register_config_schema(self, schema: PluginConfigSchema) -> None:
        """Register a plugin's configuration schema."""
        self.editor.register_schema(schema)

    async def reload_plugins(self) -> list[PluginManifest]:
        """Force reload all plugins."""
        self.registry.refresh(self.settings)
        return self.registry.all()

    def get_plugin_config_schema(self, plugin_id: str) -> PluginConfigSchema | None:
        """Get a plugin's configuration schema."""
        return self.editor.get_schema(plugin_id)

    def validate_plugin_config(
        self, plugin_id: str, config: dict[str, Any]
    ) -> tuple[bool, list[str]]:
        """Validate a plugin's configuration."""
        return self.editor.validate_config(plugin_id, config)

    def get_plugin_defaults(self, plugin_id: str) -> dict[str, Any]:
        """Get default configuration for a plugin."""
        return self.editor.get_defaults(plugin_id)

    def get_plugin_ui_hints(self, plugin_id: str) -> dict[str, Any]:
        """Get UI hints for a plugin's configuration."""
        return self.editor.get_ui_hints(plugin_id)

    def list_config_schemas(self) -> list[PluginConfigSchema]:
        """List all registered configuration schemas."""
        return self.editor.list_schemas()


# Global service
_HMR_SERVICE: PluginHMRService | None = None


async def get_plugin_hmr_service(settings: Settings | None = None) -> PluginHMRService:
    """Get or create the global plugin HMR service."""
    global _HMR_SERVICE
    from app.deepspace.services.plugins import plugin_registry

    if _HMR_SERVICE is None:
        cfg = PluginHMRConfig(
            enabled=getattr(settings or get_settings(), "deepspace_plugin_hmr_enabled", False),
            watch_interval_seconds=getattr(
                settings or get_settings(), "deepspace_plugin_hmr_interval", 5
            ),
            debounce_ms=getattr(
                settings or get_settings(), "deepspace_plugin_hmr_debounce_ms", 500
            ),
            reload_on_change=getattr(
                settings or get_settings(), "deepspace_plugin_hmr_reload", True
            ),
            validate_on_load=getattr(
                settings or get_settings(), "deepspace_plugin_hmr_validate", True
            ),
            allow_workspace_plugins=getattr(
                settings or get_settings(), "deepspace_plugin_hmr_workspace", True
            ),
            allow_deployment_plugins=getattr(
                settings or get_settings(), "deepspace_plugin_hmr_deployment", True
            ),
            config_editor_enabled=getattr(
                settings or get_settings(), "deepspace_plugin_hmr_editor", True
            ),
        )
        _HMR_SERVICE = PluginHMRService(cfg, settings or get_settings(), plugin_registry())
        await _HMR_SERVICE.initialize()
    return _HMR_SERVICE


async def init_plugin_hmr_service(settings: Settings) -> PluginHMRService:
    """Initialize the plugin HMR service."""
    global _HMR_SERVICE
    from app.deepspace.services.plugins import plugin_registry

    cfg = PluginHMRConfig(
        enabled=getattr(settings, "deepspace_plugin_hmr_enabled", False),
        watch_interval_seconds=getattr(settings, "deepspace_plugin_hmr_interval", 5),
        debounce_ms=getattr(settings, "deepspace_plugin_hmr_debounce_ms", 500),
        reload_on_change=getattr(settings, "deepspace_plugin_hmr_reload", True),
        validate_on_load=getattr(settings, "deepspace_plugin_hmr_validate", True),
        allow_workspace_plugins=getattr(settings, "deepspace_plugin_hmr_workspace", True),
        allow_deployment_plugins=getattr(settings, "deepspace_plugin_hmr_deployment", True),
        config_editor_enabled=getattr(settings, "deepspace_plugin_hmr_editor", True),
    )
    _HMR_SERVICE = PluginHMRService(cfg, settings, plugin_registry())
    await _HMR_SERVICE.initialize()
    return _HMR_SERVICE


__all__ = [
    "PluginHMRConfig",
    "PluginFileState",
    "PluginWatcher",
    "PluginConfigSchema",
    "PluginConfigEditor",
    "PluginHMRService",
    "get_plugin_hmr_service",
    "init_plugin_hmr_service",
]
