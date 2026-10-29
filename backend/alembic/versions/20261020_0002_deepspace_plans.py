"""Add the DeepSpace plan table.

Revision ID: 20261020_0002
Revises: 20261020_0001
Create Date: 2026-10-20 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261020_0002"
down_revision = "20261020_0001"
branch_labels = None
depends_on = None

UUID = sa.UUID()


def upgrade() -> None:
    """Create the reviewed-plan table."""
    op.create_table(
        "deepspace_plans",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="proposed"),
        sa.Column("feedback", sa.Text(), nullable=True),
        sa.Column("revision_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_deepspace_plans_tenant_id", "deepspace_plans", ["tenant_id"])
    op.create_index("ix_deepspace_plans_user_id", "deepspace_plans", ["user_id"])
    op.create_index("ix_deepspace_plans_conversation_id", "deepspace_plans", ["conversation_id"])
    op.create_index("ix_deepspace_plans_status", "deepspace_plans", ["status"])


def downgrade() -> None:
    """Remove the reviewed-plan table."""
    op.drop_table("deepspace_plans")
