"""Tests for DeepSpace subagent parity with DeepSeek Harness."""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from app.deepspace.services.presets import AgentPreset, PresetNotFoundError, PresetResolution
from app.deepspace.services.subagent_acp_backend import (
    AcpProvider,
    AcpRunSpec,
    AcpSubagentResult,
)
from app.deepspace.services.subagent_cold_resume import (
    cold_resume_from_db_row,
    cold_resume_from_descriptor,
)
from app.deepspace.services.subagent_descriptor import (
    SUBAGENT_DESCRIPTOR_VERSION,
    ContinuableSubagentDescriptor,
    OneShotSubagentDescriptor,
    apply_child_composition,
    descriptor_to_run_spec,
    fold_subagent_descriptor,
    has_independent_preset_selection,
    parse_subagent_descriptor,
    snapshot_subagent_descriptor,
)
from app.deepspace.services.subagent_lifecycle import (
    ActivationPool,
    SettlementState,
    SubagentManager,
    SubagentResult,
    create_activation_observer,
    create_lifecycle_emitter,
)
from app.deepspace.services.subagent_provider_claude_code import (
    ClaudeCodeProvider,
)
from app.deepspace.services.subagent_provider_codex import (
    CodexProvider,
)
from app.deepspace.services.subagent_provider_dsh_sdk import (
    DshSdkProvider,
)
from app.deepspace.services.subagent_runner import build_child_descriptor, effective_preset
from app.deepspace.services.subagent_tools import (
    CHILD_EXCLUDED_TOOL_NAMES,
    CHILD_TURN_TOOL_NAMES,
    DELEGATION_TOOL_NAMES,
    TEAM_TOOL_NAMES,
    get_agent_tool,
    interrupt_agent_tool,
    list_agents_tool,
    model_selection_settings,
    preset_selection,
    subagent_tool_definitions,
)

# === Descriptor Tests ===


def test_descriptor_version():
    assert SUBAGENT_DESCRIPTOR_VERSION == 3


def test_one_shot_descriptor():
    d = OneShotSubagentDescriptor(version=3, mode="one-shot", provider="test", label="my task")
    assert d.mode == "one-shot"
    assert d.provider == "test"
    assert d.label == "my task"


def test_continuable_descriptor():
    d = ContinuableSubagentDescriptor(
        version=3,
        mode="continuable",
        provider="test",
        label="my task",
        agent_model="gpt-4",
        persona="helper",
    )
    assert d.mode == "continuable"
    assert d.agent_model == "gpt-4"
    assert d.persona == "helper"


def test_parse_descriptor_valid():
    data = {"version": 3, "mode": "continuable", "provider": "acp", "label": "test"}
    d = parse_subagent_descriptor(data)
    assert d is not None
    assert d.mode == "continuable"
    assert d.provider == "acp"


def test_parse_descriptor_version_mismatch():
    data = {"version": 99, "mode": "continuable", "provider": "acp", "label": "test"}
    d = parse_subagent_descriptor(data)
    assert d is None


def test_fold_descriptor():
    events = [
        {"type": "other", "data": {}},
        {
            "type": "subagent/descriptor",
            "data": {"version": 3, "mode": "one-shot", "provider": "test", "label": "l"},
        },
    ]
    d = fold_subagent_descriptor(events)
    assert d is not None
    assert d.mode == "one-shot"


def test_fold_descriptor_none():
    events = [{"type": "other", "data": {}}]
    d = fold_subagent_descriptor(events)
    assert d is None


def test_snapshot_descriptor():
    snap = snapshot_subagent_descriptor("continuable", "acp", "label", agent_model="gpt-4")
    assert snap["version"] == 3
    assert snap["mode"] == "continuable"
    assert snap["agentModel"] == "gpt-4"


def test_child_descriptor_preserves_selected_preset():
    child = SimpleNamespace(
        mode="continuable",
        label="safe work",
        agent_provider=None,
        agent_model=None,
        reasoning_effort=None,
        persona=None,
        tool_filter={},
        descriptor={"agentPreset": "minimal"},
    )
    assert build_child_descriptor(child)["agentPreset"] == "minimal"


