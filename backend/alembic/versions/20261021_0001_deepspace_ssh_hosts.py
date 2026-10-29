"""Add the DeepSpace SSH host table.

Revision ID: 20261021_0001
Revises: 20261020_0002
Create Date: 2026-10-21 03:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261021_0001"
down_revision = "20261020_0002"
branch_labels = None
depends_on = None

UUID = sa.UUID()


def upgrade() -> None:
    """Create the reviewed-host table.

    The table holds no key material.  The private key is referenced by a
    name the deployment resolves, and the host key the connection must
    present is stored so a changed key is refused.
    """
    op.create_table(
        "deepspace_ssh_hosts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False, server_default="22"),
        sa.Column("username", sa.String(length=120), nullable=False),
        sa.Column("known_hosts_entry", sa.Text(), nullable=False),
        sa.Column("key_secret_ref", sa.String(length=255), nullable=True),
        sa.Column("remote_workspace", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_deepspace_ssh_hosts_tenant_id", "deepspace_ssh_hosts", ["tenant_id"])
    op.create_index("ix_deepspace_ssh_hosts_user_id", "deepspace_ssh_hosts", ["user_id"])
    op.create_index("ix_deepspace_ssh_hosts_enabled", "deepspace_ssh_hosts", ["enabled"])
    op.create_index(
        "ix_deepspace_ssh_hosts_tenant_id_name",
        "deepspace_ssh_hosts",
        ["tenant_id", "name"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the reviewed-host table."""
    op.drop_index("ix_deepspace_ssh_hosts_tenant_id_name", table_name="deepspace_ssh_hosts")
    op.drop_index("ix_deepspace_ssh_hosts_enabled", table_name="deepspace_ssh_hosts")
    op.drop_index("ix_deepspace_ssh_hosts_user_id", table_name="deepspace_ssh_hosts")
    op.drop_index("ix_deepspace_ssh_hosts_tenant_id", table_name="deepspace_ssh_hosts")
    op.drop_table("deepspace_ssh_hosts")
