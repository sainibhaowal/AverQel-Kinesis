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
import threading
import uuid
from dataclasses import replace
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
from app.deepspace.models.session_event import DeepSpaceSessionEvent
from app.deepspace.models.subagent import DeepSpaceSubagentRun
from app.deepspace.models.workspace_file import DeepSpaceWorkspaceFile
from app.deepspace.services.agent_team import notify_team_change
from app.deepspace.services.chat_service import DeepSpaceChatService
from app.deepspace.services.durable_subagent_manager import (
    DurableChildLease,
    DurableSubagentManager,
    get_durable_subagent_manager,
)
from app.deepspace.services.session_events import (
    SUBAGENT_DESCRIPTOR,
    SUBAGENT_END,
    SUBAGENT_PRESET,
    SUBAGENT_START,
    TEAM_MEMBER_STATE,
    TEAM_MESSAGE,
    SessionEventStore,
)
from app.deepspace.services.subagent_cold_resume import (
    apply_cold_resume_composition,
    cold_resume_from_db_row,
)
from app.deepspace.services.subagent_lifecycle import (
    SubagentResult as LifecycleResult,
)
from app.deepspace.services.subagent_lifecycle import (
    SubagentRunEndInfo,
    SubagentRunInfo,
    create_activation_observer,
    create_lifecycle_emitter,
)
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


