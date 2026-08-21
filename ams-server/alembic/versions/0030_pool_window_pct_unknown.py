"""계정 창의 pct 를 "미상" 으로 남길 수 있게 한다.

0028 은 ``pct`` 를 NOT NULL DEFAULT 0 으로 잡았다. proto3 이 0.0 스칼라를
직렬화에서 빼기 때문에 **누락 = 0.0** 이라는 규약이 필요했고 거기까지는 맞다.
문제는 값이 있는데 숫자로 읽히지 않는 경우까지 같은 0.0 에 접힌다는 것이다.

0% 는 중립적인 값이 아니라 "이 계정은 여유가 가득하다"는 가장 강한 주장이다.
컨트롤러의 후보 정렬이 잔여량 오름차순이므로, 파싱 실패가 곧 그 계정을 **최우선
배급 대상**으로 밀어 올린다. 창을 읽지 못했다는 사실이 그 계정을 제일 먼저
내보내는 이유가 되어서는 안 된다.

NULL 을 허용하면 "모른다"를 모른다고 적을 수 있고, 상태 계산·트리거·후보 정렬이
모두 그 창을 판단에서 빼는 쪽으로 갈라진다. 기존 행은 전부 실제 관측값이므로
넓어지는 제약이고 되돌릴 때만 조심하면 된다(NULL 행을 0 으로 메운다).

Revision ID: 0030_pool_window_pct_unknown
Revises: 0029_pool_chain_execution
Create Date: 2026-08-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030_pool_window_pct_unknown"
down_revision = "0029_pool_chain_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "account_usage_windows",
        "pct",
        existing_type=sa.Double(),
        nullable=True,
        existing_server_default=sa.text("0"),
    )


def downgrade() -> None:
    # 좁아지는 제약을 위반할 행을 먼저 메운다. 0 으로 되돌리는 것은 정보 손실이지만
    # 이 컬럼의 옛 계약이 정확히 그것이었다.
    op.execute("UPDATE account_usage_windows SET pct = 0 WHERE pct IS NULL")
    op.alter_column(
        "account_usage_windows",
        "pct",
        existing_type=sa.Double(),
        nullable=False,
        existing_server_default=sa.text("0"),
    )
