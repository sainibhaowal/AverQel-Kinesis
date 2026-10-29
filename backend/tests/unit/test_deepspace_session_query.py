"""Tests for the DeepSpace session query service and its tools.

The checks run against a session double that answers the service's queries
from memory, so they never touch a database.  The double applies the tenant,
the user, and the conversation a statement names, and leaves the text
comparison to the service, which is what decides what matched.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.deepspace.models.conversation import Conversation
from app.deepspace.services.session_events import (
    ASSISTANT_MESSAGE,
    STEP_END,
    STEP_START,
    TOOL_CALL,
    TOOL_RESULT,
    TURN_END,
    TURN_START,
)
from app.deepspace.services.session_query import SessionQueryService
from app.deepspace.services.tools import session_query as session_query_tools
from app.deepspace.services.tools._base import ToolContext, ToolHandler

pytestmark = pytest.mark.unit_no_db

TENANT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
USER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
OTHER_TENANT_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
OTHER_USER_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
CONVERSATION_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
OTHER_CONVERSATION_ID = uuid.UUID("66666666-6666-4666-8666-666666666666")


def _event(**overrides: Any) -> SimpleNamespace:
    """Build one session event row.

    Args:
        overrides: The fields to replace.

    Returns:
        The event row.
    """
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": TENANT_ID,
        "user_id": USER_ID,
        "conversation_id": CONVERSATION_ID,
        "run_id": None,
        "subagent_id": None,
        "seq": 1,
        "event_type": TOOL_CALL,
        "turn": 0,
        "step": 1,
        "tool_name": "fs_read",
        "payload": {"arguments": {"path": "notes/todo.md"}},
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _conversation(**overrides: Any) -> SimpleNamespace:
    """Build one conversation row.

    Args:
        overrides: The fields to replace.

    Returns:
        The conversation row.
    """
    values: dict[str, Any] = {
        "id": CONVERSATION_ID,
        "tenant_id": TENANT_ID,
        "user_id": USER_ID,
        "title": "Ship the release",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _param(params: dict[str, Any], prefix: str) -> Any:
    """Return the value of the first parameter whose name starts with a prefix.

    Args:
        params: The compiled parameters of a statement.
        prefix: The prefix to look for.

    Returns:
        The value, or None when the statement has no such parameter.
    """
    for name, value in params.items():
        if name.startswith(prefix):
            return value
    return None


def _list_param(params: dict[str, Any]) -> list[Any] | None:
    """Return the value of the first parameter that carries a list.

    Args:
        params: The compiled parameters of a statement.

    Returns:
        The list, or None when the statement has no such parameter.
    """
    for value in params.values():
        if isinstance(value, list | tuple):
            return list(value)
    return None


def _selects(statement: Any, model: type[Any]) -> bool:
    """Report whether a statement selects one model.

    Args:
        statement: The compiled statement.
        model: The model to look for.

    Returns:
        True when the statement selects the model.
    """
    return any(description.get("entity") is model for description in statement.column_descriptions)


class _FakeResult:
    """Stand in for the SQLAlchemy result the service reads."""

    def __init__(self, rows: list[Any]) -> None:
        """Create the result.

        Args:
            rows: The rows the statement selects.
        """
        self._rows = rows

    def scalars(self) -> _FakeResult:
        """Return the result itself, which already holds entities."""
        return self

    def all(self) -> list[Any]:
        """Return the rows."""
        return self._rows


class _FakeSession:
    """Answer the query service's statements from memory."""

    def __init__(self, events: list[Any], conversations: list[Any]) -> None:
        """Create the double.

        Args:
            events: The event rows the double knows.
            conversations: The conversation rows the double knows.
        """
        self._events = list(events)
        self._conversations = list(conversations)
        self.statements: list[Any] = []

    def execute(self, statement: Any) -> _FakeResult:
        """Answer one statement.

        Args:
            statement: The statement the service built.

        Returns:
            The rows the statement selects.
        """
        self.statements.append(statement)
        params = statement.compile().params
        tenant_id = _param(params, "tenant_id")
        user_id = _param(params, "user_id")
        if _selects(statement, Conversation):
            rows = [
                conversation
                for conversation in self._conversations
                if conversation.tenant_id == tenant_id and conversation.user_id == user_id
            ]
            wanted = _list_param(params)
            if wanted is not None:
                rows = [row for row in rows if row.id in wanted]
            else:
                one = _param(params, "id")
                if one is not None:
                    rows = [row for row in rows if row.id == one]
            return _FakeResult(rows)
        rows = [
            event
            for event in self._events
            if event.tenant_id == tenant_id and event.user_id == user_id
        ]
        conversation_id = _param(params, "conversation_id")
        if conversation_id is not None:
            rows = [row for row in rows if row.conversation_id == conversation_id]
        return _FakeResult(rows)


