"""Langfuse metrics sweep — services.langfuse_metrics (P4 console monitoring).

The Metrics API is stubbed with an httpx.MockTransport so the sweep's request
shape (grouped model query vs per-userId filter query, both crossed with the
usageType dimension) and its roll-up upsert are pinned without a live Langfuse.
What is checked: the model axis (including the null model folded to "unknown"),
the per-account user axis, usageByType-driven cache-token measurement, an unknown
usageType being ignored, fetch-error isolation (HTTP and bad JSON), the inactive
no-op, idempotent recompute-replace, the process-local cadence gate, the window
floor of 2 (a closed day is re-rolled), and the account cap.
"""

from __future__ import annotations

import itertools
import json
import logging
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx

from app.config import get_settings as _real_get_settings
from app.db import get_sessionmaker
from app.models import LangfuseUsageRollup
from app.services import inventory, langfuse_metrics

from tests.test_grpc_channel import _oauth_secret

# A fixed "now" so the sliding window resolves to deterministic UTC days.
_NOW = datetime(2026, 8, 15, 9, 30, tzinfo=UTC)
_TODAY = _NOW.date()
_YESTERDAY = _TODAY - timedelta(days=1)

# The exact fromTimestamp strings the sweep sends for each day of the window.
_FROM_TODAY = langfuse_metrics._iso(langfuse_metrics._floor_day(_NOW))
_FROM_YESTERDAY = langfuse_metrics._iso(
    langfuse_metrics._floor_day(_NOW) - timedelta(days=1)
)

_BASE = "http://langfuse.test"
_PK = "pk-test"
_SK = "sk-secret-should-never-be-logged"


def _sm():
    return get_sessionmaker()()


def _seed_tenant(*emails: str) -> uuid.UUID:
    with _sm() as db:
        tenant = inventory.create_tenant(db, "lf-" + uuid.uuid4().hex[:8])
        for email in emails:
            inventory.create_account(
                db, tenant.id, email=email, credential_type="oauth",
                secret=_oauth_secret(email),
            )
        return tenant.id


def _activate(monkeypatch, tenant_id, *, window=1, ui_url=None, max_accounts=100,
              poll_seconds=300):
    settings = replace(
        _real_get_settings(),
        langfuse_base_url=_BASE,
        langfuse_public_key=_PK,
        langfuse_secret_key=_SK,
        langfuse_tenant_id=str(tenant_id),
        langfuse_ui_url=ui_url,
        langfuse_metrics_window_days=window,
        langfuse_max_accounts=max_accounts,
        langfuse_poll_seconds=poll_seconds,
    )
    monkeypatch.setattr(langfuse_metrics, "get_settings", lambda: settings)
    monkeypatch.setattr(langfuse_metrics, "_now", lambda: _NOW)
    # Reset the process-local cadence gate and hand out ever-advancing monotonic
    # time so back-to-back sweep calls in a test are never throttled by default.
    monkeypatch.setattr(langfuse_metrics, "_LAST_POLL_MONOTONIC", None, raising=False)
    ticks = itertools.count(0, 10**6)
    monkeypatch.setattr(langfuse_metrics, "_monotonic", lambda: next(ticks))


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _payload(rows):
    return {"data": rows}


_NO_MODEL = object()


def _ut_rows(count, *, model=_NO_MODEL, **toks):
    """Expand token kwargs into usageType-crossed Metrics rows for one group.

    Mirrors the live API: usageByType is summed per usageType, and count_count
    repeats identically across a group's rows. ``model`` (incl. None) adds a
    providedModelName field for the model axis; omit it for the user axis.
    """
    kinds = [
        ("input", "input"),
        ("output", "output"),
        ("cache_read", "cache_read_input_tokens"),
        ("cache_creation", "cache_creation_input_tokens"),
        ("total", "total"),
    ]
    rows = []
    for kw, utype in kinds:
        row = {"usageType": utype, "sum_usageByType": toks.get(kw, 0),
               "count_count": count}
        if model is not _NO_MODEL:
            row["providedModelName"] = model
        rows.append(row)
    return rows


def _rows(tenant_id, dimension):
    with _sm() as db:
        return {
            (r.day, r.key): r
            for r in db.query(LangfuseUsageRollup).filter_by(
                tenant_id=tenant_id, dimension=dimension
            )
        }


