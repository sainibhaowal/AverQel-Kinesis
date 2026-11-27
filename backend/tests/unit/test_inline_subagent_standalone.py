"""Standalone test for true in-process inline subagent backend.

Tests the inline execution path added to delegation.py without requiring
the full conftest dependency chain. Verifies:
- backend="in-process-inline" returns an immediate answer (not "queued")
- child result is recorded in the database
- get_agent can read the result
- events are written to the typed log
- depth limit is enforced
- failures are handled safely
- existing Celery path is unchanged
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.deepspace.models.conversation import Conversation
from app.deepspace.models.subagent import DeepSpaceSubagentRun
from app.deepspace.services.session_events import (
    SUBAGENT_DESCRIPTOR,
    SUBAGENT_RESULT,
    SUBAGENT_SPAWN,
    SessionEventStore,
)
from app.deepspace.services.tools import delegation, dispatch_native_tool
from app.deepspace.services.tools._base import ToolContext
from app.platform.database import model_registry  # noqa: F401
from app.platform.database.base import Base
from app.platform.database.sqlite_compat import sanitize_metadata_for_sqlite
from app.providers.services.types import ChatGenerateResponse

pytestmark = pytest.mark.unit_no_db


class StubProvider:
    """A provider that answers with a scripted sequence of responses."""

    supports_tool_calling = True

    def __init__(self, script: list[dict]) -> None:
        self.script = list(script)
        self.requests: list = []

    def generate(self, request: Any) -> ChatGenerateResponse:
        self.requests.append(replace(request, messages=list(request.messages)))
        payload = self.script.pop(0) if self.script else {"content": "done"}
        return ChatGenerateResponse(
            content=str(payload.get("content") or ""),
            thinking_content=payload.get("thinking_content"),
            tool_calls=payload.get("tool_calls"),
        )


class _FailingProvider:
    supports_tool_calling = True

    def generate(self, request: Any) -> ChatGenerateResponse:
        raise RuntimeError("Provider exploded")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    settings = Settings(
        deepspace_workspace_root=str(tmp_path / "workspaces"),
        deepspace_subagent_max_depth=1,
        deepspace_subagent_max_children=4,
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'inline.db'}")
    sanitize_metadata_for_sqlite(Base.metadata)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    session.add(
        Conversation(
            id=conversation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            kind="deepspace",
            title="Inline test",
        )
    )
    session.commit()

    calls: list = []

    async def executor(name: str, arguments: dict) -> dict:
        calls.append((name, arguments))
        return {"status": "ok", "echo": arguments}

    provider = StubProvider([])
    candidate = SimpleNamespace(
        provider_type="stub",
        model_name="stub-model",
        base_url="http://stub.invalid",
        api_key="stub-key",
    )
    context = ToolContext(
        auth=SimpleNamespace(
            tenant_id=tenant_id,
            user_id=user_id,
            roles=frozenset({"member"}),
            permissions=frozenset({"queries:run"}),
        ),
        conversation_id=conversation_id,
        settings=settings,
        db=session,
        task_store=SimpleNamespace(),
        tool_result_store=SimpleNamespace(),
        provider_registry=SimpleNamespace(get_chat_provider_from_selection=lambda _c: provider),
        provider_selection=SimpleNamespace(
            resolve_chat=lambda **_: SimpleNamespace(candidates=[candidate])
        ),
        native_executor=executor,
    )
    monkeypatch.setattr(delegation, "_dispatch_child_turn", lambda *a, **k: True)
    yield {
        "context": context,
        "session": session,
        "provider": provider,
        "calls": calls,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
    }


def _child_from_result(env: Any, result: dict) -> DeepSpaceSubagentRun:
    child = env["session"].get(DeepSpaceSubagentRun, uuid.UUID(result["agent_id"]))
    assert child is not None
    return child


def test_inline_subagent_returns_answer_immediately(env: Any) -> None:
    """True in-process inline: the child runs and answers in the same call."""
    context = env["context"]
    provider = env["provider"]
    provider.script = [{"content": "The inline child reports 42."}]

    result = asyncio.run(
        dispatch_native_tool(
            "subagent",
            {
                "description": "inline check",
                "task": "Return the answer immediately.",
                "mode": "one-shot",
                "backend": "in-process-inline",
            },
            context,
        )
    )

    assert result is not None
    assert result["status"] == "ok"
    assert result["answer"] == "The inline child reports 42."
    assert result["backend"] == "in-process-inline"
    assert result["rounds"] >= 1
    assert "task_queued" not in result

    child = _child_from_result(env, result)
    assert child.result_text == "The inline child reports 42."
    assert child.stop_reason == "completed"


def test_inline_subagent_runs_tools_and_returns_result(env: Any) -> None:
    """Inline child executes tools via the shared executor and returns tool results."""
    context = env["context"]
    provider = env["provider"]
    calls = env["calls"]
    provider.script = [
        {
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "fs_read", "arguments": '{"path": "child.txt"}'},
                }
            ],
        },
        {"content": "The child read the file and reports 7."},
    ]

    result = asyncio.run(
        dispatch_native_tool(
            "subagent",
            {
                "description": "inline read",
                "task": "Read child.txt and report its content.",
                "mode": "one-shot",
                "backend": "in-process-inline",
            },
            context,
        )
    )

    assert result is not None
    assert result["status"] == "ok"
    assert result["answer"] == "The child read the file and reports 7."
    assert "fs_read" in result["tools_used"]
    assert any(name == "fs_read" for name, _ in calls)


def test_inline_subagent_records_spawn_and_result_events(env: Any) -> None:
    """Inline child writes spawn, descriptor, and result events to the typed log."""
    context = env["context"]
    provider = env["provider"]
    provider.script = [{"content": "done."}]

    asyncio.run(
        dispatch_native_tool(
            "subagent",
            {
                "description": "inline event child",
                "task": "work",
                "mode": "one-shot",
                "backend": "in-process-inline",
            },
            context,
        )
    )

    session = env["session"]
    events = SessionEventStore(session).events(
        tenant_id=env["tenant_id"],
        user_id=env["user_id"],
        conversation_id=env["conversation_id"],
    )
    kinds = [event.event_type for event in events]

    assert SUBAGENT_SPAWN in kinds
    assert SUBAGENT_DESCRIPTOR in kinds
    assert SUBAGENT_RESULT in kinds


def test_inline_subagent_get_agent_reads_result(env: Any) -> None:
    """The inline child's result is readable via get_agent immediately after the call."""
    context = env["context"]
    provider = env["provider"]
    provider.script = [{"content": "Inline result for get_agent."}]

    result = asyncio.run(
        dispatch_native_tool(
            "subagent",
            {
                "description": "inline get_agent",
                "task": "Return a value.",
                "mode": "one-shot",
                "backend": "in-process-inline",
            },
            context,
        )
    )

    agent_id = result["agent_id"]
    snapshot = asyncio.run(
        delegation._get_agent({"agent_id": agent_id, "wait_timeout_seconds": 0}, context)
    )

    assert snapshot["status"] == "ok"
    assert snapshot["agent"]["answer"] == "Inline result for get_agent."
    assert snapshot["agent"]["stop_reason"] == "completed"


