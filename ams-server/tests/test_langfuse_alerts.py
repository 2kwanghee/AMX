"""P5 Langfuse 실측 임계값 경보 — services.langfuse_alerts(BACKLOG G41).

httpx.MockTransport로 latency Metrics API를 핀하고, langfuse_usage_rollup에 직접
행을 심어 3종(usage_spike / stale / latency)의 open·resolve, 전일 0 하한, HTTP 오류
무경보를 검증한다.
"""

from __future__ import annotations

import itertools
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select

from app.config import get_settings as _real_get_settings
from app.db import get_sessionmaker
from app.models import Alert, LangfuseUsageRollup
from app.services import inventory, langfuse_alerts

_NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
_TODAY = _NOW.date()
_YESTERDAY = _TODAY - timedelta(days=1)

_BASE = "http://langfuse.test"
_PK = "pk-test"
_SK = "sk-secret-should-never-be-logged"


def _sm():
    return get_sessionmaker()()


def _seed_tenant() -> uuid.UUID:
    with _sm() as db:
        return inventory.create_tenant(db, "lfa-" + uuid.uuid4().hex[:8]).id


def _seed_rollup(tenant_id, day, total_tokens, *, updated_at=_NOW, key="claude-sonnet-5"):
    with _sm() as db:
        db.add(
            LangfuseUsageRollup(
                tenant_id=tenant_id, day=day, dimension="model", key=key,
                total_tokens=total_tokens, updated_at=updated_at,
            )
        )
        db.commit()


def _activate(monkeypatch, tenant_id, **overrides):
    settings = replace(
        _real_get_settings(),
        langfuse_base_url=_BASE, langfuse_public_key=_PK, langfuse_secret_key=_SK,
        langfuse_tenant_id=str(tenant_id),
        **overrides,
    )
    monkeypatch.setattr(langfuse_alerts, "get_settings", lambda: settings)
    monkeypatch.setattr(langfuse_alerts, "_now", lambda: _NOW)
    monkeypatch.setattr(langfuse_alerts, "_LAST_POLL_MONOTONIC", None, raising=False)
    ticks = itertools.count(0, 10**6)
    monkeypatch.setattr(langfuse_alerts, "_monotonic", lambda: next(ticks))
    return settings


def _latency_client(p95_seconds=1.0, *, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, json={"err": "boom"})
        return httpx.Response(200, json={"data": [{"p95_latency": p95_seconds}]})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _alert(tenant_id, kind, status="open"):
    with _sm() as db:
        return db.scalar(
            select(Alert).where(
                Alert.tenant_id == tenant_id, Alert.kind == kind, Alert.status == status
            )
        )


# -- usage_spike --------------------------------------------------------------
def test_usage_spike_open_and_resolve(app_env, monkeypatch):
    tenant_id = _seed_tenant()
    _activate(monkeypatch, tenant_id, alert_spike_factor=3.0)
    _seed_rollup(tenant_id, _YESTERDAY, 100)
    _seed_rollup(tenant_id, _TODAY, 400)  # 400 > 3.0 * 100 → 스파이크

    with _sm() as db:
        opened = langfuse_alerts.sweep_langfuse_alerts(db, client=_latency_client())
    assert opened == 1
    assert _alert(tenant_id, "langfuse_usage_spike") is not None

    # 오늘 토큰을 임계 아래로 낮추면 resolve.
    with _sm() as db:
        db.query(LangfuseUsageRollup).filter_by(day=_TODAY).update({"total_tokens": 200})
        db.commit()
    monkeypatch.setattr(langfuse_alerts, "_LAST_POLL_MONOTONIC", None, raising=False)
    with _sm() as db:
        langfuse_alerts.sweep_langfuse_alerts(db, client=_latency_client())
    assert _alert(tenant_id, "langfuse_usage_spike") is None
    assert _alert(tenant_id, "langfuse_usage_spike", status="resolved") is not None


