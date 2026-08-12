"""accounts.owner — P3 PR4.

A nullable free-text label naming whoever the account belongs to, for the
console listing and for audit. Deliberately not a foreign key to ``admins``:
the owner is frequently a person or team with no login on this system, and an
FK would make deleting that admin either fail or silently rewrite the label.

Nothing reads it for behaviour, so existing rows stay NULL and the downgrade is
a plain drop.

Revision ID: 0016_account_owner
Revises: 0015_account_provider
Create Date: 2026-08-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_account_owner"
down_revision = "0015_account_provider"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("owner", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("accounts", "owner")