def test_inline_subagent_depth_limit_is_enforced(env: Any) -> None:
    """Inline backend still respects the delegation depth limit."""
    context = env["context"]
    deep = ToolContext(
        auth=context.auth,
        conversation_id=context.conversation_id,
        settings=context.settings,
        db=context.db,
        task_store=context.task_store,
        tool_result_store=context.tool_result_store,
        provider_registry=context.provider_registry,
        provider_selection=context.provider_selection,
        native_executor=context.native_executor,
        depth=5,
    )

    result = asyncio.run(
        dispatch_native_tool(
            "subagent",
            {
                "description": "too deep inline",
                "task": "work",
                "mode": "one-shot",
                "backend": "in-process-inline",
            },
            deep,
        )
    )

    assert result is not None
    assert result["status"] == "denied"
    assert result["max_depth"] == 1


def test_inline_subagent_failure_is_returned_safely(env: Any) -> None:
    """Inline child failures return a failed status without raising to the caller."""
    context = env["context"]
    context.provider_registry.get_chat_provider_from_selection = lambda _c: _FailingProvider()

    result = asyncio.run(
        dispatch_native_tool(
            "subagent",
            {
                "description": "inline failing child",
                "task": "This will fail.",
                "mode": "one-shot",
                "backend": "in-process-inline",
            },
            context,
        )
    )

    assert result is not None
    assert result["status"] == "failed"
    assert result["answer"] == ""
    assert result["diagnostic"] is not None


