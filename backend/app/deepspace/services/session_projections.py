"""Projections over DeepSpace's typed session event log.

A projection is a read model the runtime asks instead of replaying the whole
log.  Four projections exist today: the turn boundary, the tool timeline, the
delegation tree, and the standing todo list.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deepspace.models.session_event import DeepSpaceSessionEvent
from app.deepspace.models.subagent import DeepSpaceSubagentRun
from app.deepspace.services.session_events import (
    STEP_END,
    STEP_START,
    SUBAGENT_RESULT,
    SUBAGENT_SPAWN,
    TODO_WRITE,
    TOOL_CALL,
    TOOL_RESULT,
    TURN_END,
    TURN_START,
)


@dataclass(frozen=True, slots=True)
class TurnBoundary:
    """Where a conversation's turn and step boundaries currently stand."""

    #: The turn that is open, when one is.
    open_turn: int | None = None
    #: The last turn that started.
    last_turn: int = 0
    #: The last step that started, when one did.
    open_step: int | None = None
    #: The last step that ended, when one did.
    last_completed_step: int | None = None
    #: The number of turns that have ended.
    completed_turns: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable projection of these boundaries."""
        return {
            "open_turn": self.open_turn,
            "last_turn": self.last_turn,
            "open_step": self.open_step,
            "last_completed_step": self.last_completed_step,
            "completed_turns": self.completed_turns,
        }


@dataclass(frozen=True, slots=True)
class ToolTimelineEntry:
    """One tool call in a conversation's timeline."""

    seq: int
    turn: int | None
    step: int | None
    tool_name: str
    status: str
    payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable projection of this entry."""
        return {
            "seq": self.seq,
            "turn": self.turn,
            "step": self.step,
            "tool_name": self.tool_name,
            "status": self.status,
            "payload": self.payload,
        }


def turn_boundary(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> TurnBoundary:
    """Read a conversation's current turn and step boundaries.

    Args:
        db: The database session.
        tenant_id: The owning tenant.
        user_id: The owning user.
        conversation_id: The conversation to read.

    Returns:
        The boundaries.
    """
    events = list(
        db.execute(
            select(DeepSpaceSessionEvent)
            .where(
                DeepSpaceSessionEvent.tenant_id == tenant_id,
                DeepSpaceSessionEvent.user_id == user_id,
                DeepSpaceSessionEvent.conversation_id == conversation_id,
            )
            .order_by(DeepSpaceSessionEvent.seq.asc())
        )
        .scalars()
        .all()
    )
    open_turn: int | None = None
    last_turn = 0
    open_step: int | None = None
    last_completed_step: int | None = None
    completed_turns = 0
    for event in events:
        if event.event_type == TURN_START:
            open_turn = int(event.turn or 0)
            last_turn = max(last_turn, open_turn)
        elif event.event_type == TURN_END:
            open_turn = None
            completed_turns += 1
        elif event.event_type == STEP_START:
            open_step = int(event.step or 0)
        elif event.event_type == STEP_END:
            last_completed_step = int(event.step or 0)
            if open_step == last_completed_step:
                open_step = None
    return TurnBoundary(
        open_turn=open_turn,
        last_turn=last_turn,
        open_step=open_step,
        last_completed_step=last_completed_step,
        completed_turns=completed_turns,
    )


@dataclass(frozen=True, slots=True)
class TodoProjection:
    """The conversation's current whole-list todo snapshot.

    Attributes:
        todos: The latest list, or ``None`` before the first write and after
            the next turn begins.
        state_version: The wire version of the projection.
        summary_excluded: Whether a session summary must omit the list.
    """

    todos: list[dict[str, str]] | None = None
    state_version: int = 2
    summary_excluded: bool = True

    def counts(self) -> dict[str, int]:
        """Count the three portable statuses of the current list."""
        todos = self.todos or ()
        return {
            "pending": sum(1 for item in todos if item.get("status") == "pending"),
            "inProgress": sum(1 for item in todos if item.get("status") == "in_progress"),
            "completed": sum(1 for item in todos if item.get("status") == "completed"),
        }

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable projection."""
        return {
            "todos": self.todos,
            "counts": self.counts(),
            "state_version": self.state_version,
            "summary_excluded": self.summary_excluded,
        }


