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


def test_the_configured_token_is_accepted(client):
    response = client.get(
        "/api/v1/tenants", headers={"Authorization": f"Bearer {TEST_ADMIN_TOKEN}"}
    )
    assert response.status_code == 200


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


# -- P2 stubs -----------------------------------------------------------------
def test_state_transition_endpoints_report_501(client):
    tenant_id = make_tenant(client)
    account = make_account(client, tenant_id)
    server = make_server(client, tenant_id)
    assignment = client.post(
        f"/api/v1/tenants/{tenant_id}/assignments",
        json={"accountId": account["id"], "serverId": server["id"]},
    ).json()

    base = f"/api/v1/tenants/{tenant_id}/assignments/{assignment['id']}"
    for action in ("deliver", "recall", "activate", "deactivate", "recover", "switch-now"):
        response = client.post(f"{base}:{action}")
        assert response.status_code == 501, action
        assert "P2" in response.json()["detail"]

    server_base = f"/api/v1/tenants/{tenant_id}/servers/{server['id']}"
    assert client.post(f"{server_base}:refresh-usage").status_code == 501
    assert client.post(f"{server_base}:switch-mode", json={"mode": "manual"}).status_code == 501
    # Usage is a real read against the snapshot cache; nothing has reported yet.
    assert client.get(f"{server_base}/usage").status_code == 404


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
