"""Central OAuth enrollment — docs/AMX-DESIGN.md §5.5.

The administrator logs in once, in the AMS console, and AMS keeps the complete
credential set. No AMA server ever performs a browser login.

Two properties of §7 are load-bearing here and are implemented in
`PkceFlowStore` rather than left to callers:

* the PKCE verifier lives only on the server, so only the session that created
  the authorize URL can redeem a code against it, and
* it is single-use — `take()` removes the flow before the token exchange runs,
  so a failed exchange burns the flow exactly as a successful one does, and a
  replayed code finds nothing to pair with.

The endpoint constants below are public values, identical to the ones tsamx
compiles in (`tsamx/src/tsamx/oauth.py:18-19`); they are not secrets.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

from app.core.errors import ApiError, bad_request

OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
OAUTH_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_SCOPES = "org:create_api_key user:profile user:inference"

_logger = logging.getLogger("ams.oauth")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@dataclass(frozen=True)
class PkceFlow:
    flow_id: str
    tenant_id: uuid.UUID
    verifier: str
    state: str
    expires_at: datetime
    label: str | None = None


class PkceFlowStore:
    """In-process, single-use store of pending enrollment flows.

    In-process is a deliberate P1 choice, not an oversight: §5.4 fixes P1 at a
    single AMS instance, and keeping verifiers out of the database means a
    database dump never contains one. A restart drops pending flows, which
    costs the administrator a repeated click.
    """

    def __init__(self) -> None:
        self._flows: dict[str, PkceFlow] = {}
        self._lock = threading.Lock()

    def create(self, tenant_id: uuid.UUID, ttl_seconds: int, label: str | None) -> PkceFlow:
        verifier = _b64url(secrets.token_bytes(32))
        flow = PkceFlow(
            flow_id="flow_" + secrets.token_urlsafe(16),
            tenant_id=tenant_id,
            verifier=verifier,
            state=_b64url(secrets.token_bytes(16)),
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
            label=label,
        )
        with self._lock:
            self._purge_expired()
            self._flows[flow.flow_id] = flow
        return flow

    def take(self, flow_id: str, tenant_id: uuid.UUID) -> PkceFlow:
        """Remove and return a live flow. Every exit path consumes it."""
        with self._lock:
            self._purge_expired()
            flow = self._flows.pop(flow_id, None)
        if flow is None:
            raise bad_request(
                "oauth.flow_not_found",
                "Unknown, already-used or expired enrollment flow. Start a new one.",
            )
        if flow.tenant_id != tenant_id:
            # Consumed above, so a probe across tenants also destroys the flow.
            raise bad_request(
                "oauth.flow_not_found",
                "Unknown, already-used or expired enrollment flow. Start a new one.",
            )
        return flow

    def _purge_expired(self) -> None:
        now = datetime.now(UTC)
        for flow_id in [fid for fid, f in self._flows.items() if f.expires_at <= now]:
            del self._flows[flow_id]

    def __len__(self) -> int:
        with self._lock:
            self._purge_expired()
            return len(self._flows)


def challenge_for(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode()).digest())


def authorize_url(flow: PkceFlow) -> str:
    params = {
        "code": "true",
        "client_id": OAUTH_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": OAUTH_REDIRECT_URI,
        "scope": OAUTH_SCOPES,
        "code_challenge": challenge_for(flow.verifier),
        "code_challenge_method": "S256",
        "state": flow.state,
    }
    return f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


def split_pasted_code(pasted: str) -> tuple[str, str | None]:
    """Split the console's `code#state` paste into its two parts.

    The callback page shows the code and the state joined by `#`; administrators
    paste the whole thing. A bare code is accepted too.
    """
    code, _, state = pasted.strip().partition("#")
    return code, (state or None)


def exchange_code(
    flow: PkceFlow, pasted_code: str, *, timeout_s: float, client: httpx.Client | None = None
) -> dict:
    """Trade an authorization code for the complete credential set.

    Returns the credential-set JSON in the same `claudeAiOauth` envelope that
    `claude login` writes — that identical shape is why an injected server
    cannot tell the credential from a local login (§5.5), and why the retired
    setup-token path failed (§2.4-5).
    """
    code, state = split_pasted_code(pasted_code)
    if not code:
        raise bad_request("oauth.missing_code", "No authorization code supplied.")

    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "client_id": OAUTH_CLIENT_ID,
        "code_verifier": flow.verifier,
        "state": state or flow.state,
    }

    owns_client = client is None
    client = client or httpx.Client(timeout=timeout_s)
    try:
        response = client.post(
            OAUTH_TOKEN_URL,
            json=payload,
            headers={"Content-Type": "application/json", "User-Agent": "ams-server/0.1"},
        )
    except httpx.HTTPError as exc:
        # Bare class name: an httpx error string can echo the request, and the
        # request body carries the code and the verifier (§7).
        _logger.warning("OAuth token exchange failed: %s", type(exc).__name__)
        raise ApiError(
            502, "Bad Gateway", "oauth.exchange_unreachable", "Token endpoint unreachable."
        ) from exc
    finally:
        if owns_client:
            client.close()

    if response.status_code >= 400:
        _logger.warning("OAuth token exchange rejected: HTTP %s", response.status_code)
        raise bad_request(
            "oauth.exchange_rejected",
            f"The token endpoint rejected the authorization code (HTTP {response.status_code}).",
        )

    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise bad_request(
            "oauth.exchange_malformed", "The token endpoint returned a non-JSON response."
        ) from exc

    return build_credential_set(body)


def build_credential_set(token_response: dict) -> dict:
    """Normalize a token-endpoint response into the credential-set envelope."""
    access_token = token_response.get("access_token")
    refresh_token = token_response.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise bad_request(
            "oauth.exchange_incomplete", "The token endpoint returned no access token."
        )
    if not isinstance(refresh_token, str) or not refresh_token:
        # A set without a refresh token is exactly the incomplete shape that
        # made interactive Claude Code demand a login (§2.4-5). Refuse it here
        # rather than store a credential that will fail on delivery.
        raise bad_request(
            "oauth.exchange_incomplete",
            "The token endpoint returned no refresh token; the credential set "
            "would be incomplete (§2.4-5).",
        )

    expires_in = token_response.get("expires_in")
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    expires_at = now_ms + int(expires_in) * 1000 if isinstance(expires_in, (int, float)) else None

    scope = token_response.get("scope")
    scopes = scope.split() if isinstance(scope, str) and scope else []

    account = token_response.get("account") if isinstance(token_response.get("account"), dict) else {}
    organization = (
        token_response.get("organization")
        if isinstance(token_response.get("organization"), dict)
        else {}
    )

    oauth: dict = {
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "scopes": scopes,
    }
    if expires_at is not None:
        oauth["expiresAt"] = expires_at
    for key, value in (
        ("accountUuid", account.get("uuid")),
        ("emailAddress", account.get("email_address") or account.get("email")),
        ("organizationUuid", organization.get("uuid")),
        ("organizationName", organization.get("name")),
        ("subscriptionType", token_response.get("subscription_type")),
    ):
        if isinstance(value, str) and value:
            oauth[key] = value

    return {"claudeAiOauth": oauth}


def email_from_credential_set(credential_set: dict) -> str | None:
    oauth = credential_set.get("claudeAiOauth", {})
    email = oauth.get("emailAddress")
    return email if isinstance(email, str) and email else None
