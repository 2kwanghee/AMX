"""P5 경보 웹훅 발송 계층 — services.alert_webhook + alerts 스테이징(BACKLOG G41).

실제 PostgreSQL에 아웃박스를 스테이징하고 httpx.MockTransport로 발송을 핀한다.
검증: open/resolve 전이에서만 아웃박스 스테이징(refresh는 무스테이징), 드레인 성공 시
행 삭제 + 서명/페이로드 형태, 실패 시 백오프, 상한 초과 폐기, 비활성 설정 완전 무부작용.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select

from app.config import get_settings as _real_get_settings
from app.db import get_sessionmaker
from app.models import Alert, AlertWebhookOutbox
from app.services import alert_webhook, alerts, inventory

_URL = "https://hook.test/ams"
_SECRET = "whsec-should-never-be-logged"


def _sm():
    return get_sessionmaker()()


def _enable(monkeypatch, *, url=_URL, secret=_SECRET):
    settings = replace(_real_get_settings(), alert_webhook_url=url, alert_webhook_secret=secret)
    monkeypatch.setattr(alerts, "get_settings", lambda: settings)
    monkeypatch.setattr(alert_webhook, "get_settings", lambda: settings)
    return settings


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _seed_server() -> tuple[uuid.UUID, uuid.UUID]:
    with _sm() as db:
        tenant = inventory.create_tenant(db, "wh-" + uuid.uuid4().hex[:8])
        server = inventory.create_server(
            db, tenant.id, name="s1", hostname="h1", switch_mode="auto"
        )
        return tenant.id, server.id


def _outbox() -> list[AlertWebhookOutbox]:
    with _sm() as db:
        return list(
            db.scalars(select(AlertWebhookOutbox).order_by(AlertWebhookOutbox.created_at))
        )


def _insert_outbox(*, attempt=0, next_attempt_at=None, status="open", kind="all_exhausted"):
    with _sm() as db:
        row = AlertWebhookOutbox(
            alert_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            server_id=None,
            kind=kind,
            status=status,
            detail={"k": "v"},
            occurred_at=datetime.now(UTC),
            attempt=attempt,
            next_attempt_at=next_attempt_at or datetime.now(UTC),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


# -- staging: open/resolve 전이에서만 -----------------------------------------
def test_open_and_resolve_stage_outbox(app_env, monkeypatch):
    _enable(monkeypatch)
    tenant_id, server_id = _seed_server()

    with _sm() as db:
        alerts.open_alert(
            db, tenant_id=tenant_id, server_id=server_id,
            kind="all_exhausted", severity="critical", detail={"source": "test"},
        )
        db.commit()
    rows = _outbox()
    assert len(rows) == 1
    assert rows[0].status == "open"
    assert rows[0].kind == "all_exhausted"
    assert rows[0].server_id == server_id

    # 이미 열린 경보를 다시 open → refresh일 뿐 새 전이가 아니므로 무스테이징.
    with _sm() as db:
        alerts.open_alert(
            db, tenant_id=tenant_id, server_id=server_id,
            kind="all_exhausted", severity="critical", detail={"source": "again"},
        )
        db.commit()
    assert len(_outbox()) == 1

    # resolve → 실제 닫힌 전이 한 건 스테이징.
    with _sm() as db:
        alerts.resolve(db, server_id=server_id, kind="all_exhausted")
        db.commit()
    rows = _outbox()
    assert len(rows) == 2
    assert rows[1].status == "resolved"

    # 재-resolve는 닫을 행이 없어 무스테이징(멱등).
    with _sm() as db:
        alerts.resolve(db, server_id=server_id, kind="all_exhausted")
        db.commit()
    assert len(_outbox()) == 2


def test_disabled_stages_nothing(app_env, monkeypatch):
    # 웹훅 비활성이면 open/resolve가 아웃박스를 전혀 건드리지 않는다(완전 무부작용).
    settings = replace(_real_get_settings(), alert_webhook_url=None, alert_webhook_secret=None)
    monkeypatch.setattr(alerts, "get_settings", lambda: settings)
    tenant_id, server_id = _seed_server()
    with _sm() as db:
        alerts.open_alert(
            db, tenant_id=tenant_id, server_id=server_id,
            kind="all_exhausted", severity="critical",
        )
        alerts.resolve(db, server_id=server_id, kind="all_exhausted")
        db.commit()
    assert _outbox() == []
    # 경보 자체는 정상적으로 열렸다 닫힌다.
    with _sm() as db:
        assert db.scalar(select(Alert).where(Alert.server_id == server_id)) is not None


# -- drain: 성공 삭제 + 서명/페이로드 -----------------------------------------
def test_drain_success_deletes_and_signs(app_env, monkeypatch):
    settings = _enable(monkeypatch)
    tenant_id, server_id = _seed_server()
    with _sm() as db:
        alerts.open_alert(
            db, tenant_id=tenant_id, server_id=server_id,
            kind="all_exhausted", severity="critical", detail={"source": "test"},
        )
        db.commit()

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        ts = request.headers["X-AMS-Timestamp"]
        expected = "sha256=" + hmac.new(
            _SECRET.encode(), (ts + body).encode(), hashlib.sha256
        ).hexdigest()
        assert request.headers["X-AMS-Signature"] == expected  # 수신자 검증 예시
        payload = json.loads(body)
        seen.update(payload)
        return httpx.Response(200, json={"ok": True})

    with _sm() as db:
        n = alert_webhook.sweep_alert_webhook(db, client=_mock_client(handler))

    assert n == 1
    assert _outbox() == []
    assert seen["kind"] == "all_exhausted"
    assert seen["status"] == "open"
    assert seen["tenantId"] == str(tenant_id)
    assert seen["serverId"] == str(server_id)
    assert _SECRET not in json.dumps(seen)


# -- drain: 실패 백오프 -------------------------------------------------------
def test_drain_failure_backoff(app_env, monkeypatch):
    _enable(monkeypatch)
    _insert_outbox()
    before = datetime.now(UTC)

    def handler(request):
        return httpx.Response(500, json={"err": "nope"})

    with _sm() as db:
        n = alert_webhook.sweep_alert_webhook(db, client=_mock_client(handler))

    assert n == 0
    rows = _outbox()
    assert len(rows) == 1
    assert rows[0].attempt == 1
    assert rows[0].next_attempt_at > before  # 뒤로 미뤄짐


# -- drain: 상한 초과 폐기 ----------------------------------------------------
def test_drain_discards_over_cap(app_env, monkeypatch):
    _enable(monkeypatch)
    # 다음 실패면 attempt가 상한에 도달 → 폐기.
    _insert_outbox(attempt=alert_webhook._MAX_ATTEMPTS - 1,
                   next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))

    def handler(request):
        return httpx.Response(503)

    with _sm() as db:
        n = alert_webhook.sweep_alert_webhook(db, client=_mock_client(handler))

    assert n == 0
    assert _outbox() == []  # 폐기됨(무한 적재 금지)


# -- drain: 비활성 무부작용 ---------------------------------------------------
def test_drain_inactive_noop(app_env, monkeypatch):
    settings = replace(_real_get_settings(), alert_webhook_url=None, alert_webhook_secret=None)
    monkeypatch.setattr(alert_webhook, "get_settings", lambda: settings)
    row_id = _insert_outbox()  # 직접 삽입(스테이징 우회)

    def _boom(request):  # pragma: no cover - 호출되면 안 됨
        raise AssertionError("inactive drain touched the network")

    with _sm() as db:
        n = alert_webhook.sweep_alert_webhook(db, client=_mock_client(_boom))

    assert n == 0
    assert {r.id for r in _outbox()} == {row_id}  # 그대로 남음
