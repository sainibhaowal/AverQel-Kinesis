"""Database-backed lifecycle coordination for durable subagent runs.

The in-memory ``SubagentManager`` owns only resident, process-local children.
This manager owns worker admission, fencing, cancellation, and recovery for
SQL/Celery runs so multiple workers and restarts share one authority.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.deepspace.models.conversation import Conversation
from app.deepspace.models.subagent import DeepSpaceSubagentRun

DEFAULT_CHILD_LEASE_SECONDS = 180


@dataclass(slots=True)
class DurableChildLease:
    """Fencing identity for one SQL-owned worker activation."""

    tenant_id: uuid.UUID
    user_id: uuid.UUID
    conversation_id: uuid.UUID
    child_id: uuid.UUID
    token: str
    duration_seconds: int = DEFAULT_CHILD_LEASE_SECONDS
    last_renewed_monotonic: float = 0.0


@dataclass(frozen=True, slots=True)
class DurableClaim:
    """A worker admission result and the messages claimed with it."""

    status: str
    child: DeepSpaceSubagentRun | None = None
    lease: DurableChildLease | None = None
    messages: tuple[Any, ...] = ()


class DurableSubagentManager:
    """Coordinate durable child state through row locks and fencing tokens."""

    def __init__(self, *, lease_seconds: int = DEFAULT_CHILD_LEASE_SECONDS) -> None:
        if lease_seconds < 30:
            raise ValueError("subagent lease duration must be at least 30 seconds")
        self.lease_seconds = lease_seconds

    @staticmethod
    def _scoped_child_query(
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        child_id: uuid.UUID,
    ):
        return select(DeepSpaceSubagentRun).where(
            DeepSpaceSubagentRun.id == child_id,
            DeepSpaceSubagentRun.tenant_id == tenant_id,
            DeepSpaceSubagentRun.user_id == user_id,
            DeepSpaceSubagentRun.conversation_id == conversation_id,
        )

    def admit(
        self,
        session: Session,
        child: DeepSpaceSubagentRun,
        *,
        max_children: int,
    ) -> bool:
        """Atomically enforce the conversation child budget and publish a child."""
        owner = session.execute(
            select(Conversation.id)
            .where(
                Conversation.id == child.conversation_id,
                Conversation.tenant_id == child.tenant_id,
                Conversation.user_id == child.user_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if owner is None:
            session.rollback()
            return False
        count = session.execute(
            select(func.count())
            .select_from(DeepSpaceSubagentRun)
            .where(
                DeepSpaceSubagentRun.tenant_id == child.tenant_id,
                DeepSpaceSubagentRun.user_id == child.user_id,
                DeepSpaceSubagentRun.conversation_id == child.conversation_id,
            )
        ).scalar_one()
        if int(count or 0) >= max_children:
            session.rollback()
            return False
        session.add(child)
        session.commit()
        return True

    def claim_turn(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        child_id: uuid.UUID,
        message: str | None = None,
        now: datetime | None = None,
    ) -> DurableClaim:
        """Claim queued work with a lease that can be safely renewed or fenced."""
        child = session.execute(
            self._scoped_child_query(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                child_id=child_id,
            ).with_for_update()
        ).scalar_one_or_none()
        if child is None:
            return DurableClaim("missing")

        current = now or datetime.now(UTC)
        if child.busy:
            expiry = child.lease_expires_at
            # A pre-lease worker cannot be safely fenced by the new code. Treat
            # that legacy activation as busy until it settles, rather than
            # risking two active workers on the same child.
            if expiry is not None and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            if child.lease_token is None or expiry is None or expiry > current:
                return DurableClaim("busy", child=child)

        if child.cancel_requested:
            child.cancel_requested = False
            child.busy = False
            child.status = "idle" if child.mode == "continuable" else "cancelled"
            child.lease_token = None
            child.lease_expires_at = None
            child.last_heartbeat_at = None
            session.commit()
            return DurableClaim("cancelled", child=child)

        pending = list(child.pending_messages or [])
        if message and not any(
            (item == message)
            or (isinstance(item, dict) and str(item.get("message") or "") == message)
            for item in pending
        ):
            pending.append({"id": "", "message": message, "attachment_file_ids": []})
        if not pending:
            return DurableClaim("idle", child=child)

        token = uuid.uuid4().hex
        child.pending_messages = []
        child.busy = True
        child.status = "running"
        child.lease_token = token
        child.lease_expires_at = current + timedelta(seconds=self.lease_seconds)
        child.last_heartbeat_at = current
        session.commit()
        lease = DurableChildLease(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            child_id=child_id,
            token=token,
            duration_seconds=self.lease_seconds,
        )
        return DurableClaim("claimed", child=child, lease=lease, messages=tuple(pending))

    def heartbeat(self, session: Session, lease: DurableChildLease) -> bool:
        """Renew only the activation holding the current fencing token."""
        now = datetime.now(UTC)
        result = session.execute(
            update(DeepSpaceSubagentRun)
            .where(
                DeepSpaceSubagentRun.id == lease.child_id,
                DeepSpaceSubagentRun.tenant_id == lease.tenant_id,
                DeepSpaceSubagentRun.user_id == lease.user_id,
                DeepSpaceSubagentRun.conversation_id == lease.conversation_id,
                DeepSpaceSubagentRun.busy.is_(True),
                DeepSpaceSubagentRun.lease_token == lease.token,
            )
            .values(
                lease_expires_at=now + timedelta(seconds=lease.duration_seconds),
                last_heartbeat_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        session.commit()
        return result.rowcount == 1

    def owns_lease(self, session: Session, lease: DurableChildLease) -> bool:
        """Confirm a worker still owns its child before it publishes output."""
        token = session.execute(
            select(DeepSpaceSubagentRun.lease_token).where(
                DeepSpaceSubagentRun.id == lease.child_id,
                DeepSpaceSubagentRun.tenant_id == lease.tenant_id,
                DeepSpaceSubagentRun.user_id == lease.user_id,
                DeepSpaceSubagentRun.conversation_id == lease.conversation_id,
                DeepSpaceSubagentRun.busy.is_(True),
            )
        ).scalar_one_or_none()
        return token == lease.token

    def request_cancel(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        child_id: uuid.UUID,
        include_descendants: bool = True,
    ) -> int:
        """Request cancellation for one child and its owned descendants."""
        rows = (
            session.execute(
                select(DeepSpaceSubagentRun)
                .where(
                    DeepSpaceSubagentRun.tenant_id == tenant_id,
                    DeepSpaceSubagentRun.user_id == user_id,
                    DeepSpaceSubagentRun.conversation_id == conversation_id,
                )
                .with_for_update()
            )
            .scalars()
            .all()
        )
        by_id = {row.id: row for row in rows}
        root = by_id.get(child_id)
        if root is None:
            return 0
        selected = {child_id}
        if include_descendants:
            traversed = {child_id}
            while True:
                descendants = {
                    row.id
                    for row in rows
                    if row.parent_agent_id in traversed and row.id not in traversed
                }
                if not descendants:
                    break
                traversed.update(descendants)
                selected.update(
                    identity
                    for identity in descendants
                    if by_id[identity].busy or by_id[identity].status == "running"
                )
        for identity in selected:
            by_id[identity].cancel_requested = True
        session.commit()
        return len(selected)

    def release(
        self,
        session: Session,
        lease: DurableChildLease,
        *,
        status: str,
        stop_reason: str,
        result_text: str | None = None,
        error: str | None = None,
        increment_turn: bool = False,
    ) -> bool:
        """Settle one activation only if its fencing token still owns the row."""
        child = session.execute(
            self._scoped_child_query(
                tenant_id=lease.tenant_id,
                user_id=lease.user_id,
                conversation_id=lease.conversation_id,
                child_id=lease.child_id,
            ).with_for_update()
        ).scalar_one_or_none()
        if child is None or not child.busy or child.lease_token != lease.token:
            return False
        child.busy = False
        child.status = status
        child.stop_reason = stop_reason
        child.last_error = error
        if result_text is not None:
            child.result_text = result_text
        if increment_turn:
            child.turn_count = int(child.turn_count or 0) + 1
        child.lease_token = None
        child.lease_expires_at = None
        child.last_heartbeat_at = None
        session.commit()
        return True


_durable_manager = DurableSubagentManager()


def get_durable_subagent_manager() -> DurableSubagentManager:
    """Return the stateless coordinator used by live SQL/Celery runs."""
    return _durable_manager


__all__ = [
    "DEFAULT_CHILD_LEASE_SECONDS",
    "DurableChildLease",
    "DurableClaim",
    "DurableSubagentManager",
    "get_durable_subagent_manager",
]
