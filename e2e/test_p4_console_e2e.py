"""P4 "console operates the whole lifecycle" completion criterion, end to end
(design note §9, p4-architecture §6/§7).

This is the integration Track A (alerts backend) and Track B (ams-web BFF/UI)
were built toward but never wired together: it drives the **real ams-web BFF**
Route Handlers — login -> session cookie, then the `[...path]` proxy with its
allowlist and server-side admin Bearer — against a **live ams-server** (REST over
HTTP + the P3 gRPC control plane + a compiled ``ama`` daemon), and proves the
full account lifecycle and the alert round trip flow through the console exactly
as the browser would reach them.

What is real: the FastAPI app over a real port, the gRPC session, a live agent
with the real tsamx CLI, the alert opened in PostgreSQL off a live
``all_exhausted`` switch event, and the BFF's own security code (path allowlist,
token attach, header hygiene, HMAC session). The alert is induced with the P3
recipe (two accounts delivered active, usage preseeded at 100/100, auto mode ->
a real ``auto --once`` tick emits ``KIND_ALL_EXHAUSTED``) and resolved by a
console ``:refresh-usage`` after the preseed drops below threshold. Same offline
promise as P2/P3: synthetic credentials, a dead HTTPS proxy, no request to
Anthropic.

The admin-token-never-reaches-the-browser invariant (Track B's four suites) is
re-asserted here against the live server: every BFF response in the run is
scanned for the admin-token sentinel — body, headers and Set-Cookie — and the
login cookie is checked never to be the token itself.

Run: ``AMX_GO_BIN=/path/to/go uv run --project ams-server pytest \
e2e/test_p4_console_e2e.py -q`` (needs Docker, a Go toolchain, uv, and Node).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

from conftest import TEST_ADMIN_TOKEN, AgentHost
from test_p3_switching_e2e import OFFLINE_ENV, mock_oauth_secret, preseed_usage

REPO_ROOT = Path(__file__).resolve().parent.parent
E2E_DIR = Path(__file__).resolve().parent
AMS_WEB_SRC = REPO_ROOT / "ams-web" / "src"

# The BFF's env. AMX_SESSION_SECRET must be >= 16 chars (env.ts guard). Since F1
# S2c the BFF login is email+password against ams-server's /auth/login (per-admin
# session token), so the console signs in as a real global-admin created through
# the bootstrap root token — not a shared password. The admin credential is a
# distinct secret from the root admin token, so a leak of one is not masked by
# the other.
SESSION_SECRET = "p4-e2e-session-secret-0123456789"
CONSOLE_EMAIL = "console-admin@p4.e2e.example"
CONSOLE_PASSWORD = "p4-console-password"


def _create_console_admin(rest_base: str) -> None:
    """Provision the global-admin the console logs in as.

    Created directly against ams-server with the bootstrap root token
    (``AMX_ADMIN_TOKEN``, the upstream M2M path) — the one way to mint the first
    admin, since ``POST /admins`` is global-admin-only. The console then signs in
    through the BFF with this admin's email+password (the S2c login contract).
    """
    resp = httpx.post(
        f"{rest_base}/admins",
        json={
            "email": CONSOLE_EMAIL,
            "password": CONSOLE_PASSWORD,
            "role": "global-admin",
        },
        headers={"Authorization": f"Bearer {TEST_ADMIN_TOKEN}"},
        timeout=30.0,
    )
    resp.raise_for_status()

CONVERGENCE_TIMEOUT_S = 120.0
ALERT_TIMEOUT_S = 60.0
POLL_INTERVAL_S = 1.0


class BffError(AssertionError):
    pass


def _bff(rest_base: str, steps: list[dict]) -> dict:
    """Run one BFF job through the real Route Handlers and return its result.

    Each call logs in fresh (cheap, stateless HMAC) and then runs ``steps``. The
    Node runner imports the actual ams-web handlers; nothing here re-implements
    BFF logic.
    """
    node = shutil.which("node")
    if node is None:  # pragma: no cover - environment guard
        pytest.skip("Node is required to drive the ams-web BFF for the P4 E2E")
    env = dict(os.environ)
    env.update(
        {
            "AMS_WEB_SRC": str(AMS_WEB_SRC),
            "AMX_API_BASE": rest_base,
            "AMX_ADMIN_TOKEN": TEST_ADMIN_TOKEN,
            "AMX_SESSION_SECRET": SESSION_SECRET,
            "AMX_CONSOLE_EMAIL": CONSOLE_EMAIL,
            "AMX_CONSOLE_PASSWORD": CONSOLE_PASSWORD,
        }
    )
    proc = subprocess.run(
        [node, str(E2E_DIR / "bff_runner.mjs")],
        input=json.dumps({"steps": steps}),
        env=env,
        cwd=str(E2E_DIR),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise BffError(f"bff runner failed:\n{proc.stderr}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - diagnosis aid
        raise BffError(f"bff runner returned non-JSON:\n{proc.stdout}\n{proc.stderr}") from exc


def _one(rest_base: str, method: str, path: str, body=None, *, expect=(200, 201, 202)) -> dict:
    """Run a single BFF step and assert it succeeded with no token leak."""
    step = {"method": method, "path": path}
    if body is not None:
        step["body"] = body
    out = _bff(rest_base, [step])
    assert out["login"]["status"] == 200, f"login failed: {out['login']}"
    assert out["login"]["leaked"] is False, "admin token leaked in login response"
    result = out["results"][0]
    assert result["status"] in expect, f"{method} {path} -> {result['status']}: {result.get('json') or result.get('bodyText')}"
    assert result["leaked"] is False, f"admin token leaked in {method} {path} response"
    return result


def _poll(rest_base: str, path: str, ready, timeout_s: float):
    """Poll a BFF GET until ``ready(json)`` is truthy; return that json or None."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = _one(rest_base, "GET", path)
        if ready(result["json"]):
            return result["json"]
        time.sleep(POLL_INTERVAL_S)
    return None