def test_independent_child_preset_can_only_narrow_parent(monkeypatch):
    import app.deepspace.services.presets as presets

    def resolution(preset_id, tools, *, ptc, depth, sandbox, spill):
        preset = AgentPreset(
            id=preset_id,
            name=preset_id,
            description="test",
            tool_allowlist=tools,
            ptc_enabled=ptc,
            subagent_max_depth=depth,
            plan_mode_by_default=False,
            sandbox_mode=sandbox,
            reasoning_effort=None,
            spill_threshold_chars=spill,
            editable=False,
        )
        return PresetResolution(
            preset=preset,
            tool_allowlist=tools,
            ptc_enabled=ptc,
            subagent_max_depth=depth,
            sandbox_mode=sandbox,
            reasoning_effort=None,
            spill_threshold_chars=spill,
            plan_mode_by_default=False,
        )

    parent = resolution(
        "parent", ("fs_read", "bash"), ptc=False, depth=2, sandbox="read-only", spill=2_000
    )
    child = resolution(
        "child", ("fs_read", "fs_write"), ptc=True, depth=4, sandbox="workspace-write", spill=1_000
    )
    monkeypatch.setattr(presets, "effective_preset_resolution", lambda **_kwargs: parent)
    monkeypatch.setattr(presets, "resolve_preset", lambda *_args, **_kwargs: child)
    context = SimpleNamespace(
        db=object(),
        auth=SimpleNamespace(tenant_id="tenant", user_id="user"),
        conversation_id="conversation",
        settings=object(),
        agent_preset_override="child",
    )

    selected = effective_preset(context)
    assert selected is not None
    assert selected.tool_allowlist == ("fs_read",)
    assert selected.ptc_enabled is False
    assert selected.subagent_max_depth == 2
    assert selected.sandbox_mode == "read-only"
    assert selected.spill_threshold_chars == 1_000

    def unknown(*_args, **_kwargs):
        raise PresetNotFoundError("unknown child preset")

    monkeypatch.setattr(presets, "resolve_preset", unknown)
    with pytest.raises(RuntimeError, match="unavailable"):
        effective_preset(context)


def test_has_independent_preset():
    events = [
        {"type": "agent-preset/selected", "data": {"agentPreset": "standard"}},
        {"type": "subagent/descriptor", "data": {}},
    ]
    assert has_independent_preset_selection(events) is True

    events2 = [
        {"type": "subagent/descriptor", "data": {}},
        {"type": "agent-preset/selected", "data": {"agentPreset": "standard"}},
    ]
    assert has_independent_preset_selection(events2) is False


def test_descriptor_to_run_spec():
    d = ContinuableSubagentDescriptor(
        version=3, mode="continuable", provider="acp", label="test", agent_model="gpt-4"
    )
    spec = descriptor_to_run_spec(d)
    assert spec["provider"] == "acp"
    assert spec["mode"] == "continuable"
    assert spec["agent_model"] == "gpt-4"


# === Lifecycle Tests ===


def test_settlement_state_enum():
    assert SettlementState.READY.value == "ready"
    assert SettlementState.WAIT.value == "wait"
    assert SettlementState.RETRY.value == "retry"
    assert SettlementState.CLOSED.value == "closed"


def test_lifecycle_emitter():
    emitter = create_lifecycle_emitter(None)
    events = []
    emitter.register(lambda name, info, parent: events.append((name, info)))
    emitter("subagent/start", {"id": "1"}, None)
    assert len(events) == 1
    assert events[0][0] == "subagent/start"


