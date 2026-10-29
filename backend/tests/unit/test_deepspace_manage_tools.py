"""Tests for the DeepSpace management tools.

The checks cover what each tool answers: the plugin manager lists the
plugins and switches one on or off, the inspection tools name the
capabilities of the runtime and read one of them, the dependency report
names only paths the host really has, and the subagent model list names the
routes a child may run under.  The database is a double, so no database is
touched.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services.plugins import (
    PluginError,
    PluginManifest,
    PluginRegistry,
    PluginStateService,
    PluginTool,
)
from app.deepspace.services.tools import dependencies as dependency_tools
from app.deepspace.services.tools import inspect as inspect_tools
from app.deepspace.services.tools import manage as manage_tools
from app.deepspace.services.tools._base import NativeToolSpec, ToolContext

pytestmark = pytest.mark.unit_no_db

TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
CONVERSATION_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
PROVIDER_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")


class _FakeResult:
    """The result of a query against the fake session."""

    def scalar(self) -> Any:
        return None

    def scalars(self) -> Any:
        return self

    def all(self) -> list[Any]:
        return []


class _FakeSession:
    """A database session that keeps the events it was given."""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.commits = 0

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        del args
        del kwargs
        return _FakeResult()

    def add(self, item: Any) -> None:
        self.added.append(item)

    def commit(self) -> None:
        self.commits += 1


class _FakeModelCache:
    """The model cache the fake selection service answers with."""

    def __init__(self, rows: list[Any]) -> None:
        self.rows = list(rows)

    def list_models(
        self,
        *,
        tenant_id: uuid.UUID,
        provider_config_id: uuid.UUID,
        model_kind: str | None = None,
    ) -> list[Any]:
        del tenant_id
        return [
            row
            for row in self.rows
            if row.provider_config_id == provider_config_id
            and (model_kind is None or row.model_kind == model_kind)
        ]


class _FakeSelection:
    """A provider selection service that resolves one fixed route."""

    def __init__(self, candidates: list[Any], rows: list[Any] | None = None) -> None:
        self.candidates = list(candidates)
        self.model_cache = _FakeModelCache(rows or [])

    def resolve_chat(self, **kwargs: Any) -> Any:
        del kwargs
        return SimpleNamespace(candidates=self.candidates)


def _settings(**overrides: Any) -> SimpleNamespace:
    """Build the settings the tools read."""
    base = {
        "deepspace_plugins_enabled": True,
        "deepspace_plugin_directory": "",
        "deepspace_permission_mode": "workspace-write",
        "deepspace_approval_mode": "never",
        "deepspace_max_concurrent_tools": 8,
        "deepspace_sandbox_backend": "auto",
        "deepspace_sandbox_network": True,
        "deepspace_sandbox_image": "",
        "deepspace_sandbox_required": False,
        "deepspace_terminal_enabled": True,
        "deepspace_terminal_max_sessions": 32,
        "deepspace_terminal_max_sessions_per_conversation": 4,
        "deepspace_terminal_idle_timeout_seconds": 900.0,
        "llm_provider": "openai",
        "llm_model": "gpt-4o",
        "llm_api_base_url": "https://api.openai.com/v1",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _context(
    *,
    db: Any = None,
    provider_selection: Any = None,
    settings: SimpleNamespace | None = None,
) -> ToolContext:
    """Build a tool context for the management tools."""
    return ToolContext(
        auth=SimpleNamespace(tenant_id=TENANT_ID, user_id=USER_ID),
        conversation_id=CONVERSATION_ID,
        settings=settings or _settings(),
        db=db,
        task_store=SimpleNamespace(),
        tool_result_store=None,
        provider_registry=SimpleNamespace(),
        provider_selection=provider_selection,
    )


def _spec(specs: list[NativeToolSpec], name: str) -> NativeToolSpec:
    """Return one tool spec by name."""
    for spec in specs:
        if spec.name == name:
            return spec
    raise AssertionError(f"The tool {name!r} is not declared.")


def _run(spec: NativeToolSpec, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Run one tool and return its result."""
    return asyncio.run(spec.handler(arguments, context))


def _manifest(**overrides: Any) -> PluginManifest:
    """Build one plugin manifest for a test."""
    values: dict[str, Any] = {
        "id": "sample",
        "name": "Sample",
        "version": "1.0.0",
        "description": "A sample plugin.",
        "source": "builtin",
        "tools": (
            PluginTool(
                name="sample_tool",
                description="Does a sample thing.",
                parameters={},
                mode="read",
            ),
        ),
        "enabled_by_default": True,
    }
    values.update(overrides)
    return PluginManifest(**values)  # type: ignore[arg-type]


