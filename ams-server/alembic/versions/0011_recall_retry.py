"""D1 recall-failure recovery — recall_retry_count counter on assignments.

A recall that fails (DIVERGED/REJECTED, or a lost ack) settles to ``recalling``
with ``pending_command_id`` NULL and can be re-armed by REST ``:recall``. Without
a bound an operator (or a script) could re-arm a permanently-failing recall
forever. This counter caps the manual retries (AMX_MAX_RECALL_RETRIES, default 3);
past the cap the REST action returns 409 and only opens a ``recall_failed`` alert.

Additive, non-destructive: a plain NOT NULL DEFAULT 0 integer column.

Revision ID: 0011_recall_retry
Revises: 0010_billing_events
Create Date: 2026-08-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0011_recall_retry"
down_revision = "0010_billing_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assignments",
        sa.Column(
            "recall_retry_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    # Admit the new account-scoped D1 alert kind into the ck_alerts_kind CHECK.
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint(
        "ck_alerts_kind",
        "alerts",
        "kind IN ('all_exhausted','drift','server_offline','quarantine','recall_failed')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint(
        "ck_alerts_kind",
        "alerts",
        "kind IN ('all_exhausted','drift','server_offline','quarantine')",
    )
    op.drop_column("assignments", "recall_retry_count")
