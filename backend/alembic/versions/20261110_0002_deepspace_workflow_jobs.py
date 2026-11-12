"""Add durable DeepSpace workflow jobs and progress events.

Revision ID: 20261110_0002
Revises: 20261110_0001
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "20261110_0002"
down_revision = "20261110_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deepspace_workflow_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="script"),
        sa.Column("workflow_name", sa.String(length=120), nullable=False),
        sa.Column("script", sa.Text(), nullable=True),
        sa.Column("input_text", sa.Text(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("args", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("roles", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("permissions", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("stop_reason", sa.String(length=32), nullable=True),
        sa.Column("current_phase", sa.String(length=255), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("result", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("output", sa.Text(), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("agents_spawned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_deepspace_workflow_jobs_tenant_id", "deepspace_workflow_jobs", ["tenant_id"]
    )
    op.create_index("ix_deepspace_workflow_jobs_user_id", "deepspace_workflow_jobs", ["user_id"])
    op.create_index(
        "ix_deepspace_workflow_jobs_conversation_id", "deepspace_workflow_jobs", ["conversation_id"]
    )
    op.create_index("ix_deepspace_workflow_jobs_status", "deepspace_workflow_jobs", ["status"])

    op.create_table(
        "deepspace_workflow_job_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("deepspace_workflow_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("job_id", "sequence", name="uq_deepspace_workflow_job_event_sequence"),
    )
    op.create_index(
        "ix_deepspace_workflow_job_events_job_id", "deepspace_workflow_job_events", ["job_id"]
    )
    op.create_index(
        "ix_deepspace_workflow_job_events_tenant_id", "deepspace_workflow_job_events", ["tenant_id"]
    )
    op.create_index(
        "ix_deepspace_workflow_job_events_user_id", "deepspace_workflow_job_events", ["user_id"]
    )
    op.create_index(
        "ix_deepspace_workflow_job_events_conversation_id",
        "deepspace_workflow_job_events",
        ["conversation_id"],
    )
    op.create_index(
        "ix_deepspace_workflow_job_events_event_type",
        "deepspace_workflow_job_events",
        ["event_type"],
    )


def downgrade() -> None:
    op.drop_table("deepspace_workflow_job_events")
    op.drop_table("deepspace_workflow_jobs")
