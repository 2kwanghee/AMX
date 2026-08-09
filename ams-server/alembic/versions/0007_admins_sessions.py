"""F1 tenant RBAC — admins + admin_sessions (§5.1, P5 S2a).

`admins.tenant_id` is the isolation anchor (RESTRICT FK to tenants). The CHECK
constraint makes global-admin (tenant_id NULL) and tenant-admin (tenant_id NOT
NULL) the only representable shapes. Email uniqueness is case-insensitive via a
functional unique index on lower(email). `admin_sessions` stores only the token
hash and cascades from admins.

Revision ID: 0007_admins_sessions
Revises: 0006_send_attempts
Create Date: 2026-08-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007_admins_sessions"
down_revision = "0006_send_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admins",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "(role = 'global-admin' AND tenant_id IS NULL) OR "
            "(role = 'tenant-admin' AND tenant_id IS NOT NULL)",
            name="ck_admins_role_tenant",
        ),
        sa.CheckConstraint(
            "role IN ('global-admin','tenant-admin')", name="ck_admins_role"
        ),
    )
    # Case-insensitive email uniqueness — login normalises to lower() first.
    op.create_index(
        "uq_admins_email_lower",
        "admins",
        [sa.text("lower(email)")],
        unique=True,
    )

    op.create_table(
        "admin_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["admin_id"], ["admins.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("token_hash", name="uq_admin_sessions_token_hash"),
    )


def downgrade() -> None:
    op.drop_table("admin_sessions")
    op.drop_index("uq_admins_email_lower", table_name="admins")
    op.drop_table("admins")
