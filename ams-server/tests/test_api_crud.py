"""REST round trips, cross-tenant access, auth, and the P2 stubs."""

from __future__ import annotations

import uuid

from tests.conftest import TEST_ADMIN_TOKEN

CREDENTIAL_SET = (
    '{"claudeAiOauth": {"accessToken": "at-test", "refreshToken": "rt-test", '
    '"expiresAt": 4102444800000, "scopes": ["user:inference", "user:profile"], '
    '"emailAddress": "a@example.com", "organizationName": "Acme"}}'
)


def make_tenant(client, name="acme"):
    response = client.post("/api/v1/tenants", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def make_account(client, tenant_id, email="a@example.com"):
    response = client.post(
        f"/api/v1/tenants/{tenant_id}/accounts",
        json={"email": email, "credentialType": "oauth", "secret": CREDENTIAL_SET},
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_server(client, tenant_id, name="runner-1"):
    response = client.post(
        f"/api/v1/tenants/{tenant_id}/servers", json={"name": name, "hostname": "10.0.0.1"}
    )
    assert response.status_code == 201, response.text
    return response.json()


# -- Auth ---------------------------------------------------------------------
def test_requests_without_a_bearer_token_are_rejected(client):
    response = client.get("/api/v1/tenants", headers={"Authorization": ""})
    assert response.status_code == 401
    assert response.json()["code"] == "auth.missing_bearer"


def test_a_wrong_bearer_token_is_rejected(client):
    response = client.get("/api/v1/tenants", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
    assert response.json()["code"] == "auth.invalid_token"


def test_a_non_ascii_bearer_token_is_rejected_not_crashed(client):
    """A 401, not an unhandled 500.

    Header bytes are decoded latin-1, so any byte above 0x7F becomes a
    non-ASCII `str` — and `secrets.compare_digest` raises TypeError on those
    rather than returning False. The header is sent as raw bytes here because
    that is what a caller puts on the wire; the HTTP client refuses to encode a
    non-ASCII header given as `str`, so a str-valued test would never reach the
    server at all.
    """
    for token in ("토큰값입니다", "café-token-value", "🔑🔑🔑"):
        response = client.get(
            "/api/v1/tenants",
            headers={"Authorization": b"Bearer " + token.encode("utf-8")},
        )
        assert response.status_code == 401, token
        assert response.json()["code"] == "auth.invalid_token"


def test_the_configured_token_is_accepted(client):
    response = client.get(
        "/api/v1/tenants", headers={"Authorization": f"Bearer {TEST_ADMIN_TOKEN}"}
    )
    assert response.status_code == 200


# -- Principal type contract (P5-S1) ------------------------------------------
# The value is not yet read by any endpoint (scoping is S2); these pin the type
# `require_admin` now returns so S2 can build on a stable contract.
def test_require_admin_returns_a_global_admin_principal_for_a_valid_token(app_env):
    from app.core.auth import Principal, require_admin

    principal = require_admin(authorization=f"Bearer {TEST_ADMIN_TOKEN}")
    assert isinstance(principal, Principal)
    assert principal.role == "global-admin"
    assert principal.all_tenants is True
    assert principal.tenant_ids == frozenset()


def test_require_admin_still_rejects_missing_or_bad_tokens(app_env):
    import pytest

    from app.core.auth import require_admin
    from app.core.errors import ApiError

    cases = {
        None: "auth.missing_bearer",
        "": "auth.missing_bearer",
        "Bearer ": "auth.missing_bearer",
        "Bearer wrong-token": "auth.invalid_token",
        f"Basic {TEST_ADMIN_TOKEN}": "auth.missing_bearer",
    }
    for header, code in cases.items():
        with pytest.raises(ApiError) as excinfo:
            require_admin(authorization=header)
        assert excinfo.value.status == 401
        assert excinfo.value.code == code


# -- Validation failures ------------------------------------------------------
CANARY = "canary-plaintext-must-not-appear-9f3a1c"


def test_a_422_never_echoes_the_submitted_secret(client):
    """FastAPI's default 422 body includes each error's `input` (§7)."""
    tenant_id = make_tenant(client)
    response = client.post(
        f"/api/v1/tenants/{tenant_id}/accounts",
        json={"credentialType": "oauth", "secret": CANARY},  # email missing
    )
    assert response.status_code == 422
    assert CANARY not in response.text
    body = response.json()
    assert body["code"] == "request.invalid"
    assert body["status"] == 422
    assert [e["loc"] for e in body["errors"]] == [["body", "email"]]
    for error in body["errors"]:
        assert set(error) == {"loc", "msg", "type"}


def test_a_422_never_echoes_a_bad_enum_value(client):
    """The offending value reaches `input` and `ctx` on enum failures too."""
    tenant_id = make_tenant(client)
    response = client.post(
        f"/api/v1/tenants/{tenant_id}/accounts",
        json={"email": "a@example.com", "credentialType": CANARY, "secret": CANARY},
    )
    assert response.status_code == 422
    assert CANARY not in response.text


def test_scrub_keeps_only_location_and_message():
    from app.core.errors import scrub_validation_errors

    scrubbed = scrub_validation_errors(
        [
            {
                "loc": ("body", "secret"),
                "msg": "field required",
                "type": "missing",
                "input": {"secret": CANARY},
                "ctx": {"expected": CANARY},
                "url": "https://errors.pydantic.dev/",
            }
        ]
    )
    assert scrubbed == [{"loc": ["body", "secret"], "msg": "field required", "type": "missing"}]


# -- Round trips --------------------------------------------------------------
def test_tenant_round_trip(client):
    tenant_id = make_tenant(client)
    assert client.get(f"/api/v1/tenants/{tenant_id}").json()["name"] == "acme"

    patched = client.patch(f"/api/v1/tenants/{tenant_id}", json={"status": "suspended"})
    assert patched.json()["status"] == "suspended"

    assert client.delete(f"/api/v1/tenants/{tenant_id}").status_code == 204
    assert client.get(f"/api/v1/tenants/{tenant_id}").status_code == 404


def test_account_round_trip_never_echoes_the_secret(client):
    tenant_id = make_tenant(client)
    account = make_account(client, tenant_id)

    assert "secret" not in account
    assert account["secretMasked"].startswith("oauth:")
    assert "at-test" not in account["secretMasked"]
    # Metadata is lifted off the credential set, the credential itself is not.
    assert account["scopes"] == ["user:inference", "user:profile"]
    assert account["organizationName"] == "Acme"
    assert account["credentialExpiresAt"] is not None

    body = client.get(f"/api/v1/tenants/{tenant_id}/accounts/{account['id']}").text
    assert "at-test" not in body and "rt-test" not in body

    listing = client.get(f"/api/v1/tenants/{tenant_id}/accounts")
    assert [a["id"] for a in listing.json()["items"]] == [account["id"]]
    assert "at-test" not in listing.text

    patched = client.patch(
        f"/api/v1/tenants/{tenant_id}/accounts/{account['id']}", json={"status": "disabled"}
    )
    assert patched.json()["status"] == "disabled"

    assert client.delete(f"/api/v1/tenants/{tenant_id}/accounts/{account['id']}").status_code == 204


def test_duplicate_account_email_within_a_tenant_conflicts(client):
    tenant_id = make_tenant(client)
    make_account(client, tenant_id)
    duplicate = client.post(
        f"/api/v1/tenants/{tenant_id}/accounts",
        json={"email": "a@example.com", "credentialType": "oauth", "secret": CREDENTIAL_SET},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "account.duplicate_email"


def test_server_round_trip_and_enroll_token(client):
    tenant_id = make_tenant(client)
    server = make_server(client, tenant_id)
    assert server["enrolled"] is False
    assert server["assignedAccountCount"] == 0

    issued = client.post(
        f"/api/v1/tenants/{tenant_id}/servers/{server['id']}/enroll-token",
        json={"ttlSeconds": 600},
    )
    assert issued.status_code == 201
    token = issued.json()["token"]
    assert token

    # Only the hash is kept, so the plaintext cannot be read back.
    fetched = client.get(f"/api/v1/tenants/{tenant_id}/servers/{server['id']}").text
    assert token not in fetched


def test_assignment_creation_and_listing(client):
    tenant_id = make_tenant(client)
    account = make_account(client, tenant_id)
    server = make_server(client, tenant_id)

    created = client.post(
        f"/api/v1/tenants/{tenant_id}/assignments",
        json={"accountId": account["id"], "serverId": server["id"]},
    )
    assert created.status_code == 201
    assert created.json()["state"] == "pending"

    duplicate = client.post(
        f"/api/v1/tenants/{tenant_id}/assignments",
        json={"accountId": account["id"], "serverId": server["id"]},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "assignment.account_already_assigned"

    listed = client.get(f"/api/v1/tenants/{tenant_id}/assignments?serverId={server['id']}")
    assert len(listed.json()["items"]) == 1

    pinned = client.patch(
        f"/api/v1/tenants/{tenant_id}/assignments/{created.json()['id']}",
        json={"pinned": True},
    )
    assert pinned.json()["pinned"] is True

    # An assigned account cannot be deleted out from under its assignment.
    assert client.delete(f"/api/v1/tenants/{tenant_id}/accounts/{account['id']}").status_code == 409
    assert client.delete(f"/api/v1/tenants/{tenant_id}/servers/{server['id']}").status_code == 409


def test_deliver_immediately_is_refused_rather_than_ignored(client):
    tenant_id = make_tenant(client)
    account = make_account(client, tenant_id)
    server = make_server(client, tenant_id)
    response = client.post(
        f"/api/v1/tenants/{tenant_id}/assignments",
        json={
            "accountId": account["id"],
            "serverId": server["id"],
            "deliverImmediately": True,
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "assignment.deliver_immediately_unsupported"


# -- Cross-tenant -------------------------------------------------------------
def test_reading_another_tenants_resources_returns_404(client):
    tenant_a = make_tenant(client, "a-corp")
    tenant_b = make_tenant(client, "b-corp")
    account = make_account(client, tenant_a)
    server = make_server(client, tenant_a)

    assert client.get(f"/api/v1/tenants/{tenant_b}/accounts/{account['id']}").status_code == 404
    assert client.get(f"/api/v1/tenants/{tenant_b}/servers/{server['id']}").status_code == 404
    assert (
        client.patch(
            f"/api/v1/tenants/{tenant_b}/accounts/{account['id']}", json={"status": "disabled"}
        ).status_code
        == 404
    )
    assert client.delete(f"/api/v1/tenants/{tenant_b}/servers/{server['id']}").status_code == 404
    assert client.get(f"/api/v1/tenants/{tenant_b}/accounts").json()["items"] == []


def test_assigning_across_tenants_is_refused_by_the_service_layer(client):
    tenant_a = make_tenant(client, "a-corp")
    tenant_b = make_tenant(client, "b-corp")
    account_a = make_account(client, tenant_a)
    server_b = make_server(client, tenant_b, name="b-runner")

    response = client.post(
        f"/api/v1/tenants/{tenant_a}/assignments",
        json={"accountId": account_a["id"], "serverId": server_b["id"]},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "server.not_found"


def test_unknown_tenant_is_404_everywhere(client):
    missing = uuid.uuid4()
    assert client.get(f"/api/v1/tenants/{missing}/accounts").status_code == 404
    assert client.get(f"/api/v1/tenants/{missing}/servers").status_code == 404
    assert client.get(f"/api/v1/tenants/{missing}/assignments").status_code == 404


# -- P2 transition wiring -----------------------------------------------------
def test_transition_endpoints_enqueue_commands_and_advance_state(client):
    # deliver/recall/activate/deactivate now land (P2 track C): they enqueue a
    # signed command on the outbox and move the assignment. The gRPC session
    # process delivers it; the REST layer only records the intent + transition.
    tenant_id = make_tenant(client)
    account = make_account(client, tenant_id)
    server = make_server(client, tenant_id)
    assignment = client.post(
        f"/api/v1/tenants/{tenant_id}/assignments",
        json={"accountId": account["id"], "serverId": server["id"]},
    ).json()
    base = f"/api/v1/tenants/{tenant_id}/assignments/{assignment['id']}"

    deliver = client.post(f"{base}:deliver")
    assert deliver.status_code == 200
    assert deliver.json()["state"] == "delivering"
    assert deliver.json()["pendingCommandId"]

    # Wired, not stubbed: activate on a delivering row fails its precondition
    # with a 409, not a 501.
    assert client.post(f"{base}:activate").status_code == 409

    recall = client.post(f"{base}:recall")
    assert recall.status_code == 200
    assert recall.json()["state"] == "recalling"


def test_p3_switching_endpoints_are_wired(client):
    # P3 track AMS: recover / switch-now / refresh-usage / switch-mode now enqueue
    # commands instead of returning 501.
    tenant_id = make_tenant(client)
    account = make_account(client, tenant_id)
    server = make_server(client, tenant_id)
    assignment = client.post(
        f"/api/v1/tenants/{tenant_id}/assignments",
        json={"accountId": account["id"], "serverId": server["id"]},
    ).json()

    base = f"/api/v1/tenants/{tenant_id}/assignments/{assignment['id']}"
    # switch-now on a pending (not-yet-installed) assignment fails its precondition
    # with a 409 — wired, not a 501.
    assert client.post(f"{base}:switch-now").status_code == 409
    # recover requires quarantined; a pending assignment gets a 409.
    assert client.post(f"{base}:recover").status_code == 409

    server_base = f"/api/v1/tenants/{tenant_id}/servers/{server['id']}"
    # refresh-usage queues a RequestReport → 202 Accepted.
    assert client.post(f"{server_base}:refresh-usage").status_code == 202
    # switch-mode persists servers.switch_mode and returns the updated server.
    resp = client.post(f"{server_base}:switch-mode", json={"mode": "manual"})
    assert resp.status_code == 200
    assert resp.json()["switchMode"] == "manual"
    # Usage is a real read against the snapshot cache; nothing has reported yet.
    assert client.get(f"{server_base}/usage").status_code == 404


def test_server_policy_patch_persists_and_is_masked_none_by_default(client):
    tenant_id = make_tenant(client)
    server = make_server(client, tenant_id)
    server_base = f"/api/v1/tenants/{tenant_id}/servers/{server['id']}"
    # Fresh server: no central policy.
    assert server["thresholdPct"] is None
    assert server["defaultStrategy"] is None
    assert server["cooldownSeconds"] is None
    assert server["hysteresisPct"] is None
    # PATCH sets the O4-C + F4 (O4-B) policy columns; cooldown 0 is a real value.
    patched = client.patch(
        server_base,
        json={
            "thresholdPct": 90,
            "defaultStrategy": "best",
            "cooldownSeconds": 0,
            "hysteresisPct": 12.5,
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["thresholdPct"] == 90
    assert patched.json()["defaultStrategy"] == "best"
    assert patched.json()["cooldownSeconds"] == 0
    assert patched.json()["hysteresisPct"] == 12.5
    # A name-only PATCH leaves the whole policy untouched.
    renamed = client.patch(server_base, json={"name": "runner-renamed"})
    assert renamed.json()["thresholdPct"] == 90
    assert renamed.json()["defaultStrategy"] == "best"
    assert renamed.json()["cooldownSeconds"] == 0
    assert renamed.json()["hysteresisPct"] == 12.5
    # An explicit None clears just that field back to the local default.
    cleared = client.patch(server_base, json={"cooldownSeconds": None})
    assert cleared.json()["cooldownSeconds"] is None
    assert cleared.json()["hysteresisPct"] == 12.5


def test_stub_endpoints_do_not_leak_other_tenants_ids(client):
    tenant_a = make_tenant(client, "a-corp")
    tenant_b = make_tenant(client, "b-corp")
    account = make_account(client, tenant_a)
    server = make_server(client, tenant_a)
    assignment = client.post(
        f"/api/v1/tenants/{tenant_a}/assignments",
        json={"accountId": account["id"], "serverId": server["id"]},
    ).json()

    response = client.post(
        f"/api/v1/tenants/{tenant_b}/assignments/{assignment['id']}:deliver"
    )
    assert response.status_code == 404
