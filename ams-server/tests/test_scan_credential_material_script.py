"""`scripts/scan_credential_material.py` must find a poisoned row and disclose nothing."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

from tests.test_api_crud import make_tenant

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "scan_credential_material.py"

HEALTHY = '{"claudeAiOauth": {"accessToken": "at-live-xyz", "refreshToken": "rt-live-xyz"}}'
# The shape observed on 2026-08-17: the keys survive, the tokens do not.
POISONED = '{"claudeAiOauth": {"accessToken": "", "refreshToken": "", "expiresAt": 0}}'


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=dict(os.environ, PYTHONPATH=str(ROOT)),
    )


def _add_account(client, tenant_id: str, email: str, secret: str) -> str:
    return client.post(
        f"/api/v1/tenants/{tenant_id}/accounts",
        json={"email": email, "credentialType": "oauth", "secret": secret},
    ).json()["id"]


def test_a_healthy_credential_passes_without_printing_the_token(client):
    tenant_id = make_tenant(client)
    _add_account(client, tenant_id, "healthy@example.com", HEALTHY)

    result = _run("--tenant", tenant_id)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: PASS" in result.stdout
    assert "carries token material: 1" in result.stdout

    # --all shows the shape of a healthy row: presence and length, never content.
    verbose = _run("--tenant", tenant_id, "--all").stdout
    assert "accessToken=len11, refreshToken=len11" in verbose
    for disclosed in ("at-live-xyz", "rt-live-xyz", "healthy@example.com"):
        assert disclosed not in verbose


def test_an_emptied_credential_is_reported_and_fails(client):
    tenant_id = make_tenant(client)
    account_id = _add_account(client, tenant_id, "shell@example.com", POISONED)

    result = _run("--tenant", tenant_id)
    assert result.returncode == 1, result.stdout + result.stderr
    out = result.stdout
    assert "RESULT: FAIL" in out
    assert "POISONED (the re-sync guard would refuse this set): 1" in out
    assert account_id in out
    assert "accessToken=BLANK, refreshToken=BLANK" in out
    # The remedy has to be in the output; an operator reading only this must know
    # a re-assignment would deliver a dead credential.
    assert "Re-enrol" in out


def test_emails_are_printed_only_when_asked(client):
    tenant_id = make_tenant(client)
    _add_account(client, tenant_id, "named@example.com", POISONED)

    assert "named@example.com" not in _run("--tenant", tenant_id).stdout
    assert "named@example.com" in _run("--tenant", tenant_id, "--emails").stdout
