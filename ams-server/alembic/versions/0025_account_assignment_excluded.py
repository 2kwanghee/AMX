"""accounts.assignment_excluded — opt-in guard against dual-use OAuth accounts.

An account a person runs directly from their own profile (outside AMS) must
never also be handed to a server: the two sides race the same OAuth
refresh-token rotation and both end up broken (observed 2026-08-17). This
column lets an operator flag such an account so `create_assignment` refuses
any FUTURE assignment for it. Default False keeps every existing account
assignable exactly as before, and setting the flag never touches an
assignment already in place (docs/AMX-DESIGN.md §5.2).

Deliberately not named after "pool": the console already uses that word for
the usage-capacity gauge (`poolSummary.maxUtilizationPct`), and a badge
borrowing it next to that gauge would read as "excluded from usage
accounting," which this column is not.

Revision ID: 0025_account_assignment_excluded
Revises: 0024_admin_audit_logs
Create Date: 2026-08-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025_account_assignment_excluded"
down_revision = "0024_admin_audit_logs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column(
            "assignment_excluded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("accounts", "assignment_excluded")
