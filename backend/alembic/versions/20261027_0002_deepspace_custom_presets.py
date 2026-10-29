"""Add user-owned DeepSpace custom agent presets.

Revision ID: 20261027_0002
Revises: 20261027_0001
Create Date: 2026-10-27 15:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261027_0002"
down_revision = "20261027_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create durable per-user custom preset storage."""
    op.create_table(
        "deep_space_custom_presets",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("settings_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deep_space_custom_presets"),
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            "name",
            name="uq_deep_space_custom_presets_owner_name",
        ),
    )
    op.create_index(
        "ix_deep_space_custom_presets_tenant_id",
        "deep_space_custom_presets",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_deep_space_custom_presets_user_id",
        "deep_space_custom_presets",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove durable custom preset storage."""
    op.drop_table("deep_space_custom_presets")
