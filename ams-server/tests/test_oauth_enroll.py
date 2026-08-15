"""Central OAuth enrollment (§5.5) and the single-use verifier rule (§7).

The token endpoint is an `httpx.MockTransport` in every test here — nothing in
this file opens a socket to platform.claude.com.
"""

from __future__ import annotations

import json
import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.core.crypto import decrypt_secret
from app.services import oauth_enroll
from tests.test_api_crud import make_tenant

# P2a moved the per-provider endpoint constants off the module and into
# OAUTH_PROFILES; the OAuth start/complete flow defaults to the "claude"
# provider, so the tests assert against that profile's values.
CLAUDE_PROFILE = oauth_enroll.profile_for("claude")

TOKEN_RESPONSE = {
    "access_token": "at-live",
    "refresh_token": "rt-live",
    "expires_in": 3600,
    "scope": "user:inference user:profile",
    "account": {"uuid": "acct-uuid-1", "email_address": "owner@example.com"},
    "organization": {"uuid": "org-uuid-1", "name": "Acme Inc"},
    "subscription_type": "max",
}


def install_token_stub(app, *, response=None, status_code=200, recorder=None):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == CLAUDE_PROFILE.token_url
        if recorder is not None:
            recorder.append(json.loads(request.content))
        return httpx.Response(status_code, json=response if response is not None else TOKEN_RESPONSE)

    app.state.oauth_http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return app.state.oauth_http_client


def start_flow(client, tenant_id):
    response = client.post(f"/api/v1/tenants/{tenant_id}/accounts:oauth-start", json={})
    assert response.status_code == 201, response.text
    return response.json()


def test_oauth_start_returns_a_pkce_authorize_url(client, app):
    tenant_id = make_tenant(client)
    body = start_flow(client, tenant_id)

    parsed = urlparse(body["authorizeUrl"])
    params = parse_qs(parsed.query)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == CLAUDE_PROFILE.authorize_url
    assert params["client_id"] == [CLAUDE_PROFILE.client_id]
    assert params["code_challenge_method"] == ["S256"]
    assert params["response_type"] == ["code"]
    assert params["redirect_uri"] == [CLAUDE_PROFILE.redirect_uri]
    # The verifier itself must never appear in a URL the administrator handles.
    challenge = params["code_challenge"][0]
    assert challenge
    assert len(app.state.oauth_flows) == 1


def test_the_authorize_url_carries_the_challenge_for_the_stored_verifier(app):
    flow = app.state.oauth_flows.create(uuid.uuid4(), 600, None)
    params = parse_qs(urlparse(oauth_enroll.authorize_url(flow)).query)
    assert params["code_challenge"] == [oauth_enroll.challenge_for(flow.verifier)]


def test_complete_stores_the_full_credential_set_encrypted(client, app):
    tenant_id = make_tenant(client)
    sent = []
    install_token_stub(app, recorder=sent)
    flow = start_flow(client, tenant_id)

    response = client.post(
        f"/api/v1/tenants/{tenant_id}/accounts:oauth-complete",
        json={"flowId": flow["flowId"], "code": "auth-code-1#state-1"},
    )
    assert response.status_code == 201, response.text
    account = response.json()

    # The exchange used the code and the server-held verifier, as PKCE requires.
    assert sent[0]["grant_type"] == "authorization_code"
    assert sent[0]["code"] == "auth-code-1"
    assert sent[0]["code_verifier"]
    assert sent[0]["redirect_uri"] == CLAUDE_PROFILE.redirect_uri

    assert account["email"] == "owner@example.com"
    assert account["credentialType"] == "oauth"
    assert "at-live" not in response.text and "rt-live" not in response.text

    from app.db import get_sessionmaker
    from app.models import Account

    with get_sessionmaker()() as db:
        row = db.get(Account, uuid.UUID(account["id"]))
        stored = json.loads(
            decrypt_secret(row.encrypted_secret, tenant_id=row.tenant_id, db=db)
        )

    oauth = stored["claudeAiOauth"]
    assert oauth["accessToken"] == "at-live"
    assert oauth["refreshToken"] == "rt-live"
    assert oauth["expiresAt"] > 0
    assert oauth["scopes"] == ["user:inference", "user:profile"]
    assert oauth["accountUuid"] == "acct-uuid-1"
    assert oauth["organizationName"] == "Acme Inc"
    assert oauth["subscriptionType"] == "max"


