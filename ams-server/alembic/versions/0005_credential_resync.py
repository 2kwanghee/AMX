"""O9 credential re-sync — monotonicity marker on accounts (design §5.7).

When an AMA pushes a refreshed OAuth credential set (CredentialUpdate,
AmaMessage.cred_update=15), AMS re-encrypts it into ``accounts.encrypted_secret``.
``credential_observed_at`` records the agent's local observation time of the
refresh, so a delayed or duplicated re-sync that is not strictly newer is
ignored and cannot roll the stored copy back (monotonicity, cf. P3 SetPolicy).

NULL means no re-sync has been observed yet — the first update always wins.

Revision ID: 0005_credential_resync
Revises: 0004_alerts
Create Date: 2026-08-08
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005_credential_resync"
down_revision = "0004_alerts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("credential_observed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("accounts", "credential_observed_at")