# -- inactive -----------------------------------------------------------------
def test_sweep_inactive_without_settings(app_env):
    # Real settings carry no AMX_LANGFUSE_* — the sweep is a no-op and must not
    # even build a client (a client that raises proves it is never called).
    def _boom(request):  # pragma: no cover - must not run
        raise AssertionError("sweep touched the API while inactive")

    with _sm() as db:
        n = langfuse_metrics.sweep_langfuse_metrics(db, client=_mock_client(_boom))
    assert n == 0


# -- model + user axes --------------------------------------------------------
def test_sweep_model_and_user_axes(app_env, monkeypatch):
    tenant_id = _seed_tenant("alice@ex.com", "bob@ex.com")
    _activate(monkeypatch, tenant_id)

    def handler(request: httpx.Request) -> httpx.Response:
        # Auth header carries the Basic secret; assert present but never surfaced.
        assert request.headers["Authorization"].startswith("Basic ")
        query = json.loads(request.url.params["query"])
        assert query["view"] == "observations"
        assert query["metrics"][0]["measure"] == "usageByType"
        # Only "today" carries data; yesterday (window floor of 2) is empty here.
        if query["fromTimestamp"] != _FROM_TODAY:
            return httpx.Response(200, json=_payload([]))
        dims = [d["field"] for d in query.get("dimensions", [])]
        assert "usageType" in dims
        if "providedModelName" in dims:
            return httpx.Response(200, json=_payload(
                _ut_rows(5, model="claude-sonnet-5", input=100, output=20,
                         cache_read=500, cache_creation=30, total=650)
                + _ut_rows(3, model=None, total=0)
            ))
        flt = query["filters"][0]
        assert flt["column"] == "userId"
        per_user = {
            "alice@ex.com": _ut_rows(2, input=40, output=10, total=50),
            "bob@ex.com": _ut_rows(0, total=0),
        }
        return httpx.Response(200, json=_payload(per_user[flt["value"]]))

    with _sm() as db:
        n = langfuse_metrics.sweep_langfuse_metrics(db, client=_mock_client(handler))

    # model: sonnet + unknown (null folded, count 3 keeps it despite 0 tokens).
    # user: alice only (bob had no activity, skipped). All on today.
    assert n == 3
    models = _rows(tenant_id, "model")
    assert set(models) == {(_TODAY, "claude-sonnet-5"), (_TODAY, "unknown")}
    sonnet = models[(_TODAY, "claude-sonnet-5")]
    assert sonnet.input_tokens == 100  # pure input, not input+cache
    assert sonnet.output_tokens == 20
    assert sonnet.total_tokens == 650
    assert sonnet.observation_count == 5  # from the total row only
    assert sonnet.cache_read_tokens == 500
    assert sonnet.cache_creation_tokens == 30
    assert models[(_TODAY, "unknown")].observation_count == 3

    users = _rows(tenant_id, "user")
    assert set(users) == {(_TODAY, "alice@ex.com")}
    assert users[(_TODAY, "alice@ex.com")].total_tokens == 50


# -- window floor of 2: a closed day (yesterday) is re-rolled ------------------
def test_window_floor_two_rerolls_closed_day(app_env, monkeypatch):
    # window=1 is clamped up to 2, so yesterday (already closed/final) is fetched
    # and its finalised total stored — the W2 defect this guards against.
    tenant_id = _seed_tenant()
    _activate(monkeypatch, tenant_id, window=1)

    def handler(request):
        query = json.loads(request.url.params["query"])
        dims = [d["field"] for d in query.get("dimensions", [])]
        if "providedModelName" not in dims:
            return httpx.Response(200, json=_payload([]))
        totals = {_FROM_TODAY: 11, _FROM_YESTERDAY: 22}
        tok = totals.get(query["fromTimestamp"], 0)
        return httpx.Response(200, json=_payload(
            _ut_rows(1, model="m", total=tok)))

    with _sm() as db:
        n = langfuse_metrics.sweep_langfuse_metrics(db, client=_mock_client(handler))
    assert n == 2
    models = _rows(tenant_id, "model")
    assert models[(_TODAY, "m")].total_tokens == 11
    assert models[(_YESTERDAY, "m")].total_tokens == 22


