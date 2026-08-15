"""P2 completion criterion, end to end (design note §8, §9).

One tenant, three servers, ten mock accounts. Assign 3/5/2, deliver them all
through the channel, and check that each host's real tsamx pool ends up holding
exactly its share. Then recall two of them and check the O2 rule: the assignment
detaches at AMS while the local record survives, merely disabled.

Everything crosses a process boundary the way it will in production — REST
writes an outbox row, a separate gRPC process signs and pushes the command, a
compiled Go daemon opens the credential envelope and drives the tsamx CLI, and
the ack travels back to move the assignment.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest

from conftest import AgentHost

# tsamx's proto AllocationStatus values (contracts/proto/amx.proto).
ALLOCATION_ACTIVE = 2
ALLOCATION_INACTIVE = 3

FLEET = {"a": 3, "b": 5, "c": 2}
TOTAL_ACCOUNTS = sum(FLEET.values())

CONVERGENCE_TIMEOUT_S = 120.0


def mock_oauth_secret(email: str) -> str:
    """A synthetic OAuth credential set.

    Deliberately carries no ``accessToken``: tsamx classifies such an account as
    statically "no credentials" and never enters its usage-fetch path, which is
    what keeps this suite off the network entirely. Everything the delivery path
    actually exercises — sealing, transport, envelope opening, the file tsamx
    captures — is unaffected.
    """
    return json.dumps(
        {
            "claudeAiOauth": {
                "refreshToken": "e2e-mock-refresh-" + uuid.uuid4().hex,
                "scopes": ["user:inference"],
                "emailAddress": email,
                "organizationName": "E2E Test Org",
                "expiresAt": 0,
            }
        }
    )


class Fleet:
    """The whole scenario's state: tenant, hosts, accounts, assignments."""

    def __init__(self, client, tenant_id: str):
        self.client = client
        self.tenant_id = tenant_id
        self.hosts: dict[str, AgentHost] = {}
        self.account_ids: dict[str, str] = {}     # email -> account id
        self.assignments: dict[str, str] = {}     # email -> assignment id
        self.host_of: dict[str, str] = {}         # email -> host label
        self.refresh_tokens: dict[str, str] = {}  # email -> the delivered secret

    def base(self, suffix: str = "") -> str:
        return f"/api/v1/tenants/{self.tenant_id}{suffix}"

    def emails_on(self, label: str) -> set[str]:
        return {e for e, host in self.host_of.items() if host == label}

    def assignment_state(self, email: str) -> str:
        response = self.client.get(self.base(f"/assignments/{self.assignments[email]}"))
        response.raise_for_status()
        return response.json()["state"]

    def wait_for_states(self, emails, expected: str, timeout_s: float) -> dict[str, str]:
        deadline = time.monotonic() + timeout_s
        states = {}
        while True:
            states = {email: self.assignment_state(email) for email in emails}
            if all(state == expected for state in states.values()):
                return states
            if time.monotonic() >= deadline:
                return states
            time.sleep(0.5)


@pytest.fixture(scope="module")
def fleet(client, grpc_server, signing_keys, tsamx_bin, ama_binary, workdir):
    """Build the tenant, enroll three agents, and register ten accounts."""
    log_dir = workdir / "logs"
    log_dir.mkdir(exist_ok=True)

    response = client.post("/api/v1/tenants", json={"name": "e2e-" + uuid.uuid4().hex[:8]})
    response.raise_for_status()
    state = Fleet(client, response.json()["id"])

    for label, count in FLEET.items():
        host = AgentHost(label, workdir / f"host-{label}", tsamx_bin, ama_binary)
        response = client.post(
            state.base("/servers"),
            json={"name": f"server-{label}", "hostname": f"host-{label}.e2e", "switch_mode": "auto"},
        )
        response.raise_for_status()
        host.server_id = response.json()["id"]

        response = client.post(state.base(f"/servers/{host.server_id}/enroll-token"), json={})
        response.raise_for_status()
        enroll_token = response.json()["token"]

        host.start(grpc_server, enroll_token, signing_keys["public_key"], log_dir)
        state.hosts[label] = host

        for index in range(count):
            email = f"{label}{index + 1}@fleet-{label}.e2e.example"
            secret = mock_oauth_secret(email)
            response = client.post(
                state.base("/accounts"),
                json={"email": email, "credential_type": "oauth", "secret": secret},
            )
            response.raise_for_status()
            state.account_ids[email] = response.json()["id"]
            state.host_of[email] = label
            state.refresh_tokens[email] = json.loads(secret)["claudeAiOauth"]["refreshToken"]

    try:
        yield state
    finally:
        for host in state.hosts.values():
            host.stop()


