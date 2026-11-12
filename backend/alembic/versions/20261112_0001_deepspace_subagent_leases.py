"""Add fenced worker leases to durable DeepSpace child runs.

Revision ID: 20261112_0001
Revises: 20261110_0003
Create Date: 2026-11-12
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261112_0001"
down_revision = "20261110_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deepspace_subagent_runs", sa.Column("lease_token", sa.String(64), nullable=True))
    op.add_column(
        "deepspace_subagent_runs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "deepspace_subagent_runs",
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_deepspace_subagent_runs_lease_token",
        "deepspace_subagent_runs",
        ["lease_token"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_deepspace_subagent_runs_lease_token",
        table_name="deepspace_subagent_runs",
    )
    op.drop_column("deepspace_subagent_runs", "last_heartbeat_at")
    op.drop_column("deepspace_subagent_runs", "lease_expires_at")
    op.drop_column("deepspace_subagent_runs", "lease_token")