@pytest.fixture(scope="module")
def console(rest_server, grpc_server, signing_keys, tsamx_bin, ama_binary, workdir):
    """A live server with a delivered, exhausted two-account pool, reached only
    through the BFF. Yields the context the test drives the console with."""
    if shutil.which("node") is None:  # pragma: no cover - environment guard
        pytest.skip("Node is required to drive the ams-web BFF for the P4 E2E")
    log_dir = workdir / "logs"
    log_dir.mkdir(exist_ok=True)

    # The console signs in as a real global-admin (S2c email+password login);
    # mint it via the root token before any BFF login can run.
    _create_console_admin(rest_server)

    # -- Console provisioning, entirely through the BFF ----------------------
    tenant = _one(rest_server, "POST", "tenants", {"name": "p4-" + uuid.uuid4().hex[:8]})["json"]
    tid = tenant["id"]

    emails = ["a@p4.e2e.example", "b@p4.e2e.example"]
    account_ids: dict[str, str] = {}
    for email in emails:
        acc = _one(
            rest_server, "POST", f"tenants/{tid}/accounts",
            {"email": email, "credentialType": "oauth", "secret": mock_oauth_secret(email, with_token=True)},
        )["json"]
        account_ids[email] = acc["id"]

    server = _one(
        rest_server, "POST", f"tenants/{tid}/servers",
        {"name": "server-p4", "hostname": "host-p4.e2e", "switchMode": "manual"},
    )["json"]
    sid = server["id"]

    enroll = _one(rest_server, "POST", f"tenants/{tid}/servers/{sid}/enroll-token", {})["json"]

    host = AgentHost("p4", workdir / "p4-host", tsamx_bin, ama_binary)
    host.server_id = sid
    host.start(grpc_server, enroll["token"], signing_keys["public_key"], log_dir, extra_env=OFFLINE_ENV)

    # -- Deliver both accounts and wait each to go active, via the BFF -------
    assignment_ids: dict[str, str] = {}
    for email in emails:
        asg = _one(
            rest_server, "POST", f"tenants/{tid}/assignments",
            {"accountId": account_ids[email], "serverId": sid},
        )["json"]
        assignment_ids[email] = asg["id"]
        _one(rest_server, "POST", f"tenants/{tid}/assignments/{asg['id']}:deliver")
        state = _poll(
            rest_server, f"tenants/{tid}/assignments/{asg['id']}",
            lambda j: j and j.get("state") == "active", CONVERGENCE_TIMEOUT_S,
        )
        assert state is not None, _report(host, f"{email} never reached active")

    try:
        yield {
            "rest": rest_server, "tid": tid, "sid": sid,
            "emails": emails, "account_ids": account_ids,
            "assignment_ids": assignment_ids, "host": host,
        }
    finally:
        host.stop()


