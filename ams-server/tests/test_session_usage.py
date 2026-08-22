"""세션 비용구조 수신·조회 — POST /api/v1/ingest/session-usage, GET .../usage/sessions.

검증: 토큰/테넌트 미설정 404 / 오토큰·헤더부재 401 / 정상 저장(1h·5m 캐시 분리 보존) /
멱등 upsert(같은 세션 재전송은 행 증가 없이 값 교체) / 계정 이메일 미매칭 시 NULL 저장 /
provider가 codex인 동명 계정에 오귀속하지 않음 / 조회 엔드포인트·테넌트 격리 /
레이트 제한 429 / Content-Length 상한 413 / 보존 스윕 purge /
훅이 만드는 페이로드 키가 서버 스키마 별칭과 일치(계약 회귀 방지).
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app import schemas
from app.config import get_settings as _real_get_settings
from app.models import SessionUsage
from app.services import inventory, session_usage as svc

_TOKEN = "session-ingest-token-should-never-be-logged"
_HOOK_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "deploy" / "langfuse" / "session_usage_hook.py"
)


def _seed_tenant() -> uuid.UUID:
    from app.db import get_sessionmaker

    with get_sessionmaker()() as db:
        return inventory.create_tenant(db, "sess-" + uuid.uuid4().hex[:6]).id


# codex 계정은 credential 이 auth.json 이어야 통과한다(inventory._validate_codex_secret).
_CODEX_SECRET = '{"auth_mode": "chatgpt", "tokens": {"refresh_token": "rt"}}'


def _seed_server(tenant_id: uuid.UUID, name: str, hostname: str | None) -> uuid.UUID:
    from app.db import get_sessionmaker
    from app.services import inventory

    with get_sessionmaker()() as db:
        server = inventory.create_server(
            db, tenant_id, name=name, hostname=hostname, switch_mode="auto"
        )
        db.commit()
        return server.id


def _seed_account(tenant_id: uuid.UUID, email: str, provider: str = "claude") -> uuid.UUID:
    from app.db import get_sessionmaker

    with get_sessionmaker()() as db:
        account = inventory.create_account(
            db,
            tenant_id,
            email=email,
            credential_type="api_key",
            secret="k" if provider == "claude" else _CODEX_SECRET,
            provider=provider,
        )
        db.commit()
        return account.id


def _enable(monkeypatch, *, tenant_id=None, **overrides) -> uuid.UUID:
    """엔드포인트를 활성화하고 귀속 테넌트를 반환한다."""
    if tenant_id is None:
        tenant_id = _seed_tenant()
    settings = replace(
        _real_get_settings(),
        session_ingest_token=_TOKEN,
        session_tenant_id=str(tenant_id),
        **overrides,
    )
    import app.api.v1.ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(svc, "get_settings", lambda: settings)
    svc.reset_rate_limit()
    return tenant_id


def _model(**over) -> dict:
    m = {
        "model": "claude-opus-5",
        "inputTokens": 952,
        "outputTokens": 518061,
        "cacheReadTokens": 214640991,
        "cacheCreate1HTokens": 3384181,
        "cacheCreate5MTokens": 0,
        "thinkingTokens": 145857,
        "webSearchRequests": 2,
        "webFetchRequests": 1,
        "messageCount": 476,
        "serviceTierCounts": {"standard": 476},
        "stopReasonCounts": {"tool_use": 422, "end_turn": 53, "max_tokens": 1},
        "startedAt": "2026-08-17T13:00:47+00:00",
        "endedAt": "2026-08-19T13:56:11+00:00",
    }
    m.update(over)
    return m


def _body(**over) -> dict:
    b = {
        "sessionId": "4fc022e6-374d-4d9e-bf8e-5de30b8fb957",
        "accountEmail": "khee@sess.test",
        "hostname": "runner-1",
        "cwd": "/mnt/c/workspace/AMX",
        "models": [_model()],
    }
    b.update(over)
    return b


def _post(client, body, token=_TOKEN):
    headers = {"X-AMX-Ingest-Token": token} if token is not None else {}
    return client.post("/api/v1/ingest/session-usage", json=body, headers=headers)


def _rows(tenant_id=None):
    from app.db import get_sessionmaker

    with get_sessionmaker()() as db:
        stmt = select(SessionUsage)
        if tenant_id is not None:
            stmt = stmt.where(SessionUsage.tenant_id == tenant_id)
        return list(db.scalars(stmt.order_by(SessionUsage.model)))


# -- 인증·활성 게이트 ----------------------------------------------------------


def test_disabled_returns_404(client):
    # conftest 는 AMX_SESSION_INGEST_TOKEN 을 설정하지 않으므로 기본 비활성.
    assert _post(client, _body()).status_code == 404


def test_wrong_token_401(client, monkeypatch):
    _enable(monkeypatch)
    assert _post(client, _body(), token="nope").status_code == 401
    assert _rows() == []


def test_missing_token_header_401(client, monkeypatch):
    _enable(monkeypatch)
    assert _post(client, _body(), token=None).status_code == 401


def test_token_without_tenant_is_disabled_404(client, monkeypatch):
    settings = replace(
        _real_get_settings(),
        session_ingest_token=_TOKEN,
        session_tenant_id=None,
        langfuse_tenant_id=None,
    )
    import app.api.v1.ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "get_settings", lambda: settings)
    svc.reset_rate_limit()
    assert _post(client, _body()).status_code == 404


def test_tenant_falls_back_to_langfuse_tenant(client, monkeypatch):
    tenant_id = _seed_tenant()
    settings = replace(
        _real_get_settings(),
        session_ingest_token=_TOKEN,
        session_tenant_id=None,
        langfuse_tenant_id=str(tenant_id),
    )
    import app.api.v1.ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "get_settings", lambda: settings)
    svc.reset_rate_limit()
    assert _post(client, _body()).status_code == 200
    assert [r.tenant_id for r in _rows()] == [tenant_id]


def test_danger_token_is_not_accepted_here(client, monkeypatch):
    # 두 수신 경로는 토큰을 공유하지 않는다: danger 토큰으로 이 경로를 호출하면 401.
    tenant_id = _seed_tenant()
    settings = replace(
        _real_get_settings(),
        session_ingest_token=_TOKEN,
        session_tenant_id=str(tenant_id),
        danger_ingest_token="a-different-danger-token",
        danger_tenant_id=str(tenant_id),
    )
    import app.api.v1.ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "get_settings", lambda: settings)
    svc.reset_rate_limit()
    assert _post(client, _body(), token="a-different-danger-token").status_code == 401


# -- 저장 ---------------------------------------------------------------------


def test_valid_stores_cache_split(client, monkeypatch):
    tenant_id = _enable(monkeypatch)
    account_id = _seed_account(tenant_id, "khee@sess.test")
    r = _post(client, _body())
    assert r.status_code == 200, r.text
    assert r.json() == {"accepted": True, "rows": 1, "accountResolved": True}
    rows = _rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.tenant_id == tenant_id
    assert row.account_id == account_id
    assert row.model == "claude-opus-5"
    # 1시간/5분 캐시 쓰기가 각각 남는다(합쳐지지 않는다) — 이 경로의 존재 이유.
    assert row.cache_create_1h_tokens == 3384181
    assert row.cache_create_5m_tokens == 0
    assert row.cache_read_tokens == 214640991
    assert row.thinking_tokens == 145857
    assert row.service_tier_counts == {"standard": 476}
    assert row.stop_reason_counts["max_tokens"] == 1
    assert row.started_at == datetime(2026, 8, 17, 13, 0, 47, tzinfo=UTC)
    assert row.ended_at == datetime(2026, 8, 19, 13, 56, 11, tzinfo=UTC)


def test_multiple_models_are_separate_rows(client, monkeypatch):
    _enable(monkeypatch)
    body = _body(models=[_model(), _model(model="claude-sonnet-5", inputTokens=1)])
    assert _post(client, body).json()["rows"] == 2
    rows = _rows()
    assert [r.model for r in rows] == ["claude-opus-5", "claude-sonnet-5"]


def test_idempotent_upsert_replaces_not_accumulates(client, monkeypatch):
    _enable(monkeypatch)
    assert _post(client, _body()).status_code == 200
    # 훅은 매번 트랜스크립트 전체를 다시 집계해 보낸다 → 재전송은 교체여야 한다.
    again = _body(models=[_model(outputTokens=999, messageCount=480)])
    assert _post(client, again).status_code == 200
    rows = _rows()
    assert len(rows) == 1  # 행이 늘지 않는다.
    assert rows[0].output_tokens == 999  # 518061+999 이 아니라 교체.
    assert rows[0].message_count == 480


def test_duplicate_model_in_one_payload_is_folded(client, monkeypatch):
    _enable(monkeypatch)
    body = _body(models=[_model(outputTokens=1), _model(outputTokens=2)])
    r = _post(client, body)
    assert r.status_code == 200, r.text
    rows = _rows()
    assert len(rows) == 1
    assert rows[0].output_tokens == 2  # 뒤 항목이 이긴다(ON CONFLICT 자기충돌 회피).


def test_unmatched_email_stores_null_account(client, monkeypatch):
    _enable(monkeypatch)
    r = _post(client, _body(accountEmail="nobody@sess.test"))
    assert r.status_code == 200
    assert r.json()["accountResolved"] is False
    assert _rows()[0].account_id is None


def test_missing_email_stores_null_account(client, monkeypatch):
    _enable(monkeypatch)
    body = _body()
    body.pop("accountEmail")
    assert _post(client, body).status_code == 200
    assert _rows()[0].account_id is None


def test_codex_account_with_same_email_is_not_attributed(client, monkeypatch):
    # 같은 이메일의 codex 계정만 있으면 Claude 세션을 그 행에 매달지 않는다.
    tenant_id = _enable(monkeypatch)
    _seed_account(tenant_id, "khee@sess.test", provider="codex")
    r = _post(client, _body())
    assert r.status_code == 200
    assert r.json()["accountResolved"] is False
    assert _rows()[0].account_id is None


# -- 서버·프로젝트 축 -----------------------------------------------------------


def test_hostname_matching_server_stores_server_id(client, monkeypatch):
    tenant_id = _enable(monkeypatch)
    server_id = _seed_server(tenant_id, "srv-a", "runner-1")
    r = _post(client, _body())
    assert r.status_code == 200, r.text
    assert _rows()[0].server_id == server_id


def test_unmatched_hostname_stores_null_server_id(client, monkeypatch):
    _enable(monkeypatch)
    r = _post(client, _body(hostname="no-such-host"))
    assert r.status_code == 200, r.text
    assert _rows()[0].server_id is None


def test_cwd_last_path_segment_is_stored_as_project(client, monkeypatch):
    _enable(monkeypatch)
    assert _post(client, _body(cwd="/home/u/work/AMX/")).status_code == 200
    assert _rows()[0].project == "AMX"


def test_windows_cwd_last_path_segment_is_stored_as_project(client, monkeypatch):
    _enable(monkeypatch)
    assert _post(client, _body(cwd="C:\\ws\\AMX")).status_code == 200
    assert _rows()[0].project == "AMX"


def test_missing_cwd_stores_null_project(client, monkeypatch):
    _enable(monkeypatch)
    body = _body()
    body.pop("cwd")
    assert _post(client, body).status_code == 200
    assert _rows()[0].project is None


def test_rereport_replaces_server_id_and_project(client, monkeypatch):
    tenant_id = _enable(monkeypatch)
    server_a = _seed_server(tenant_id, "srv-a", "runner-1")
    server_b = _seed_server(tenant_id, "srv-b", "runner-2")
    assert _post(client, _body(hostname="runner-1", cwd="/work/AMX")).status_code == 200
    row = _rows()[0]
    assert row.server_id == server_a
    assert row.project == "AMX"

    assert _post(client, _body(hostname="runner-2", cwd="/work/Other")).status_code == 200
    rows = _rows()
    assert len(rows) == 1  # 같은 세션·모델의 재보고 — 행이 늘지 않는다.
    row = rows[0]
    assert row.server_id == server_b
    assert row.project == "Other"


def _touch_last_seen(server_id: uuid.UUID, when: datetime) -> None:
    from app.db import get_sessionmaker
    from app.models import Server

    with get_sessionmaker()() as db:
        server = db.get(Server, server_id)
        server.last_seen_at = when
        db.commit()


def test_hostname_match_is_case_insensitive(client, monkeypatch):
    tenant_id = _enable(monkeypatch)
    server_id = _seed_server(tenant_id, "srv-a", "Runner-1")
    r = _post(client, _body(hostname="runner-1"))
    assert r.status_code == 200, r.text
    assert _rows()[0].server_id == server_id


def test_same_hostname_multiple_servers_picks_latest_last_seen(client, monkeypatch):
    tenant_id = _enable(monkeypatch)
    older = _seed_server(tenant_id, "srv-old", "runner-1")
    newer = _seed_server(tenant_id, "srv-new", "runner-1")
    now = datetime.now(UTC)
    _touch_last_seen(older, now - timedelta(hours=1))
    _touch_last_seen(newer, now)
    r = _post(client, _body(hostname="runner-1"))
    assert r.status_code == 200, r.text
    assert _rows()[0].server_id == newer


def test_same_hostname_other_tenant_server_not_matched(client, monkeypatch):
    tenant_id = _enable(monkeypatch)
    other_tenant = _seed_tenant()
    _seed_server(other_tenant, "srv-other-tenant", "runner-1")
    r = _post(client, _body(hostname="runner-1"))
    assert r.status_code == 200, r.text
    assert _rows(tenant_id)[0].server_id is None


def test_whitespace_only_cwd_stores_null_project(client, monkeypatch):
    _enable(monkeypatch)
    assert _post(client, _body(cwd="   ")).status_code == 200
    assert _rows()[0].project is None


def test_bare_root_cwd_stores_null_project(client, monkeypatch):
    _enable(monkeypatch)
    assert _post(client, _body(cwd="/")).status_code == 200
    assert _rows()[0].project is None


def test_windows_drive_root_cwd_stores_null_project(client, monkeypatch):
    _enable(monkeypatch)
    assert _post(client, _body(cwd="C:\\")).status_code == 200
    assert _rows()[0].project is None


def test_bare_windows_drive_letter_cwd_stores_null_project(client, monkeypatch):
    _enable(monkeypatch)
    assert _post(client, _body(cwd="C:")).status_code == 200
    assert _rows()[0].project is None


def test_unc_cwd_last_path_segment_is_stored_as_project(client, monkeypatch):
    _enable(monkeypatch)
    assert _post(client, _body(cwd="//srv/share/x/")).status_code == 200
    assert _rows()[0].project == "x"


def test_negative_token_is_422(client, monkeypatch):
    _enable(monkeypatch)
    assert _post(client, _body(models=[_model(inputTokens=-1)])).status_code == 422


def test_oversized_count_map_is_422(client, monkeypatch):
    _enable(monkeypatch)
    huge = {f"tier-{i}": 1 for i in range(21)}
    assert _post(client, _body(models=[_model(serviceTierCounts=huge)])).status_code == 422


def test_empty_models_is_422(client, monkeypatch):
    _enable(monkeypatch)
    assert _post(client, _body(models=[])).status_code == 422


def test_too_many_models_is_422(client, monkeypatch):
    # 리터럴 51 — 스키마의 max_length=50을 완화하면 여기서 잡혀야 한다.
    _enable(monkeypatch)
    flood = [_model(model=f"claude-flood-{i}") for i in range(51)]
    assert _post(client, _body(models=flood)).status_code == 422


def test_rate_limit_429(client, monkeypatch):
    _enable(monkeypatch, session_rate_limit_per_min=1)
    assert _post(client, _body()).status_code == 200
    assert _post(client, _body(sessionId="other")).status_code == 429


def test_oversized_content_length_413(client, monkeypatch):
    _enable(monkeypatch)
    big = b"x" * (300 * 1024)
    r = client.post(
        "/api/v1/ingest/session-usage",
        content=big,
        headers={"X-AMX-Ingest-Token": _TOKEN, "Content-Type": "application/json"},
    )
    assert r.status_code == 413
    assert _rows() == []


# -- 조회 ---------------------------------------------------------------------


def test_read_endpoint_returns_rows(client, monkeypatch):
    tenant_id = _enable(monkeypatch)
    _seed_account(tenant_id, "khee@sess.test")
    # 창(days) 안에 들도록 ended_at을 최근으로.
    now = datetime.now(UTC)
    body = _body(
        models=[
            _model(
                startedAt=(now - timedelta(hours=2)).isoformat(),
                endedAt=(now - timedelta(minutes=5)).isoformat(),
            )
        ]
    )
    assert _post(client, body).status_code == 200

    resp = client.get(f"/api/v1/tenants/{tenant_id}/usage/sessions")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["lastReportedAt"] is not None
    assert len(payload["rows"]) == 1
    row = payload["rows"][0]
    assert row["cacheCreate1HTokens"] == 3384181
    assert row["cacheCreate5MTokens"] == 0
    assert row["accountEmail"] == "khee@sess.test"
    assert row["stopReasonCounts"]["max_tokens"] == 1


def test_read_endpoint_window_excludes_old_rows(client, monkeypatch):
    tenant_id = _enable(monkeypatch)
    old = datetime.now(UTC) - timedelta(days=40)
    body = _body(models=[_model(startedAt=old.isoformat(), endedAt=old.isoformat())])
    assert _post(client, body).status_code == 200
    assert client.get(f"/api/v1/tenants/{tenant_id}/usage/sessions").json()["rows"] == []
    wide = client.get(f"/api/v1/tenants/{tenant_id}/usage/sessions?days=60").json()
    assert len(wide["rows"]) == 1


def test_read_endpoint_is_tenant_scoped(client, monkeypatch):
    tenant_id = _enable(monkeypatch)
    other = _seed_tenant()
    now = datetime.now(UTC)
    body = _body(models=[_model(startedAt=now.isoformat(), endedAt=now.isoformat())])
    assert _post(client, body).status_code == 200
    resp = client.get(f"/api/v1/tenants/{other}/usage/sessions")
    assert resp.status_code == 200
    assert resp.json() == {"rows": [], "lastReportedAt": None}
    assert len(_rows(tenant_id)) == 1


# -- 보존 스윕 -----------------------------------------------------------------


def test_retention_purges_aged_rows(client, monkeypatch, db):
    tenant_id = _enable(monkeypatch, session_usage_retention_days=30)
    assert _post(client, _body()).status_code == 200
    # updated_at 을 창 밖으로 밀어 놓는다(보존 기준 컬럼).
    row = db.scalars(select(SessionUsage)).one()
    row.updated_at = datetime.now(UTC) - timedelta(days=31)
    db.commit()
    assert svc.sweep_session_usage_retention(db) == 1
    assert _rows(tenant_id) == []


def test_retention_disabled_keeps_rows(client, monkeypatch, db):
    tenant_id = _enable(monkeypatch, session_usage_retention_days=0)
    assert _post(client, _body()).status_code == 200
    row = db.scalars(select(SessionUsage)).one()
    row.updated_at = datetime.now(UTC) - timedelta(days=400)
    db.commit()
    assert svc.sweep_session_usage_retention(db) == 0
    assert len(_rows(tenant_id)) == 1


def test_retention_keeps_fresh_rows(client, monkeypatch, db):
    tenant_id = _enable(monkeypatch, session_usage_retention_days=30)
    assert _post(client, _body()).status_code == 200
    assert svc.sweep_session_usage_retention(db) == 0
    assert len(_rows(tenant_id)) == 1


# -- 훅 ↔ 스키마 계약 ----------------------------------------------------------


def _load_hook():
    spec = importlib.util.spec_from_file_location("session_usage_hook_contract", _HOOK_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hook_payload_keys_match_schema_aliases():
    """훅이 만든 페이로드가 서버 스키마로 **별칭 그대로** 검증돼야 한다.

    훅은 서버 코드를 import 하지 않는(표준 라이브러리 전용) 별개 프로세스라, 키 이름이
    어긋나면 런타임 422로만 드러난다. 여기서 두 쪽을 한 번에 묶어 회귀를 막는다.
    """
    hook = _load_hook()
    line = (
        '{"type":"assistant","timestamp":"2026-08-19T10:00:00.000Z","message":'
        '{"id":"m1","model":"claude-opus-5","stop_reason":"end_turn","usage":'
        '{"input_tokens":2,"output_tokens":435,"cache_read_input_tokens":22119,'
        '"cache_creation":{"ephemeral_1h_input_tokens":15847,'
        '"ephemeral_5m_input_tokens":3},"output_tokens_details":'
        '{"thinking_tokens":292},"server_tool_use":{"web_search_requests":1,'
        '"web_fetch_requests":0},"service_tier":"standard"},'
        '"content":[{"type":"text","text":"RAW-TEXT"}]}}'
    )
    models = hook.build_models(hook.aggregate([line]))
    payload = {"sessionId": "s-1", "hostname": "h", "models": models}

    parsed = schemas.SessionUsageIngest.model_validate(payload)
    stat = parsed.models[0]
    assert stat.cache_create_1h_tokens == 15847
    assert stat.cache_create_5m_tokens == 3
    assert stat.thinking_tokens == 292
    assert stat.web_search_requests == 1
    assert stat.service_tier_counts == {"standard": 1}
    assert stat.stop_reason_counts == {"end_turn": 1}
    # 어떤 필드에도 원문이 실리지 않는다.
    assert "RAW-TEXT" not in parsed.model_dump_json()


def test_count_map_value_cap_is_422(client, monkeypatch):
    # 상한이 없으면 40자리 정수가 JSONB에 들어가고 콘솔의 클라이언트 합산에서 JS 정수
    # 정밀도가 무너진다. 토큰 필드와 같은 2**53 상한을 쓴다.
    _enable(monkeypatch)
    over = _model(stopReasonCounts={"end_turn": 10**40})
    assert _post(client, _body(models=[over])).status_code == 422
    ok = _model(stopReasonCounts={"end_turn": 2**53})
    assert _post(client, _body(models=[ok])).status_code == 200


def test_long_count_map_key_is_422(client, monkeypatch):
    _enable(monkeypatch)
    long_key = _model(serviceTierCounts={"t" * 33: 1})
    assert _post(client, _body(models=[long_key])).status_code == 422


def test_hook_folded_count_map_fits_server_cap(client, monkeypatch):
    """훅이 키 상한에서 접은 결과(_OTHER 포함 20키)가 서버 상한을 통과해야 한다.

    훅과 서버의 상한이 어긋나면 정상 보고가 422로 거절된다. 훅 쪽 고유 키를 한 자리
    적게 잡는 이유가 이것이다(session_usage_hook._MAX_DISTINCT_COUNT_KEYS).
    """
    hook = _load_hook()
    lines = [
        (
            '{"type":"assistant","message":{"id":"m%d","model":"claude-opus-5",'
            '"stop_reason":"reason-%d","usage":{"output_tokens":1}}}' % (i, i)
        )
        for i in range(60)
    ]
    models = hook.build_models(hook.aggregate(lines))
    counts = models[0]["stopReasonCounts"]
    assert len(counts) == 20 and counts["<other>"] == 41

    _enable(monkeypatch)
    body = _body(models=models)
    body["models"][0]["startedAt"] = None
    body["models"][0]["endedAt"] = None
    assert _post(client, body).status_code == 200, "훅 산출물이 서버 상한을 넘겼다"
    assert _rows()[0].stop_reason_counts["<other>"] == 41


def test_rereport_leaves_rows_of_models_no_longer_present(client, monkeypatch):
    """재보고에서 빠진 모델의 옛 행은 남는다 — 현재 동작을 고정한다.

    훅이 부분 보고를 보낼 수 있으므로(트랜스크립트 회전·잘림) 사라진 모델의 행을 지우면
    정상 보고가 데이터를 없애는 셈이 된다. 정리는 90일 보존 스윕이 맡는다.
    """
    _enable(monkeypatch)
    two = _body(models=[_model(), _model(model="claude-sonnet-5")])
    assert _post(client, two).json()["rows"] == 2
    one = _body(models=[_model(outputTokens=1)])
    assert _post(client, one).json()["rows"] == 1
    rows = _rows()
    assert [r.model for r in rows] == ["claude-opus-5", "claude-sonnet-5"]
    assert rows[0].output_tokens == 1  # 보고된 모델은 갱신.


def test_truncated_flag_is_stored_and_returned(client, monkeypatch):
    tenant_id = _enable(monkeypatch)
    now = datetime.now(UTC)
    body = _body(
        truncated=True,
        models=[_model(startedAt=now.isoformat(), endedAt=now.isoformat())],
    )
    assert _post(client, body).status_code == 200
    assert _rows()[0].truncated is True
    row = client.get(f"/api/v1/tenants/{tenant_id}/usage/sessions").json()["rows"][0]
    assert row["truncated"] is True


def test_truncated_defaults_false_and_flips_on_rereport(client, monkeypatch):
    # 플래그가 없는 페이로드는 False. 재보고로 갱신된다(멱등 upsert의 set_ 에 포함).
    _enable(monkeypatch)
    assert _post(client, _body()).status_code == 200
    assert _rows()[0].truncated is False
    assert _post(client, _body(truncated=True)).status_code == 200
    assert _rows()[0].truncated is True


def test_adversary_label_payload_is_bounded_end_to_end(client, monkeypatch):
    """훅이 만든 최악 페이로드가 서버를 통과해도 임의 텍스트를 담지 못한다.

    문자셋만 있던 시점에는 base64url 라벨로 6만 자(직렬화 88KB)가 통과해 JSONB에 저장됐다.
    라벨 총량 상한이 그 통로를 닫는다 — 이 테스트는 훅 산출물을 실제 엔드포인트에 먹여
    저장된 라벨까지 확인한다.
    """
    import base64

    _enable(monkeypatch)
    hook = _load_hook()

    def b64(prefix: str, n: int) -> str:
        raw = base64.urlsafe_b64encode((prefix * 64).encode()).decode().rstrip("=")
        return (raw + "-_")[:n]

    lines = [
        (
            '{"type":"assistant","message":{"id":"m%d-%d","model":"%s",'
            '"stop_reason":"%s","usage":{"service_tier":"%s","output_tokens":1}}}'
            % (mi, ki, b64(f"M{mi}", 48), b64(f"S{mi}{ki}", 32), b64(f"T{mi}{ki}", 32))
        )
        for mi in range(50)
        for ki in range(19)
    ]
    models = hook.build_models(hook.aggregate(lines))
    body = _body(models=models)
    for m in body["models"]:
        m["startedAt"] = None
        m["endedAt"] = None
    assert len(json.dumps(body)) < 8192, "페이로드가 여전히 크다"
    assert _post(client, body).status_code == 200

    stored = json.dumps([
        {"model": r.model, "t": r.service_tier_counts, "s": r.stop_reason_counts}
        for r in _rows()
    ])
    assert b64("M40", 48) not in stored
    assert "<other>" in stored
