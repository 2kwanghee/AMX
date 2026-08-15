"""billing_watermark_future — admit the G27 watermark-future alert kind.

``ck_alerts_kind`` (last set in 0014) has to accept ``billing_watermark_future``
before the guard can raise it. A forward wall-clock step can park the usage-rollup
watermark ahead of real time; snapshots that then land below it are never rolled up
and go silently unbilled (BACKLOG G27). The new kind is system-scoped (server_id
NULL, tenant-scoped dedupe key) and carries no new command type, so — unlike 0014 —
only ``ck_alerts_kind`` moves. Follows 0014's handling exactly: drop and recreate.

Revision ID: 0019_watermark_future_alert
Revises: 0018_usage_snap_idx
Create Date: 2026-08-15
"""

from __future__ import annotations

from alembic import op

revision = "0019_watermark_future_alert"
down_revision = "0018_usage_snap_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint(
        "ck_alerts_kind",
        "alerts",
        "kind IN ('all_exhausted','drift','server_offline','quarantine',"
        "'recall_failed','command_send_failed','self_update_failed',"
        "'billing_watermark_future')",
    )


def downgrade() -> None:
    # Rows of the retired kind would violate the narrower constraint, so they go
    # first — a watermark-future alert is meaningless once the kind is unknown.
    op.execute("DELETE FROM alerts WHERE kind = 'billing_watermark_future'")
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint(
        "ck_alerts_kind",
        "alerts",
        "kind IN ('all_exhausted','drift','server_offline','quarantine',"
        "'recall_failed','command_send_failed','self_update_failed')",
    )