def test_activation_observer():
    emitter = create_lifecycle_emitter(None)
    events = []
    emitter.register(lambda name, info, parent: events.append((name, info, parent)))
    obs = create_activation_observer(emitter, "test", "child-1", None, run_id="run-1")
    assert obs.run_id is not None
    obs.start()
    obs.settle(SubagentResult(stop_reason="completed"))
    obs.start()
    obs.settle(SubagentResult(stop_reason="error"))
    assert [name for name, _, _ in events] == ["subagent/start", "subagent/end"]
    assert events[0][1].run_id == "run-1"


def test_activation_pool():
    pool = ActivationPool(max_active=2)
    release1 = pool.reserve(2)
    release2 = pool.reserve(2)
    assert pool._active == 2
    release1()
    assert pool._active == 1
    release2()
    assert pool._active == 0
    limited_release = pool.reserve(1)
    try:
        with pytest.raises(RuntimeError, match="limit reached"):
            pool.reserve(1)
    finally:
        limited_release()


def test_subagent_manager_init():
    mgr = SubagentManager(ctx=None, max_active_subagents=4, max_depth=2)
    assert mgr.max_active_subagents() == 4
    assert mgr.max_depth() == 2
    assert mgr.draining is False


def test_subagent_manager_draining():
    mgr = SubagentManager(ctx=None)
    assert mgr.is_draining() is False
    mgr.start_draining()
    assert mgr.is_draining() is True
    mgr.stop_draining()
    assert mgr.is_draining() is False


def test_subagent_manager_hold_ownership():
    mgr = SubagentManager(ctx=None)
    release = mgr.hold_ownership(None, "child-1")
    # Should be no-op since no resident parent
    assert callable(release)
    release()


@pytest.mark.asyncio
async def test_subagent_manager_requires_real_handles_and_disposes_owned_children():
    class Handle:
        def __init__(self, agent):
            self.agent = agent
            self.disposed = False

        async def dispose(self):
            self.disposed = True

    root_agent = SimpleNamespace(
        id="root-agent",
        session=SimpleNamespace(messages=[], header=None),
        whenIdle=lambda: asyncio.sleep(0),
        cancel=lambda _reason: None,
    )
    child_agent = SimpleNamespace(
        id="child-agent",
        session=SimpleNamespace(messages=[], header=None),
        whenIdle=lambda: asyncio.sleep(0),
        cancel=lambda _reason: None,
    )
    context = SimpleNamespace(agents={root_agent.id: root_agent, child_agent.id: child_agent})
    manager = SubagentManager(ctx=context)

    with pytest.raises(ValueError, match="materialized local session handle"):
        manager.start_local({"request": {"parent": None}})

    root_handle = Handle(root_agent)
    root = manager.start_local(
        {
            "provider": "in-process",
            "childId": root_agent.id,
            "request": {"parent": None},
            "handle": root_handle,
        }
    )
    child_handle = Handle(child_agent)
    child = manager.start_local(
        {
            "provider": "in-process",
            "childId": child_agent.id,
            "request": {"parent": root_agent},
            "handle": child_handle,
        }
    )
    root_activation = manager.get_resident(root_agent.id)
    assert root_activation is not None
    child_activation = manager.get_resident(child_agent.id)
    assert child_activation is not None
    assert child_agent.id in root_activation.owned_children
    assert manager.list_resident() == [root_agent.id, child_agent.id]

    settlement = asyncio.create_task(manager.wait_for_settlement(root_activation))
    await asyncio.sleep(0)
    await root["dispose"]()
    assert await asyncio.wait_for(settlement, timeout=1) == SettlementState.CLOSED
    assert root_handle.disposed and child_handle.disposed
    assert manager.get_resident(root_agent.id) is None
    assert manager.get_resident(child_agent.id) is None
    assert (await root["result"]).stop_reason == "cancelled"
    assert (await child["result"]).stop_reason == "cancelled"


def test_subagent_manager_start_requires_active_external_handle():
    manager = SubagentManager(ctx=None)
    with pytest.raises(ValueError, match="active external run handle"):
        manager.start_external({"provider": "acp", "request": {"parent": None}})


# === ACP Backend Tests ===


