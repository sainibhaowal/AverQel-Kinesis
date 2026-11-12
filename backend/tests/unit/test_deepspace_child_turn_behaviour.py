"""Tests for the behaviour of a delegated child's own turn.

A child's turn is part of the conversation it belongs to: its tool calls
reach the typed log, its finished status is the one the product reports, and
the turn it runs does not hold the parent's write lock.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.deepspace.models.conversation import Conversation
from app.deepspace.models.subagent import DeepSpaceSubagentRun
from app.deepspace.services.chat_service import DeepSpaceChatService
from app.deepspace.services.runtime_policy import DeepSpaceToolPolicy
from app.deepspace.services.session_events import TOOL_CALL, TOOL_RESULT, SessionEventStore
from app.deepspace.services.subagent_runner import child_reported_status, run_child_turn
from app.deepspace.services.tools import delegation as delegation_module
from app.deepspace.services.tools import dispatch_native_tool
from app.deepspace.services.tools._base import ToolContext
from app.platform.database import model_registry  # noqa: F401
from app.platform.database.base import Base
from app.platform.database.sqlite_compat import sanitize_metadata_for_sqlite
from app.providers.services.types import ChatGenerateResponse

pytestmark = pytest.mark.unit_no_db


@pytest.fixture(autouse=True)
def _disable_background_child_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep child-behaviour assertions inside the fixture's private SQLite session."""
    monkeypatch.setattr(delegation_module, "_dispatch_child_turn", lambda *_args, **_kwargs: False)


def _run_child_inline(environment: dict[str, Any], task: str) -> DeepSpaceSubagentRun:
    """Run the child loop against the test-owned session, not the worker DB."""
    session = environment["session"]
    child = (
        session.query(DeepSpaceSubagentRun)
        .filter(DeepSpaceSubagentRun.conversation_id == environment["conversation_id"])
        .one()
    )
    outcome = asyncio.run(run_child_turn(context=environment["context"], child=child, task=task))
    child.result_text = outcome.text
    child.stop_reason = outcome.stop_reason
    child.status = "idle" if outcome.stop_reason == "completed" else outcome.stop_reason
    child.last_error = outcome.diagnostic
    session.commit()
    return child


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