def _session(
    events: list[Any] | None = None,
    conversations: list[Any] | None = None,
) -> SessionQueryService:
    """Return a query service bound to a session double.

    Args:
        events: The event rows the double knows.
        conversations: The conversation rows the double knows.

    Returns:
        The service.
    """
    return SessionQueryService(
        _FakeSession(
            events if events is not None else [],
            conversations if conversations is not None else [],
        )
    )


def _handler(name: str) -> ToolHandler:
    """Return the handler one session query tool declares.

    Args:
        name: The tool's name.

    Returns:
        The tool's handler.
    """
    for spec in session_query_tools.SESSION_QUERY_TOOLS:
        if spec.name == name:
            return spec.handler
    raise AssertionError(f"no session query tool named {name!r}")


def _context(db: Any, *, auth: Any = None, **overrides: Any) -> ToolContext:
    """Build a tool context for the session query tools.

    Args:
        db: The database session the tools read.
        auth: The tenant and user the call belongs to.
        overrides: The context fields to replace.

    Returns:
        The tool context.
    """
    return ToolContext(
        auth=auth or SimpleNamespace(tenant_id=TENANT_ID, user_id=USER_ID),
        conversation_id=CONVERSATION_ID,
        settings=SimpleNamespace(),
        db=db,
        task_store=SimpleNamespace(),
        tool_result_store=None,
        provider_registry=SimpleNamespace(),
        **overrides,
    )


def _events() -> list[Any]:
    """Return the events of one conversation.

    Returns:
        The events, oldest first.
    """
    return [
        _event(
            seq=1,
            event_type=TURN_START,
            turn=0,
            step=None,
            tool_name=None,
            payload={"provider_request_started": True},
            created_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        ),
        _event(
            seq=2,
            event_type=STEP_START,
            turn=0,
            step=1,
            tool_name=None,
            payload={"phase": "model"},
            created_at=datetime(2026, 1, 1, 10, 0, 1, tzinfo=UTC),
        ),
        _event(
            seq=3,
            event_type=TOOL_CALL,
            turn=0,
            step=1,
            tool_name="fs_read",
            payload={"arguments": {"path": "notes/todo.md"}},
            created_at=datetime(2026, 1, 1, 10, 0, 2, tzinfo=UTC),
        ),
        _event(
            seq=4,
            event_type=TOOL_RESULT,
            turn=0,
            step=1,
            tool_name="fs_read",
            payload={"status": "completed"},
            created_at=datetime(2026, 1, 1, 10, 0, 3, tzinfo=UTC),
        ),
        _event(
            seq=5,
            event_type=STEP_END,
            turn=0,
            step=1,
            tool_name=None,
            payload={},
            created_at=datetime(2026, 1, 1, 10, 0, 4, tzinfo=UTC),
        ),
        _event(
            seq=6,
            event_type=ASSISTANT_MESSAGE,
            turn=0,
            step=None,
            tool_name=None,
            payload={"text": "The todo list is empty.", "citations": 0},
            created_at=datetime(2026, 1, 1, 10, 0, 5, tzinfo=UTC),
        ),
        _event(
            seq=7,
            event_type=TURN_END,
            turn=0,
            step=None,
            tool_name=None,
            payload={},
            created_at=datetime(2026, 1, 1, 10, 0, 6, tzinfo=UTC),
        ),
    ]


def test_a_session_search_reports_the_matching_conversation() -> None:
    service = _session(events=_events(), conversations=[_conversation()])

    results = service.search_sessions(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        query="todo.md",
    )

    assert len(results) == 1
    assert results[0]["conversation_id"] == str(CONVERSATION_ID)
    assert results[0]["title"] == "Ship the release"
    assert results[0]["matched_event"]["seq"] == 3
    assert results[0]["matched_event"]["event_type"] == TOOL_CALL
    assert results[0]["matched_event"]["tool_name"] == "fs_read"
    assert "notes/todo.md" in results[0]["matched_event"]["excerpt"]


