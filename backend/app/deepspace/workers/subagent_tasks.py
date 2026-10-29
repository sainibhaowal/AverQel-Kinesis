"""The background task that drives a delegated child agent.

A continuable child receives messages after its parent's turn has moved on,
so the turn has to run outside the parent's tool call.  This task owns that
turn: it claims the child's queued messages, runs one child turn, and records
the answer on the child.

The task is durable, so a child continued in a later process — after a
restart, or from another worker's turn — resumes with the composition its
descriptor recorded.

A named teammate is a continuable child that a conversation addresses by
name.  The task keeps the teammate's own records in step with the turn: the
teammate is running while its turn runs, the messages that waited for the
teammate are delivered when the turn picks them up, and the teammate is
finished with the work when the turn ends.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext
from app.core.config import get_settings
from app.deepspace.models.agent_team import (
    TEAM_MEMBER_STATE_FINISHED,
    TEAM_MEMBER_STATE_IDLE,
    TEAM_MEMBER_STATE_RUNNING,
    TEAM_MESSAGE_STATE_DELIVERED,
    TEAM_MESSAGE_STATE_QUEUED,
    TeamMember,
    TeamMessage,
)
from app.deepspace.models.subagent import DeepSpaceSubagentRun
from app.deepspace.services.agent_team import notify_team_change
from app.deepspace.services.chat_service import DeepSpaceChatService
from app.deepspace.services.session_events import TEAM_MEMBER_STATE, TEAM_MESSAGE, SessionEventStore
from app.deepspace.services.subagent_runner import run_child_turn
from app.platform.database.session import get_session_factory, set_db_tenant_context
from app.platform.worker.celery_app import celery_app

logger = logging.getLogger(__name__)

#: The largest number of turns one task runs for one child.
#:
#: A message that arrives while a turn runs waits in the child's own queue,
#: and the next turn of the same task answers it, so a teammate that is busy
#: does not need a new dispatch for every message it receives.
MAX_TURNS_PER_TASK = 8


def _claim_pending_messages(session: Session, child: DeepSpaceSubagentRun) -> list[str]:
    """Take the child's queued messages and clear the queue.

    The queue is read from the row rather than from the loaded record, so a
    message that another process queued after this task started is still
    seen.  The row is read under a lock, so a message that is queued while
    this task claims the queue is not lost.  The lock is held until the
    caller ends the transaction, so the caller commits before the turn runs
    and a message that is sent during the turn does not wait for it.

    Args:
        session: The database session.
        child: The durable child record.

    Returns:
        The messages the turn must answer.
    """
    raw = session.execute(
        select(DeepSpaceSubagentRun.pending_messages)
        .where(DeepSpaceSubagentRun.id == child.id)
        .with_for_update()
    ).scalar()
    pending = raw if isinstance(raw, list) else []
    messages = [str(item.get("message") or "") for item in pending if isinstance(item, dict)]
    messages = [message for message in messages if message]
    child.pending_messages = []
    return messages


def _teammate_of(session: Session, child: DeepSpaceSubagentRun) -> TeamMember | None:
    """Read the named teammate one child run works.

    Args:
        session: The database session.
        child: The durable child record.

    Returns:
        The teammate, or None when the child is a plain delegated child that
        no teammate is named after.
    """
    return session.execute(
        select(TeamMember).where(
            TeamMember.child_id == child.id,
            TeamMember.tenant_id == child.tenant_id,
            TeamMember.user_id == child.user_id,
            TeamMember.conversation_id == child.conversation_id,
        )
    ).scalar_one_or_none()


def _deliver_queued_messages(
    session: Session,
    member: TeamMember,
    *,
    run_id: uuid.UUID | None,
) -> None:
    """Deliver the messages that waited for a teammate.

    The turn that is about to run answers the messages the teammate's queue
    holds, so they are no longer waiting.

    Args:
        session: The database session.
        member: The teammate that is about to work.
        run_id: The run the teammate belongs to.
    """
    rows = (
        session.execute(
            select(TeamMessage)
            .execution_options(populate_existing=True)
            .where(
                TeamMessage.tenant_id == member.tenant_id,
                TeamMessage.user_id == member.user_id,
                TeamMessage.conversation_id == member.conversation_id,
                TeamMessage.recipient == member.name,
                TeamMessage.state == TEAM_MESSAGE_STATE_QUEUED,
            )
            .order_by(TeamMessage.created_at.asc())
        )
        .scalars()
        .all()
    )
    if not rows:
        return
    now = datetime.now(UTC)
    events = SessionEventStore(session)
    for row in rows:
        row.state = TEAM_MESSAGE_STATE_DELIVERED
        row.delivered_at = now
        events.append(
            tenant_id=row.tenant_id,
            user_id=row.user_id,
            conversation_id=row.conversation_id,
            event_type=TEAM_MESSAGE,
            payload={
                "message_id": str(row.id),
                "from": row.sender,
                "to": row.recipient,
                "state": row.state,
            },
            run_id=run_id,
            tool_name="team_send_message",
        )
    session.commit()
    notify_team_change(
        tenant_id=member.tenant_id,
        user_id=member.user_id,
        conversation_id=member.conversation_id,
    )


def _finish_teammate(member: TeamMember, stop_reason: str) -> None:
    """Record how a teammate's turn ended.

    A teammate that answered is finished with the work it was given, and a
    teammate that did not is idle again, so the agent that owns the team can
    tell the two apart and send the work again.

    Args:
        member: The teammate that worked.
        stop_reason: Why the turn ended.
    """
    member.state = (
        TEAM_MEMBER_STATE_FINISHED if stop_reason == "completed" else TEAM_MEMBER_STATE_IDLE
    )


def _release_child(
    session: Session,
    child: DeepSpaceSubagentRun | None,
    teammate: TeamMember | None,
) -> None:
    """Release a child whose turn ended in a failure.

    Args:
        session: The database session.
        child: The durable child record, when the turn found one.
        teammate: The teammate the child works, when the child is a teammate.
    """
    try:
        if child is not None:
            child.busy = False
        if teammate is not None:
            teammate.state = TEAM_MEMBER_STATE_IDLE
            SessionEventStore(session).append(
                tenant_id=teammate.tenant_id,
                user_id=teammate.user_id,
                conversation_id=teammate.conversation_id,
                event_type=TEAM_MEMBER_STATE,
                payload={
                    "member_id": str(teammate.id),
                    "name": teammate.name,
                    "state": teammate.state,
                    "stop_reason": "failed",
                },
                run_id=child.parent_run_id if child is not None else None,
                tool_name="team_member_state",
            )
        session.commit()
        if teammate is not None:
            notify_team_change(
                tenant_id=teammate.tenant_id,
                user_id=teammate.user_id,
                conversation_id=teammate.conversation_id,
            )
    except Exception:  # noqa: BLE001
        session.rollback()


@celery_app.task(bind=True, name="deepspace.subagent_turn")  # type: ignore[misc]
def run_subagent_turn_task(
    self: Any,
    *,
    tenant_id: str,
    user_id: str,
    conversation_id: str,
    child_id: str,
    message: str | None = None,
    roles: list[str] | None = None,
    permissions: list[str] | None = None,
) -> str:
    """Run one turn for a delegated child agent.

    The child answers the messages that wait in its own queue, and a message
    that arrives while the turn runs is answered by the next turn of the same
    task.

    Args:
        self: The bound task, used for its request identity.
        tenant_id: The owning tenant.
        user_id: The owning user.
        conversation_id: The conversation the child belongs to.
        child_id: The child to run.
        message: A message delivered with this turn, when the caller supplied
            one.
        roles: The caller's roles, so the child's tools are authorized the
            same way the parent's are.
        permissions: The caller's permissions.

    Returns:
        The child's stop reason.
    """
    settings = get_settings()
    parsed_tenant_id = uuid.UUID(tenant_id)
    parsed_user_id = uuid.UUID(user_id)
    parsed_conversation_id = uuid.UUID(conversation_id)
    parsed_child_id = uuid.UUID(child_id)
    session = get_session_factory()()
    child: DeepSpaceSubagentRun | None = None
    teammate: TeamMember | None = None
    try:
        session.execute(text("SET ROLE aks_app"))
        set_db_tenant_context(session, parsed_tenant_id)
        child = session.execute(
            select(DeepSpaceSubagentRun).where(
                DeepSpaceSubagentRun.id == parsed_child_id,
                DeepSpaceSubagentRun.tenant_id == parsed_tenant_id,
                DeepSpaceSubagentRun.user_id == parsed_user_id,
                DeepSpaceSubagentRun.conversation_id == parsed_conversation_id,
            )
        ).scalar_one_or_none()
        if child is None:
            logger.info("DeepSpace subagent turn skipped: the child no longer exists")
            return "missing"
        if child.busy:
            logger.info("DeepSpace subagent turn skipped: the child is already running")
            return "busy"
        queued = _claim_pending_messages(session, child)
        if message and message not in queued:
            queued.append(message)
        if not queued:
            return "idle"
        child.busy = True
        child.cancel_requested = False
        session.commit()

        auth = AuthContext(
            user_id=parsed_user_id,
            tenant_id=parsed_tenant_id,
            roles=frozenset(roles or ()),
            permissions=frozenset(permissions or ()),
            token_id=f"deepspace-subagent:{child_id}",
            auth_type="worker",
        )
        service = DeepSpaceChatService(db=session, settings=settings)
        teammate = _teammate_of(session, child)
        if teammate is not None:
            teammate.state = TEAM_MEMBER_STATE_RUNNING
            SessionEventStore(session).append(
                tenant_id=teammate.tenant_id,
                user_id=teammate.user_id,
                conversation_id=teammate.conversation_id,
                event_type=TEAM_MEMBER_STATE,
                payload={
                    "member_id": str(teammate.id),
                    "name": teammate.name,
                    "state": teammate.state,
                },
                run_id=child.parent_run_id,
                tool_name="team_member_state",
            )
            _deliver_queued_messages(session, teammate, run_id=child.parent_run_id)
            session.commit()
            notify_team_change(
                tenant_id=teammate.tenant_id,
                user_id=teammate.user_id,
                conversation_id=teammate.conversation_id,
            )

        async def execute(turn_messages: list[str]) -> tuple[str, str, str | None]:
            context = service.build_tool_context(
                auth=auth,
                conversation_id=parsed_conversation_id,
                web_provider=None,
                web_candidate=None,
                request=None,
                depth=int(child.delegation_depth),
                parent_agent_id=child.parent_agent_id,
            )
            outcome = await run_child_turn(
                context=context,
                child=child,
                task="\n\n".join(turn_messages),
                inherited_history=None,
            )
            return outcome.stop_reason, outcome.text, outcome.diagnostic

        stop_reason = "idle"
        answer = ""
        diagnostic: str | None = None
        for _turn in range(MAX_TURNS_PER_TASK):
            stop_reason, answer, diagnostic = asyncio.run(execute(queued))
            child.turn_count = int(child.turn_count or 0) + 1
            child.result_text = answer
            child.stop_reason = stop_reason
            child.status = "idle" if stop_reason == "completed" else stop_reason
            child.last_error = diagnostic
            session.commit()
            queued = _claim_pending_messages(session, child)
            # The claim holds the child's row until the transaction ends, so
            # the transaction ends here and a message that is sent while the
            # next turn runs is not held up by it.
            session.commit()
            if not queued:
                break
        child.busy = False
        if teammate is not None:
            _finish_teammate(teammate, stop_reason)
            SessionEventStore(session).append(
                tenant_id=teammate.tenant_id,
                user_id=teammate.user_id,
                conversation_id=teammate.conversation_id,
                event_type=TEAM_MEMBER_STATE,
                payload={
                    "member_id": str(teammate.id),
                    "name": teammate.name,
                    "state": teammate.state,
                    "stop_reason": stop_reason,
                },
                run_id=child.parent_run_id,
                tool_name="team_member_state",
            )
        session.commit()
        if teammate is not None:
            notify_team_change(
                tenant_id=teammate.tenant_id,
                user_id=teammate.user_id,
                conversation_id=teammate.conversation_id,
            )
        return stop_reason
    except Exception:
        session.rollback()
        # A turn that failed must not leave the child busy for good, or the
        # child would refuse every message that follows.
        _release_child(session, child, teammate)
        logger.warning("DeepSpace subagent turn failed safely", exc_info=True)
        raise
    finally:
        try:
            session.rollback()
            session.execute(text("RESET ROLE"))
            session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()
        cast(Any, session).close()
