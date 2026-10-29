"""Session query tools for DeepSpace.

DeepSpace records a typed event log for every run it makes.  These tools are
the read side of that log: an agent can search its own history, read one
event with the events around it, and follow the timeline of a conversation.

Every tool is read-only, and every read is scoped to the tenant and the user
that own the conversation.  A conversation of somebody else is reported as
not found, so a caller never learns that it exists.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import Field

from app.deepspace.services.session_query import (
    MAX_EVENT_RESULTS,
    MAX_SESSION_RESULTS,
    SessionQueryService,
)
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    define_native_tool,
    integer_property,
    require_db,
    string_property,
)

#: The largest number of characters one query may carry.
MAX_QUERY_CHARS = 200

#: The largest number of characters one conversation id may carry.
MAX_CONVERSATION_ID_CHARS = 64

#: The number of conversations one session search reports when the model
#: asks for no particular number.
DEFAULT_SESSION_LIMIT = 10

#: The number of events one event search reports when the model asks for no
#: particular number.
DEFAULT_EVENT_LIMIT = 20

#: The message a caller gets when a conversation is not theirs.
CONVERSATION_NOT_FOUND = "No conversation with that id belongs to this account."

#: The message a caller gets when an event does not exist.
EVENT_NOT_FOUND = "No event with that sequence number belongs to this conversation."


class SessionSearchArguments(ToolArguments):
    """Search the account's conversations for a text."""

    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    limit: int | None = Field(default=None, ge=1, le=MAX_SESSION_RESULTS)


class SessionEventSearchArguments(ToolArguments):
    """Search the events of one conversation for a text."""

    conversation_id: str = Field(min_length=1, max_length=MAX_CONVERSATION_ID_CHARS)
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    limit: int | None = Field(default=None, ge=1, le=MAX_EVENT_RESULTS)


class SessionEventReadArguments(ToolArguments):
    """Read one event of one conversation with the events around it."""

    conversation_id: str = Field(min_length=1, max_length=MAX_CONVERSATION_ID_CHARS)
    seq: int = Field(ge=1)


class SessionTraceArguments(ToolArguments):
    """Read the timeline of one conversation."""

    conversation_id: str = Field(min_length=1, max_length=MAX_CONVERSATION_ID_CHARS)


class SessionEventTraceArguments(ToolArguments):
    """Read the turn, the step, and the events of one event."""

    conversation_id: str = Field(min_length=1, max_length=MAX_CONVERSATION_ID_CHARS)
    seq: int = Field(ge=1)


def _service(context: ToolContext) -> SessionQueryService:
    """Return the query service for the calling request.

    Args:
        context: The calling request's tool context.

    Returns:
        The service.
    """
    return SessionQueryService(require_db(context))


def _conversation_id(raw: Any) -> uuid.UUID | None:
    """Return the conversation a model asked about.

    Args:
        raw: The identity the model supplied.

    Returns:
        The conversation's identity, or None when it is not one.
    """
    try:
        return uuid.UUID(str(raw).strip())
    except (AttributeError, TypeError, ValueError):
        return None


def _not_found(message: str) -> dict[str, Any]:
    """Return the result a caller gets when nothing matches.

    Args:
        message: The message that says what was not found.

    Returns:
        The result.
    """
    return {"status": "not_found", "message": message}