def test_acp_provider_init():
    provider = AcpProvider(command="python", permission="allow")
    assert provider.command == "python"
    assert provider.permission == "allow"
    assert provider.name == "acp"


def test_acp_run_spec():
    spec = AcpRunSpec(command="python", args=["-m", "test"], cwd="/tmp")
    assert spec.command == "python"
    assert spec.args == ["-m", "test"]


def test_acp_subagent_result():
    r = AcpSubagentResult(status="completed", answer="hello", stop_reason="completed")
    assert r.status == "completed"
    assert r.answer == "hello"


@pytest.mark.asyncio
async def test_acp_provider_real_protocol_and_permission_answer(tmp_path):
    script = r"""import json, sys
def emit(value):
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        emit({"jsonrpc":"2.0","id":request["id"],"result":{"protocolVersion":1}})
    elif method == "session/new":
        emit({"jsonrpc":"2.0","id":request["id"],"result":{"sessionId":"session-test"}})
    elif method == "session/prompt":
        emit({"jsonrpc":"2.0","id":90,"method":"session/request_permission","params":{"sessionId":"session-test","options":[{"kind":"allow_once","optionId":"allow"}]}})
        permission = json.loads(sys.stdin.readline())
        text = "approved" if permission.get("result",{}).get("outcome",{}).get("outcome") == "selected" else "rejected"
        emit({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"session-test","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":text}}}})
        emit({"jsonrpc":"2.0","id":request["id"],"result":{"stopReason":"end_turn"}})
    elif method == "session/cancel":
        emit({"jsonrpc":"2.0","id":request["id"],"result":{}})
"""
    provider = AcpProvider(
        command=sys.executable,
        args=["-u", "-c", script],
        permission="allow",
        cwd=str(tmp_path),
    )
    spec = type(
        "Spec", (), {"task": "inspect", "persona": "carefully", "cwd": str(tmp_path), "env": {}}
    )()
    result = await provider.run(spec)
    assert result.status == "completed"
    assert result.answer == "approved"


@pytest.mark.asyncio
async def test_acp_provider_applies_requested_model_only_from_advertised_options(tmp_path):
    script = r"""import json, sys
selected = "default-model"
def emit(value):
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        emit({"jsonrpc":"2.0","id":request["id"],"result":{"protocolVersion":1}})
    elif method == "session/new":
        emit({"jsonrpc":"2.0","id":request["id"],"result":{"sessionId":"session-model","configOptions":[{"id":"model","category":"model","type":"select","currentValue":selected,"options":[{"value":"model-a","name":"Model A"},{"value":"model-b","name":"Model B"}]}]}})
    elif method == "session/set_config_option":
        selected = request["params"]["value"]
        emit({"jsonrpc":"2.0","id":request["id"],"result":{"configOptions":[{"id":"model","category":"model","type":"select","currentValue":selected,"options":[{"value":"model-a","name":"Model A"},{"value":"model-b","name":"Model B"}]}]}})
    elif method == "session/prompt":
        text = "model selected: " + selected
        emit({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"session-model","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":text}}}})
        emit({"jsonrpc":"2.0","id":request["id"],"result":{"stopReason":"end_turn"}})
"""
    provider = AcpProvider(
        command=sys.executable,
        args=["-u", "-c", script],
        cwd=str(tmp_path),
    )

    result = await provider.run(
        SimpleNamespace(
            task="inspect",
            persona="Be precise",
            model="model-b",
            cwd=str(tmp_path),
            env={},
        )
    )

    assert result.status == "completed"
    assert result.answer == "model selected: model-b"