class _ChildLeaseHeartbeat:
    """Renew a durable child lease on a separate DB session while it runs."""

    def __init__(self, lease: DurableChildLease, manager: DurableSubagentManager) -> None:
        self.lease = lease
        self.manager = manager
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"deepspace-child-lease-{lease.child_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def _run(self) -> None:
        interval = max(5, min(30, self.lease.duration_seconds // 3))
        while not self._stop.wait(interval):
            session = get_session_factory()()
            role_applied = False
            try:
                role_applied = _apply_worker_role(session)
                set_db_tenant_context(session, self.lease.tenant_id)
                if not self.manager.heartbeat(session, self.lease):
                    self.lost.set()
                    return
            except Exception:  # noqa: BLE001 - loss of lease proof stops the old worker
                self.lost.set()
                logger.warning("DeepSpace child lease heartbeat failed", exc_info=True)
                return
            finally:
                try:
                    session.rollback()
                    if role_applied:
                        session.execute(text("RESET ROLE"))
                        session.commit()
                except Exception:  # noqa: BLE001
                    session.rollback()
                cast(Any, session).close()


def _apply_worker_role(session: Session) -> bool:
    """Apply the PostgreSQL worker role where the selected database supports it."""
    bind = getattr(session, "bind", None)
    dialect = getattr(getattr(bind, "dialect", None), "name", None)
    if dialect == "sqlite":
        return False
    session.execute(text("SET ROLE aks_app"))
    return True


def _claim_pending_messages(session: Session, child: DeepSpaceSubagentRun) -> list[dict[str, Any]]:
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
        Normalized text and image references the turn must answer. Older
        rows containing only a ``message`` string remain readable.
    """
    raw = session.execute(
        select(DeepSpaceSubagentRun.pending_messages)
        .where(DeepSpaceSubagentRun.id == child.id)
        .with_for_update()
    ).scalar()
    pending = raw if isinstance(raw, list) else []
    messages: list[dict[str, Any]] = []
    for item in pending:
        if isinstance(item, str):
            message = item.strip()
            attachments: list[str] = []
            message_id = ""
        elif isinstance(item, dict):
            message = str(item.get("message") or "").strip()
            raw_ids = item.get("attachment_file_ids")
            attachments = (
                list(dict.fromkeys(str(value) for value in raw_ids if isinstance(value, str)))[:10]
                if isinstance(raw_ids, list)
                else []
            )
            message_id = str(item.get("id") or "")
        else:
            continue
        if message or attachments:
            messages.append(
                {
                    "id": message_id,
                    "message": message,
                    "attachment_file_ids": attachments,
                }
            )
    child.pending_messages = []
    return messages


async def _prepare_child_messages(
    session: Session,
    child: DeepSpaceSubagentRun,
    service: DeepSpaceChatService,
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Resolve queued images within the child's tenant/user/conversation boundary."""
    from app.deepspace.services.chat_service import ATTACHMENT_VISION_MIME_TYPES

    text_parts: list[str] = []
    requested_ids: list[uuid.UUID] = []
    invalid_ids = 0
    for item in messages:
        text = str(item.get("message") or "").strip()
        if text:
            text_parts.append(text)
        raw_ids = item.get("attachment_file_ids")
        if not isinstance(raw_ids, list):
            continue
        for raw_id in raw_ids:
            try:
                file_id = uuid.UUID(str(raw_id))
            except (TypeError, ValueError, AttributeError):
                invalid_ids += 1
                continue
            if file_id not in requested_ids and len(requested_ids) < 10:
                requested_ids.append(file_id)

    image_files: list[DeepSpaceWorkspaceFile] = []
    unavailable = invalid_ids
    if requested_ids:
        rows = (
            session.execute(
                select(DeepSpaceWorkspaceFile).where(
                    DeepSpaceWorkspaceFile.id.in_(requested_ids),
                    DeepSpaceWorkspaceFile.tenant_id == child.tenant_id,
                    DeepSpaceWorkspaceFile.user_id == child.user_id,
                    DeepSpaceWorkspaceFile.conversation_id == child.conversation_id,
                )
            )
            .scalars()
            .all()
        )
        by_id = {row.id: row for row in rows}
        unavailable += len(set(requested_ids) - set(by_id))
        for file_id in requested_ids:
            row = by_id.get(file_id)
            if row is None:
                continue
            if str(row.content_type or "").casefold() in ATTACHMENT_VISION_MIME_TYPES:
                image_files.append(row)
            else:
                text_parts.append(f"[Attachment {row.name!r} is not a supported image.]")

    images: list[str] = []
    statuses: dict[str, str] = {}
    if image_files:
        try:
            images, statuses = await service._load_attachment_vision_inputs(
                image_files,
                tenant_id=child.tenant_id,
            )
        except Exception:  # noqa: BLE001 - an unavailable image must not lose its text task
            logger.warning("DeepSpace child image attachment could not be loaded", exc_info=True)
            statuses = {str(row.id): "Image data could not be loaded." for row in image_files}
    for row in image_files:
        status = statuses.get(str(row.id))
        if status and status != "Image included as visual input.":
            extracted = str(row.extracted_text or "").strip()
            detail = f" Extracted text: {extracted[:6000]}" if extracted else ""
            text_parts.append(f"[Image {row.name!r} not attached: {status}.{detail}]")
    if unavailable:
        text_parts.append(f"[{unavailable} image attachment(s) were unavailable and not opened.]")

    prompt = "\n\n".join(text_parts)[:20_000]
    if not prompt and images:
        prompt = "Please inspect the attached image."
    return {"message": prompt, "images": images}


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
    lease: DurableChildLease | None = None,
) -> None:
    """Release a child whose turn ended in a failure.

    Args:
        session: The database session.
        child: The durable child record, when the turn found one.
        teammate: The teammate the child works, when the child is a teammate.
    """
    try:
        if child is not None and lease is not None:
            get_durable_subagent_manager().release(
                session,
                lease,
                status="failed",
                stop_reason="failed",
                error="The background child task failed.",
            )
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


def _worker_lifecycle_observer(
    session: Session,
    child: DeepSpaceSubagentRun,
    activation_run_id: str,
):
    """Persist lifecycle edges for one claimed durable child activation."""
    emitter = create_lifecycle_emitter(session)

    def persist(name: str, info: Any, _parent: Any) -> None:
        if name not in {SUBAGENT_START, SUBAGENT_END}:
            return
        if isinstance(info, SubagentRunInfo):
            payload = {
                "activation_run_id": info.run_id,
                "provider": info.provider,
                "child_id": str(child.id),
                "local": info.local,
            }
        elif isinstance(info, SubagentRunEndInfo):
            payload = {
                "activation_run_id": info.run_id,
                "provider": str(child.agent_provider or "deepspace"),
                "child_id": str(child.id),
                "stop_reason": info.stop_reason,
                "has_error": info.error is not None,
            }
        else:
            return
        try:
            SessionEventStore(session).append(
                tenant_id=child.tenant_id,
                user_id=child.user_id,
                conversation_id=child.conversation_id,
                event_type=name,
                payload=payload,
                run_id=child.parent_run_id,
                subagent_id=child.id,
                turn=child.turn_count + 1,
                tool_name="subagent",
            )
            session.commit()
        except Exception:  # noqa: BLE001 - lifecycle telemetry must not lose child work
            session.rollback()
            logger.warning("DeepSpace child lifecycle event could not be persisted", exc_info=True)

    emitter.register(persist)
    return create_activation_observer(
        emitter,
        str(child.agent_provider or "deepspace"),
        str(child.id),
        parent=None,
        run_id=activation_run_id,
    )


def _restore_continuable_child(session: Session, child: DeepSpaceSubagentRun) -> None:
    """Restore the persisted child composition before executing its next turn.

    The durable row is authoritative. Tenant/user/conversation-scoped typed
    events provide compatibility for older rows with incomplete descriptors.
    """
    if child.mode != "continuable":
        return

    event_rows = (
        session.execute(
            select(DeepSpaceSessionEvent)
            .where(
                DeepSpaceSessionEvent.tenant_id == child.tenant_id,
                DeepSpaceSessionEvent.user_id == child.user_id,
                DeepSpaceSessionEvent.conversation_id == child.conversation_id,
                DeepSpaceSessionEvent.subagent_id == child.id,
                DeepSpaceSessionEvent.event_type.in_(
                    (SUBAGENT_DESCRIPTOR, SUBAGENT_PRESET, "agent-preset/selected")
                ),
            )
            .order_by(DeepSpaceSessionEvent.seq.asc())
            .limit(200)
        )
        .scalars()
        .all()
    )
    events = [{"type": row.event_type, "data": dict(row.payload or {})} for row in event_rows]
    restored = cold_resume_from_db_row(child, events=events)
    if not restored.success:
        raise RuntimeError(restored.error or "The child continuation could not be restored.")
    apply_cold_resume_composition(child, restored)


@celery_app.task(bind=True, name="deepspace.subagent_turn")  # type: ignore[misc]
def run_subagent_turn_task(
    self: Any,
    *,
    tenant_id: str,
    user_id: str,
    conversation_id: str,
    child_id: str,
    activation_run_id: str | None = None,
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
        activation_run_id: Stable identity for this worker activation's start/end events.
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
    lease: DurableChildLease | None = None
    heartbeat: _ChildLeaseHeartbeat | None = None
    teammate: TeamMember | None = None
    observer = None
    role_applied = False
    try:
        role_applied = _apply_worker_role(session)
        set_db_tenant_context(session, parsed_tenant_id)
        manager = get_durable_subagent_manager()
        claim = manager.claim_turn(
            session,
            tenant_id=parsed_tenant_id,
            user_id=parsed_user_id,
            conversation_id=parsed_conversation_id,
            child_id=parsed_child_id,
            message=message,
        )
        if claim.status != "claimed" or claim.child is None or claim.lease is None:
            if claim.status == "missing":
                logger.info("DeepSpace subagent turn skipped: the child no longer exists")
            elif claim.status == "busy":
                logger.info("DeepSpace subagent turn skipped: the child is already running")
            return claim.status
        child = claim.child
        lease = claim.lease
        queued = [
            (
                item
                if isinstance(item, dict)
                else {"id": "", "message": str(item), "attachment_file_ids": []}
            )
            for item in claim.messages
        ]
        heartbeat = _ChildLeaseHeartbeat(lease, manager)
        heartbeat.start()
        _restore_continuable_child(session, child)
        observer = _worker_lifecycle_observer(
            session,
            child,
            activation_run_id or str(uuid.uuid4()),
        )
        observer.start(child)

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

        async def execute(
            turn_messages: list[dict[str, Any]],
        ) -> tuple[str, str, str | None]:
            context = service.build_tool_context(
                auth=auth,
                conversation_id=parsed_conversation_id,
                web_provider=None,
                web_candidate=None,
                request=None,
                depth=int(child.delegation_depth),
                # A nested delegation is owned by this child, not by the
                # child's own parent. This ancestry is also the durable
                # cancellation-cascade link.
                parent_agent_id=child.id,
            )

            def child_cancel_requested() -> bool:
                if heartbeat is not None and heartbeat.lost.is_set():
                    return True
                if lease is None or not manager.owns_lease(session, lease):
                    if heartbeat is not None:
                        heartbeat.lost.set()
                    return True
                return bool(
                    session.execute(
                        select(DeepSpaceSubagentRun.cancel_requested)
                        .where(
                            DeepSpaceSubagentRun.id == parsed_child_id,
                            DeepSpaceSubagentRun.tenant_id == parsed_tenant_id,
                            DeepSpaceSubagentRun.user_id == parsed_user_id,
                            DeepSpaceSubagentRun.conversation_id == parsed_conversation_id,
                        )
                        .execution_options(populate_existing=True)
                    ).scalar_one_or_none()
                )

            descriptor = child.descriptor if isinstance(child.descriptor, dict) else {}
            context = replace(
                context,
                cancel_check=child_cancel_requested,
                agent_preset_override=(
                    str(descriptor["agentPreset"]) if descriptor.get("agentPreset") else None
                ),
            )
            prepared = await _prepare_child_messages(session, child, service, turn_messages)

            async def poll_messages() -> list[dict[str, Any]]:
                newly_queued = _claim_pending_messages(session, child)
                session.commit()
                if not newly_queued:
                    return []
                return [await _prepare_child_messages(session, child, service, newly_queued)]

            outcome = await run_child_turn(
                context=context,
                child=child,
                task=str(prepared["message"]),
                inherited_history=None,
                images=list(prepared["images"]),
                poll_messages=poll_messages,
            )
            return outcome.stop_reason, outcome.text, outcome.diagnostic

        stop_reason = "idle"
        answer = ""
        diagnostic: str | None = None
        for _turn in range(MAX_TURNS_PER_TASK):
            stop_reason, answer, diagnostic = asyncio.run(execute(queued))
            if heartbeat.lost.is_set() or not manager.owns_lease(session, lease):
                heartbeat.lost.set()
                return "lease_lost"
            child.turn_count = int(child.turn_count or 0) + 1
            child.result_text = answer
            child.stop_reason = stop_reason
            child.status = "idle" if stop_reason == "completed" else stop_reason
            child.last_error = diagnostic
            if stop_reason == "cancelled":
                # Stop the current activation but keep messages queued while it
                # was running so a later explicit continuation can handle them.
                child.cancel_requested = False
            session.commit()
            if stop_reason == "cancelled":
                break
            queued = _claim_pending_messages(session, child)
            # The claim holds the child's row until the transaction ends, so
            # the transaction ends here and a message that is sent while the
            # next turn runs is not held up by it.
            session.commit()
            if not queued:
                break
        if not manager.release(
            session,
            lease,
            status=child.status,
            stop_reason=stop_reason,
            result_text=answer,
            error=diagnostic,
        ):
            return "lease_lost"
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
        if observer is not None:
            observer.settle(
                LifecycleResult(
                    output=[answer] if answer else [],
                    stop_reason=stop_reason,
                    error=RuntimeError(diagnostic) if diagnostic else None,
                )
            )
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
        _release_child(session, child, teammate, lease)
        if observer is not None:
            observer.settle(
                LifecycleResult(
                    stop_reason="error",
                    error=RuntimeError("The background child task failed."),
                )
            )
        logger.warning("DeepSpace subagent turn failed safely", exc_info=True)
        raise
    finally:
        if heartbeat is not None:
            heartbeat.stop()
        try:
            session.rollback()
            if role_applied:
                session.execute(text("RESET ROLE"))
                session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()
        cast(Any, session).close()
