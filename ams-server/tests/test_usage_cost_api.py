"""Usage-cost REST endpoint and account price CRUD (PR3).

The allocation math itself is pinned in test_usage_cost.py; what is checked here
is the wire: the account-first service result regrouped server-first, the month
parameter's validation, and that a price sent to the account API actually lands
on the row the allocation reads.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.db import get_sessionmaker
from app.models import Account

from tests.test_grpc_channel import _oauth_secret, _seed_tenant_account_server
from tests.test_rbac import _auth, _login, _make_admin, _make_tenant
from tests.test_usage_cost import _acc, _add_account, _add_server, _plant, _set_price, _yesterday_base

API = "/api/v1"


def _cost(client, tenant_id, month=None, **kwargs):
    url = f"{API}/tenants/{tenant_id}/usage/cost"
    if month is not None:
        url += f"?month={month}"
    return client.get(url, **kwargs)


def _month_of(base: datetime) -> str:
    return f"{base.year:04d}-{base.month:02d}"


def _plant_pair(tenant_id, server_id, base, account_id, pct):
    """Two ticks 300s apart, so the account is held-current on that server."""
    for offset in (3600, 3900):
        _plant(
            tenant_id,
            server_id,
            base + timedelta(seconds=offset),
            [_acc(account_id, current=True, positional=(pct, 0.0))],
        )


# -- response shape -----------------------------------------------------------
def test_cost_response_is_server_first(client, app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("api-uc-1@ex.com")
    _set_price(account_id, "100", "USD")
    base = _yesterday_base()
    _plant_pair(tenant_id, server_id, base, account_id, 50.0)

    r = _cost(client, tenant_id, _month_of(base))
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["month"] == _month_of(base)
    assert body["asOf"] is not None
    # Yesterday is inside the live tail until the rollup sweep seals it.
    assert body["isPartial"] is True
    assert len(body["servers"]) == 1

    server = body["servers"][0]
    assert server["serverId"] == str(server_id)
    assert server["name"] is not None
    assert server["costs"] == [{"currency": "USD", "amount": "100.00"}]
    # u = 50 held for the whole observed span -> mean utilization 50%.
    assert server["utilizationPct"] == "50.00"

    line = server["accounts"][0]
    assert line["accountId"] == str(account_id)
    assert line["email"] == "api-uc-1@ex.com"
    assert line["provider"] == "claude"
    assert line["monthlyPrice"] == "100.00"
    assert line["currency"] == "USD"
    assert line["basis"] == "held"
    assert line["sharePct"] == "100.00"
    assert line["cost"] == "100.00"

    assert body["subtotals"] == [
        {"currency": "USD", "allocatedCost": "100.00", "unallocatedCost": "0.00"}
    ]


def test_one_account_across_two_servers_splits_60_40(client, app_env):
    tenant_id, account_id, server_a = _seed_tenant_account_server("api-uc-2@ex.com")
    server_b = _add_server(tenant_id)
    _set_price(account_id, "110", "USD")
    base = _yesterday_base()
    _plant_pair(tenant_id, server_a, base, account_id, 60.0)
    _plant_pair(tenant_id, server_b, base, account_id, 40.0)

    body = _cost(client, tenant_id, _month_of(base)).json()
    costs = {s["serverId"]: s["costs"] for s in body["servers"]}
    assert costs[str(server_a)] == [{"currency": "USD", "amount": "66.00"}]
    assert costs[str(server_b)] == [{"currency": "USD", "amount": "44.00"}]
    # Most expensive server first.
    assert body["servers"][0]["serverId"] == str(server_a)
    assert body["subtotals"][0]["allocatedCost"] == "110.00"


def test_one_server_holding_two_currencies_keeps_them_apart(client, app_env):
    tenant_id, account_usd, server_id = _seed_tenant_account_server("api-uc-3usd@ex.com")
    _set_price(account_usd, "100", "USD")
    account_eur = _add_account(tenant_id, "api-uc-3eur@ex.com", "200", "EUR")
    base = _yesterday_base()
    # Both accounts ride in the same payload, as a real agent report does: two
    # snapshots for one server at the same instant would collapse to a zero-length
    # interval in the integrator.
    for offset in (3600, 3900):
        _plant(
            tenant_id,
            server_id,
            base + timedelta(seconds=offset),
            [
                _acc(account_usd, current=True, positional=(50.0, 0.0)),
                _acc(account_eur, current=True, positional=(50.0, 0.0)),
            ],
        )

    body = _cost(client, tenant_id, _month_of(base)).json()
    assert len(body["servers"]) == 1
    # Never summed into one number: one entry per currency, sorted by code.
    assert body["servers"][0]["costs"] == [
        {"currency": "EUR", "amount": "200.00"},
        {"currency": "USD", "amount": "100.00"},
    ]
    assert {s["currency"]: s["allocatedCost"] for s in body["subtotals"]} == {
        "EUR": "200.00",
        "USD": "100.00",
    }


def test_account_without_a_price_is_listed_at_zero(client, app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("api-uc-4@ex.com")
    _set_price(account_id, None)
    base = _yesterday_base()
    _plant_pair(tenant_id, server_id, base, account_id, 50.0)

    body = _cost(client, tenant_id, _month_of(base)).json()
    line = body["servers"][0]["accounts"][0]
    assert line["basis"] == "no_price"
    assert line["monthlyPrice"] is None
    assert line["cost"] == "0.00"
    assert body["servers"][0]["costs"] == []
    assert body["subtotals"] == []


# -- month parameter ----------------------------------------------------------
def test_month_defaults_to_the_current_utc_month(client, app_env):
    tenant_id, _account_id, _server_id = _seed_tenant_account_server("api-uc-5@ex.com")
    r = _cost(client, tenant_id)
    assert r.status_code == 200, r.text
    now = datetime.now(UTC)
    assert r.json()["month"] == f"{now.year:04d}-{now.month:02d}"


@pytest.mark.parametrize(
    "month", ["2026-13", "2026-00", "2026-1", "202603", "2026-3x", "not-a-month", "0000-01", ""]
)
def test_a_malformed_month_is_422(client, app_env, month):
    tenant_id, _a, _s = _seed_tenant_account_server(f"api-uc-m{uuid.uuid4().hex[:8]}@ex.com")
    assert _cost(client, tenant_id, month).status_code == 422


def test_a_future_month_is_an_empty_200(client, app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("api-uc-6@ex.com")
    _set_price(account_id, "100", "USD")
    base = _yesterday_base()
    _plant_pair(tenant_id, server_id, base, account_id, 50.0)

    future = datetime.now(UTC) + timedelta(days=400)
    body = _cost(client, tenant_id, f"{future.year:04d}-{future.month:02d}").json()
    assert body["servers"] == []
    assert body["subtotals"] == []


def test_a_foreign_tenant_cost_query_is_404(client, db, app_env):
    own = _make_tenant(client, "own-cost")
    other = _make_tenant(client, "other-cost")
    _make_admin(db, email="ta-cost@x.example.com", role="tenant-admin", tenant_id=own)
    token = _login(client, "ta-cost@x.example.com").json()["sessionToken"]

    assert _cost(client, own, "2026-03", headers=_auth(token)).status_code == 200
    foreign = _cost(client, other, "2026-03", headers=_auth(token))
    assert foreign.status_code == 404, foreign.text
    assert foreign.json()["code"] == "tenant.not_found"


# -- price CRUD round trip ----------------------------------------------------
def _db_price(account_id):
    with get_sessionmaker()() as db:
        row = db.get(Account, account_id)
        return row.monthly_price, row.currency


def _create(client, tenant_id, email, **extra):
    return client.post(
        f"{API}/tenants/{tenant_id}/accounts",
        json={
            "email": email,
            "credentialType": "oauth",
            "secret": _oauth_secret(email),
            **extra,
        },
    )


def test_create_account_stores_price_and_currency(client, app_env):
    tenant_id = _make_tenant(client, "price-create")
    r = _create(client, tenant_id, "p1@ex.com", monthlyPrice="29.00", currency="EUR")
    assert r.status_code == 201, r.text
    assert r.json()["monthlyPrice"] == "29.00"
    assert r.json()["currency"] == "EUR"
    assert _db_price(uuid.UUID(r.json()["id"])) == (Decimal("29.00"), "EUR")


def test_create_account_without_a_price_defaults_to_usd_and_null(client, app_env):
    tenant_id = _make_tenant(client, "price-default")
    r = _create(client, tenant_id, "p2@ex.com")
    assert r.status_code == 201, r.text
    assert r.json()["monthlyPrice"] is None
    assert r.json()["currency"] == "USD"
    assert _db_price(uuid.UUID(r.json()["id"])) == (None, "USD")


def test_patch_updates_clears_and_preserves_the_price(client, app_env):
    tenant_id = _make_tenant(client, "price-patch")
    account_id = _create(client, tenant_id, "p3@ex.com", monthlyPrice="20.00").json()["id"]
    url = f"{API}/tenants/{tenant_id}/accounts/{account_id}"

    changed = client.patch(url, json={"monthlyPrice": "35.50", "currency": "KRW"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["monthlyPrice"] == "35.50"
    assert _db_price(uuid.UUID(account_id)) == (Decimal("35.50"), "KRW")

    # An unrelated PATCH must not disturb the price (omitted != null).
    kept = client.patch(url, json={"owner": "platform-team"})
    assert kept.json()["monthlyPrice"] == "35.50"
    assert kept.json()["currency"] == "KRW"
    assert kept.json()["owner"] == "platform-team"

    # An explicit null clears it — a valid state meaning "no price recorded".
    cleared = client.patch(url, json={"monthlyPrice": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["monthlyPrice"] is None
    assert _db_price(uuid.UUID(account_id)) == (None, "KRW")


def test_a_negative_price_is_rejected(client, app_env):
    tenant_id = _make_tenant(client, "price-negative")
    r = _create(client, tenant_id, "p4@ex.com", monthlyPrice="-1.00")
    assert r.status_code == 422


def test_a_price_set_through_the_api_reaches_the_allocation(client, app_env):
    """The end-to-end claim: what the console types is what the cost query spends."""
    tenant_id, account_id, server_id = _seed_tenant_account_server("api-uc-7@ex.com")
    r = client.patch(
        f"{API}/tenants/{tenant_id}/accounts/{account_id}",
        json={"monthlyPrice": "42.00", "currency": "USD"},
    )
    assert r.status_code == 200, r.text
    base = _yesterday_base()
    _plant_pair(tenant_id, server_id, base, account_id, 50.0)

    body = _cost(client, tenant_id, _month_of(base)).json()
    assert body["servers"][0]["costs"] == [{"currency": "USD", "amount": "42.00"}]
