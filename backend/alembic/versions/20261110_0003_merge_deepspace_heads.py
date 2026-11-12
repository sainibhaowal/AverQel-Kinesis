"""Merge the existing DeepSpace migration line with the workflow line.

Revision ID: 20261110_0003
Revises: 20261028_0002, 20261110_0002
"""

from __future__ import annotations

revision = "20261110_0003"
down_revision = ("20261028_0002", "20261110_0002")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Merge point only; both parent branches already apply their changes."""


def downgrade() -> None:
    """Merge point only; Alembic restores the two parent heads."""
