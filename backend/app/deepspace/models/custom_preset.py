"""User-owned DeepSpace custom agent presets."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, Column, DateTime, String, Text, UniqueConstraint

from app.core.ids import generate_uuid7_with_fallback
from app.platform.database.base import Base


class DeepSpaceCustomPreset(Base):
    """One named custom agent preset owned by a user."""

    __tablename__ = "deep_space_custom_presets"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "name",
            name="uq_deep_space_custom_presets_owner_name",
        ),
    )

    id = Column(String, primary_key=True, default=lambda: str(generate_uuid7_with_fallback()))
    tenant_id = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    name = Column(String(120), nullable=False)
    description = Column(Text, nullable=False, default="")
    settings_json = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


__all__ = ["DeepSpaceCustomPreset"]
