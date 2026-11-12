"""Scripted end-to-end tests for the DeepSpace delegated child loop.

The provider is stubbed and the child is run directly against a self-contained
SQLite database. Background dispatch and its concurrency are covered separately.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.deepspace.models.conversation import Conversation
from app.deepspace.models.session_event import DeepSpaceSessionEvent
from app.deepspace.models.subagent import DeepSpaceSubagentRun
from app.deepspace.models.workspace_file import DeepSpaceWorkspaceFile
from app.deepspace.services.session_events import (
    SUBAGENT_DESCRIPTOR,
    SUBAGENT_END,
    SUBAGENT_PRESET,
    SUBAGENT_RESULT,
    SUBAGENT_SPAWN,
    SUBAGENT_START,
    SessionEventStore,
)
from app.deepspace.services.subagent_runner import resolve_child_candidate, run_child_turn
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

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.requests: list[Any] = []

    def generate(self, request: Any) -> ChatGenerateResponse:
        self.requests.append(replace(request, messages=copy.deepcopy(request.messages)))
        payload = self.script.pop(0) if self.script else {"content": "done"}
        return ChatGenerateResponse(
            content=str(payload.get("content") or ""),
            tool_calls=payload.get("tool_calls"),
        )


@pytest.fixture
def delegation_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Build a self-contained database, context, and provider for a delegation."""
    settings = Settings(
        deepspace_workspace_root=str(tmp_path / "workspaces"),
        deepspace_subagent_max_depth=1,
        deepspace_subagent_max_children=4,
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'delegation.db'}")
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
            title="Delegation test",
        )
    )
    session.commit()

    calls: list[tuple[str, dict[str, Any]]] = []

    async def executor(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
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
        provider_registry=SimpleNamespace(
            get_chat_provider_from_selection=lambda _candidate: provider
        ),
        provider_selection=SimpleNamespace(
            resolve_chat=lambda **_: SimpleNamespace(candidates=[candidate])
        ),
        native_executor=executor,
    )
    monkeypatch.setattr(delegation, "_dispatch_child_turn", lambda *_args, **_kwargs: True)
    yield {
        "context": context,
        "session": session,
        "provider": provider,
        "calls": calls,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
    }


def _child_from_result(environment: Any, result: dict[str, Any]) -> DeepSpaceSubagentRun:
    child = environment["session"].get(DeepSpaceSubagentRun, uuid.UUID(result["agent_id"]))
    assert child is not None
    return child


def _run_child_in_test(
    environment: Any,
    child: DeepSpaceSubagentRun,
    task: str,
) -> Any:
    outcome = asyncio.run(run_child_turn(context=environment["context"], child=child, task=task))
    child.result_text = outcome.text
    child.stop_reason = outcome.stop_reason
    child.status = "idle" if outcome.stop_reason == "completed" else outcome.stop_reason
    child.turn_count = int(child.turn_count or 0) + 1
    environment["session"].commit()
    return outcome


def test_a_delegated_child_runs_its_own_tool_calls(delegation_environment: Any) -> None:
    context = delegation_environment["context"]
    provider = delegation_environment["provider"]
    calls = delegation_environment["calls"]
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
                "description": "read the child file",
                "task": "Read child.txt and report its content.",
                "mode": "continuable",
            },
            context,
        )
    )

    assert result is not None
    assert result["status"] == "queued"
    child = _child_from_result(delegation_environment, result)
    outcome = _run_child_in_test(
        delegation_environment, child, "Read child.txt and report its content."
    )
    assert "reports 7" in outcome.text
    assert any(name == "fs_read" for name, _ in calls)
    offered = {tool["function"]["name"] for tool in (provider.requests[0].tools or [])}
    assert "fs_read" in offered
    assert "subagent" not in offered
    assert "subagent_fork" not in offered


@pytest.mark.asyncio
async def test_get_agent_can_wait_once_for_a_queued_child(
    delegation_environment: Any, monkeypatch: Any
) -> None:
    context = delegation_environment["context"]
    agent_id = str(uuid.uuid4())
    snapshots = iter(
        [
            {"agent_id": agent_id, "status": "running", "answer": ""},
            {"agent_id": agent_id, "status": "completed", "answer": "GPU check finished."},
        ]
    )
    monkeypatch.setattr(delegation, "_load_child_snapshot", lambda *_args: next(snapshots))

    result = await delegation._get_agent(
        {"agent_id": agent_id, "wait_timeout_seconds": 5},
        context,
    )

    assert result["status"] == "ok"
    assert result["agent"]["status"] == "completed"
    assert result["agent"]["answer"] == "GPU check finished."
    assert result["wait_timed_out"] is False


