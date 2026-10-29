"""End-to-end tests for the DeepSpace delegation tools.

The provider is stubbed, so the check exercises the real child loop, the real
child records, the real tool dispatch, and the real typed event log against a
self-contained SQLite database.
"""

from __future__ import annotations

import asyncio
import uuid
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
    SUBAGENT_RESULT,
    SUBAGENT_SPAWN,
    SessionEventStore,
)
from app.deepspace.services.tools import dispatch_native_tool
from app.deepspace.services.tools._base import ToolContext
from app.platform.database import model_registry  # noqa: F401
from app.platform.database.base import Base
from app.platform.database.sqlite_compat import sanitize_metadata_for_sqlite
from app.providers.services.types import ChatGenerateResponse

pytestmark = pytest.mark.unit_no_db


class StubProvider:
    """A provider that answers with a scripted sequence of responses."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.requests: list[Any] = []

    def generate(self, request: Any) -> ChatGenerateResponse:
        self.requests.append(request)
        payload = self.script.pop(0) if self.script else {"content": "done"}
        return ChatGenerateResponse(
            content=str(payload.get("content") or ""),
            tool_calls=payload.get("tool_calls"),
        )


@pytest.fixture
def delegation_environment(tmp_path: Path) -> Any:
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
        auth=SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
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
    yield {
        "context": context,
        "session": session,
        "provider": provider,
        "calls": calls,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
    }


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
    assert result["status"] == "ok"
    assert "reports 7" in str(result["answer"])
    assert any(name == "fs_read" for name, _ in calls)
    offered = {tool["function"]["name"] for tool in (provider.requests[0].tools or [])}
    assert "fs_read" in offered
    assert "subagent" not in offered
    assert "subagent_fork" not in offered


def test_a_delegated_child_is_durable(delegation_environment: Any) -> None:
    context = delegation_environment["context"]
    provider = delegation_environment["provider"]
    provider.script = [{"content": "The child reports 7."}]

    asyncio.run(
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
    assert "reports 7" in str(child.result_text)
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
    assert SUBAGENT_RESULT in kinds


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
    provider.script = [{"content": "The child reports 7."}]

    asyncio.run(
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
    assert result["status"] == "ok"
    assert result["origin"] == "fork"
    assert result["mode"] == "one-shot"
