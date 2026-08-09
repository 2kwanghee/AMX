"""F1 tenant RBAC (P5 S2b) — /admins management API and delete_tenant guard.

The load-bearing claims: only a global-admin manages admins; the bcrypt hash and
plaintext password never leave the server; disabling an admin kills its live
sessions; the last enabled global-admin cannot be removed; and deleting a tenant
that still has a pinned admin is a clean 409, never a 500.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models import Admin
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


def _ta_token(client, db, tenant_id, email="ta@x.example.com"):
    _make_admin(db, email=email, role="tenant-admin", tenant_id=tenant_id)
    return _login(client, email).json()["sessionToken"]


# -- create -------------------------------------------------------------------
def test_global_admin_creates_tenant_admin(client, db):
    tid = _make_tenant(client, "acme")
    r = client.post(
        f"{API}/admins",
        json={"email": "new@acme.example.com", "password": "pw", "role": "tenant-admin", "tenantId": tid},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["role"] == "tenant-admin"
    assert body["tenantId"] == tid
    assert body["disabled"] is False
    # The created admin can log in with the password we set.
    assert _login(client, "new@acme.example.com", password="pw").status_code == 200


def test_create_global_admin_without_tenant(client):
    r = client.post(
        f"{API}/admins",
        json={"email": "ga2@x.example.com", "password": "pw", "role": "global-admin"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["tenantId"] is None


def test_create_response_never_carries_hash_or_plaintext(client):
    r = client.post(
        f"{API}/admins",
        json={"email": "leak@x.example.com", "password": "super-secret-pw", "role": "global-admin"},
    )
    assert r.status_code == 201
    assert "passwordHash" not in r.json() and "password_hash" not in r.json()
    assert "super-secret-pw" not in r.text
    assert "sha256:" not in r.text and "$2b$" not in r.text


def test_create_rejects_role_tenant_mismatch(client):
    # tenant-admin without tenant_id → 400.
    a = client.post(
        f"{API}/admins",
        json={"email": "m1@x.example.com", "password": "pw", "role": "tenant-admin"},
    )
    assert a.status_code == 400
    assert a.json()["code"] == "admin.role_tenant_mismatch"
    # global-admin with a tenant_id → 400.
    tid = _make_tenant(client, "mism")
    b = client.post(
        f"{API}/admins",
        json={"email": "m2@x.example.com", "password": "pw", "role": "global-admin", "tenantId": tid},
    )
    assert b.status_code == 400
    assert b.json()["code"] == "admin.role_tenant_mismatch"


def test_create_unknown_tenant_is_404(client):
    r = client.post(
        f"{API}/admins",
        json={
            "email": "ghost@x.example.com",
            "password": "pw",
            "role": "tenant-admin",
            "tenantId": "00000000-0000-0000-0000-000000000009",
        },
    )
    assert r.status_code == 404
    assert r.json()["code"] == "tenant.not_found"


def test_create_duplicate_email_is_409(client):
    client.post(
        f"{API}/admins",
        json={"email": "dup@x.example.com", "password": "pw", "role": "global-admin"},
    )
    r = client.post(
        f"{API}/admins",
        json={"email": "DUP@x.example.com", "password": "pw", "role": "global-admin"},
    )
    assert r.status_code == 409
    assert r.json()["code"] == "admin.duplicate_email"


# -- authorization (global-admin only) ---------------------------------------
def test_tenant_admin_cannot_reach_admins(client, db):
    tid = _make_tenant(client, "ta-forbidden")
    token = _ta_token(client, db, tid)
    assert client.get(f"{API}/admins", headers=_auth(token)).status_code == 403
    assert client.post(
        f"{API}/admins",
        json={"email": "x@x.example.com", "password": "pw", "role": "global-admin"},
        headers=_auth(token),
    ).status_code == 403


def test_admins_rejects_invalid_token(client):
    r = client.get(f"{API}/admins", headers=_auth("not-a-real-token"))
    assert r.status_code == 401
    assert r.json()["code"] == "auth.invalid_token"


# -- list / get ---------------------------------------------------------------
def test_list_and_get_mask_the_hash(client, db):
    _make_admin(db, email="l1@x.example.com", role="global-admin")
    lst = client.get(f"{API}/admins")
    assert lst.status_code == 200
    assert "passwordHash" not in lst.text and "sha256:" not in lst.text
    admin_id = lst.json()["items"][0]["id"]
    one = client.get(f"{API}/admins/{admin_id}")
    assert one.status_code == 200
    assert "passwordHash" not in one.json()


def test_get_unknown_admin_is_404(client):
    r = client.get(f"{API}/admins/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404
    assert r.json()["code"] == "admin.not_found"


# -- patch: password reset ----------------------------------------------------
def test_password_reset_changes_login(client, db):
    admin = _make_admin(db, email="pw@x.example.com", role="global-admin", password="old-pw")
    assert _login(client, "pw@x.example.com", password="old-pw").status_code == 200
    r = client.patch(f"{API}/admins/{admin.id}", json={"password": "new-pw"})
    assert r.status_code == 200
    assert _login(client, "pw@x.example.com", password="old-pw").status_code == 401
    assert _login(client, "pw@x.example.com", password="new-pw").status_code == 200


# -- patch/delete: disable invalidates sessions -------------------------------
def test_disable_via_patch_kills_live_session(client, db):
    # Two global-admins so disabling one is allowed by the last-admin guard.
    admin = _make_admin(db, email="d1@x.example.com", role="global-admin")
    _make_admin(db, email="keep@x.example.com", role="global-admin")
    token = _login(client, "d1@x.example.com").json()["sessionToken"]
    assert client.get(f"{API}/tenants", headers=_auth(token)).status_code == 200
    r = client.patch(f"{API}/admins/{admin.id}", json={"disabled": True})
    assert r.status_code == 200 and r.json()["disabled"] is True
    assert client.get(f"{API}/tenants", headers=_auth(token)).status_code == 401
    assert _login(client, "d1@x.example.com").status_code == 401


def test_delete_admin_removes_it_and_its_sessions(client, db):
    admin = _make_admin(db, email="del@x.example.com", role="global-admin")
    _make_admin(db, email="keep2@x.example.com", role="global-admin")
    token = _login(client, "del@x.example.com").json()["sessionToken"]
    assert client.delete(f"{API}/admins/{admin.id}").status_code == 204
    # Row gone (404) and the live session is dead (CASCADE).
    assert client.get(f"{API}/admins/{admin.id}").status_code == 404
    assert client.get(f"{API}/tenants", headers=_auth(token)).status_code == 401


# -- last global-admin protection --------------------------------------------
def test_cannot_delete_last_enabled_global_admin(client, db):
    # The admins table holds exactly one enabled global-admin.
    admin = _make_admin(db, email="only@x.example.com", role="global-admin")
    r = client.delete(f"{API}/admins/{admin.id}")
    assert r.status_code == 409
    assert r.json()["code"] == "admin.last_global_admin"


def test_cannot_disable_last_enabled_global_admin(client, db):
    admin = _make_admin(db, email="onlyd@x.example.com", role="global-admin")
    r = client.patch(f"{API}/admins/{admin.id}", json={"disabled": True})
    assert r.status_code == 409
    assert r.json()["code"] == "admin.last_global_admin"


def test_disabled_global_admin_does_not_count_toward_last(client, db):
    # One enabled + one already-disabled global-admin: the disabled one may be
    # deleted (it provides no access), the enabled one may not.
    enabled = _make_admin(db, email="en@x.example.com", role="global-admin")
    disabled = _make_admin(db, email="dis@x.example.com", role="global-admin", disabled=True)
    assert client.delete(f"{API}/admins/{disabled.id}").status_code == 204
    assert client.delete(f"{API}/admins/{enabled.id}").status_code == 409


def test_tenant_admin_is_not_a_global_admin_for_the_guard(client, db):
    # A lone global-admin plus tenant-admins: deleting a tenant-admin is fine and
    # does not trip the guard; the global-admin is still protected.
    tid = _make_tenant(client, "guard")
    ga = _make_admin(db, email="ga@x.example.com", role="global-admin")
    ta = _make_admin(db, email="ta2@x.example.com", role="tenant-admin", tenant_id=tid)
    assert client.delete(f"{API}/admins/{ta.id}").status_code == 204
    assert client.delete(f"{API}/admins/{ga.id}").status_code == 409


# -- delete_tenant guard (500 → 409) -----------------------------------------
def test_delete_tenant_with_pinned_admin_is_409_not_500(client, db):
    tid = _make_tenant(client, "pinned")
    _make_admin(db, email="pinned@x.example.com", role="tenant-admin", tenant_id=tid)
    r = client.delete(f"{API}/tenants/{tid}")
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "tenant.has_admins"
    # After removing the admin, the tenant deletes cleanly.
    admin = db.scalars(
        select(Admin).where(Admin.tenant_id.isnot(None))
    ).first()
    db.delete(admin)
    db.commit()
    assert client.delete(f"{API}/tenants/{tid}").status_code == 204


def test_delete_empty_tenant_still_works(client):
    tid = _make_tenant(client, "empty")
    assert client.delete(f"{API}/tenants/{tid}").status_code == 204
