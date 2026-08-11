"""Server host metrics — cpu/mem/disk columns on servers.

The heartbeat now carries a best-effort SystemMetrics sample (proto §8). AMS
stores the latest trio on the ``servers`` row, plus a freshness stamp. All
nullable: NULL means "no metrics-bearing heartbeat yet", distinct from a real
0%. A heartbeat without the field leaves the columns untouched. DB-only addition;
the proto already carries the field on this branch.

⚠ DEPLOY ORDER — RUN THIS MIGRATION BEFORE STARTING THE NEW SERVER CODE.
The heartbeat handler (``_touch_last_seen``) writes cpu_pct/mem_pct/disk_pct/
metrics_reported_at whenever a beat carries metrics. If the new code runs against
a schema still at 0012, that UPDATE raises on the missing columns; because every
online server heartbeats on the same 30s cadence, the failures are broad and each
affected server is flipped offline. Apply ``alembic upgrade head`` first, then
roll the gRPC server. The columns are additive and nullable, so the old code runs
fine against this new schema — making "migrate first, then deploy" always safe.

Revision ID: 0013_server_metrics
Revises: 0012_command_send_failed_alert
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0013_server_metrics"
down_revision = "0012_command_send_failed_alert"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("cpu_pct", sa.Float(), nullable=True))
    op.add_column("servers", sa.Column("mem_pct", sa.Float(), nullable=True))
    op.add_column("servers", sa.Column("disk_pct", sa.Float(), nullable=True))
    op.add_column(
        "servers",
        sa.Column("metrics_reported_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("servers", "metrics_reported_at")
    op.drop_column("servers", "disk_pct")
    op.drop_column("servers", "mem_pct")
    op.drop_column("servers", "cpu_pct")
