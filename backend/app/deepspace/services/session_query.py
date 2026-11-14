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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import Text, bindparam, cast, func, or_, select, text
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
    SUBAGENT_END,
    SUBAGENT_PRESET,
    SUBAGENT_RESULT,
    SUBAGENT_SPAWN,
    SUBAGENT_START,
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
    if event_type == SUBAGENT_START:
        return f"child activation started ({payload.get('provider') or 'unknown provider'})"
    if event_type == SUBAGENT_PRESET:
        return f"child preset selected ({payload.get('agentPreset') or 'unknown'})"
    if event_type == SUBAGENT_END:
        return f"child activation ended ({payload.get('stop_reason') or 'unknown'})"
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


# ──────────────────────────────────────────────────────────────────────
# FTS5 Full-Text Search Enhancement
# ──────────────────────────────────────────────────────────────────────

FTS5_TABLE = "deepspace_session_event_fts5"
FTS5_TRIGGERS = (
    f"CREATE TRIGGER IF NOT EXISTS {FTS5_TABLE}_ai AFTER INSERT ON deepspace_session_event BEGIN "
    f"INSERT INTO {FTS5_TABLE} (rowid, tenant_id, user_id, conversation_id, event_type, tool_name, payload_text) "
    f"VALUES (new.id, new.tenant_id, new.user_id, new.conversation_id, new.event_type, new.tool_name, "
    f"json_extract(new.payload, '$.text') || ' ' || json_extract(new.payload, '$.content') || ' ' || "
    f"json_extract(new.payload, '$.message') || ' ' || new.tool_name || ' ' || new.event_type); END;",
    f"CREATE TRIGGER IF NOT EXISTS {FTS5_TABLE}_ad AFTER DELETE ON deepspace_session_event BEGIN "
    f"INSERT INTO {FTS5_TABLE} ({FTS5_TABLE}, rowid, tenant_id, user_id, conversation_id, event_type, tool_name, payload_text) "
    f"VALUES ('delete', old.id, old.tenant_id, old.user_id, old.conversation_id, old.event_type, old.tool_name, "
    f"json_extract(old.payload, '$.text') || ' ' || json_extract(old.payload, '$.content') || ' ' || "
    f"json_extract(old.payload, '$.message') || ' ' || old.tool_name || ' ' || old.event_type); END;",
    f"CREATE TRIGGER IF NOT EXISTS {FTS5_TABLE}_au AFTER UPDATE ON deepspace_session_event BEGIN "
    f"INSERT INTO {FTS5_TABLE} ({FTS5_TABLE}, rowid, tenant_id, user_id, conversation_id, event_type, tool_name, payload_text) "
    f"VALUES ('delete', old.id, old.tenant_id, old.user_id, old.conversation_id, old.event_type, old.tool_name, "
    f"json_extract(old.payload, '$.text') || ' ' || json_extract(old.payload, '$.content') || ' ' || "
    f"json_extract(old.payload, '$.message') || ' ' || old.tool_name || ' ' || old.event_type); "
    f"INSERT INTO {FTS5_TABLE} (rowid, tenant_id, user_id, conversation_id, event_type, tool_name, payload_text) "
    f"VALUES (new.id, new.tenant_id, new.user_id, new.conversation_id, new.event_type, new.tool_name, "
    f"json_extract(new.payload, '$.text') || ' ' || json_extract(new.payload, '$.content') || ' ' || "
    f"json_extract(new.payload, '$.message') || ' ' || new.tool_name || ' ' || new.event_type); END;",
)


