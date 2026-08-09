"""D2 sent-未ack recovery — send_attempts counter on agent_commands.

The poll loop marks a queued command ``sent`` but nothing re-sends it if the
agent never acks (recovery-architecture §2). The sent-timeout sweeper re-queues
a stuck ``sent`` command (idempotent, same command_id) and counts the attempts
here; past MAX_SEND_ATTEMPTS the command is failed and its assignment reverted.

Additive, non-destructive: a plain NOT NULL DEFAULT 0 integer column.

Revision ID: 0006_send_attempts
Revises: 0005_credential_resync
Create Date: 2026-08-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006_send_attempts"
down_revision = "0005_credential_resync"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_commands",
        sa.Column(
            "send_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_commands", "send_attempts")
