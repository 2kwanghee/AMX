"""`scripts/verify_credential.py` must answer without disclosing anything."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

from tests.test_api_crud import make_tenant
from tests.test_oauth_enroll import install_token_stub, start_flow

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "verify_credential.py"


def _run(tenant_id: str, account_id: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--tenant", tenant_id, "--account", account_id],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=dict(os.environ, PYTHONPATH=str(ROOT)),
    )


def test_it_reports_a_complete_credential_set_without_printing_it(client, app):
    tenant_id = make_tenant(client)
    install_token_stub(app)
    flow = start_flow(client, tenant_id)
    account = client.post(
        f"/api/v1/tenants/{tenant_id}/accounts:oauth-complete",
        json={"flowId": flow["flowId"], "code": "auth-code-1"},
    ).json()

    result = _run(tenant_id, account["id"])
    assert result.returncode == 0, result.stderr
    out = result.stdout

    for line in (
        "decrypts: true",
        "accessToken: true",
        "refreshToken: true",
        "expiresAt: true",
        "scopes: true",
        "accountUuid: true",
        "credential_set_complete: true",
    ):
        assert line in out, out

    # The whole point: presence, never values.
    for secret in ("at-live", "rt-live", "owner@example.com", "Acme Inc"):
        assert secret not in out


def test_an_incomplete_set_is_reported_as_incomplete(client):
    tenant_id = make_tenant(client)
    account = client.post(
        f"/api/v1/tenants/{tenant_id}/accounts",
        json={
            "email": "partial@example.com",
            "credentialType": "oauth",
            "secret": '{"claudeAiOauth": {"accessToken": "at-only", "scopes": []}}',
        },
    ).json()

    result = _run(tenant_id, account["id"])
    assert result.returncode == 2
    assert "refreshToken: false" in result.stdout
    assert "credential_set_complete: false" in result.stdout
    assert "at-only" not in result.stdout


def test_an_unknown_account_is_reported_not_found(client):
    import uuid

    tenant_id = make_tenant(client)
    result = _run(tenant_id, str(uuid.uuid4()))
    assert result.returncode == 2
    assert "account_found: false" in result.stdout