def initialize_fts5(db: Session) -> None:
    """Create the FTS5 virtual table and triggers for full-text search.

    The table indexes all searchable fields of session events for fast
    full-text search across tenant/user boundaries.
    """
    db.execute(text(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {FTS5_TABLE} USING fts5(
            tenant_id UNINDEXED,
            user_id UNINDEXED,
            conversation_id UNINDEXED,
            event_type,
            tool_name,
            payload_text,
            content='deepspace_session_event',
            content_rowid='id'
        );
    """))
    for trigger in FTS5_TRIGGERS:
        db.execute(text(trigger))
    db.commit()


def populate_fts5(db: Session, batch_size: int = 1000) -> int:
    """Populate the FTS5 table with existing events.

    Args:
        db: The database session.
        batch_size: Number of events to process per batch.

    Returns:
        Number of events indexed.
    """
    from sqlalchemy import select

    from app.deepspace.models.session_event import DeepSpaceSessionEvent

    total = db.execute(select(func.count(DeepSpaceSessionEvent.id))).scalar() or 0

    offset = 0
    indexed = 0
    while offset < total:
        rows = (
            db.execute(
                select(DeepSpaceSessionEvent)
                .order_by(DeepSpaceSessionEvent.id)
                .limit(batch_size)
                .offset(offset)
            )
            .scalars()
            .all()
        )

        if not rows:
            break

        for event in rows:
            pt = _payload_text(event.payload)
            db.execute(
                text(f"""
                INSERT INTO {FTS5_TABLE} (rowid, tenant_id, user_id, conversation_id, event_type, tool_name, payload_text)
                VALUES (:rowid, :tenant_id, :user_id, :conversation_id, :event_type, :tool_name, :payload_text)
            """),
                {
                    "rowid": event.id,
                    "tenant_id": str(event.tenant_id),
                    "user_id": str(event.user_id),
                    "conversation_id": str(event.conversation_id),
                    "event_type": event.event_type or "",
                    "tool_name": event.tool_name or "",
                    "payload_text": pt,
                },
            )
        db.commit()
        indexed += len(rows)
        offset += batch_size

    return indexed


def fts5_search_sessions(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    query: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Fast full-text search for sessions using FTS5.

    Uses the FTS5 virtual table for fast full-text search across all
    event fields, then groups by conversation.
    """
    from sqlalchemy import text

    terms = _terms(query)
    if not terms:
        return []

    # Build FTS5 query - use phrase matching for exact phrases, OR for terms
    fts_query = " ".join(f'"{term}"' for term in terms)

    sql = text(f"""
        SELECT DISTINCT fts.conversation_id,
               MAX(dse.created_at) as last_event,
               MAX(dse.seq) as max_seq,
               SNIPPET({FTS5_TABLE}, 0, '<mark>', '</mark>', '…', 32) as snippet
        FROM {FTS5_TABLE} fts
        JOIN deepspace_session_event dse ON fts.rowid = dse.id
        WHERE fts.tenant_id = :tenant_id
          AND fts.user_id = :user_id
          AND {FTS5_TABLE} MATCH :query
        GROUP BY fts.conversation_id
        ORDER BY MAX(dse.created_at) DESC
        LIMIT :limit
    """)

    rows = db.execute(
        sql,
        {
            "tenant_id": str(tenant_id),
            "user_id": str(user_id),
            "query": fts_query,
            "limit": max(1, min(limit, MAX_SESSION_RESULTS)),
        },
    ).all()

    results = []
    for row in rows:
        results.append(
            {
                "conversation_id": str(row.conversation_id),
                "title": "",  # Filled by caller
                "matched_event": {
                    "snippet": row.snippet,
                },
            }
        )
    return results


def fts5_search_events(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    query: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Fast full-text search for events within one conversation."""
    from sqlalchemy import text

    terms = _terms(query)
    if not terms:
        return []

    fts_query = " ".join(f'"{term}"' for term in _terms(query))

    sql = text(f"""
        SELECT fts.rowid, fts.event_type, fts.tool_name,
               SNIPPET({FTS5_TABLE}, 0, '<mark>', '</mark>', '…', 32) as snippet,
               dse.seq, dse.turn, dse.step, dse.created_at
        FROM {FTS5_TABLE} fts
        JOIN deepspace_session_event dse ON fts.rowid = dse.id
        WHERE fts.tenant_id = :tenant_id
          AND fts.user_id = :user_id
          AND fts.conversation_id = :conversation_id
          AND {FTS5_TABLE} MATCH :query
        ORDER BY dse.seq ASC
        LIMIT :limit
    """)

    rows = db.execute(
        sql,
        {
            "tenant_id": str(tenant_id),
            "user_id": str(user_id),
            "conversation_id": str(conversation_id),
            "query": fts_query,
            "limit": max(1, min(limit, MAX_EVENT_RESULTS)),
        },
    ).all()

    return [
        {
            "seq": int(row.seq),
            "event_type": row.event_type,
            "tool_name": row.tool_name,
            "turn": row.turn,
            "step": row.step,
            "excerpt": row.snippet,
        }
        for row in rows
    ]


# ──────────────────────────────────────────────────────────────────────
# Observation Extraction (Auto-summarize tool calls/results)
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ToolObservation:
    """Extracted observation from a tool call/result pair."""

    tool_name: str
    call_seq: int
    result_seq: int | None
    turn: int
    step: int
    status: str  # called, completed, failed, error
    input_summary: str
    output_summary: str
    duration_ms: int | None
    error: str | None


def extract_tool_observations(
    events: list[DeepSpaceSessionEvent],
) -> list[ToolObservation]:
    """Extract structured observations from tool call/result events.

    Matches TOOL_CALL with its corresponding TOOL_RESULT and extracts
    a structured summary of what the tool did, its input, output, and outcome.
    """
    calls_by_seq: dict[int, DeepSpaceSessionEvent] = {}
    results_by_call: dict[int, DeepSpaceSessionEvent] = {}

    for event in events:
        if event.event_type == TOOL_CALL:
            calls_by_seq[event.seq] = event
        elif event.event_type == TOOL_RESULT:
            payload = dict(event.payload or {})
            call_seq = payload.get("call_seq")
            if isinstance(call_seq, int | str) and call_seq:
                results_by_call[int(call_seq)] = event

    observations = []
    for call_seq, call_event in sorted(calls_by_seq.items()):
        result_event = results_by_call.get(call_seq)

        call_payload = dict(call_event.payload or {})
        result_payload = dict(result_event.payload or {}) if result_event else {}

        status = "called"
        if result_event:
            status = str(result_payload.get("status") or "completed")
            if status in ("failed", "error"):
                status = "failed"

        # Summarize input
        input_summary = _summarize_tool_input(call_event.tool_name, call_payload)

        # Summarize output
        output_summary = ""
        error = None
        if result_event:
            output_summary = _summarize_tool_output(result_event.tool_name, result_payload)
            if status == "failed":
                error = str(
                    result_payload.get("error") or result_payload.get("message") or "Unknown error"
                )

        # Calculate duration
        duration_ms = None
        if result_event and call_event.created_at and result_event.created_at:
            duration_ms = int(
                (result_event.created_at - call_event.created_at).total_seconds() * 1000
            )

        observations.append(
            ToolObservation(
                tool_name=call_event.tool_name or "unknown",
                call_seq=call_seq,
                result_seq=result_event.seq if result_event else None,
                turn=call_event.turn if call_event.turn is not None else 0,
                step=call_event.step if call_event.step is not None else 0,
                status=status,
                input_summary=input_summary,
                output_summary=output_summary,
                duration_ms=duration_ms,
                error=error,
            )
        )

    return observations


def _summarize_tool_input(tool_name: str | None, payload: dict[str, Any]) -> str:
    """Create a one-line summary of tool input."""
    if not tool_name:
        return "unknown tool"

    # Tool-specific summarizers
    summarizers: dict[str, Callable[[dict[str, Any]], str]] = {
        "bash": lambda p: f"cmd: {p.get('command', '')[:80]}",
        "pwsh": lambda p: f"cmd: {p.get('command', '')[:80]}",
        "fs_read": lambda p: f"read {p.get('path', '')}",
        "fs_write": lambda p: f"write {p.get('path', '')} ({len(p.get('content', ''))} chars)",
        "fs_list": lambda p: f"list {p.get('path', '')}",
        "web_search": lambda p: f"search: {p.get('query', '')[:80]}",
        "web_fetch": lambda p: f"fetch {p.get('url', '')}",
        "subagent": lambda p: f"delegated: {p.get('description') or p.get('task', '')[:80]}",
        "ssh_run": lambda p: f"ssh {p.get('host', '')}: {p.get('command', '')[:80]}",
        "terminal_write": lambda p: f"term input: {p.get('data', '')[:80]}",
    }

    if tool_name in summarizers:
        return summarizers[tool_name](payload)

    # Generic: first few keys
    keys = list(payload.keys())[:3]
    return f"{tool_name}({', '.join(keys)})"


def _summarize_tool_output(tool_name: str | None, payload: dict[str, Any]) -> str:
    """Create a one-line summary of tool output."""
    status = str(payload.get("status") or "completed")
    if status in ("failed", "error"):
        error = payload.get("error") or payload.get("message") or "unknown error"
        return f"failed: {error[:120]}"

    output = payload.get("output") or payload.get("result") or ""
    if isinstance(output, str):
        return output[:200]
    if isinstance(output, dict):
        keys = list(output.keys())[:5]
        return f"object with keys: {', '.join(keys)}"
    return str(output)[:200]


# ──────────────────────────────────────────────────────────────────────
# Cold-Read API (Raw Event Store Access)
# ──────────────────────────────────────────────────────────────────────


class ColdReadService:
    """Direct, low-level access to the raw event store.

    Unlike the high-level query service, this exposes the raw event
    rows with minimal transformation, for debugging and audit.
    """

    def __init__(self, db: Session):
        self.db = db

    def read_raw_events(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID | None = None,
        event_types: list[str] | None = None,
        seq_range: tuple[int, int] | None = None,
        time_range: tuple[datetime, datetime] | None = None,
        limit: int = 1000,
        offset: int = 0,
        order: Literal["asc", "desc"] = "asc",
    ) -> list[dict[str, Any]]:
        """Read raw event rows with minimal transformation.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: Optional conversation to filter.
            event_types: Event types to include.
            seq_range: Inclusive (start, end) sequence range.
            time_range: Inclusive (start, end) time range.
            limit: Maximum rows.
            offset: Pagination offset.
            order: Sequence order.

        Returns:
            Raw event rows with all fields.
        """
        from sqlalchemy import select

        conditions = [
            DeepSpaceSessionEvent.tenant_id == tenant_id,
            DeepSpaceSessionEvent.user_id == user_id,
        ]

        if conversation_id:
            conditions.append(DeepSpaceSessionEvent.conversation_id == conversation_id)

        if event_types:
            conditions.append(DeepSpaceSessionEvent.event_type.in_(event_types))

        if seq_range:
            conditions.append(DeepSpaceSessionEvent.seq.between(seq_range[0], seq_range[1]))

        if time_range:
            conditions.append(
                DeepSpaceSessionEvent.created_at.between(time_range[0], time_range[1])
            )

        order_by = (
            DeepSpaceSessionEvent.seq.asc() if order == "asc" else DeepSpaceSessionEvent.seq.desc()
        )

        rows = (
            self.db.execute(
                select(DeepSpaceSessionEvent)
                .where(*conditions)
                .order_by(order_by)
                .limit(limit)
                .offset(offset)
            )
            .scalars()
            .all()
        )

        return [_raw_event_row(event) for event in rows]

    def count_events(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID | None = None,
        event_types: list[str] | None = None,
        seq_range: tuple[int, int] | None = None,
        time_range: tuple[datetime, datetime] | None = None,
    ) -> int:
        """Count events matching criteria."""
        from sqlalchemy import select

        conditions = [
            DeepSpaceSessionEvent.tenant_id == tenant_id,
            DeepSpaceSessionEvent.user_id == user_id,
        ]

        if conversation_id:
            conditions.append(DeepSpaceSessionEvent.conversation_id == conversation_id)
        if event_types:
            conditions.append(DeepSpaceSessionEvent.event_type.in_(event_types))
        if seq_range:
            conditions.append(DeepSpaceSessionEvent.seq.between(seq_range[0], seq_range[1]))

        return (
            self.db.execute(
                select(func.count(DeepSpaceSessionEvent.id)).where(*conditions)
            ).scalar()
            or 0
        )

    def get_event_stats(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Get aggregate statistics about the event store."""
        from sqlalchemy import select

        conditions = [
            DeepSpaceSessionEvent.tenant_id == tenant_id,
            DeepSpaceSessionEvent.user_id == user_id,
        ]
        if conversation_id:
            conditions.append(DeepSpaceSessionEvent.conversation_id == conversation_id)

        total = (
            self.db.execute(
                select(func.count(DeepSpaceSessionEvent.id)).where(*conditions)
            ).scalar()
            or 0
        )

        by_type: dict[str | None, int] = {}
        for event_type, count in self.db.execute(
            select(DeepSpaceSessionEvent.event_type, func.count(DeepSpaceSessionEvent.id))
            .where(*conditions)
            .group_by(DeepSpaceSessionEvent.event_type)
        ).all():
            by_type[event_type] = int(count)

        by_tool: dict[str | None, int] = {}
        for tool_name, count in self.db.execute(
            select(DeepSpaceSessionEvent.tool_name, func.count(DeepSpaceSessionEvent.id))
            .where(*conditions)
            .where(DeepSpaceSessionEvent.tool_name.isnot(None))
            .group_by(DeepSpaceSessionEvent.tool_name)
            .order_by(func.count(DeepSpaceSessionEvent.id).desc())
            .limit(20)
        ).all():
            by_tool[tool_name] = int(count)

        return {
            "total_events": total,
            "by_type": by_type,
            "top_tools": by_tool,
        }


def _raw_event_row(event: DeepSpaceSessionEvent) -> dict[str, Any]:
    """Return one event as a raw row with all fields."""
    return {
        "id": str(event.id),
        "seq": int(event.seq),
        "event_type": str(event.event_type or ""),
        "tool_name": str(event.tool_name) if event.tool_name else None,
        "turn": event.turn,
        "step": event.step,
        "run_id": str(event.run_id) if event.run_id else None,
        "subagent_id": str(event.subagent_id) if event.subagent_id else None,
        "created_at": event.created_at.isoformat() if event.created_at else None,
        "payload": dict(event.payload or {}),
    }


# ──────────────────────────────────────────────────────────────────────
# Enhanced Lineage / Full Trace
# ──────────────────────────────────────────────────────────────────────


def trace_full_lineage(
    service: SessionQueryService,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    seq: int,
) -> dict[str, Any] | None:
    """Read the complete lineage of one event with full context.

    Returns the full ancestry chain: conversation → turn → step → events,
    plus all sibling events in the same step, plus subagent children.
    """
    if (
        service._conversation(tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id)
        is None
    ):
        return None

    target = service._event_at(
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        seq=seq,
    )
    if target is None:
        return None

    # Get full conversation events
    events = service._conversation_events(
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    # Build turn/step hierarchy
    turns: list[dict[str, Any]] = []
    current_turn: dict[str, Any] | None = None
    current_step: dict[str, Any] | None = None

    for event in events:
        if event.event_type == TURN_START:
            current_turn = {
                "seq": int(event.seq),
                "turn": event.turn,
                "start_time": event.created_at.isoformat() if event.created_at else None,
                "steps": [],
            }
            turns.append(current_turn)
            current_step = None
        elif event.event_type == TURN_END and current_turn:
            current_turn["end_time"] = event.created_at.isoformat() if event.created_at else None
            current_turn = None
            current_step = None
        elif event.event_type == STEP_START:
            current_step = {
                "seq": int(event.seq),
                "step": event.step,
                "turn": event.turn,
                "start_time": event.created_at.isoformat() if event.created_at else None,
                "events": [],
            }
            if current_turn:
                current_turn["steps"].append(current_step)
        elif event.event_type == STEP_END and current_step:
            current_step["end_time"] = event.created_at.isoformat() if event.created_at else None
            current_step = None

        # Add event to current step
        if current_step:
            current_step["events"].append(_event_row(event))

    # Find target's position
    target_turn = None
    target_step = None
    for turn in turns:
        if turn["seq"] <= target.seq:
            target_turn = turn
        for step in turn.get("steps", []):
            if step["seq"] <= target.seq:
                target_step = step

    return {
        "target": _full_event(target),
        "conversation_id": str(conversation_id),
        "turns": turns,
        "target_turn": target_turn,
        "target_step": target_step,
        "lineage": {
            "turn": target.turn,
            "step": target.step,
            "ancestors": _collect_ancestors(events, target),
        },
    }


def _collect_ancestors(
    events: list[DeepSpaceSessionEvent],
    target: DeepSpaceSessionEvent,
) -> list[dict[str, Any]]:
    """Collect all ancestor events leading to the target."""
    ancestors = []
    for event in events:
        if int(event.seq) < int(target.seq):
            ancestors.append(_event_row(event))
    return ancestors


# ──────────────────────────────────────────────────────────────────────
# Multi-Source Corpus (Events + Messages + Attachments)
# ──────────────────────────────────────────────────────────────────────


class CorpusSearchService:
    """Unified search across events, messages, and attachments."""

    def __init__(self, db: Session):
        self.db = db
        self.event_service = SessionQueryService(db)

    def search(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        query: str,
        sources: list[str] | None = None,
        conversation_id: uuid.UUID | None = None,
        limit: int = 20,
    ) -> dict[str, list[dict[str, Any]]]:
        """Search across all available sources.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            query: The search text.
            sources: Sources to search ("events", "messages", "attachments").
            conversation_id: Optional conversation to limit search.
            limit: Max results per source.

        Returns:
            Dict with results per source.
        """
        available_sources = sources or ["events", "messages", "attachments"]
        results = {}

        if "events" in available_sources:
            if conversation_id:
                events = self.event_service.search_events(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    query=query,
                    limit=limit,
                )
            else:
                events = self.event_service.search_sessions(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    query=query,
                    limit=limit,
                )
            results["events"] = events or []

        if "messages" in available_sources:
            results["messages"] = self._search_messages(
                tenant_id, user_id, query, conversation_id, limit
            )

        if "attachments" in available_sources:
            results["attachments"] = self._search_attachments(
                tenant_id, user_id, query, conversation_id, limit
            )

        return results

    def _search_messages(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        query: str,
        conversation_id: uuid.UUID | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        from sqlalchemy import Text, cast, or_, select

        from app.deepspace.models.message import Message

        terms = _terms(query)
        if not terms:
            return []

        conditions = [
            Conversation.tenant_id == tenant_id,
            Conversation.user_id == user_id,
        ]
        if conversation_id:
            conditions.append(Message.conversation_id == conversation_id)

        # Build LIKE conditions for message content
        from sqlalchemy import bindparam

        patterns = [_like_pattern(term) for term in terms]
        conditions.append(
            or_(
                *[
                    cast(Message.content, Text).ilike(
                        bindparam(f"msg_term_{i}"), escape=_LIKE_ESCAPE
                    )
                    for i in range(len(patterns))
                ]
            )
        )

        params = {f"msg_term_{i}": _like_pattern(term) for i, term in enumerate(terms)}

        rows = (
            self.db.execute(
                select(Message)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(*conditions)
                .order_by(Message.created_at.desc())
                .limit(limit)
                .params(**params)
            )
            .scalars()
            .all()
        )

        return [
            {
                "id": str(msg.id),
                "conversation_id": str(msg.conversation_id),
                "role": msg.role,
                "content_excerpt": _excerpt(msg.content or "", terms),
                "created_at": msg.created_at.isoformat() if msg.created_at else None,
            }
            for msg in rows
        ]

    def _search_attachments(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        query: str,
        conversation_id: uuid.UUID | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        # Attachment search would require attachment model
        # Placeholder for future implementation
        return []


# ──────────────────────────────────────────────────────────────────────
# Rich Export (Markdown + CSS + Archive)
# ──────────────────────────────────────────────────────────────────────

EXPORT_CSS = """
/* DeepSpace Session Export Styles */
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; max-width: 900px; margin: 0 auto; padding: 2rem; color: #1a1a2e; }
h1 { color: #0f172a; border-bottom: 2px solid #3b82f6; padding-bottom: 0.5rem; }
h2 { color: #1e293b; margin-top: 2rem; }
h3 { color: #334155; }
code { background: #f1f5f9; padding: 0.125rem 0.375rem; border-radius: 0.25rem; font-family: 'Monaco', 'Menlo', monospace; font-size: 0.875rem; }
pre { background: #0f172a; color: #e2e8f0; padding: 1rem; border-radius: 0.5rem; overflow-x: auto; }
pre code { background: transparent; padding: 0; }
.blockquote { border-left: 4px solid #3b82f6; padding-left: 1rem; margin: 1rem 0; color: #475569; font-style: italic; }
.turn { border: 1px solid #e2e8f0; border-radius: 0.5rem; margin: 1.5rem 0; padding: 1rem; background: #fafafa; }
.turn-header { display: flex; gap: 1rem; margin-bottom: 0.5rem; font-size: 0.875rem; color: #64748b; }
.turn-number { font-weight: 600; color: #3b82f6; }
.tool-call { background: #fef3c7; border-left: 4px solid #f59e0b; padding: 0.75rem; margin: 0.5rem 0; border-radius: 0.25rem; }
.tool-call-header { font-weight: 600; color: #92400e; margin-bottom: 0.25rem; }
.tool-result { background: #dcfce7; border-left: 4px solid #22c55e; padding: 0.75rem; margin: 0.5rem 0; border-radius: 0.25rem; }
.tool-result.error { background: #fef2f2; border-left-color: #ef4444; }
.tool-name { font-family: monospace; font-weight: 600; color: #1e293b; }
.tool-status { font-size: 0.75rem; padding: 0.125rem 0.375rem; border-radius: 0.25rem; }
.tool-status.completed { background: #dcfce7; color: #166534; }
.tool-status.failed { background: #fef2f2; color: #991b1b; }
.tool-status.called { background: #fef3c7; color: #92400e; }
.tool-duration { font-size: 0.75rem; color: #64748b; }
.observation { background: #f0f9ff; border-left: 4px solid #0ea5e9; padding: 0.75rem; margin: 0.5rem 0; border-radius: 0.25rem; }
.observation-input { color: #0369a1; }
.observation-output { color: #0c4a6e; }
.observation-error { color: #dc2626; }
.event-row { font-family: monospace; font-size: 0.8125rem; padding: 0.25rem 0; border-bottom: 1px solid #f1f5f9; }
.event-seq { color: #94a3b8; }
.event-type { color: #3b82f6; font-weight: 600; }
.event-tool { color: #8b5cf6; }
.attachment { border: 1px dashed #cbd5e1; padding: 0.75rem; border-radius: 0.25rem; margin: 0.5rem 0; }
.attachment-name { font-weight: 600; }
.attachment-meta { font-size: 0.8125rem; color: #64748b; }
.export-meta { background: #f8fafc; border: 1px solid #e2e8f0; padding: 1rem; border-radius: 0.5rem; margin-bottom: 2rem; font-size: 0.875rem; color: #475569; }
"""

EXPORT_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>{css}</style>
</head>
<body>
    <div class="export-meta">
        <strong>Conversation:</strong> {title} ({conversation_id})<br>
        <strong>Exported:</strong> {export_time}<br>
        <strong>Turns:</strong> {turn_count} | <strong>Messages:</strong> {message_count} | <strong>Events:</strong> {event_count}
    </div>
    {content}
</body>
</html>
"""


class SessionExportService:
    """Export conversations to rich HTML/Markdown with styling."""

    def __init__(self, db: Session):
        self.db = db
        self.query_service = SessionQueryService(db)

    async def export_conversation(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        format: Literal["html", "markdown"] = "html",
        include_events: bool = True,
        include_observations: bool = True,
    ) -> tuple[str, str]:
        """Export a conversation to HTML or Markdown.

        Returns:
            Tuple of (content, filename)
        """
        # Get conversation metadata
        conversation = self.query_service._conversation(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
        )
        if conversation is None:
            raise ValueError("Conversation not found")

        # Get trace with turns
        trace = await self.query_service.get_trace(
            conversation_id=conversation_id,
            auth=type("Auth", (), {"tenant_id": tenant_id, "user_id": user_id})(),
            limit=1000,
        )
        if trace is None:
            raise ValueError("Could not read conversation trace")

        # Get events if requested
        events = []
        observations = []
        if include_events:
            events = self.query_service._conversation_events(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
            )

        if include_observations and events:
            from app.deepspace.services.session_query import extract_tool_observations

            observations = extract_tool_observations(events)

        if format == "html":
            content = self._render_html(trace, events, observations, conversation)
            filename = (
                f"deepspace-{conversation.id}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.html"
            )
            return content, filename
        else:
            content = self._render_markdown(trace, events, observations, conversation)
            filename = (
                f"deepspace-{conversation.id}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.md"
            )
            return content, filename

    def _render_html(
        self,
        trace: dict[str, Any],
        events: list[DeepSpaceSessionEvent],
        observations: list[Any],
        conversation: Conversation,
    ) -> str:
        parts = []

        # Turns
        for turn in trace.get("turns", []):
            parts.append('<div class="turn">')
            parts.append('<div class="turn-header">')
            parts.append(f'<span class="turn-number">Turn {turn["turn"]}</span>')
            parts.append(
                f'<span class="tool-duration">{len(turn.get("tool_calls", []))} tool calls</span>'
            )
            parts.append("</div>")

            if turn.get("user_message"):
                parts.append(
                    f'<div class="blockquote"><strong>User:</strong> {self._escape(turn["user_message"])}</div>'
                )

            if turn.get("assistant_message"):
                parts.append(
                    f'<div class="blockquote"><strong>Assistant:</strong> {self._escape(turn["assistant_message"])}</div>'
                )

            for tc in turn.get("tool_calls", []):
                status = tc.get("status", "called")
                parts.append('<div class="tool-call">')
                parts.append('<div class="tool-call-header">')
                parts.append(f'<span class="tool-name">{self._escape(tc.get("tool", ""))}</span>')
                parts.append(f'<span class="tool-status {status}">{status}</span>')
                parts.append("</div>")
                parts.append("</div>")

            parts.append("</div>")

        # Observations
        if observations:
            parts.append("<h2>Tool Observations</h2>")
            for obs in observations:
                parts.append('<div class="observation">')
                parts.append(
                    f'<div class="observation-input"><strong>{self._escape(obs.tool_name)}</strong> (turn {obs.turn}, step {obs.step})</div>'
                )
                parts.append(
                    f'<div class="observation-input">Input: {self._escape(obs.input_summary)}</div>'
                )
                if obs.output_summary:
                    parts.append(
                        f'<div class="observation-output">Output: {self._escape(obs.output_summary)}</div>'
                    )
                if obs.error:
                    parts.append(
                        f'<div class="observation-error">Error: {self._escape(obs.error)}</div>'
                    )
                if obs.duration_ms:
                    parts.append(f'<div class="tool-duration">Duration: {obs.duration_ms}ms</div>')
                parts.append("</div>")

        # Events timeline
        parts.append("<h2>Event Timeline</h2>")
        for event in events:
            parts.append('<div class="event-row">')
            parts.append(f'<span class="event-seq">#{event.seq}</span>')
            parts.append(f'<span class="event-type">{event.event_type}</span>')
            if event.tool_name:
                parts.append(f'<span class="event-tool">{event.tool_name}</span>')
            parts.append(f"<span>{_summary(event)}</span>")
            parts.append("</div>")

        content = "\n".join(parts)

        return EXPORT_HTML_TEMPLATE.format(
            title=f"DeepSpace Conversation: {conversation.title or 'Untitled'}",
            conversation_id=str(conversation.id),
            export_time=datetime.now(UTC).isoformat(),
            turn_count=len(trace.get("turns", [])),
            message_count=trace.get("message_count", 0),
            event_count=len(events),
            css=EXPORT_CSS,
            content=content,
        )

    def _render_markdown(
        self,
        trace: dict[str, Any],
        events: list[DeepSpaceSessionEvent],
        observations: list[Any],
        conversation: Conversation,
    ) -> str:
        lines = [
            f"# {conversation.title or 'Untitled Conversation'}",
            f"**ID:** {conversation.id}  ",
            f"**Exported:** {datetime.now(UTC).isoformat()}  ",
            f"**Turns:** {len(trace.get('turns', []))}  ",
            f"**Events:** {len(events)}  ",
            "",
        ]

        for turn in trace.get("turns", []):
            lines.append(f"## Turn {turn['turn']}")
            if turn.get("user_message"):
                lines.append(f"> **User:** {turn['user_message']}")
                lines.append("")
            if turn.get("assistant_message"):
                lines.append(f"> **Assistant:** {turn['assistant_message']}")
                lines.append("")

            for tc in turn.get("tool_calls", []):
                status = tc.get("status", "called")
                lines.append(f"**Tool:** `{tc.get('tool', '')}` — *{status}*")
                lines.append("")

        if observations:
            lines.append("## Tool Observations")
            lines.append("")
            for obs in observations:
                lines.append(f"### {obs.tool_name} (turn {obs.turn}, step {obs.step})")
                lines.append(f"**Input:** {obs.input_summary}")
                if obs.output_summary:
                    lines.append(f"**Output:** {obs.output_summary}")
                if obs.error:
                    lines.append(f"**Error:** {obs.error}")
                if obs.duration_ms:
                    lines.append(f"**Duration:** {obs.duration_ms}ms")
                lines.append("")

        lines.append("## Event Timeline")
        for event in events:
            lines.append(
                f"- `#{event.seq}` **{event.event_type}** {event.tool_name or ''} — {_summary(event)}"
            )

        return "\n".join(lines)

    @staticmethod
    def _escape(text: str) -> str:
        return (
            text.replace("&", "&")
            .replace("<", "<")
            .replace(">", ">")
            .replace("\u0022", "\u0022")
            .replace("\u0027", "\u0027")
        )


# ──────────────────────────────────────────────────────────────────────
# Archive Export (Zip with HTML + JSON + Assets)
# ──────────────────────────────────────────────────────────────────────

# async def export_conversation_archive(
#     db: Session,
#     *,
#     tenant_id: uuid.UUID,
#     user_id: uuid.UUID,
#     conversation_id: uuid.UUID,
# ) -> bytes:
#     """Export a conversation as a ZIP archive with HTML, JSON, and assets.
#     """
#     import io
#     import zipfile
#     from datetime import UTC, datetime
#
#     export_service = SessionExportService(db)
#     html_content, _ = export_service.export_conversation(
#         tenant_id=tenant_id,
#         user_id=user_id,
#         conversation_id=conversation_id,
#         format="html",
#     )
#
#     # Also export raw JSON
#     from app.deepspace.services.session_query import SessionQueryService
#     query_service = SessionQueryService(db)
#     trace = query_service.get_trace(
#         conversation_id=conversation_id,
#         auth=type("Auth", (), {"tenant_id": tenant_id, "user_id": user_id})(),
#         limit=1000,
#     )
#     events = query_service._conversation_events(
#         tenant_id=tenant_id,
#         user_id=user_id,
#         conversation_id=conversation_id,
#     )
#
#     json_data = {
#         "conversation_id": str(conversation_id),
#         "exported_at": datetime.now(UTC).isoformat(),
#         "trace": trace,
#         "events": [_full_event(e) for e in events],
#     }
#
#     zip_buffer = io.BytesIO()
#     with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
#         zf.writestr("conversation.html", html_content)
#         zf.writestr("conversation.json", json.dumps(json_data, indent=2))
#         zf.writestr("README.txt", f"DeepSpace Conversation Export\nConversation: {conversation_id}\nExported: {datetime.now(UTC).isoformat()}")
#
#     return zip_buffer.getvalue()


__all__ = [
    "EXCERPT_CHARS",
    "MAX_EVENT_RESULTS",
    "MAX_SEARCH_SCAN",
    "MAX_SESSION_RESULTS",
    "MAX_TRACE_EVENTS",
    "SessionQueryService",
    # FTS5
    "FTS5_TABLE",
    "initialize_fts5",
    "populate_fts5",
    "fts5_search_sessions",
    "fts5_search_events",
    # Observations
    "ToolObservation",
    "extract_tool_observations",
    # Cold read
    "ColdReadService",
    "_raw_event_row",
    # Lineage
    "trace_full_lineage",
    # Corpus
    "CorpusSearchService",
    # Export
    "EXPORT_CSS",
    "EXPORT_HTML_TEMPLATE",
    "SessionExportService",
    # New types
    "ToolObservation",
    "ColdReadService",
    "CorpusSearchService",
    "SessionExportService",
]