def test_a_session_search_reports_the_newest_conversation_first() -> None:
    older = _event(
        seq=1,
        conversation_id=OTHER_CONVERSATION_ID,
        tool_name="fs_read",
        payload={"arguments": {"path": "notes/todo.md"}},
        created_at=datetime(2025, 12, 1, tzinfo=UTC),
    )
    service = _session(
        events=[older, *_events()],
        conversations=[
            _conversation(),
            _conversation(id=OTHER_CONVERSATION_ID, title="Older chat"),
        ],
    )

    results = service.search_sessions(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        query="todo.md",
    )

    assert [result["conversation_id"] for result in results] == [
        str(CONVERSATION_ID),
        str(OTHER_CONVERSATION_ID),
    ]


def test_a_session_search_names_the_tool_that_matched() -> None:
    service = _session(events=_events(), conversations=[_conversation()])

    results = service.search_sessions(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        query="fs_read",
    )

    assert results[0]["matched_event"]["tool_name"] == "fs_read"
    assert results[0]["matched_event"]["seq"] == 3


def test_a_session_search_ignores_the_events_of_another_tenant() -> None:
    foreign = _event(
        seq=1,
        tenant_id=OTHER_TENANT_ID,
        tool_name="fs_read",
        payload={"arguments": {"path": "notes/todo.md"}},
    )
    service = _session(
        events=[foreign],
        conversations=[_conversation(tenant_id=OTHER_TENANT_ID)],
    )

    results = service.search_sessions(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        query="todo.md",
    )

    assert results == []


def test_a_session_search_ignores_the_events_of_another_user() -> None:
    foreign = _event(seq=1, user_id=OTHER_USER_ID, payload={"text": "todo.md"})
    service = _session(events=[foreign], conversations=[_conversation(user_id=OTHER_USER_ID)])

    results = service.search_sessions(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        query="todo.md",
    )

    assert results == []


def test_a_session_search_returns_nothing_when_no_event_matches() -> None:
    service = _session(events=_events(), conversations=[_conversation()])

    results = service.search_sessions(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        query="kubernetes",
    )

    assert results == []


def test_a_session_search_needs_every_term_of_the_query() -> None:
    service = _session(events=_events(), conversations=[_conversation()])

    both = service.search_sessions(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        query="todo.md completed",
    )
    one = service.search_sessions(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        query="todo.md kubernetes",
    )

    assert both == []
    assert one == []


def test_a_session_search_ignores_the_case_of_the_query() -> None:
    service = _session(events=_events(), conversations=[_conversation()])

    results = service.search_sessions(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        query="TODO.MD",
    )

    assert len(results) == 1


def test_an_event_search_reports_the_matching_events_in_order() -> None:
    service = _session(events=_events(), conversations=[_conversation()])

    events = service.search_events(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        query="fs_read",
    )

    assert events is not None
    assert [event["seq"] for event in events] == [3, 4]
    assert events[0]["event_type"] == TOOL_CALL
    assert events[0]["tool_name"] == "fs_read"
    assert events[0]["turn"] == 0
    assert events[0]["step"] == 1
    assert "notes/todo.md" in events[0]["excerpt"]


def test_an_event_search_of_a_conversation_of_another_tenant_is_not_found() -> None:
    service = _session(events=_events(), conversations=[_conversation()])

    events = service.search_events(
        tenant_id=OTHER_TENANT_ID,
        user_id=OTHER_USER_ID,
        conversation_id=CONVERSATION_ID,
        query="fs_read",
    )

    assert events is None


def test_an_event_search_of_a_conversation_that_does_not_exist_is_not_found() -> None:
    service = _session(events=_events(), conversations=[_conversation()])

    events = service.search_events(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_id=uuid.uuid4(),
        query="fs_read",
    )

    assert events is None


def test_an_event_search_returns_nothing_when_no_event_matches() -> None:
    service = _session(events=_events(), conversations=[_conversation()])

    events = service.search_events(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        query="kubernetes",
    )

    assert events == []


