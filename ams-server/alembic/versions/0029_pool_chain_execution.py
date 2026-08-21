"""계정 풀 P2+P3 — 체인 실행에 필요한 컬럼과 체인 실패 경보.

0028 이 만든 ``pool_chains`` 는 비어 있는 껍데기였다. 실행기를 붙이면서 세 가지가
모자란 것이 드러났다.

``kind`` — 체인의 종류를 ``from_account_id``/``to_account_id`` 조합으로 되돌릴 수
없다. prefetch(예열만)와 swap(전환 후 회수)은 둘 다 from·to 를 모두 채우지만
끝나는 지점이 다르다. 추론으로 메우면 "예열이었는데 이전 계정을 회수해 버렸다"가
되므로 종류를 명시적으로 적는다.

``command_id`` — deliver/recall 은 배정 state 가 진행을 말해 주지만 switch_now 는
비-state 명령이라 배정에 아무 흔적도 남기지 않는다(§6.3 markerless). 스윕이
"이미 전환을 걸었는가"를 물을 곳이 없으면 30초마다 같은 명령을 다시 낸다. 그래서
그 한 건의 command_id 를 체인이 들고 있는다.

``acked_at`` — 실패한 체인을 운영자가 확인했는지. 확인 전까지 그 서버의 자동
실행을 멈추는 빗장이라, 실패를 못 본 사이 컨트롤러가 같은 실패를 반복하는 일을
막는다.

``pool_chain_failed`` 는 서버 범위 경보다. 한 서버에 활성 체인은 하나뿐이므로
dedupe 를 서버로 잡아도 두 실패가 겹쳐 가려지지 않고, 어느 체인이었는지는
detail 의 ``chain_id`` 가 말한다.

Revision ID: 0029_pool_chain_execution
Revises: 0028_account_pool
Create Date: 2026-08-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029_pool_chain_execution"
down_revision = "0028_account_pool"
branch_labels = None
depends_on = None

_REC_KINDS = ("prefetch", "swap", "recall_idle", "lease")

_KINDS_BEFORE = (
    "all_exhausted,drift,server_offline,quarantine,recall_failed,command_send_failed,"
    "self_update_failed,billing_watermark_future,langfuse_usage_spike,langfuse_stale,"
    "langfuse_latency,alert_webhook_dropped,dangerous_command,credential_unusable,"
    "account_window_high"
)
_NEW_KIND = "pool_chain_failed"
_KINDS_AFTER = _KINDS_BEFORE + "," + _NEW_KIND


def _kind_check(kinds: str) -> str:
    quoted = ",".join(f"'{k}'" for k in kinds.split(","))
    return f"kind IN ({quoted})"


def upgrade() -> None:
    # 테이블이 비어 있으므로(P1 은 체인을 한 줄도 만들지 않았다) server_default 는
    # 기존 행을 메우기 위한 것이 아니라, 이 컬럼을 모르는 옛 코드가 INSERT 해도
    # NOT NULL 을 깨지 않게 하는 안전장치다.
    op.add_column(
        "pool_chains",
        sa.Column("kind", sa.String(16), nullable=False, server_default="swap"),
    )
    op.create_check_constraint(
        "ck_pool_chains_kind",
        "pool_chains",
        "kind IN (" + ",".join(f"'{k}'" for k in _REC_KINDS) + ")",
    )
    op.add_column("pool_chains", sa.Column("command_id", sa.Text(), nullable=True))
    op.add_column(
        "pool_chains",
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
    )
    # "이 서버에 지금 도는 체인이 있는가"가 매 스윕·매 apply 마다 물어보는 질문이다.
    op.create_index("ix_pool_chains_server_step", "pool_chains", ["server_id", "step"])

    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint("ck_alerts_kind", "alerts", _kind_check(_KINDS_AFTER))


def downgrade() -> None:
    op.execute(f"DELETE FROM alerts WHERE kind = '{_NEW_KIND}'")
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint("ck_alerts_kind", "alerts", _kind_check(_KINDS_BEFORE))

    op.drop_index("ix_pool_chains_server_step", table_name="pool_chains")
    op.drop_column("pool_chains", "acked_at")
    op.drop_column("pool_chains", "command_id")
    op.drop_constraint("ck_pool_chains_kind", "pool_chains", type_="check")
    op.drop_column("pool_chains", "kind")
