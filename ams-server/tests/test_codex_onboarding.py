"""Codex account onboarding — credential import checks and the per-server cap.

The credential checks are pure functions over a string, so they are also
runnable without the PostgreSQL container the rest of the suite needs:

    python tests/test_codex_onboarding.py

That path runs only the DB-free half. The assignment-cap tests below need the
`client` fixture and therefore the container, same as the rest of the suite.
"""

from __future__ import annotations

import base64
import contextlib
import json

try:
    import pytest
except ModuleNotFoundError:  # standalone run on an env without the dev extras
    class _Caught:
        value: BaseException | None = None

    class pytest:  # noqa: N801 — stands in for the module, same call shape
        @staticmethod
        @contextlib.contextmanager
        def raises(expected):
            caught = _Caught()
            try:
                yield caught
            except expected as exc:
                caught.value = exc
            else:
                raise AssertionError(f"expected {expected.__name__}, nothing raised")

from app.core.errors import ApiError
from app.models import Account
from app.services import inventory


def _id_token(claims: dict) -> str:
    """A JWT-shaped id_token with `claims` as its payload; header/signature are
    filler because nothing verifies the signature (by design)."""
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{body}.not-a-real-signature"


def codex_auth_json(email: str = "ops@example.com", **overrides) -> str:
    tokens = {
        "id_token": _id_token({"email": email, "sub": "user-1"}),
        "access_token": "at-codex",
        "refresh_token": "rt-codex",
        "account_id": "acct_01HZZZ",
    }
    tokens.update(overrides.pop("tokens", {}))
    payload = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": tokens,
        "last_refresh": "2026-08-12T00:00:00Z",
    }
    payload.update(overrides)
    return json.dumps(payload)


def _codex_account(email: str = "ops@example.com") -> Account:
    # Detached instance: the metadata pass touches columns only, never a session.
    return Account(provider="codex", email=email)


# -- credential import (no database) ------------------------------------------
def test_a_well_formed_auth_json_is_accepted():
    secret = codex_auth_json()
    inventory._validate_codex_secret(secret)
    account = _codex_account()
    inventory._apply_credential_metadata(account, secret)
    assert account.account_uuid == "acct_01HZZZ"


def test_a_credential_without_a_refresh_token_is_rejected():
    secret = json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "at"}})
    with pytest.raises(ApiError) as caught:
        inventory._validate_codex_secret(secret)
    assert caught.value.status == 400
    assert caught.value.code == "account.codex_credential_invalid"
    assert "tokens.refresh_token" in caught.value.detail


def test_an_empty_refresh_token_is_rejected_like_a_missing_one():
    secret = codex_auth_json(tokens={"refresh_token": "   "})
    with pytest.raises(ApiError) as caught:
        inventory._validate_codex_secret(secret)
    assert caught.value.code == "account.codex_credential_invalid"


def test_a_credential_that_is_not_json_is_rejected():
    with pytest.raises(ApiError) as caught:
        inventory._validate_codex_secret("sk-not-a-json-file")
    assert caught.value.status == 400
    assert caught.value.code == "account.codex_credential_invalid"


def test_a_json_array_is_rejected_as_not_an_auth_json():
    with pytest.raises(ApiError) as caught:
        inventory._validate_codex_secret('["tokens"]')
    assert caught.value.code == "account.codex_credential_invalid"


def test_an_oversized_credential_is_rejected_before_parsing():
    padded = json.dumps({"tokens": {"refresh_token": "rt"}, "pad": "x" * (64 * 1024)})
    with pytest.raises(ApiError) as caught:
        inventory._validate_codex_secret(padded)
    assert caught.value.code == "account.codex_credential_invalid"
    assert "limit" in caught.value.detail


def test_an_auth_json_for_a_different_mailbox_is_rejected():
    secret = codex_auth_json(email="someone-else@example.com")
    with pytest.raises(ApiError) as caught:
        inventory._apply_credential_metadata(_codex_account("ops@example.com"), secret)
    assert caught.value.status == 400
    assert caught.value.code == "account.codex_email_mismatch"


def test_the_email_comparison_ignores_case():
    secret = codex_auth_json(email="OPS@Example.com")
    account = _codex_account("ops@example.com")
    inventory._apply_credential_metadata(account, secret)
    assert account.account_uuid == "acct_01HZZZ"


def test_an_unparseable_id_token_skips_the_comparison():
    # Convenience extraction only — an id_token AMS cannot read is not grounds
    # to refuse a credential whose refresh token is present and well formed.
    secret = codex_auth_json(tokens={"id_token": "not.a.jwt"})
    account = _codex_account("ops@example.com")
    inventory._apply_credential_metadata(account, secret)
    assert account.account_uuid == "acct_01HZZZ"


