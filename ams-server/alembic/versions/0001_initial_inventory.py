"""P1 inventory schema — docs/AMX-DESIGN.md §5.1.

Revision ID: 0001_initial_inventory
Revises:
Create Date: 2026-08-08
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial_inventory"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('active','suspended')", name="ck_tenants_status"),
    )

    op.create_table(
        "accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("credential_type", sa.String(32), nullable=False),
        sa.Column("encrypted_secret", sa.Text(), nullable=True),
        sa.Column("secret_masked", sa.Text(), nullable=True),
        sa.Column("account_uuid", sa.Text(), nullable=True),
        sa.Column("organization_name", sa.Text(), nullable=True),
        sa.Column("scopes", postgresql.JSONB(), nullable=True),
        sa.Column("credential_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="available"),
        sa.Column("last_switched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        # ★ isolation anchor: the target of assignments' composite FK.
        sa.UniqueConstraint("id", "tenant_id", name="uq_accounts_id_tenant"),
        sa.UniqueConstraint("tenant_id", "email", name="uq_accounts_tenant_email"),
        sa.CheckConstraint(
            "credential_type IN ('oauth','api_key')", name="ck_accounts_credential_type"
        ),
        sa.CheckConstraint(
            "status IN ('available','assigned','disabled','quarantined')",
            name="ck_accounts_status",
        ),
    )

    op.create_table(
        "servers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("hostname", sa.Text(), nullable=True),
        sa.Column("enroll_token_hash", sa.Text(), nullable=True),
        sa.Column("enroll_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("server_cred_hash", sa.Text(), nullable=True),
        sa.Column("agent_id", sa.Text(), nullable=True),
        sa.Column("agent_version", sa.Text(), nullable=True),
        sa.Column("tsamx_version", sa.Text(), nullable=True),
        sa.Column("switch_mode", sa.String(16), nullable=False, server_default="auto"),
        sa.Column("status", sa.String(16), nullable=False, server_default="offline"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        # ★ isolation anchor.
        sa.UniqueConstraint("id", "tenant_id", name="uq_servers_id_tenant"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_servers_tenant_name"),
        sa.CheckConstraint("switch_mode IN ('auto','manual')", name="ck_servers_switch_mode"),
        sa.CheckConstraint(
            "status IN ('online','offline','degraded')", name="ck_servers_status"
        ),
    )

    op.create_table(
        "assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("server_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("pending_command_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        # ★ The two composite foreign keys are the tenant-isolation invariant
        # (§5.1). A row carries one tenant_id; an account and a server from
        # different tenants cannot both match it, so the INSERT fails in the
        # database with no application check involved.
        sa.ForeignKeyConstraint(
            ["account_id", "tenant_id"],
            ["accounts.id", "accounts.tenant_id"],
            name="fk_assignments_account_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["server_id", "tenant_id"],
            ["servers.id", "servers.tenant_id"],
            name="fk_assignments_server_tenant",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "state IN ('pending','delivering','active','inactive','quarantined',"
            "'recalling','detached')",
            name="ck_assignments_state",
        ),
    )

    # One account is installed on at most one server at a time; detached rows
    # are retained for audit and are exempt.
    op.create_index(
        "uq_assignments_active_account",
        "assignments",
        ["tenant_id", "account_id"],
        unique=True,
        postgresql_where=sa.text("state <> 'detached'"),
    )
    op.create_index("ix_assignments_server", "assignments", ["tenant_id", "server_id"])

    op.create_table(
        "usage_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("server_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("report_type", sa.String(32), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["server_id", "tenant_id"],
            ["servers.id", "servers.tenant_id"],
            name="fk_usage_snapshots_server_tenant",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "report_type IN ('usage','switch_event')", name="ck_usage_snapshots_report_type"
        ),
    )
    op.create_index(
        "ix_usage_snapshots_server_time", "usage_snapshots", ["server_id", "reported_at"]
    )


def downgrade() -> None:
    op.drop_table("usage_snapshots")
    op.drop_table("assignments")
    op.drop_table("servers")
    op.drop_table("accounts")
    op.drop_table("tenants")
