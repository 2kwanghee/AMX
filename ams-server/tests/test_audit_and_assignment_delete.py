"""Console-test gaps G53 (admin audit trail) and G54 (detached delete + sweep).

The audit middleware and the read endpoint are exercised through the real HTTP
stack against a real Postgres, because the point of the trail is what actually
lands in `admin_audit_logs` after a response — status code included.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import func, select, update

from app.core.auth import ROOT_PRINCIPAL_EMAIL
from app.models import AdminAuditLog, Assignment
from app.services import admins, audit, inventory
from tests.test_api_crud import make_account, make_server, make_tenant

API = "/api/v1"


def _audit_rows(db):
    db.expire_all()
    return list(db.scalars(select(AdminAuditLog).order_by(AdminAuditLog.created_at)).all())


def _assignment_exists(db, assignment_id) -> bool:
    db.expire_all()
    return db.scalar(
        select(func.count()).select_from(Assignment).where(Assignment.id == assignment_id)
    ) > 0


# -- G53: the middleware records mutating calls -------------------------------
def test_successful_mutation_is_recorded_with_status_and_identity(client, db):
    make_tenant(client, name="audit-ok")
    created = [r for r in _audit_rows(db) if r.action == "POST /tenants"]
    assert len(created) == 1
    row = created[0]
    assert row.method == "POST"
    assert row.path == "/api/v1/tenants"
    assert row.status_code == 201
    assert row.admin_email == ROOT_PRINCIPAL_EMAIL  # root bearer used by the client fixture
    assert row.tenant_id is None  # tenant-create is a global (tenant-less) action


def test_failed_mutation_is_recorded_with_its_error_status(client, db):
    make_tenant(client, name="dup-name")
    # A second tenant with the same name is a 409; the attempt must still be logged.
    resp = client.post(f"{API}/tenants", json={"name": "dup-name"})
    assert resp.status_code == 409
    rows = [r for r in _audit_rows(db) if r.action == "POST /tenants"]
    assert [r.status_code for r in rows] == [201, 409]


def test_target_id_is_the_trailing_uuid_segment(client, db):
    tid = make_tenant(client, name="audit-target")
    resp = client.patch(f"{API}/tenants/{tid}", json={"status": "suspended"})
    assert resp.status_code == 200
    patched = [r for r in _audit_rows(db) if r.method == "PATCH"]
    assert len(patched) == 1
    assert str(patched[0].target_id) == str(tid)
    assert patched[0].action == "PATCH /tenants/{tenant_id}"


def test_read_and_excluded_paths_are_not_recorded(client, db):
    make_tenant(client, name="audit-exclude")
    client.get(f"{API}/tenants")  # GET is read-only
    client.post(f"{API}/auth/login", json={"email": "nobody@x.example.com", "password": "x"})
    client.get("/healthz")
    rows = _audit_rows(db)
    assert all(r.method not in ("GET", "HEAD", "OPTIONS") for r in rows)
    assert all(r.path != f"{API}/auth/login" for r in rows)
    assert all(r.path != "/healthz" for r in rows)


def test_unauthenticated_mutation_is_not_recorded(client, db):
    # A rejected-before-auth request has no principal; it must leave no trail
    # (anonymous attempts are an unbounded-growth vector, not audit material).
    resp = client.post(
        f"{API}/tenants", json={"name": "anon"}, headers={"Authorization": "Bearer nope"}
    )
    assert resp.status_code == 401
    assert _audit_rows(db) == []


def test_audit_write_failure_does_not_break_the_request(client, monkeypatch):
    # If the audit row cannot be written, the underlying action still succeeds.
    import app.api.audit as audit_mw

    def _boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(audit_mw, "get_sessionmaker", _boom)
    resp = client.post(f"{API}/tenants", json={"name": "audit-resilient"})
    assert resp.status_code == 201


# -- G53: the read endpoint ---------------------------------------------------
def _login_headers(client, email, password="pw-correct-horse"):
    token = client.post(f"{API}/auth/login", json={"email": email, "password": password}).json()[
        "sessionToken"
    ]
    return {"Authorization": f"Bearer {token}"}


def test_read_scopes_tenant_and_global_rows_by_role(client, db):
    t1 = make_tenant(client, name="scope-t1")
    t2 = make_tenant(client, name="scope-t2")
    make_account(client, t1, email="in-t1@example.com")  # tenant-scoped row (tenant_id=t1)

    admins.create_admin(
        db, email="ta-t1@x.example.com", password="pw-correct-horse",
        role="tenant-admin", tenant_id=uuid.UUID(t1),
    )
    ta_headers = _login_headers(client, "ta-t1@x.example.com")

    # global-admin (root bearer) sees the t1 rows AND the global tenant-create rows.
    ga = client.get(f"{API}/tenants/{t1}/audit-logs").json()
    ga_actions = {item["action"] for item in ga["items"]}
    assert "POST /tenants" in ga_actions  # a global (tenant-less) row
    assert "POST /tenants/{tenant_id}/accounts" in ga_actions  # a t1 row

    # tenant-admin sees only its own tenant's rows, never the global ones.
    ta = client.get(f"{API}/tenants/{t1}/audit-logs", headers=ta_headers).json()
    ta_actions = {item["action"] for item in ta["items"]}
    assert "POST /tenants/{tenant_id}/accounts" in ta_actions
    assert "POST /tenants" not in ta_actions
    # ta cannot even reach t2 — that is 404, not an empty page.
    assert client.get(f"{API}/tenants/{t2}/audit-logs", headers=ta_headers).status_code == 404


def test_read_pagination_and_time_window(client, db):
    tid = make_tenant(client, name="paginate")
    for i in range(3):
        make_server(client, tid, name=f"srv-{i}")
    first = client.get(f"{API}/tenants/{tid}/audit-logs?limit=2").json()
    assert len(first["items"]) == 2
    token = first["pageInfo"]["nextPageToken"]
    assert token
    second = client.get(f"{API}/tenants/{tid}/audit-logs?limit=2&pageToken={token}").json()
    assert len(second["items"]) >= 1
    # newest-first ordering: page 1's first row is at least as new as page 2's.
    assert first["items"][0]["createdAt"] >= second["items"][0]["createdAt"]

    # A `to` bound in the far past excludes everything.
    empty = client.get(f"{API}/tenants/{tid}/audit-logs?to=2000-01-01T00:00:00Z").json()
    assert empty["items"] == []


def test_audit_read_endpoint_itself_is_not_recorded(client, db):
    tid = make_tenant(client, name="no-self-log")
    client.get(f"{API}/tenants/{tid}/audit-logs")
    assert all("audit-logs" not in r.path for r in _audit_rows(db))


def test_naive_datetime_bounds_are_coerced_to_utc(client, db):
    # A bound without a timezone must be accepted and treated as UTC, not depend
    # on the DB session's timezone.
    tid = make_tenant(client, name="naive-tz")
    all_rows = client.get(f"{API}/tenants/{tid}/audit-logs?from=2000-01-01T00:00:00").json()
    assert len(all_rows["items"]) >= 1  # naive `from` in the past keeps everything
    empty = client.get(f"{API}/tenants/{tid}/audit-logs?to=2000-01-01T00:00:00").json()
    assert empty["items"] == []  # naive `to` in the past excludes everything


# -- G53: audit retention sweep -----------------------------------------------
def _age_all_audit_rows(db, *, days):
    db.execute(
        update(AdminAuditLog).values(created_at=inventory._now() - timedelta(days=days))
    )
    db.commit()


def _audit_count(db):
    db.expire_all()
    return db.scalar(select(func.count()).select_from(AdminAuditLog))


def test_audit_retention_sweep_purges_aged_rows_when_enabled(client, db, monkeypatch):
    make_tenant(client, name="aud-ret")
    _age_all_audit_rows(db, days=100)
    assert _audit_count(db) >= 1
    monkeypatch.setattr(audit, "get_settings", lambda: SimpleNamespace(audit_retention_days=90))
    purged = audit.sweep_audit_retention(db)
    assert purged >= 1
    assert _audit_count(db) == 0


def test_audit_retention_default_zero_keeps_everything(client, db, monkeypatch):
    make_tenant(client, name="aud-keep")
    _age_all_audit_rows(db, days=100000)
    before = _audit_count(db)
    assert before >= 1
    monkeypatch.setattr(audit, "get_settings", lambda: SimpleNamespace(audit_retention_days=0))
    assert audit.sweep_audit_retention(db) == 0
    assert _audit_count(db) == before


# -- G54: detached assignment delete ------------------------------------------
def _pending_assignment(client, tid, *, email="asg@example.com", server_name="asg-srv"):
    account = make_account(client, tid, email=email)
    server = make_server(client, tid, name=server_name)
    resp = client.post(
        f"{API}/tenants/{tid}/assignments",
        json={"accountId": account["id"], "serverId": server["id"]},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _force_detached(db, assignment_id, *, age_days: float = 0.0):
    # Set state via a core UPDATE (no identity-map caching) so later count
    # queries reflect what the endpoint/sweep actually did.
    db.execute(
        update(Assignment)
        .where(Assignment.id == uuid.UUID(assignment_id))
        .values(state="detached", updated_at=inventory._now() - timedelta(days=age_days))
    )
    db.commit()


def test_delete_detached_assignment_returns_204_and_is_audited(client, db):
    tid = make_tenant(client, name="del-detached")
    asg = _pending_assignment(client, tid)
    _force_detached(db, asg["id"])

    resp = client.delete(f"{API}/tenants/{tid}/assignments/{asg['id']}")
    assert resp.status_code == 204
    assert not _assignment_exists(db, uuid.UUID(asg["id"]))

    del_rows = [r for r in _audit_rows(db) if r.method == "DELETE"]
    assert len(del_rows) == 1
    assert del_rows[0].status_code == 204
    assert str(del_rows[0].target_id) == asg["id"]


def test_delete_non_detached_assignment_is_409(client, db):
    tid = make_tenant(client, name="del-active")
    asg = _pending_assignment(client, tid)  # state is `pending`, not deletable
    resp = client.delete(f"{API}/tenants/{tid}/assignments/{asg['id']}")
    assert resp.status_code == 409
    assert resp.json()["code"] == "assignment.not_deletable"
    assert _assignment_exists(db, uuid.UUID(asg["id"]))


def test_delete_foreign_tenant_assignment_is_404(client, db):
    t1 = make_tenant(client, name="own")
    t2 = make_tenant(client, name="other")
    asg = _pending_assignment(client, t1)
    resp = client.delete(f"{API}/tenants/{t2}/assignments/{asg['id']}")
    assert resp.status_code == 404


# -- G54: retention sweep -----------------------------------------------------
def _detached_assignment(client, db, tid, *, age_days, email, server_name):
    asg = _pending_assignment(client, tid, email=email, server_name=server_name)
    _force_detached(db, asg["id"], age_days=age_days)
    return uuid.UUID(asg["id"])


def test_sweep_purges_only_aged_out_detached_rows(client, db, monkeypatch):
    tid = make_tenant(client, name="sweep")
    old = _detached_assignment(client, db, tid, age_days=100, email="old@x.example.com", server_name="s-old")
    fresh = _detached_assignment(client, db, tid, age_days=1, email="new@x.example.com", server_name="s-new")

    monkeypatch.setattr(inventory, "get_settings", lambda: SimpleNamespace(assignment_retention_days=90))
    assert inventory.sweep_assignment_retention(db) == 1
    assert not _assignment_exists(db, old)
    assert _assignment_exists(db, fresh)


def test_sweep_disabled_purges_nothing(client, db, monkeypatch):
    tid = make_tenant(client, name="sweep-off")
    old = _detached_assignment(client, db, tid, age_days=1000, email="anc@x.example.com", server_name="s-anc")

    monkeypatch.setattr(inventory, "get_settings", lambda: SimpleNamespace(assignment_retention_days=0))
    assert inventory.sweep_assignment_retention(db) == 0
    assert _assignment_exists(db, old)
