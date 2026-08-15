"""F5 billing_events outbox — sweep aggregation + REST (design note p5 §6).

Plants ``usage_snapshots`` rows directly (the ledger) and drives the real
``services.billing.sweep_billing`` against the same PostgreSQL the rest of the
suite uses, then exercises the REST list/export surface through the client.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import ApiError
from app.db import get_sessionmaker
from app.models import BillingCursor, BillingEvent, Tenant, UsageSnapshot
from app.services import billing, inventory

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


class _RecordCollector(logging.Handler):
    """Captures ams.billing log messages directly, independent of caplog quirks."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[tuple[int, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append((record.levelno, record.getMessage()))


def _capture_billing_logs():
    # app_env runs alembic in-process, and alembic's fileConfig sets
    # disable_existing_loggers, which flips ams.billing's ``disabled`` flag.
    # Clear it (and restore) so the warning logic is observable (matches the
    # ams.grpc capture in test_kek_wrap.py).
    logger = logging.getLogger("ams.billing")
    handler = _RecordCollector()
    logger.addHandler(handler)
    prev_level, prev_disabled = logger.level, logger.disabled
    logger.setLevel(logging.DEBUG)
    logger.disabled = False

    def restore():
        logger.removeHandler(handler)
        logger.setLevel(prev_level)
        logger.disabled = prev_disabled

    return handler, restore


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


# -- helpers for G25/G26 ------------------------------------------------------
def _drop_inventory(tenant_id, account_id, server_id):
    """Delete a tenant's account + server so only ledger guards remain."""
    with get_sessionmaker()() as db:
        inventory.delete_account(db, tenant_id, account_id)
        inventory.delete_server(db, tenant_id, server_id)


def _by_kind(events):
    out: dict = {}
    for e in events:
        out.setdefault(e.kind, []).append(e)
    return out