def fold_todos(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> TodoProjection:
    """Fold the conversation's ``todo/write`` log into one whole-list snapshot.

    The fold is last-wins: every write carries the complete replacement list.
    A later ``turn/start`` clears the standing plan so a finished checklist
    never appears current in the next turn; ``turn/end`` deliberately keeps it
    visible until then.

    Args:
        db: The database session.
        tenant_id: The owning tenant.
        user_id: The owning user.
        conversation_id: The conversation to read.

    Returns:
        The current snapshot.
    """
    events = (
        db.execute(
            select(DeepSpaceSessionEvent)
            .where(
                DeepSpaceSessionEvent.tenant_id == tenant_id,
                DeepSpaceSessionEvent.user_id == user_id,
                DeepSpaceSessionEvent.conversation_id == conversation_id,
                DeepSpaceSessionEvent.event_type.in_([TODO_WRITE, TURN_START]),
            )
            .order_by(DeepSpaceSessionEvent.seq.asc())
        )
        .scalars()
        .all()
    )
    todos: list[dict[str, str]] | None = None
    for event in events:
        if event.event_type == TURN_START:
            todos = None
            continue
        payload = dict(event.payload or {})
        raw = payload.get("todos")
        if isinstance(raw, list):
            todos = [
                {
                    "content": str(item.get("content") or ""),
                    "status": str(item.get("status") or "pending"),
                }
                for item in raw
                if isinstance(item, dict)
            ]
    return TodoProjection(todos=todos)


def tool_timeline(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    limit: int = 200,
) -> list[ToolTimelineEntry]:
    """Read a conversation's tool timeline.

    Args:
        db: The database session.
        tenant_id: The owning tenant.
        user_id: The owning user.
        conversation_id: The conversation to read.
        limit: The largest number of entries to return.

    Returns:
        The timeline entries, oldest first.
    """
    events = (
        db.execute(
            select(DeepSpaceSessionEvent)
            .where(
                DeepSpaceSessionEvent.tenant_id == tenant_id,
                DeepSpaceSessionEvent.user_id == user_id,
                DeepSpaceSessionEvent.conversation_id == conversation_id,
                DeepSpaceSessionEvent.event_type.in_([TOOL_CALL, TOOL_RESULT]),
            )
            .order_by(DeepSpaceSessionEvent.seq.asc())
            .limit(max(1, min(limit, 2_000)))
        )
        .scalars()
        .all()
    )
    entries: list[ToolTimelineEntry] = []
    for event in events:
        entries.append(
            ToolTimelineEntry(
                seq=int(event.seq),
                turn=event.turn,
                step=event.step,
                tool_name=str(event.tool_name or ""),
                status="result" if event.event_type == TOOL_RESULT else "call",
                payload=dict(event.payload or {}),
            )
        )
    return entries


def delegation_tree(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Read a conversation's delegation tree.

    Args:
        db: The database session.
        tenant_id: The owning tenant.
        user_id: The owning user.
        conversation_id: The conversation to read.

    Returns:
        One entry per child agent, with its parent and its depth.
    """
    from app.deepspace.services.subagent_runner import child_reported_status

    children = (
        db.execute(
            select(DeepSpaceSubagentRun)
            .where(
                DeepSpaceSubagentRun.tenant_id == tenant_id,
                DeepSpaceSubagentRun.user_id == user_id,
                DeepSpaceSubagentRun.conversation_id == conversation_id,
            )
            .order_by(DeepSpaceSubagentRun.created_at.asc())
        )
        .scalars()
        .all()
    )
    return [
        {
            "agent_id": str(child.id),
            "parent_agent_id": str(child.parent_agent_id) if child.parent_agent_id else None,
            "parent_run_id": str(child.parent_run_id) if child.parent_run_id else None,
            "label": child.label,
            "mode": child.mode,
            "origin": child.origin,
            "status": child_reported_status(child),
            "depth": child.delegation_depth,
            "turn_count": child.turn_count,
        }
        for child in children
    ]


def delegation_events(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Read a conversation's delegation events.

    Args:
        db: The database session.
        tenant_id: The owning tenant.
        user_id: The owning user.
        conversation_id: The conversation to read.

    Returns:
        The delegation events, oldest first.
    """
    events = (
        db.execute(
            select(DeepSpaceSessionEvent)
            .where(
                DeepSpaceSessionEvent.tenant_id == tenant_id,
                DeepSpaceSessionEvent.user_id == user_id,
                DeepSpaceSessionEvent.conversation_id == conversation_id,
                DeepSpaceSessionEvent.event_type.in_([SUBAGENT_SPAWN, SUBAGENT_RESULT]),
            )
            .order_by(DeepSpaceSessionEvent.seq.asc())
        )
        .scalars()
        .all()
    )
    return [
        {
            "seq": int(event.seq),
            "event_type": event.event_type,
            "subagent_id": str(event.subagent_id) if event.subagent_id else None,
            "payload": dict(event.payload or {}),
        }
        for event in events
    ]
