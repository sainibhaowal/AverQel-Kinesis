"""A durable goal for a DeepSpace conversation.

A goal is the standing objective a conversation works toward.  It is
separate from the task ledger: the ledger records the steps of the current
plan, while a goal records what the conversation is for, so it survives a
compaction and a new turn.

Phase model: active, paused, blocked, complete
Revision tracking for compare-and-set mutations.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.database.base import Base


class GoalPhase(str):
    """Goal lifecycle phase."""

    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETE = "complete"


class DeepSpaceGoal(Base):
    """One named goal owned by a DeepSpace conversation."""

    __tablename__ = "deepspace_goals"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "conversation_id",
            "name",
            name="uq_deepspace_goals_name",
        ),
    )

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
    #: The goal's name, unique within the conversation.
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: The goal's statement.
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Phase: active, paused, blocked, complete
    phase: Mapped[str] = mapped_column(
        String(32), nullable=False, default=GoalPhase.ACTIVE, index=True
    )
    #: Revision for compare-and-set mutations
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: Maximum goal rounds allowed
    max_goal_rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    #: Number of rounds started
    rounds_started: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Blocked reason when phase is blocked
    blocked_reason: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    #: Why the goal was abandoned, when it was.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    #: Legacy status column in database (active, reached, abandoned)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