@pytest.mark.asyncio
async def test_acp_provider_fails_closed_when_requested_model_is_not_advertised(tmp_path):
    script = r"""import json, sys
def emit(value):
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()
for line in sys.stdin:
    request = json.loads(line)
    if request.get("method") == "initialize":
        emit({"jsonrpc":"2.0","id":request["id"],"result":{"protocolVersion":1}})
    elif request.get("method") == "session/new":
        emit({"jsonrpc":"2.0","id":request["id"],"result":{"sessionId":"session-model","configOptions":[]}})
"""
    provider = AcpProvider(
        command=sys.executable,
        args=["-u", "-c", script],
        cwd=str(tmp_path),
    )

    result = await provider.run(
        SimpleNamespace(task="inspect", model="unavailable-model", cwd=str(tmp_path), env={})
    )

    assert result.status == "failed"
    assert "does not expose a selectable model" in str(result.error)


@pytest.mark.asyncio
async def test_acp_provider_cancellation_reaps_child(tmp_path):
    script = r"""import json, sys, time
def emit(value):
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()
for line in sys.stdin:
    request = json.loads(line)
    if request.get("method") == "initialize":
        emit({"jsonrpc":"2.0","id":request["id"],"result":{}})
    elif request.get("method") == "session/new":
        emit({"jsonrpc":"2.0","id":request["id"],"result":{"sessionId":"session-test"}})
    elif request.get("method") == "session/prompt":
        while True: time.sleep(1)
"""
    provider = AcpProvider(
        command=sys.executable,
        args=["-u", "-c", script],
        cwd=str(tmp_path),
        dispose_eof_grace_ms=0,
        dispose_grace_ms=100,
    )
    checks = 0

    def cancel_check():
        nonlocal checks
        checks += 1
        return checks > 8

    result = await asyncio.wait_for(
        provider.run(SimpleNamespace(task="wait", cwd=str(tmp_path), env={}), cancel_check),
        timeout=4,
    )
    assert result.status == "cancelled"


# === Tool Tests ===


def test_subagent_tool_definitions():
    tools = subagent_tool_definitions()
    assert len(tools) == 6
    names = [t.name for t in tools]
    assert "subagent" in names
    assert "list_agents" in names
    assert "get_agent" in names
    from app.deepspace.services.tools.delegation import DELEGATION_TOOLS
    from app.deepspace.services.tools.team import TEAM_TOOLS

    active = {tool.name: tool for tool in (*DELEGATION_TOOLS, *TEAM_TOOLS)}
    for projected in tools:
        assert projected.parameters == active[projected.name].definition["function"]["parameters"]
        assert projected.handler is active[projected.name].handler


def test_delegation_tool_names():
    assert "subagent" in DELEGATION_TOOL_NAMES
    assert "send_message" in DELEGATION_TOOL_NAMES
    assert "ask_user" not in DELEGATION_TOOL_NAMES


def test_child_turn_tool_names():
    assert "subagent" in CHILD_TURN_TOOL_NAMES
    assert "subagent_fork" in CHILD_TURN_TOOL_NAMES


def test_team_tool_names():
    assert "spawn_teammate" in TEAM_TOOL_NAMES
    assert "team_send_message" in TEAM_TOOL_NAMES


def test_child_excluded_tool_names():
    assert "ask_user" in CHILD_EXCLUDED_TOOL_NAMES


def test_list_agents_tool():
    result = list_agents_tool([{"id": "1", "status": "running"}])
    assert result["status"] == "ok"
    assert result["count"] == 1


def test_get_agent_tool_found():
    result = get_agent_tool("1", {"id": "1", "status": "running"})
    assert result["status"] == "ok"


def test_get_agent_tool_not_found():
    result = get_agent_tool("999", None)
    assert result["status"] == "not_found"


def test_interrupt_agent_tool():
    result = interrupt_agent_tool("agent-1", "user cancel")
    assert result["status"] == "interrupted"
    assert result["agent_id"] == "agent-1"


def test_model_selection_settings():
    settings = model_selection_settings(provider="openai", model="gpt-4", reasoning_effort="high")
    assert settings["provider"] == "openai"
    assert settings["model"] == "gpt-4"
    assert settings["reasoning_effort"] == "high"


def test_preset_selection():
    result = preset_selection("standard")
    assert result == {"agentPreset": "standard"}
    assert preset_selection(None) == {}


