"""P3 switching control — O4-C policy columns, reconcile-on-report drift, and
the widened command-type set (design note O4-C, §5).

* ``servers.threshold_pct`` / ``servers.default_strategy`` — the O4-C hybrid
  policy AMS owns and re-asserts each session via SetPolicy (proto cmd 17).
  NULL keeps the tsamx-local default (no injection).
* ``usage_snapshots.drift`` — reconcile_from_report marks desired-vs-actual
  drift here on each report.
* ``agent_commands.command_type`` check constraint widened to carry the
  session-control commands (set_mode / switch_now / req_report / set_policy).

Proto is unchanged from the SetPolicy addition already on this branch; this is a
DB-only schema addition.

Revision ID: 0003_switching_policy
Revises: 0002_agent_commands
Create Date: 2026-08-08
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_switching_policy"
down_revision = "0002_agent_commands"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("threshold_pct", sa.Float(), nullable=True))
    op.add_column(
        "servers", sa.Column("default_strategy", sa.String(16), nullable=True)
    )
    op.add_column(
        "usage_snapshots", sa.Column("drift", postgresql.JSONB(), nullable=True)
    )

    op.drop_constraint("ck_agent_commands_command_type", "agent_commands", type_="check")
    op.create_check_constraint(
        "ck_agent_commands_command_type",
        "agent_commands",
        "command_type IN ('deliver','recall','activate','deactivate',"
        "'set_mode','switch_now','req_report','set_policy')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_agent_commands_command_type", "agent_commands", type_="check")
    op.create_check_constraint(
        "ck_agent_commands_command_type",
        "agent_commands",
        "command_type IN ('deliver','recall','activate','deactivate')",
    )
    op.drop_column("usage_snapshots", "drift")
    op.drop_column("servers", "default_strategy")
    op.drop_column("servers", "threshold_pct")
