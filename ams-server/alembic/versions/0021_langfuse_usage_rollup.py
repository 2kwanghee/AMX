"""langfuse_usage_rollup — Langfuse Metrics API daily token roll-up (P4 console).

A compaction of the external Langfuse Metrics API keyed by
``(tenant_id, day, dimension, key)`` — that composite primary key is the
idempotency anchor the periodic sweep (``services.langfuse_metrics``) upserts on.
``dimension`` is ``"model"`` or ``"user"``; ``key`` is the provided model name
(``"unknown"`` when Langfuse reports it null) or the account email used as the
Metrics API ``userId`` filter. ``tenant_id`` is the operator-configured
``AMX_LANGFUSE_TENANT_ID`` under an ``ON DELETE CASCADE`` foreign key to
``tenants`` (mirroring ``usage_daily_rollup``), so a deleted tenant's monitoring
rows drop with it.

Token columns are BIGINT (monthly totals overflow int32). ``cache_read_tokens`` /
``cache_creation_tokens`` exist but stay 0 today — the Metrics API exposes no
cache-token measure — so cache detail can be backfilled later without a migration.

CREATE INDEX CONCURRENTLY is not used: alembic/env.py runs every migration inside
a transaction, where CONCURRENTLY is disallowed — same convention as 0017/0020.

Revision ID: 0021_langfuse_usage_rollup
Revises: 0020_usage_snap_retention_idx
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PgUUID

revision = "0021_langfuse_usage_rollup"
down_revision = "0020_usage_snap_retention_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "langfuse_usage_rollup",
        sa.Column("tenant_id", PgUUID(as_uuid=True), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("dimension", sa.String(16), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "cache_read_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "cache_creation_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("total_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "observation_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "day", "dimension", "key", name="pk_langfuse_usage_rollup"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_langfuse_usage_rollup_tenant",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_langfuse_usage_rollup_tenant_day", "langfuse_usage_rollup", ["tenant_id", "day"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_langfuse_usage_rollup_tenant_day", table_name="langfuse_usage_rollup"
    )
    op.drop_table("langfuse_usage_rollup")
