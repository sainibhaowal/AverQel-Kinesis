"""Durable state for a delegated DeepSpace child agent.

A child is a separate run with its own identity, its own durable record, and
its own place in the conversation's delegation tree.  The row is what makes a
continuable child resumable: it stores the composition the child was started
with, so a later turn can rebuild the same child without the parent having to
repeat every parameter.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.database.base import Base


class DeepSpaceSubagentRun(Base):
    """One delegated child agent owned by a DeepSpace conversation."""

    __tablename__ = "deepspace_subagent_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: The run that started this child, when the child was delegated.
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("deepspace_agent_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: The immediate parent child, for a nested delegation.
    parent_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    #: ``subagent`` for a fresh child, ``fork`` for one that inherited history.
    origin: Mapped[str] = mapped_column(String(32), nullable=False, default="subagent")
    #: ``one-shot`` for a child that ends with its task, ``continuable`` for one
    #: that can receive further messages.
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="one-shot")
    #: The short description the delegating agent supplied.
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: The delegation depth, counted from the root agent.
    delegation_depth: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: The provider, model, and reasoning effort the child runs under.
    agent_provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    agent_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: The per-child persona that shadows the deployment persona.
    persona: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The tools the child may use, as ``allow`` and ``deny`` name lists.
    tool_filter: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    #: The versioned descriptor that makes a continuable child resumable.
    descriptor: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    #: The history prefix a forked child inherited from its parent.
    inherited_history: Mapped[list[object]] = mapped_column(JSON, nullable=False, default=list)
    #: The messages queued for the child's next turn.
    pending_messages: Mapped[list[object]] = mapped_column(JSON, nullable=False, default=list)
    #: The child's own exchanges, so a continued child keeps its context.
    turn_history: Mapped[list[object]] = mapped_column(JSON, nullable=False, default=list)
    #: ``running``, ``idle``, ``completed``, ``failed``, or ``cancelled``.
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="running", index=True)
    #: Whether a turn is in flight for this child.
    busy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Whether a stop was requested for the child's current turn.
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: The child's most recent answer.
    result_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The reason the child's last turn ended.
    stop_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)
    #: The number of turns the child has run.
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