async def _session_search(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Search the account's conversations for a text."""
    try:
        sessions = _service(context).search_sessions(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            query=str(arguments["query"]),
            limit=int(arguments.get("limit") or DEFAULT_SESSION_LIMIT),
        )
    except Exception as exc:  # noqa: BLE001 - a tool never fails the turn
        return {"status": "error", "message": str(exc)}
    return {"status": "ok", "sessions": sessions}


async def _session_event_search(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Search the events of one conversation for a text."""
    conversation_id = _conversation_id(arguments.get("conversation_id"))
    if conversation_id is None:
        return _not_found(CONVERSATION_NOT_FOUND)
    try:
        events = _service(context).search_events(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=conversation_id,
            query=str(arguments["query"]),
            limit=int(arguments.get("limit") or DEFAULT_EVENT_LIMIT),
        )
    except Exception as exc:  # noqa: BLE001 - a tool never fails the turn
        return {"status": "error", "message": str(exc)}
    if events is None:
        return _not_found(CONVERSATION_NOT_FOUND)
    return {"status": "ok", "events": events}


async def _session_event_read(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read one event of one conversation with the events around it."""
    conversation_id = _conversation_id(arguments.get("conversation_id"))
    if conversation_id is None:
        return _not_found(CONVERSATION_NOT_FOUND)
    try:
        found = _service(context).read_event(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=conversation_id,
            seq=int(arguments["seq"]),
        )
    except Exception as exc:  # noqa: BLE001 - a tool never fails the turn
        return {"status": "error", "message": str(exc)}
    if found is None:
        return _not_found(EVENT_NOT_FOUND)
    return {"status": "ok", **found}


async def _session_trace(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read the timeline of one conversation."""
    conversation_id = _conversation_id(arguments.get("conversation_id"))
    if conversation_id is None:
        return _not_found(CONVERSATION_NOT_FOUND)
    try:
        trace = _service(context).trace_session(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=conversation_id,
        )
    except Exception as exc:  # noqa: BLE001 - a tool never fails the turn
        return {"status": "error", "message": str(exc)}
    if trace is None:
        return _not_found(CONVERSATION_NOT_FOUND)
    return {"status": "ok", **trace}


async def _session_event_trace(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read the turn, the step, and the events of one event."""
    conversation_id = _conversation_id(arguments.get("conversation_id"))
    if conversation_id is None:
        return _not_found(CONVERSATION_NOT_FOUND)
    try:
        trace = _service(context).trace_event(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=conversation_id,
            seq=int(arguments["seq"]),
        )
    except Exception as exc:  # noqa: BLE001 - a tool never fails the turn
        return {"status": "error", "message": str(exc)}
    if trace is None:
        return _not_found(EVENT_NOT_FOUND)
    return {"status": "ok", **trace}


SESSION_SEARCH_DESCRIPTION = (
    "Search this account's past conversations for a text, such as a file name, a tool "
    "name, or a phrase from an earlier answer. Use it to find out what was already "
    "done before repeating the work. It reports the conversations that mention the "
    "text, newest first, with the event that matched."
)
SESSION_EVENT_SEARCH_DESCRIPTION = (
    "Search the events of one conversation for a text. Use it after session_search "
    "found a conversation, to find the exact events that mention a term. The events "
    "come back in the order the conversation recorded them."
)
SESSION_EVENT_READ_DESCRIPTION = (
    "Read one event of one conversation in full, with the events before and after it. "
    "Use it to see what the agent did at one point of a conversation."
)
SESSION_TRACE_DESCRIPTION = (
    "Read the timeline of one conversation: its turns, its steps, and its tool calls in "
    "the order they happened. Use it to see how a conversation unfolded."
)
SESSION_EVENT_TRACE_DESCRIPTION = (
    "Read the lineage of one event of one conversation: the turn it belongs to, the step "
    "it belongs to, and the events of that step. Use it to see what surrounded one "
    "event."
)

SESSION_QUERY_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="session_search",
        description=SESSION_SEARCH_DESCRIPTION,
        properties={
            "query": string_property(
                "The text to look for.",
                min_length=1,
                max_length=MAX_QUERY_CHARS,
            ),
            "limit": integer_property(
                "Largest number of conversations to report.",
                minimum=1,
                maximum=MAX_SESSION_RESULTS,
            ),
        },
        required=["query"],
        arguments_model=SessionSearchArguments,
        mode="read",
        handler=_session_search,
    ),
    define_native_tool(
        name="session_event_search",
        description=SESSION_EVENT_SEARCH_DESCRIPTION,
        properties={
            "conversation_id": string_property(
                "The conversation to search.",
                min_length=1,
                max_length=MAX_CONVERSATION_ID_CHARS,
            ),
            "query": string_property(
                "The text to look for.",
                min_length=1,
                max_length=MAX_QUERY_CHARS,
            ),
            "limit": integer_property(
                "Largest number of events to report.",
                minimum=1,
                maximum=MAX_EVENT_RESULTS,
            ),
        },
        required=["conversation_id", "query"],
        arguments_model=SessionEventSearchArguments,
        mode="read",
        handler=_session_event_search,
    ),
    define_native_tool(
        name="session_event_read",
        description=SESSION_EVENT_READ_DESCRIPTION,
        properties={
            "conversation_id": string_property(
                "The conversation the event belongs to.",
                min_length=1,
                max_length=MAX_CONVERSATION_ID_CHARS,
            ),
            "seq": integer_property(
                "The event's sequence number in the conversation.",
                minimum=1,
            ),
        },
        required=["conversation_id", "seq"],
        arguments_model=SessionEventReadArguments,
        mode="read",
        handler=_session_event_read,
    ),
    define_native_tool(
        name="session_trace",
        description=SESSION_TRACE_DESCRIPTION,
        properties={
            "conversation_id": string_property(
                "The conversation to trace.",
                min_length=1,
                max_length=MAX_CONVERSATION_ID_CHARS,
            ),
        },
        required=["conversation_id"],
        arguments_model=SessionTraceArguments,
        mode="read",
        handler=_session_trace,
    ),
    define_native_tool(
        name="session_event_trace",
        description=SESSION_EVENT_TRACE_DESCRIPTION,
        properties={
            "conversation_id": string_property(
                "The conversation the event belongs to.",
                min_length=1,
                max_length=MAX_CONVERSATION_ID_CHARS,
            ),
            "seq": integer_property(
                "The event's sequence number in the conversation.",
                minimum=1,
            ),
        },
        required=["conversation_id", "seq"],
        arguments_model=SessionEventTraceArguments,
        mode="read",
        handler=_session_event_trace,
    ),
]

__all__ = [
    "SESSION_QUERY_TOOLS",
    "SessionEventReadArguments",
    "SessionEventSearchArguments",
    "SessionEventTraceArguments",
    "SessionSearchArguments",
    "SessionTraceArguments",
]