def test_deeply_nested_json_is_a_400_not_a_recursion_500():
    # Comfortably under the 64 KiB cap, deep enough to exhaust the parser's
    # recursion limit. RecursionError is not a ValueError, so this reaches the
    # caller as a 500 unless it is caught explicitly.
    nested = "[" * 20000 + "]" * 20000
    with pytest.raises(ApiError) as caught:
        inventory._validate_codex_secret(nested)
    assert caught.value.status == 400
    assert caught.value.code == "account.codex_credential_invalid"


def test_a_lone_surrogate_is_a_400_not_an_encoding_500():
    # A real lone surrogate in the secret string — which is what pydantic hands
    # us when a request body carries the "\ud800" escape. It explodes the strict
    # .encode() inside encrypt_secret/mask_secret, which run after validation.
    secret = '{"tokens": {"refresh_token": "rt-\ud800-bad"}}'
    with pytest.raises(ApiError) as caught:
        inventory._validate_codex_secret(secret)
    assert caught.value.status == 400
    assert caught.value.code == "account.codex_credential_invalid"
    # The guarantee the check exists to make: what it accepts, crypto can encode.
    inventory._validate_codex_secret(codex_auth_json())
    codex_auth_json().encode("utf-8")


def test_json_extension_literals_are_rejected():
    # Python's parser accepts NaN/Infinity; Go's encoding/json — which reads the
    # staged auth.json on the agent — does not.
    for literal in ("NaN", "Infinity", "-Infinity"):
        secret = '{"tokens": {"refresh_token": "SECRET-RT", "skew": %s}}' % literal
        with pytest.raises(ApiError) as caught:
            inventory._validate_codex_secret(secret)
        assert caught.value.code == "account.codex_credential_invalid", literal
        assert literal in caught.value.detail
        assert "SECRET-RT" not in caught.value.detail


def test_no_error_message_ever_quotes_the_credential():
    secret = codex_auth_json(email="someone-else@example.com")
    details = []
    for bad in ("not-json", '["x"]', json.dumps({"tokens": {}}), "x" * (65 * 1024)):
        with pytest.raises(ApiError) as caught:
            inventory._validate_codex_secret(bad)
        details.append(caught.value.detail)
    with pytest.raises(ApiError) as caught:
        inventory._apply_credential_metadata(_codex_account(), secret)
    details.append(caught.value.detail)
    for detail in details:
        assert "rt-codex" not in detail
        assert "at-codex" not in detail
        assert "someone-else@example.com" not in detail
        assert "eyJ" not in detail


