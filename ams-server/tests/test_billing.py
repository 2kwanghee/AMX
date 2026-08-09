"""F5 billing_events outbox — sweep aggregation + REST (design note p5 §6).

Plants ``usage_snapshots`` rows directly (the ledger) and drives the real
``services.billing.sweep_billing`` against the same PostgreSQL the rest of the
suite uses, then exercises the REST list/export surface through the client.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.db import get_sessionmaker
from app.models import BillingCursor, BillingEvent, UsageSnapshot
from app.services import billing

from tests.test_grpc_channel import _seed_tenant_account_server


# -- helpers ------------------------------------------------------------------
def _closed_day(days_ago: int = 3, hour: int = 12) -> datetime:
    """A UTC instant well inside a fully-closed day (past the grace window)."""
    d = datetime.now(UTC) - timedelta(days=days_ago)
    return d.replace(hour=hour, minute=0, second=0, microsecond=0)


def _usage_payload(active_ids=(), absent_ids=(), max_util=None) -> dict:
    """Mirror MessageToDict(preserving_proto_field_name=True) of a UsageReport."""
    accounts = [
        {"account": {"ams_account_id": aid}, "allocation_status": "ALLOCATION_STATUS_ACTIVE"}
        for aid in active_ids
    ] + [
        {"account": {"ams_account_id": aid}, "allocation_status": "ALLOCATION_STATUS_ABSENT"}
        for aid in absent_ids
    ]
    payload: dict = {"accounts": accounts}
    if max_util is not None:
        payload["pool_summary"] = {"max_utilization_pct": max_util}
    return payload


def _plant(tenant_id, server_id, reported_at, payload, report_type="usage"):
    with get_sessionmaker()() as db:
        db.add(
            UsageSnapshot(
                tenant_id=tenant_id,
                server_id=server_id,
                account_id=None,
                report_type=report_type,
                payload=payload,
                reported_at=reported_at,
            )
        )
        db.commit()


def _sweep() -> int:
    with get_sessionmaker()() as db:
        return billing.sweep_billing(db)


def _events(tenant_id):
    with get_sessionmaker()() as db:
        from sqlalchemy import select

        return db.scalars(
            select(BillingEvent).where(BillingEvent.tenant_id == tenant_id)
        ).all()


# -- (a) idempotency: two sweeps, no duplicates -------------------------------
def test_sweep_is_idempotent(app_env):
    tenant_id, _acc, server_id = _seed_tenant_account_server("bill-a@ex.com")
    _plant(tenant_id, server_id, _closed_day(), _usage_payload(active_ids=["acc-1"]))

    first = _sweep()
    assert first == 1
    second = _sweep()
    assert second == 0
    assert len(_events(tenant_id)) == 1


# -- (b) today's day is not yet closed ----------------------------------------
def test_open_day_is_not_billed(app_env):
    tenant_id, _acc, server_id = _seed_tenant_account_server("bill-b@ex.com")
    _plant(tenant_id, server_id, datetime.now(UTC), _usage_payload(active_ids=["acc-1"]))

    assert _sweep() == 0
    assert len(_events(tenant_id)) == 0


# -- (c) absent excluded, account_days correctness ----------------------------
def test_absent_accounts_excluded(app_env):
    tenant_id, _acc, server_id = _seed_tenant_account_server("bill-c@ex.com")
    day = _closed_day()
    _plant(
        tenant_id, server_id, day,
        _usage_payload(active_ids=["a1", "a2"], absent_ids=["a3"], max_util=40.0),
    )
    # A second snapshot the same day repeats a1 (distinct) and adds a4.
    _plant(
        tenant_id, server_id, day.replace(hour=18),
        _usage_payload(active_ids=["a1", "a4"], max_util=55.5),
    )

    assert _sweep() == 1
    events = _events(tenant_id)
    assert len(events) == 1
    payload = events[0].payload
    assert payload["account_days"] == 3
    assert payload["account_ids"] == ["a1", "a2", "a4"]
    assert payload["server_count"] == 1
    assert payload["snapshot_count"] == 2
    assert payload["max_utilization_pct"] == 55.5


# -- (d) tenant isolation (GET + cross-tenant export 404) ----------------------
def test_tenant_isolation(app_env, client):
    tenant_a, _a, server_a = _seed_tenant_account_server("bill-dA@ex.com")
    tenant_b, _b, server_b = _seed_tenant_account_server("bill-dB@ex.com")
    _plant(tenant_a, server_a, _closed_day(), _usage_payload(active_ids=["a1"]))
    _plant(tenant_b, server_b, _closed_day(), _usage_payload(active_ids=["b1"]))
    assert _sweep() == 2

    body = client.get(f"/api/v1/tenants/{tenant_a}/billing/events").json()
    assert body["pageInfo"]["totalSize"] == 1
    assert body["items"][0]["tenantId"] == str(tenant_a)

    event_b = _events(tenant_b)[0]
    denied = client.post(
        f"/api/v1/tenants/{tenant_a}/billing/events/{event_b.id}/export"
    )
    assert denied.status_code == 404
    # A random id under a real tenant is also 404.
    missing = client.post(
        f"/api/v1/tenants/{tenant_a}/billing/events/{uuid.uuid4()}/export"
    )
    assert missing.status_code == 404


# -- (e) export is idempotent -------------------------------------------------
def test_export_is_idempotent(app_env, client):
    tenant_id, _acc, server_id = _seed_tenant_account_server("bill-e@ex.com")
    _plant(tenant_id, server_id, _closed_day(), _usage_payload(active_ids=["a1"]))
    assert _sweep() == 1
    event_id = _events(tenant_id)[0].id

    first = client.post(f"/api/v1/tenants/{tenant_id}/billing/events/{event_id}/export")
    assert first.status_code == 200
    assert first.json()["status"] == "exported"
    exported_at = first.json()["exportedAt"]
    assert exported_at is not None

    second = client.post(f"/api/v1/tenants/{tenant_id}/billing/events/{event_id}/export")
    assert second.status_code == 200
    assert second.json()["status"] == "exported"
    assert second.json()["exportedAt"] == exported_at

    # status filter narrows the list.
    listed = client.get(
        f"/api/v1/tenants/{tenant_id}/billing/events?status=exported"
    ).json()
    assert listed["pageInfo"]["totalSize"] == 1
    assert (
        client.get(f"/api/v1/tenants/{tenant_id}/billing/events?status=pending").json()[
            "pageInfo"
        ]["totalSize"]
        == 0
    )


# -- (f) watermark advances ---------------------------------------------------
def test_watermark_advances(app_env):
    tenant_id, _acc, server_id = _seed_tenant_account_server("bill-f@ex.com")
    _plant(tenant_id, server_id, _closed_day(days_ago=3), _usage_payload(active_ids=["a1"]))
    assert _sweep() == 1

    with get_sessionmaker()() as db:
        cursor = db.get(BillingCursor, "usage_daily")
        assert cursor is not None
        expected = billing._floor_day(
            datetime.now(UTC) - timedelta(seconds=billing.get_settings().billing_close_grace_seconds)
        )
        assert cursor.watermark.astimezone(UTC) == expected

    # A late snapshot inside an already-swept day is not re-billed.
    _plant(tenant_id, server_id, _closed_day(days_ago=3, hour=20), _usage_payload(active_ids=["a2"]))
    assert _sweep() == 0
    assert len(_events(tenant_id)) == 1


# -- (g) malformed payload rows are skipped -----------------------------------
def test_malformed_rows_skipped(app_env):
    tenant_id, _acc, server_id = _seed_tenant_account_server("bill-g@ex.com")
    day = _closed_day()
    _plant(tenant_id, server_id, day, _usage_payload(active_ids=["a1"], max_util=10.0))
    # payload is a JSON array, not an object — defensively skipped, not counted.
    _plant(tenant_id, server_id, day.replace(hour=13), ["not", "an", "object"])

    assert _sweep() == 1
    payload = _events(tenant_id)[0].payload
    assert payload["snapshot_count"] == 1
    assert payload["account_days"] == 1
    assert payload["account_ids"] == ["a1"]
