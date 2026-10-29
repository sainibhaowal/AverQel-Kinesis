"""The read side of DeepSpace's typed session event log.

DeepSpace records a typed event for every turn, step, tool call, tool result,
and delegation it makes.  The log is durable, but nothing could read it: an
agent could not look up what it did in an earlier conversation, and an
operator could not audit a turn's tool timeline.

This module is that read side.  It offers five queries over the log:

* :meth:`SessionQueryService.search_sessions` finds the conversations whose
  events mention a text.
* :meth:`SessionQueryService.search_events` finds the events of one
  conversation that mention a text.
* :meth:`SessionQueryService.read_event` reads one event with its neighbours.
* :meth:`SessionQueryService.trace_session` reads a conversation's timeline.
* :meth:`SessionQueryService.trace_event` reads the lineage of one event.

Every read is scoped to the tenant and the user that own the conversation.  A
conversation that belongs to somebody else is reported as not found, so a
caller never learns that it exists.

The database narrows a search with a case-insensitive ``LIKE`` over the tool
name, the event type, and the rendered payload.  The matcher in this module
decides what matched, so the answer is the same whether or not the database
narrowed the rows.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Text, bindparam, cast, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import BindParameter, ColumnElement

from app.core.config import get_settings
from app.deepspace.models.conversation import Conversation
from app.deepspace.models.message import Message
from app.deepspace.models.session_event import DeepSpaceSessionEvent
from app.deepspace.services.session_events import (
    ASSISTANT_MESSAGE,
    PLAN_REVIEWED,
    PLAN_SUBMITTED,
    STEP_END,
    STEP_START,
    SUBAGENT_DESCRIPTOR,
    SUBAGENT_RESULT,
    SUBAGENT_SPAWN,
    TOOL_CALL,
    TOOL_RESULT,
    TURN_END,
    TURN_START,
)

#: The largest number of conversations one session search reports.
MAX_SESSION_RESULTS = 50

#: The largest number of events one event search reports.
MAX_EVENT_RESULTS = 100

#: The largest number of events one trace reads.
MAX_TRACE_EVENTS = 2_000

#: The largest number of matching events one search scans.
MAX_SEARCH_SCAN = 2_000

#: The number of characters one excerpt carries.
EXCERPT_CHARS = 240

#: The number of characters one summary line carries.
SUMMARY_CHARS = 160

#: The escape character the ``LIKE`` patterns escape their wildcards with.
_LIKE_ESCAPE = "\\"

#: The event types that open and close a turn.
_TURN_EVENTS = (TURN_START, TURN_END)

#: The event types that open and close a step.
_STEP_EVENTS = (STEP_START, STEP_END)


def _terms(query: str) -> list[str]:
    """Return the case-folded terms of a search query.

    A query is split on whitespace, so ``"database migration"`` asks for
    events that mention both words rather than for that exact phrase.

    Args:
        query: The text the caller asked for.

    Returns:
        The terms, lowercased and without duplicates.
    """
    seen: list[str] = []
    for term in str(query or "").lower().split():
        if term and term not in seen:
            seen.append(term)
    return seen


def _like_pattern(term: str) -> str:
    """Return the ``LIKE`` pattern that matches one term literally.

    Args:
        term: The term to match.

    Returns:
        The pattern, with the wildcards in the term escaped.
    """
    escaped = (
        term.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", f"{_LIKE_ESCAPE}%")
        .replace("_", f"{_LIKE_ESCAPE}_")
    )
    return f"%{escaped}%"


def _payload_is_searchable() -> bool:
    """Report whether the database can search an event's payload.

    An encrypted payload is ciphertext in the database, so a SQL text match
    on the column cannot find a term in it.  The matcher reads the decrypted
    payload instead, so the query narrows by the columns that stay plaintext
    and the matcher applies every term.

    Returns:
        True when payloads are stored as plaintext and can be searched in
        SQL.
    """
    return not bool(getattr(get_settings(), "deepspace_session_log_encryption", False))


def _term_condition(term: str, index: int) -> ColumnElement[bool]:
    """Return the condition that finds one term in an event's text.

    The event's text is its tool name, its event type, and its payload
    rendered as text.  The three are searched separately, because one term
    never spans two of them.  The condition applies only to a plaintext
    payload; :func:`_payload_is_searchable` decides whether it runs.

    Args:
        term: The term to find.
        index: The term's position in the query, which names the parameter.

    Returns:
        The condition.
    """
    pattern: BindParameter[str] = bindparam(f"term_{index}", value=_like_pattern(term))
    return or_(
        DeepSpaceSessionEvent.tool_name.ilike(pattern, escape=_LIKE_ESCAPE),
        DeepSpaceSessionEvent.event_type.ilike(pattern, escape=_LIKE_ESCAPE),
        cast(DeepSpaceSessionEvent.payload, Text).ilike(pattern, escape=_LIKE_ESCAPE),
    )


def _term_conditions(terms: list[str]) -> list[ColumnElement[bool]]:
    """Return the conditions that find every term of a query.

    Args:
        terms: The terms to find.

    Returns:
        One condition per term, which together require every term.
    """
    return [_term_condition(term, index) for index, term in enumerate(terms)]


def _payload_text(payload: dict[str, Any] | None) -> str:
    """Return one event's payload rendered as text.

    The rendering matches the text the database stores, so a term that the
    database finds is a term the matcher finds too.

    Args:
        payload: The event's payload.

    Returns:
        The rendered payload.
    """
    if not payload:
        return ""
    try:
        return json.dumps(payload)
    except (TypeError, ValueError):
        return str(payload)


def _event_parts(event: DeepSpaceSessionEvent) -> dict[str, str]:
    """Return the lowercased text of one event.

    Args:
        event: The event to read.

    Returns:
        The tool name, the event type, and the rendered payload.
    """
    return {
        "tool_name": str(event.tool_name or "").lower(),
        "event_type": str(event.event_type or "").lower(),
        "payload": _payload_text(event.payload).lower(),
    }


def _matches(parts: dict[str, str], terms: list[str]) -> bool:
    """Report whether one event mentions every term of a query.

    Args:
        parts: The event's text.
        terms: The terms to find.

    Returns:
        True when every term appears in the event's text.
    """
    return all(any(term in part for part in parts.values()) for term in terms)


def _excerpt(text: str, terms: list[str]) -> str:
    """Return the part of one text that carries the query.

    Args:
        text: The text to excerpt.
        terms: The terms to find.

    Returns:
        A short excerpt around the first term, or the start of the text when
        no term is in it.
    """
    if not text:
        return ""
    lowered = text.lower()
    position = -1
    for term in terms:
        found = lowered.find(term)
        if found >= 0 and (position < 0 or found < position):
            position = found
    if position < 0:
        return text[:EXCERPT_CHARS].strip()
    start = max(0, position - EXCERPT_CHARS // 3)
    end = min(len(text), start + EXCERPT_CHARS)
    excerpt = text[start:end].strip()
    if start > 0:
        excerpt = f"…{excerpt}"
    if end < len(text):
        excerpt = f"{excerpt}…"
    return excerpt


def _summary(event: DeepSpaceSessionEvent) -> str:
    """Return one line that says what one event records.

    Args:
        event: The event to summarize.

    Returns:
        The summary line.
    """
    event_type = str(event.event_type or "")
    tool_name = str(event.tool_name or "")
    payload = dict(event.payload or {})
    if event_type == TURN_START:
        return f"turn {event.turn} started"
    if event_type == TURN_END:
        return f"turn {event.turn} ended"
    if event_type == STEP_START:
        return f"step {event.step} started"
    if event_type == STEP_END:
        return f"step {event.step} ended"
    if event_type == TOOL_CALL:
        return f"called {tool_name}" if tool_name else "tool call"
    if event_type == TOOL_RESULT:
        return f"{tool_name} returned" if tool_name else "tool result"
    if event_type == SUBAGENT_SPAWN:
        label = str(payload.get("label") or "")
        return f"delegated to {label}" if label else "delegated to a child agent"
    if event_type == SUBAGENT_RESULT:
        return "the child agent finished"
    if event_type == SUBAGENT_DESCRIPTOR:
        return "child agent descriptor"
    if event_type == ASSISTANT_MESSAGE:
        text = str(payload.get("text") or "").strip().replace("\n", " ")
        return f"assistant message: {text[:SUMMARY_CHARS]}" if text else "assistant message"
    if event_type == PLAN_SUBMITTED:
        return "plan submitted"
    if event_type == PLAN_REVIEWED:
        return f"plan {payload.get('decision') or 'reviewed'}"
    return event_type or "event"


def _event_time(event: DeepSpaceSessionEvent) -> datetime:
    """Return the time one event was recorded.

    Args:
        event: The event to read.

    Returns:
        The event's time, or the earliest possible time when it has none.
    """
    created_at = event.created_at
    if created_at.tzinfo is None:
        return created_at.replace(tzinfo=UTC)
    return created_at


def _strongest(events: list[DeepSpaceSessionEvent], terms: list[str]) -> DeepSpaceSessionEvent:
    """Return the event that names the query most directly.

    A term in the tool name is the strongest signal, because the tool name
    says what the agent did.  The earliest event wins a tie, so the result
    points at where the topic entered the conversation.

    Args:
        events: The matching events of one conversation.
        terms: The terms of the query.

    Returns:
        The strongest event.
    """

    def rank(event: DeepSpaceSessionEvent) -> tuple[int, int]:
        parts = _event_parts(event)
        named = sum(1 for term in terms if term in parts["tool_name"])
        return (-named, int(event.seq))

    return min(events, key=rank)


def _event_row(event: DeepSpaceSessionEvent) -> dict[str, Any]:
    """Return one event as a model-facing row.

    Args:
        event: The event to project.

    Returns:
        The row.
    """
    return {
        "seq": int(event.seq),
        "event_type": str(event.event_type or ""),
        "tool_name": str(event.tool_name) if event.tool_name else None,
        "turn": event.turn,
        "step": event.step,
        "summary": _summary(event),
    }


def _full_event(event: DeepSpaceSessionEvent) -> dict[str, Any]:
    """Return one event in full.

    Args:
        event: The event to project.

    Returns:
        The event's fields, including its whole payload.
    """
    return {
        "seq": int(event.seq),
        "event_type": str(event.event_type or ""),
        "tool_name": str(event.tool_name) if event.tool_name else None,
        "turn": event.turn,
        "step": event.step,
        "run_id": str(event.run_id) if event.run_id else None,
        "subagent_id": str(event.subagent_id) if event.subagent_id else None,
        "created_at": event.created_at.isoformat() if event.created_at else None,
        "payload": dict(event.payload or {}),
        "summary": _summary(event),
    }


class SessionQueryService:
    """Query the typed session event log of one tenant's conversations."""

    def __init__(self, db: Session) -> None:
        """Create the service.

        Args:
            db: The database session the queries run on.
        """
        self.db = db

    def search_sessions(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        query: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Find the conversations whose events mention a text.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            query: The text to look for.
            limit: The largest number of conversations to return.

        Returns:
            The matching conversations, newest first, each with the strongest
            matching event.  An empty list when nothing matches.
        """
        terms = _terms(query)
        if not terms:
            return []
        events = self._matching_events(
            tenant_id=tenant_id,
            user_id=user_id,
            terms=terms,
        )
        grouped: dict[uuid.UUID, list[DeepSpaceSessionEvent]] = {}
        for event in events:
            grouped.setdefault(event.conversation_id, []).append(event)
        if not grouped:
            return []
        ordered = sorted(
            grouped.items(),
            key=lambda item: (
                max(_event_time(event) for event in item[1]),
                max(int(event.seq) for event in item[1]),
            ),
            reverse=True,
        )
        selected = ordered[: max(1, min(limit, MAX_SESSION_RESULTS))]
        titles = self._conversation_titles(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_ids=[conversation_id for conversation_id, _ in selected],
        )
        results: list[dict[str, Any]] = []
        for conversation_id, matching in selected:
            strongest = _strongest(matching, terms)
            results.append(
                {
                    "conversation_id": str(conversation_id),
                    "title": titles.get(conversation_id, "Untitled"),
                    "matched_event": {
                        "seq": int(strongest.seq),
                        "event_type": str(strongest.event_type or ""),
                        "tool_name": str(strongest.tool_name) if strongest.tool_name else None,
                        "excerpt": _excerpt(_payload_text(strongest.payload), terms),
                    },
                }
            )
        return results

    def search_events(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        query: str,
        limit: int = 20,
    ) -> list[dict[str, Any]] | None:
        """Find the events of one conversation that mention a text.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to search.
            query: The text to look for.
            limit: The largest number of events to return.

        Returns:
            The matching events in sequence order, or None when the
            conversation does not belong to the tenant and the user.  An
            empty list when the conversation has no matching event.
        """
        if (
            self._conversation(
                tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
            )
            is None
        ):
            return None
        terms = _terms(query)
        if not terms:
            return []
        events = self._matching_events(
            tenant_id=tenant_id,
            user_id=user_id,
            terms=terms,
            conversation_id=conversation_id,
            by_sequence=True,
        )
        events.sort(key=lambda event: int(event.seq))
        return [
            {
                "seq": int(event.seq),
                "event_type": str(event.event_type or ""),
                "tool_name": str(event.tool_name) if event.tool_name else None,
                "turn": event.turn,
                "step": event.step,
                "excerpt": _excerpt(_payload_text(event.payload), terms),
            }
            for event in events[: max(1, min(limit, MAX_EVENT_RESULTS))]
        ]

    def read_event(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        seq: int,
    ) -> dict[str, Any] | None:
        """Read one event of one conversation with its neighbours.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the event belongs to.
            seq: The event's sequence number.

        Returns:
            The event in full and the events around it, or None when the
            conversation does not belong to the tenant and the user or the
            event does not exist.
        """
        if (
            self._conversation(
                tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
            )
            is None
        ):
            return None
        target = self._event_at(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            seq=seq,
        )
        if target is None:
            return None
        previous = self._boundary(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            seq=seq,
            before=True,
            inclusive=False,
        )
        following = self._boundary(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            seq=seq,
            before=False,
            inclusive=False,
        )
        neighbours = [_event_row(event) for event in (previous, following) if event is not None]
        return {"event": _full_event(target), "neighbours": neighbours}

    def trace_session(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> dict[str, Any] | None:
        """Read the timeline of one conversation.

        The timeline holds every event of the conversation in sequence order,
        so a reader sees the turns, the steps, and the tool calls the agent
        made, in the order it made them.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to trace.

        Returns:
            The timeline, or None when the conversation does not belong to the
            tenant and the user.
        """
        if (
            self._conversation(
                tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
            )
            is None
        ):
            return None
        events = self._conversation_events(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        return {"timeline": [_event_row(event) for event in events]}

    async def get_trace(
        self,
        *,
        conversation_id: uuid.UUID,
        auth: Any,
        limit: int = 10,
    ) -> dict[str, Any] | None:
        """Read a conversation as turns of messages and tool calls.

        The messages are the conversation's durable transcript; the tool-call
        and tool-result events are merged by creation time so each turn shows
        the tools the agent ran while producing its answer.

        Args:
            conversation_id: The conversation to read.
            auth: The authenticated tenant and user that must own it.
            limit: The largest number of turns to return, newest last.

        Returns:
            The title, timestamps, counts, a snippet, and the turns, or None
            when the conversation does not belong to the tenant and user.
        """
        conversation = self._conversation(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        )
        if conversation is None:
            return None
        messages = list(
            self.db.execute(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at.asc(), Message.id.asc())
            )
            .scalars()
            .all()
        )
        events = self._conversation_events(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        )
        entries: list[tuple[float, int, str, Any]] = []
        for index, message in enumerate(messages):
            entries.append(
                (
                    message.created_at.timestamp() if message.created_at else 0.0,
                    index,
                    "message",
                    message,
                )
            )
        for index, event in enumerate(events):
            if event.event_type not in (TOOL_CALL, TOOL_RESULT):
                continue
            entries.append(
                (
                    event.created_at.timestamp() if event.created_at else 0.0,
                    len(messages) + index,
                    "event",
                    event,
                )
            )
        entries.sort(key=lambda entry: (entry[0], entry[1]))

        turns: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for _, _, kind, item in entries:
            if kind == "message":
                if item.role == "user":
                    current = {
                        "turn": len(turns) + 1,
                        "user_message": str(item.content or ""),
                        "assistant_message": "",
                        "tool_calls": [],
                    }
                    turns.append(current)
                elif item.role == "assistant" and current is not None:
                    answer = str(item.content or "")
                    if answer:
                        separator = "\n\n" if current["assistant_message"] else ""
                        current["assistant_message"] = (
                            current["assistant_message"] + separator + answer
                        )[:20_000]
                continue
            if current is None:
                continue
            payload = dict(item.payload or {})
            current["tool_calls"].append(
                {
                    "tool": str(item.tool_name or ""),
                    "status": (
                        str(payload.get("status") or "completed")
                        if item.event_type == TOOL_RESULT
                        else "called"
                    ),
                }
            )

        snippet = ""
        for turn in reversed(turns):
            snippet = str(turn["assistant_message"] or turn["user_message"])[:EXCERPT_CHARS]
            if snippet:
                break
        bounded = max(1, min(int(limit or 1), 100))
        return {
            "conversation_id": str(conversation.id),
            "title": str(conversation.title or ""),
            "updated_at": (conversation.updated_at.timestamp() if conversation.updated_at else 0.0),
            "turn_count": len(turns),
            "message_count": len(messages),
            "snippet": snippet,
            "turns": turns[-bounded:],
        }

    def trace_event(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        seq: int,
    ) -> dict[str, Any] | None:
        """Read the lineage of one event of one conversation.

        The lineage holds the turn the event belongs to, the step it belongs
        to, and the events of that step.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the event belongs to.
            seq: The event's sequence number.

        Returns:
            The lineage, or None when the conversation does not belong to the
            tenant and the user or the event does not exist.
        """
        if (
            self._conversation(
                tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
            )
            is None
        ):
            return None
        target = self._event_at(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            seq=seq,
        )
        if target is None:
            return None
        window = self._turn_window(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            seq=seq,
        )
        turns = [
            {
                "seq": int(event.seq),
                "event_type": str(event.event_type or ""),
                "turn": event.turn,
                "summary": _summary(event),
            }
            for event in window
            if event.event_type in _TURN_EVENTS
        ]
        steps = [
            {
                "seq": int(event.seq),
                "event_type": str(event.event_type or ""),
                "turn": event.turn,
                "step": event.step,
                "summary": _summary(event),
            }
            for event in window
            if event.event_type in _STEP_EVENTS
        ]
        scope = [
            event for event in window if event.turn == target.turn and event.step == target.step
        ]
        if not scope:
            scope = [event for event in window if event.turn == target.turn]
        if not scope:
            scope = list(window)
        return {
            "lineage": {
                "turns": turns,
                "steps": steps,
                "events": [_event_row(event) for event in scope],
            }
        }

    def _conversation(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> Conversation | None:
        """Read the conversation a query is about.

        The conversation is read through the tenant and the user that own it,
        so a conversation of somebody else is not found.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.

        Returns:
            The conversation, or None when it does not exist for this tenant
            and user.
        """
        rows = list(
            self.db.execute(
                select(Conversation).where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.user_id == user_id,
                    Conversation.id == conversation_id,
                )
            )
            .scalars()
            .all()
        )
        return rows[0] if rows else None

    def _conversation_titles(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, str]:
        """Read the titles of several conversations.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_ids: The conversations to read.

        Returns:
            The title of each conversation that was found.
        """
        if not conversation_ids:
            return {}
        rows = list(
            self.db.execute(
                select(Conversation).where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.user_id == user_id,
                    Conversation.id.in_(conversation_ids),
                )
            )
            .scalars()
            .all()
        )
        return {row.id: str(row.title or "") for row in rows}

    def _matching_events(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        terms: list[str],
        conversation_id: uuid.UUID | None = None,
        by_sequence: bool = False,
    ) -> list[DeepSpaceSessionEvent]:
        """Read the events of a tenant and a user that mention every term.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            terms: The terms to find.
            conversation_id: The conversation to read, when the search is
                about one conversation.
            by_sequence: Whether to order the events by their sequence number
                instead of by the time they were recorded.

        Returns:
            The matching events.
        """
        conditions: list[ColumnElement[bool]] = [
            DeepSpaceSessionEvent.tenant_id == tenant_id,
            DeepSpaceSessionEvent.user_id == user_id,
        ]
        if _payload_is_searchable():
            conditions.extend(_term_conditions(terms))
        # An encrypted payload is ciphertext, so the terms are applied to the
        # decrypted value by the matcher after the rows are read.
        if conversation_id is not None:
            conditions.append(DeepSpaceSessionEvent.conversation_id == conversation_id)
        order_by: tuple[ColumnElement[Any], ...]
        if by_sequence:
            order_by = (DeepSpaceSessionEvent.seq.asc(),)
        else:
            order_by = (
                DeepSpaceSessionEvent.created_at.desc(),
                DeepSpaceSessionEvent.seq.desc(),
            )
        rows = list(
            self.db.execute(
                select(DeepSpaceSessionEvent)
                .where(*conditions)
                .order_by(*order_by)
                .limit(MAX_SEARCH_SCAN)
            )
            .scalars()
            .all()
        )
        return [row for row in rows if _matches(_event_parts(row), terms)]

    def _conversation_events(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        limit: int = MAX_TRACE_EVENTS,
    ) -> list[DeepSpaceSessionEvent]:
        """Read the events of one conversation in sequence order.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.
            limit: The largest number of events to return.

        Returns:
            The events, oldest first.
        """
        return self._events_in_conversation(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            conditions=[],
            order_by=(DeepSpaceSessionEvent.seq.asc(),),
            limit=limit,
        )

    def _events_in_conversation(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        conditions: list[ColumnElement[bool]],
        order_by: tuple[ColumnElement[Any], ...],
        limit: int,
    ) -> list[DeepSpaceSessionEvent]:
        """Read the events of one conversation that satisfy some conditions.

        The conditions are part of the query, so the database reads the rows
        the caller asked for instead of the whole conversation.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.
            conditions: The extra conditions the rows have to satisfy.
            order_by: The order the rows come back in.
            limit: The largest number of events to return.

        Returns:
            The events.
        """
        rows = list(
            self.db.execute(
                select(DeepSpaceSessionEvent)
                .where(
                    DeepSpaceSessionEvent.tenant_id == tenant_id,
                    DeepSpaceSessionEvent.user_id == user_id,
                    DeepSpaceSessionEvent.conversation_id == conversation_id,
                    *conditions,
                )
                .order_by(*order_by)
                .limit(max(1, min(limit, MAX_TRACE_EVENTS)))
            )
            .scalars()
            .all()
        )
        return sorted(rows, key=lambda event: int(event.seq))

    def _event_at(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        seq: int,
    ) -> DeepSpaceSessionEvent | None:
        """Read one event of one conversation.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the event belongs to.
            seq: The event's sequence number.

        Returns:
            The event, or None when the conversation has no such event.
        """
        rows = self._events_in_conversation(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            conditions=[DeepSpaceSessionEvent.seq == seq],
            order_by=(DeepSpaceSessionEvent.seq.asc(),),
            limit=1,
        )
        for row in rows:
            if int(row.seq) == int(seq):
                return row
        return None

    def _boundary(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        seq: int,
        before: bool,
        event_type: str | None = None,
        inclusive: bool = True,
    ) -> DeepSpaceSessionEvent | None:
        """Read the event that bounds one sequence number.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.
            seq: The sequence number to bound.
            before: Whether to read the event before the sequence number
                instead of the event after it.
            event_type: The event type the boundary has to have, when the
                caller needs one.
            inclusive: Whether the event at the sequence number itself is a
                boundary.

        Returns:
            The bounding event, or None when the conversation has none.
        """
        conditions: list[ColumnElement[bool]] = []
        if before:
            conditions.append(
                DeepSpaceSessionEvent.seq <= seq if inclusive else DeepSpaceSessionEvent.seq < seq
            )
        else:
            conditions.append(
                DeepSpaceSessionEvent.seq >= seq if inclusive else DeepSpaceSessionEvent.seq > seq
            )
        if event_type is not None:
            conditions.append(DeepSpaceSessionEvent.event_type == event_type)
        order_by: tuple[ColumnElement[Any], ...] = (
            (DeepSpaceSessionEvent.seq.desc(),) if before else (DeepSpaceSessionEvent.seq.asc(),)
        )
        rows = self._events_in_conversation(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            conditions=conditions,
            order_by=order_by,
            limit=1,
        )
        candidates: list[DeepSpaceSessionEvent] = []
        for row in rows:
            row_seq = int(row.seq)
            if before and (row_seq > seq or (not inclusive and row_seq == seq)):
                continue
            if not before and (row_seq < seq or (not inclusive and row_seq == seq)):
                continue
            if event_type is not None and row.event_type != event_type:
                continue
            candidates.append(row)
        if not candidates:
            return None
        if before:
            return max(candidates, key=lambda row: int(row.seq))
        return min(candidates, key=lambda row: int(row.seq))

    def _turn_window(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        seq: int,
    ) -> list[DeepSpaceSessionEvent]:
        """Read the events of the turn that holds one event.

        The turn opens at the last turn start at or before the event and
        closes at the first turn end at or after it.  A turn that is still
        open closes at the last event of the conversation.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.
            seq: The event's sequence number.

        Returns:
            The events of the turn, oldest first.
        """
        start = self._boundary(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            seq=seq,
            before=True,
            event_type=TURN_START,
        )
        end = self._boundary(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            seq=seq,
            before=False,
            event_type=TURN_END,
        )
        if end is None:
            end = self._boundary(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                seq=seq,
                before=False,
            )
        low = int(start.seq) if start is not None else int(seq)
        high = int(end.seq) if end is not None else int(seq)
        rows = self._events_in_conversation(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            conditions=[
                DeepSpaceSessionEvent.seq >= low,
                DeepSpaceSessionEvent.seq <= high,
            ],
            order_by=(DeepSpaceSessionEvent.seq.asc(),),
            limit=MAX_TRACE_EVENTS,
        )
        return [row for row in rows if low <= int(row.seq) <= high]


__all__ = [
    "EXCERPT_CHARS",
    "MAX_EVENT_RESULTS",
    "MAX_SEARCH_SCAN",
    "MAX_SESSION_RESULTS",
    "MAX_TRACE_EVENTS",
    "SessionQueryService",
]
