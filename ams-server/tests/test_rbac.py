"""F1 tenant RBAC (P5 S2a) — session auth, scoping, cross-tenant isolation.

The load-bearing claim is that a tenant-admin reaches only its own tenant: a
foreign tenant is 404 (hidden) on every resource, own-tenant management is 403
(capability), and the bootstrap root token is unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.routing import APIRoute

from app.api.deps import require_tenant_scope
from app.core import crypto
from app.models import AdminSession
from app.services import admins

API = "/api/v1"


# -- helpers ------------------------------------------------------------------
def _make_tenant(client, name: str) -> str:
    r = client.post(f"{API}/tenants", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _make_admin(db, *, email, role, tenant_id=None, password="pw-correct-horse", disabled=False):
    admin = admins.create_admin(db, email=email, password=password, role=role, tenant_id=tenant_id)
    if disabled:
        admin.disabled = True
        db.commit()
    return admin


def _login(client, email, password="pw-correct-horse"):
    return client.post(f"{API}/auth/login", json={"email": email, "password": password})


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# -- bcrypt -------------------------------------------------------------------
def test_bcrypt_roundtrip_and_wrong_password():
    h = crypto.hash_password("s3cret-password")
    assert h != "s3cret-password"
    assert crypto.verify_password("s3cret-password", h)
    assert not crypto.verify_password("wrong", h)


def test_bcrypt_prehash_defeats_72_byte_truncation():
    # Two long passwords sharing a 72-byte prefix must NOT verify against each
    # other — the sha256 pre-hash covers the whole input.
    base = "A" * 72
    h = crypto.hash_password(base + "-tail-one")
    assert not crypto.verify_password(base + "-tail-two", h)
    assert crypto.verify_password(base + "-tail-one", h)


# -- login / session ----------------------------------------------------------
def test_login_issues_session_and_authenticates(client, db):
    tid = _make_tenant(client, "acme")
    _make_admin(db, email="ta@acme.example.com", role="tenant-admin", tenant_id=tid)

    r = _login(client, "ta@acme.example.com")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "tenant-admin"
    assert body["tenantIds"] == [tid]
    assert body["sessionToken"]

    # The issued token authenticates a scoped request.
    got = client.get(f"{API}/tenants/{tid}/accounts", headers=_auth(body["sessionToken"]))
    assert got.status_code == 200


def test_login_normalises_email_case(client, db):
    _make_admin(db, email="Mixed@case.example.com", role="global-admin")
    r = _login(client, "mixed@case.example.com")
    assert r.status_code == 200


def test_wrong_password_is_401(client, db):
    _make_admin(db, email="ga@x.example.com", role="global-admin")
    r = _login(client, "ga@x.example.com", password="nope")
    assert r.status_code == 401
    assert r.json()["code"] == "auth.invalid_credentials"


def test_unknown_email_is_401(client):
    r = _login(client, "ghost@x.example.com")
    assert r.status_code == 401
    assert r.json()["code"] == "auth.invalid_credentials"


def test_disabled_admin_cannot_login(client, db):
    _make_admin(db, email="off@x.example.com", role="global-admin", disabled=True)
    r = _login(client, "off@x.example.com")
    assert r.status_code == 401


def test_expired_session_is_rejected(client, db):
    admin = _make_admin(db, email="exp@x.example.com", role="global-admin")
    raw = crypto.new_token()
    db.add(
        AdminSession(
            admin_id=admin.id,
            token_hash=crypto.hash_token(raw),
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    db.commit()
    r = client.get(f"{API}/tenants", headers=_auth(raw))
    assert r.status_code == 401
    assert r.json()["code"] == "auth.invalid_token"


def test_logout_revokes_the_session(client, db):
    _make_admin(db, email="lo@x.example.com", role="global-admin")
    token = _login(client, "lo@x.example.com").json()["sessionToken"]
    assert client.get(f"{API}/tenants", headers=_auth(token)).status_code == 200
    assert client.post(f"{API}/auth/logout", headers=_auth(token)).status_code == 204
    # Second use is dead.
    assert client.get(f"{API}/tenants", headers=_auth(token)).status_code == 401


def test_disabled_after_login_kills_live_session(client, db):
    admin = _make_admin(db, email="rug@x.example.com", role="global-admin")
    token = _login(client, "rug@x.example.com").json()["sessionToken"]
    assert client.get(f"{API}/tenants", headers=_auth(token)).status_code == 200
    admin.disabled = True
    db.commit()
    assert client.get(f"{API}/tenants", headers=_auth(token)).status_code == 401


# -- root token (no regression) ----------------------------------------------
def test_root_token_still_maps_to_global_admin(client):
    # `client` carries the root AMX_ADMIN_TOKEN and creates tenants freely.
    tid = _make_tenant(client, "root-owned")
    assert client.get(f"{API}/tenants/{tid}").status_code == 200


# -- global-admin session reaches any tenant ---------------------------------
RESOURCES = ["accounts", "servers", "assignments", "alerts"]


@pytest.mark.parametrize("resource", RESOURCES)
def test_global_admin_session_reaches_any_tenant(client, db, resource):
    tid = _make_tenant(client, f"ga-{resource}")
    _make_admin(db, email=f"ga-{resource}@x.example.com", role="global-admin")
    token = _login(client, f"ga-{resource}@x.example.com").json()["sessionToken"]
    r = client.get(f"{API}/tenants/{tid}/{resource}", headers=_auth(token))
    assert r.status_code == 200


# -- tenant-admin isolation (the core claim) ---------------------------------
@pytest.mark.parametrize("resource", RESOURCES)
def test_tenant_admin_sees_own_but_not_foreign_tenant(client, db, resource):
    own = _make_tenant(client, f"own-{resource}")
    other = _make_tenant(client, f"other-{resource}")
    _make_admin(db, email=f"ta-{resource}@x.example.com", role="tenant-admin", tenant_id=own)
    token = _login(client, f"ta-{resource}@x.example.com").json()["sessionToken"]

    assert client.get(f"{API}/tenants/{own}/{resource}", headers=_auth(token)).status_code == 200
    foreign = client.get(f"{API}/tenants/{other}/{resource}", headers=_auth(token))
    assert foreign.status_code == 404, foreign.text
    assert foreign.json()["code"] == "tenant.not_found"


def test_tenant_admin_list_tenants_is_scoped(client, db):
    own = _make_tenant(client, "own-list")
    _make_tenant(client, "other-list")
    _make_admin(db, email="ta-list@x.example.com", role="tenant-admin", tenant_id=own)
    token = _login(client, "ta-list@x.example.com").json()["sessionToken"]
    r = client.get(f"{API}/tenants", headers=_auth(token))
    assert r.status_code == 200
    ids = [t["id"] for t in r.json()["items"]]
    assert ids == [own]
    assert r.json()["pageInfo"]["totalSize"] == 1


def test_tenant_admin_get_foreign_tenant_is_404(client, db):
    own = _make_tenant(client, "own-get")
    other = _make_tenant(client, "other-get")
    _make_admin(db, email="ta-get@x.example.com", role="tenant-admin", tenant_id=own)
    token = _login(client, "ta-get@x.example.com").json()["sessionToken"]
    assert client.get(f"{API}/tenants/{own}", headers=_auth(token)).status_code == 200
    assert client.get(f"{API}/tenants/{other}", headers=_auth(token)).status_code == 404


def test_tenant_admin_cannot_create_tenant(client, db):
    own = _make_tenant(client, "own-create")
    _make_admin(db, email="ta-create@x.example.com", role="tenant-admin", tenant_id=own)
    token = _login(client, "ta-create@x.example.com").json()["sessionToken"]
    r = client.post(f"{API}/tenants", json={"name": "sneaky"}, headers=_auth(token))
    assert r.status_code == 403
    assert r.json()["code"] == "auth.forbidden"


def test_tenant_admin_cannot_rename_or_delete_own_tenant(client, db):
    own = _make_tenant(client, "own-mutate")
    _make_admin(db, email="ta-mut@x.example.com", role="tenant-admin", tenant_id=own)
    token = _login(client, "ta-mut@x.example.com").json()["sessionToken"]
    patch = client.patch(f"{API}/tenants/{own}", json={"name": "renamed"}, headers=_auth(token))
    assert patch.status_code == 403
    delete = client.delete(f"{API}/tenants/{own}", headers=_auth(token))
    assert delete.status_code == 403


def test_tenant_admin_mutating_foreign_tenant_is_404_not_403(client, db):
    own = _make_tenant(client, "own-fmut")
    other = _make_tenant(client, "other-fmut")
    _make_admin(db, email="ta-fmut@x.example.com", role="tenant-admin", tenant_id=own)
    token = _login(client, "ta-fmut@x.example.com").json()["sessionToken"]
    # Scope (404) is evaluated before capability (403): a foreign id is hidden.
    assert client.patch(f"{API}/tenants/{other}", json={"name": "x"}, headers=_auth(token)).status_code == 404
    assert client.delete(f"{API}/tenants/{other}", headers=_auth(token)).status_code == 404


def test_global_admin_session_can_create_tenant(client, db):
    _make_admin(db, email="ga-create@x.example.com", role="global-admin")
    token = _login(client, "ga-create@x.example.com").json()["sessionToken"]
    r = client.post(f"{API}/tenants", json={"name": "ga-made"}, headers=_auth(token))
    assert r.status_code == 201


# -- /admins is S2b, absent here ---------------------------------------------
def test_admins_management_api_is_absent(client):
    r = client.get(f"{API}/admins")
    assert r.status_code == 404


# -- bootstrap CLI ------------------------------------------------------------
def test_admin_cli_creates_a_working_admin(client, db, monkeypatch, capsys):
    from app import admin_cli

    monkeypatch.setenv("AMX_BOOTSTRAP_PASSWORD", "cli-set-password")
    rc = admin_cli.main(["create-admin", "--email", "cli@x.example.com", "--role", "global-admin"])
    assert rc == 0
    out = capsys.readouterr().out
    # Never prints the password or a token.
    assert "cli-set-password" not in out
    assert "sha256:" not in out and "bcrypt" not in out.lower()

    r = _login(client, "cli@x.example.com", password="cli-set-password")
    assert r.status_code == 200
    assert r.json()["role"] == "global-admin"


def test_admin_cli_rejects_role_tenant_mismatch(db, monkeypatch):
    from app import admin_cli

    monkeypatch.setenv("AMX_BOOTSTRAP_PASSWORD", "pw")
    # global-admin with a tenant id is rejected.
    rc = admin_cli.main(
        ["create-admin", "--email", "bad@x.example.com", "--role", "global-admin", "--tenant-id", "00000000-0000-0000-0000-000000000001"]
    )
    assert rc == 1


def test_create_admin_service_rejects_duplicate_email(db):
    _make_admin(db, email="dup@x.example.com", role="global-admin")
    from app.core.errors import ApiError

    with pytest.raises(ApiError) as exc:
        admins.create_admin(db, email="DUP@x.example.com", password="pw", role="global-admin", tenant_id=None)
    assert exc.value.status == 409


# -- meta test: no scope-less /tenants/{tenant_id} route can be added ---------
def _dependant_calls(dependant) -> set:
    calls = set()
    if dependant.call is not None:
        calls.add(dependant.call)
    for sub in dependant.dependencies:
        calls |= _dependant_calls(sub)
    return calls


def _iter_api_routes(routes):
    # This FastAPI version keeps `include_router` results as nested
    # `_IncludedRouter` objects rather than flattening them into app.routes; the
    # child APIRoutes hang off `original_router.routes`. Descend through both a
    # plain `.routes` and that wrapper so every real endpoint is reached.
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        included = getattr(route, "original_router", None)
        if included is not None and getattr(included, "routes", None):
            yield from _iter_api_routes(included.routes)
        sub = getattr(route, "routes", None)
        if sub:
            yield from _iter_api_routes(sub)


def test_every_tenant_scoped_route_enforces_require_tenant_scope(app):
    scoped_paths = [
        route
        for route in _iter_api_routes(app.routes)
        if "/tenants/{tenant_id}" in route.path
    ]
    # Sanity: the sub-routers registered at least the four resources.
    assert scoped_paths, "no /tenants/{tenant_id} routes found — wiring changed"
    for route in scoped_paths:
        calls = _dependant_calls(route.dependant)
        assert require_tenant_scope in calls, (
            f"{route.methods} {route.path} is missing require_tenant_scope"
        )
