"""Add the DeepSpace agent-mode enablement table.

Revision ID: 20261023_0001
Revises: 20261021_0002
Create Date: 2026-10-23 02:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261023_0001"
down_revision = "20261021_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the per-user agent-mode enablement table."""
    op.create_table(
        "deep_space_preset_enablement",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("preset_id", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deep_space_preset_enablement"),
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            "preset_id",
            name="uq_deep_space_preset_enablement",
        ),
    )
    op.create_index(
        "ix_deep_space_preset_enablement_tenant_id",
        "deep_space_preset_enablement",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_deep_space_preset_enablement_user_id",
        "deep_space_preset_enablement",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the per-user agent-mode enablement table."""
    op.drop_table("deep_space_preset_enablement")