def test_usage_spike_prev_zero_uses_min_floor(app_env, monkeypatch):
    tenant_id = _seed_tenant()
    _activate(monkeypatch, tenant_id, alert_spike_min_tokens=1000)
    # 전일 행 없음 → prev=0 → 배수 대신 절대 하한(1000) 적용.
    _seed_rollup(tenant_id, _TODAY, 500)  # 500 < 1000 → 미발생
    with _sm() as db:
        opened = langfuse_alerts.sweep_langfuse_alerts(db, client=_latency_client())
    assert opened == 0
    assert _alert(tenant_id, "langfuse_usage_spike") is None

    _seed_rollup(tenant_id, _TODAY, 900, key="another")  # 합계 1400 > 1000 → 발생
    monkeypatch.setattr(langfuse_alerts, "_LAST_POLL_MONOTONIC", None, raising=False)
    with _sm() as db:
        langfuse_alerts.sweep_langfuse_alerts(db, client=_latency_client())
    assert _alert(tenant_id, "langfuse_usage_spike") is not None


# -- stale --------------------------------------------------------------------
def test_stale_open_and_resolve(app_env, monkeypatch):
    tenant_id = _seed_tenant()
    _activate(monkeypatch, tenant_id, alert_stale_minutes=60)
    # 마지막 갱신이 120분 전 → stale. total은 작게 둬 스파이크는 안 나게.
    _seed_rollup(tenant_id, _TODAY, 10, updated_at=_NOW - timedelta(minutes=120))
    with _sm() as db:
        opened = langfuse_alerts.sweep_langfuse_alerts(db, client=_latency_client())
    assert opened == 1
    assert _alert(tenant_id, "langfuse_stale") is not None

    # 갱신 재개(신선) → resolve.
    with _sm() as db:
        db.query(LangfuseUsageRollup).filter_by(day=_TODAY).update({"updated_at": _NOW})
        db.commit()
    monkeypatch.setattr(langfuse_alerts, "_LAST_POLL_MONOTONIC", None, raising=False)
    with _sm() as db:
        langfuse_alerts.sweep_langfuse_alerts(db, client=_latency_client())
    assert _alert(tenant_id, "langfuse_stale") is None


def test_stale_never_synced_skips(app_env, monkeypatch):
    tenant_id = _seed_tenant()
    _activate(monkeypatch, tenant_id)
    # 롤업 행이 전혀 없음 → 평가 스킵, 경보 없음.
    with _sm() as db:
        opened = langfuse_alerts.sweep_langfuse_alerts(db, client=_latency_client())
    assert opened == 0
    assert _alert(tenant_id, "langfuse_stale") is None


# -- latency ------------------------------------------------------------------
def test_latency_open_and_resolve(app_env, monkeypatch):
    tenant_id = _seed_tenant()
    _activate(monkeypatch, tenant_id, alert_latency_p95_ms=60000.0)
    # 70s = 70000ms > 60000 → open.
    with _sm() as db:
        opened = langfuse_alerts.sweep_langfuse_alerts(db, client=_latency_client(70.0))
    assert opened == 1
    assert _alert(tenant_id, "langfuse_latency") is not None

    monkeypatch.setattr(langfuse_alerts, "_LAST_POLL_MONOTONIC", None, raising=False)
    with _sm() as db:
        langfuse_alerts.sweep_langfuse_alerts(db, client=_latency_client(1.0))
    assert _alert(tenant_id, "langfuse_latency") is None


def test_latency_http_error_no_alert(app_env, monkeypatch):
    tenant_id = _seed_tenant()
    _activate(monkeypatch, tenant_id)
    with _sm() as db:
        opened = langfuse_alerts.sweep_langfuse_alerts(
            db, client=_latency_client(status=500)
        )
    assert opened == 0
    assert _alert(tenant_id, "langfuse_latency") is None
    assert _alert(tenant_id, "langfuse_latency", status="resolved") is None


# -- 비활성 -------------------------------------------------------------------
def test_inactive_without_langfuse(app_env):
    tenant_id = _seed_tenant()

    def _boom(request):  # pragma: no cover
        raise AssertionError("inactive sweep touched the API")

    with _sm() as db:
        n = langfuse_alerts.sweep_langfuse_alerts(
            db, client=httpx.Client(transport=httpx.MockTransport(_boom))
        )
    assert n == 0
