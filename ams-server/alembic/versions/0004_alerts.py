"""P4 alerts — the operational alert store (design note §4, decision 3).

A single new table, ``alerts``, fed by the gRPC/service layer:

* ``all_exhausted`` / ``quarantine`` account events (``_store_event``),
* reconcile-on-report drift (same transaction as the snapshot insert),
* server offline (``_mark_offline`` + the last_seen_at sweeper).

Tenant isolation reuses the P1 anchor: the composite ``(server_id, tenant_id)``
foreign key into ``servers(id, tenant_id)``. A partial unique index on
``dedupe_key WHERE status = 'open'`` keeps at most one open alert per key so a
persistent condition reported every 5 minutes never floods.

No proto change, no contract change — a DB-only schema addition.

Revision ID: 0004_alerts
Revises: 0003_switching_policy
Create Date: 2026-08-08
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_alerts"
down_revision = "0003_switching_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alerts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("server_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("source_snapshot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acked_by", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["server_id", "tenant_id"],
            ["servers.id", "servers.tenant_id"],
            name="fk_alerts_server_tenant",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "kind IN ('all_exhausted','drift','server_offline','quarantine')",
            name="ck_alerts_kind",
        ),
        sa.CheckConstraint(
            "severity IN ('critical','warning')", name="ck_alerts_severity"
        ),
        sa.CheckConstraint(
            "status IN ('open','acked','resolved')", name="ck_alerts_status"
        ),
    )
    op.create_index(
        "uq_alerts_open_dedupe",
        "alerts",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index("ix_alerts_tenant_status", "alerts", ["tenant_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_alerts_tenant_status", table_name="alerts")
    op.drop_index("uq_alerts_open_dedupe", table_name="alerts")
    op.drop_table("alerts")