def test_reading_an_event_returns_it_with_its_neighbours() -> None:
    service = _session(events=_events(), conversations=[_conversation()])

    found = service.read_event(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        seq=3,
    )

    assert found is not None
    assert found["event"]["seq"] == 3
    assert found["event"]["event_type"] == TOOL_CALL
    assert found["event"]["tool_name"] == "fs_read"
    assert found["event"]["payload"] == {"arguments": {"path": "notes/todo.md"}}
    assert [neighbour["seq"] for neighbour in found["neighbours"]] == [2, 4]
    assert found["neighbours"][0]["event_type"] == STEP_START


def test_reading_an_event_of_another_tenants_conversation_is_not_found() -> None:
    service = _session(events=_events(), conversations=[_conversation()])

    found = service.read_event(
        tenant_id=OTHER_TENANT_ID,
        user_id=OTHER_USER_ID,
        conversation_id=CONVERSATION_ID,
        seq=3,
    )

    assert found is None


def test_reading_an_event_that_does_not_exist_is_not_found() -> None:
    service = _session(events=_events(), conversations=[_conversation()])

    found = service.read_event(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        seq=99,
    )

    assert found is None


def test_a_session_trace_reports_the_timeline_in_order() -> None:
    service = _session(events=_events(), conversations=[_conversation()])

    trace = service.trace_session(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
    )

    assert trace is not None
    assert [entry["seq"] for entry in trace["timeline"]] == [1, 2, 3, 4, 5, 6, 7]
    assert trace["timeline"][2]["tool_name"] == "fs_read"
    assert trace["timeline"][2]["turn"] == 0
    assert trace["timeline"][2]["step"] == 1
    assert trace["timeline"][2]["summary"] == "called fs_read"
    assert trace["timeline"][0]["summary"] == "turn 0 started"


def test_tracing_a_conversation_of_another_tenant_is_not_found() -> None:
    service = _session(events=_events(), conversations=[_conversation()])

    trace = service.trace_session(
        tenant_id=OTHER_TENANT_ID,
        user_id=OTHER_USER_ID,
        conversation_id=CONVERSATION_ID,
    )

    assert trace is None


def test_an_event_trace_reports_the_turn_the_step_and_the_events() -> None:
    service = _session(events=_events(), conversations=[_conversation()])

    trace = service.trace_event(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        seq=3,
    )

    assert trace is not None
    lineage = trace["lineage"]
    assert [turn["event_type"] for turn in lineage["turns"]] == [TURN_START, TURN_END]
    assert [step["event_type"] for step in lineage["steps"]] == [STEP_START, STEP_END]
    assert [event["seq"] for event in lineage["events"]] == [2, 3, 4, 5]
    assert lineage["events"][1]["tool_name"] == "fs_read"


def test_tracing_an_event_of_another_tenants_conversation_is_not_found() -> None:
    service = _session(events=_events(), conversations=[_conversation()])

    trace = service.trace_event(
        tenant_id=OTHER_TENANT_ID,
        user_id=OTHER_USER_ID,
        conversation_id=CONVERSATION_ID,
        seq=3,
    )

    assert trace is None


def test_the_session_query_tools_declare_their_names_and_modes() -> None:
    specs = {spec.name: spec.mode for spec in session_query_tools.SESSION_QUERY_TOOLS}

    assert specs == {
        "session_search": "read",
        "session_event_search": "read",
        "session_event_read": "read",
        "session_trace": "read",
        "session_event_trace": "read",
    }


def test_the_session_query_tools_declare_their_arguments() -> None:
    specs = {
        spec.name: spec.definition["function"]["parameters"]
        for spec in session_query_tools.SESSION_QUERY_TOOLS
    }

    assert specs["session_search"]["required"] == ["query"]
    assert set(specs["session_search"]["properties"]) == {"query", "limit"}
    assert specs["session_event_search"]["required"] == ["conversation_id", "query"]
    assert set(specs["session_event_search"]["properties"]) == {
        "conversation_id",
        "query",
        "limit",
    }
    assert specs["session_event_read"]["required"] == ["conversation_id", "seq"]
    assert specs["session_trace"]["required"] == ["conversation_id"]
    assert specs["session_event_trace"]["required"] == ["conversation_id", "seq"]


def test_the_session_query_arguments_refuse_an_undeclared_field() -> None:
    with pytest.raises(ValidationError):
        session_query_tools.SessionSearchArguments(query="a", unexpected="b")


