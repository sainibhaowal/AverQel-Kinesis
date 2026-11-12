"""The typed session event log for a DeepSpace conversation.

The frame log in ``run_events`` stores the opaque SSE text a browser
replays.  This module stores the same run as structured records, so a
projection can ask "which turn is open", "which tools ran", and "what did
the child agents do" without parsing frames.

The log is additive.  A deployment that does not write typed events loses
nothing but the projections.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.deepspace.models.session_event import DeepSpaceSessionEvent

logger = logging.getLogger(__name__)
_TELEMETRY_QUEUE_KEY = "deepspace_committed_session_events"


def _queue_telemetry_event(session: Any, event_record: DeepSpaceSessionEvent) -> None:
    """Queue only non-content event metadata until the transaction commits."""
    info = getattr(session, "info", None)
    if not isinstance(info, dict):
        return
    info.setdefault(_TELEMETRY_QUEUE_KEY, []).append(
        {
            "conversation_id": str(event_record.conversation_id),
            "type": event_record.event_type,
            "time": int(time.time() * 1000),
            "sequence": event_record.seq,
            "turn": event_record.turn,
            "step": event_record.step,
            "tool": event_record.tool_name,
        }
    )


def _forward_committed_session_events(session: Session) -> None:
    """Forward queued summaries after commit; telemetry must never break commits."""
    summaries = session.info.pop(_TELEMETRY_QUEUE_KEY, [])
    if not summaries:
        return
    try:
        from app.deepspace.services.session_telemetry import get_session_telemetry

        telemetry = get_session_telemetry()
        for summary in summaries:
            conversation_id = summary.pop("conversation_id")
            try:
                telemetry.record_session_event(conversation_id=conversation_id, event=summary)
            except Exception:  # noqa: BLE001 - telemetry is best effort
                logger.warning(
                    "Could not queue committed session event for telemetry", exc_info=True
                )
    except Exception:  # noqa: BLE001 - telemetry is best effort
        logger.warning("Could not initialize session-event telemetry", exc_info=True)


def _discard_rolled_back_session_events(session: Session) -> None:
    """Do not report session events whose database transaction rolled back."""
    session.info.pop(_TELEMETRY_QUEUE_KEY, None)


event.listen(Session, "after_commit", _forward_committed_session_events)
event.listen(Session, "after_rollback", _discard_rolled_back_session_events)

#: The version of the typed event log's record format.
#:
#: A structural change to the event names or the payload shape bumps this
#: number, so a reader can tell an old log from a new one.
SESSION_FORMAT_VERSION = 1

#: The event name for the start of a turn.
TURN_START = "turn/start"

#: The event name for the end of a turn.
TURN_END = "turn/end"

#: The event name for the start of a step.
STEP_START = "step/start"

#: The event name for the end of a step.
STEP_END = "step/end"

#: The event name for a tool call.
TOOL_CALL = "tool/call"

#: The event name for a tool result.
TOOL_RESULT = "tool/result"

#: The event name for a whole-list todo snapshot.
TODO_WRITE = "todo/write"

#: The event name for a delegated child.
SUBAGENT_SPAWN = "subagent/spawn"

#: The event name for a child activation that has actually started running.
SUBAGENT_START = "subagent/start"

#: The event name for an independently selected child preset.
SUBAGENT_PRESET = "subagent/preset"

#: The event name for one child activation's terminal result.
SUBAGENT_END = "subagent/end"

#: The event name for a delegated child's result.
SUBAGENT_RESULT = "subagent/result"

#: The event name for the assistant's message.
ASSISTANT_MESSAGE = "assistant/message"

#: The event name for a child agent's descriptor.
SUBAGENT_DESCRIPTOR = "subagent/descriptor"

#: The event name for a plan-mode state change.
PLAN_MODE = "plan/mode"

#: The event name for a sandbox-mode state change.
SANDBOX_MODE = "sandbox/mode"

#: The event name for an agent-preset state change.
PRESET_MODE = "preset/mode"

#: The event name for a workspace selection.
WORKSPACE_SELECTED = "workspace/selected"

#: The event name for a conversation's model selection.
MODEL_SELECTION = "model/selection"

#: The event name for a hook that ran around a tool call.
HOOK_INVOKED = "hook/invoked"

#: The event name for what a hook decided.
HOOK_RESULT = "hook/result"

#: The event name for a named teammate that was created.
TEAM_MEMBER_SPAWNED = "team/member-spawned"

#: The event name for a named teammate that was interrupted.
TEAM_MEMBER_INTERRUPTED = "team/member-interrupted"

#: The event name for a named teammate lifecycle change.
TEAM_MEMBER_STATE = "team/member-state"

#: The event name for a message that went to a named teammate.
TEAM_MESSAGE = "team/message"

#: The event name for a change on the shared team task board.
TEAM_TASK = "team/task"

#: The event name for enabling or disabling agent teams for a conversation.
TEAM_ENABLED = "team/enabled"

#: The event name for a plan the agent proposed.
PLAN_SUBMITTED = "plan/submitted"

#: The event name for the user's decision about a proposed plan.
PLAN_REVIEWED = "plan/reviewed"

#: The event name for session-level feedback.
FEEDBACK_GIVEN = "feedback/given"


class SessionEventStore:
    """Append and read the typed events of one conversation."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def next_sequence(self, conversation_id: uuid.UUID) -> int:
        """Return the next sequence number for a conversation.

        Args:
            conversation_id: The conversation the event belongs to.

        Returns:
            The next sequence number, starting at one.
        """
        current = self.db.execute(
            select(func.max(DeepSpaceSessionEvent.seq)).where(
                DeepSpaceSessionEvent.conversation_id == conversation_id
            )
        ).scalar()
        return int(current or 0) + 1

    def append(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any] | None = None,
        run_id: uuid.UUID | None = None,
        subagent_id: uuid.UUID | None = None,
        turn: int | None = None,
        step: int | None = None,
        tool_name: str | None = None,
    ) -> DeepSpaceSessionEvent:
        """Append one typed event.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the event belongs to.
            event_type: The event name.
            payload: The event's data.
            run_id: The run that produced the event.
            subagent_id: The delegated child the event belongs to.
            turn: The turn the event belongs to.
            step: The step the event belongs to.
            tool_name: The tool the event concerns.

        Returns:
            The appended event.
        """
        event = DeepSpaceSessionEvent(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            run_id=run_id,
            subagent_id=subagent_id,
            seq=self.next_sequence(conversation_id),
            event_type=event_type,
            turn=turn,
            step=step,
            tool_name=tool_name,
            payload=dict(payload or {}),
        )
        self.db.add(event)
        _queue_telemetry_event(self.db, event)
        # The session factory does not autoflush, so the next sequence number
        # must see this row. Without the flush, every event of one batch
        # would carry the same sequence.
        flush = getattr(self.db, "flush", None)
        if callable(flush):
            flush()
        return event

    def append_many(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        events: list[dict[str, Any]],
    ) -> list[DeepSpaceSessionEvent]:
        """Append several typed events in one transaction.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the events belong to.
            events: The event descriptions, each with an ``event_type``.

        Returns:
            The appended events.
        """
        appended: list[DeepSpaceSessionEvent] = []
        for description in events:
            appended.append(
                self.append(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    event_type=str(description.get("event_type") or ""),
                    payload=description.get("payload"),
                    run_id=description.get("run_id"),
                    subagent_id=description.get("subagent_id"),
                    turn=description.get("turn"),
                    step=description.get("step"),
                    tool_name=description.get("tool_name"),
                )
            )
        return appended

    def events(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        limit: int = 500,
    ) -> list[DeepSpaceSessionEvent]:
        """Read a conversation's typed events in order.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.
            limit: The largest number of events to return.

        Returns:
            The events, oldest first.
        """
        return list(
            self.db.execute(
                select(DeepSpaceSessionEvent)
                .where(
                    DeepSpaceSessionEvent.tenant_id == tenant_id,
                    DeepSpaceSessionEvent.user_id == user_id,
                    DeepSpaceSessionEvent.conversation_id == conversation_id,
                )
                .order_by(DeepSpaceSessionEvent.seq.asc())
                .limit(max(1, min(limit, 5_000)))
            )
            .scalars()
            .all()
        )
