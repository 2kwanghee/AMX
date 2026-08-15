"""Langfuse 실측 임계값 경보 스윕 — services.langfuse_alerts (P5 / BACKLOG G41).

Langfuse 모니터링 데이터(로컬 ``langfuse_usage_rollup`` + Metrics API)를 실측 기준으로
평가해 경보 3종을 open/resolve 한다. F5/Langfuse 스윕의 형제로, 자체 advisory 락(…09)과
Langfuse **활성 게이트(langfuse_enabled)** 및 폴 주기(``langfuse_poll_seconds``)를 공유하되
process-local 캐던스 상태는 독립적이다:

* ``langfuse_usage_spike`` — 당일(UTC) 총 토큰이 전일 대비 ``alert_spike_factor`` 배수를
  초과하면 open. 전일이 0이면 배수가 무의미하므로 절대 하한 ``alert_spike_min_tokens``를
  초과할 때만 open. 복귀 시 resolve.
* ``langfuse_stale`` — 롤업 ``max(updated_at)``이 ``alert_stale_minutes``를 넘겨 정체되면
  open, 갱신 재개 시 resolve. 한 번도 동기화된 적 없으면(NULL) 평가하지 않는다(오발 방지).
* ``langfuse_latency`` — Metrics API latency p95(최근 1시간)가 ``alert_latency_p95_ms``를
  초과하면 open, 이하 복귀 시 resolve. HTTP 오류/무데이터는 경고 후 스킵(경보 오발 금지).

세 경보 모두 system 범위(server_id NULL·테넌트 범위 dedupe)이고, ``alerts`` 프리미티브
(``open_system_alert``/``resolve_system_alert``)를 통과하므로 웹훅 아웃박스에도 동일하게
스테이징된다. latency의 HTTP GET은 락·트랜잭션 밖에서 먼저 하고, 그 뒤 짧은 락+커밋으로
경보 전이를 반영한다(P4 교훈).
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import try_advisory_xact_lock as _try_advisory_xact_lock
from app.models import LangfuseUsageRollup
from app.services import alerts
from app.services.langfuse_metrics import (
    _METRICS_PATH,
    _auth_header,
    _iso,
    _query_metrics,
)

_logger = logging.getLogger("ams.langfuse")

# 형제 스윕 락 키 — langfuse-metrics(…07)·alert-webhook(…08)에 이은 아홉 번째.
# 트랜잭션 범위(경보 전이 커밋에서 해제).
_LANGFUSE_ALERT_SWEEP_LOCK_KEY = 0x414D580F09

# Langfuse latency measure는 초 단위이므로 ms 임계값과 비교하려면 1000을 곱한다.
_SECONDS_TO_MS = 1000.0

_USAGE_SPIKE_KIND = "langfuse_usage_spike"
_STALE_KIND = "langfuse_stale"
_LATENCY_KIND = "langfuse_latency"

# 캐던스 게이트(process-local): 게이트를 통과한 마지막 실행의 monotonic 시각.
# langfuse-metrics 스윕과 poll 주기 값만 공유하고 상태는 독립적이다.
_LAST_POLL_MONOTONIC: float | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _monotonic() -> float:
    return time.monotonic()


def _floor_day(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _fetch_latency_p95_ms(client: httpx.Client, settings, tenant_id: uuid.UUID) -> float | None:
    """최근 1시간 Metrics API latency p95(ms). HTTP 오류/무데이터/파싱 실패면 None.

    langfuse_metrics의 호출 관례(auth 헤더·Metrics 경로·query 직렬화)를 재사용한다.
    실패 로그에 시크릿/URL을 남기지 않도록 예외는 클래스명만 남긴다.
    """
    now = _now()
    query = {
        "view": "observations",
        "metrics": [{"measure": "latency", "aggregation": "p95"}],
        "fromTimestamp": _iso(now - timedelta(hours=1)),
        "toTimestamp": _iso(now),
    }
    try:
        data = _query_metrics(client, settings, query)
    except (httpx.HTTPError, ValueError) as exc:
        _logger.warning(
            "langfuse latency alert: metrics fetch error (%s); skipping latency this tick",
            type(exc).__name__,
        )
        return None
    if not data:
        return None
    value = data[0].get("p95_latency")
    if value is None:
        return None
    try:
        return float(value) * _SECONDS_TO_MS
    except (TypeError, ValueError):
        return None


def _eval_usage_spike(db: Session, settings, tenant_id: uuid.UUID) -> bool:
    """당일 총 토큰이 스파이크 조건을 만족하면 open, 아니면 resolve. open 여부 반환."""
    today = _floor_day(_now()).date()
    yesterday = today - timedelta(days=1)
    rows = dict(
        db.execute(
            select(
                LangfuseUsageRollup.day,
                func.coalesce(func.sum(LangfuseUsageRollup.total_tokens), 0),
            )
            .where(
                LangfuseUsageRollup.tenant_id == tenant_id,
                LangfuseUsageRollup.dimension == "model",
                LangfuseUsageRollup.day.in_([today, yesterday]),
            )
            .group_by(LangfuseUsageRollup.day)
        ).all()
    )
    today_total = int(rows.get(today, 0))
    prev_total = int(rows.get(yesterday, 0))

    if prev_total > 0:
        spike = today_total > settings.alert_spike_factor * prev_total
    else:
        spike = today_total > settings.alert_spike_min_tokens

    if spike:
        alerts.open_system_alert(
            db,
            tenant_id=tenant_id,
            kind=_USAGE_SPIKE_KIND,
            severity="warning",
            detail={
                "today_total_tokens": today_total,
                "prev_total_tokens": prev_total,
                "factor": settings.alert_spike_factor,
                "min_tokens": settings.alert_spike_min_tokens,
            },
        )
    else:
        alerts.resolve_system_alert(db, tenant_id=tenant_id, kind=_USAGE_SPIKE_KIND)
    return spike


def _eval_stale(db: Session, settings, tenant_id: uuid.UUID) -> bool | None:
    """롤업이 stale하면 open, 신선하면 resolve. 한 번도 동기화 안 됐으면 None(스킵)."""
    latest = db.scalar(
        select(func.max(LangfuseUsageRollup.updated_at)).where(
            LangfuseUsageRollup.tenant_id == tenant_id
        )
    )
    if latest is None:
        return None
    age = _now() - latest
    stale = age > timedelta(minutes=settings.alert_stale_minutes)
    if stale:
        alerts.open_system_alert(
            db,
            tenant_id=tenant_id,
            kind=_STALE_KIND,
            severity="warning",
            detail={
                "last_updated_at": latest.astimezone(UTC).isoformat(),
                "age_seconds": int(age.total_seconds()),
                "threshold_minutes": settings.alert_stale_minutes,
            },
        )
    else:
        alerts.resolve_system_alert(db, tenant_id=tenant_id, kind=_STALE_KIND)
    return stale


def _eval_latency(
    db: Session, settings, tenant_id: uuid.UUID, p95_ms: float | None
) -> bool | None:
    """latency p95가 임계 초과면 open, 이하면 resolve. p95_ms가 None이면 스킵."""
    if p95_ms is None:
        return None
    over = p95_ms > settings.alert_latency_p95_ms
    if over:
        alerts.open_system_alert(
            db,
            tenant_id=tenant_id,
            kind=_LATENCY_KIND,
            severity="warning",
            detail={
                "p95_ms": p95_ms,
                "threshold_ms": settings.alert_latency_p95_ms,
                "window": "1h",
            },
        )
    else:
        alerts.resolve_system_alert(db, tenant_id=tenant_id, kind=_LATENCY_KIND)
    return over


def sweep_langfuse_alerts(db: Session, *, client: httpx.Client | None = None) -> int:
    """Langfuse 임계값 경보 3종을 평가·전이한다. open 상태로 만든 경보 수를 반환한다.

    langfuse 비활성이면 no-op(0). 캐던스 게이트를 통과하면 latency HTTP GET을 락 밖에서
    먼저 하고, …09 락을 잡아 spike/stale/latency 전이를 한 트랜잭션으로 커밋한다(락 해제).
    """
    global _LAST_POLL_MONOTONIC

    settings = get_settings()
    if not settings.langfuse_enabled:
        return 0

    poll_seconds = max(60, settings.langfuse_poll_seconds)
    now_m = _monotonic()
    if _LAST_POLL_MONOTONIC is not None and now_m - _LAST_POLL_MONOTONIC < poll_seconds:
        return 0
    _LAST_POLL_MONOTONIC = now_m

    try:
        tenant_id = uuid.UUID(settings.langfuse_tenant_id)
    except (ValueError, TypeError):
        _logger.warning(
            "langfuse alert sweep: AMX_LANGFUSE_TENANT_ID is not a valid UUID; skipping"
        )
        return 0

    # Stage 1 — latency HTTP, 락·트랜잭션 밖.
    owns_client = client is None
    client = client or httpx.Client(timeout=settings.http_timeout_seconds)
    try:
        p95_ms = _fetch_latency_p95_ms(client, settings, tenant_id)
    finally:
        if owns_client:
            client.close()

    # Stage 2 — 짧은 락 + 경보 전이 + 커밋.
    if not _try_advisory_xact_lock(db, _LANGFUSE_ALERT_SWEEP_LOCK_KEY):
        return 0
    opened = 0
    if _eval_usage_spike(db, settings, tenant_id):
        opened += 1
    if _eval_stale(db, settings, tenant_id):
        opened += 1
    if _eval_latency(db, settings, tenant_id, p95_ms):
        opened += 1
    db.commit()
    return opened