@pytest.mark.asyncio
async def test_get_agent_returns_running_status_when_bounded_wait_times_out(
    delegation_environment: Any,
    monkeypatch: Any,
) -> None:
    context = delegation_environment["context"]
    agent_id = str(uuid.uuid4())
    monkeypatch.setattr(
        delegation,
        "_load_child_snapshot",
        lambda *_args: {"agent_id": agent_id, "status": "running", "answer": ""},
    )

    result = await delegation._get_agent(
        {"agent_id": agent_id, "wait_timeout_seconds": 1},
        context,
    )

    assert result["status"] == "ok"
    assert result["agent"]["status"] == "running"
    assert result["wait_timed_out"] is True


def test_a_live_child_task_retries_once_until_it_calls_a_tool(
    delegation_environment: Any,
) -> None:
    context = delegation_environment["context"]
    provider = delegation_environment["provider"]
    calls = delegation_environment["calls"]
    provider.script = [
        {"content": "The file says 7."},
        {
            "content": "",
            "tool_calls": [
                {
                    "id": "call-read",
                    "type": "function",
                    "function": {
                        "name": "fs_read",
                        "arguments": '{"path":"child.txt"}',
                    },
                }
            ],
        },
        {"content": "The tool returned the file content."},
    ]

    result = asyncio.run(
        dispatch_native_tool(
            "subagent",
            {
                "description": "verify the child file",
                "task": "Read child.txt and report its content.",
                "mode": "one-shot",
            },
            context,
        )
    )

    assert result is not None and result["status"] == "queued"
    outcome = _run_child_in_test(
        delegation_environment,
        _child_from_result(delegation_environment, result),
        "Read child.txt and report its content.",
    )
    assert outcome.called_tools == ["fs_read"]
    assert calls == [("fs_read", {"path": "child.txt"})]
    assert len(provider.requests) == 3
    assert provider.requests[0].tool_choice == "required"
    assert provider.requests[1].tool_choice == "required"
    assert provider.requests[1].messages[-1]["role"] == "user"
    assert "actual structured tool call" in provider.requests[1].messages[-1]["content"]


def test_worker_cold_resume_restores_preset_from_scoped_persisted_events(
    delegation_environment: Any,
) -> None:
    from app.deepspace.workers.subagent_tasks import _restore_continuable_child

    context = delegation_environment["context"]
    session = delegation_environment["session"]
    child = DeepSpaceSubagentRun(
        id=uuid.uuid4(),
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        conversation_id=context.conversation_id,
        mode="continuable",
        status="idle",
        descriptor={
            "version": 3,
            "mode": "continuable",
            "provider": "deepspace",
            "label": "restored child",
        },
    )
    session.add(child)
    store = SessionEventStore(session)
    store.append(
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        conversation_id=context.conversation_id,
        event_type=SUBAGENT_PRESET,
        payload={"agentPreset": "minimal"},
        subagent_id=child.id,
        tool_name="subagent",
    )
    store.append(
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        conversation_id=context.conversation_id,
        event_type=SUBAGENT_DESCRIPTOR,
        payload=dict(child.descriptor),
        subagent_id=child.id,
        tool_name="subagent",
    )
    session.commit()

    _restore_continuable_child(session, child)

    assert child.descriptor["agentPreset"] == "minimal"


def test_worker_cold_resume_ignores_cross_tenant_preset_events(
    delegation_environment: Any,
) -> None:
    from app.deepspace.workers.subagent_tasks import _restore_continuable_child

    context = delegation_environment["context"]
    session = delegation_environment["session"]
    child = DeepSpaceSubagentRun(
        id=uuid.uuid4(),
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        conversation_id=context.conversation_id,
        mode="continuable",
        status="idle",
        descriptor={
            "version": 3,
            "mode": "continuable",
            "provider": "deepspace",
            "label": "scoped child",
        },
    )
    session.add(child)
    session.add(
        DeepSpaceSessionEvent(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            subagent_id=child.id,
            seq=1,
            event_type=SUBAGENT_PRESET,
            payload={"agentPreset": "must-not-cross-tenant"},
        )
    )
    session.commit()

    _restore_continuable_child(session, child)

    assert "agentPreset" not in child.descriptor


