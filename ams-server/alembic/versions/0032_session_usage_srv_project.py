"""session_usage에 서버 축과 프로젝트 축을 추가한다.

훅(``deploy/langfuse/session_usage_hook.py``)은 처음부터 ``hostname``·``cwd``를
보내고 있었지만 받는 쪽 스키마에 대응 컬럼이 없어 버려지고 있었다(0026). 대시보드
통계(서버별 호출 횟수·최다 모델, 계정별 프로젝트)가 이 두 축을 요구하면서 이제야
저장할 자리가 필요해졌다.

``server_id``는 ``hostname``을 ``servers.hostname``과 대조해 해석한 결과다.
``alerts.account_id``와 같은 관례로 FK를 걸지 않는다 — 매칭 안 되는 hostname,
아직 등록 전인 서버, 삭제된 서버 모두 NULL로 남는 게 정상 상태이지 오류가 아니고,
세션 관측 자체는 서버 귀속과 무관하게 유효하기 때문이다. 인덱스는 서버별 집계
질의(대시보드 통계)를 위한 것이다.

``project``는 ``cwd``의 마지막 경로 요소만 남긴 것이다. 전체 경로를 그대로 저장하면
로컬 사용자명·드라이브 문자 같은 것이 텍스트로 새는데, 통계가 필요로 하는 건
프로젝트 이름뿐이다.

Revision ID: 0032_session_usage_srv_project
Revises: 0031_pool_chain_step_clock
Create Date: 2026-08-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0032_session_usage_srv_project"
down_revision = "0031_pool_chain_step_clock"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "session_usage",
        sa.Column("server_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "session_usage",
        sa.Column("project", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_session_usage_tenant_server",
        "session_usage",
        ["tenant_id", "server_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_session_usage_tenant_server", table_name="session_usage")
    op.drop_column("session_usage", "project")
    op.drop_column("session_usage", "server_id")