def test_a_flow_cannot_be_completed_twice(client, app):
    tenant_id = make_tenant(client)
    install_token_stub(app)
    flow = start_flow(client, tenant_id)

    first = client.post(
        f"/api/v1/tenants/{tenant_id}/accounts:oauth-complete",
        json={"flowId": flow["flowId"], "code": "auth-code-1"},
    )
    assert first.status_code == 201

    replay = client.post(
        f"/api/v1/tenants/{tenant_id}/accounts:oauth-complete",
        json={"flowId": flow["flowId"], "code": "auth-code-1"},
    )
    assert replay.status_code == 400
    assert replay.json()["code"] == "oauth.flow_not_found"
    assert len(app.state.oauth_flows) == 0


def test_a_failed_exchange_still_burns_the_verifier(client, app):
    tenant_id = make_tenant(client)
    install_token_stub(app, response={"error": "invalid_grant"}, status_code=400)
    flow = start_flow(client, tenant_id)

    rejected = client.post(
        f"/api/v1/tenants/{tenant_id}/accounts:oauth-complete",
        json={"flowId": flow["flowId"], "code": "bad-code"},
    )
    assert rejected.status_code == 400
    assert rejected.json()["code"] == "oauth.exchange_rejected"

    retry = client.post(
        f"/api/v1/tenants/{tenant_id}/accounts:oauth-complete",
        json={"flowId": flow["flowId"], "code": "auth-code-1"},
    )
    assert retry.json()["code"] == "oauth.flow_not_found"
    assert len(app.state.oauth_flows) == 0


def test_another_tenant_cannot_redeem_a_flow(client, app):
    tenant_a = make_tenant(client, "a-corp")
    tenant_b = make_tenant(client, "b-corp")
    install_token_stub(app)
    flow = start_flow(client, tenant_a)

    stolen = client.post(
        f"/api/v1/tenants/{tenant_b}/accounts:oauth-complete",
        json={"flowId": flow["flowId"], "code": "auth-code-1"},
    )
    assert stolen.status_code == 400
    assert stolen.json()["code"] == "oauth.flow_not_found"
    # Consumed by the probe, so the rightful tenant cannot redeem it either.
    assert len(app.state.oauth_flows) == 0


def test_an_expired_flow_is_gone(client, app):
    tenant_id = make_tenant(client)
    install_token_stub(app)
    flow = app.state.oauth_flows.create(uuid.UUID(tenant_id), 0, None)

    response = client.post(
        f"/api/v1/tenants/{tenant_id}/accounts:oauth-complete",
        json={"flowId": flow.flow_id, "code": "auth-code-1"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "oauth.flow_not_found"


def test_a_response_without_a_refresh_token_is_refused(client, app):
    """The retired setup-token shape must not be storable (§2.4-5)."""
    tenant_id = make_tenant(client)
    install_token_stub(
        app,
        response={"access_token": "at-only", "expires_in": 3600, "scope": "user:inference"},
    )
    flow = start_flow(client, tenant_id)

    response = client.post(
        f"/api/v1/tenants/{tenant_id}/accounts:oauth-complete",
        json={"flowId": flow["flowId"], "code": "auth-code-1"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "oauth.exchange_incomplete"


def test_email_override_wins_over_the_credential_set(client, app):
    tenant_id = make_tenant(client)
    install_token_stub(app)
    flow = start_flow(client, tenant_id)

    response = client.post(
        f"/api/v1/tenants/{tenant_id}/accounts:oauth-complete",
        json={"flowId": flow["flowId"], "code": "c", "email": "override@example.com"},
    )
    assert response.json()["email"] == "override@example.com"


def test_a_422_on_oauth_complete_never_echoes_the_code(client):
    """The authorization code is single-use material; a 422 must not carry it."""
    tenant_id = make_tenant(client)
    canary = "auth-code-canary-7b2e4d"
    response = client.post(
        f"/api/v1/tenants/{tenant_id}/accounts:oauth-complete",
        json={"code": canary},  # flowId missing
    )
    assert response.status_code == 422
    assert canary not in response.text
    body = response.json()
    assert body["code"] == "request.invalid"
    assert [e["loc"] for e in body["errors"]] == [["body", "flowId"]]


def test_a_422_on_oauth_start_never_echoes_the_body(client):
    tenant_id = make_tenant(client)
    canary = "label-canary-3c9f1a"
    response = client.post(
        f"/api/v1/tenants/{tenant_id}/accounts:oauth-start",
        json={"label": {"unexpected": canary}},
    )
    assert response.status_code == 422
    assert canary not in response.text


def test_build_credential_set_requires_an_access_token():
    from app.core.errors import ApiError

    with pytest.raises(ApiError) as exc:
        oauth_enroll.build_credential_set({"refresh_token": "rt"})
    assert exc.value.code == "oauth.exchange_incomplete"


def test_split_pasted_code_handles_both_forms():
    assert oauth_enroll.split_pasted_code(" code#state ") == ("code", "state")
    assert oauth_enroll.split_pasted_code("code") == ("code", None)