def _registry(monkeypatch: pytest.MonkeyPatch, manifest: PluginManifest) -> PluginRegistry:
    """Install a registry that offers one plugin, in place of the real one."""
    from app.deepspace.services import plugins as plugins_module

    def _discover(directory: Any, *, source: str = "deployment") -> list[PluginManifest]:
        del directory
        return [manifest] if source == "builtin" else []

    registry = PluginRegistry()
    monkeypatch.setattr(plugins_module, "discover_directory_plugins", _discover)
    monkeypatch.setattr(plugins_module, "discover_package_plugins", list)
    monkeypatch.setattr(plugins_module, "plugin_registry", lambda: registry)
    monkeypatch.setattr(manage_tools, "plugin_registry", lambda: registry)
    monkeypatch.setattr(inspect_tools, "plugin_registry", lambda: registry)
    registry.refresh()
    return registry


def test_the_plugin_manager_lists_the_plugins_the_deployment_ships(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registry(monkeypatch, _manifest())
    context = _context(db=_FakeSession())

    result = _run(_spec(manage_tools.MANAGE_TOOLS, "plugin_manager"), {"action": "list"}, context)

    assert result["status"] == "ok"
    assert len(result["plugins"]) == 1
    entry = result["plugins"][0]
    assert entry["id"] == "sample"
    assert entry["name"] == "Sample"
    assert entry["version"] == "1.0.0"
    assert entry["source"] == "builtin"
    assert entry["enabled"] is True
    assert entry["tools"] == ["sample_tool"]


def test_the_plugin_manager_disables_and_enables_a_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registry(monkeypatch, _manifest())
    db = _FakeSession()
    context = _context(db=db)
    tool = _spec(manage_tools.MANAGE_TOOLS, "plugin_manager")

    disabled = _run(tool, {"action": "disable", "plugin_id": "sample"}, context)
    enabled = _run(tool, {"action": "enable", "plugin_id": "sample"}, context)

    assert disabled == {"status": "ok", "plugin_id": "sample", "enabled": False}
    assert enabled == {"status": "ok", "plugin_id": "sample", "enabled": True}
    # The change is recorded in the typed session log, once per change.
    assert [event.payload for event in db.added] == [
        {"enabled": []},
        {"enabled": ["sample"]},
    ]
    assert {event.event_type for event in db.added} == {"plugins/enabled"}


def test_the_plugin_manager_removes_a_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _registry(monkeypatch, _manifest())
    context = _context(db=_FakeSession())
    tool = _spec(manage_tools.MANAGE_TOOLS, "plugin_manager")

    removed = _run(tool, {"action": "remove", "plugin_id": "sample"}, context)
    listed = _run(tool, {"action": "list"}, context)

    assert removed == {"status": "ok", "plugin_id": "sample", "enabled": False}
    assert registry.get("sample") is None
    assert listed["plugins"] == []


def test_the_plugin_manager_refuses_an_unknown_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registry(monkeypatch, _manifest())
    context = _context(db=_FakeSession())

    result = _run(
        _spec(manage_tools.MANAGE_TOOLS, "plugin_manager"),
        {"action": "enable", "plugin_id": "not-a-plugin"},
        context,
    )

    assert result["status"] == "error"
    assert "not-a-plugin" in result["message"]


def test_the_plugin_manager_refuses_changes_when_plugins_are_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registry(monkeypatch, _manifest())
    context = _context(db=_FakeSession(), settings=_settings(deepspace_plugins_enabled=False))

    result = _run(
        _spec(manage_tools.MANAGE_TOOLS, "plugin_manager"),
        {"action": "enable", "plugin_id": "sample"},
        context,
    )

    assert result == {"status": "error", "message": "The deployment has disabled plugins."}


def test_a_plugin_the_deployment_disabled_is_not_offered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(monkeypatch, _manifest())
    registry.set_enabled("sample", False)

    assert registry.enabled("sample") is False
    assert registry.all() == []
    # The plugin is still known, so a caller can tell it from a plugin that
    # does not exist.
    assert registry.get("sample") is not None
    assert [manifest.id for manifest in registry.known()] == ["sample"]


def test_a_disabled_plugin_survives_a_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _registry(monkeypatch, _manifest())
    registry.set_enabled("sample", False)
    registry.remove("sample")

    registry.reload()

    assert registry.all() == []
    assert registry.known() == []


def test_the_plugin_manager_switches_on_a_plugin_the_deployment_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(monkeypatch, _manifest())
    registry.set_enabled("sample", False)
    context = _context(db=_FakeSession())

    result = _run(
        _spec(manage_tools.MANAGE_TOOLS, "plugin_manager"),
        {"action": "enable", "plugin_id": "sample"},
        context,
    )

    assert result == {"status": "ok", "plugin_id": "sample", "enabled": True}
    assert registry.enabled("sample") is True


def test_a_conversation_cannot_switch_on_a_plugin_the_deployment_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(monkeypatch, _manifest())
    registry.set_enabled("sample", False)
    service = PluginStateService(_FakeSession())

    with pytest.raises(PluginError):
        service.set_plugin_enabled(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            conversation_id=CONVERSATION_ID,
            plugin_id="sample",
            enabled=True,
        )


def test_the_inspection_tool_names_the_capabilities_of_the_runtime() -> None:
    context = _context()

    result = _run(_spec(inspect_tools.INSPECT_TOOLS, "averqel_inspect_list"), {}, context)

    assert result["status"] == "ok"
    names = {capability["name"] for capability in result["capabilities"]}
    assert names == {"plugins", "tools", "sandbox", "providers", "terminal", "schedule"}
    for capability in result["capabilities"]:
        assert capability["kind"]
        assert capability["description"]
        assert isinstance(capability["tools"], list)


def test_the_inspection_tool_reads_one_capability() -> None:
    context = _context()

    result = _run(
        _spec(inspect_tools.INSPECT_TOOLS, "averqel_inspect_query"),
        {"capability": "plugins"},
        context,
    )

    assert result["status"] == "ok"
    capability = result["capability"]
    assert capability["name"] == "plugins"
    assert capability["kind"] == "plugin-system"
    assert "settings" in capability
    assert "state" in capability
    assert all("mode" in tool for tool in capability["tools"])


def test_the_inspection_tool_refuses_an_unknown_capability() -> None:
    context = _context()

    result = _run(
        _spec(inspect_tools.INSPECT_TOOLS, "averqel_inspect_query"),
        {"capability": "not-a-capability"},
        context,
    )

    assert result["status"] == "error"
    assert "not-a-capability" in result["message"]


def test_the_inspection_tools_are_read_only() -> None:
    modes = {spec.name: spec.mode for spec in inspect_tools.INSPECT_TOOLS}

    assert modes == {"averqel_inspect_list": "read", "averqel_inspect_query": "read"}


def test_the_inspection_tool_reports_the_mode_of_each_tool() -> None:
    context = _context()

    result = _run(
        _spec(inspect_tools.INSPECT_TOOLS, "averqel_inspect_query"),
        {"capability": "terminal"},
        context,
    )

    modes = {tool["name"]: tool["mode"] for tool in result["capability"]["tools"]}
    assert modes["terminal_open"] == "write"
    assert modes["terminal_read"] == "read"


def test_the_dependency_report_names_only_paths_that_exist() -> None:
    context = _context()

    result = _run(
        _spec(dependency_tools.DEPENDENCY_TOOLS, "load_workspace_dependencies"),
        {},
        context,
    )

    assert result["status"] == "ok"
    for path in result["python_packages"].values():
        assert os.path.exists(path)
    for name in ("node", "pnpm", "python"):
        path = result[name]
        if path is not None:
            assert os.path.exists(path)


def test_the_subagent_model_list_names_the_routes_a_child_may_use() -> None:
    candidate = SimpleNamespace(
        provider_config_id=PROVIDER_ID,
        provider_type="openai",
        model_name="gpt-4o",
    )
    selection = _FakeSelection(
        [candidate],
        [
            SimpleNamespace(
                provider_config_id=PROVIDER_ID,
                model_kind="chat",
                model_name="gpt-4o",
            ),
            SimpleNamespace(
                provider_config_id=PROVIDER_ID,
                model_kind="chat",
                model_name="gpt-4o-mini",
            ),
        ],
    )
    context = _context(provider_selection=selection)

    result = _run(_spec(manage_tools.MANAGE_TOOLS, "list_subagent_models"), {}, context)

    assert result["status"] == "ok"
    assert len(result["routes"]) == 1
    route = result["routes"][0]
    assert route["provider_id"] == str(PROVIDER_ID)
    assert route["provider_type"] == "openai"
    assert route["model_name"] == "gpt-4o"
    assert route["models"] == ["gpt-4o", "gpt-4o-mini"]


def test_the_subagent_model_list_is_empty_without_a_provider_route() -> None:
    context = _context(provider_selection=None)

    result = _run(_spec(manage_tools.MANAGE_TOOLS, "list_subagent_models"), {}, context)

    assert result == {"status": "ok", "routes": []}