# -- G25: tenant delete blocked while pending billing exists ------------------
def test_delete_tenant_blocked_by_pending_billing(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("g25-a@ex.com")
    _plant(tenant_id, server_id, _closed_day(), _usage_payload(active_ids=["a1"]))
    assert _sweep() == 1  # one pending billing_event
    _drop_inventory(tenant_id, account_id, server_id)

    with get_sessionmaker()() as db:
        with pytest.raises(ApiError) as ei:
            inventory.delete_tenant(db, tenant_id)
    assert ei.value.status == 409
    assert ei.value.code == "tenant.has_pending_billing"

    # The tenant and its pending ledger survive the refused delete.
    with get_sessionmaker()() as db:
        assert db.get(Tenant, tenant_id) is not None
    assert len(_events(tenant_id)) == 1


# -- G25: exported-only ledger no longer blocks delete ------------------------
def test_delete_tenant_allowed_after_export(app_env, client):
    tenant_id, account_id, server_id = _seed_tenant_account_server("g25-b@ex.com")
    _plant(tenant_id, server_id, _closed_day(), _usage_payload(active_ids=["a1"]))
    assert _sweep() == 1
    event_id = _events(tenant_id)[0].id
    exported = client.post(
        f"/api/v1/tenants/{tenant_id}/billing/events/{event_id}/export"
    )
    assert exported.status_code == 200
    _drop_inventory(tenant_id, account_id, server_id)

    with get_sessionmaker()() as db:
        inventory.delete_tenant(db, tenant_id)  # exported-only ledger: allowed
    with get_sessionmaker()() as db:
        assert db.get(Tenant, tenant_id) is None
    # billing_events cascaded away with the tenant.
    assert len(_events(tenant_id)) == 0


# -- G25: a tenant with no billing at all deletes cleanly ---------------------
def test_delete_tenant_with_no_billing(app_env):
    with get_sessionmaker()() as db:
        tenant = inventory.create_tenant(db, "g25-c-" + uuid.uuid4().hex[:8])
        tenant_id = tenant.id
    with get_sessionmaker()() as db:
        inventory.delete_tenant(db, tenant_id)
    with get_sessionmaker()() as db:
        assert db.get(Tenant, tenant_id) is None


# -- G26: void reverses an exported event and re-aggregates the day -----------
def test_void_reaggregates_and_preserves_original(app_env, client):
    tenant_id, _acc, server_id = _seed_tenant_account_server("g26-a@ex.com")
    day = _closed_day()
    _plant(tenant_id, server_id, day, _usage_payload(active_ids=["a1"]))
    assert _sweep() == 1
    original = _events(tenant_id)[0]
    original_id = original.id
    assert original.payload["account_days"] == 1

    exported = client.post(
        f"/api/v1/tenants/{tenant_id}/billing/events/{original_id}/export"
    )
    assert exported.status_code == 200

    # A late snapshot for the same day lands after the watermark passed.
    _plant(
        tenant_id, server_id, day.replace(hour=20),
        _usage_payload(active_ids=["a1", "a2"]),
    )

    voided = client.post(
        f"/api/v1/tenants/{tenant_id}/billing/events/{original_id}/void"
    )
    assert voided.status_code == 200
    body = voided.json()
    assert body["kind"] == "usage_daily_void"
    assert body["status"] == "pending"
    # payload is an opaque JSONB dict — its keys are not camelCased on the wire.
    assert body["payload"]["voids_event_id"] == str(original_id)
    assert body["payload"]["voided_payload"]["account_days"] == 1

    by_kind = _by_kind(_events(tenant_id))
    assert set(by_kind) == {"usage_daily", "usage_daily_void", "usage_daily_reagg"}
    # Original row is immutable: still exported, unchanged aggregate.
    orig = by_kind["usage_daily"][0]
    assert orig.id == original_id
    assert orig.status == "exported"
    assert orig.payload["account_days"] == 1
    # Re-aggregation picks up the late snapshot.
    reagg = by_kind["usage_daily_reagg"][0]
    assert reagg.status == "pending"
    assert reagg.payload["account_days"] == 2
    assert reagg.payload["account_ids"] == ["a1", "a2"]
    # Net over the day = original - void + reagg = corrected truth (2).
    void_ev = by_kind["usage_daily_void"][0]
    net = (
        orig.payload["account_days"]
        - void_ev.payload["voided_payload"]["account_days"]
        + reagg.payload["account_days"]
    )
    assert net == 2

    # Double void of the same original is an idempotent no-op (200).
    again = client.post(
        f"/api/v1/tenants/{tenant_id}/billing/events/{original_id}/void"
    )
    assert again.status_code == 200
    assert again.json()["kind"] == "usage_daily_void"
    assert len(_events(tenant_id)) == 3  # no duplicate void/reagg rows

    # Sweep idempotency invariant holds: the voided day is behind the watermark.
    assert _sweep() == 0
    assert len(_events(tenant_id)) == 3


# -- G26: a pending (un-exported) event cannot be voided ----------------------
def test_void_pending_rejected(app_env, client):
    tenant_id, _acc, server_id = _seed_tenant_account_server("g26-b@ex.com")
    _plant(tenant_id, server_id, _closed_day(), _usage_payload(active_ids=["a1"]))
    assert _sweep() == 1
    event_id = _events(tenant_id)[0].id

    denied = client.post(
        f"/api/v1/tenants/{tenant_id}/billing/events/{event_id}/void"
    )
    assert denied.status_code == 409
    assert denied.json()["code"] == "billing.void_requires_exported"
    assert len(_events(tenant_id)) == 1  # nothing created


# -- G26: cross-tenant void is a 404, not a leak ------------------------------
def test_void_cross_tenant_404(app_env, client):
    tenant_a, _a, server_a = _seed_tenant_account_server("g26-cA@ex.com")
    tenant_b, _b, _server_b = _seed_tenant_account_server("g26-cB@ex.com")
    _plant(tenant_a, server_a, _closed_day(), _usage_payload(active_ids=["a1"]))
    assert _sweep() == 1
    event_a = _events(tenant_a)[0].id
    client.post(f"/api/v1/tenants/{tenant_a}/billing/events/{event_a}/export")

    denied = client.post(
        f"/api/v1/tenants/{tenant_b}/billing/events/{event_a}/void"
    )
    assert denied.status_code == 404
    # The event under its real tenant is untouched by the cross-tenant attempt.
    assert len(_events(tenant_a)) == 1


# -- G27: clock-forward-jump silent non-billing is surfaced -------------------
def _set_watermark(watermark: datetime) -> None:
    with get_sessionmaker()() as db:
        db.add(
            BillingCursor(kind="usage_daily", watermark=watermark, updated_at=datetime.now(UTC))
        )
        db.commit()


def test_below_watermark_snapshot_warns(app_env):
    """A forward wall-clock step parks the watermark in the future; usage that
    lands with reported_at below it is skipped forever and must be logged."""
    tenant_id, _acc, server_id = _seed_tenant_account_server("g27-a@ex.com")
    # Simulate the jump: watermark already 10 days ahead of real time.
    future = billing._floor_day(datetime.now(UTC)) + timedelta(days=10)
    _set_watermark(future)
    # A real report now lands below that watermark (but at/after last_closed_end).
    _plant(tenant_id, server_id, datetime.now(UTC), _usage_payload(active_ids=["a1"]))

    handler, restore = _capture_billing_logs()
    try:
        created = _sweep()
    finally:
        restore()

    assert created == 0  # watermark is ahead of last_closed_end: nothing billed
    warnings = [m for lvl, m in handler.messages if lvl == logging.WARNING]
    assert any("below the watermark" in m for m in warnings), warnings
    assert any("min reported_at=" in m for m in warnings)


def test_large_watermark_advance_warns(app_env):
    """An abnormally large single-run advance (outage recovery / clock step) is
    surfaced, but only once a watermark already exists."""
    tenant_id, _acc, server_id = _seed_tenant_account_server("g27-b@ex.com")
    # Watermark 5 days behind last_closed_end -> one run jumps >2 days forward.
    behind = billing._floor_day(datetime.now(UTC)) - timedelta(days=5)
    _set_watermark(behind)
    _plant(tenant_id, server_id, _closed_day(days_ago=4), _usage_payload(active_ids=["a1"]))

    handler, restore = _capture_billing_logs()
    try:
        _sweep()
    finally:
        restore()

    warnings = [m for lvl, m in handler.messages if lvl == logging.WARNING]
    assert any("watermark advancing" in m for m in warnings), warnings


def test_normal_first_sweep_is_quiet(app_env):
    """A first run (no cursor) backfilling a few days must not warn on advance,
    and a healthy sweep emits no below-watermark warning."""
    tenant_id, _acc, server_id = _seed_tenant_account_server("g27-c@ex.com")
    _plant(tenant_id, server_id, _closed_day(days_ago=3), _usage_payload(active_ids=["a1"]))

    handler, restore = _capture_billing_logs()
    try:
        assert _sweep() == 1
    finally:
        restore()

    warnings = [m for lvl, m in handler.messages if lvl == logging.WARNING]
    assert warnings == []


# -- G28: multi-day span chunked per-day yields identical results -------------
def test_multiday_chunked_sweep(app_env):
    """Three distinct closed days are aggregated into one event per (tenant, day)
    by the day-at-a-time loader, with per-day payloads intact."""
    tenant_id, _acc, server_id = _seed_tenant_account_server("g28-a@ex.com")
    _plant(tenant_id, server_id, _closed_day(days_ago=3), _usage_payload(active_ids=["d3a", "d3b"]))
    _plant(tenant_id, server_id, _closed_day(days_ago=4), _usage_payload(active_ids=["d4a"]))
    _plant(tenant_id, server_id, _closed_day(days_ago=5), _usage_payload(active_ids=["d5a"], max_util=42.0))

    assert _sweep() == 3
    events = _events(tenant_id)
    assert len(events) == 3
    by_start = {billing._floor_day(e.period_start.astimezone(UTC)): e for e in events}
    d3 = by_start[billing._floor_day(_closed_day(days_ago=3))]
    d4 = by_start[billing._floor_day(_closed_day(days_ago=4))]
    d5 = by_start[billing._floor_day(_closed_day(days_ago=5))]
    assert d3.payload["account_ids"] == ["d3a", "d3b"]
    assert d3.payload["snapshot_count"] == 1
    assert d4.payload["account_ids"] == ["d4a"]
    assert d5.payload["account_ids"] == ["d5a"]
    assert d5.payload["max_utilization_pct"] == 42.0
    # Each event spans exactly its own UTC day.
    for e in events:
        assert e.period_end - e.period_start == timedelta(days=1)

    # Idempotent re-run over the same multi-day span creates nothing new.
    assert _sweep() == 0
    assert len(_events(tenant_id)) == 3


# -- G27 self-heal: parked-future watermark rewinds and reheals ---------------
def _watermark() -> datetime | None:
    with get_sessionmaker()() as db:
        cur = db.get(BillingCursor, "usage_daily")
        return cur.watermark.astimezone(UTC) if cur is not None else None


def _last_closed_end() -> datetime:
    from app.config import get_settings

    grace = get_settings().billing_close_grace_seconds
    return billing._floor_day(datetime.now(UTC) - timedelta(seconds=grace))


def test_future_watermark_rewinds_to_last_closed_end(app_env):
    """A watermark parked in the future is rewound to last_closed_end on sweep,
    so the early return can never leave the cursor stranded ahead of real time."""
    tenant_id, _acc, server_id = _seed_tenant_account_server("g27-rw@ex.com")
    future = billing._floor_day(datetime.now(UTC)) + timedelta(days=10)
    _set_watermark(future)
    _plant(tenant_id, server_id, _closed_day(), _usage_payload(active_ids=["r1"]))

    handler, restore = _capture_billing_logs()
    try:
        assert _sweep() == 0  # this tick idles
    finally:
        restore()

    assert _watermark() == _last_closed_end()  # rewound out of the future
    warnings = [m for lvl, m in handler.messages if lvl == logging.WARNING]
    assert any("rewound" in m for m in warnings), warnings


def test_rewound_watermark_reheals_reopened_day(app_env, monkeypatch):
    """The rewind unfreezes forward progress: the day that was blocked by the
    parked cursor is billed exactly once as soon as it closes (orphan resolved),
    and a further sweep adds nothing (ON CONFLICT anchor). ``_now`` is pinned so
    the "next tick" deterministically lands on the following, now-closed day."""
    tenant_id, _acc, server_id = _seed_tenant_account_server("g27-rh@ex.com")
    t1 = datetime(2026, 3, 10, 23, 0, tzinfo=UTC)
    monkeypatch.setattr(billing, "_now", lambda: t1)
    _set_watermark(billing._floor_day(t1) + timedelta(days=10))  # forward jump
    _plant(tenant_id, server_id, datetime(2026, 3, 10, 12, 0, tzinfo=UTC),
           _usage_payload(active_ids=["h1"]))

    assert _sweep() == 0  # rewind tick, idles
    assert _watermark() == billing._floor_day(t1)  # rewound to 2026-03-10

    monkeypatch.setattr(billing, "_now", lambda: t1 + timedelta(days=1))
    assert _sweep() == 1  # 03-10 has now closed -> the reopened day is billed
    assert len(_events(tenant_id)) == 1
    assert _sweep() == 0  # idempotent: no double-bill
    assert len(_events(tenant_id)) == 1


def test_monotonic_future_does_not_flap(app_env):
    """A watermark that only ever moves forward with real time (no backward
    clock correction) must not re-trigger a rewind on the following tick."""
    tenant_id, _acc, server_id = _seed_tenant_account_server("g27-fl@ex.com")
    _plant(tenant_id, server_id, _closed_day(), _usage_payload(active_ids=["f1"]))
    assert _sweep() == 1  # normal advance to last_closed_end
    wm_after = _watermark()

    handler, restore = _capture_billing_logs()
    try:
        assert _sweep() == 0  # nothing new
    finally:
        restore()

    assert _watermark() == wm_after  # unchanged
    warnings = [m for lvl, m in handler.messages if lvl == logging.WARNING]
    assert not any("rewound" in m for m in warnings), warnings


def test_rewind_clamps_up_to_purge_cursor(app_env):
    """The rewind target is clamped UP to the retention purge cursor, so a rewind
    can never drop below already-purged data and re-bill a day from its survivors."""
    tenant_id, _acc, server_id = _seed_tenant_account_server("g27-clamp@ex.com")
    purge = _last_closed_end() + timedelta(days=1)  # purge reached into a not-yet-closed day
    with get_sessionmaker()() as db:
        db.merge(BillingCursor(kind=billing.PURGE_KIND, watermark=purge,
                               updated_at=datetime.now(UTC)))
        db.commit()
    _set_watermark(billing._floor_day(datetime.now(UTC)) + timedelta(days=10))  # forward jump
    _plant(tenant_id, server_id, _closed_day(), _usage_payload(active_ids=["c1"]))

    assert _sweep() == 0  # rewind tick
    assert _watermark() == purge  # clamped up to the purge cursor, not last_closed_end