# -- cadence gate -------------------------------------------------------------
def test_cadence_gate_skips_under_interval(app_env, monkeypatch):
    tenant_id = _seed_tenant()
    _activate(monkeypatch, tenant_id, window=2, poll_seconds=300)
    # Drive a controllable monotonic clock instead of the advancing default.
    clock = {"t": 1000.0}
    monkeypatch.setattr(langfuse_metrics, "_monotonic", lambda: clock["t"])

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        query = json.loads(request.url.params["query"])
        dims = [d["field"] for d in query.get("dimensions", [])]
        if "providedModelName" not in dims:
            return httpx.Response(200, json=_payload([]))
        return httpx.Response(200, json=_payload(
            _ut_rows(1, model="m", input=1, total=1)))

    with _sm() as db:
        assert langfuse_metrics.sweep_langfuse_metrics(db, client=_mock_client(handler)) == 2
    hits_after_first = calls["n"]
    assert hits_after_first > 0

    # 200s later (< 300s poll) — gated, no new API calls, no work.
    clock["t"] = 1200.0
    with _sm() as db:
        assert langfuse_metrics.sweep_langfuse_metrics(db, client=_mock_client(handler)) == 0
    assert calls["n"] == hits_after_first

    # 300s past the first run — due again.
    clock["t"] = 1300.0
    with _sm() as db:
        assert langfuse_metrics.sweep_langfuse_metrics(db, client=_mock_client(handler)) == 2
    assert calls["n"] > hits_after_first


# -- account cap --------------------------------------------------------------
def test_account_cap_truncates_sorted(app_env, monkeypatch):
    tenant_id = _seed_tenant("a@ex.com", "b@ex.com", "c@ex.com")
    _activate(monkeypatch, tenant_id, window=2, max_accounts=2)

    queried: set[str] = set()

    def handler(request):
        query = json.loads(request.url.params["query"])
        if "filters" in query:  # user axis carries the userId filter
            queried.add(query["filters"][0]["value"])
        return httpx.Response(200, json=_payload([]))

    with _sm() as db:
        langfuse_metrics.sweep_langfuse_metrics(db, client=_mock_client(handler))
    # Only the first 2 sorted emails are ever queried; c@ex.com is dropped.
    assert queried == {"a@ex.com", "b@ex.com"}


# -- fetch-error isolation ----------------------------------------------------
def test_sweep_http_error_isolated(app_env, monkeypatch):
    tenant_id = _seed_tenant("alice@ex.com")
    _activate(monkeypatch, tenant_id, window=2)

    def handler(request):
        return httpx.Response(502, text="bad gateway")

    with _sm() as db:
        n = langfuse_metrics.sweep_langfuse_metrics(db, client=_mock_client(handler))
    assert n == 0
    assert _rows(tenant_id, "model") == {}
    assert _rows(tenant_id, "user") == {}


def test_sweep_bad_json_isolated(app_env, monkeypatch):
    tenant_id = _seed_tenant()
    _activate(monkeypatch, tenant_id, window=2)

    def handler(request):
        # 200 with an unparseable body -> json.JSONDecodeError (a ValueError).
        return httpx.Response(200, text="not json{")

    with _sm() as db:
        n = langfuse_metrics.sweep_langfuse_metrics(db, client=_mock_client(handler))
    assert n == 0
    assert _rows(tenant_id, "model") == {}


# -- idempotent recompute-replace ---------------------------------------------
def test_sweep_idempotent_recompute_replace(app_env, monkeypatch):
    tenant_id = _seed_tenant()
    _activate(monkeypatch, tenant_id, window=2)
    total = {"v": 120}

    def handler(request):
        query = json.loads(request.url.params["query"])
        dims = [d["field"] for d in query.get("dimensions", [])]
        if "providedModelName" in dims and query["fromTimestamp"] == _FROM_TODAY:
            return httpx.Response(200, json=_payload(
                _ut_rows(5, model="claude-sonnet-5", input=100, output=20,
                         total=total["v"])))
        return httpx.Response(200, json=_payload([]))

    with _sm() as db:
        assert langfuse_metrics.sweep_langfuse_metrics(db, client=_mock_client(handler)) == 1
    assert _rows(tenant_id, "model")[(_TODAY, "claude-sonnet-5")].total_tokens == 120

    # Second run with a changed total must REPLACE the row, not duplicate it.
    total["v"] = 999
    with _sm() as db:
        assert langfuse_metrics.sweep_langfuse_metrics(db, client=_mock_client(handler)) == 1
    models = _rows(tenant_id, "model")
    assert set(models) == {(_TODAY, "claude-sonnet-5")}
    assert models[(_TODAY, "claude-sonnet-5")].total_tokens == 999


