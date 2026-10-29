"""Add the DeepSpace subagent and session event tables.

Revision ID: 20261020_0001
Revises: 20261017_0002
Create Date: 2026-10-20 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261020_0001"
down_revision = "20261017_0002"
branch_labels = None
depends_on = None

UUID = sa.UUID()


def upgrade() -> None:
    """Create the delegated-child and typed session event tables."""
    op.create_table(
        "deepspace_subagent_runs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("parent_run_id", UUID, nullable=True),
        sa.Column("parent_agent_id", UUID, nullable=True),
        sa.Column("origin", sa.String(length=32), nullable=False, server_default="subagent"),
        sa.Column("mode", sa.String(length=32), nullable=False, server_default="one-shot"),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("delegation_depth", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("agent_provider", sa.String(length=100), nullable=True),
        sa.Column("agent_model", sa.String(length=200), nullable=True),
        sa.Column("reasoning_effort", sa.String(length=40), nullable=True),
        sa.Column("persona", sa.Text(), nullable=True),
        sa.Column("tool_filter", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("descriptor", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("inherited_history", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("pending_messages", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("turn_history", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="running"),
        sa.Column("busy", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "cancel_requested", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("result_text", sa.Text(), nullable=True),
        sa.Column("stop_reason", sa.String(length=80), nullable=True),
        sa.Column("turn_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["parent_run_id"], ["deepspace_agent_runs.id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        "ix_deepspace_subagent_runs_tenant_id", "deepspace_subagent_runs", ["tenant_id"]
    )
    op.create_index("ix_deepspace_subagent_runs_user_id", "deepspace_subagent_runs", ["user_id"])
    op.create_index(
        "ix_deepspace_subagent_runs_conversation_id",
        "deepspace_subagent_runs",
        ["conversation_id"],
    )
    op.create_index(
        "ix_deepspace_subagent_runs_parent_run_id",
        "deepspace_subagent_runs",
        ["parent_run_id"],
    )
    op.create_index(
        "ix_deepspace_subagent_runs_parent_agent_id",
        "deepspace_subagent_runs",
        ["parent_agent_id"],
    )
    op.create_index("ix_deepspace_subagent_runs_status", "deepspace_subagent_runs", ["status"])

    op.create_table(
        "deepspace_session_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("run_id", UUID, nullable=True),
        sa.Column("subagent_id", UUID, nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("turn", sa.Integer(), nullable=True),
        sa.Column("step", sa.Integer(), nullable=True),
        sa.Column("tool_name", sa.String(length=100), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["deepspace_agent_runs.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_deepspace_session_events_tenant_id", "deepspace_session_events", ["tenant_id"]
    )
    op.create_index("ix_deepspace_session_events_user_id", "deepspace_session_events", ["user_id"])
    op.create_index(
        "ix_deepspace_session_events_conversation_id",
        "deepspace_session_events",
        ["conversation_id"],
    )
    op.create_index("ix_deepspace_session_events_run_id", "deepspace_session_events", ["run_id"])
    op.create_index(
        "ix_deepspace_session_events_subagent_id", "deepspace_session_events", ["subagent_id"]
    )
    op.create_index("ix_deepspace_session_events_seq", "deepspace_session_events", ["seq"])
    op.create_index(
        "ix_deepspace_session_events_event_type", "deepspace_session_events", ["event_type"]
    )
    op.create_index("ix_deepspace_session_events_turn", "deepspace_session_events", ["turn"])

    op.create_table(
        "deepspace_goals",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "tenant_id", "user_id", "conversation_id", "name", name="uq_deepspace_goals_name"
        ),
    )
    op.create_index("ix_deepspace_goals_tenant_id", "deepspace_goals", ["tenant_id"])
    op.create_index("ix_deepspace_goals_user_id", "deepspace_goals", ["user_id"])
    op.create_index("ix_deepspace_goals_conversation_id", "deepspace_goals", ["conversation_id"])
    op.create_index("ix_deepspace_goals_status", "deepspace_goals", ["status"])


def downgrade() -> None:
    """Remove the delegated-child, goal, and typed session event tables."""
    op.drop_table("deepspace_goals")
    op.drop_table("deepspace_session_events")
    op.drop_table("deepspace_subagent_runs")
