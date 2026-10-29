"""Tests for the DeepSpace plugin system.

The plugin system adds behaviour to the product without changing it, so
the tests cover the three things that could go wrong: a manifest that is
not usable, a plugin that offers a tool the product cannot dispatch, and a
plugin that is switched on but not permitted.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services.plugins import (
    PluginCommandBridge,
    PluginError,
    PluginManifest,
    PluginRegistry,
    PluginScope,
    PluginStateService,
    call_plugin_tool,
    discover_directory_plugins,
    parse_manifest,
    plugin_allowed_tools,
    plugin_denied_tools,
    plugin_guidance,
    plugin_registry,
    plugin_runtime_summary,
    plugin_tool_specs,
)

pytestmark = pytest.mark.unit_no_db


def _manifest(**overrides: Any) -> PluginManifest:
    """Build one plugin manifest for a test.

    Args:
        overrides: The fields to override.

    Returns:
        The manifest.
    """
    values: dict[str, Any] = {
        "id": "sample",
        "name": "Sample",
        "version": "1.0.0",
        "description": "A sample plugin.",
        "source": "deployment",
        "tools": (
            {
                "name": "sample_tool",
                "description": "Does a sample thing.",
                "mode": "read",
                "parameters": {"type": "object", "properties": {}},
            },
        ),
        "enabled_by_default": True,
    }
    values.update(overrides)
    return PluginManifest(**values)  # type: ignore[arg-type]


def test_a_manifest_without_an_id_is_refused() -> None:
    with pytest.raises(PluginError):
        parse_manifest({"name": "No id"}, source="deployment")


def test_a_manifest_with_an_unsafe_id_is_refused() -> None:
    with pytest.raises(PluginError):
        parse_manifest({"id": "bad id/../etc"}, source="deployment")


def test_a_manifest_with_an_unknown_tool_mode_is_refused() -> None:
    with pytest.raises(PluginError):
        parse_manifest(
            {
                "id": "sample",
                "tools": [{"name": "t", "mode": "destroy"}],
            },
            source="deployment",
        )


def test_a_manifest_is_parsed_with_its_tools_and_policy() -> None:
    manifest = parse_manifest(
        {
            "id": "sample",
            "name": "Sample",
            "version": "2.1.0",
            "description": "A sample plugin.",
            "guidance": "Do the sample thing.",
            "allow_tools": ["bash"],
            "deny_tools": ["fs_delete"],
            "tools": [
                {
                    "name": "sample_tool",
                    "description": "Does a sample thing.",
                    "mode": "write",
                    "parameters": {"type": "object", "properties": {"a": {"type": "string"}}},
                }
            ],
        },
        source="deployment",
    )

    assert manifest.id == "sample"
    assert manifest.name == "Sample"
    assert manifest.version == "2.1.0"
    assert manifest.guidance == "Do the sample thing."
    assert manifest.allow_tools == ("bash",)
    assert manifest.deny_tools == ("fs_delete",)
    assert [tool.name for tool in manifest.tools] == ["sample_tool"]
    assert manifest.tools[0].mode == "write"
    assert manifest.tools[0].parameters["properties"]["a"]["type"] == "string"


def test_a_directory_of_plugins_is_discovered(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "sample"
    plugin_dir.mkdir()
    (plugin_dir / "deepspace-plugin.json").write_text(
        json.dumps({"id": "sample", "name": "Sample"}), encoding="utf-8"
    )
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "deepspace-plugin.json").write_text("{not json", encoding="utf-8")

    manifests = discover_directory_plugins(tmp_path)

    # The broken plugin is skipped, and the good one is read.
    assert [manifest.id for manifest in manifests] == ["sample"]


def test_the_registry_keeps_the_first_of_two_plugins_with_one_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.deepspace.services import plugins as plugins_module

    first = _manifest(id="same", name="First")
    second = _manifest(id="same", name="Second")
    monkeypatch.setattr(plugins_module, "discover_directory_plugins", lambda *a, **k: [first])
    monkeypatch.setattr(plugins_module, "discover_package_plugins", lambda: [second])

    registry = PluginRegistry()
    manifests = registry.refresh()

    # The first declaration wins, and the clash is reported.
    assert [manifest.name for manifest in manifests if manifest.native_tool is None] == ["First"]
    assert registry.errors()


def test_a_plugin_tool_joins_the_native_registry() -> None:
    from app.deepspace.services.tools import NATIVE_TOOL_MODES, NATIVE_TOOL_SPECS

    registry = plugin_registry()
    registry.refresh()
    if registry.get("code-metrics") is None:
        pytest.skip("The built-in plugins are not readable in this checkout.")

    names = [spec.name for spec in NATIVE_TOOL_SPECS]
    assert "code_metrics" in names
    assert NATIVE_TOOL_MODES["code_metrics"] == "read"


def test_a_plugin_tool_is_dispatched_like_a_native_one() -> None:
    import asyncio

    from app.deepspace.services.tools import dispatch_native_tool

    registry = plugin_registry()
    registry.refresh()
    if registry.get("code-metrics") is None:
        pytest.skip("The built-in plugins are not readable in this checkout.")

    result = asyncio.run(dispatch_native_tool("code_metrics", {}, None))  # type: ignore[arg-type]

    assert result["status"] == "ok"
    assert "files" in result


def test_a_plugin_tool_becomes_a_native_tool_spec() -> None:
    registry = plugin_registry()
    registry.refresh()
    if registry.get("code-metrics") is None:
        pytest.skip("The built-in plugins are not readable in this checkout.")

    specs = plugin_tool_specs(("code-metrics",))

    assert [spec.name for spec in specs] == ["code_metrics"]
    assert specs[0].mode == "read"
    assert specs[0].definition["function"]["name"] == "code_metrics"


def test_every_native_registry_tool_has_one_native_plugin_adapter() -> None:
    from app.deepspace.services.chat_service import NATIVE_TOOL_REGISTRY

    registry = plugin_registry()
    registry.refresh()
    manifests = registry.known()
    native_manifests = [manifest for manifest in manifests if manifest.native_tool is not None]
    plugin_tool_names = {tool.name for manifest in manifests for tool in manifest.tools}

    assert plugin_tool_names == set(NATIVE_TOOL_REGISTRY.names())
    assert len(plugin_tool_names) == len(NATIVE_TOOL_REGISTRY.names())
    assert all(manifest.handler_kind == "native" for manifest in native_manifests)
    assert all(manifest.handler is None and not manifest.command for manifest in native_manifests)


def test_a_native_adapter_reuses_the_native_dispatch_path() -> None:
    registry = plugin_registry()
    registry.refresh()
    manifest = registry.get("native.fs_read")

    assert manifest is not None
    assert manifest.native_tool == "fs_read"
    assert plugin_tool_specs((manifest.id,)) == []
    with pytest.raises(PluginError, match="DeepSpace runtime"):
        call_plugin_tool(manifest, tool_name="fs_read", arguments={})


def test_native_adapters_follow_each_preset_allowlist() -> None:
    settings = SimpleNamespace(
        deepspace_default_preset="standard",
        deepspace_plugins_enabled=True,
    )
    summary = plugin_runtime_summary(settings)
    presets = {
        row["id"]: {plugin["id"] for plugin in row["plugins"]} for row in summary["session_presets"]
    }

    assert "native.fs_read" in presets["standard"]
    assert "native.fs_read" in presets["ptc"]
    assert "native.fs_write" not in presets["minimal"]
    assert "native.fs_read" in presets["minimal"]
    assert "native.fs_read" not in presets["creator"]
    assert "native.run_code" in presets["ptc"]


def test_a_plugin_tool_call_runs_the_plugin_handler() -> None:
    manifest = parse_manifest(
        {
            "id": "sample",
            "handler": "app.deepspace.plugins.builtin.code_metrics.handler:code_metrics",
            "tools": [{"name": "code_metrics", "description": "Counts code.", "mode": "read"}],
        },
        source="builtin",
    )

    result = call_plugin_tool(manifest, tool_name="code_metrics", arguments={})

    assert result["status"] == "ok"
    assert "files" in result
    assert "lines" in result


def test_a_plugin_command_answers_a_call_over_json_rpc(tmp_path: Path) -> None:
    script = tmp_path / "plugin.py"
    script.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': request['id'],"
        " 'result': {'status': 'ok', 'echo': request['params']['arguments']}}) + '\\n')\n"
        "    sys.stdout.flush()\n",
        encoding="utf-8",
    )
    manifest = parse_manifest(
        {
            "id": "command",
            "command": ["python3", str(script)],
            "tools": [{"name": "echo", "description": "Echoes.", "mode": "read"}],
        },
        source="deployment",
    )

    with PluginCommandBridge(manifest) as bridge:
        result = bridge.call("echo", {"value": 7})

    assert result == {"status": "ok", "echo": {"value": 7}}


def test_a_plugin_command_that_cannot_start_fails_the_call(tmp_path: Path) -> None:
    manifest = parse_manifest(
        {
            "id": "broken",
            "command": ["definitely-not-a-real-command-4f2a"],
            "tools": [{"name": "echo", "description": "Echoes.", "mode": "read"}],
        },
        source="deployment",
    )

    with pytest.raises(PluginError):
        call_plugin_tool(manifest, tool_name="echo", arguments={})


def test_a_plugin_deny_list_wins_over_its_allow_list() -> None:
    registry = plugin_registry()
    registry.refresh()

    denied = plugin_denied_tools(("code-metrics",))
    allowed = plugin_allowed_tools(("code-metrics",))

    assert isinstance(denied, frozenset)
    assert isinstance(allowed, frozenset)


def test_plugin_guidance_names_the_plugin_that_adds_it() -> None:
    registry = plugin_registry()
    registry.refresh()
    if registry.get("code-metrics") is None:
        pytest.skip("The built-in plugins are not readable in this checkout.")

    guidance = plugin_guidance(("code-metrics",))

    assert "Code metrics" in guidance


def test_plugin_guidance_is_empty_without_an_enabled_plugin() -> None:
    assert plugin_guidance(()) == ""


def test_plugin_runtime_summary_publishes_global_and_preset_planes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.deepspace.services import plugins as plugins_module

    manifest = parse_manifest(
        {
            "id": "summary-plugin",
            "name": "Summary plugin",
            "guidance": "Use the summary tool carefully.",
            "tools": [
                {
                    "name": "summary_tool",
                    "description": "Reads a summary.",
                    "mode": "read",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        },
        source="builtin",
    )

    class _Registry:
        def known(self) -> list[PluginManifest]:
            return [manifest]

        def enabled(self, plugin_id: str) -> bool:
            return plugin_id == manifest.id

        def errors(self) -> list[str]:
            return []

    monkeypatch.setattr(plugins_module, "plugin_registry", lambda: _Registry())
    summary = plugin_runtime_summary(
        SimpleNamespace(deepspace_default_preset="standard", deepspace_plugins_enabled=True)
    )

    assert summary["plugins"] == summary["global_plugins"]
    assert summary["core_runtime"]
    assert summary["counts"] == {
        "extensions": 1,
        "enabled_extensions": 1,
        "tools": 1,
        "native_tools": 0,
        "compatibility_tools": 0,
        "extension_tools": 1,
    }
    assert summary["global_plugins"][0]["deployment_enabled"] is True
    assert summary["global_plugins"][0]["has_guidance"] is True
    presets = {row["id"]: row for row in summary["session_presets"]}
    assert presets["standard"]["is_default"] is True
    assert [row["id"] for row in presets["standard"]["plugins"]] == [manifest.id]
    assert presets["standard"]["plugin_count"] == 1
    assert presets["standard"]["enabled_plugin_count"] == 1
    assert presets["standard"]["tools"] == ["summary_tool"]
    assert presets["standard"]["tool_count"] == 1
    assert presets["standard"]["ptc_enabled"] is False
    assert presets["standard"]["sandbox_mode"] == "workspace-write"
    assert presets["standard"]["subagent_max_depth"] == 3
    assert [row["id"] for row in presets["creator"]["plugins"]] == []
    assert summary["global_plugins"][0]["available_in_presets"] == [
        "standard",
        "custom",
    ]


def test_plugin_runtime_summary_applies_the_user_custom_mode_and_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.deepspace.services import plugins as plugins_module

    manifest = parse_manifest(
        {
            "id": "custom-summary-plugin",
            "name": "Custom summary plugin",
            "tools": [
                {
                    "name": "custom_summary_tool",
                    "description": "Reads a custom summary.",
                    "mode": "read",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        },
        source="builtin",
    )

    class _Registry:
        def known(self) -> list[PluginManifest]:
            return [manifest]

        def enabled(self, plugin_id: str) -> bool:
            return plugin_id == manifest.id

        def errors(self) -> list[str]:
            return []

    monkeypatch.setattr(plugins_module, "plugin_registry", lambda: _Registry())
    summary = plugin_runtime_summary(
        SimpleNamespace(deepspace_default_preset="standard", deepspace_plugins_enabled=True),
        custom_settings={"tools": ["custom_summary_tool"], "ptc_enabled": True},
        default_preset="custom",
    )

    custom = next(row for row in summary["session_presets"] if row["id"] == "custom")
    assert summary["default_preset"] == "custom"
    assert custom["is_default"] is True
    assert custom["ptc_enabled"] is True
    assert custom["tools"] == ["custom_summary_tool"]
    assert [row["id"] for row in custom["plugins"]] == [manifest.id]


def test_plugin_guidance_reads_the_conversation_scope() -> None:
    manifest = _manifest(guidance="Workspace-specific instructions.")
    scope = PluginScope(registry=PluginRegistry(), workspace=(manifest,))

    assert "Workspace-specific instructions." in plugin_guidance((manifest.id,), scope=scope)


def test_the_state_service_records_the_conversation_plugins() -> None:
    class _Events:
        def __init__(self) -> None:
            self.appended: list[dict[str, Any]] = []

        def append(self, **kwargs: Any) -> None:
            self.appended.append(kwargs)

    class _Session:
        def __init__(self) -> None:
            self.committed = False

        def commit(self) -> None:
            self.committed = True

        def execute(self, *args: Any, **kwargs: Any) -> Any:
            class _Result:
                def scalars(self) -> Any:
                    return self

                def all(self) -> list[Any]:
                    return []

            return _Result()

    service = PluginStateService(_Session())  # type: ignore[arg-type]
    service.events = _Events()  # type: ignore[assignment]

    recorded = service.set_plugins(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        enabled=("code-metrics", "not-a-plugin"),
    )

    assert recorded["enabled"] == ["code-metrics"]
    assert service.events.appended[0]["event_type"] == "plugins/enabled"  # type: ignore[attr-defined]
