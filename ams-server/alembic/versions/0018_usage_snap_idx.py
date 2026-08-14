"""usage_snapshots (tenant_id, report_type, reported_at) index — usage-cost PR2.

The usage-cost rollup sweep and the live-tail integration both scan
``usage_snapshots`` by ``tenant_id`` + ``report_type == 'usage'`` over a
``reported_at`` day range. The only pre-existing index is
``(server_id, reported_at)`` (0-th migration), which does not serve a
tenant-scoped day scan. This composite index matches the filter directly.

Revision ID: 0018_usage_snap_idx
Revises: 0017_usage_cost_schema
Create Date: 2026-08-14
"""

from __future__ import annotations

from alembic import op

revision = "0018_usage_snap_idx"
down_revision = "0017_usage_cost_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_usage_snapshots_tenant_type_time",
        "usage_snapshots",
        ["tenant_id", "report_type", "reported_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_usage_snapshots_tenant_type_time", table_name="usage_snapshots"
    )