def test_a_child_receives_the_scoped_mcp_broker_when_the_parent_allows_it(
    delegation_environment: Any,
) -> None:
    from app.deepspace.services.mcp_tool_broker import MCP_BROKER_TOOL

    context = delegation_environment["context"]
    provider = delegation_environment["provider"]
    context = replace(context, mcp_bindings={"mcp_example_lookup": object()})
    delegation_environment["context"] = context
    provider.script = [
        {
            "content": "",
            "tool_calls": [
                {
                    "id": "call-mcp",
                    "type": "function",
                    "function": {
                        "name": MCP_BROKER_TOOL,
                        "arguments": '{"operation":"search","query":"calendar events"}',
                    },
                }
            ],
        },
        {"content": "The MCP tool catalog returned a calendar tool."},
    ]

    result = asyncio.run(
        dispatch_native_tool(
            "subagent",
            {
                "description": "inspect available connected tools",
                "task": "Search the connected MCP tools for a calendar event lookup tool.",
                "mode": "one-shot",
                "tool_allow": [MCP_BROKER_TOOL],
            },
            context,
        )
    )

    assert result is not None and result["status"] == "queued"
    outcome = _run_child_in_test(
        delegation_environment,
        _child_from_result(delegation_environment, result),
        "Search the connected MCP tools for a calendar event lookup tool.",
    )
    assert "catalog returned a calendar tool" in outcome.text
    assert provider.requests[0].tool_choice == "required"
    offered = {tool["function"]["name"] for tool in (provider.requests[0].tools or [])}
    assert MCP_BROKER_TOOL in offered
    assert delegation_environment["calls"] == [
        (MCP_BROKER_TOOL, {"operation": "search", "query": "calendar events"})
    ]


def test_live_child_task_skips_a_model_known_not_to_support_tools() -> None:
    first = SimpleNamespace(
        provider_type="openai",
        model_name="no-tools",
        provider_config_id=uuid.uuid4(),
    )
    second = SimpleNamespace(
        provider_type="openai",
        model_name="tool-capable",
        provider_config_id=uuid.uuid4(),
    )

    class ModelCache:
        @staticmethod
        def get_model(*, model_name: str, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                capabilities_json={"supports_tools": model_name == "tool-capable"}
            )

    context = SimpleNamespace(
        auth=SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4()),
        provider_selection=SimpleNamespace(
            model_cache=ModelCache(),
            resolve_chat=lambda **_: SimpleNamespace(candidates=[first, second]),
        ),
    )

    selected = resolve_child_candidate(
        context,
        SimpleNamespace(agent_provider=None, agent_model=None),
        requires_tools=True,
    )

    assert selected is second


def test_a_delegated_child_is_durable(delegation_environment: Any) -> None:
    context = delegation_environment["context"]
    result = asyncio.run(
        dispatch_native_tool(
            "subagent",
            {
                "description": "read the child file",
                "task": "Read child.txt and report its content.",
                "mode": "continuable",
            },
            context,
        )
    )
    session = delegation_environment["session"]
    children = (
        session.query(DeepSpaceSubagentRun)
        .filter(DeepSpaceSubagentRun.conversation_id == delegation_environment["conversation_id"])
        .all()
    )

    assert len(children) == 1
    child = children[0]
    assert child.mode == "continuable"
    assert child.label == "read the child file"
    assert result is not None and result["status"] == "queued"
    assert child.status == "queued"
    assert "Read child.txt" in str(child.pending_messages[0]["message"])
    assert child.result_text is None
    assert child.descriptor["version"] == 3
    assert child.descriptor["mode"] == "continuable"


def test_a_delegation_reaches_the_typed_log(delegation_environment: Any) -> None:
    context = delegation_environment["context"]
    provider = delegation_environment["provider"]
    provider.script = [{"content": "done"}]

    asyncio.run(dispatch_native_tool("subagent", {"description": "child", "task": "work"}, context))
    events = SessionEventStore(delegation_environment["session"]).events(
        tenant_id=delegation_environment["tenant_id"],
        user_id=delegation_environment["user_id"],
        conversation_id=delegation_environment["conversation_id"],
    )
    kinds = [event.event_type for event in events]

    assert SUBAGENT_SPAWN in kinds
    assert SUBAGENT_DESCRIPTOR in kinds


