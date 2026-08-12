"""self_update — admit the command type and its failure alert kind.

Two CHECK constraints stand between the new command and the database:
``ck_agent_commands_command_type`` (last set in 0003) has to accept
``self_update`` before the outbox row can be inserted at all, and
``ck_alerts_kind`` (last set in 0012) has to accept ``self_update_failed``.

The alert kind is not decoration. ``self_update`` is server-scoped
(assignment_id NULL), so a DIVERGED ack reverts no assignment and nothing else
would make the failure visible: the agent stays on its old binary and simply
nacks (preflight / git_pull_failed / commit_mismatch / build_failed). Without the
alert an operator would go on believing the fleet had moved to the new commit.

Both constraints follow 0012's handling exactly: drop and recreate.

Revision ID: 0014_self_update_failed_alert
Revises: 0013_server_metrics
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op

revision = "0014_self_update_failed_alert"
down_revision = "0013_server_metrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_agent_commands_command_type", "agent_commands", type_="check")
    op.create_check_constraint(
        "ck_agent_commands_command_type",
        "agent_commands",
        "command_type IN ('deliver','recall','activate','deactivate',"
        "'set_mode','switch_now','req_report','set_policy','self_update')",
    )
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint(
        "ck_alerts_kind",
        "alerts",
        "kind IN ('all_exhausted','drift','server_offline','quarantine',"
        "'recall_failed','command_send_failed','self_update_failed')",
    )


def downgrade() -> None:
    # Rows of the retired type/kind would violate the narrower constraints, so
    # they go first — an un-drained self_update is meaningless to an AMS that no
    # longer knows how to build one.
    op.execute("DELETE FROM agent_commands WHERE command_type = 'self_update'")
    op.execute("DELETE FROM alerts WHERE kind = 'self_update_failed'")
    op.drop_constraint("ck_agent_commands_command_type", "agent_commands", type_="check")
    op.create_check_constraint(
        "ck_agent_commands_command_type",
        "agent_commands",
        "command_type IN ('deliver','recall','activate','deactivate',"
        "'set_mode','switch_now','req_report','set_policy')",
    )
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint(
        "ck_alerts_kind",
        "alerts",
        "kind IN ('all_exhausted','drift','server_offline','quarantine',"
        "'recall_failed','command_send_failed')",
    )
