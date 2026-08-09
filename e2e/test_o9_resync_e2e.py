"""O9 credential re-sync, end to end (design note §5.7, §9).

Extends the P2/P3 harness (real AMS gRPC process, compiled ``ama`` daemons, the
real ``tsamx`` CLI) with the full O9 round trip that no unit test can settle —
because it is a claim about what a *second server* ends up holding after a
rotation on the *first*:

  1. Deliver an account to server A and let it go active. tsamx writes the live
     credential to ``CLAUDE_CONFIG_DIR/.credentials.json`` (the file the resyncer
     watches). Simulate a local refresh-token rotation by rewriting that file
     with a new ``refreshToken`` — exactly the on-disk change tsamx makes when it
     refreshes in place, minus the real Anthropic call. The running ``ama``
     detects the fingerprint change on its next tick, seals the refreshed set
     under the session KEK, and pushes a ``CredentialUpdate`` over the live gRPC
     session. AMS re-encrypts under the at-rest key and updates
     ``accounts.encrypted_secret`` — verified by decrypting the stored secret.

  2. Cross-server re-assignment (the actual value of O9): recall the account from
     A, then assign+deliver it to server B. B's real ``ama`` opens the delivered
     envelope and stages it into B's real tsamx pool. B's ``.credentials.json``
     must carry the *rotated* refresh token, not the stale one A was originally
     delivered — proof that the re-assignment path serves the resynced latest.

  3. Monotonicity, steady state: once the rotation is applied, further ticks over
     the unchanged file must not resend or move ``credential_observed_at`` — the
     baseline has advanced. (Backward-observed_at rejection is exhaustively
     covered by the ams-server unit suite; here we assert the live agent does not
     spuriously revert.)

Offline like P2/P3: synthetic OAuth sets with no ``accessToken`` (tsamx never
enters its usage-fetch path), a dead HTTPS proxy as belt-and-suspenders, and
``no_proxy`` keeping the gRPC dial to 127.0.0.1 direct. No real login, no real
token, no request to Anthropic. ``AMX_REPORT_INTERVAL`` forces a prompt
report/resync tick instead of the 5-minute default.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest

from conftest import AgentHost

# A dead proxy so any accidental tsamx egress fails instantly; no_proxy keeps the
# agent's gRPC dial to the AMS (127.0.0.1) direct. AMX_REPORT_INTERVAL forces the
# report+resync ticker to fire sub-second instead of every 5 minutes.
RESYNC_ENV = {
    "https_proxy": "http://127.0.0.1:9",
    "http_proxy": "http://127.0.0.1:9",
    "HTTPS_PROXY": "http://127.0.0.1:9",
    "HTTP_PROXY": "http://127.0.0.1:9",
    "no_proxy": "127.0.0.1,localhost,::1",
    "NO_PROXY": "127.0.0.1,localhost,::1",
    "AMX_REPORT_INTERVAL": "300ms",
}

CONVERGENCE_TIMEOUT_S = 120.0
RESYNC_TIMEOUT_S = 60.0


def mock_oauth_secret(email: str, refresh_token: str) -> str:
    """A synthetic OAuth set carrying a specific refresh token, no accessToken.

    No ``accessToken`` keeps tsamx off its usage-fetch path entirely (as in P2).
    The refresh token is the identity the O9 fingerprint hashes, so a distinct
    value here is exactly what a rotation changes.
    """
    return json.dumps(
        {
            "claudeAiOauth": {
                "refreshToken": refresh_token,
                "scopes": ["user:inference"],
                "emailAddress": email,
                "organizationName": "E2E Test Org",
                "expiresAt": 0,
            }
        }
    )


class Server:
    """One server: its AgentHost plus REST helpers scoped to a shared tenant."""

    def __init__(self, client, tenant_id: str, host: AgentHost):
        self.client = client
        self.tenant_id = tenant_id
        self.host = host
        self.server_id = host.server_id
        self.assignments: dict[str, str] = {}  # email -> assignment id

    def base(self, suffix: str = "") -> str:
        return f"/api/v1/tenants/{self.tenant_id}{suffix}"

    def assignment_state(self, email: str) -> str:
        response = self.client.get(self.base(f"/assignments/{self.assignments[email]}"))
        response.raise_for_status()
        return response.json()["state"]

    def assign_and_deliver(self, account_id: str, email: str) -> None:
        response = self.client.post(
            self.base("/assignments"),
            json={"account_id": account_id, "server_id": self.server_id},
        )
        response.raise_for_status()
        self.assignments[email] = response.json()["id"]
        response = self.client.post(
            self.base(f"/assignments/{self.assignments[email]}:deliver")
        )
        response.raise_for_status()
        self.wait_state(email, "active", CONVERGENCE_TIMEOUT_S)

    def recall(self, email: str) -> None:
        response = self.client.post(
            self.base(f"/assignments/{self.assignments[email]}:recall")
        )
        response.raise_for_status()
        self.wait_state(email, "detached", CONVERGENCE_TIMEOUT_S)

    def wait_state(self, email: str, expected: str, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        state = self.assignment_state(email)
        while state != expected and time.monotonic() < deadline:
            time.sleep(0.5)
            state = self.assignment_state(email)
        assert state == expected, _report(
            self.host, f"{email} stuck at {state!r}, wanted {expected!r}"
        )

    def active_email(self) -> str | None:
        for account in self.host.tsamx_accounts():
            if account.get("active"):
                return account["email"]
        return None

    def live_refresh_token(self) -> str | None:
        """The refresh token in this host's live active-credential file."""
        path = self.host.config_dir / ".credentials.json"
        if not path.exists():
            return None
        return json.loads(path.read_text())["claudeAiOauth"].get("refreshToken")


