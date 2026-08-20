"""세션 비용구조 수신·조회 (session_usage_hook.py → session_usage 테이블).

Claude Code는 세션 트랜스크립트의 ``assistant`` 레코드마다 ``message.usage`` 전체를
적어 두는데, 그 안의 ``cache_creation.ephemeral_1h_input_tokens`` /
``ephemeral_5m_input_tokens``는 AMX의 다른 어떤 경로에도 없다. 두 캐시 쓰기는 가격이
다르고 Langfuse Metrics API는 둘을 합쳐서 보고하므로(usageByType), Langfuse를 경유하면
구분이 복구 불가능하다. 그래서 Stop 훅이 로컬에서 집계해 AMS로 직접 보낸다.

특성:
* **멱등 upsert**. 키는 ``(tenant_id, session_id, model)``이고 같은 세션의 재보고는
  누적이 아니라 **교체**다(``langfuse_metrics._upsert``의 recompute-replace 관례).
  훅은 매 Stop마다 트랜스크립트 전체를 다시 집계하므로, 누적하면 이중 계산이 된다.
* **계정 귀속은 실패해도 거부하지 않는다**. 페이로드의 이메일을 해당 테넌트에서
  찾아 ``account_id``를 채우고, 못 찾으면 NULL로 둔다.
* **원문을 저장하지 않는다**. 스키마에 원문을 담을 필드 자체가 없다(schemas.py).

레이트 제한은 danger 수신과 같은 **전역 고정창**(분당 상한)이다 — 무인 호출이라 테넌트
축이 없고, 프로세스 로컬 상태이므로 다중 인스턴스에서는 인스턴스당 상한이다.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app import schemas
from app.config import get_settings
from app.db import try_advisory_xact_lock as _try_advisory_xact_lock
from app.models import Account, SessionUsage

_logger = logging.getLogger(__name__)

# 보존 스윕 전용 advisory lock — audit 보존(…0B) 다음 번호. 한 인스턴스가 이번 틱의
# purge를 소유해도 다른 인스턴스의 형제 스윕을 막지 않는다.
_SESSION_RETENTION_SWEEP_LOCK_KEY = 0x414D580F0C
# 한 문장당 삭제 행 수 — 단일 대량 DELETE로 락·행락 집합을 길게 붙잡지 않는다
# (snapshot/assignment/audit 보존 스윕과 같은 관례).
_SESSION_RETENTION_BATCH = 5000

# 전역 고정창 레이트 리미터 상태(프로세스 로컬).
_rl_lock = threading.Lock()
_rl_window_start: float = 0.0
_rl_count: int = 0


def _now() -> datetime:
    return datetime.now(UTC)


def _monotonic() -> float:
    return time.monotonic()


def reset_rate_limit() -> None:
    """테스트용 — 고정창 카운터를 비운다."""
    global _rl_window_start, _rl_count
    with _rl_lock:
        _rl_window_start = 0.0
        _rl_count = 0


def allow_request(limit_per_min: int) -> bool:
    """이번 호출이 분당 상한 안이면 True. ``limit_per_min <= 0`` 이면 제한 없음."""
    if limit_per_min <= 0:
        return True
    global _rl_window_start, _rl_count
    now = _monotonic()
    with _rl_lock:
        if now - _rl_window_start >= 60.0:
            _rl_window_start = now
            _rl_count = 0
        if _rl_count >= limit_per_min:
            return False
        _rl_count += 1
        return True


def resolve_account_id(db: Session, tenant_id: uuid.UUID, email: str | None) -> uuid.UUID | None:
    """이메일을 해당 테넌트의 계정 id로 해석한다. 못 찾으면 None(거부하지 않는다).

    ``accounts``의 유일성은 ``(tenant_id, provider, email)``이라 한 이메일이 provider별로
    여러 행일 수 있다. 이 경로는 Claude Code 세션 훅 발이므로 ``provider="claude"``로
    좁힌다 — codex 행에 Claude 세션의 토큰을 매다는 오귀속을 막는다.
    """
    if not email:
        return None
    return db.scalar(
        select(Account.id).where(
            Account.tenant_id == tenant_id,
            Account.provider == "claude",
            Account.email == email,
        )
    )


def record_session_usage(
    db: Session, tenant_id: uuid.UUID, payload: schemas.SessionUsageIngest
) -> tuple[int, bool]:
    """페이로드의 모델별 집계를 멱등 upsert 한다. ``(행 수, 계정 해석 성공)``. caller가 커밋한다.

    같은 ``(tenant, session, model)``의 재보고는 값을 **교체**한다. 훅이 매번 트랜스크립트
    전체를 다시 집계해 보내기 때문이며, 누적하면 재보고마다 이중 계산이 된다.

    한 페이로드에 같은 모델이 두 번 오면(훅은 그렇게 만들지 않지만 방어) 뒤 항목이
    앞 항목을 덮는다 — 하나의 INSERT 문 안에서 같은 키가 두 번 나오면 Postgres가
    ``ON CONFLICT``로 자기 자신을 갱신할 수 없어 실패하므로, 문장에 넣기 전에 접는다.
    """
    account_id = resolve_account_id(db, tenant_id, payload.account_email)
    now = _now()

    by_model: dict[str, dict] = {}
    for stat in payload.models:
        by_model[stat.model] = {
            "tenant_id": tenant_id,
            "session_id": payload.session_id,
            "model": stat.model,
            "account_id": account_id,
            "input_tokens": stat.input_tokens,
            "output_tokens": stat.output_tokens,
            "cache_read_tokens": stat.cache_read_tokens,
            "cache_create_1h_tokens": stat.cache_create_1h_tokens,
            "cache_create_5m_tokens": stat.cache_create_5m_tokens,
            "thinking_tokens": stat.thinking_tokens,
            "web_search_requests": stat.web_search_requests,
            "web_fetch_requests": stat.web_fetch_requests,
            "message_count": stat.message_count,
            # 세션 단위 사실을 그 세션의 모든 모델 행에 복사한다.
            "truncated": payload.truncated,
            "service_tier_counts": stat.service_tier_counts,
            "stop_reason_counts": stat.stop_reason_counts,
            "started_at": stat.started_at,
            "ended_at": stat.ended_at,
            "updated_at": now,
        }
    values = list(by_model.values())
    if not values:
        return 0, account_id is not None

    stmt = pg_insert(SessionUsage).values(values)
    db.execute(
        stmt.on_conflict_do_update(
            constraint="pk_session_usage",
            set_={
                "account_id": stmt.excluded.account_id,
                "input_tokens": stmt.excluded.input_tokens,
                "output_tokens": stmt.excluded.output_tokens,
                "cache_read_tokens": stmt.excluded.cache_read_tokens,
                "cache_create_1h_tokens": stmt.excluded.cache_create_1h_tokens,
                "cache_create_5m_tokens": stmt.excluded.cache_create_5m_tokens,
                "thinking_tokens": stmt.excluded.thinking_tokens,
                "web_search_requests": stmt.excluded.web_search_requests,
                "web_fetch_requests": stmt.excluded.web_fetch_requests,
                "message_count": stmt.excluded.message_count,
                "truncated": stmt.excluded.truncated,
                "service_tier_counts": stmt.excluded.service_tier_counts,
                "stop_reason_counts": stmt.excluded.stop_reason_counts,
                "started_at": stmt.excluded.started_at,
                "ended_at": stmt.excluded.ended_at,
                "updated_at": stmt.excluded.updated_at,
            },
        )
    )
    return len(values), account_id is not None


def read_session_usage(
    db: Session, tenant_id: uuid.UUID, *, since: datetime, limit: int
) -> list[tuple[SessionUsage, str | None]]:
    """``ended_at >= since``인 한 테넌트의 행을 최근순으로. 계정 이메일을 함께 준다.

    ``ended_at``이 NULL인 행(타임스탬프를 못 읽은 트랜스크립트)은 창 비교가 불가능하므로
    빠진다 — 창 질의의 대상이 아니라는 뜻이고, 행 자체는 테이블에 남는다.

    계정 이메일은 ``accounts``와의 **outer** 조인이다: ``account_id``에 FK가 없어 계정이
    지워진 뒤에도 행이 남고, 애초에 NULL일 수 있다.
    """
    rows = db.execute(
        select(SessionUsage, Account.email)
        .outerjoin(Account, Account.id == SessionUsage.account_id)
        .where(SessionUsage.tenant_id == tenant_id, SessionUsage.ended_at >= since)
        .order_by(SessionUsage.ended_at.desc(), SessionUsage.session_id, SessionUsage.model)
        .limit(limit)
    ).all()
    return [(r[0], r[1]) for r in rows]


def last_reported_at(db: Session, tenant_id: uuid.UUID) -> datetime | None:
    """이 테넌트에서 가장 최근에 보고된 시각. 한 번도 없으면 None."""
    return db.scalar(
        select(func.max(SessionUsage.updated_at)).where(SessionUsage.tenant_id == tenant_id)
    )


def _pk_in(keys: list) -> ColumnElement[bool]:
    """복합 PK 목록을 ``(tenant_id, session_id, model) IN ((..),(..))`` 조건으로 만든다.

    단일 컬럼 PK가 아니라서 ``id.in_(ids)``를 쓸 수 없다. Postgres의 행 값 비교를 그대로
    쓴다 — PK 인덱스를 타므로 배치 삭제가 인덱스 스캔으로 끝난다.
    """
    return tuple_(SessionUsage.tenant_id, SessionUsage.session_id, SessionUsage.model).in_(
        [(k[0], k[1], k[2]) for k in keys]
    )


def sweep_session_usage_retention(db: Session) -> int:
    """보존 창을 넘긴 ``session_usage`` 행을 purge 한다. 삭제 행 수 반환.

    ``usage_snapshots``와 달리 정산 경계 가드가 없다 — 이 테이블 위에서 도는 적분이
    없고(비용 배분은 ``usage_daily_rollup``을 읽는다) 진단 목적이라, 나이만으로 지운다.
    기준 컬럼은 ``updated_at``이다: ``ended_at``은 NULL일 수 있어 그 행이 영구히 남게 된다.

    배치마다 자기 트랜잭션에서 트랜잭션 범위 advisory lock을 다시 잡는다(앞 배치의
    커밋이 락을 놓기 때문). 재획득 실패는 다른 인스턴스가 이번 틱의 purge를 가져갔다는
    뜻이므로 남은 배치를 양보한다. ``session_usage_retention_days <= 0`` 이면 0.
    """
    days = get_settings().session_usage_retention_days
    if days <= 0:
        return 0
    delete_before = _now() - timedelta(days=days)

    total = 0
    while True:
        if not _try_advisory_xact_lock(db, _SESSION_RETENTION_SWEEP_LOCK_KEY):
            break
        keys = db.execute(
            select(SessionUsage.tenant_id, SessionUsage.session_id, SessionUsage.model)
            .where(SessionUsage.updated_at < delete_before)
            .limit(_SESSION_RETENTION_BATCH)
        ).all()
        if not keys:
            db.rollback()  # 락 반납; 지울 게 없다.
            break
        db.execute(delete(SessionUsage).where(_pk_in(keys)))
        db.commit()  # 다음 배치가 다시 잡을 때까지 advisory lock 반납.
        total += len(keys)
        if len(keys) < _SESSION_RETENTION_BATCH:
            break
    if total:
        _logger.info(
            "session usage retention purged %d row(s) older than %s",
            total,
            delete_before.isoformat(),
        )
    return total