# === Claude Code Backend Tests ===


def test_claude_code_provider_init():
    provider = ClaudeCodeProvider(command="claude")
    assert provider.name == "claude-code"
    assert provider.command == "claude"


@pytest.mark.asyncio
async def test_claude_code_run_not_found():
    provider = ClaudeCodeProvider(command="nonexistent-claude-xyz")
    spec = type("Spec", (), {"task": "test", "cwd": ".", "env": {}})()
    result = await provider.run(spec)
    assert result.status == "failed"
    assert (
        "not found" in (result.error or "").lower()
        or "no such" in (result.error or "").lower()
        or result.error is not None
    )


# === Codex Backend Tests ===


def test_codex_provider_init():
    provider = CodexProvider(command="codex")
    assert provider.name == "codex"


@pytest.mark.asyncio
async def test_codex_run_not_found():
    provider = CodexProvider(command="nonexistent-codex-xyz")
    spec = type("Spec", (), {"task": "test", "cwd": ".", "env": {}})()
    result = await provider.run(spec)
    assert result.status == "failed"


# === DSH SDK Backend Tests ===


def test_dsh_sdk_provider_init():
    provider = DshSdkProvider(model="deepseek-chat")
    assert provider.name == "dsh-sdk"
    assert provider.model == "deepseek-chat"


@pytest.mark.asyncio
async def test_dsh_sdk_runs_against_local_jsonrpc_runtime(tmp_path):
    script = r"""import json, sys
def emit(value):
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        emit({"jsonrpc":"2.0","id":request["id"],"result":{"serverInfo":{"name":"test"}}})
    elif method == "session/query":
        session_id = request["params"]["sessionId"]
        emit({"jsonrpc":"2.0","method":"session.event","params":{"sessionId":session_id,"event":{"type":"assistant/message","data":{"message":{"content":[{"type":"text","text":"sdk-answer"}]}}}}})
        emit({"jsonrpc":"2.0","method":"session.status","params":{"sessionId":session_id,"status":"idle"}})
        emit({"jsonrpc":"2.0","id":request["id"],"result":{"kind":"prompt"}})
    elif method == "shutdown":
        emit({"jsonrpc":"2.0","id":request["id"],"result":{}})
"""
    provider = DshSdkProvider(
        command=sys.executable,
        args=["-u", "-c", script],
        dsh_home=str(tmp_path / "dsh-home"),
        dispose_eof_grace_ms=100,
    )
    spec = SimpleNamespace(task="test task", cwd=str(tmp_path), env={})
    result = await provider.run(spec)
    assert result.status == "completed"
    assert result.answer == "sdk-answer"


@pytest.mark.asyncio
async def test_dsh_sdk_fails_closed_without_home_or_with_unenforceable_filter(tmp_path):
    provider = DshSdkProvider(command="must-not-spawn")
    no_home = await provider.run(SimpleNamespace(task="x", env={}, cwd=str(tmp_path)))
    assert no_home.status == "failed"
    assert "DSH_HOME" in (no_home.error or "")

    with_home = DshSdkProvider(command="must-not-spawn", dsh_home=str(tmp_path))
    filtered = await with_home.run(
        SimpleNamespace(task="x", env={}, cwd=str(tmp_path), tool_deny=["bash"])
    )
    assert filtered.status == "failed"
    assert "cannot enforce" in (filtered.error or "")


# === Cold Resume Tests ===


def test_cold_resume_from_descriptor_continuable():
    events = [
        {
            "type": "subagent/descriptor",
            "data": {"version": 3, "mode": "continuable", "provider": "acp", "label": "test"},
        },
    ]
    result = cold_resume_from_descriptor("child-1", events)
    assert result.success is True
    assert result.descriptor is not None
    assert result.descriptor.mode == "continuable"


