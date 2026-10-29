"""Tests for DeepSpace's executable extensions and runtime discovery.

The checks cover the four things that could go wrong: a handler that does
not run, a handler that reaches outside the workspace, a command that sees
more of the host than it should, and a deployment that refuses extension
code.  The workspace is a temporary directory, the database is absent, and
nothing reaches the network.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services.plugins import (
    PluginError,
    PluginManifest,
    PluginScope,
    call_plugin_tool,
    conversation_plugin_scope,
    discover_workspace_plugins,
    parse_manifest,
    plugin_code_allowed,
    plugin_registry,
    plugin_tool_handler,
    plugin_tool_specs,
    workspace_plugin_tool_specs,
)
from app.deepspace.services.tools import runtime_discover as runtime_discover_tools
from app.deepspace.services.tools._base import ToolContext
from app.deepspace.services.tools.extension import EXTENSION_TOOLS

pytestmark = pytest.mark.unit_no_db

TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
CONVERSATION_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


def _settings(tmp_path: Path, **overrides: Any) -> SimpleNamespace:
    """Build the settings the tools read.

    Args:
        tmp_path: The temporary directory the workspace lives in.
        overrides: The settings to override.

    Returns:
        The settings.
    """
    base: dict[str, Any] = {
        "deepspace_workspace_root": str(tmp_path / "workspaces"),
        "local_storage_path": str(tmp_path / "storage"),
        "deepspace_plugins_enabled": True,
        "deepspace_plugin_directory": "",
        "deepspace_plugin_code_enabled": True,
        "deepspace_permission_mode": "workspace-write",
        "deepspace_approval_mode": "never",
        "llm_provider": "openai",
        "llm_model": "gpt-4o",
        "llm_api_base_url": "https://api.openai.com/v1",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _context(settings: SimpleNamespace) -> ToolContext:
    """Build the tool context of one conversation.

    Args:
        settings: The settings in force.

    Returns:
        The context.
    """
    return ToolContext(
        auth=SimpleNamespace(tenant_id=TENANT_ID, user_id=USER_ID),
        conversation_id=CONVERSATION_ID,
        settings=settings,
        db=None,
        task_store=SimpleNamespace(),
        tool_result_store=None,
        provider_registry=SimpleNamespace(),
    )


def _workspace(settings: SimpleNamespace) -> Path:
    """Return the workspace the conversation works in.

    Args:
        settings: The settings in force.

    Returns:
        The workspace root.
    """
    return Path(settings.deepspace_workspace_root) / str(TENANT_ID) / str(USER_ID)


def _write_handler_module(root: Path, name: str, body: str) -> Path:
    """Write one handler module into a workspace.

    Args:
        root: The workspace root.
        name: The module's file name.
        body: The module's source.

    Returns:
        The module's path.
    """
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(body, encoding="utf-8")
    return path


def _spec(specs: list[Any], name: str) -> Any:
    """Return one tool spec by name.

    Args:
        specs: The specs to search.
        name: The tool's name.

    Returns:
        The spec.
    """
    for spec in specs:
        if spec.name == name:
            return spec
    raise AssertionError(f"The tool {name!r} is not declared.")


def _run(spec: Any, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Run one tool and return its result.

    Args:
        spec: The tool's spec.
        arguments: The tool's arguments.
        context: The calling request's tool context.

    Returns:
        The tool's result.
    """
    return asyncio.run(spec.handler(arguments, context))


def test_a_workspace_python_handler_runs_and_answers(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _workspace(settings)
    _write_handler_module(
        root,
        "greeter.py",
        "def greet(arguments, context):\n"
        "    return {\n"
        "        'status': 'ok',\n"
        "        'greeting': 'hello ' + str(arguments['name']),\n"
        "        'conversation': str(context.conversation_id),\n"
        "    }\n",
    )
    manifest = parse_manifest(
        {
            "id": "greeter",
            "handler": "greeter:greet",
            "handler_module": "greeter.py",
            "tools": [
                {
                    "name": "greet",
                    "description": "Greets somebody.",
                    "mode": "read",
                    "parameters": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                    },
                }
            ],
        },
        source="workspace",
    )
    context = _context(settings)

    result = call_plugin_tool(
        manifest,
        tool_name="greet",
        arguments={"name": "world"},
        context=context,
    )

    assert result["status"] == "ok"
    assert result["greeting"] == "hello world"
    # The handler receives the tool context, so it behaves like a native one.
    assert result["conversation"] == str(CONVERSATION_ID)


