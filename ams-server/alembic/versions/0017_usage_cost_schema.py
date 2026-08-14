"""account price + usage daily rollup — usage-cost PR1.

Two additive pieces, no behaviour attached yet:

* ``accounts.monthly_price`` (NUMERIC(10,2), nullable) and ``accounts.currency``
  (CHAR(3), NOT NULL default 'USD'). Existing rows keep a NULL price, which the
  allocation reads as "no cost to spread" — distinct from a real 0. The
  ``currency`` server_default is kept after the backfill, matching 0015's
  handling of ``provider``, so an insert that omits the unit still stores one.
* ``usage_daily_rollup``, a compaction of ``usage_snapshots`` keyed by
  ``(tenant_id, day, server_id, account_id)``. That composite primary key is
  the idempotency anchor for recomputing a day. ``server_id`` reuses the P1
  tenant anchor via a composite FK into ``servers(id, tenant_id)``;
  ``account_id`` deliberately has no FK so billing history outlives the
  deletion of the account it names.

Revision ID: 0017_usage_cost_schema
Revises: 0016_account_owner
Create Date: 2026-08-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PgUUID

revision = "0017_usage_cost_schema"
down_revision = "0016_account_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("monthly_price", sa.Numeric(10, 2), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
    )
    # Explicit backfill for existing rows; the server_default already covers
    # inserted values, but this keeps the intent visible and independent of it.
    op.execute("UPDATE accounts SET currency = 'USD' WHERE currency IS NULL")

    op.create_table(
        "usage_daily_rollup",
        sa.Column("tenant_id", PgUUID(as_uuid=True), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("server_id", PgUUID(as_uuid=True), nullable=False),
        sa.Column("account_id", PgUUID(as_uuid=True), nullable=False),
        sa.Column(
            "held_util_seconds", sa.Numeric(20, 6), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "observed_seconds", sa.Numeric(20, 6), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("snapshot_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "day", "server_id", "account_id", name="pk_usage_daily_rollup"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_usage_daily_rollup_tenant", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["server_id", "tenant_id"],
            ["servers.id", "servers.tenant_id"],
            name="fk_usage_daily_rollup_server_tenant",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_usage_daily_rollup_tenant_day", "usage_daily_rollup", ["tenant_id", "day"]
    )


def downgrade() -> None:
    op.drop_index("ix_usage_daily_rollup_tenant_day", table_name="usage_daily_rollup")
    op.drop_table("usage_daily_rollup")
    op.drop_column("accounts", "currency")
    op.drop_column("accounts", "monthly_price")
