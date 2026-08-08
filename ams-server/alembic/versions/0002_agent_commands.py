"""P2 command outbox — design note §2 (`agent_commands`).

REST transition actions enqueue a row; the gRPC session process drains it onto
the live agent. Proto is unchanged — this is a DB-only schema addition.

Revision ID: 0002_agent_commands
Revises: 0001_initial_inventory
Create Date: 2026-08-08
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_agent_commands"
down_revision = "0001_initial_inventory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_commands",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("server_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assignment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("command_id", sa.Text(), nullable=False),
        sa.Column("command_type", sa.String(16), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        # ★ isolation anchor: the command reaches its server through the same
        # composite (server_id, tenant_id) key the rest of §5.1 uses, so a
        # command can never be attached to a server in another tenant.
        sa.ForeignKeyConstraint(
            ["server_id", "tenant_id"],
            ["servers.id", "servers.tenant_id"],
            name="fk_agent_commands_server_tenant",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("command_id", name="uq_agent_commands_command_id"),
        sa.CheckConstraint(
            "command_type IN ('deliver','recall','activate','deactivate')",
            name="ck_agent_commands_command_type",
        ),
        sa.CheckConstraint(
            "status IN ('queued','sent','acked','failed')",
            name="ck_agent_commands_status",
        ),
    )
    op.create_index(
        "ix_agent_commands_dispatch", "agent_commands", ["server_id", "status"]
    )


def downgrade() -> None:
    op.drop_table("agent_commands")