def test_inline_subagent_tool_filters_enforced(env: Any) -> None:
    """Inline backend enforces tool_allow/tool_deny like the queued in-process backend."""
    context = env["context"]
    provider = env["provider"]
    provider.script = [{"content": "done."}]

    result = asyncio.run(
        dispatch_native_tool(
            "subagent",
            {
                "description": "inline restricted",
                "task": "Read a file",
                "mode": "one-shot",
                "backend": "in-process-inline",
                "tool_allow": ["fs_read"],
            },
            context,
        )
    )

    assert result is not None
    assert result["status"] == "failed"
    child = _child_from_result(env, result)
    restriction = child.tool_filter if isinstance(child.tool_filter, dict) else {}
    assert "fs_read" in restriction.get("allow", [])


def test_existing_celery_path_unchanged(env: Any) -> None:
    """Verify the default in-process (Celery) path still returns 'queued'."""
    context = env["context"]
    provider = env["provider"]
    provider.script = [{"content": "done."}]

    result = asyncio.run(
        dispatch_native_tool(
            "subagent",
            {
                "description": "celery path",
                "task": "work",
                "mode": "one-shot",
                "backend": "in-process",
            },
            context,
        )
    )

    assert result is not None
    assert result["status"] == "queued"
    assert result["task_queued"] is True


