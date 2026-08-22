"""servers.owner — 시트 엔진 P1 정책 축.

accounts.owner(0016)와 같은 모양의 nullable 자유 텍스트 라벨이다. FK 가 아닌
이유도 같다 — 소유자는 로그인이 없는 사람·팀일 때가 많다. 여기서는 감사·표시용을
넘어 판정에 쓰인다: rotation_scope=owner(기본값) 정책에서 서버의 owner 가 후보
계정 필터의 입력이 된다(app/services/pool.py _candidates, §5 결정 1). 기존
서버는 전부 NULL 이라 전환 당일 동작은 바뀌지 않는다.

Revision ID: 0034_server_owner
Revises: 0033_pool_usage_stale_alert
Create Date: 2026-08-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0034_server_owner"
down_revision = "0033_pool_usage_stale_alert"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("owner", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "owner")
