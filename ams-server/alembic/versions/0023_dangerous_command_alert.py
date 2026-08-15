"""dangerous_command 경보 kind 추가 (P5 경로 d).

Claude Code PreToolUse 위험명령 훅(``deploy/langfuse/danger_hook.py``)의 통보를
받는 ``services.danger_alerts`` 가 여는 ``dangerous_command`` 경보 kind를
``ck_alerts_kind`` 에 추가한다. system 범위(server_id NULL·명시 dedupe 키)이고 새
command type를 만들지 않으므로 0022와 마찬가지로 CHECK 하나만 drop/recreate 한다.

Revision ID: 0023_dangerous_command_alert
Revises: 0022_alert_webhook
Create Date: 2026-08-15
"""

from __future__ import annotations

from alembic import op

revision = "0023_dangerous_command_alert"
down_revision = "0022_alert_webhook"
branch_labels = None
depends_on = None

# 0022가 세운 12종 + 이번 1종.
_KINDS_BEFORE = (
    "all_exhausted,drift,server_offline,quarantine,recall_failed,"
    "command_send_failed,self_update_failed,billing_watermark_future,"
    "langfuse_usage_spike,langfuse_stale,langfuse_latency,alert_webhook_dropped"
)
_NEW_KIND = "dangerous_command"
_KINDS_AFTER = _KINDS_BEFORE + "," + _NEW_KIND


def _kind_check(kinds: str) -> str:
    quoted = ",".join(f"'{k}'" for k in kinds.split(","))
    return f"kind IN ({quoted})"


def upgrade() -> None:
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint("ck_alerts_kind", "alerts", _kind_check(_KINDS_AFTER))


def downgrade() -> None:
    # 좁아지는 CHECK를 위반할 신규 kind 행을 먼저 제거한다(0019/0022 관례).
    op.execute(f"DELETE FROM alerts WHERE kind = '{_NEW_KIND}'")
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint("ck_alerts_kind", "alerts", _kind_check(_KINDS_BEFORE))