def _report(state: Fleet, message: str) -> str:
    """Failure message with each agent's log tail attached."""
    parts = [message]
    for label, host in state.hosts.items():
        if host.process is not None:
            parts.append(f"--- ama-{label} ---\n{host.process.logs()[-4000:]}")
    return "\n".join(parts)


def test_fleet_delivery_and_recall_round_trip(fleet: Fleet):
    # -- Assign 3/5/2 and deliver every one of them ---------------------------
    for email, label in fleet.host_of.items():
        response = fleet.client.post(
            fleet.base("/assignments"),
            json={
                "account_id": fleet.account_ids[email],
                "server_id": fleet.hosts[label].server_id,
            },
        )
        response.raise_for_status()
        fleet.assignments[email] = response.json()["id"]

    assert len(fleet.assignments) == TOTAL_ACCOUNTS

    for email in fleet.assignments:
        response = fleet.client.post(fleet.base(f"/assignments/{fleet.assignments[email]}:deliver"))
        response.raise_for_status()
        assert response.json()["state"] == "delivering"

    # (a) every assignment converges to active.
    states = fleet.wait_for_states(fleet.assignments, "active", CONVERGENCE_TIMEOUT_S)
    unconverged = {email: state for email, state in states.items() if state != "active"}
    assert not unconverged, _report(fleet, f"assignments did not converge: {unconverged}")

    # (b) each host's real tsamx pool holds exactly its share, and only its own
    #     accounts — the cross-host isolation claim, not just the count.
    for label, host in fleet.hosts.items():
        accounts = host.tsamx_accounts()
        assert {a["email"] for a in accounts} == fleet.emails_on(label), _report(
            fleet, f"host {label} tsamx pool mismatch: {accounts}"
        )
        assert len(accounts) == FLEET[label]
        assert not any(a.get("disabled") for a in accounts), f"host {label}: unexpected disable"

        records = host.manifest_records()
        assert {r["email"] for r in records} == fleet.emails_on(label)
        assert all(r["allocationStatus"] == ALLOCATION_ACTIVE for r in records)

    # -- Recall one account from A and one from B (O2 default: keep + disable) --
    recalled = {"a": sorted(fleet.emails_on("a"))[0], "b": sorted(fleet.emails_on("b"))[0]}
    for email in recalled.values():
        response = fleet.client.post(fleet.base(f"/assignments/{fleet.assignments[email]}:recall"))
        response.raise_for_status()
        assert response.json()["state"] == "recalling"

    # (c) recalled assignments detach at AMS...
    states = fleet.wait_for_states(recalled.values(), "detached", CONVERGENCE_TIMEOUT_S)
    stuck = {email: state for email, state in states.items() if state != "detached"}
    assert not stuck, _report(fleet, f"recalls did not detach: {stuck}")

    #     ...while the local copy is fully removed. This is the current O2
    #     decision (2026-08-14): recall means full detach, not disable — the
    #     account leaves the host's tsamx pool and its manifest record is
    #     deleted, its history living only in the AMS-side detached row.
    for label, email in recalled.items():
        host = fleet.hosts[label]
        accounts = host.tsamx_accounts()
        assert len(accounts) == FLEET[label] - 1, _report(
            fleet, f"host {label}: recall did not remove the tsamx record ({accounts})"
        )
        assert email not in {a["email"] for a in accounts}, _report(
            fleet, f"host {label}: {email} still in the tsamx pool after purge ({accounts})"
        )

        assert email not in {r["email"] for r in host.manifest_records()}, (
            f"host {label}: {email} manifest record survived recall (O2 is full detach)"
        )

    # Untouched assignments on the same hosts stay active — a recall is scoped
    # to its own assignment, not to the host.
    for label, email in recalled.items():
        for other in fleet.emails_on(label) - {email}:
            assert fleet.assignment_state(other) == "active"


def test_delivered_credentials_are_never_written_in_the_clear(fleet: Fleet):
    """No agent log and no manifest may contain a delivered refresh token.

    The credential set only ever legitimately lands in tsamx's own credential
    store; anywhere else — a log line, the manifest's plaintext metadata — is a
    §7 violation.
    """
    delivered = set(fleet.refresh_tokens.values())
    assert delivered, "no credentials were delivered — the check would be vacuous"
    for label, host in fleet.hosts.items():
        assert host.process is not None
        blob = host.process.logs()
        manifest = host.state_dir / "manifest.enc"
        if manifest.exists():
            blob += manifest.read_text()
        assert "refreshToken" not in blob, f"host {label} wrote a credential field in the clear"
        for token in delivered:
            assert token not in blob, f"host {label} leaked a delivered refresh token"