def test_external_subagent_runs_one_shot_and_persists_activation_edges(
    delegation_environment: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.deepspace.services import subagent_providers

    class CapturingProvider:
        name = "codex"

        def __init__(self) -> None:
            self.spec = None

        async def run(self, spec, _cancel_check):
            self.spec = spec
            return subagent_providers.SubagentResult(
                status="completed",
                answer="External child finished.",
                tool_calls=[{"name": "command_execution"}],
            )

    provider = CapturingProvider()
    monkeypatch.setitem(subagent_providers._PROVIDERS, "codex", provider)
    context = delegation_environment["context"]

    result = asyncio.run(
        dispatch_native_tool(
            "subagent",
            {
                "description": "external check",
                "task": "Check the workspace",
                "persona": "Report only verified findings",
                "model": "codex-test-model",
                "mode": "one-shot",
                "backend": "codex",
            },
            context,
        )
    )

    assert result is not None
    assert result["status"] == "ok"
    assert result["answer"] == "External child finished."
    assert result["tools_used"] == ["command_execution"]
    assert provider.spec is not None
    assert provider.spec.task == "Check the workspace"
    assert provider.spec.persona == "Report only verified findings"
    assert provider.spec.model == "codex-test-model"
    child = _child_from_result(delegation_environment, result)
    assert child.status == "idle"
    assert child.result_text == "External child finished."

    events = SessionEventStore(delegation_environment["session"]).events(
        tenant_id=delegation_environment["tenant_id"],
        user_id=delegation_environment["user_id"],
        conversation_id=delegation_environment["conversation_id"],
    )
    child_events = [event for event in events if event.subagent_id == child.id]
    kinds = [event.event_type for event in child_events]
    assert SUBAGENT_SPAWN in kinds
    assert SUBAGENT_START in kinds
    assert SUBAGENT_END in kinds
    assert SUBAGENT_RESULT in kinds


def test_external_subagent_fails_closed_on_unenforceable_tool_filter(
    delegation_environment: Any,
) -> None:
    result = asyncio.run(
        dispatch_native_tool(
            "subagent",
            {
                "description": "restricted external check",
                "task": "Read a file",
                "mode": "one-shot",
                "backend": "codex",
                "tool_allow": ["fs_read"],
            },
            delegation_environment["context"],
        )
    )

    assert result is not None
    assert result["status"] == "denied"
    assert "cannot enforce" in result["message"]
    assert delegation_environment["session"].query(DeepSpaceSubagentRun).count() == 0


def test_external_subagent_refuses_continuable_mode_before_admission(
    delegation_environment: Any,
) -> None:
    result = asyncio.run(
        dispatch_native_tool(
            "subagent",
            {
                "description": "continuable external check",
                "task": "Continue after this turn",
                "mode": "continuable",
                "backend": "codex",
            },
            delegation_environment["context"],
        )
    )

    assert result is not None
    assert result["status"] == "denied"
    assert "one-shot runs only" in result["message"]
    assert delegation_environment["session"].query(DeepSpaceSubagentRun).count() == 0


def test_the_control_tools_address_a_child(delegation_environment: Any) -> None:
    context = delegation_environment["context"]
    provider = delegation_environment["provider"]
    provider.script = [{"content": "done"}]
    asyncio.run(
        dispatch_native_tool(
            "subagent",
            {"description": "child", "task": "work", "mode": "continuable"},
            context,
        )
    )
    session = delegation_environment["session"]
    child_id = str(
        session.query(DeepSpaceSubagentRun)
        .filter(DeepSpaceSubagentRun.conversation_id == delegation_environment["conversation_id"])
        .one()
        .id
    )

    listed = asyncio.run(dispatch_native_tool("list_agents", {}, context))
    sent = asyncio.run(
        dispatch_native_tool(
            "send_message", {"agent_id": child_id, "message": "one more thing"}, context
        )
    )
    interrupted = asyncio.run(
        dispatch_native_tool("interrupt_agent", {"agent_id": child_id}, context)
    )

    assert listed is not None and listed["agent_count"] == 1
    assert sent is not None and sent["status"] == "delivered"
    assert interrupted is not None and interrupted["status"] == "accepted"


def test_a_delegation_past_the_depth_limit_is_refused(delegation_environment: Any) -> None:
    context = delegation_environment["context"]
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
        dispatch_native_tool("subagent", {"description": "too deep", "task": "work"}, deep)
    )

    assert result is not None
    assert result["status"] == "denied"
    assert result["max_depth"] == 1


