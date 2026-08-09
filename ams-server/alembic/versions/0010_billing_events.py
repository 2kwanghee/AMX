"""F5 billing_events outbox — usage_snapshots-derived internal charging schema.

* ``billing_events`` — one aggregated row per (tenant, closed UTC day), created
  idempotently by ``services.billing.sweep_billing``. The
  ``UNIQUE (tenant_id, kind, period_start)`` is the idempotency anchor.
* ``billing_cursors`` — the sweep watermark (one row per ``kind``).

Internal billing only; no external payment integration. proto/contracts
unchanged (F5 design decision: no proto change).

Revision ID: 0010_billing_events
Revises: 0009_full_central_policy
Create Date: 2026-08-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID


revision = "0010_billing_events"
down_revision = "0009_full_central_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_events",
        sa.Column("id", PgUUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", PgUUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("exported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "kind", "period_start",
            name="uq_billing_events_tenant_kind_period",
        ),
    )
    op.create_index(
        "ix_billing_events_tenant_status", "billing_events", ["tenant_id", "status"]
    )

    op.create_table(
        "billing_cursors",
        sa.Column("kind", sa.String(32), primary_key=True),
        sa.Column("watermark", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("billing_cursors")
    op.drop_index("ix_billing_events_tenant_status", table_name="billing_events")
    op.drop_table("billing_events")