# -- cache tokens are measured (usageByType), not zeroed ----------------------
def test_cache_tokens_measured(app_env, monkeypatch):
    # The core fix: cache_read/creation land as measured values, and input_tokens
    # is pure input (not the old input+cache sum), on both axes.
    tenant_id = _seed_tenant("alice@ex.com")
    _activate(monkeypatch, tenant_id, window=1)

    def handler(request):
        query = json.loads(request.url.params["query"])
        if query["fromTimestamp"] != _FROM_TODAY:
            return httpx.Response(200, json=_payload([]))
        dims = [d["field"] for d in query.get("dimensions", [])]
        toks = dict(input=100, output=20, cache_read=800, cache_creation=60, total=980)
        if "providedModelName" in dims:
            return httpx.Response(200, json=_payload(
                _ut_rows(4, model="claude-sonnet-5", **toks)))
        return httpx.Response(200, json=_payload(_ut_rows(4, **toks)))

    with _sm() as db:
        langfuse_metrics.sweep_langfuse_metrics(db, client=_mock_client(handler))

    m = _rows(tenant_id, "model")[(_TODAY, "claude-sonnet-5")]
    assert m.input_tokens == 100  # pure input, not input+cache_read+cache_creation
    assert m.cache_read_tokens == 800
    assert m.cache_creation_tokens == 60
    assert m.total_tokens == 980
    assert m.observation_count == 4  # from the total row only, not summed per type
    u = _rows(tenant_id, "user")[(_TODAY, "alice@ex.com")]
    assert u.cache_read_tokens == 800
    assert u.cache_creation_tokens == 60
    assert u.observation_count == 4


# -- unknown usageType is ignored (with a warning), not misfiled --------------
def test_unknown_usage_type_ignored(app_env, monkeypatch):
    tenant_id = _seed_tenant()
    _activate(monkeypatch, tenant_id, window=1)

    def handler(request):
        query = json.loads(request.url.params["query"])
        dims = [d["field"] for d in query.get("dimensions", [])]
        if "providedModelName" not in dims or query["fromTimestamp"] != _FROM_TODAY:
            return httpx.Response(200, json=_payload([]))
        rows = _ut_rows(2, model="m", input=10, total=15)
        rows.append({"providedModelName": "m", "usageType": "brand_new_type",
                     "sum_usageByType": 999, "count_count": 2})
        return httpx.Response(200, json=_payload(rows))

    with _sm() as db:
        langfuse_metrics.sweep_langfuse_metrics(db, client=_mock_client(handler))
    m = _rows(tenant_id, "model")[(_TODAY, "m")]
    assert m.input_tokens == 10
    assert m.total_tokens == 15
    assert m.observation_count == 2
    # The unknown 999 was dropped into no column.
    assert (m.output_tokens, m.cache_read_tokens, m.cache_creation_tokens) == (0, 0, 0)


# -- row_limit 상한 명시 (G37) ------------------------------------------------
def _bare_settings():
    return replace(
        _real_get_settings(),
        langfuse_base_url=_BASE,
        langfuse_public_key=_PK,
        langfuse_secret_key=_SK,
    )


def test_every_query_pins_row_limit_config(app_env):
    # 페이지네이션이 없는 Metrics API의 기본 100행 무언 잘림을 막기 위해 모든 쿼리가
    # config.row_limit=1000을 실어야 한다. _query_metrics 단일 통로에서 주입된다.
    seen = {}

    def handler(request):
        q = json.loads(request.url.params["query"])
        seen["config"] = q.get("config")
        return httpx.Response(200, json=_payload([]))

    data = langfuse_metrics._query_metrics(
        _mock_client(handler), _bare_settings(), {"view": "observations"}
    )
    assert data == []
    assert seen["config"] == {"row_limit": 1000}