@pytest.fixture(scope="module")
def delegation_database(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Build the database the delegation checks share."""
    tmp_path = tmp_path_factory.mktemp("delegation")
    settings = Settings(
        deepspace_workspace_root=str(tmp_path / "workspaces"),
        deepspace_subagent_max_depth=1,
        deepspace_subagent_max_children=4,
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'delegation.db'}")
    sanitize_metadata_for_sqlite(Base.metadata)
    Base.metadata.create_all(bind=engine)
    return engine, settings


@pytest.fixture
def delegation_environment(delegation_database: Any) -> Any:
    """Build a self-contained context and provider for one delegation."""
    engine, settings = delegation_database
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
    session.close()


def test_a_child_tool_call_reaches_the_typed_log(delegation_environment: Any) -> None:
    context = delegation_environment["context"]
    provider = delegation_environment["provider"]
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
        {"content": "The child read the file."},
    ]

    asyncio.run(
        dispatch_native_tool(
            "subagent",
            {"description": "read the child file", "task": "Read child.txt."},
            context,
        )
    )
    _run_child_inline(delegation_environment, "Read child.txt.")
    session = delegation_environment["session"]
    child = (
        session.query(DeepSpaceSubagentRun)
        .filter(DeepSpaceSubagentRun.conversation_id == delegation_environment["conversation_id"])
        .one()
    )
    events = SessionEventStore(session).events(
        tenant_id=delegation_environment["tenant_id"],
        user_id=delegation_environment["user_id"],
        conversation_id=delegation_environment["conversation_id"],
    )
    recorded = [(event.event_type, event.tool_name, event.subagent_id) for event in events]

    assert (TOOL_CALL, "fs_read", child.id) in recorded
    assert (TOOL_RESULT, "fs_read", child.id) in recorded


def test_a_finished_child_is_reported_as_completed(delegation_environment: Any) -> None:
    context = delegation_environment["context"]
    provider = delegation_environment["provider"]
    provider.script = [{"content": "The child reports 7."}]

    asyncio.run(
        dispatch_native_tool(
            "subagent",
            {"description": "child", "task": "work", "mode": "continuable"},
            context,
        )
    )
    _run_child_inline(delegation_environment, "work")
    session = delegation_environment["session"]
    child = (
        session.query(DeepSpaceSubagentRun)
        .filter(DeepSpaceSubagentRun.conversation_id == delegation_environment["conversation_id"])
        .one()
    )

    assert child.stop_reason == "completed"
    assert child_reported_status(child) == "completed"


def test_list_agents_hides_a_finished_child(delegation_environment: Any) -> None:
    context = delegation_environment["context"]
    provider = delegation_environment["provider"]
    provider.script = [{"content": "The child reports 7."}]
    asyncio.run(
        dispatch_native_tool(
            "subagent",
            {"description": "child", "task": "work", "mode": "continuable"},
            context,
        )
    )
    _run_child_inline(delegation_environment, "work")

    listed = asyncio.run(dispatch_native_tool("list_agents", {"include_completed": False}, context))
    every = asyncio.run(dispatch_native_tool("list_agents", {"include_completed": True}, context))

    assert listed is not None
    assert listed["agent_count"] == 0
    assert every is not None
    assert every["agent_count"] == 1
    assert every["agents"][0]["status"] == "completed"


def test_list_agents_returns_only_the_callers_owned_tree(delegation_environment: Any) -> None:
    environment = delegation_environment
    context = environment["context"]
    provider = environment["provider"]
    provider.script = [{"content": "root one"}, {"content": "root two"}]
    asyncio.run(
        dispatch_native_tool("subagent", {"description": "root one", "task": "work"}, context)
    )
    asyncio.run(
        dispatch_native_tool("subagent", {"description": "root two", "task": "work"}, context)
    )
    session = environment["session"]
    roots = session.query(DeepSpaceSubagentRun).all()
    first = next(row for row in roots if row.label == "root one")
    second = next(row for row in roots if row.label == "root two")
    nested = DeepSpaceSubagentRun(
        tenant_id=environment["tenant_id"],
        user_id=environment["user_id"],
        conversation_id=environment["conversation_id"],
        parent_agent_id=first.id,
        origin="subagent",
        mode="continuable",
        label="owned nested child",
        delegation_depth=2,
        tool_filter={},
        descriptor={},
        inherited_history=[],
        pending_messages=[],
        turn_history=[],
        status="idle",
        busy=False,
        cancel_requested=False,
        turn_count=0,
    )
    session.add(nested)
    session.commit()

    root_listing = asyncio.run(dispatch_native_tool("list_agents", {}, context))
    child_context = replace(context, parent_agent_id=first.id)
    child_listing = asyncio.run(dispatch_native_tool("list_agents", {}, child_context))

    assert root_listing is not None
    assert root_listing["agent_count"] == 3
    tree_by_id = {agent["agent_id"]: agent for agent in root_listing["agents"]}
    assert set(tree_by_id) == {str(first.id), str(second.id)}
    assert tree_by_id[str(first.id)]["children"][0]["agent_id"] == str(nested.id)
    assert child_listing is not None
    assert child_listing["agent_count"] == 1
    assert [agent["agent_id"] for agent in child_listing["agents"]] == [str(nested.id)]
    with pytest.raises(ValueError, match="No child agent"):
        delegation_module._load_child(child_context, str(second.id))
    with pytest.raises(ValueError, match="No child agent"):
        delegation_module._load_child(context, str(nested.id))


def test_list_agents_revision_ignores_parent_tool_events(delegation_environment: Any) -> None:
    context = delegation_environment["context"]
    provider = delegation_environment["provider"]
    provider.script = [{"content": "The child reports 7."}]
    asyncio.run(
        dispatch_native_tool(
            "subagent",
            {"description": "child", "task": "work", "mode": "continuable"},
            context,
        )
    )

    first = asyncio.run(dispatch_native_tool("list_agents", {}, context))
    SessionEventStore(delegation_environment["session"]).append(
        tenant_id=delegation_environment["tenant_id"],
        user_id=delegation_environment["user_id"],
        conversation_id=delegation_environment["conversation_id"],
        event_type=TOOL_CALL,
        payload={"tool_name": "list_agents"},
        tool_name="list_agents",
    )
    delegation_environment["session"].commit()
    second = asyncio.run(dispatch_native_tool("list_agents", {}, context))

    assert first is not None
    assert second is not None
    assert second["revision"] == first["revision"]


def test_list_agents_keeps_a_child_whose_turn_failed(delegation_environment: Any) -> None:
    context = delegation_environment["context"]
    provider = delegation_environment["provider"]
    provider.script = [{"content": ""}]
    asyncio.run(
        dispatch_native_tool(
            "subagent",
            {"description": "child", "task": "work", "mode": "continuable"},
            context,
        )
    )
    session = delegation_environment["session"]
    child = (
        session.query(DeepSpaceSubagentRun)
        .filter(DeepSpaceSubagentRun.conversation_id == delegation_environment["conversation_id"])
        .one()
    )
    child.status = "error"
    session.commit()

    listed = asyncio.run(dispatch_native_tool("list_agents", {"include_completed": False}, context))

    assert listed is not None
    assert listed["agent_count"] == 1
    assert listed["agents"][0]["status"] == "error"


def test_new_child_message_cancels_and_restarts_the_active_provider_stream(
    delegation_environment: Any,
) -> None:
    environment = delegation_environment
    context = environment["context"]
    asyncio.run(
        dispatch_native_tool(
            "subagent",
            {"description": "steerable child", "task": "Read a.txt and summarize it."},
            context,
        )
    )
    child = (
        environment["session"]
        .query(DeepSpaceSubagentRun)
        .filter(DeepSpaceSubagentRun.conversation_id == environment["conversation_id"])
        .one()
    )

    class StreamingProvider:
        supports_tool_calling = True

        def __init__(self) -> None:
            self.requests: list[Any] = []
            self.first_stream_closed = asyncio.Event()

        async def stream_generate_events(self, request: Any):
            self.requests.append(
                SimpleNamespace(
                    stream=request.stream,
                    messages=[dict(message) for message in request.messages],
                )
            )
            if len(self.requests) == 1:
                try:
                    await asyncio.Event().wait()
                finally:
                    self.first_stream_closed.set()
                yield {"type": "delta", "text": "This stale answer must be discarded."}
                return
            if len(self.requests) == 2:
                yield {
                    "type": "tool_calls_delta",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-steered-read",
                            "function": {
                                "name": "fs_read",
                                "arguments": '{"path":"a.txt"}',
                            },
                        }
                    ],
                }
                return
            yield {"type": "delta", "text": "I followed the updated instruction."}

    provider = StreamingProvider()
    context = replace(
        context,
        provider_registry=SimpleNamespace(
            get_chat_provider_from_selection=lambda _candidate: provider
        ),
    )
    poll_count = 0

    async def poll_messages() -> list[dict[str, Any]]:
        nonlocal poll_count
        poll_count += 1
        if poll_count == 2:
            return [{"message": "Read a.txt first, then follow the updated instruction."}]
        return []

    outcome = asyncio.run(
        run_child_turn(
            context=context,
            child=child,
            task="Read a.txt and summarize it.",
            poll_messages=poll_messages,
        )
    )

    assert outcome.stop_reason == "completed"
    assert outcome.text == "I followed the updated instruction."
    assert outcome.called_tools == ["fs_read"]
    assert environment["calls"] == [("fs_read", {"path": "a.txt"})]
    assert len(provider.requests) == 3
    assert provider.first_stream_closed.is_set()
    assert provider.requests[0].stream is True
    assert provider.requests[1].messages[-1] == {
        "role": "user",
        "content": "Read a.txt first, then follow the updated instruction.",
    }


def _chat_service() -> DeepSpaceChatService:
    """Build a chat service with only the parts a tool call needs."""
    service = object.__new__(DeepSpaceChatService)
    service.settings = Settings()
    service.db = None
    service.tool_policy = DeepSpaceToolPolicy()
    service.session_events = None
    service.runtime = SimpleNamespace(is_cancel_requested=lambda **_kwargs: False)
    return service


async def _run_tool_call(
    service: DeepSpaceChatService,
    *,
    tool_name: str,
    write_lock: asyncio.Lock,
    observed: dict[str, Any],
) -> dict[str, Any]:
    """Run one tool call and record whether the write lock was held."""
    service._execute_productivity_tool = _observed_executor(observed)  # type: ignore[method-assign]
    return await service._run_tool_call(
        tool_name=tool_name,
        arguments={},
        auth=SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4()),
        conversation_id=uuid.uuid4(),
        web_provider=None,
        web_candidate=None,
        request=None,
        loop_deadline=asyncio.get_running_loop().time() + 60,
        run_id=None,
        read_semaphore=asyncio.Semaphore(4),
        write_lock=write_lock,
    )


def _observed_executor(observed: dict[str, Any]) -> Any:
    """Build a tool executor that records the state of the write lock."""

    async def execute(**_kwargs: Any) -> dict[str, Any]:
        observed["write_lock_held"] = observed["write_lock"].locked()
        return {"status": "ok"}

    return execute


def test_a_child_turn_runs_without_the_parents_write_lock() -> None:
    service = _chat_service()
    write_lock = asyncio.Lock()
    observed: dict[str, Any] = {"write_lock": write_lock}

    result = asyncio.run(
        _run_tool_call(service, tool_name="subagent", write_lock=write_lock, observed=observed)
    )

    assert result["success"] is True
    assert observed["write_lock_held"] is False


def test_a_write_tool_holds_the_parents_write_lock() -> None:
    service = _chat_service()
    write_lock = asyncio.Lock()
    observed: dict[str, Any] = {"write_lock": write_lock}

    result = asyncio.run(
        _run_tool_call(service, tool_name="fs_write", write_lock=write_lock, observed=observed)
    )

    assert result["success"] is True
    assert observed["write_lock_held"] is True