def test_the_session_query_arguments_refuse_a_limit_above_the_cap() -> None:
    with pytest.raises(ValidationError):
        session_query_tools.SessionSearchArguments(query="a", limit=5_000)
    with pytest.raises(ValidationError):
        session_query_tools.SessionEventSearchArguments(conversation_id="c", query="a", limit=5_000)


async def test_the_session_search_tool_reports_the_matching_sessions() -> None:
    db = _FakeSession(_events(), [_conversation()])
    context = _context(db)

    result = await _handler("session_search")({"query": "todo.md"}, context)

    assert result["status"] == "ok"
    assert result["sessions"][0]["conversation_id"] == str(CONVERSATION_ID)
    assert result["sessions"][0]["matched_event"]["seq"] == 3


async def test_the_session_event_search_tool_reports_the_matching_events() -> None:
    db = _FakeSession(_events(), [_conversation()])
    context = _context(db)

    result = await _handler("session_event_search")(
        {"conversation_id": str(CONVERSATION_ID), "query": "fs_read"},
        context,
    )

    assert result["status"] == "ok"
    assert [event["seq"] for event in result["events"]] == [3, 4]


async def test_the_session_event_read_tool_reports_the_event_and_its_neighbours() -> None:
    db = _FakeSession(_events(), [_conversation()])
    context = _context(db)

    result = await _handler("session_event_read")(
        {"conversation_id": str(CONVERSATION_ID), "seq": 3},
        context,
    )

    assert result["status"] == "ok"
    assert result["event"]["seq"] == 3
    assert [neighbour["seq"] for neighbour in result["neighbours"]] == [2, 4]


async def test_the_session_trace_tool_reports_the_timeline() -> None:
    db = _FakeSession(_events(), [_conversation()])
    context = _context(db)

    result = await _handler("session_trace")(
        {"conversation_id": str(CONVERSATION_ID)},
        context,
    )

    assert result["status"] == "ok"
    assert [entry["seq"] for entry in result["timeline"]] == [1, 2, 3, 4, 5, 6, 7]


async def test_the_session_event_trace_tool_reports_the_lineage() -> None:
    db = _FakeSession(_events(), [_conversation()])
    context = _context(db)

    result = await _handler("session_event_trace")(
        {"conversation_id": str(CONVERSATION_ID), "seq": 3},
        context,
    )

    assert result["status"] == "ok"
    assert [turn["event_type"] for turn in result["lineage"]["turns"]] == [
        TURN_START,
        TURN_END,
    ]
    assert [event["seq"] for event in result["lineage"]["events"]] == [2, 3, 4, 5]


async def test_the_tools_report_a_conversation_of_another_tenant_as_not_found() -> None:
    db = _FakeSession(_events(), [_conversation()])
    context = _context(
        db,
        auth=SimpleNamespace(tenant_id=OTHER_TENANT_ID, user_id=OTHER_USER_ID),
    )

    search = await _handler("session_event_search")(
        {"conversation_id": str(CONVERSATION_ID), "query": "fs_read"},
        context,
    )
    read = await _handler("session_event_read")(
        {"conversation_id": str(CONVERSATION_ID), "seq": 3},
        context,
    )
    trace = await _handler("session_trace")(
        {"conversation_id": str(CONVERSATION_ID)},
        context,
    )
    event_trace = await _handler("session_event_trace")(
        {"conversation_id": str(CONVERSATION_ID), "seq": 3},
        context,
    )

    for result in (search, read, trace, event_trace):
        assert result["status"] == "not_found"
        assert "belongs to" in str(result["message"])


async def test_the_tools_report_a_conversation_id_that_is_not_one_as_not_found() -> None:
    db = _FakeSession(_events(), [_conversation()])
    context = _context(db)

    result = await _handler("session_trace")({"conversation_id": "not-an-id"}, context)

    assert result["status"] == "not_found"


async def test_a_tool_reports_a_failure_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("the database is unreachable")

    monkeypatch.setattr(
        session_query_tools,
        "SessionQueryService",
        _explode,
    )
    context = _context(_FakeSession(_events(), [_conversation()]))

    result = await _handler("session_search")({"query": "todo.md"}, context)

    assert result == {"status": "error", "message": "the database is unreachable"}


async def test_a_tool_without_a_database_session_reports_an_error() -> None:
    context = _context(None)

    result = await _handler("session_search")({"query": "todo.md"}, context)

    assert result["status"] == "error"
    assert "database session" in str(result["message"])
