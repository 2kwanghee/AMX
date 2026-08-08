"""P4 Track A: the alert backend (design note §4, decision 3).

Drives the real service/servicer primitives against the same PostgreSQL the rest
of the suite uses:

* event triggers via ``ControlPlaneServicer._store_event`` (all_exhausted /
  quarantine),
* reconcile-on-report triggers + auto-resolve via ``._store_usage``,
* the last_seen_at sweeper via ``alerts.sweep_offline``,
* ack + cross-tenant isolation via the REST client.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db import get_sessionmaker
from app.grpc import signing
from app.grpc.proto import pb
from app.grpc.server import ControlPlaneServicer
from app.models import Alert, Server
from app.services import alerts, inventory

from tests.test_grpc_channel import (
    AGENT_ID,
    _create_assignment,
    _seed_tenant_account_server,
)


# -- helpers ------------------------------------------------------------------
def _servicer() -> ControlPlaneServicer:
    return ControlPlaneServicer(
        signing.Signer.from_env_or_generate(), session_factory=get_sessionmaker()
    )


def _set_state(tenant_id, assignment_id, state):
    with get_sessionmaker()() as db:
        a = inventory.get_assignment(db, tenant_id, assignment_id)
        a.state = state
        db.commit()


def _open_alerts(server_id, kind=None):
    with get_sessionmaker()() as db:
        where = [Alert.server_id == server_id, Alert.status == "open"]
        if kind:
            where.append(Alert.kind == kind)
        return db.scalars(select(Alert).where(*where)).all()


def _all_alerts(server_id):
    with get_sessionmaker()() as db:
        return db.scalars(select(Alert).where(Alert.server_id == server_id)).all()


def _exhausted_event() -> pb.AccountEvent:
    event = pb.AccountEvent(
        schema_version=1,
        agent_id=AGENT_ID,
        event_id="evt_" + uuid.uuid4().hex,
        kind=pb.AccountEvent.KIND_ALL_EXHAUSTED,
        trigger=pb.AccountEvent.TRIGGER_AT_LIMIT,
    )
    event.pool_summary.all_exhausted = True
    event.pool_summary.max_utilization_pct = 100.0
    return event


def _usage_report(all_exhausted: bool, accounts: dict[str, int] | None = None) -> pb.UsageReport:
    report = pb.UsageReport(schema_version=1, agent_id=AGENT_ID)
    report.pool_summary.all_exhausted = all_exhausted
    for ams_account_id, status in (accounts or {}).items():
        au = report.accounts.add()
        au.account.ams_account_id = ams_account_id
        au.allocation_status = status
    return report


# -- event triggers -----------------------------------------------------------
def test_all_exhausted_event_opens_critical_alert(app_env):
    tenant_id, _a, server_id = _seed_tenant_account_server("exh@ex.com")
    _servicer()._store_event(server_id, tenant_id, _exhausted_event())

    alerts_open = _open_alerts(server_id, "all_exhausted")
    assert len(alerts_open) == 1
    a = alerts_open[0]
    assert a.severity == "critical"
    assert a.tenant_id == tenant_id
    assert a.account_id is None
    assert a.source_snapshot_id is not None
    # The switch_event is still persisted for the timeline.
    with get_sessionmaker()() as db:
        assert inventory.list_switch_events(db, tenant_id, server_id, limit=10, offset=0)[1] == 1


def test_quarantine_event_opens_warning_alert_scoped_to_account(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("quar@ex.com")
    event = pb.AccountEvent(
        schema_version=1,
        agent_id=AGENT_ID,
        event_id="evt_" + uuid.uuid4().hex,
        kind=pb.AccountEvent.KIND_QUARANTINE,
        trigger=pb.AccountEvent.TRIGGER_FAILOVER,
    )
    # Quarantine leaves `to` unset; the quarantined account rides in `from`.
    getattr(event, "from").ams_account_id = str(account_id)
    _servicer()._store_event(server_id, tenant_id, event)

    alerts_open = _open_alerts(server_id, "quarantine")
    assert len(alerts_open) == 1
    assert alerts_open[0].severity == "warning"
    assert alerts_open[0].account_id == account_id


def test_all_exhausted_event_dedupes_to_single_open(app_env):
    tenant_id, _a, server_id = _seed_tenant_account_server("dedupe@ex.com")
    svc = _servicer()
    svc._store_event(server_id, tenant_id, _exhausted_event())
    svc._store_event(server_id, tenant_id, _exhausted_event())
    svc._store_event(server_id, tenant_id, _exhausted_event())

    assert len(_open_alerts(server_id, "all_exhausted")) == 1


# -- reconcile-on-report triggers + auto-resolve ------------------------------
def test_report_opens_all_exhausted_and_drift_then_autoresolves(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("rep@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, assignment_id, "active")
    svc = _servicer()

    # Report 1: all accounts exhausted + the active account absent locally
    # (drift). Opens one critical all_exhausted + one warning drift alert.
    svc._store_usage(server_id, tenant_id, _usage_report(True), report_type="usage")
    assert len(_open_alerts(server_id, "all_exhausted")) == 1
    drift_open = _open_alerts(server_id, "drift")
    assert len(drift_open) == 1 and drift_open[0].account_id == account_id

    # Report 2: no longer exhausted, account now present active (drift cleared).
    svc._store_usage(
        server_id,
        tenant_id,
        _usage_report(False, {str(account_id): pb.ALLOCATION_STATUS_ACTIVE}),
        report_type="usage",
    )
    assert _open_alerts(server_id, "all_exhausted") == []
    assert _open_alerts(server_id, "drift") == []
    # Auto-resolve moves them to resolved, not delete.
    with get_sessionmaker()() as db:
        resolved = db.scalars(
            select(Alert).where(Alert.server_id == server_id, Alert.status == "resolved")
        ).all()
    assert len(resolved) == 2
    assert all(r.resolved_at is not None for r in resolved)


def test_report_drift_is_per_account_on_autoresolve(app_env):
    # Two drifting accounts; the next report clears only one. The other's alert
    # stays open (auto-resolve is per-account, not per-server).
    tenant_id, account_a, server_id = _seed_tenant_account_server("multi-a@ex.com")
    with get_sessionmaker()() as db:
        account_b = inventory.create_account(
            db, tenant_id, email="multi-b@ex.com", credential_type="api_key", secret="k"
        ).id
    assign_a = _create_assignment(tenant_id, account_a, server_id)
    assign_b = _create_assignment(tenant_id, account_b, server_id)
    _set_state(tenant_id, assign_a, "active")
    _set_state(tenant_id, assign_b, "active")
    svc = _servicer()

    svc._store_usage(server_id, tenant_id, _usage_report(False), report_type="usage")
    assert len(_open_alerts(server_id, "drift")) == 2

    # A now present, B still absent.
    svc._store_usage(
        server_id,
        tenant_id,
        _usage_report(False, {str(account_a): pb.ALLOCATION_STATUS_ACTIVE}),
        report_type="usage",
    )
    still_open = _open_alerts(server_id, "drift")
    assert len(still_open) == 1 and still_open[0].account_id == account_b


# -- offline: mark_offline + sweeper ------------------------------------------
def test_mark_offline_opens_alert_and_reconnect_resolves(app_env):
    tenant_id, _a, server_id = _seed_tenant_account_server("off@ex.com")
    svc = _servicer()
    svc._mark_offline(server_id)
    assert len(_open_alerts(server_id, "server_offline")) == 1

    # A heartbeat (reconnect) resolves it.
    svc._touch_last_seen(server_id)
    assert _open_alerts(server_id, "server_offline") == []
    with get_sessionmaker()() as db:
        assert db.get(Server, server_id).status == "online"


def test_sweeper_marks_stale_server_offline_and_alerts(app_env):
    tenant_id, _a, stale_id = _seed_tenant_account_server("stale@ex.com")
    _tenant2, _b, fresh_id = _seed_tenant_account_server("fresh@ex.com")
    now = datetime.now(UTC)
    with get_sessionmaker()() as db:
        stale = db.get(Server, stale_id)
        stale.status = "online"
        stale.last_seen_at = now - timedelta(seconds=200)
        fresh = db.get(Server, fresh_id)
        fresh.status = "online"
        fresh.last_seen_at = now - timedelta(seconds=5)
        db.commit()

    with get_sessionmaker()() as db:
        swept = alerts.sweep_offline(db, stale_after_seconds=90)
    assert swept == [stale_id]

    with get_sessionmaker()() as db:
        assert db.get(Server, stale_id).status == "offline"
        assert db.get(Server, fresh_id).status == "online"
    assert len(_open_alerts(stale_id, "server_offline")) == 1
    assert _open_alerts(fresh_id, "server_offline") == []


def test_sweeper_dedupes_and_reconnect_resolves(app_env):
    tenant_id, _a, server_id = _seed_tenant_account_server("sweepdedupe@ex.com")
    with get_sessionmaker()() as db:
        s = db.get(Server, server_id)
        s.status = "online"
        s.last_seen_at = datetime.now(UTC) - timedelta(seconds=200)
        db.commit()
    with get_sessionmaker()() as db:
        alerts.sweep_offline(db, stale_after_seconds=90)
    # A second sweep must not stack a second offline alert (server already
    # offline -> not selected; dedupe would catch it regardless).
    with get_sessionmaker()() as db:
        alerts.sweep_offline(db, stale_after_seconds=90)
    assert len(_open_alerts(server_id, "server_offline")) == 1


# -- REST: ack + cross-tenant isolation ---------------------------------------
def _seed_alert(kind="all_exhausted"):
    tenant_id, _a, server_id = _seed_tenant_account_server(f"{uuid.uuid4().hex[:6]}@ex.com")
    _servicer()._store_event(server_id, tenant_id, _exhausted_event())
    alert = _open_alerts(server_id, kind)[0]
    return tenant_id, server_id, alert.id


def test_list_and_ack_alert(app_env, client):
    tenant_id, server_id, alert_id = _seed_alert()

    listed = client.get(f"/api/v1/tenants/{tenant_id}/alerts")
    assert listed.status_code == 200
    body = listed.json()
    assert body["pageInfo"]["totalSize"] == 1
    assert body["items"][0]["status"] == "open"

    acked = client.post(
        f"/api/v1/tenants/{tenant_id}/alerts/{alert_id}:ack", json={"ackedBy": "alice"}
    )
    assert acked.status_code == 200
    assert acked.json()["status"] == "acked"
    assert acked.json()["ackedBy"] == "alice"
    assert acked.json()["ackedAt"] is not None


def test_list_filters_by_status_and_kind(app_env, client):
    tenant_id, server_id, alert_id = _seed_alert()
    assert client.get(
        f"/api/v1/tenants/{tenant_id}/alerts?status=open&kind=all_exhausted"
    ).json()["pageInfo"]["totalSize"] == 1
    assert client.get(
        f"/api/v1/tenants/{tenant_id}/alerts?status=resolved"
    ).json()["pageInfo"]["totalSize"] == 0
    assert client.get(
        f"/api/v1/tenants/{tenant_id}/alerts?kind=drift"
    ).json()["pageInfo"]["totalSize"] == 0


def test_cross_tenant_alert_is_invisible_and_unackable(app_env, client):
    tenant_a, _server_a, alert_a = _seed_alert()
    tenant_b, _b, _server_b = _seed_tenant_account_server("otherT@ex.com")

    # Listing under tenant B never shows tenant A's alert.
    assert client.get(
        f"/api/v1/tenants/{tenant_b}/alerts"
    ).json()["pageInfo"]["totalSize"] == 0
    # Acking tenant A's alert via tenant B's path is a 404 (no existence leak).
    denied = client.post(f"/api/v1/tenants/{tenant_b}/alerts/{alert_a}:ack")
    assert denied.status_code == 404
    # And tenant A's alert is untouched.
    with get_sessionmaker()() as db:
        assert db.get(Alert, alert_a).status == "open"


def test_ack_resolved_alert_conflicts(app_env, client):
    tenant_id, server_id, alert_id = _seed_alert()
    with get_sessionmaker()() as db:
        alert = db.get(Alert, alert_id)
        alert.status = "resolved"
        db.commit()
    resp = client.post(f"/api/v1/tenants/{tenant_id}/alerts/{alert_id}:ack")
    assert resp.status_code == 409


# -- REST: events timeline ----------------------------------------------------
def test_events_endpoint_returns_switch_events(app_env, client):
    tenant_id, account_id, server_id = _seed_tenant_account_server("evtapi@ex.com")
    switch = pb.AccountEvent(
        schema_version=1,
        agent_id=AGENT_ID,
        event_id="evt_" + uuid.uuid4().hex,
        kind=pb.AccountEvent.KIND_SWITCH,
    )
    _servicer()._store_event(server_id, tenant_id, switch)

    resp = client.get(f"/api/v1/tenants/{tenant_id}/servers/{server_id}/events")
    assert resp.status_code == 200
    body = resp.json()
    assert body["pageInfo"]["totalSize"] == 1
    assert body["items"][0]["reportType"] == "switch_event"


def test_events_endpoint_cross_tenant_404(app_env, client):
    tenant_a, _a, server_a = _seed_tenant_account_server("evtA@ex.com")
    tenant_b, _b, _server_b = _seed_tenant_account_server("evtB@ex.com")
    resp = client.get(f"/api/v1/tenants/{tenant_b}/servers/{server_a}/events")
    assert resp.status_code == 404