def test_a_continuable_child_keeps_its_own_history(delegation_environment: Any) -> None:
    context = delegation_environment["context"]
    provider = delegation_environment["provider"]
    provider.script = [
        {
            "content": "",
            "tool_calls": [
                {
                    "id": "call-read",
                    "type": "function",
                    "function": {
                        "name": "fs_read",
                        "arguments": '{"path":"child.txt"}',
                    },
                }
            ],
        },
        {"content": "The child reports 7."},
    ]

    spawn = asyncio.run(
        dispatch_native_tool(
            "subagent",
            {
                "description": "read the child file",
                "task": "Read child.txt and report its content.",
                "mode": "continuable",
            },
            context,
        )
    )
    session = delegation_environment["session"]
    child = (
        session.query(DeepSpaceSubagentRun)
        .filter(DeepSpaceSubagentRun.conversation_id == delegation_environment["conversation_id"])
        .one()
    )
    assert spawn is not None and spawn["status"] == "queued"
    _run_child_in_test(
        delegation_environment,
        child,
        "Read child.txt and report its content.",
    )

    assert len(child.turn_history) == 2
    assert child.turn_history[0]["role"] == "user"
    assert "Read child.txt" in str(child.turn_history[0]["content"])
    assert child.turn_history[1]["role"] == "assistant"
    assert "reports 7" in str(child.turn_history[1]["content"])


def test_a_follow_up_turn_reads_the_child_own_history(delegation_environment: Any) -> None:
    from app.deepspace.services.subagent_runner import run_child_turn

    context = delegation_environment["context"]
    provider = delegation_environment["provider"]
    provider.script = [{"content": "The first answer."}]

    asyncio.run(
        dispatch_native_tool(
            "subagent",
            {"description": "child", "task": "The first task.", "mode": "continuable"},
            context,
        )
    )
    session = delegation_environment["session"]
    child = (
        session.query(DeepSpaceSubagentRun)
        .filter(DeepSpaceSubagentRun.conversation_id == delegation_environment["conversation_id"])
        .one()
    )
    _run_child_in_test(delegation_environment, child, "The first task.")
    provider.script = [{"content": "The second answer."}]

    asyncio.run(run_child_turn(context=context, child=child, task="The follow-up task."))

    sent_messages = provider.requests[-1].messages
    roles = [str(message.get("role")) for message in sent_messages]
    assert "user" in roles
    assert "assistant" in roles
    assert any(
        "The first answer." in str(message.get("content") or "") for message in sent_messages
    )
    assert any(
        "The follow-up task." in str(message.get("content") or "") for message in sent_messages
    )


def test_child_turn_passes_images_and_picks_up_live_inbox_messages(
    delegation_environment: Any,
) -> None:
    environment = delegation_environment
    context = environment["context"]
    provider = environment["provider"]
    provider.script = [
        {
            "content": "",
            "tool_calls": [
                {
                    "id": "call-steer",
                    "type": "function",
                    "function": {"name": "fs_read", "arguments": '{"path":"child.txt"}'},
                }
            ],
        },
        {"content": "I used the image and the follow-up instruction."},
    ]
    result = asyncio.run(
        dispatch_native_tool(
            "subagent",
            {"description": "inspect a chart", "task": "Read child.txt.", "mode": "continuable"},
            context,
        )
    )
    child = _child_from_result(environment, result)
    poll_count = 0
    steer_image = "data:image/png;base64,aGVsbG8="

    async def poll_messages() -> list[dict[str, Any]]:
        nonlocal poll_count
        poll_count += 1
        if poll_count == 2:
            return [{"message": "Also inspect this chart.", "images": [steer_image]}]
        return []

    initial_image = "data:image/jpeg;base64,aGVsbG8="
    outcome = asyncio.run(
        run_child_turn(
            context=context,
            child=child,
            task="Read child.txt.",
            images=[initial_image],
            poll_messages=poll_messages,
        )
    )

    assert outcome.stop_reason == "completed"
    assert provider.requests[0].images == [initial_image]
    assert provider.requests[1].images == [initial_image, steer_image]
    assert any(
        "Also inspect this chart." in str(item.get("content") or "")
        for item in provider.requests[1].messages
    )
    assert any("Also inspect this chart." in item["content"] for item in child.turn_history)


