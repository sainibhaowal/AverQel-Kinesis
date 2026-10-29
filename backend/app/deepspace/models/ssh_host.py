"""Tenant-owned SSH hosts DeepSpace may reach.

A host is a named remote machine the deployment has reviewed: where it
lives, who runs the command on it, the host key the connection must
present, and the private key that identifies the client.  The row holds no
key material: the key is referenced by a name the deployment resolves, so
a database copy never carries a secret.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import generate_uuid7_with_fallback
from app.platform.database.base import Base


class DeepSpaceSshHost(Base):
    """One reviewed remote host.

    Attributes:
        id: The host's id.
        tenant_id: The owning tenant.
        user_id: The user who added the host.
        name: The host's name inside the workspace.
        hostname: The host or address to connect to.
        port: The port to connect to.
        username: The user the command runs as.
        known_hosts_entry: The host key the connection must present, in
            ``known_hosts`` form.  The connection is refused when the host
            presents a different key.
        key_secret_ref: The name of the private key the deployment holds.
            The key itself is never stored here.
        remote_workspace: The directory the command runs in, on the host.
        enabled: Whether the host may be reached.
        timeout_seconds: How long one command may run.
        metadata_json: The host's own settings.
        created_at: When the host was added.
        updated_at: When the host last changed.
    """

    __tablename__ = "deepspace_ssh_hosts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7_with_fallback
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=22)
    username: Mapped[str] = mapped_column(String(120), nullable=False)
    known_hosts_entry: Mapped[str] = mapped_column(Text, nullable=False)
    key_secret_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    remote_workspace: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    metadata_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


__all__ = ["DeepSpaceSshHost"]
