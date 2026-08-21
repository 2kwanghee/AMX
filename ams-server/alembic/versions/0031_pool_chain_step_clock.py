"""체인 단계 시계를 감사 타임스탬프에서 분리하고, 서버당 1체인을 DB로 강제한다.

``updated_at`` 하나가 두 가지 일을 하고 있었다. "이 행이 마지막으로 바뀐 때"와
"지금 단계가 시작된 때"다. 그 둘이 갈라지는 순간이 실제로 있다 — 같은 단계 안에서
전달 명령을 다시 내는 경우다. 명령 outbox 는 ack 없는 ``sent`` 를 최대 5회
재큐잉하다가 실패로 접고 배정을 ``pending`` 으로 되돌리는데, 컨트롤러는 그
``pending`` 을 보고 전달을 다시 낸다. 그때마다 시계를 되감으면 그 단계는 제한
시간에 영영 닿지 못하고 재발행만 반복한다.

그래서 ``step_started_at`` 을 따로 둔다. 단계가 실제로 바뀔 때만 움직이므로 단계
타임아웃의 기준이 되고, ``updated_at`` 은 감사용으로 자유롭게 움직인다.

부분 유니크 인덱스는 "서버당 활성 체인 하나"를 코드가 아니라 스키마가 보장하게
한다. 그 규칙을 지금까지는 조회 후 판단으로만 지켰는데, 조회와 삽입 사이에 다른
인스턴스(또는 같은 인스턴스의 다른 요청)가 끼어들면 두 체인이 같은 서버의 같은
배정을 반대 방향으로 밀 수 있다. advisory lock 은 협조적 장치이고 이 인덱스는
그렇지 않다.

Revision ID: 0031_pool_chain_step_clock
Revises: 0030_pool_window_pct_unknown
Create Date: 2026-08-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031_pool_chain_step_clock"
down_revision = "0030_pool_window_pct_unknown"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pool_chains",
        sa.Column(
            "step_started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # 기존 행(있다면)의 단계 시작 시각은 마지막 변경 시각이 가장 가까운 근사다.
    op.execute("UPDATE pool_chains SET step_started_at = updated_at")
    op.create_index(
        "uq_pool_chains_active_server",
        "pool_chains",
        ["server_id"],
        unique=True,
        postgresql_where=sa.text("step NOT IN ('done','failed')"),
    )


def downgrade() -> None:
    op.drop_index("uq_pool_chains_active_server", table_name="pool_chains")
    op.drop_column("pool_chains", "step_started_at")
