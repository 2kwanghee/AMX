"""P5 위험명령 수신 — POST /api/v1/ingest/danger-command (services.danger_alerts).

검증: 토큰 미설정 404 / 오토큰 401 / 정상 경보 open·마스킹 detail / dedupe(같은
host+pattern+sha 반복은 refresh) / 웹훅 아웃박스 스테이징 / 전역 레이트 제한 429.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace

from sqlalchemy import select

from app.config import get_settings as _real_get_settings
from app.models import Alert, AlertWebhookOutbox
from app.services import alerts, danger_alerts

_TOKEN = "ingest-token-should-never-be-logged"
_SHA = hashlib.sha256(b"rm -rf /srv/secret").hexdigest()


def _enable(monkeypatch, **overrides):
    settings = replace(_real_get_settings(), danger_ingest_token=_TOKEN, **overrides)
    import app.api.v1.ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(alerts, "get_settings", lambda: settings)
    danger_alerts.reset_rate_limit()
    return settings


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
    _enable(monkeypatch)
    r = _post(client, _body())
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] is True
    rows = _open_danger()
    assert len(rows) == 1
    a = rows[0]
    assert a.severity == "critical"
    assert a.server_id is None
    assert a.tenant_id == danger_alerts.SYSTEM_TENANT_ID
    assert a.detail["commandSha256"] == _SHA
    assert a.detail["commandMasked"] == "rm************"
    assert a.detail["hostname"] == "runner-1"


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