def test_row_limit_reached_warns_but_loads_rows(app_env, monkeypatch):
    # 응답 행 수가 상한과 같으면 잘림 가능성 경고를 남기되, 받은 행은 그대로 반환한다.
    monkeypatch.setattr(langfuse_metrics, "_ROW_LIMIT", 2)
    logger = logging.getLogger("ams.langfuse")
    handler_rec = _RecordCollector()
    logger.addHandler(handler_rec)
    prev_level, prev_disabled = logger.level, logger.disabled
    logger.setLevel(logging.DEBUG)
    logger.disabled = False
    try:
        def at_cap(request):
            return httpx.Response(200, json=_payload(
                [{"providedModelName": "a"}, {"providedModelName": "b"}]))

        def under_cap(request):
            return httpx.Response(200, json=_payload([{"providedModelName": "a"}]))

        capped = langfuse_metrics._query_metrics(
            _mock_client(at_cap), _bare_settings(),
            {"view": "observations", "dimensions": [{"field": "providedModelName"}]},
        )
        under = langfuse_metrics._query_metrics(
            _mock_client(under_cap), _bare_settings(), {"view": "observations"}
        )
    finally:
        logger.removeHandler(handler_rec)
        logger.setLevel(prev_level)
        logger.disabled = prev_disabled

    assert len(capped) == 2  # 상한 도달 시에도 받은 행은 정상 반환
    assert len(under) == 1
    warnings = [m for m in handler_rec.messages if "row_limit" in m]
    assert len(warnings) == 1  # 상한 미만(1행)에서는 경고 없음
    assert "결과가 잘렸을 수 있음" in warnings[0]


# -- 마지막 정상 스윕 마커 (langfuse_stale 판정 소스) --------------------------
def test_sync_marker_advances_on_clean_tick(app_env, monkeypatch):
    # 정상 왕복이면 데이터가 없어도(무활동) 마커가 _NOW로 상향된다.
    tenant_id = _seed_tenant()
    _activate(monkeypatch, tenant_id, window=1)

    def handler(request):
        return httpx.Response(200, json=_payload([]))

    with _sm() as db:
        n = langfuse_metrics.sweep_langfuse_metrics(db, client=_mock_client(handler))
    assert n == 0  # 무활동
    with _sm() as db:
        assert langfuse_metrics.last_metrics_sync_at(db) == _NOW


def test_sync_marker_not_advanced_on_error(app_env, monkeypatch):
    # HTTP 오류로 왕복이 깨지면 마커를 갱신하지 않는다 → langfuse_stale이 정체를 잡는다.
    tenant_id = _seed_tenant()
    _activate(monkeypatch, tenant_id, window=1)

    def handler(request):
        return httpx.Response(500, json={"e": "boom"})

    with _sm() as db:
        n = langfuse_metrics.sweep_langfuse_metrics(db, client=_mock_client(handler))
    assert n == 0
    with _sm() as db:
        assert langfuse_metrics.last_metrics_sync_at(db) is None


class _RecordCollector(logging.Handler):
    """Captures ams.langfuse messages directly, independent of caplog quirks
    (alembic's fileConfig disables existing loggers — see test_billing.py)."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


# -- 미지 usageType 경고는 프로세스 수명 동안 값별 1회만 (G38) ------------------
def test_unknown_usage_type_warning_suppressed_per_value(monkeypatch):
    # 프로세스-로컬 seen set을 비워 다른 테스트의 잔여 상태와 격리한다.
    monkeypatch.setattr(langfuse_metrics, "_WARNED_UNKNOWN_USAGE_TYPES", set())
    logger = logging.getLogger("ams.langfuse")
    handler = _RecordCollector()
    logger.addHandler(handler)
    prev_level, prev_disabled = logger.level, logger.disabled
    logger.setLevel(logging.DEBUG)
    logger.disabled = False
    try:
        tenant_id = uuid.uuid4()
        row_a = {"usageType": "brand_new_type", "sum_usageByType": 1, "count_count": 1}
        row_b = {"usageType": "another_type", "sum_usageByType": 1, "count_count": 1}
        # 같은 값이 두 번 등장(같은/다음 스윕 모사) → 경고 1회. 다른 값은 각각 1회.
        langfuse_metrics._assemble(tenant_id, _TODAY, "model", "m", [row_a])
        langfuse_metrics._assemble(tenant_id, _TODAY, "model", "m", [row_a])
        langfuse_metrics._assemble(tenant_id, _TODAY, "user", "u", [row_b])
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)
        logger.disabled = prev_disabled

    warnings = [m for m in handler.messages if "unknown usageType" in m]
    assert len(warnings) == 2
    assert sum("brand_new_type" in m for m in warnings) == 1
    assert sum("another_type" in m for m in warnings) == 1
    assert "이후 동일 값 경고는 억제됨" in warnings[0]
