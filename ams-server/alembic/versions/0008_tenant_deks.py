"""F2 envelope encryption — tenant_deks table + backfill (design §3, §4-A).

Introduces per-tenant data-encryption keys. Each row is one wrapped DEK version;
the active one is the highest ``version`` with ``retired_at`` NULL. The KEK
provider (local AES-256-GCM MVP) wraps the DEK with the tenant_id bound as AAD,
so a wrapped DEK is useless under any other tenant.

This migration is step A of the no-downtime rollout: it creates the table and
backfills a v1 DEK for every existing tenant (local KEK wrap), and does NOT
touch ``accounts``. Credential ciphertext stays legacy Fernet until the code is
deployed and ``AMX_ENVELOPE_WRITE=1`` flips new writes to v2 (step B/C).

Revision ID: 0008_tenant_deks
Revises: 0007_admins_sessions
Create Date: 2026-08-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_tenant_deks"
down_revision = "0007_admins_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_deks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("kek_provider", sa.String(length=32), nullable=False),
        sa.Column("kek_key_id", sa.Text(), nullable=False),
        sa.Column(
            "algorithm", sa.String(length=32), nullable=False, server_default="AES-256-GCM"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "version", name="uq_tenant_deks_tenant_version"),
    )

    # Backfill a v1 DEK for every existing tenant through the same runtime path
    # (ensure_tenant_dek) the app uses, so the wrapping matches exactly what it
    # will later unwrap.
    from sqlalchemy.orm import Session

    from app.core.kek import ensure_tenant_dek

    bind = op.get_bind()
    tenant_ids = list(bind.execute(sa.text("SELECT id FROM tenants")).scalars())
    with Session(bind=bind) as session:
        for tenant_id in tenant_ids:
            ensure_tenant_dek(session, tenant_id)
        session.commit()


def downgrade() -> None:
    # Refuse if any credential is still v2: dropping tenant_deks would strand its
    # DEK and make that ciphertext permanently unopenable (ADVERSARY H4). The
    # operator must first run `rewrap_secrets.py --reverse` to fold v2 back to
    # legacy Fernet, then downgrade.
    bind = op.get_bind()
    orphaned = bind.execute(
        sa.text("SELECT 1 FROM accounts WHERE encrypted_secret LIKE 'v2:%' LIMIT 1")
    ).first()
    if orphaned is not None:
        raise RuntimeError(
            "downgrade refused: v2 (tenant-DEK) credentials exist. Run "
            "`AMX_ENVELOPE_WRITE unset python scripts/rewrap_secrets.py --reverse` "
            "to fold them back to legacy Fernet before dropping tenant_deks."
        )
    op.drop_table("tenant_deks")