# -- registration and the per-server cap (needs the container) -----------------
def _make_tenant(client):
    response = client.post("/api/v1/tenants", json={"name": "codex-tenant"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _make_codex_account(client, tenant_id, email="ops@example.com"):
    return client.post(
        f"/api/v1/tenants/{tenant_id}/accounts",
        json={
            "email": email,
            "provider": "codex",
            "credentialType": "oauth",
            "secret": codex_auth_json(email),
            "owner": "platform-team",
        },
    )


def _make_server(client, tenant_id, name):
    response = client.post(f"/api/v1/tenants/{tenant_id}/servers", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_a_codex_account_registers_with_its_owner_label(client):
    tenant_id = _make_tenant(client)
    response = _make_codex_account(client, tenant_id)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["provider"] == "codex"
    assert body["owner"] == "platform-team"
    assert body["accountUuid"] == "acct_01HZZZ"
    assert "secret" not in body


def test_a_malformed_codex_credential_is_a_400_at_the_api(client):
    tenant_id = _make_tenant(client)
    response = client.post(
        f"/api/v1/tenants/{tenant_id}/accounts",
        json={
            "email": "ops@example.com",
            "provider": "codex",
            "credentialType": "oauth",
            "secret": "not-an-auth-json",
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "account.codex_credential_invalid"


def test_oauth_start_refuses_codex_and_points_at_the_import_path(client):
    tenant_id = _make_tenant(client)
    response = client.post(
        f"/api/v1/tenants/{tenant_id}/accounts:oauth-start", json={"provider": "codex"}
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == "oauth.provider_unsupported"
    assert "POST /accounts" in body["detail"]


def test_a_server_takes_only_one_codex_account(client):
    tenant_id = _make_tenant(client)
    server_id = _make_server(client, tenant_id, "codex-runner")
    first = _make_codex_account(client, tenant_id, "one@example.com").json()
    second = _make_codex_account(client, tenant_id, "two@example.com").json()

    ok = client.post(
        f"/api/v1/tenants/{tenant_id}/assignments",
        json={"accountId": first["id"], "serverId": server_id},
    )
    assert ok.status_code == 201, ok.text

    blocked = client.post(
        f"/api/v1/tenants/{tenant_id}/assignments",
        json={"accountId": second["id"], "serverId": server_id},
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["code"] == "assignment.server_codex_capacity"

    # The cap is per server, not per tenant: a second host takes it happily.
    other_server = _make_server(client, tenant_id, "codex-runner-2")
    elsewhere = client.post(
        f"/api/v1/tenants/{tenant_id}/assignments",
        json={"accountId": second["id"], "serverId": other_server},
    )
    assert elsewhere.status_code == 201, elsewhere.text


def test_claude_accounts_still_share_a_server(client):
    from tests.test_api_crud import CREDENTIAL_SET

    tenant_id = _make_tenant(client)
    server_id = _make_server(client, tenant_id, "claude-runner")
    for email in ("a@example.com", "b@example.com"):
        account = client.post(
            f"/api/v1/tenants/{tenant_id}/accounts",
            json={"email": email, "credentialType": "oauth", "secret": CREDENTIAL_SET},
        )
        assert account.status_code == 201, account.text
        assigned = client.post(
            f"/api/v1/tenants/{tenant_id}/assignments",
            json={"accountId": account.json()["id"], "serverId": server_id},
        )
        assert assigned.status_code == 201, assigned.text


def test_changing_a_codex_email_without_its_credential_is_refused(client):
    tenant_id = _make_tenant(client)
    account = _make_codex_account(client, tenant_id, "ops@example.com").json()

    bare = client.patch(
        f"/api/v1/tenants/{tenant_id}/accounts/{account['id']}",
        json={"email": "someone-else@example.com"},
    )
    assert bare.status_code == 400, bare.text
    assert bare.json()["code"] == "account.codex_email_requires_credential"

    # Unchanged email, and non-email edits, stay allowed.
    same = client.patch(
        f"/api/v1/tenants/{tenant_id}/accounts/{account['id']}",
        json={"email": "ops@example.com", "owner": "sre"},
    )
    assert same.status_code == 200, same.text

    # With the matching credential the move is allowed and re-verified...
    moved = client.patch(
        f"/api/v1/tenants/{tenant_id}/accounts/{account['id']}",
        json={
            "email": "someone-else@example.com",
            "secret": codex_auth_json("someone-else@example.com"),
        },
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["email"] == "someone-else@example.com"

    # ...and a credential that does not match the new email is still refused.
    mismatched = client.patch(
        f"/api/v1/tenants/{tenant_id}/accounts/{account['id']}",
        json={"email": "third@example.com", "secret": codex_auth_json("ops@example.com")},
    )
    assert mismatched.status_code == 400, mismatched.text
    assert mismatched.json()["code"] == "account.codex_email_mismatch"


def test_claude_email_edits_are_untouched_by_the_codex_rule(client):
    from tests.test_api_crud import CREDENTIAL_SET

    tenant_id = _make_tenant(client)
    account = client.post(
        f"/api/v1/tenants/{tenant_id}/accounts",
        json={"email": "a@example.com", "credentialType": "oauth", "secret": CREDENTIAL_SET},
    ).json()
    renamed = client.patch(
        f"/api/v1/tenants/{tenant_id}/accounts/{account['id']}",
        json={"email": "renamed@example.com"},
    )
    assert renamed.status_code == 200, renamed.text


# -- recall must purge for codex (F1) ------------------------------------------
def _delivered_assignment(db, provider, email, secret):
    """A tenant/account/server with one assignment parked in `active`."""
    from app.services import inventory as inv

    tenant = inv.create_tenant(db, f"recall-{provider}-{uuid.uuid4().hex[:6]}")
    account = inv.create_account(
        db, tenant.id, email=email, credential_type="oauth", secret=secret, provider=provider
    )
    server = inv.create_server(
        db, tenant.id, name=f"host-{uuid.uuid4().hex[:6]}", hostname=None, switch_mode="auto"
    )
    assignment = inv.create_assignment(
        db, tenant.id, account_id=account.id, server_id=server.id, pinned=False
    )
    assignment.state = "active"
    db.commit()
    return tenant, account, assignment


def _last_recall_payload(db, assignment_id):
    from sqlalchemy import select

    from app.models import AgentCommand

    row = db.scalars(
        select(AgentCommand)
        .where(AgentCommand.assignment_id == assignment_id, AgentCommand.command_type == "recall")
        .order_by(AgentCommand.created_at.desc())
    ).first()
    assert row is not None, "no recall command was enqueued"
    return row.payload


def test_recalling_a_codex_account_purges_the_local_copy(db):
    # Without the purge the agent keeps its identity sidecar, and its Add then
    # refuses every OTHER account on that host (codex_single_account) while the
    # server-side cap — seeing only a detached row — waves the next one through.
    from app.services import commands

    tenant, _account, assignment = _delivered_assignment(
        db, "codex", "ops@example.com", codex_auth_json("ops@example.com")
    )
    commands.request_recall(db, tenant.id, assignment.id)
    assert _last_recall_payload(db, assignment.id)["purge_local_copy"] is True


def test_recalling_a_claude_account_still_preserves_the_local_copy(db):
    from app.services import commands
    from tests.test_api_crud import CREDENTIAL_SET

    tenant, _account, assignment = _delivered_assignment(
        db, "claude", "a@example.com", CREDENTIAL_SET
    )
    commands.request_recall(db, tenant.id, assignment.id)
    assert _last_recall_payload(db, assignment.id)["purge_local_copy"] is False


def test_a_recalled_server_accepts_a_different_codex_account(client):
    # The end-to-end shape of F1: recall to detached, then re-assign someone else.
    tenant_id = _make_tenant(client)
    server_id = _make_server(client, tenant_id, "codex-runner")
    first = _make_codex_account(client, tenant_id, "one@example.com").json()
    second = _make_codex_account(client, tenant_id, "two@example.com").json()

    created = client.post(
        f"/api/v1/tenants/{tenant_id}/assignments",
        json={"accountId": first["id"], "serverId": server_id},
    ).json()
    blocked = client.post(
        f"/api/v1/tenants/{tenant_id}/assignments",
        json={"accountId": second["id"], "serverId": server_id},
    )
    assert blocked.status_code == 409

    recalled = client.post(
        f"/api/v1/tenants/{tenant_id}/assignments/{created['id']}:recall"
    )
    assert recalled.status_code == 200, recalled.text
    # The command the agent will receive is the purging form, so the sidecar
    # goes with it and the host is genuinely free.
    assignment_id = created["id"]
    from app.db import get_sessionmaker

    with get_sessionmaker()() as session:
        assert _last_recall_payload(session, uuid.UUID(assignment_id))["purge_local_copy"] is True


# -- the per-server cap survives concurrency (A4) ------------------------------
def test_two_simultaneous_codex_assignments_cannot_both_win(client):
    """The cap is check-then-insert, so it needs the FOR UPDATE row lock.

    A second session is made to attempt the create while the server row is held
    locked. If the lock were dropped from create_assignment the attempt would
    finish immediately against a stale count and both assignments would exist;
    with it, the attempt blocks until this test commits and then loses with 409.
    """
    import threading

    from sqlalchemy import select

    from app.db import get_sessionmaker
    from app.models import Server
    from app.services import inventory as inv

    tenant_id = _make_tenant(client)
    server_id = uuid.UUID(_make_server(client, tenant_id, "codex-runner"))
    first = uuid.UUID(_make_codex_account(client, tenant_id, "one@example.com").json()["id"])
    second = uuid.UUID(_make_codex_account(client, tenant_id, "two@example.com").json()["id"])
    tenant_uuid = uuid.UUID(tenant_id)

    outcome: dict[str, object] = {}
    started = threading.Event()

    def contender():
        started.set()
        with get_sessionmaker()() as session:
            try:
                inv.create_assignment(
                    session,
                    tenant_uuid,
                    account_id=second,
                    server_id=server_id,
                    pinned=False,
                )
                outcome["result"] = "created"
            except ApiError as exc:
                outcome["result"] = exc.code
            except Exception as exc:  # pragma: no cover - surfaced in the assert
                outcome["result"] = f"error:{exc!r}"

    holder = get_sessionmaker()()
    try:
        # Hold the server row exactly as create_assignment does.
        holder.execute(select(Server.id).where(Server.id == server_id).with_for_update())
        thread = threading.Thread(target=contender, daemon=True)
        thread.start()
        started.wait(timeout=5)
        thread.join(timeout=2.0)
        assert thread.is_alive(), (
            "the second create finished while the server row was locked — "
            "create_assignment is not taking the FOR UPDATE lock"
        )
        # Now let the first assignment land and release the lock.
        inv.create_assignment(
            holder, tenant_uuid, account_id=first, server_id=server_id, pinned=False
        )
    finally:
        holder.close()

    thread.join(timeout=15)
    assert not thread.is_alive(), "the blocked create never resumed"
    assert outcome["result"] == "assignment.server_codex_capacity", outcome


if __name__ == "__main__":
    # Everything that takes no fixture argument is a pure check and runs here;
    # the rest need the PostgreSQL container and are left to pytest.
    import inspect

    ran = 0
    for _name, _case in list(globals().items()):
        if not _name.startswith("test_") or not callable(_case):
            continue
        if inspect.signature(_case).parameters:
            continue
        _case()
        ran += 1
        print(f"ok  {_name}")
    print(f"{ran} database-free checks passed")
