"""alert_webhook_outbox + Langfuse 임계값 경보 kind 3종 (BACKLOG G41 / P5).

두 가지를 한 리비전에 담는다:

* ``alert_webhook_outbox`` 신규 테이블 — 경보 open/resolve 전이의 발송 대기 큐.
  ``services.alerts``가 경보를 여닫는 것과 같은 트랜잭션으로 행을 스테이징하고,
  형제 스윕(``services.alert_webhook``, advisory 락 …08)이 HTTP POST로 드레인한다.
  ``next_attempt_at`` 부분 없는 단일 인덱스로 발송 대상(만기 행)을 싸게 고른다.

* ``ck_alerts_kind`` 확장 — Langfuse 실측 임계값 경보 3종
  (``langfuse_usage_spike`` / ``langfuse_stale`` / ``langfuse_latency``)과 웹훅 폐기
  셀프 경보 ``alert_webhook_dropped``을 허용한다. 모두 system 범위(server_id NULL·
  테넌트 범위 dedupe)이고 새 command type를 만들지 않아, 0019와 마찬가지로
  ``ck_alerts_kind`` 하나만 drop/recreate 한다.

Revision ID: 0022_alert_webhook
Revises: 0021_langfuse_usage_rollup
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID

revision = "0022_alert_webhook"
down_revision = "0021_langfuse_usage_rollup"
branch_labels = None
depends_on = None

# 0019가 세운 8종 + P5 3종. downgrade 시 좁아지는 CHECK를 위반할 P5 행은 먼저 지운다.
_KINDS_BEFORE = (
    "all_exhausted,drift,server_offline,quarantine,recall_failed,"
    "command_send_failed,self_update_failed,billing_watermark_future"
)
_NEW_KINDS = (
    "langfuse_usage_spike",
    "langfuse_stale",
    "langfuse_latency",
    "alert_webhook_dropped",
)
_KINDS_AFTER = _KINDS_BEFORE + "," + ",".join(_NEW_KINDS)


def _kind_check(kinds: str) -> str:
    quoted = ",".join(f"'{k}'" for k in kinds.split(","))
    return f"kind IN ({quoted})"


def upgrade() -> None:
    op.create_table(
        "alert_webhook_outbox",
        sa.Column("id", PgUUID(as_uuid=True), primary_key=True),
        sa.Column("alert_id", PgUUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", PgUUID(as_uuid=True), nullable=False),
        sa.Column("server_id", PgUUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("detail", JSONB(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", PgUUID(as_uuid=True), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_alert_webhook_outbox_due", "alert_webhook_outbox", ["next_attempt_at"]
    )

    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint("ck_alerts_kind", "alerts", _kind_check(_KINDS_AFTER))


def downgrade() -> None:
    # 좁아지는 CHECK를 위반할 신규 kind 행을 먼저 제거한다(0019 관례).
    quoted = ",".join(f"'{k}'" for k in _NEW_KINDS)
    op.execute(f"DELETE FROM alerts WHERE kind IN ({quoted})")
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint("ck_alerts_kind", "alerts", _kind_check(_KINDS_BEFORE))

    op.drop_index("ix_alert_webhook_outbox_due", table_name="alert_webhook_outbox")
    op.drop_table("alert_webhook_outbox")
