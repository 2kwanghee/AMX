"""P3 "switching control" completion criterion, end to end (design note §7, §9).

Extends the P2 harness (real AMS gRPC process, a compiled ``ama`` daemon, the
real ``tsamx`` CLI) with the P3 round trip: deliver a threshold policy, let a
real ``tsamx auto --once`` tick fire, and observe the resulting switch reach AMS
as an ``AccountEvent{kind=switch, trigger=at-limit}`` plus a ``usage_snapshots``
row.

How the switch is driven fully offline: each account is delivered with an OAuth
credential that *does* carry an ``accessToken`` (so tsamx does not classify it as
"no credentials" and will read a usage measurement for it), while the usage store
(``cache/usage.json``) is preseeded with a fresh ``lastGood`` — the active
account at 91%, the candidate at 5%. tsamx's own pacing serves a fresh store row
without fetching, so ``auto --once`` decides on the preseeded numbers and never
reaches the network. A dead HTTPS proxy (with ``no_proxy`` keeping the gRPC dial
to 127.0.0.1 direct) is a belt-and-suspenders guarantee: any stray usage fetch
fails instantly without egress, and stale-on-error keeps the preseeded lastGood.
No real login, no real token, no request to Anthropic — same promise as P2.

The prompt tick is forced with ``AMX_TICK_INTERVAL`` (a test-only env on the
agent); the switch event reaches AMS over the live session because the agent now
drains its outbox on a short interval, not only on reconnect.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest
from sqlalchemy import select

from conftest import AgentHost

# A dead proxy so any accidental tsamx usage fetch fails instantly without
# egress; no_proxy keeps the agent's gRPC dial to the AMS (127.0.0.1) direct.
# AMX_TICK_INTERVAL forces a prompt scheduler tick instead of the 60s default.
OFFLINE_ENV = {
    "https_proxy": "http://127.0.0.1:9",
    "http_proxy": "http://127.0.0.1:9",
    "HTTPS_PROXY": "http://127.0.0.1:9",
    "HTTP_PROXY": "http://127.0.0.1:9",
    "no_proxy": "127.0.0.1,localhost,::1",
    "NO_PROXY": "127.0.0.1,localhost,::1",
    "AMX_TICK_INTERVAL": "500ms",
}

CONVERGENCE_TIMEOUT_S = 120.0
EVENT_TIMEOUT_S = 60.0


def mock_oauth_secret(email: str, *, with_token: bool) -> str:
    """A synthetic OAuth credential set.

    ``with_token=True`` carries an ``accessToken`` and a far-future ``expiresAt``
    so tsamx treats the account as a real (fetchable) OAuth account whose token
    never needs freshening — the store preseed then supplies its usage without a
    network call. The token is bogus; the dead proxy guarantees it is never used
    against a real endpoint.
    """
    oauth = {
        "refreshToken": "e2e-mock-refresh-" + uuid.uuid4().hex,
        "scopes": ["user:inference"],
        "emailAddress": email,
        "organizationName": "E2E Test Org",
        "expiresAt": 0,
    }
    if with_token:
        oauth["accessToken"] = "sk-ant-oat01-e2e-" + uuid.uuid4().hex
        oauth["expiresAt"] = int((time.time() + 86400) * 1000)
    return json.dumps({"claudeAiOauth": oauth})


def preseed_usage(host: AgentHost, rows: list[tuple[str, str, float]]) -> None:
    """Write the tsamx usage store so ``auto --once`` decides offline.

    ``rows`` is ``(slot_number, email, five_hour_pct)``. Each row is fresh
    (``fetchedAt`` = now, well inside the serve TTL) with a far-future
    ``nextPollAt``, so tsamx serves it without fetching — including the auto
    engine's near-threshold escalation pass.
    """
    store = host.data_home / "tsamx" / "cache" / "usage.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    accounts = {
        slot: {
            "email": email,
            "organizationUuid": "",
            "fetchedAt": now,
            "lastGood": {
                "five_hour": {"pct": pct, "resets_at": None},
                "seven_day": {"pct": pct / 10.0, "resets_at": None},
            },
            "nextPollAt": now + 3600.0,
            "pollIntervalS": 3600.0,
            "consecutiveFailures": 0,
        }
        for slot, email, pct in rows
    }
    store.write_text(json.dumps({"schemaVersion": 2, "accounts": accounts}))


class Server:
    """One P3 server: its AgentHost plus REST helpers scoped to a tenant."""

    def __init__(self, client, tenant_id: str, host: AgentHost):
        self.client = client
        self.tenant_id = tenant_id
        self.host = host
        self.server_id = host.server_id
        self.account_ids: dict[str, str] = {}      # email -> account id
        self.assignments: dict[str, str] = {}      # email -> assignment id

    def base(self, suffix: str = "") -> str:
        return f"/api/v1/tenants/{self.tenant_id}{suffix}"

    def add_account(self, email: str, *, with_token: bool) -> str:
        response = self.client.post(
            self.base("/accounts"),
            json={
                "email": email,
                "credential_type": "oauth",
                "secret": mock_oauth_secret(email, with_token=with_token),
            },
        )
        response.raise_for_status()
        self.account_ids[email] = response.json()["id"]
        return self.account_ids[email]

    def assignment_state(self, email: str) -> str:
        response = self.client.get(self.base(f"/assignments/{self.assignments[email]}"))
        response.raise_for_status()
        return response.json()["state"]

    def deliver(self, email: str) -> None:
        """Assign then deliver one account and wait for it to go active.

        Sequential (waits for active) so tsamx's slot numbering follows the
        delivery order deterministically: the first delivered account is slot 1,
        the second slot 2, and the last delivered is the active one.
        """
        response = self.client.post(
            self.base("/assignments"),
            json={"account_id": self.account_ids[email], "server_id": self.server_id},
        )
        response.raise_for_status()
        self.assignments[email] = response.json()["id"]
        response = self.client.post(
            self.base(f"/assignments/{self.assignments[email]}:deliver")
        )
        response.raise_for_status()
        self.wait_state(email, "active", CONVERGENCE_TIMEOUT_S)

    def wait_state(self, email: str, expected: str, timeout_s: float) -> str:
        deadline = time.monotonic() + timeout_s
        state = self.assignment_state(email)
        while state != expected and time.monotonic() < deadline:
            time.sleep(0.5)
            state = self.assignment_state(email)
        assert state == expected, _report(
            self.host, f"{email} stuck at {state!r}, wanted {expected!r}"
        )
        return state

    def set_policy(self, *, threshold_pct: float, default_strategy: str) -> None:
        response = self.client.patch(
            self.base(f"/servers/{self.server_id}"),
            json={"threshold_pct": threshold_pct, "default_strategy": default_strategy},
        )
        response.raise_for_status()

    def set_mode(self, mode: str) -> None:
        response = self.client.post(
            self.base(f"/servers/{self.server_id}:switch-mode"), json={"mode": mode}
        )
        response.raise_for_status()

    def active_email(self) -> str | None:
        for account in self.host.tsamx_accounts():
            if account.get("active"):
                return account["email"]
        return None


def _report(host: AgentHost, message: str) -> str:
    tail = host.process.logs()[-4000:] if host.process is not None else ""
    return f"{message}\n--- {host.label} ---\n{tail}"


@pytest.fixture(scope="module")
def make_server(client, grpc_server, signing_keys, tsamx_bin, ama_binary, workdir):
    """Factory: build a tenant+server+agent host in the given switch mode."""
    log_dir = workdir / "logs"
    log_dir.mkdir(exist_ok=True)
    hosts: list[AgentHost] = []

    def factory(label: str, *, switch_mode: str) -> Server:
        response = client.post(
            "/api/v1/tenants", json={"name": "e2e-p3-" + uuid.uuid4().hex[:8]}
        )
        response.raise_for_status()
        tenant_id = response.json()["id"]

        host = AgentHost(label, workdir / f"p3-host-{label}", tsamx_bin, ama_binary)
        response = client.post(
            f"/api/v1/tenants/{tenant_id}/servers",
            json={
                "name": f"server-{label}",
                "hostname": f"host-{label}.p3.e2e",
                "switch_mode": switch_mode,
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
            grpc_server, enroll_token, signing_keys["public_key"], log_dir, extra_env=OFFLINE_ENV
        )
        hosts.append(host)
        return Server(client, tenant_id, host)

    try:
        yield factory
    finally:
        for host in hosts:
            host.stop()


def _switch_events(tenant_id: str, server_id: str) -> list[dict]:
    """Every ``switch_event`` snapshot payload for one server, newest not sorted."""
    from app.db import get_sessionmaker
    from app.models import UsageSnapshot

    with get_sessionmaker()() as db:
        rows = db.scalars(
            select(UsageSnapshot).where(
                UsageSnapshot.tenant_id == uuid.UUID(tenant_id),
                UsageSnapshot.server_id == uuid.UUID(server_id),
                UsageSnapshot.report_type == "switch_event",
            )
        ).all()
        return [r.payload for r in rows]


def _wait_for_event(tenant_id: str, server_id: str, predicate, timeout_s: float):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for payload in _switch_events(tenant_id, server_id):
            if predicate(payload):
                return payload
        time.sleep(0.5)
    return None


def test_threshold_delivery_drives_a_switch_event(make_server):
    """§9 completion: deliver threshold=90, active account preseeded at 91%, a
    real ``auto --once`` tick switches, and AMS records the at-limit switch.

    The server starts in manual mode so no tick fires until the store is seeded
    and the policy is delivered; flipping to auto then starts the scheduler.
    """
    server = make_server("switch", switch_mode="manual")
    low = "cand@p3.e2e.example"     # slot 1 (delivered first): the switch target
    high = "active@p3.e2e.example"  # slot 2 (delivered last): active, over limit

    server.add_account(low, with_token=True)
    server.add_account(high, with_token=True)
    server.deliver(low)
    server.deliver(high)
    assert server.active_email() == high, _report(server.host, "high account is not active pre-tick")

    # Preseed usage: active (slot 2) at 91%, candidate (slot 1) at 5%.
    preseed_usage(server.host, [("1", low, 5.0), ("2", high, 91.0)])

    # Deliver the policy, then start the scheduler. Both are queued in order, so
    # the threshold is applied before the first tick evaluates it.
    server.set_policy(threshold_pct=90.0, default_strategy="best")
    server.set_mode("auto")

    event = _wait_for_event(
        server.tenant_id,
        server.server_id,
        lambda p: p.get("kind") == "KIND_SWITCH" and p.get("trigger") == "TRIGGER_AT_LIMIT",
        EVENT_TIMEOUT_S,
    )
    assert event is not None, _report(
        server.host, "no at-limit switch event reached AMS within the timeout"
    )
    # from = the over-limit active account, to = the low-usage candidate.
    assert event.get("from", {}).get("email") == high
    assert event.get("to", {}).get("email") == low

    # (b) tsamx's live active account actually moved to the candidate.
    assert server.active_email() == low, _report(server.host, "tsamx active did not switch")


def test_all_accounts_exhausted_emits_critical_event(make_server):
    """Every account over the threshold => no viable target => all-exhausted."""
    server = make_server("exhausted", switch_mode="manual")
    a = "a@p3ex.e2e.example"
    b = "b@p3ex.e2e.example"
    server.add_account(a, with_token=True)
    server.add_account(b, with_token=True)
    server.deliver(a)
    server.deliver(b)

    # Both accounts pinned at 100%: nothing to switch to.
    preseed_usage(server.host, [("1", a, 100.0), ("2", b, 100.0)])
    server.set_policy(threshold_pct=90.0, default_strategy="best")
    server.set_mode("auto")

    event = _wait_for_event(
        server.tenant_id,
        server.server_id,
        lambda p: p.get("kind") == "KIND_ALL_EXHAUSTED",
        EVENT_TIMEOUT_S,
    )
    assert event is not None, _report(
        server.host, "no all-exhausted event reached AMS within the timeout"
    )
    # The active account did not move — there was nowhere to go.
    assert server.active_email() == b, _report(server.host, "active moved despite all-exhausted")


def test_manual_mode_never_ticks(make_server):
    """A manual-mode server must not switch even with the active account over the
    threshold: the scheduler is stopped, so no tick evaluates the policy."""
    server = make_server("manual", switch_mode="manual")
    low = "low@p3man.e2e.example"
    high = "high@p3man.e2e.example"
    server.add_account(low, with_token=True)
    server.add_account(high, with_token=True)
    server.deliver(low)
    server.deliver(high)

    preseed_usage(server.host, [("1", low, 5.0), ("2", high, 91.0)])
    # Policy delivered, but mode stays manual — no scheduler, no tick.
    server.set_policy(threshold_pct=90.0, default_strategy="best")

    # Give a would-be tick ample time to fire, then assert nothing happened.
    time.sleep(5.0)
    assert server.active_email() == high, _report(server.host, "manual mode switched the active account")
    assert _switch_events(server.tenant_id, server.server_id) == [], _report(
        server.host, "manual mode produced a switch event"
    )
