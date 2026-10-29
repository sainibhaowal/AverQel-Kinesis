"""End-to-end checks for the DeepSpace session query service and its tools.

The checks run against the isolated test services, so they exercise the real
queries, the real tenant scope, and the real database.  They write a small
session log, search it, and read one of its events back.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

from app.auth.dependencies import AuthContext
from app.core.config import get_settings
from app.deepspace.repositories.chat import DeepSpaceChatRepository
from app.deepspace.services.session_events import (
    ASSISTANT_MESSAGE,
    STEP_START,
    TOOL_CALL,
    TOOL_RESULT,
    TURN_START,
    SessionEventStore,
)
from app.deepspace.services.session_query import SessionQueryService
from app.deepspace.services.tools._base import ToolContext
from app.deepspace.services.tools.session_query import SESSION_QUERY_TOOLS
from tests.conftest import SeededUser


def _handler(name: str) -> Any:
    """Return the handler one session query tool declares.

    Args:
        name: The tool's name.

    Returns:
        The tool's handler.
    """
    for spec in SESSION_QUERY_TOOLS:
        if spec.name == name:
            return spec.handler
    raise AssertionError(f"no session query tool named {name!r}")


def _context(db: Any, seeded: SeededUser, conversation_id: uuid.UUID) -> ToolContext:
    """Build the tool context one request carries.

    Args:
        db: The database session the tools read.
        seeded: The seeded user the call belongs to.
        conversation_id: The conversation the call belongs to.

    Returns:
        The tool context.
    """
    return ToolContext(
        auth=AuthContext(
            user_id=seeded.user_id,
            tenant_id=seeded.tenant_id,
            roles=frozenset({"user"}),
            token_id="session-query-test",
        ),
        conversation_id=conversation_id,
        settings=get_settings(),
        db=db,
        task_store=SimpleNamespace(),
        tool_result_store=None,
        provider_registry=SimpleNamespace(),
    )


def _conversation(db: Any, seeded: SeededUser, title: str) -> uuid.UUID:
    """Create one conversation.

    The conversation is created through the repository rather than over the
    API, because the API refuses a chat creation while the host is under
    resource pressure, which has nothing to do with the session log.

    Args:
        db: The database session to write through.
        seeded: The seeded user the conversation belongs to.
        title: The conversation's title.

    Returns:
        The conversation's identity.
    """
    conversation = DeepSpaceChatRepository(db).create_conversation(
        tenant_id=seeded.tenant_id,
        user_id=seeded.user_id,
        title=title,
    )
    db.commit()
    return conversation.id


def _seed_events(
    db: Any,
    seeded: SeededUser,
    conversation_id: uuid.UUID,
) -> None:
    """Write a small session log for one conversation.

    Args:
        db: The database session to write through.
        seeded: The seeded user that owns the conversation.
        conversation_id: The conversation the events belong to.
    """
    store = SessionEventStore(db)
    for description in (
        {
            "event_type": TURN_START,
            "turn": 0,
            "payload": {"provider_request_started": True},
        },
        {
            "event_type": STEP_START,
            "turn": 0,
            "step": 1,
            "payload": {"phase": "model"},
        },
        {
            "event_type": TOOL_CALL,
            "turn": 0,
            "step": 1,
            "tool_name": "fs_read",
            "payload": {"arguments": {"path": "notes/todo.md"}},
        },
        {
            "event_type": TOOL_RESULT,
            "turn": 0,
            "step": 1,
            "tool_name": "fs_read",
            "payload": {"status": "completed"},
        },
        {
            "event_type": ASSISTANT_MESSAGE,
            "turn": 0,
            "payload": {"text": "The todo list is empty.", "citations": 0},
        },
    ):
        store.append(
            tenant_id=seeded.tenant_id,
            user_id=seeded.user_id,
            conversation_id=conversation_id,
            event_type=str(description["event_type"]),
            payload=description.get("payload"),
            turn=description.get("turn"),
            step=description.get("step"),
            tool_name=description.get("tool_name"),
        )
        # The store reads the last sequence number from the database, so the
        # event it just added has to be visible to the next one.
        db.flush()
    db.commit()


def test_a_session_search_finds_the_conversation_that_mentions_the_text(
    seed_user,
    db_session,
) -> None:
    seeded = seed_user(
        "Session Query Tenant", "session-query@example.com", "password123", ("user",)
    )
    conversation_id = _conversation(db_session, seeded, "Session query chat")
    _seed_events(db_session, seeded, conversation_id)

    service = SessionQueryService(db_session)
    results = service.search_sessions(
        tenant_id=seeded.tenant_id,
        user_id=seeded.user_id,
        query="todo.md",
    )

    assert len(results) == 1
    assert results[0]["conversation_id"] == str(conversation_id)
    assert results[0]["title"] == "Session query chat"
    assert results[0]["matched_event"]["event_type"] == TOOL_CALL
    assert results[0]["matched_event"]["tool_name"] == "fs_read"
    assert "notes/todo.md" in results[0]["matched_event"]["excerpt"]


def test_a_session_search_finds_nothing_when_no_event_mentions_the_text(
    seed_user,
    db_session,
) -> None:
    seeded = seed_user(
        "Session Query Empty", "session-query-empty@example.com", "password123", ("user",)
    )
    conversation_id = _conversation(db_session, seeded, "Session query empty chat")
    _seed_events(db_session, seeded, conversation_id)

    service = SessionQueryService(db_session)
    results = service.search_sessions(
        tenant_id=seeded.tenant_id,
        user_id=seeded.user_id,
        query="kubernetes",
    )

    assert results == []


def test_an_event_search_reports_the_matching_events_in_order(
    seed_user,
    db_session,
) -> None:
    seeded = seed_user(
        "Session Query Events", "session-query-events@example.com", "password123", ("user",)
    )
    conversation_id = _conversation(db_session, seeded, "Session query events chat")
    _seed_events(db_session, seeded, conversation_id)

    service = SessionQueryService(db_session)
    events = service.search_events(
        tenant_id=seeded.tenant_id,
        user_id=seeded.user_id,
        conversation_id=conversation_id,
        query="fs_read",
    )

    assert events is not None
    assert [event["seq"] for event in events] == [3, 4]
    assert events[0]["tool_name"] == "fs_read"
    assert events[0]["turn"] == 0
    assert events[0]["step"] == 1
    assert "notes/todo.md" in events[0]["excerpt"]


def test_reading_an_event_returns_its_payload_and_its_neighbours(
    seed_user,
    db_session,
) -> None:
    seeded = seed_user(
        "Session Query Read", "session-query-read@example.com", "password123", ("user",)
    )
    conversation_id = _conversation(db_session, seeded, "Session query read chat")
    _seed_events(db_session, seeded, conversation_id)

    service = SessionQueryService(db_session)
    found = service.read_event(
        tenant_id=seeded.tenant_id,
        user_id=seeded.user_id,
        conversation_id=conversation_id,
        seq=3,
    )

    assert found is not None
    assert found["event"]["event_type"] == TOOL_CALL
    assert found["event"]["tool_name"] == "fs_read"
    assert found["event"]["payload"] == {"arguments": {"path": "notes/todo.md"}}
    assert [neighbour["seq"] for neighbour in found["neighbours"]] == [2, 4]


def test_a_conversation_of_another_tenant_is_not_found(
    seed_user,
    db_session,
) -> None:
    owner = seed_user(
        "Session Query Owner", "session-query-owner@example.com", "password123", ("user",)
    )
    other = seed_user(
        "Session Query Other", "session-query-other@example.com", "password123", ("user",)
    )
    conversation_id = _conversation(db_session, owner, "Session query owner chat")
    _seed_events(db_session, owner, conversation_id)

    service = SessionQueryService(db_session)
    events = service.search_events(
        tenant_id=other.tenant_id,
        user_id=other.user_id,
        conversation_id=conversation_id,
        query="fs_read",
    )
    read = service.read_event(
        tenant_id=other.tenant_id,
        user_id=other.user_id,
        conversation_id=conversation_id,
        seq=3,
    )
    trace = service.trace_session(
        tenant_id=other.tenant_id,
        user_id=other.user_id,
        conversation_id=conversation_id,
    )

    assert events is None
    assert read is None
    assert trace is None


def test_the_session_search_tool_reports_the_matching_sessions(
    seed_user,
    db_session,
) -> None:
    seeded = seed_user(
        "Session Query Tool", "session-query-tool@example.com", "password123", ("user",)
    )
    conversation_id = _conversation(db_session, seeded, "Session query tool chat")
    _seed_events(db_session, seeded, conversation_id)

    result = asyncio.run(
        _handler("session_search")(
            {"query": "todo.md"},
            _context(db_session, seeded, conversation_id),
        )
    )

    assert result["status"] == "ok"
    assert result["sessions"][0]["conversation_id"] == str(conversation_id)
    assert result["sessions"][0]["matched_event"]["seq"] == 3


def test_the_session_trace_tool_reports_the_timeline(
    seed_user,
    db_session,
) -> None:
    seeded = seed_user(
        "Session Query Trace", "session-query-trace@example.com", "password123", ("user",)
    )
    conversation_id = _conversation(db_session, seeded, "Session query trace chat")
    _seed_events(db_session, seeded, conversation_id)

    result = asyncio.run(
        _handler("session_trace")(
            {"conversation_id": str(conversation_id)},
            _context(db_session, seeded, conversation_id),
        )
    )

    assert result["status"] == "ok"
    assert [entry["seq"] for entry in result["timeline"]] == [1, 2, 3, 4, 5]
    assert result["timeline"][2]["tool_name"] == "fs_read"
