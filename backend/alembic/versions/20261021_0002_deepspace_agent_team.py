"""Add the DeepSpace agent team tables.

Revision ID: 20261021_0002
Revises: 20261021_0001
Create Date: 2026-10-21 06:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261021_0002"
down_revision = "20261021_0001"
branch_labels = None
depends_on = None

UUID = sa.UUID()


def upgrade() -> None:
    """Create the teammate, message, and shared task board tables."""
    op.create_table(
        "deepspace_team_members",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=True),
        sa.Column("model", sa.String(length=200), nullable=True),
        sa.Column("persona", sa.Text(), nullable=True),
        sa.Column("tool_filter", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("descriptor", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("child_id", sa.UUID(), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="idle"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["child_id"], ["deepspace_subagent_runs.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            "conversation_id",
            "name",
            name="uq_deepspace_team_members_name",
        ),
    )
    op.create_index("ix_deepspace_team_members_tenant_id", "deepspace_team_members", ["tenant_id"])
    op.create_index("ix_deepspace_team_members_user_id", "deepspace_team_members", ["user_id"])
    op.create_index(
        "ix_deepspace_team_members_conversation_id",
        "deepspace_team_members",
        ["conversation_id"],
    )
    op.create_index("ix_deepspace_team_members_state", "deepspace_team_members", ["state"])

    op.create_table(
        "deepspace_team_messages",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("sender", sa.String(length=64), nullable=False),
        sa.Column("recipient", sa.String(length=64), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
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
    op.create_index(
        "ix_deepspace_team_messages_tenant_id", "deepspace_team_messages", ["tenant_id"]
    )
    op.create_index("ix_deepspace_team_messages_user_id", "deepspace_team_messages", ["user_id"])
    op.create_index(
        "ix_deepspace_team_messages_conversation_id",
        "deepspace_team_messages",
        ["conversation_id"],
    )
    op.create_index(
        "ix_deepspace_team_messages_recipient", "deepspace_team_messages", ["recipient"]
    )
    op.create_index("ix_deepspace_team_messages_state", "deepspace_team_messages", ["state"])
    op.create_index(
        "ix_deepspace_team_messages_recipient_state",
        "deepspace_team_messages",
        ["recipient", "state"],
    )

    op.create_table(
        "deepspace_team_tasks",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("owner", sa.String(length=64), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("depends_on", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
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
    op.create_index("ix_deepspace_team_tasks_tenant_id", "deepspace_team_tasks", ["tenant_id"])
    op.create_index("ix_deepspace_team_tasks_user_id", "deepspace_team_tasks", ["user_id"])
    op.create_index(
        "ix_deepspace_team_tasks_conversation_id", "deepspace_team_tasks", ["conversation_id"]
    )
    op.create_index("ix_deepspace_team_tasks_state", "deepspace_team_tasks", ["state"])
    op.create_index("ix_deepspace_team_tasks_owner", "deepspace_team_tasks", ["owner"])


def downgrade() -> None:
    """Remove the teammate, message, and shared task board tables."""
    op.drop_table("deepspace_team_tasks")
    op.drop_table("deepspace_team_messages")
    op.drop_table("deepspace_team_members")
