"""Focused tests for DeepSpace plugin hot reload wiring."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services.plugin_hmr import PluginHMRConfig, PluginWatcher
from app.deepspace.services.plugins import PluginManifest

pytestmark = pytest.mark.unit_no_db


def _manifest(path: Path | None = None) -> PluginManifest:
    """Build a minimal deployment plugin manifest."""
    return PluginManifest(
        id="sample",
        name="Sample",
        version="1.0.0",
        description="A sample plugin.",
        source="deployment",
        path=str(path) if path else None,
    )


def test_plugin_file_collection_accepts_manifest_string_paths(tmp_path: Path) -> None:
    """A discovered manifest path is a string and must still be watchable."""
    manifest_path = tmp_path / "sample" / "deepspace-plugin.json"
    manifest_path.parent.mkdir()
    manifest_path.write_text("{}", encoding="utf-8")
    (manifest_path.parent / "handler.py").write_text("value = 1\n", encoding="utf-8")

    watcher = PluginWatcher(PluginHMRConfig(enabled=True), SimpleNamespace(), SimpleNamespace())

    assert set(watcher._collect_plugin_files(_manifest(manifest_path))) == {
        manifest_path,
        manifest_path.parent / "handler.py",
    }


def test_reload_notifies_callbacks_for_the_changed_plugin() -> None:
    """Reload refreshes the registry and reports exactly changed plugins."""
    manifest = _manifest()
    refreshed: list[Any] = []

    class Registry:
        def get(self, plugin_id: str) -> PluginManifest | None:
            return manifest if plugin_id == manifest.id else None

        def refresh(self, *, settings: Any = None) -> list[PluginManifest]:
            refreshed.append(settings)
            return [manifest]

    watcher = PluginWatcher(PluginHMRConfig(enabled=True), SimpleNamespace(), Registry())
    callbacks: list[list[PluginManifest]] = []

    async def callback(items: list[PluginManifest]) -> None:
        callbacks.append(items)

    watcher.add_callback(callback)
    asyncio.run(watcher._reload_plugins({manifest.id}))

    assert refreshed == [watcher.settings]
    assert callbacks == [[manifest]]