def test_a_workspace_plugin_tool_becomes_a_native_tool_and_runs(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _workspace(settings)
    _write_handler_module(
        root,
        "greeter.py",
        "def greet(arguments):\n"
        "    return {'status': 'ok', 'greeting': 'hi ' + str(arguments['name'])}\n",
    )
    manifest = parse_manifest(
        {
            "id": "greeter",
            "handler": "greeter:greet",
            "handler_module": "greeter.py",
            "tools": [
                {
                    "name": "greet",
                    "description": "Greets somebody.",
                    "mode": "read",
                    "parameters": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                    },
                }
            ],
        },
        source="workspace",
    )
    scope = PluginScope(registry=plugin_registry(), workspace=(manifest,))

    specs = plugin_tool_specs(("greeter",), scope=scope)

    # The plugin's tool is declared, validated and dispatched like a native one.
    assert [spec.name for spec in specs] == ["greet"]
    assert specs[0].mode == "read"
    assert specs[0].definition["function"]["name"] == "greet"
    result = asyncio.run(specs[0].handler({"name": "world"}, _context(settings)))
    assert result["greeting"] == "hi world"


def test_the_workspace_plugin_tools_are_offered_to_the_conversation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _workspace(settings)
    _write_handler_module(
        root,
        "greeter.py",
        "def greet(arguments):\n    return {'status': 'ok'}\n",
    )
    plugin_dir = root / ".deepspace" / "plugins" / "greeter"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "deepspace-plugin.json").write_text(
        json.dumps(
            {
                "id": "greeter",
                "name": "Greeter",
                "handler": "greeter:greet",
                "handler_module": "greeter.py",
                "tools": [
                    {
                        "name": "greet",
                        "description": "Greets somebody.",
                        "mode": "read",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    context = _context(settings)

    specs = workspace_plugin_tool_specs(settings=settings, tenant_id=TENANT_ID, user_id=USER_ID)

    assert [spec.name for spec in specs] == ["greet"]
    handler = plugin_tool_handler("greet", context=context)
    assert handler is not None
    assert asyncio.run(handler({}, context))["status"] == "ok"
    assert plugin_tool_handler("not_a_tool", context=context) is None


def test_a_handler_whose_module_leaves_the_workspace_is_refused(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _workspace(settings)
    root.mkdir(parents=True, exist_ok=True)
    (root.parent / "outside.py").write_text(
        "def run(arguments):\n    return {}\n", encoding="utf-8"
    )

    with pytest.raises(PluginError, match="may not leave the workspace"):
        parse_manifest(
            {
                "id": "escape",
                "handler": "outside:run",
                "handler_module": "../outside.py",
            },
            source="workspace",
        )

    # A manifest that reached the call anyway is refused there as well.
    manifest = PluginManifest(
        id="escape",
        name="escape",
        version="1.0.0",
        description="",
        source="workspace",
        handler="outside:run",
        handler_module="../outside.py",
    )
    with pytest.raises(PluginError, match="leaves the workspace"):
        call_plugin_tool(
            manifest,
            tool_name="run",
            arguments={},
            context=_context(settings),
        )


def test_a_command_handler_runs_without_the_deployment_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AKS_CLUSTER_SECRET", "s3cret")
    settings = _settings(tmp_path)
    root = _workspace(settings)
    root.mkdir(parents=True, exist_ok=True)
    manifest = parse_manifest(
        {
            "id": "shell",
            "handler": 'echo "secret=[$AKS_CLUSTER_SECRET]"; pwd',
            "handler_kind": "command",
            "handler_timeout_seconds": 10,
        },
        source="workspace",
    )

    result = call_plugin_tool(
        manifest,
        tool_name="run",
        arguments={},
        context=_context(settings),
    )

    assert result["status"] == "ok"
    assert result["exit_code"] == 0
    # The deployment's secret never reaches the command, and the command runs
    # in the conversation's workspace.
    assert "s3cret" not in result["output"]
    assert "secret=[]" in result["output"]
    assert str(root) in result["output"]


def test_a_command_handler_that_runs_too_long_is_refused(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _workspace(settings)
    root.mkdir(parents=True, exist_ok=True)
    manifest = parse_manifest(
        {
            "id": "slow",
            "handler": "sleep 30",
            "handler_kind": "command",
            "handler_timeout_seconds": 1,
        },
        source="workspace",
    )

    with pytest.raises(PluginError, match="did not finish"):
        call_plugin_tool(
            manifest,
            tool_name="run",
            arguments={},
            context=_context(settings),
        )


def test_a_plugin_in_the_workspace_is_offered_to_that_conversation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _workspace(settings)
    plugin_dir = root / ".deepspace" / "plugins" / "hello"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "deepspace-plugin.json").write_text(
        json.dumps(
            {
                "id": "hello",
                "name": "Hello",
                "handler": "hello:run",
                "handler_module": "hello.py",
                "tools": [{"name": "hello_tool", "description": "Says hello.", "mode": "read"}],
            }
        ),
        encoding="utf-8",
    )

    manifests = discover_workspace_plugins(root)
    scope = conversation_plugin_scope(settings, tenant_id=TENANT_ID, user_id=USER_ID)

    assert [manifest.id for manifest in manifests] == ["hello"]
    assert manifests[0].source == "workspace"
    assert scope.get("hello") is not None
    assert "hello" in [manifest.id for manifest in scope.known()]
    # The deployment's own registry does not hold it, so no other
    # conversation is offered it.
    assert plugin_registry().get("hello") is None


def test_a_deployment_that_refuses_extension_code_refuses_the_call(tmp_path: Path) -> None:
    settings = _settings(tmp_path, deepspace_plugin_code_enabled=False)
    root = _workspace(settings)
    _write_handler_module(
        root,
        "greeter.py",
        "def greet(arguments):\n    return {'status': 'ok'}\n",
    )
    manifest = parse_manifest(
        {
            "id": "greeter",
            "handler": "greeter:greet",
            "handler_module": "greeter.py",
        },
        source="workspace",
    )
    command = parse_manifest(
        {
            "id": "shell",
            "handler": "echo hello",
            "handler_kind": "command",
        },
        source="workspace",
    )

    with pytest.raises(PluginError, match="does not run extension code"):
        call_plugin_tool(
            manifest,
            tool_name="greet",
            arguments={},
            context=_context(settings),
        )
    with pytest.raises(PluginError, match="does not run extension code"):
        call_plugin_tool(
            command,
            tool_name="run",
            arguments={},
            context=_context(settings),
        )
    # The deployment's own code is not extension code, so it still runs.
    own = parse_manifest(
        {"id": "own", "handler": "app.deepspace.plugins.builtin.code_metrics.handler:code_metrics"},
        source="builtin",
    )
    assert plugin_code_allowed(own, settings) is True
    assert plugin_code_allowed(manifest, settings) is False


def test_the_runtime_discovery_reports_the_tools_the_plugins_and_the_routes(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    root = _workspace(settings)
    plugin_dir = root / ".deepspace" / "plugins" / "hello"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "deepspace-plugin.json").write_text(
        json.dumps(
            {
                "id": "hello",
                "name": "Hello",
                "tools": [{"name": "hello_tool", "description": "Says hello.", "mode": "read"}],
            }
        ),
        encoding="utf-8",
    )
    context = _context(settings)

    result = _run(
        _spec(runtime_discover_tools.RUNTIME_DISCOVER_TOOLS, "runtime_discover"), {}, context
    )

    assert result["status"] == "ok"
    assert result["kind"] == "all"
    names = {entry["name"] for entry in result["tools"]}
    # The catalogue the runtime reports is the native tool registry, so it
    # holds the product's own tools.  The discovery tool joins that
    # catalogue when the tool registry declares it.
    assert "extension_list" in names
    assert all(entry["mode"] for entry in result["tools"])
    assert "hello_tool" in names
    assert "hello" in [plugin["id"] for plugin in result["plugins"]]
    hello = next(plugin for plugin in result["plugins"] if plugin["id"] == "hello")
    assert hello["source"] == "workspace"
    assert hello["tools"] == ["hello_tool"]
    assert result["routes"] == []
    assert result["extensions"] == []


def test_the_runtime_discovery_reports_one_part_at_a_time(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    context = _context(settings)

    result = _run(
        _spec(runtime_discover_tools.RUNTIME_DISCOVER_TOOLS, "runtime_discover"),
        {"kind": "routes"},
        context,
    )

    assert result["status"] == "ok"
    assert result["kind"] == "routes"
    assert "routes" in result
    assert "tools" not in result
    assert "plugins" not in result


def test_the_runtime_discovery_refuses_an_unknown_part(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    result = _run(
        _spec(runtime_discover_tools.RUNTIME_DISCOVER_TOOLS, "runtime_discover"),
        {"kind": "everything"},
        _context(settings),
    )

    assert result["status"] == "error"
    assert "everything" in result["message"]


def test_the_runtime_discovery_runs_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _workspace(settings)
    marker = root / "ran.txt"
    _write_handler_module(
        root,
        "side_effect.py",
        "def run(arguments):\n"
        "    open(" + repr(str(marker)) + ", 'w').write('ran')\n"
        "    return {'status': 'ok'}\n",
    )
    plugin_dir = root / ".deepspace" / "plugins" / "side-effect"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "deepspace-plugin.json").write_text(
        json.dumps(
            {
                "id": "side-effect",
                "name": "Side effect",
                "handler": "side_effect:run",
                "handler_module": "side_effect.py",
                "tools": [{"name": "side_effect_tool", "description": "Runs.", "mode": "read"}],
            }
        ),
        encoding="utf-8",
    )

    result = _run(
        _spec(runtime_discover_tools.RUNTIME_DISCOVER_TOOLS, "runtime_discover"),
        {},
        _context(settings),
    )

    assert result["status"] == "ok"
    assert "side-effect" in [plugin["id"] for plugin in result["plugins"]]
    # The discovery reports the plugin; it never runs it.
    assert not marker.exists()
    assert _spec(runtime_discover_tools.RUNTIME_DISCOVER_TOOLS, "runtime_discover").mode == "read"


def test_the_agent_installs_a_plugin_in_its_own_workspace(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _workspace(settings)
    context = _context(settings)

    result = _run(
        _spec(EXTENSION_TOOLS, "plugin_install"),
        {
            "name": "greeter",
            "description": "Greets somebody.",
            "handler": "greeter:greet",
            "handler_module": "greeter.py",
            "tools": [
                {
                    "name": "greet",
                    "description": "Greets somebody.",
                    "mode": "read",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        },
        context,
    )

    assert result["status"] == "ok"
    manifest_path = root / ".deepspace" / "plugins" / "greeter" / "deepspace-plugin.json"
    assert manifest_path.is_file()
    assert [manifest.id for manifest in discover_workspace_plugins(root)] == ["greeter"]


def test_the_agent_cannot_install_a_plugin_when_the_deployment_refuses_code(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, deepspace_plugin_code_enabled=False)
    root = _workspace(settings)

    result = _run(
        _spec(EXTENSION_TOOLS, "plugin_install"),
        {
            "name": "greeter",
            "handler": "greeter:greet",
            "handler_module": "greeter.py",
        },
        _context(settings),
    )

    assert result["status"] == "error"
    assert "extension code" in result["message"]
    assert not (root / ".deepspace" / "plugins" / "greeter").exists()
