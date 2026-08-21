"""account pool (P0+P1) — 계정 창 정규화, 풀 상태, 정책, 권고·체인·이벤트.

docs/design-notes/account-pool-automation-plan.md 의 P0(정규화)·P1(관측만)을 담는다.
지금까지 5시간/7일 창의 ``pct``·``resets_at`` 은 ``usage_snapshots.payload`` JSONB
안에만 있었고 ``resets_at`` 은 아무도 읽지 않았다. 계정 단위로 그 값을 읽으려면
JSONB 전체를 훑어야 하는데, 30초 스윕이 그걸 할 수는 없다. 그래서
``account_usage_windows`` 는 **최신값만** 담는 upsert 테이블이다 — 이력은 기존
JSONB 원장이 그대로 갖고 있으므로 여기서 다시 쌓을 이유가 없고, PK
``(tenant_id, account_id, window_id)`` 가 그 "최신값 한 줄" 규약 자체다.

``accounts.pool_state`` 는 기존 ``accounts.status`` 와 별개다. status 는 재고
상태(available/assigned/disabled/quarantined)이고 pool_state 는 배급 순환의
위치다. 둘을 한 컬럼에 욱여넣으면 "quarantined 이면서 충전 중" 같은 실재하는
조합이 표현되지 않는다.

``servers.pool_policy`` 와 ``tenants.pool_automation_paused`` 는 JSONB/bool 기본값을
갖는 NOT NULL 이라 기존 행이 마이그레이션만으로 전부 유효해진다 — 빈 정책 ``{}`` 은
"mode=manual + 나머지 기본값"으로 읽히므로(services/pool.py ``resolve_policy``)
아무 서버의 동작도 바뀌지 않는다. P1 은 관측만 하고 명령을 내지 않는다.

``pool_chains`` 와 ``tenants.pool_automation_paused`` 는 P2/P3 것이지만 지금 함께
만든다. 나중에 컬럼 하나 때문에 마이그레이션을 또 돌리는 것보다, 비어 있는 테이블을
미리 두는 편이 배포 횟수를 줄인다.

CREATE INDEX CONCURRENTLY 는 쓰지 않는다: alembic/env.py 가 모든 마이그레이션을
트랜잭션 안에서 돌리므로 CONCURRENTLY 가 금지된다(0017/0020/0021/0027 관례).

Revision ID: 0028_account_pool
Revises: 0027_session_usage
Create Date: 2026-08-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID

revision = "0028_account_pool"
down_revision = "0027_session_usage"
branch_labels = None
depends_on = None

_POOL_STATES = ("ready", "leased", "recalling", "cooling", "pinned", "held")
_REC_KINDS = ("prefetch", "swap", "recall_idle", "lease")
_CHAIN_STEPS = ("deliver", "switch", "recall", "done", "failed")

# 0026 이 남긴 경보 kind 로스터에 계정 창 고사용 경고를 더한다.
_KINDS_BEFORE = (
    "all_exhausted,drift,server_offline,quarantine,recall_failed,command_send_failed,"
    "self_update_failed,billing_watermark_future,langfuse_usage_spike,langfuse_stale,"
    "langfuse_latency,alert_webhook_dropped,dangerous_command,credential_unusable"
)
_NEW_KIND = "account_window_high"
_KINDS_AFTER = _KINDS_BEFORE + "," + _NEW_KIND


def _kind_check(kinds: str) -> str:
    quoted = ",".join(f"'{k}'" for k in kinds.split(","))
    return f"kind IN ({quoted})"


def _in_check(column: str, values: tuple[str, ...]) -> str:
    quoted = ",".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    # -- P0: 계정 창 정규화 ---------------------------------------------------
    op.create_table(
        "account_usage_windows",
        sa.Column("tenant_id", PgUUID(as_uuid=True), nullable=False),
        sa.Column("account_id", PgUUID(as_uuid=True), nullable=False),
        # tsamx 의 provider-local key ("five_hour" / "seven_day" / …).
        sa.Column("window_id", sa.Text(), nullable=False),
        sa.Column("pct", sa.Float(), nullable=False, server_default=sa.text("0")),
        # 창이 언제 풀리는지. NULL 이면 공급자가 리셋 시각을 주지 않은 것이고,
        # 그런 창은 쿨다운 만료를 계산할 수 없으므로 cooling 판정에서 제외된다.
        sa.Column("resets_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_minutes", sa.Integer(), nullable=True),
        # tsamx 캐시 항목의 신선도(에이전트 관측 시각). 보고 시각과 다르다.
        sa.Column("usage_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        # 이 관측을 올린 서버. 계정이 다른 서버로 옮겨가면 갱신된다.
        sa.Column("server_id", PgUUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "account_id", "window_id", name="pk_account_usage_windows"
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "tenant_id"],
            ["accounts.id", "accounts.tenant_id"],
            name="fk_account_usage_windows_account_tenant",
            ondelete="CASCADE",
        ),
    )
    # 쿨다운 만료 스캔(resets_at <= now)이 이 인덱스를 탄다.
    op.create_index(
        "ix_account_usage_windows_resets_at", "account_usage_windows", ["resets_at"]
    )

    # -- P1: 계정 풀 상태 -----------------------------------------------------
    op.add_column(
        "accounts",
        sa.Column(
            "pool_state", sa.String(16), nullable=False, server_default=sa.text("'ready'")
        ),
    )
    op.add_column(
        "accounts", sa.Column("cooling_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("accounts", sa.Column("cooling_window_id", sa.Text(), nullable=True))
    op.add_column(
        "accounts",
        sa.Column("pool_state_changed_at", sa.DateTime(timezone=True), nullable=True),
    )
    # 마지막 대여가 끝난 시각 — 후보 정렬의 3순위(공평 순환) 키.
    op.add_column(
        "accounts",
        sa.Column("last_lease_ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_accounts_pool_state", "accounts", _in_check("pool_state", _POOL_STATES)
    )

    # -- P1: 서버 슬롯 정책 / 테넌트 자동화 스위치 ----------------------------
    op.add_column(
        "servers",
        sa.Column(
            "pool_policy", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "pool_automation_paused",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # -- P1: 권고 ------------------------------------------------------------
    op.create_table(
        "pool_recommendations",
        sa.Column("id", PgUUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", PgUUID(as_uuid=True), nullable=False),
        sa.Column("server_id", PgUUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("from_account_id", PgUUID(as_uuid=True), nullable=True),
        sa.Column("to_account_id", PgUUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("trigger_pct", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["server_id", "tenant_id"],
            ["servers.id", "servers.tenant_id"],
            name="fk_pool_recommendations_server_tenant",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(_in_check("kind", _REC_KINDS), name="ck_pool_recommendations_kind"),
    )
    # 같은 (server, kind, from, to) 권고는 하나뿐이다. PostgreSQL 에서 NULL 은 유니크
    # 비교상 서로 다르므로, 계정이 없는 쪽(lease 의 from, recall_idle 의 to)을 0-UUID 로
    # 접어 넣은 식 인덱스로 만든다 — 그렇지 않으면 from 이 NULL 인 권고가 매 틱마다
    # 새로 쌓인다.
    op.create_index(
        "uq_pool_recommendations_dedupe",
        "pool_recommendations",
        [
            "server_id",
            "kind",
            sa.text("COALESCE(from_account_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            sa.text("COALESCE(to_account_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
        ],
        unique=True,
    )
    op.create_index(
        "ix_pool_recommendations_tenant", "pool_recommendations", ["tenant_id", "created_at"]
    )

    # -- P2 자리: 체인 (이번 단계에서는 쓰지 않는다) --------------------------
    op.create_table(
        "pool_chains",
        sa.Column("id", PgUUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", PgUUID(as_uuid=True), nullable=False),
        sa.Column("server_id", PgUUID(as_uuid=True), nullable=False),
        # 권고 행은 조건이 해소되면 지워지므로 FK 를 걸지 않는다 — 체인은 자신이
        # 어느 권고에서 출발했는지를 기록으로만 갖는다.
        sa.Column("recommendation_id", PgUUID(as_uuid=True), nullable=True),
        sa.Column("from_account_id", PgUUID(as_uuid=True), nullable=True),
        sa.Column("to_account_id", PgUUID(as_uuid=True), nullable=True),
        sa.Column("step", sa.String(16), nullable=False, server_default=sa.text("'deliver'")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["server_id", "tenant_id"],
            ["servers.id", "servers.tenant_id"],
            name="fk_pool_chains_server_tenant",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(_in_check("step", _CHAIN_STEPS), name="ck_pool_chains_step"),
    )
    op.create_index("ix_pool_chains_tenant_step", "pool_chains", ["tenant_id", "step"])

    # -- P1: 이벤트(자동 변경 전용 감사) --------------------------------------
    # REST 감사 미들웨어는 스윕을 타지 않는다. 컨트롤러의 자동 변경은 여기에만
    # 남으므로 P1 부터 함께 넣는다(기획서 §4.5).
    op.create_table(
        "pool_events",
        sa.Column("id", PgUUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", PgUUID(as_uuid=True), nullable=False),
        sa.Column("account_id", PgUUID(as_uuid=True), nullable=True),
        sa.Column("server_id", PgUUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("detail", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        # 'pool-controller'(스윕) 또는 관리자 이메일.
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_pool_events_tenant", ondelete="CASCADE"
        ),
    )
    op.create_index("ix_pool_events_tenant_created", "pool_events", ["tenant_id", "created_at"])

    # -- 경보 kind 확장 -------------------------------------------------------
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint("ck_alerts_kind", "alerts", _kind_check(_KINDS_AFTER))


def downgrade() -> None:
    # 좁아지는 CHECK 를 위반할 신규 kind 행을 먼저 지운다(0019/0022/0023/0026 관례).
    op.execute(f"DELETE FROM alerts WHERE kind = '{_NEW_KIND}'")
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint("ck_alerts_kind", "alerts", _kind_check(_KINDS_BEFORE))

    op.drop_index("ix_pool_events_tenant_created", table_name="pool_events")
    op.drop_table("pool_events")
    op.drop_index("ix_pool_chains_tenant_step", table_name="pool_chains")
    op.drop_table("pool_chains")
    op.drop_index("ix_pool_recommendations_tenant", table_name="pool_recommendations")
    op.drop_index("uq_pool_recommendations_dedupe", table_name="pool_recommendations")
    op.drop_table("pool_recommendations")

    op.drop_column("tenants", "pool_automation_paused")
    op.drop_column("servers", "pool_policy")

    op.drop_constraint("ck_accounts_pool_state", "accounts", type_="check")
    op.drop_column("accounts", "last_lease_ended_at")
    op.drop_column("accounts", "pool_state_changed_at")
    op.drop_column("accounts", "cooling_window_id")
    op.drop_column("accounts", "cooling_until")
    op.drop_column("accounts", "pool_state")

    op.drop_index("ix_account_usage_windows_resets_at", table_name="account_usage_windows")
    op.drop_table("account_usage_windows")
