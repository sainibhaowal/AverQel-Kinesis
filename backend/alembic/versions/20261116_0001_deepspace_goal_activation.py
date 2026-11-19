"""Add the continuation activation column used by DeepSpace goals.

Revision ID: 20261116_0001
Revises: 20261112_0001
Create Date: 2026-11-16 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "20261116_0001"
down_revision = "20261112_0001"
branch_labels = None
depends_on = None

#: The index the ORM declares for the activation column.
_ACTIVATION_INDEX = "ix_deepspace_goals_activation"


def upgrade() -> None:
    """Add the activation column to the goal table."""
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("deepspace_goals"):
        return

    columns = {column["name"] for column in inspector.get_columns("deepspace_goals")}
    if "activation" not in columns:
        op.add_column(
            "deepspace_goals",
            sa.Column(
                "activation",
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("'disarmed'"),
            ),
        )

    index_names = {index["name"] for index in inspector.get_indexes("deepspace_goals")}
    if _ACTIVATION_INDEX not in index_names:
        op.create_index(_ACTIVATION_INDEX, "deepspace_goals", ["activation"])


def downgrade() -> None:
    """Remove the activation column from the goal table."""
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("deepspace_goals"):
        return

    index_names = {index["name"] for index in inspector.get_indexes("deepspace_goals")}
    if _ACTIVATION_INDEX in index_names:
        op.drop_index(_ACTIVATION_INDEX, table_name="deepspace_goals")

    columns = {column["name"] for column in inspector.get_columns("deepspace_goals")}
    if "activation" in columns:
        op.drop_column("deepspace_goals", "activation")