def test_send_message_persists_only_conversation_scoped_image_references(
    delegation_environment: Any,
) -> None:
    environment = delegation_environment
    context = environment["context"]
    session = environment["session"]
    result = asyncio.run(
        dispatch_native_tool(
            "subagent",
            {"description": "child", "task": "Start the work.", "mode": "continuable"},
            context,
        )
    )
    child = _child_from_result(environment, result)
    child.pending_messages = []
    session.commit()
    image = DeepSpaceWorkspaceFile(
        tenant_id=environment["tenant_id"],
        user_id=environment["user_id"],
        conversation_id=environment["conversation_id"],
        name="chart.png",
        content_type="image/png",
        content="",
        source="user",
        metadata_json={},
        size_bytes=8,
        is_binary=True,
        storage_bucket="test-bucket",
        storage_key="test/chart.png",
    )
    session.add(image)
    session.commit()

    sent = asyncio.run(
        dispatch_native_tool(
            "send_message",
            {
                "agent_id": str(child.id),
                "message": "Inspect this chart.",
                "attachment_file_ids": [str(image.id)],
            },
            context,
        )
    )

    assert sent["status"] == "delivered"
    session.refresh(child)
    assert child.pending_messages[0]["message"] == "Inspect this chart."
    assert child.pending_messages[0]["attachment_file_ids"] == [str(image.id)]

    from app.deepspace.workers.subagent_tasks import _prepare_child_messages

    class _ImageLoader:
        async def _load_attachment_vision_inputs(
            self, files: list[DeepSpaceWorkspaceFile], *, tenant_id: uuid.UUID
        ) -> tuple[list[str], dict[str, str]]:
            assert tenant_id == environment["tenant_id"]
            assert files == [image]
            return ["data:image/png;base64,aGVsbG8="], {
                str(image.id): "Image included as visual input."
            }

    prepared = asyncio.run(
        _prepare_child_messages(session, child, cast(Any, _ImageLoader()), child.pending_messages)
    )
    assert prepared["message"] == "Inspect this chart."
    assert prepared["images"] == ["data:image/png;base64,aGVsbG8="]

    other_conversation = Conversation(
        tenant_id=environment["tenant_id"],
        user_id=environment["user_id"],
        kind="deepspace",
        title="Other conversation",
    )
    session.add(other_conversation)
    session.flush()
    foreign_image = DeepSpaceWorkspaceFile(
        tenant_id=environment["tenant_id"],
        user_id=environment["user_id"],
        conversation_id=other_conversation.id,
        name="other.png",
        content_type="image/png",
        content="",
        source="user",
        metadata_json={},
        size_bytes=8,
        is_binary=True,
        storage_bucket="test-bucket",
        storage_key="test/other.png",
    )
    session.add(foreign_image)
    session.commit()

    rejected = asyncio.run(
        dispatch_native_tool(
            "send_message",
            {
                "agent_id": str(child.id),
                "message": "Try this other image.",
                "attachment_file_ids": [str(foreign_image.id)],
            },
            context,
        )
    )
    assert rejected["status"] == "rejected"
    session.refresh(child)
    assert len(child.pending_messages) == 1


def test_a_fork_is_recorded_as_a_one_shot_child(delegation_environment: Any) -> None:
    context = delegation_environment["context"]
    provider = delegation_environment["provider"]
    provider.script = [{"content": "The fork reports what it inherited."}]

    result = asyncio.run(
        dispatch_native_tool(
            "subagent_fork",
            {
                "description": "fork the conversation",
                "task": "Summarize the conversation so far.",
                "history_messages": 5,
            },
            context,
        )
    )

    assert result is not None
    assert result["status"] == "queued"
    assert result["origin"] == "fork"
    assert result["mode"] == "one-shot"
