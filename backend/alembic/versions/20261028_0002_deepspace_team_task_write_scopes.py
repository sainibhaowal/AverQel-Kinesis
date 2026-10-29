"""Add write_scopes column to deepspace_team_tasks.

Revision ID: 20261028_0002
Revises: 20261028_0001
Create Date: 2026-10-28 01:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "20261028_0002"
down_revision = "20261028_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add write_scopes to deepspace_team_tasks if present."""
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("deepspace_team_tasks"):
        return

    columns = {column["name"] for column in inspector.get_columns("deepspace_team_tasks")}
    if "write_scopes" not in columns:
        op.add_column(
            "deepspace_team_tasks",
            sa.Column(
                "write_scopes",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
        )


def downgrade() -> None:
    """Remove write_scopes from deepspace_team_tasks."""
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("deepspace_team_tasks"):
        return

    columns = {column["name"] for column in inspector.get_columns("deepspace_team_tasks")}
    if "write_scopes" in columns:
        op.drop_column("deepspace_team_tasks", "write_scopes")
