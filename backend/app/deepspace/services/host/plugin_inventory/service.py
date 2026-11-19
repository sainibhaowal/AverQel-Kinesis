"""Plugin Inventory service for DeepSpace.

Provides a read-only projection of the current Cordis Loader plugin state
with each agent preset's composition.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class FiberPhase(str, Enum):
    """Fiber phase states."""

    PENDING = "pending"
    LOADING = "loading"
    ACTIVE = "active"
    FAILED = "failed"
    UNLOADING = "unloading"
    NULL = "null"


@dataclass(frozen=True, slots=True)
class PluginInventoryEntry:
    """A single plugin inventory entry."""

    entry_id: str
    module_specifier: str
    effective_enablement: bool
    phase: FiberPhase
    meta_title: str | None = None
    meta_description: str | None = None


@dataclass(frozen=True, slots=True)
class PresetPluginEntry:
    """A plugin entry within a preset composition."""

    entry_id: str | None
    module_specifier: str
    effective_enablement: bool
    disabled_expression: str | None = None
    phase: FiberPhase | None = None


@dataclass(frozen=True, slots=True)
class PresetComposition:
    """A preset's composition."""

    preset_id: str
    name: str
    description: str | None
    is_builtin: bool
    is_default: bool
    plugin_rows: list[PresetPluginEntry]


@dataclass(frozen=True, slots=True)
class PluginInventorySnapshot:
    """A complete plugin inventory snapshot."""

    entries: list[PluginInventoryEntry]
    agent_presets: list[Any]  # PresetComposition


class PluginInventoryService:
    """
    Read-only projection of the current Cordis Loader plugin state.
    """

    def __init__(self) -> None:
        self._loader = None  # Would be injected in real implementation

    async def list(self) -> PluginInventorySnapshot:
        """Get a point-in-time snapshot of the plugin inventory."""
        # In a real implementation, this would read from ctx.loader.entries()
        # and ctx.get('agentPresets') for preset compositions
        entries: list[PluginInventoryEntry] = []
        presets: list[Any] = []

        # This is a placeholder - real implementation would read from Loader
        return PluginInventorySnapshot(entries=entries, agent_presets=presets)

    def set_loader(self, loader: Any) -> None:
        """Set the loader reference."""
        self._loader = loader


def create_plugin_inventory_service() -> PluginInventoryService:
    """Create the plugin inventory service."""
    return PluginInventoryService()


__all__ = [
    "PluginInventoryService",
    "PluginInventoryEntry",
    "PresetPluginEntry",
    "PresetComposition",
    "PluginInventorySnapshot",
    "FiberPhase",
    "create_plugin_inventory_service",
]