def test_cold_resume_restores_independent_preset_before_descriptor():
    events = [
        {"type": "subagent/preset", "data": {"agentPreset": "minimal"}},
        {
            "type": "subagent/descriptor",
            "data": {
                "version": 3,
                "mode": "continuable",
                "provider": "deepspace",
                "label": "safe child",
            },
        },
    ]
    result = cold_resume_from_descriptor("child-1", events)
    assert result.success
    assert result.composition["agentPreset"] == "minimal"


def test_cold_resume_from_descriptor_one_shot():
    events = [
        {
            "type": "subagent/descriptor",
            "data": {"version": 3, "mode": "one-shot", "provider": "test", "label": "test"},
        },
    ]
    result = cold_resume_from_descriptor("child-1", events)
    assert result.success is False
    assert "one-shot" in (result.error or "")


def test_cold_resume_from_descriptor_none():
    events = [{"type": "other", "data": {}}]
    result = cold_resume_from_descriptor("child-1", events)
    assert result.success is False


def test_cold_resume_from_db_row():
    row = {
        "id": "child-1",
        "mode": "continuable",
        "descriptor": {"version": 3, "mode": "continuable", "provider": "test", "label": "test"},
        "persona": "helper",
        "tool_filter": {"allow": ["read_file"]},
        "agent_provider": "openai",
        "agent_model": "gpt-4",
        "reasoning_effort": "high",
        "label": "test task",
    }
    result = cold_resume_from_db_row(row)
    assert result.success is True
    assert result.composition.get("persona") == "helper"
    assert result.composition.get("toolFilter") == {"allow": ["read_file"]}


def test_cold_resume_from_db_row_recovers_independent_preset_event():
    row = {
        "id": "child-1",
        "mode": "continuable",
        "descriptor": {
            "version": 3,
            "mode": "continuable",
            "provider": "deepspace",
            "label": "safe child",
        },
    }
    events = [
        {"type": "subagent/preset", "data": {"agentPreset": "minimal"}},
        {"type": "subagent/descriptor", "data": row["descriptor"]},
    ]

    result = cold_resume_from_db_row(row, events=events)

    assert result.success
    assert result.composition["agentPreset"] == "minimal"


def test_cold_resume_one_shot_db():
    row = {
        "id": "child-2",
        "mode": "one-shot",
        "descriptor": {"version": 3, "mode": "one-shot", "provider": "test", "label": "test"},
    }
    result = cold_resume_from_db_row(row)
    assert result.success is False


def test_apply_child_composition():
    class FakeChild:
        pass

    child = FakeChild()
    apply_child_composition(child, None, {"persona": "helper"})
    # Should not crash even if child doesn't have persona attr


# === Cold Resume with Independent Preset ===


def test_cold_resume_independent_preset():
    events = [
        {"type": "agent-preset/selected", "data": {"agentPreset": "standard"}},
        {
            "type": "subagent/descriptor",
            "data": {"version": 3, "mode": "continuable", "provider": "test", "label": "test"},
        },
    ]
    result = cold_resume_from_descriptor("child-1", events)
    assert result.success is True
    assert result.composition.get("agentPreset") == "standard"


def test_cold_resume_missing_preset():
    events = [
        {"type": "agent-preset/selected", "data": {}},  # no agentPreset
        {
            "type": "subagent/descriptor",
            "data": {"version": 3, "mode": "continuable", "provider": "test", "label": "test"},
        },
    ]
    result = cold_resume_from_descriptor("child-1", events)
    assert result.success is False
    assert "preset" in (result.error or "").lower()


# === Apply Composition Test ===


def test_apply_child_composition_with_attrs():
    class FakeChild:
        def __init__(self):
            self.persona = None
            self.tool_filter = None
            self.agent_preset = None

    child = FakeChild()
    apply_child_composition(
        child, None, {"persona": "helper", "toolFilter": {"allow": ["x"]}, "agentPreset": "std"}
    )
    assert child.persona == "helper"
    assert child.tool_filter == {"allow": ["x"]}
    assert child.agent_preset == "std"
