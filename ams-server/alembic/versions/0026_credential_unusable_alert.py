"""credential_unusable 경보 kind 추가 (§5.7 1차 사고 신호).

AMA resyncer의 토큰 재료 가드가 로그아웃 껍데기를 드롭할 때 그 사실이 로그 한 줄로만
남아, 운영자는 계정이 실제로 못 쓰게 된 뒤 ``quarantine`` 경보로만 뒤늦게 알게 된다.
드롭 시점에 에이전트가 올리는 ``KIND_CREDENTIAL_UNUSABLE`` 이벤트를 계정 범위 경보로
열려면 ``ck_alerts_kind`` 가 새 kind를 받아야 한다. 새 테이블·컬럼·command type 없이
CHECK 하나만 drop/recreate 한다(0019·0022·0023 관례).

Revision ID: 0026_credential_unusable_alert
Revises: 0025_account_assignment_excluded
Create Date: 2026-08-18
"""

from __future__ import annotations

from alembic import op

revision = "0026_credential_unusable_alert"
down_revision = "0025_account_assignment_excluded"
branch_labels = None
depends_on = None

# 0023이 세운 13종 + 이번 1종.
_KINDS_BEFORE = (
    "all_exhausted,drift,server_offline,quarantine,recall_failed,"
    "command_send_failed,self_update_failed,billing_watermark_future,"
    "langfuse_usage_spike,langfuse_stale,langfuse_latency,alert_webhook_dropped,"
    "dangerous_command"
)
_NEW_KIND = "credential_unusable"
_KINDS_AFTER = _KINDS_BEFORE + "," + _NEW_KIND


def _kind_check(kinds: str) -> str:
    quoted = ",".join(f"'{k}'" for k in kinds.split(","))
    return f"kind IN ({quoted})"


def upgrade() -> None:
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint("ck_alerts_kind", "alerts", _kind_check(_KINDS_AFTER))


def downgrade() -> None:
    # 좁아지는 CHECK를 위반할 신규 kind 행을 먼저 제거한다(0019/0022/0023 관례).
    op.execute(f"DELETE FROM alerts WHERE kind = '{_NEW_KIND}'")
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint("ck_alerts_kind", "alerts", _kind_check(_KINDS_BEFORE))
