"""Database models for the durable workspace registry.

This replaces the filesystem manifest with a proper database-backed store,
enabling multi-device sync, web deployments, and cross-machine sharing.
The filesystem markers (`.deepspace-workspace.json`) are preserved for
portability and desktop offline operation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.ids import generate_uuid7_with_fallback
from app.platform.database.base import Base

if TYPE_CHECKING:
    pass


class WorkspaceRegistry(Base):
    """One workspace registration for a tenant/user account.

    The identifier (id) is a UUID that never changes. The directory name
    (slug) is derived from the title at creation time and is used only for
    the filesystem registration directory. The display title can be changed
    without affecting the UUID or the directory name.
    """

    __tablename__ = "deepspace_workspace_registry"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "slug", name="uq_workspace_slug_per_account"),
        Index("ix_workspace_registry_account", "tenant_id", "user_id"),
        Index("ix_workspace_registry_default", "tenant_id", "user_id", "is_default"),
    )

    #: Stable UUID identifier — never changes, used in all APIs
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7_with_fallback
    )

    #: Owning tenant
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: Owning user
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: Filesystem directory slug (derived from title at create, immutable)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)

    #: User-facing display title (can be changed)
    title: Mapped[str] = mapped_column(String(256), nullable=False)

    #: Optional custom project directory path (external to registration root)
    custom_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Whether this is the account's default workspace
    is_default: Mapped[bool] = mapped_column(nullable=False, default=False, index=True)

    #: User-controlled display order (0 = first, higher = later)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Creation timestamp
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    #: Last modification timestamp
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Relationships
    session_positions: Mapped[list[WorkspaceSessionPosition]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        order_by="WorkspaceSessionPosition.position",
    )

    def effective_path(self, named_root: str) -> str:
        """Return the filesystem path this workspace points to."""
        if self.custom_path:
            return self.custom_path
        return f"{named_root}/{self.slug}"

    def to_dict(self, named_root: str) -> dict:
        """Return API-friendly dictionary."""
        return {
            "id": str(self.id),
            "slug": self.slug,
            "title": self.title,
            "path": self.effective_path(named_root),
            "custom_path": self.custom_path,
            "is_default": self.is_default,
            "display_order": self.display_order,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class WorkspaceSessionPosition(Base):
    """Explicit position of a conversation within a workspace.

    This replaces the registry's `positions` dict. Each row represents one
    conversation's position in the user's manual order for a workspace.
    """

    __tablename__ = "deepspace_workspace_session_positions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "conversation_id", name="uq_workspace_session_position"),
        UniqueConstraint("workspace_id", "position", name="uq_workspace_position"),
        Index("ix_session_positions_conversation", "conversation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7_with_fallback
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("deepspace_workspace_registry.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: User-defined position (0 = first). Gaps allowed for easy insertion.
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    workspace: Mapped[WorkspaceRegistry] = relationship(back_populates="session_positions")


class WorkspaceOrder(Base):
    """Global workspace display order for an account.

    This replaces the registry's `order` array. Each account has one row
    per workspace defining its position in the user's workspace list.
    """

    __tablename__ = "deepspace_workspace_order"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "workspace_id", name="uq_account_workspace_order"),
        UniqueConstraint("tenant_id", "user_id", "position", name="uq_account_workspace_position"),
        Index("ix_workspace_order_account", "tenant_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7_with_fallback
    )

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

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("deepspace_workspace_registry.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: User-defined position in workspace list (0 = first after default)
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class WorkspaceDeletedLog(Base):
    """Log of deleted workspaces for audit/recovery.

    When a workspace is deleted, its registration moves to a quarantine
    directory on the filesystem. This table records the deletion for
    audit purposes and potential recovery.
    """

    __tablename__ = "deepspace_workspace_deleted_log"
    __table_args__ = (Index("ix_deleted_log_account", "tenant_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7_with_fallback
    )

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

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)

    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    custom_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    was_default: Mapped[bool] = mapped_column(nullable=False, default=False)

    #: Filesystem quarantine path where registration was moved
    quarantine_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    deleted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
