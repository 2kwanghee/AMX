"""F4 (O4-B) full central policy — cooldown/hysteresis columns on servers.

* ``servers.cooldown_seconds`` / ``servers.hysteresis_pct`` — the remaining
  autoswitch policy AMS now owns and re-asserts each session via SetPolicy
  (proto cmd 17, fields 3/4). NULL keeps the tsamx-local default; AMS delivers
  a negative "unset" sentinel so a stored 0 (a real value, e.g. cooldown
  disabled) is distinguishable from unset. DB-only schema addition; proto
  already carries the fields on this branch.

Revision ID: 0009_full_central_policy
Revises: 0008_tenant_deks
Create Date: 2026-08-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0009_full_central_policy"
down_revision = "0008_tenant_deks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("cooldown_seconds", sa.Float(), nullable=True))
    op.add_column("servers", sa.Column("hysteresis_pct", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "hysteresis_pct")
    op.drop_column("servers", "cooldown_seconds")
