"""Add workspace registry tables for UUID-based workspace identity.

Revision ID: 20261110_0001
Revises: 20260402_0015
Create Date: 2026-11-10
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "20261110_0001"
down_revision = "20260402_0015"
branch_labels = None
depends_on = None


def _gen_uuid() -> str:
    return str(uuid.uuid4())


def upgrade() -> None:
    # Workspace registry table
    op.create_table(
        "deepspace_workspace_registry",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=_gen_uuid),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("slug", sa.String(128), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("custom_path", sa.Text, nullable=True),
        sa.Column("is_default", sa.Boolean, nullable=False, default=False, index=True),
        sa.Column("display_order", sa.Integer, nullable=False, default=0),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            onupdate=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_unique_constraint(
        "uq_workspace_slug_per_account",
        "deepspace_workspace_registry",
        ["tenant_id", "user_id", "slug"],
    )
    op.create_index(
        "ix_workspace_registry_account",
        "deepspace_workspace_registry",
        ["tenant_id", "user_id"],
    )
    op.create_index(
        "ix_workspace_registry_default",
        "deepspace_workspace_registry",
        ["tenant_id", "user_id", "is_default"],
    )

    # Workspace session positions table
    op.create_table(
        "deepspace_workspace_session_positions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=_gen_uuid),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("deepspace_workspace_registry.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "conversation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            onupdate=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_unique_constraint(
        "uq_workspace_session_position",
        "deepspace_workspace_session_positions",
        ["workspace_id", "conversation_id"],
    )
    op.create_unique_constraint(
        "uq_workspace_position",
        "deepspace_workspace_session_positions",
        ["workspace_id", "position"],
    )
    op.create_index(
        "ix_session_positions_conversation",
        "deepspace_workspace_session_positions",
        ["conversation_id"],
    )

    # Workspace order table (global workspace list order per account)
    op.create_table(
        "deepspace_workspace_order",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=_gen_uuid),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("deepspace_workspace_registry.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            onupdate=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_unique_constraint(
        "uq_account_workspace_order",
        "deepspace_workspace_order",
        ["tenant_id", "user_id", "workspace_id"],
    )
    op.create_unique_constraint(
        "uq_account_workspace_position",
        "deepspace_workspace_order",
        ["tenant_id", "user_id", "position"],
    )
    op.create_index(
        "ix_workspace_order_account",
        "deepspace_workspace_order",
        ["tenant_id", "user_id"],
    )

    # Workspace deleted log table
    op.create_table(
        "deepspace_workspace_deleted_log",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=_gen_uuid),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("slug", sa.String(128), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("custom_path", sa.Text, nullable=True),
        sa.Column("was_default", sa.Boolean, nullable=False, default=False),
        sa.Column("quarantine_path", sa.Text, nullable=True),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("deleted_by", UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_deleted_log_account",
        "deepspace_workspace_deleted_log",
        ["tenant_id", "user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_deleted_log_account", table_name="deepspace_workspace_deleted_log")
    op.drop_table("deepspace_workspace_deleted_log")

    op.drop_index("ix_workspace_order_account", table_name="deepspace_workspace_order")
    op.drop_constraint("uq_account_workspace_position", "deepspace_workspace_order", type_="unique")
    op.drop_constraint("uq_account_workspace_order", "deepspace_workspace_order", type_="unique")
    op.drop_table("deepspace_workspace_order")

    op.drop_index(
        "ix_session_positions_conversation", table_name="deepspace_workspace_session_positions"
    )
    op.drop_constraint(
        "uq_workspace_position", "deepspace_workspace_session_positions", type_="unique"
    )
    op.drop_constraint(
        "uq_workspace_session_position", "deepspace_workspace_session_positions", type_="unique"
    )
    op.drop_table("deepspace_workspace_session_positions")

    op.drop_index("ix_workspace_registry_default", table_name="deepspace_workspace_registry")
    op.drop_index("ix_workspace_registry_account", table_name="deepspace_workspace_registry")
    op.drop_constraint(
        "uq_workspace_slug_per_account", "deepspace_workspace_registry", type_="unique"
    )
    op.drop_table("deepspace_workspace_registry")
