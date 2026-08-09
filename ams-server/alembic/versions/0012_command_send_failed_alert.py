"""D2 sent-final-failure alert — admit ``command_send_failed`` alert kind.

The D2 sent-ack sweeper (:func:`commands.sweep_sent_timeouts`) re-queues a stuck
``sent`` command up to MAX_SEND_ATTEMPTS and, past the cap, fails it and reverts
its assignment. Before this change the cap-exhausted failure was silent — for the
command types whose intent is lost on failure (deliver reverts to pending,
activate/deactivate drop the marker) only an operator can re-issue, so the
failure must be visible. A recall failure re-uses the existing ``recall_failed``
kind (D1); every other type opens the new ``command_send_failed`` kind.

Mirrors 0011's CHECK handling exactly: drop and recreate ``ck_alerts_kind``.

Revision ID: 0012_command_send_failed_alert
Revises: 0011_recall_retry
Create Date: 2026-08-09
"""

from __future__ import annotations

from alembic import op

revision = "0012_command_send_failed_alert"
down_revision = "0011_recall_retry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint(
        "ck_alerts_kind",
        "alerts",
        "kind IN ('all_exhausted','drift','server_offline','quarantine',"
        "'recall_failed','command_send_failed')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint(
        "ck_alerts_kind",
        "alerts",
        "kind IN ('all_exhausted','drift','server_offline','quarantine',"
        "'recall_failed')",
    )