def test_inline_subagent_runs_parallel_read_tools_concurrently_and_preserves_order(
    env: Any,
) -> None:
    """Verify child subagent executes read-only tool calls in parallel via asyncio.gather."""
    from dataclasses import replace

    provider = env["provider"]

    active = 0
    max_active = 0
    events: list[str] = []

    async def parallel_executor(name: str, arguments: dict) -> dict:
        nonlocal active, max_active
        events.append(f"start:{name}:{arguments.get('path', arguments.get('pattern', ''))}")
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.03)
        active -= 1
        events.append(f"end:{name}:{arguments.get('path', arguments.get('pattern', ''))}")
        return {"status": "ok", "tool": name, "args": arguments}

    context = replace(env["context"], native_executor=parallel_executor)

    provider.script = [
        {
            "content": "",
            "tool_calls": [
                {
                    "id": "tc-1",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path": "a.py"}'},
                },
                {
                    "id": "tc-2",
                    "type": "function",
                    "function": {"name": "glob", "arguments": '{"pattern": "*.py"}'},
                },
                {
                    "id": "tc-3",
                    "type": "function",
                    "function": {"name": "write", "arguments": '{"path": "b.py", "content": "x"}'},
                },
            ],
        },
        {"content": "All parallel reads and barrier write completed."},
    ]

    result = asyncio.run(
        dispatch_native_tool(
            "subagent",
            {
                "description": "parallel child",
                "task": "Read a.py, glob *.py, then write b.py",
                "mode": "one-shot",
                "backend": "in-process-inline",
            },
            context,
        )
    )

    assert result is not None
    assert result["status"] == "ok"
    assert result["answer"] == "All parallel reads and barrier write completed."
    # read + glob overlapped concurrently (max_active == 2), while write waited as barrier.
    assert max_active == 2
    write_start = events.index("start:write:b.py")
    assert events.index("end:read:a.py") < write_start
    assert events.index("end:glob:*.py") < write_start
    # Tool result messages recorded for round 2 stay in exact tool_call_id order.
    second_turn_messages = provider.requests[1].messages
    tool_msgs = [m for m in second_turn_messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["tc-1", "tc-2", "tc-3"]


def test_inline_subagent_wrap_up_budget_and_progress_callback(env: Any) -> None:
    """Verify configurable round budget injects wrap-up notice, skips tools on final round, and emits progress."""
    from dataclasses import replace

    from app.deepspace.services.subagent_runner import (
        ADAPTIVE_CHILD_MAX_ROUNDS,
        MAX_CHILD_ROUNDS,
        _effective_child_max_rounds,
    )

    assert MAX_CHILD_ROUNDS == 64
    adaptive_ctx = replace(
        env["context"],
        settings=env["context"].settings.model_copy(update={"deepspace_subagent_max_rounds": 0}),
    )
    assert _effective_child_max_rounds(adaptive_ctx) == ADAPTIVE_CHILD_MAX_ROUNDS

    progress_updates: list[str] = []
    bounded_settings = env["context"].settings.model_copy(
        update={"deepspace_subagent_max_rounds": 3}
    )
    context = replace(
        env["context"],
        settings=bounded_settings,
        progress_callback=progress_updates.append,
    )
    provider = env["provider"]
    provider.script = [
        {
            "content": "",
            "tool_calls": [
                {
                    "id": "r1",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path": "a.py"}'},
                },
            ],
        },
        {
            "content": "",
            "tool_calls": [
                {
                    "id": "r2",
                    "type": "function",
                    "function": {"name": "glob", "arguments": '{"pattern": "*.py"}'},
                },
            ],
        },
        {"content": "Completed chapter 1 and 2; ready for chapter 3."},
    ]

    result = asyncio.run(
        dispatch_native_tool(
            "subagent",
            {
                "description": "wrap-up check",
                "task": "Work and summarize cleanly",
                "mode": "continuable",
                "backend": "in-process-inline",
            },
            context,
        )
    )

    assert result is not None
    assert result["status"] == "ok"
    assert result["rounds"] == 3
    assert "Completed chapter 1 and 2" in result["answer"]
    # Round 3 omitted tools to guarantee clean final synthesis, and Round 2 injected wrap-up prompt.
    assert provider.requests[2].tools is None
    assert any(
        "Final round budget notice" in str(m.get("content", ""))
        for m in provider.requests[2].messages
    )
    assert any("Round 1: read" in msg for msg in progress_updates)
    child = _child_from_result(env, result)
    assert child.turn_history and len(child.turn_history) == 2


def test_inline_subagent_anti_loop_guard_forces_summary_on_identical_repeats(env: Any) -> None:
    """Verify repeated identical tool calls trigger anti-loop steering and forced final summary."""
    provider = env["provider"]
    repeated_call = {
        "id": "dup-1",
        "type": "function",
        "function": {"name": "read", "arguments": '{"path": "same.py"}'},
    }
    provider.script = [
        {"content": "", "tool_calls": [dict(repeated_call, id=f"dup-{i}")]} for i in range(1, 6)
    ] + [{"content": "Stopped repeating and summarized findings."}]

    result = asyncio.run(
        dispatch_native_tool(
            "subagent",
            {
                "description": "anti-loop child",
                "task": "Inspect same.py",
                "mode": "one-shot",
                "backend": "in-process-inline",
            },
            env["context"],
        )
    )

    assert result is not None
    assert result["status"] == "ok"
    assert result["rounds"] == 6
    assert "Stopped repeating" in result["answer"]
    assert provider.requests[5].tools is None
    assert any(
        "Anti-loop guard" in str(m.get("content", "")) for m in provider.requests[5].messages
    )


def test_inline_subagent_emits_structured_activity_and_records_turn_timeline(env: Any) -> None:
    """Verify inline subagent emits start/tool_start/tool_result/end and records isolated turn timeline."""
    from dataclasses import replace

    from app.deepspace.services.chat_service.turns.tool_calls import _TurnToolCallsMixin
    from app.deepspace.services.chat_service.utils import _TurnState

    provider = env["provider"]
    provider.script = [
        {
            "content": "",
            "tool_calls": [
                {
                    "id": "call-sub-read-1",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path": "notes.md"}'},
                }
            ],
        },
        {"content": "Verified notes.md contents."},
    ]
    activity_events: list[dict[str, Any]] = []
    context = replace(env["context"], activity_callback=activity_events.append)

    result = asyncio.run(
        dispatch_native_tool(
            "subagent",
            {
                "description": "Docs checker",
                "task": "Read notes.md and report",
                "mode": "one-shot",
                "backend": "in-process-inline",
            },
            context,
        )
    )

    assert result is not None
    assert result["status"] == "ok"
    kinds = [ev.get("kind") for ev in activity_events]
    assert kinds == ["start", "tool_start", "tool_result", "end"]
    assert activity_events[1]["tool_name"] == "read"
    assert activity_events[2]["status"] == "completed"
    assert activity_events[3]["rounds"] == 2

    state = _TurnState(
        auth=env["context"].auth,
        conversation_id=env["context"].conversation_id,
        prompt="Delegate",
        run_id=None,
    )
    for ev in activity_events:
        enriched = dict(ev)
        enriched["parent_tool_id"] = "parent-call-1"
        _TurnToolCallsMixin._record_subagent_activity(state, enriched)

    sub_id = str(activity_events[0]["subagent_id"])
    recorded = state.subagent_timelines[sub_id]
    assert recorded["label"] == "Docs checker"
    assert recorded["parentToolId"] == "parent-call-1"
    assert recorded["status"] == "completed"
    assert recorded["rounds"] == 2
    assert recorded["toolCount"] == 1
    assert len(recorded["steps"]) == 1
    assert recorded["steps"][0]["toolName"] == "read"
    assert recorded["steps"][0]["status"] == "completed"
    assert "Verified notes.md contents." in str(recorded["summary"])


def test_inline_subagent_aliases_workspace_bridge_tool_allow_names(env: Any) -> None:
    """Verify averqel__fs_* tool_allow entries map onto native child tools cleanly."""
    provider = env["provider"]
    provider.script = [
        {
            "content": "",
            "tool_calls": [
                {
                    "id": "alias-glob-1",
                    "type": "function",
                    "function": {"name": "glob", "arguments": '{"pattern": "*.py"}'},
                }
            ],
        },
        {"content": "Alias mapping worked cleanly."},
    ]

    result = asyncio.run(
        dispatch_native_tool(
            "subagent",
            {
                "description": "Alias Scout",
                "task": "Use glob to inspect Python files",
                "mode": "one-shot",
                "backend": "in-process-inline",
                "tool_allow": ["averqel__fs_glob", "averqel__fs_grep", "averqel__fs_read"],
            },
            env["context"],
        )
    )

    assert result is not None
    assert result["status"] == "ok"
    assert "Alias mapping worked cleanly." in result["answer"]
    offered_names = {
        str(defn["function"]["name"])
        for defn in (provider.requests[0].tools or [])
        if isinstance(defn, dict) and isinstance(defn.get("function"), dict)
    }
    assert {"glob", "grep", "read"}.issubset(offered_names)


def test_queued_subagent_emits_immediate_start_and_restores_across_resume(env: Any) -> None:
    """Verify queued subagents emit immediate start tabs, normalize naive UTC timestamps, and restore across resumes."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.deepspace.services.chat_service.turns.tool_calls import _TurnToolCallsMixin
    from app.deepspace.services.chat_service.utils import _TurnState

    assert _TurnToolCallsMixin._utc_iso(datetime(2026, 11, 25, 15, 25, 16)) == (
        "2026-11-25T15:25:16+00:00"
    )
    aware_iso = _TurnToolCallsMixin._utc_iso(datetime(2026, 11, 25, 15, 25, 16, tzinfo=UTC))
    assert aware_iso == "2026-11-25T15:25:16+00:00"

    state = _TurnState(
        auth=env["context"].auth,
        conversation_id=env["context"].conversation_id,
        prompt="Run parallel inspectors",
        run_id=None,
    )
    mixin = _TurnToolCallsMixin()
    frames = mixin._emit_queued_subagent_start(
        state,
        tool_name="subagent",
        arguments={
            "description": "Backend Inspector",
            "task": "Inspect backend/",
            "mode": "one-shot",
        },
        payload={
            "status": "queued",
            "agent_id": "child-bg-1",
            "mode": "one-shot",
            "depth": 1,
            "label": "Backend Inspector",
        },
        turn_index=2,
    )
    assert len(frames) == 1
    assert "event: subagent_activity" in frames[0]
    assert "Backend Inspector" in frames[0]
    assert "child-bg-1" in state.bg_subagent_started
    assert state.subagent_timelines["child-bg-1"]["status"] == "running"

    # Duplicate emission for the same queued child is suppressed.
    assert (
        mixin._emit_queued_subagent_start(
            state,
            tool_name="subagent",
            arguments={"description": "Backend Inspector", "task": "Inspect backend/"},
            payload={"status": "queued", "agent_id": "child-bg-1"},
            turn_index=2,
        )
        == []
    )

    # Restoring onto a resumed turn state keeps earlier subagent tabs and tool sequence cursors.
    saved = list(state.subagent_timelines.values())
    saved[0]["steps"] = [
        {
            "id": "child_child-bg-1_seq_14",
            "stepId": "child_child-bg-1_seq_14",
            "status": "completed",
        }
    ]
    resumed_state = _TurnState(
        auth=env["context"].auth,
        conversation_id=env["context"].conversation_id,
        prompt="",
        run_id=None,
    )
    resumed_state.assistant_message = SimpleNamespace(metadata_json={"subagent_timelines": saved})
    _TurnToolCallsMixin._restore_subagent_timelines(resumed_state)
    assert "child-bg-1" in resumed_state.subagent_timelines
    assert "child-bg-1" in resumed_state.bg_subagent_started
    assert "child-bg-1" not in resumed_state.bg_subagent_ended
    assert resumed_state.bg_subagent_event_seq["child-bg-1"] == 14


def test_inline_subagent_aliases_web_tools_and_turn_state_slots(env: Any) -> None:
    """Verify averqel__web_* aliases expand onto native child tools and _TurnState exposes slotted poll state."""
    from app.deepspace.services.chat_service.utils import _TurnState
    from app.deepspace.services.subagent_runner import _normalize_child_tool_names

    state = _TurnState(
        auth=env["context"].auth,
        conversation_id=env["context"].conversation_id,
        prompt="Test",
        run_id=None,
    )
    assert state.last_round_bg_poll == 0.0
    state.last_round_bg_poll = 42.5
    assert state.last_round_bg_poll == 42.5

    expanded = _normalize_child_tool_names(
        ["averqel__web_search", "averqel__web_fetch", "averqel__fs_str_replace"]
    )
    assert {"web_search", "web_fetch", "edit"}.issubset(expanded)


def test_native_plugin_wrappers_do_not_force_critical_approval_for_subagents() -> None:
    """Verify native.* pseudo-plugins do not enter custom _plugin_modes and trigger critical prompts in Full Access."""
    from pathlib import Path

    from app.deepspace.services.plugins import plugin_registry
    from app.deepspace.services.runtime_policy import DeepSpaceToolPolicy

    custom_plugin_modes = {
        tool.name: tool.mode
        for manifest in plugin_registry().all()
        if manifest.native_tool is None
        for tool in manifest.tools
    }
    policy = DeepSpaceToolPolicy(
        permission_mode="danger-full-access",
        approval_mode="never",
    )
    policy.set_plugin_rules(modes=custom_plugin_modes)
    policy.set_workspace_root(Path("/tmp/workspace"))

    # Routine subagent delegation and workspace writes run without human interruption.
    sub_dec = policy.before_tool("subagent", {"description": "Scout", "task": "Inspect backend"})
    assert sub_dec.allowed is True
    assert sub_dec.requires_approval is False
    assert sub_dec.risk_level != "critical"

    write_dec = policy.before_tool(
        "write", {"target": "workspace", "path": "report.md", "content": "ok"}
    )
    assert write_dec.allowed is True
    assert write_dec.requires_approval is False

    # Destructive actions still keep mandatory human confirmation guardrails.
    del_dec = policy.before_tool("fs_delete", {"path": "report.md"})
    assert del_dec.requires_approval is True
    assert del_dec.risk_level == "critical"

    bash_dec = policy.before_tool("bash", {"command": "rm -rf /tmp/something"})
    assert bash_dec.requires_approval is True
    assert bash_dec.risk_level == "critical"


def test_subagent_activity_merges_call_id_and_preserves_web_search_tools(env: Any) -> None:
    """Verify child tool_start/tool_result merge cleanly without duplicate steps and web_search stays enabled when web options exist."""
    from dataclasses import replace

    from app.deepspace.services.chat_service.turns.tool_calls import _TurnToolCallsMixin
    from app.deepspace.services.chat_service.utils import _TurnState

    state = _TurnState(
        auth=env["context"].auth,
        conversation_id=env["context"].conversation_id,
        prompt="Run search",
        run_id=None,
    )
    _TurnToolCallsMixin._record_subagent_activity(
        state,
        {
            "kind": "start",
            "subagent_id": "sub-merge-1",
            "label": "Searcher",
            "mode": "one-shot",
            "started_at": "2026-11-25T16:10:16+00:00",
        },
    )
    _TurnToolCallsMixin._record_subagent_activity(
        state,
        {
            "kind": "tool_start",
            "subagent_id": "sub-merge-1",
            "seq": 10,
            "step_id": "child_sub-merge-1_call_c1",
            "tool_id": "c1",
            "tool_name": "web_fetch",
            "tool_input": {"url": "https://example.com"},
            "started_at": "2026-11-25T16:10:17+00:00",
        },
    )
    # Simulate an approval/resume gap where tool_result arrives with a fallback step_id:
    _TurnToolCallsMixin._record_subagent_activity(
        state,
        {
            "kind": "tool_result",
            "subagent_id": "sub-merge-1",
            "seq": 11,
            "step_id": "child_sub-merge-1_seq_11",
            "tool_id": "child_sub-merge-1_seq_11",
            "tool_name": "web_fetch",
            "status": "completed",
            "output_preview": "ok",
            "completed_at": "2026-11-25T16:10:18+00:00",
        },
    )
    timeline = state.subagent_timelines["sub-merge-1"]
    assert len(timeline["steps"]) == 1
    assert timeline["toolCount"] == 1
    assert timeline["steps"][0]["title"] == "Ran web_fetch"
    assert timeline["steps"][0]["status"] == "completed"
    assert timeline["steps"][0]["toolInput"] == {"url": "https://example.com"}

    # When web_search_options is populated, a child with tool_allow=['web_search'] retains web_search.
    provider = env["provider"]
    provider.script = [
        {
            "content": "",
            "tool_calls": [
                {
                    "id": "ws-call-1",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": '{"query": "AI news"}'},
                }
            ],
        },
        {"content": "Ready to search."},
    ]
    web_ctx = replace(env["context"], web_search_options={"searxng": (object(), object())})
    outcome = asyncio.run(
        dispatch_native_tool(
            "subagent",
            {
                "description": "Web Scout",
                "task": "Search the web for news",
                "mode": "one-shot",
                "backend": "in-process-inline",
                "tool_allow": ["web_search"],
            },
            web_ctx,
        )
    )
    assert outcome is not None
    assert outcome["status"] == "ok", outcome
    offered = {
        str(d["function"]["name"])
        for d in (provider.requests[-1].tools or [])
        if isinstance(d, dict) and isinstance(d.get("function"), dict)
    }
    assert "web_search" in offered


def test_subagent_inherits_reasoning_and_records_thinking_and_model_message(env: Any) -> None:
    provider = env["provider"]
    provider.script = [
        {
            "content": "Checking workspace version file before answering.",
            "thinking_content": "I should read pyproject.toml to verify the package version.",
            "tool_calls": [
                {
                    "id": "call-read-1",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path": "pyproject.toml"}'},
                }
            ],
        },
        {
            "content": "Version is 1.0.0.",
            "thinking_content": "The file showed version 1.0.0, ready to report.",
        },
    ]
    recorded_events: list[dict[str, object]] = []
    ctx = replace(
        env["context"],
        thinking_enabled=True,
        reasoning_effort="high",
        activity_callback=lambda payload: recorded_events.append(dict(payload)),
    )
    outcome = asyncio.run(
        dispatch_native_tool(
            "subagent",
            {
                "description": "Inspector",
                "task": "Read pyproject.toml and summarize",
                "mode": "one-shot",
                "backend": "in-process-inline",
            },
            ctx,
        )
    )
    assert outcome["status"] == "ok", outcome
    assert provider.requests[0].reasoning_enabled is True
    assert provider.requests[0].reasoning_effort == "high"

    kinds = [ev.get("kind") for ev in recorded_events]
    assert "thinking" in kinds
    assert "model_message" in kinds
    assert kinds.index("thinking") < kinds.index("model_message") < kinds.index("tool_start")

    from app.deepspace.services.chat_service.turns.tool_calls import _TurnToolCallsMixin
    from app.deepspace.services.chat_service.utils import _TurnState

    state = _TurnState(
        auth=ctx.auth,
        conversation_id=ctx.conversation_id,
        prompt="Inspect project",
        run_id=None,
    )
    for ev in recorded_events:
        _TurnToolCallsMixin._record_subagent_activity(state, ev)
    tl = next(iter(state.subagent_timelines.values()))
    step_types = [step["type"] for step in tl["steps"]]
    assert "thinking" in step_types
    assert "model_message" in step_types
    assert tl["toolCount"] == 1
    thinking_step = next(step for step in tl["steps"] if step["type"] == "thinking")
    assert thinking_step["title"] == "Internal Thought"
    assert "read pyproject.toml" in thinking_step["details"]
    msg_step = next(step for step in tl["steps"] if step["type"] == "model_message")
    assert msg_step["title"] == "Model message"
    assert "Checking workspace version file" in msg_step["details"]

    # Verify streaming child provider turns capture thinking & provider_continuation too.
    from app.deepspace.services.subagent_runner import _stream_child_provider_turn
    from app.providers.services.types import ChatGenerateRequest

    class _StreamingProvider:
        async def stream_generate_events(self, _req: Any) -> Any:
            yield {"type": "thinking", "text": "Streamed thought "}
            yield {"type": "thinking", "text": "on round 1."}
            yield {"type": "provider_continuation", "reasoning_content": "opaque-token"}
            yield {"type": "delta", "text": "Calling tool now."}
            yield {
                "type": "tool_calls_delta",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "sc-1",
                        "type": "function",
                        "function": {"name": "read", "arguments": '{"path": "README.md"}'},
                    }
                ],
            }

    async def _no_poll() -> list[dict[str, Any]]:
        return []

    stream_resp, steered, continuation = asyncio.run(
        _stream_child_provider_turn(
            _StreamingProvider(),
            ChatGenerateRequest(
                model="stub",
                messages=[],
                temperature=0.2,
                max_tokens=None,
                base_url="",
            ),
            poll_messages=_no_poll,
            cancel_check=None,
        )
    )
    assert steered == []
    assert continuation == "opaque-token"
    assert stream_resp is not None
    assert stream_resp.thinking_content == "Streamed thought on round 1."
    assert stream_resp.content == "Calling tool now."
    assert stream_resp.tool_calls and len(stream_resp.tool_calls) == 1

    # Verify background TOOL_RESULT session event persists output_preview for live UI tool outputs.
    from sqlalchemy import select

    from app.deepspace.models.session_event import DeepSpaceSessionEvent
    from app.deepspace.services.session_events import SUBAGENT_THINKING, TOOL_RESULT

    ev_rows = list(
        env["session"]
        .execute(
            select(DeepSpaceSessionEvent)
            .where(DeepSpaceSessionEvent.conversation_id == ctx.conversation_id)
            .order_by(DeepSpaceSessionEvent.seq.asc())
        )
        .scalars()
        .all()
    )
    assert any(ev.event_type == SUBAGENT_THINKING for ev in ev_rows)
    tool_result_events = [ev for ev in ev_rows if ev.event_type == TOOL_RESULT]
    assert tool_result_events
    assert any(
        isinstance(ev.payload, dict) and str(ev.payload.get("output_preview") or "").strip()
        for ev in tool_result_events
    )

    # Verify WriteArguments defaults to library when filename is given and rejects content-only with actionable guidance.
    from pydantic import ValidationError

    from app.deepspace.services.tools.unified import WriteArguments

    inferred = WriteArguments(filename="case_study.md", content="Findings")
    assert inferred.target == "library"

    with pytest.raises(ValidationError) as exc_info:
        WriteArguments(content="Draft only")
    assert "Do not call write merely to reply in chat" in str(exc_info.value)
