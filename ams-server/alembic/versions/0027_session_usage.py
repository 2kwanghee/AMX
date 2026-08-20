"""session_usage — per-(session, model) cost-structure aggregate from Claude Code transcripts.

Claude Code already writes a full ``message.usage`` block into every ``assistant``
record of its session transcript, and two of those numbers exist nowhere else in
AMX: ``cache_creation.ephemeral_1h_input_tokens`` and ``ephemeral_5m_input_tokens``.
The 1h and 5m cache writes are priced differently, and the Langfuse Metrics API
reports them summed (``usageByType`` collapses the split), so the distinction is
unrecoverable downstream of Langfuse. The Stop hook
(``deploy/langfuse/session_usage_hook.py``) therefore posts the aggregate straight
to AMS.

Primary key ``(tenant_id, session_id, model)``: one session mixes models (main
model plus whatever a subagent runs on), so the model belongs in the key. The hook
may report the same session more than once — a session can Stop repeatedly — so
the ingest is an idempotent upsert on this key, replacing (not accumulating) the
recomputed totals, mirroring ``langfuse_usage_rollup``'s recompute-replace.

``account_id`` is nullable and carries **no** foreign key, following
``alerts.account_id``: the hook resolves the active account by asking tsamx for an
email, and that lookup can fail (tsamx absent, timeout, or an email that matches
no AMS account). A session with no resolvable account is still a valid
cost-structure observation, so it is stored with NULL rather than rejected. No FK
also means the row survives the deletion of the account it names.

``truncated`` marks a partial aggregate: the hook bounds how much of a transcript it
reads (line count, byte budget, iterations per record), and a report that hit one of
those bounds undercounts. Storing the flag beside the numbers keeps that visible —
without it a padded transcript could depress a session's totals silently.

``service_tier_counts`` / ``stop_reason_counts`` are JSONB ``{key: count}`` maps
rather than columns: both value sets are provider-defined and grow (a new tier or
a new stop reason should not need a migration), and neither is ever aggregated in
SQL — the console sums them client-side.

Retention (90 days, always on — ``AMX_SESSION_USAGE_RETENTION_DAYS``): this table
is a **diagnostic** input, not a billing one. Nothing integrates over it — cost
allocation reads ``usage_daily_rollup`` from ``usage_snapshots`` — so the age purge
here needs none of the settlement-boundary guard that
``usage_cost.sweep_snapshot_retention`` must apply before deleting a snapshot.
That makes it a plain age delete, and it is on by default rather than opt-in (the
``audit_retention_days`` shape) because an always-appending diagnostic table with
no downstream consumer has nothing to protect and must not grow forever. The
window is the same 90 days as ``usage_snapshot_retention_days`` so an operator
reasons about one horizon for "raw usage detail", not two.

CREATE INDEX CONCURRENTLY is not used: alembic/env.py runs every migration inside
a transaction, where CONCURRENTLY is disallowed — same convention as 0017/0020/0021.

Revision ID: 0027_session_usage
Revises: 0026_credential_unusable_alert
Create Date: 2026-08-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID

revision = "0027_session_usage"
down_revision = "0026_credential_unusable_alert"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_usage",
        sa.Column("tenant_id", PgUUID(as_uuid=True), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        # Nullable, no FK — see the module docstring.
        sa.Column("account_id", PgUUID(as_uuid=True), nullable=True),
        # Token counters are BIGINT: a month of one runner's cache reads passes int32.
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "cache_read_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        # The point of this table: the two cache-write classes stay separate because
        # they are priced separately.
        sa.Column(
            "cache_create_1h_tokens",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "cache_create_5m_tokens",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "thinking_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "web_search_requests", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "web_fetch_requests", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        # Assistant messages the aggregate was built from, deduplicated by the
        # provider's message id (the transcript repeats one API response once per
        # content block, so a raw line count would double-count).
        sa.Column("message_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        # 훅이 줄 수·바이트·iterations 상한에 걸려 일부를 버렸으면 True. 부분 집계도 값이
        # 있어 저장하되, 표시가 없으면 조작된 패딩으로 과소집계를 조용히 유도할 수 있다.
        sa.Column(
            "truncated", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "service_tier_counts", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "stop_reason_counts", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        # First/last assistant-record timestamp for this (session, model). Nullable:
        # a transcript record may carry no parsable timestamp.
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("tenant_id", "session_id", "model", name="pk_session_usage"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_session_usage_tenant",
            ondelete="CASCADE",
        ),
    )
    # The console reads a trailing window newest-first; the retention purge scans
    # the same column. One index serves both.
    op.create_index(
        "ix_session_usage_tenant_ended", "session_usage", ["tenant_id", "ended_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_session_usage_tenant_ended", table_name="session_usage")
    op.drop_table("session_usage")
