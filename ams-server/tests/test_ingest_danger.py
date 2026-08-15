"""P5 위험명령 수신 — POST /api/v1/ingest/danger-command (services.danger_alerts).

검증: 토큰/테넌트 미설정 404 / 오토큰 401 / 정상 경보 open·마스킹 detail·실 테넌트 귀속·
콘솔 목록 노출 / dedupe(같은 host+pattern+sha 반복은 refresh) / 웹훅 아웃박스 스테이징 /
전역 레이트 제한 429 / Content-Length 상한 413.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

from sqlalchemy import select

from app.config import get_settings as _real_get_settings
from app.models import Alert, AlertWebhookOutbox
from app.services import alerts, danger_alerts, inventory

_TOKEN = "ingest-token-should-never-be-logged"
_SHA = hashlib.sha256(b"rm -rf /srv/secret").hexdigest()


def _seed_tenant():
    from app.db import get_sessionmaker

    with get_sessionmaker()() as db:
        return inventory.create_tenant(db, "dgr-" + hashlib.md5(b"x").hexdigest()[:6]).id


def _enable(monkeypatch, *, tenant_id=None, **overrides):
    """엔드포인트를 활성화하고 귀속 테넌트를 반환한다.

    실 테넌트를 만들어 ``danger_tenant_id``로 지정한다 — 경보가 nil이 아니라 실 테넌트에
    귀속돼 콘솔 목록·ack 동선에 잡히는지 검증하려는 것이다.
    """
    if tenant_id is None:
        tenant_id = _seed_tenant()
    settings = replace(
        _real_get_settings(),
        danger_ingest_token=_TOKEN,
        danger_tenant_id=str(tenant_id),
        **overrides,
    )
    import app.api.v1.ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(alerts, "get_settings", lambda: settings)
    danger_alerts.reset_rate_limit()
    return tenant_id


def _body(**over):
    b = {
        "patternName": "rm_recursive_force",
        "commandSha256": _SHA,
        "commandMasked": "rm************",
        "sessionId": "s-1",
        "cwd": "/work",
        "hostname": "runner-1",
        "userId": "khee@tscorp.ai",
        "ts": "2026-08-15T00:00:00+00:00",
    }
    b.update(over)
    return b


def _post(client, body, token=_TOKEN):
    headers = {"X-AMX-Ingest-Token": token} if token is not None else {}
    return client.post("/api/v1/ingest/danger-command", json=body, headers=headers)


def _open_danger():
    from app.db import get_sessionmaker

    with get_sessionmaker()() as db:
        return list(
            db.scalars(
                select(Alert).where(
                    Alert.kind == "dangerous_command", Alert.status == "open"
                )
            )
        )


def _outbox():
    from app.db import get_sessionmaker

    with get_sessionmaker()() as db:
        return list(db.scalars(select(AlertWebhookOutbox)))


def test_disabled_returns_404(client):
    # conftest 는 AMX_DANGER_INGEST_TOKEN 을 설정하지 않으므로 기본 비활성.
    r = _post(client, _body())
    assert r.status_code == 404


def test_wrong_token_401(client, monkeypatch):
    _enable(monkeypatch)
    r = _post(client, _body(), token="nope")
    assert r.status_code == 401
    assert _open_danger() == []


def test_missing_token_header_401(client, monkeypatch):
    _enable(monkeypatch)
    r = _post(client, _body(), token=None)
    assert r.status_code == 401


def test_valid_opens_alert_with_masked_detail(client, monkeypatch):
    tenant_id = _enable(monkeypatch)
    r = _post(client, _body())
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] is True
    rows = _open_danger()
    assert len(rows) == 1
    a = rows[0]
    assert a.severity == "critical"
    assert a.server_id is None
    assert a.tenant_id == tenant_id  # nil이 아니라 실 테넌트에 귀속.
    assert a.detail["commandSha256"] == _SHA
    assert a.detail["commandMasked"] == "rm************"
    assert a.detail["hostname"] == "runner-1"


def test_alert_visible_in_console_list(client, monkeypatch):
    # 실 테넌트 귀속이므로 tenant-scoped 경보 목록 API에 잡혀야 한다(콘솔·ack 동선).
    tenant_id = _enable(monkeypatch)
    assert _post(client, _body()).status_code == 200
    resp = client.get(f"/api/v1/tenants/{tenant_id}/alerts?kind=dangerous_command")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "dangerous_command"
    assert items[0]["status"] == "open"


def test_invalid_sha_422(client, monkeypatch):
    _enable(monkeypatch)
    r = _post(client, _body(commandSha256="not-a-sha"))
    assert r.status_code == 422


def test_dedupe_same_command_refreshes(client, monkeypatch):
    _enable(monkeypatch)
    assert _post(client, _body(commandMasked="rm***v1")).status_code == 200
    assert _post(client, _body(commandMasked="rm***v2")).status_code == 200
    rows = _open_danger()
    assert len(rows) == 1  # 폭주하지 않고 하나로 dedupe.
    assert rows[0].detail["commandMasked"] == "rm***v2"  # detail 갱신.


def test_different_host_opens_separate_alert(client, monkeypatch):
    _enable(monkeypatch)
    assert _post(client, _body(hostname="host-a")).status_code == 200
    assert _post(client, _body(hostname="host-b")).status_code == 200
    assert len(_open_danger()) == 2


def test_webhook_staged_once(client, monkeypatch):
    _enable(monkeypatch, alert_webhook_url="https://hook.test/x", alert_webhook_secret="s")
    assert _post(client, _body()).status_code == 200
    assert _post(client, _body(commandMasked="refresh")).status_code == 200  # dedupe refresh
    box = _outbox()
    assert len(box) == 1  # 신규 open 전이만 스테이징(refresh는 무스테이징).
    assert box[0].kind == "dangerous_command"
    assert box[0].status == "open"


def test_rate_limit_429(client, monkeypatch):
    _enable(monkeypatch, danger_rate_limit_per_min=1)
    assert _post(client, _body(hostname="rl-a")).status_code == 200
    assert _post(client, _body(hostname="rl-b")).status_code == 429


def test_token_without_tenant_is_disabled_404(client, monkeypatch):
    # 토큰은 있으나 귀속 테넌트(전용·langfuse 폴백 모두)가 없으면 비활성 404.
    settings = replace(
        _real_get_settings(),
        danger_ingest_token=_TOKEN,
        danger_tenant_id=None,
        langfuse_tenant_id=None,
    )
    import app.api.v1.ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "get_settings", lambda: settings)
    danger_alerts.reset_rate_limit()
    assert _post(client, _body()).status_code == 404


def test_oversized_content_length_413(client, monkeypatch):
    _enable(monkeypatch)
    # 본문 파싱 전 Content-Length 상한(64KB)으로 값싸게 413.
    big = b"x" * (70 * 1024)
    r = client.post(
        "/api/v1/ingest/danger-command",
        content=big,
        headers={"X-AMX-Ingest-Token": _TOKEN, "Content-Type": "application/json"},
    )
    assert r.status_code == 413