def _report(host: AgentHost, message: str) -> str:
    tail = host.process.logs()[-4000:] if host.process is not None else ""
    return f"{message}\n--- {host.label} ---\n{tail}"


def _account_row(tenant_id: str, account_id: str):
    from app.db import get_sessionmaker
    from app.models import Account

    with get_sessionmaker()() as db:
        return db.get(Account, uuid.UUID(account_id))


def _stored_refresh_token(tenant_id: str, account_id: str) -> str:
    """Decrypt ``accounts.encrypted_secret`` and return its refresh token."""
    from app.core import crypto
    from app.db import get_sessionmaker
    from app.models import Account

    with get_sessionmaker()() as db:
        account = db.get(Account, uuid.UUID(account_id))
        payload = json.loads(
            crypto.decrypt_secret(
                account.encrypted_secret,
                tenant_id=account.tenant_id,
                db=db,
            )
        )
    return payload["claudeAiOauth"]["refreshToken"]


def _wait_stored_refresh(tenant_id: str, account_id: str, expected: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if _stored_refresh_token(tenant_id, account_id) == expected:
                return True
        except Exception:  # noqa: BLE001 - transient decrypt during in-flight write
            pass
        time.sleep(0.3)
    return False


@pytest.fixture(scope="module")
def two_servers(client, grpc_server, signing_keys, tsamx_bin, ama_binary, workdir):
    """One tenant, two enrolled servers (A, B), each with a running resync-enabled
    ``ama`` daemon. Returns ``(tenant_id, server_a, server_b)``."""
    log_dir = workdir / "logs"
    log_dir.mkdir(exist_ok=True)
    hosts: list[AgentHost] = []

    response = client.post(
        "/api/v1/tenants", json={"name": "e2e-o9-" + uuid.uuid4().hex[:8]}
    )
    response.raise_for_status()
    tenant_id = response.json()["id"]

    def make(label: str) -> Server:
        host = AgentHost(label, workdir / f"o9-host-{label}", tsamx_bin, ama_binary)
        response = client.post(
            f"/api/v1/tenants/{tenant_id}/servers",
            json={
                "name": f"server-{label}",
                "hostname": f"host-{label}.o9.e2e",
                "switch_mode": "manual",
            },
        )
        response.raise_for_status()
        host.server_id = response.json()["id"]
        response = client.post(
            f"/api/v1/tenants/{tenant_id}/servers/{host.server_id}/enroll-token", json={}
        )
        response.raise_for_status()
        enroll_token = response.json()["token"]
        host.start(
            grpc_server, enroll_token, signing_keys["public_key"], log_dir, extra_env=RESYNC_ENV
        )
        hosts.append(host)
        return Server(client, tenant_id, host)

    try:
        yield tenant_id, make("a"), make("b")
    finally:
        for host in hosts:
            host.stop()


def test_rotation_resyncs_and_cross_server_reassign_delivers_latest(two_servers):
    tenant_id, server_a, server_b = two_servers
    client = server_a.client
    email = "rotator@o9.e2e.example"
    original_rt = "e2e-o9-orig-" + uuid.uuid4().hex
    rotated_rt = "e2e-o9-rotated-" + uuid.uuid4().hex

    response = client.post(
        f"/api/v1/tenants/{tenant_id}/accounts",
        json={
            "email": email,
            "credential_type": "oauth",
            "secret": mock_oauth_secret(email, original_rt),
        },
    )
    response.raise_for_status()
    account_id = response.json()["id"]

    # -- Deliver to A; it goes active and tsamx writes .credentials.json --------
    server_a.assign_and_deliver(account_id, email)
    assert server_a.active_email() == email, _report(server_a.host, "A did not activate the account")
    assert server_a.live_refresh_token() == original_rt, _report(
        server_a.host, "A's live credential is not the delivered original"
    )
    assert _stored_refresh_token(tenant_id, account_id) == original_rt

    # -- Simulate a local rotation: rewrite A's live credential in place --------
    cred_path = server_a.host.config_dir / ".credentials.json"
    cred_path.write_text(mock_oauth_secret(email, rotated_rt))

    # (1) The running ama detects the fingerprint change and pushes the refreshed
    #     set; AMS re-encrypts and updates accounts.encrypted_secret.
    assert _wait_stored_refresh(tenant_id, account_id, rotated_rt, RESYNC_TIMEOUT_S), _report(
        server_a.host, "AMS did not receive the rotated credential within the timeout"
    )
    observed_after = _account_row(tenant_id, account_id).credential_observed_at
    assert observed_after is not None, "credential_observed_at was not stamped by the re-sync"

    # (3) Steady state: further ticks over the unchanged file must not resend nor
    #     move observed_at backward — the baseline has advanced.
    time.sleep(2.0)
    assert _stored_refresh_token(tenant_id, account_id) == rotated_rt
    assert _account_row(tenant_id, account_id).credential_observed_at == observed_after, _report(
        server_a.host, "steady-state tick reverted or re-moved credential_observed_at"
    )

    # -- Cross-server re-assignment: recall from A, deliver to B ----------------
    server_a.recall(email)
    server_b.assign_and_deliver(account_id, email)

    # (2) B's live pool must hold the RESYNCED latest, not the stale original A
    #     was first delivered.
    assert server_b.active_email() == email, _report(server_b.host, "B did not activate the account")
    assert server_b.live_refresh_token() == rotated_rt, _report(
        server_b.host,
        f"B received a stale credential (got {server_b.live_refresh_token()!r}, "
        f"wanted rotated {rotated_rt!r})",
    )
    assert server_b.live_refresh_token() != original_rt
