"""A durable plan for one DeepSpace conversation.

A plan is the reviewed design an agent produced before it started work.  It
is separate from the task ledger: the ledger records the steps of the plan
that are already running, while the plan records the design itself and the
review that approved it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.database.base import Base

#: The plan is waiting for the user to approve it or ask for changes.
PLAN_STATUS_PROPOSED = "proposed"

#: The user approved the plan, and the work may proceed.
PLAN_STATUS_APPROVED = "approved"

#: The user asked for changes, and the agent must revise the plan.
PLAN_STATUS_REVISION_REQUESTED = "revision_requested"

#: The user rejected the plan outright.
PLAN_STATUS_REJECTED = "rejected"

#: The plan states the accepted statuses.
PLAN_STATUSES = (
    PLAN_STATUS_PROPOSED,
    PLAN_STATUS_APPROVED,
    PLAN_STATUS_REVISION_REQUESTED,
    PLAN_STATUS_REJECTED,
)


class DeepSpacePlan(Base):
    """One proposed plan owned by a DeepSpace conversation."""

    __tablename__ = "deepspace_plans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: The plan's title.
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The plan's Markdown body.
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: The plan's review status.
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PLAN_STATUS_PROPOSED, index=True
    )
    #: Why the plan was changed, when the user asked for changes.
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The number of times the plan was revised after feedback.
    revision_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
