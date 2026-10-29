"""Tenant/user-owned controls for which DeepSpace agent modes may be chosen."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, Column, DateTime, String, UniqueConstraint

from app.core.ids import generate_uuid7_with_fallback
from app.platform.database.base import Base


class DeepSpacePresetEnablement(Base):
    """One user's choice to keep an agent preset available or hidden.

    A row named for a preset records whether the user's mode selector lists
    it.  An absent row keeps the preset available, so the catalogue the
    deployment ships stays available before the user touches a toggle.
    """

    __tablename__ = "deep_space_preset_enablement"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "user_id", "preset_id", name="uq_deep_space_preset_enablement"
        ),
    )

    id = Column(String, primary_key=True, default=lambda: str(generate_uuid7_with_fallback()))
    tenant_id = Column(String, index=True, nullable=False)
    user_id = Column(String, index=True, nullable=False)
    preset_id = Column(String, nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC), nullable=False)
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
