"""Langfuse metrics sweep — services.langfuse_metrics (P4 console monitoring).

The Metrics API is stubbed with an httpx.MockTransport so the sweep's request
shape (grouped model query vs per-userId filter query) and its roll-up upsert are
pinned without a live Langfuse. What is checked: the model axis (including the null
model folded to "unknown"), the per-account user axis, HTTP-error isolation, the
inactive no-op, and idempotent recompute-replace.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from app.config import get_settings as _real_get_settings
from app.db import get_sessionmaker
from app.models import Account, LangfuseUsageRollup
from app.services import inventory, langfuse_metrics

from tests.test_grpc_channel import _oauth_secret

# A fixed "now" so the sliding window resolves to one deterministic UTC day.
_NOW = datetime(2026, 8, 15, 9, 30, tzinfo=UTC)
_DAY = _NOW.date()

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


def _activate(monkeypatch, tenant_id, *, window=1, ui_url=None):
    settings = replace(
        _real_get_settings(),
        langfuse_base_url=_BASE,
        langfuse_public_key=_PK,
        langfuse_secret_key=_SK,
        langfuse_tenant_id=str(tenant_id),
        langfuse_ui_url=ui_url,
        langfuse_metrics_window_days=window,
    )
    monkeypatch.setattr(langfuse_metrics, "get_settings", lambda: settings)
    monkeypatch.setattr(langfuse_metrics, "_now", lambda: _NOW)


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _model_payload(rows):
    return {"data": rows}


def _rows(tenant_id, dimension):
    with _sm() as db:
        return {
            r.key: r
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
        # Auth header carries the Basic secret; assert it is present but never
        # surfaced elsewhere.
        assert request.headers["Authorization"].startswith("Basic ")
        query = json.loads(request.url.params["query"])
        assert query["view"] == "observations"
        if query.get("dimensions"):
            return httpx.Response(200, json=_model_payload([
                {"providedModelName": "claude-sonnet-5", "sum_inputTokens": 100,
                 "sum_outputTokens": 20, "sum_totalTokens": 120, "count_count": 5},
                {"providedModelName": None, "sum_inputTokens": 0,
                 "sum_outputTokens": 0, "sum_totalTokens": 0, "count_count": 3},
            ]))
        # user axis: one filter on userId
        flt = query["filters"][0]
        assert flt["column"] == "userId"
        per_user = {
            "alice@ex.com": {"sum_inputTokens": 40, "sum_outputTokens": 10,
                             "sum_totalTokens": 50, "count_count": 2},
            "bob@ex.com": {"sum_inputTokens": 0, "sum_outputTokens": 0,
                           "sum_totalTokens": 0, "count_count": 0},
        }
        return httpx.Response(200, json=_model_payload([per_user[flt["value"]]]))

    with _sm() as db:
        n = langfuse_metrics.sweep_langfuse_metrics(db, client=_mock_client(handler))

    # model: sonnet + unknown (null folded, count 3 keeps it despite 0 tokens).
    # user: alice only (bob had no activity, skipped).
    assert n == 3
    models = _rows(tenant_id, "model")
    assert set(models) == {"claude-sonnet-5", "unknown"}
    assert models["claude-sonnet-5"].input_tokens == 100
    assert models["claude-sonnet-5"].total_tokens == 120
    assert models["claude-sonnet-5"].observation_count == 5
    assert models["unknown"].total_tokens == 0
    assert models["unknown"].observation_count == 3
    # Cache columns are always 0 (no Metrics API measure).
    assert models["claude-sonnet-5"].cache_read_tokens == 0
    assert models["claude-sonnet-5"].cache_creation_tokens == 0

    users = _rows(tenant_id, "user")
    assert set(users) == {"alice@ex.com"}
    assert users["alice@ex.com"].total_tokens == 50
    assert users["alice@ex.com"].observation_count == 2
    assert users["alice@ex.com"].day == _DAY


# -- HTTP error isolation -----------------------------------------------------
def test_sweep_http_error_isolated(app_env, monkeypatch):
    tenant_id = _seed_tenant("alice@ex.com")
    _activate(monkeypatch, tenant_id)

    def handler(request):
        return httpx.Response(502, text="bad gateway")

    with _sm() as db:
        # No exception propagates; nothing written.
        n = langfuse_metrics.sweep_langfuse_metrics(db, client=_mock_client(handler))
    assert n == 0
    assert _rows(tenant_id, "model") == {}
    assert _rows(tenant_id, "user") == {}


# -- idempotent recompute-replace ---------------------------------------------
def test_sweep_idempotent_recompute_replace(app_env, monkeypatch):
    tenant_id = _seed_tenant()
    _activate(monkeypatch, tenant_id)
    total = {"v": 120}

    def handler(request):
        query = json.loads(request.url.params["query"])
        if query.get("dimensions"):
            return httpx.Response(200, json=_model_payload([
                {"providedModelName": "claude-sonnet-5", "sum_inputTokens": 100,
                 "sum_outputTokens": 20, "sum_totalTokens": total["v"], "count_count": 5},
            ]))
        return httpx.Response(200, json=_model_payload([]))

    with _sm() as db:
        assert langfuse_metrics.sweep_langfuse_metrics(db, client=_mock_client(handler)) == 1
    assert _rows(tenant_id, "model")["claude-sonnet-5"].total_tokens == 120

    # Second run with a changed total must REPLACE the row, not duplicate it.
    total["v"] = 999
    with _sm() as db:
        assert langfuse_metrics.sweep_langfuse_metrics(db, client=_mock_client(handler)) == 1
    models = _rows(tenant_id, "model")
    assert set(models) == {"claude-sonnet-5"}
    assert models["claude-sonnet-5"].total_tokens == 999
