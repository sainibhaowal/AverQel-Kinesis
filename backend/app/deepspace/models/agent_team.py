"""Durable state for the named team of one DeepSpace conversation.

A team is a set of named teammates that work the same task as the agent that
created them.  Three rows carry it: the members, the messages that travel
between them, and the task board they share.

Every row is scoped to a tenant, a user, and a conversation, so a teammate,
a message, and a task never reach another conversation.  The rows outlive the
process, so a teammate that was offline receives the messages that were
queued for it when it comes back.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.database.base import Base

#: The teammate exists and is not working.
TEAM_MEMBER_STATE_IDLE = "idle"

#: The teammate is working on a task.
TEAM_MEMBER_STATE_RUNNING = "running"

#: The teammate finished the work it was given.
TEAM_MEMBER_STATE_FINISHED = "finished"

#: The states a teammate is in.
TEAM_MEMBER_STATES = (
    TEAM_MEMBER_STATE_IDLE,
    TEAM_MEMBER_STATE_RUNNING,
    TEAM_MEMBER_STATE_FINISHED,
)

#: The message waits for a teammate that is not running.
TEAM_MESSAGE_STATE_QUEUED = "queued"

#: The message reached a running teammate.
TEAM_MESSAGE_STATE_DELIVERED = "delivered"

#: The states a message is in.
TEAM_MESSAGE_STATES = (
    TEAM_MESSAGE_STATE_QUEUED,
    TEAM_MESSAGE_STATE_DELIVERED,
)

#: The task is waiting for an owner.
TEAM_TASK_STATE_PENDING = "pending"

#: The task is being worked on.
TEAM_TASK_STATE_IN_PROGRESS = "in_progress"

#: The task cannot go on until something changes.
TEAM_TASK_STATE_BLOCKED = "blocked"

#: The task is done.
TEAM_TASK_STATE_COMPLETED = "completed"

#: The states a task is in.
TEAM_TASK_STATES = (
    TEAM_TASK_STATE_PENDING,
    TEAM_TASK_STATE_IN_PROGRESS,
    TEAM_TASK_STATE_BLOCKED,
    TEAM_TASK_STATE_COMPLETED,
)


class TeamMember(Base):
    """One named teammate of a DeepSpace conversation."""

    __tablename__ = "deepspace_team_members"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: The name the agent addresses the teammate by.
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The provider route the teammate runs under.
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    #: The model the teammate runs under.
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: The persona that shapes how the teammate works.
    persona: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The tools the teammate may use, as ``allow`` and ``deny`` name lists.
    tool_filter: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    #: The versioned descriptor that makes the teammate resumable.
    descriptor: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    child_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("deepspace_subagent_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: ``idle``, ``running``, or ``finished``.
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TEAM_MEMBER_STATE_IDLE, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "conversation_id",
            "name",
            name="uq_deepspace_team_members_name",
        ),
    )


class TeamMessage(Base):
    """One message between the members of a DeepSpace conversation."""

    __tablename__ = "deepspace_team_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: The name of the sender, or ``agent`` for the agent that owns the team.
    sender: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The name of the teammate the message is for.
    recipient: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: The message text.
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: ``queued`` while the recipient is not running, ``delivered`` after.
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TEAM_MESSAGE_STATE_QUEUED, index=True
    )
    #: When the message reached its recipient.
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class TeamTask(Base):
    """One task on the shared board of a DeepSpace conversation."""

    __tablename__ = "deepspace_team_tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: The short title of the task.
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    #: The task body.
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: ``pending``, ``in_progress``, ``blocked``, or ``completed``.
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TEAM_TASK_STATE_PENDING, index=True
    )
    #: The name of the teammate that owns the task.
    owner: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: The revision of the task, raised by every accepted update.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: The ids of the tasks this one waits for.
    depends_on: Mapped[list[object]] = mapped_column(JSON, nullable=False, default=list)
    #: The advisory directory write scopes (e.g. ['research/scout']).
    write_scopes: Mapped[list[object]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


__all__ = [
    "TEAM_MEMBER_STATE_FINISHED",
    "TEAM_MEMBER_STATE_IDLE",
    "TEAM_MEMBER_STATE_RUNNING",
    "TEAM_MEMBER_STATES",
    "TEAM_MESSAGE_STATE_DELIVERED",
    "TEAM_MESSAGE_STATE_QUEUED",
    "TEAM_MESSAGE_STATES",
    "TEAM_TASK_STATE_BLOCKED",
    "TEAM_TASK_STATE_COMPLETED",
    "TEAM_TASK_STATE_IN_PROGRESS",
    "TEAM_TASK_STATE_PENDING",
    "TEAM_TASK_STATES",
    "TeamMember",
    "TeamMessage",
    "TeamTask",
]
