"""Langfuse usage REST endpoint — GET /tenants/{id}/usage/langfuse (P4 console).

The sweep math is pinned in test_langfuse_metrics.py; what is checked here is the
wire: roll-up rows seeded into the table, regrouped into model/user lists, the
inclusive day-range filter, the uiUrl fallback, and that another tenant's query is
empty (the sweep only ever writes the configured tenant's rows).
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date

from app.config import get_settings as _real_get_settings
from app.db import get_sessionmaker
from app.models import LangfuseUsageRollup
from app.services import inventory, langfuse_metrics

API = "/api/v1"


def _sm():
    return get_sessionmaker()()


def _tenant() -> uuid.UUID:
    with _sm() as db:
        return inventory.create_tenant(db, "lf-" + uuid.uuid4().hex[:8]).id


def _seed(tenant_id, day, dimension, key, **cols):
    with _sm() as db:
        db.add(LangfuseUsageRollup(
            tenant_id=tenant_id, day=day, dimension=dimension, key=key, **cols
        ))
        db.commit()


def _get(client, tenant_id, frm=None, to=None):
    url = f"{API}/tenants/{tenant_id}/usage/langfuse"
    q = []
    if frm:
        q.append(f"from={frm}")
    if to:
        q.append(f"to={to}")
    if q:
        url += "?" + "&".join(q)
    return client.get(url)


def test_response_splits_model_and_user_rows(client, app_env):
    tenant_id = _tenant()
    day = date(2026, 8, 14)
    _seed(tenant_id, day, "model", "claude-sonnet-5",
          input_tokens=100, output_tokens=20, total_tokens=120, observation_count=5)
    _seed(tenant_id, day, "model", "unknown", observation_count=3)
    _seed(tenant_id, day, "user", "alice@ex.com", total_tokens=50, observation_count=2)

    r = _get(client, tenant_id, "2026-08-14", "2026-08-14")
    assert r.status_code == 200, r.text
    body = r.json()

    assert {m["model"] for m in body["modelRows"]} == {"claude-sonnet-5", "unknown"}
    sonnet = next(m for m in body["modelRows"] if m["model"] == "claude-sonnet-5")
    assert sonnet["inputTokens"] == 100
    assert sonnet["totalTokens"] == 120
    assert sonnet["observations"] == 5
    assert sonnet["cacheReadTokens"] == 0
    assert sonnet["cacheCreationTokens"] == 0

    assert len(body["userRows"]) == 1
    assert body["userRows"][0]["userId"] == "alice@ex.com"
    assert body["userRows"][0]["totalTokens"] == 50
    # No AMX_LANGFUSE_* configured in the test env -> uiUrl null.
    assert body["uiUrl"] is None


def test_range_filter_is_inclusive(client, app_env):
    tenant_id = _tenant()
    _seed(tenant_id, date(2026, 8, 10), "model", "m", total_tokens=1)
    _seed(tenant_id, date(2026, 8, 12), "model", "m", total_tokens=2)
    _seed(tenant_id, date(2026, 8, 15), "model", "m", total_tokens=3)

    r = _get(client, tenant_id, "2026-08-11", "2026-08-12")
    assert r.status_code == 200
    days = [m["day"] for m in r.json()["modelRows"]]
    assert days == ["2026-08-12"]


def test_other_tenant_is_empty(client, app_env):
    tenant_id = _tenant()
    other_id = _tenant()
    _seed(tenant_id, date(2026, 8, 14), "model", "m", total_tokens=9)

    r = _get(client, other_id, "2026-08-14", "2026-08-14")
    assert r.status_code == 200
    assert r.json()["modelRows"] == []
    assert r.json()["userRows"] == []


def test_ui_url_falls_back_to_base_url(client, app_env, monkeypatch):
    tenant_id = _tenant()
    monkeypatch.setattr(
        langfuse_metrics, "get_settings",
        lambda: replace(_real_get_settings(), langfuse_ui_url=None,
                        langfuse_base_url="http://lf.example"),
    )
    r = _get(client, tenant_id)
    assert r.status_code == 200
    assert r.json()["uiUrl"] == "http://lf.example"
