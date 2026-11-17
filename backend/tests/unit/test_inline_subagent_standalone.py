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