def _report(host: AgentHost, message: str) -> str:
    tail = host.process.logs()[-4000:] if host.process is not None else ""
    return f"{message}\n--- {host.label} ---\n{tail}"


def test_console_drives_lifecycle_and_alert_round_trip(console):
    """§9 completion, through the console: induce an ``all_exhausted`` alert on a
    live server, list and acknowledge it via the BFF, resolve it with a console
    refresh, then recall an assignment — every hop over the real BFF, and no
    admin token ever reaching the browser side."""
    rest = console["rest"]
    tid, sid = console["tid"], console["sid"]
    emails = console["emails"]
    host = console["host"]

    # -- Induce the alert: both accounts pinned over threshold, auto mode. The
    # first auto tick finds no viable target and emits KIND_ALL_EXHAUSTED, which
    # ams-server promotes to an open critical alert (Track A). ----------------
    preseed_usage(host, [("1", emails[0], 100.0), ("2", emails[1], 100.0)])
    _one(rest, "PATCH", f"tenants/{tid}/servers/{sid}", {"thresholdPct": 90.0, "defaultStrategy": "best"})
    _one(rest, "POST", f"tenants/{tid}/servers/{sid}:switch-mode", {"mode": "auto"})

    # -- The console sees the open alert. ------------------------------------
    def has_open_exhausted(page):
        return page and any(a["kind"] == "all_exhausted" and a["status"] == "open" for a in page.get("items", []))

    page = _poll(rest, f"tenants/{tid}/alerts?status=open", has_open_exhausted, ALERT_TIMEOUT_S)
    assert page is not None, _report(host, "no open all_exhausted alert surfaced to the console")
    alert = next(a for a in page["items"] if a["kind"] == "all_exhausted" and a["status"] == "open")
    assert alert["severity"] == "critical"
    alert_id = alert["id"]

    # -- Acknowledge it through the console. ---------------------------------
    acked = _one(rest, "POST", f"tenants/{tid}/alerts/{alert_id}:ack")["json"]
    assert acked["status"] == "acked", f"ack did not move the alert to acked: {acked}"
    assert acked["ackedAt"], "acked alert is missing ackedAt"

    # -- Resolve it: drop one account below threshold, then a console refresh
    # makes the agent send a fresh usage report; all_exhausted clears and the
    # acked alert auto-resolves (design note §4). ----------------------------
    preseed_usage(host, [("1", emails[0], 5.0), ("2", emails[1], 100.0)])
    _one(rest, "POST", f"tenants/{tid}/servers/{sid}:refresh-usage")

    def alert_resolved(page):
        match = [a for a in (page or {}).get("items", []) if a["id"] == alert_id]
        return bool(match) and match[0]["status"] == "resolved"

    resolved = _poll(rest, f"tenants/{tid}/alerts", alert_resolved, ALERT_TIMEOUT_S)
    assert resolved is not None, _report(host, f"alert {alert_id} never resolved after refresh")

    # -- A lifecycle state transition through the console: recall detaches the
    # assignment at AMS while the local record survives (O2). ----------------
    asg_id = console["assignment_ids"][emails[0]]
    _one(rest, "POST", f"tenants/{tid}/assignments/{asg_id}:recall")
    detached = _poll(
        rest, f"tenants/{tid}/assignments/{asg_id}",
        lambda j: j and j.get("state") == "detached", CONVERGENCE_TIMEOUT_S,
    )
    assert detached is not None, _report(host, "recall never reached detached through the console")


def test_bff_refuses_unauthenticated_console_access(console):
    """The live BFF still refuses a proxy request with no session cookie — the
    admin Bearer is never reachable without logging in first."""
    rest = console["rest"]
    tid = console["tid"]
    out = _bff(rest, [{"method": "GET", "path": f"tenants/{tid}/alerts", "noauth": True}])
    result = out["results"][0]
    assert result["status"] == 401, f"expected 401 without a session, got {result['status']}"
    assert result["leaked"] is False
