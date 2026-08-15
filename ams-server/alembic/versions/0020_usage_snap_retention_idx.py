"""usage_snapshots retention-sweep partial index — reported_at WHERE report_type='usage'.

The retention sweep (usage_cost.sweep_snapshot_retention) deletes by the exact
predicate ``report_type = 'usage' AND reported_at < X``. That filter carries no
``tenant_id``, so the existing composite ``ix_usage_snapshots_tenant_type_time``
(tenant_id, report_type, reported_at) — leading with tenant_id — cannot be used,
and the first purge over a large backlog degrades to a seq-scan.

A PARTIAL index on ``reported_at WHERE report_type = 'usage'`` is used rather than
a plain ``(report_type, reported_at)`` composite: the sweep's report_type is a
constant equality, so folding it into the index predicate leaves reported_at as
the sole ordered key (a tight range scan) and restricts the index to usage rows,
keeping it smaller than a composite that would also index switch_event rows the
sweep never reads.

CREATE INDEX CONCURRENTLY was considered for zero-downtime on a large table but is
NOT used: alembic/env.py wraps run_migrations in ``context.begin_transaction()``,
so every migration runs inside a transaction, and CONCURRENTLY is disallowed in a
transaction block. Switching this migration to autocommit would break the repo's
one-transaction-per-run convention; the plain build matches sibling index
migration 0018 (same table). A brief write lock during the build is accepted.

Revision ID: 0020_usage_snap_retention_idx
Revises: 0019_watermark_future_alert
Create Date: 2026-08-15
"""

from __future__ import annotations

from alembic import op

revision = "0020_usage_snap_retention_idx"
down_revision = "0019_watermark_future_alert"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_usage_snapshots_usage_reported_at",
        "usage_snapshots",
        ["reported_at"],
        postgresql_where="report_type = 'usage'",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_usage_snapshots_usage_reported_at", table_name="usage_snapshots"
    )
