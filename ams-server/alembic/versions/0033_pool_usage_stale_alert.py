"""계정 풀 F1 — leased 계정의 관측 두절 경보(``pool_usage_stale``).

0029(``ck_alerts_kind``를 마지막으로 다시 쓴 마이그레이션)와 같은 방식 —
drop/recreate 만 하고 다른 컬럼은 안 건드린다. leased 계정의 창이 전부
stale/미상인데 그 창을 올렸어야 할 서버는 online일 때 여는 계정 범위 경보다
(신선한 관측이 돌아오면 자동 resolve, app/services/pool.py). modarra9 83%
동결 사고(2026-08-22) — 스왑 트리거가 관측 두절을 조용히 놓쳐 실제로는
100%까지 소진됐다.

Revision ID: 0033_pool_usage_stale_alert
Revises: 0032_session_usage_srv_project
Create Date: 2026-08-22
"""

from __future__ import annotations

from alembic import op

revision = "0033_pool_usage_stale_alert"
down_revision = "0032_session_usage_srv_project"
branch_labels = None
depends_on = None

_KINDS_BEFORE = (
    "all_exhausted,drift,server_offline,quarantine,recall_failed,command_send_failed,"
    "self_update_failed,billing_watermark_future,langfuse_usage_spike,langfuse_stale,"
    "langfuse_latency,alert_webhook_dropped,dangerous_command,credential_unusable,"
    "account_window_high,pool_chain_failed"
)
_NEW_KIND = "pool_usage_stale"
_KINDS_AFTER = _KINDS_BEFORE + "," + _NEW_KIND


def _kind_check(kinds: str) -> str:
    quoted = ",".join(f"'{k}'" for k in kinds.split(","))
    return f"kind IN ({quoted})"


def upgrade() -> None:
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint("ck_alerts_kind", "alerts", _kind_check(_KINDS_AFTER))


def downgrade() -> None:
    op.execute(f"DELETE FROM alerts WHERE kind = '{_NEW_KIND}'")
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint("ck_alerts_kind", "alerts", _kind_check(_KINDS_BEFORE))
