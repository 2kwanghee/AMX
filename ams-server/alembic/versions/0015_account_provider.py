"""account provider axis — P2a.

Adds ``accounts.provider`` (NOT NULL, default 'claude') and widens the
per-tenant email uniqueness to ``(tenant_id, provider, email)`` so the same
mailbox can back one account per provider. Every existing row predates any
non-Claude provider, so the backfill is a straight 'claude'.

The unique constraint keeps its name (``uq_accounts_tenant_email``) across the
swap; only its column set changes.

Revision ID: 0015_account_provider
Revises: 0014_self_update_failed_alert
Create Date: 2026-08-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_account_provider"
down_revision = "0014_self_update_failed_alert"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("provider", sa.String(32), nullable=False, server_default="claude"),
    )
    # Explicit backfill for existing rows. The server_default already covers the
    # inserted values, but this keeps the intent visible and independent of it.
    op.execute("UPDATE accounts SET provider = 'claude' WHERE provider IS NULL")
    op.drop_constraint("uq_accounts_tenant_email", "accounts", type_="unique")
    op.create_unique_constraint(
        "uq_accounts_tenant_email", "accounts", ["tenant_id", "provider", "email"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_accounts_tenant_email", "accounts", type_="unique")
    op.create_unique_constraint(
        "uq_accounts_tenant_email", "accounts", ["tenant_id", "email"]
    )
    op.drop_column("accounts", "provider")
