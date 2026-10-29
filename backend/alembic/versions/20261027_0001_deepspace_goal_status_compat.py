"""Restore the legacy DeepSpace goal status column for ORM compatibility.

Revision ID: 20261027_0001
Revises: 20261023_0001
Create Date: 2026-10-27 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "20261027_0001"
down_revision = "20261023_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add status when an older goal table does not have it."""
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("deepspace_goals"):
        return
    columns = {column["name"] for column in inspector.get_columns("deepspace_goals")}
    if "status" in columns:
        return
    op.add_column(
        "deepspace_goals",
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default=sa.text("'active'")
        ),
    )
    op.create_index("ix_deepspace_goals_status", "deepspace_goals", ["status"])


def downgrade() -> None:
    """Remove the compatibility column added by this migration."""
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("deepspace_goals"):
        return
    columns = {column["name"] for column in inspector.get_columns("deepspace_goals")}
    if "status" not in columns:
        return
    op.drop_index("ix_deepspace_goals_status", table_name="deepspace_goals")
    op.drop_column("deepspace_goals", "status")
