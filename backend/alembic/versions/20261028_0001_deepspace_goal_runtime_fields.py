"""Add the runtime fields used by DeepSpace goals.

Revision ID: 20261028_0001
Revises: 20261027_0002
Create Date: 2026-10-28 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect, text
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261028_0001"
down_revision = "20261027_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Bring existing goal tables up to the current ORM definition."""
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("deepspace_goals"):
        return

    columns = {column["name"] for column in inspector.get_columns("deepspace_goals")}
    additions: tuple[tuple[str, sa.Column], ...] = (
        (
            "phase",
            sa.Column(
                "phase",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'active'"),
            ),
        ),
        (
            "revision",
            sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        ),
        (
            "max_goal_rounds",
            sa.Column(
                "max_goal_rounds",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("3"),
            ),
        ),
        (
            "rounds_started",
            sa.Column(
                "rounds_started",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        ),
        (
            "blocked_reason",
            sa.Column(
                "blocked_reason",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
        ),
    )
    for name, column in additions:
        if name not in columns:
            op.add_column("deepspace_goals", column)

    # Preserve the meaning of legacy status values when phase is introduced.
    if "status" in columns:
        bind.execute(
            text(
                "UPDATE deepspace_goals "
                "SET phase = CASE "
                "WHEN status = 'reached' THEN 'complete' "
                "WHEN status = 'abandoned' THEN 'blocked' "
                "ELSE 'active' END"
            )
        )

    indexes = {index["name"] for index in inspect(bind).get_indexes("deepspace_goals")}
    if "ix_deepspace_goals_phase" not in indexes:
        op.create_index("ix_deepspace_goals_phase", "deepspace_goals", ["phase"])


def downgrade() -> None:
    """Remove the runtime fields added by this migration."""
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("deepspace_goals"):
        return

    indexes = {index["name"] for index in inspector.get_indexes("deepspace_goals")}
    if "ix_deepspace_goals_phase" in indexes:
        op.drop_index("ix_deepspace_goals_phase", table_name="deepspace_goals")

    columns = {column["name"] for column in inspect(bind).get_columns("deepspace_goals")}
    for name in ("blocked_reason", "rounds_started", "max_goal_rounds", "revision", "phase"):
        if name in columns:
            op.drop_column("deepspace_goals", name)
