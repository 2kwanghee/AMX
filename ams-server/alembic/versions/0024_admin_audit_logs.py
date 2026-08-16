"""admin_audit_logs — 변경성 관리 REST 감사 트레일 (콘솔 테스트 갭 G53).

모든 POST/PATCH/DELETE 관리 호출을 응답 후 한 행으로 남기는 append-only 테이블.
``tenant_id`` 는 nullable 이고 **FK가 없다**: 전역 액션(테넌트 없음)을 담아야 하고,
테넌트를 지워도 감사 이력은 CASCADE로 함께 사라지면 안 되기 때문이다(``models.py``
``AdminAuditLog`` 참조). 요청 바디는 저장하지 않는다(자격증명·시크릿 유출 방지, §7).

Revision ID: 0024_admin_audit_logs
Revises: 0023_dangerous_command_alert
Create Date: 2026-08-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PgUUID

revision = "0024_admin_audit_logs"
down_revision = "0023_dangerous_command_alert"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_audit_logs",
        sa.Column("id", PgUUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", PgUUID(as_uuid=True), nullable=True),
        sa.Column("admin_email", sa.Text(), nullable=True),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_id", PgUUID(as_uuid=True), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_admin_audit_logs_tenant_created",
        "admin_audit_logs",
        ["tenant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_admin_audit_logs_tenant_created", table_name="admin_audit_logs")
    op.drop_table("admin_audit_logs")
