"""D2 sent-未ack recovery (recovery-architecture §2).

The poll loop marks a command ``sent`` but nothing re-sends it if the agent never
acks; :func:`commands.sweep_sent_timeouts` re-queues a stuck ``sent`` command
(idempotent, same command_id) up to MAX_SEND_ATTEMPTS, then fails it and reverts
its assignment. These drive the sweep directly with an aged ``sent_at`` — the
same primitive the offline-sweeper sibling calls on its timer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.errors import ApiError
from app.db import get_sessionmaker
from app.models import AgentCommand, Alert
from app.services import commands, inventory, reconcile

from tests.test_grpc_channel import (
    _create_assignment,
    _seed_tenant_account_server,
)


# -- helpers ------------------------------------------------------------------
def _set_state(tenant_id, assignment_id, state, *, pending_command_id="__keep__"):
    with get_sessionmaker()() as db:
        a = inventory.get_assignment(db, tenant_id, assignment_id)
        a.state = state
        if pending_command_id != "__keep__":
            a.pending_command_id = pending_command_id
        db.commit()


def _age_sent(command_id, *, seconds, send_attempts=None):
    """Force a command into aged ``sent`` (optionally at a given attempt count)."""
    with get_sessionmaker()() as db:
        cmd = db.scalar(
            select(AgentCommand).where(AgentCommand.command_id == command_id)
        )
        cmd.status = "sent"
        cmd.sent_at = datetime.now(UTC) - timedelta(seconds=seconds)
        if send_attempts is not None:
            cmd.send_attempts = send_attempts
        db.commit()


def _age_queued(command_id, *, seconds):
    """Force a still-``queued`` command's ``created_at`` into the past.

    Models a command a poll loop never once claimed (``fetch_queued`` only
    ever sees ``queued`` rows for an *online* server — the D3 case is a server
    whose agent never opens a session, so nothing ever claims the row).
    """
    with get_sessionmaker()() as db:
        cmd = db.scalar(
            select(AgentCommand).where(AgentCommand.command_id == command_id)
        )
        cmd.created_at = datetime.now(UTC) - timedelta(seconds=seconds)
        db.commit()


def _command(command_id):
    with get_sessionmaker()() as db:
        return db.scalar(
            select(AgentCommand).where(AgentCommand.command_id == command_id)
        )


def _assignment(tenant_id, assignment_id):
    with get_sessionmaker()() as db:
        return inventory.get_assignment(db, tenant_id, assignment_id)


def _open_alerts(server_id, kind):
    with get_sessionmaker()() as db:
        return db.scalars(
            select(Alert).where(
                Alert.server_id == server_id,
                Alert.kind == kind,
                Alert.status == "open",
            )
        ).all()


# -- re-queue path ------------------------------------------------------------
def test_sent_timeout_below_cap_requeues_idempotently(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("d2req@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, assignment_id, "pending")
    with get_sessionmaker()() as db:
        commands.request_deliver(db, tenant_id, assignment_id)
        command_id = db.scalar(
            select(AgentCommand.command_id).where(
                AgentCommand.assignment_id == assignment_id
            )
        )
    _age_sent(command_id, seconds=1000)

    with get_sessionmaker()() as db:
        requeued, failed = commands.sweep_sent_timeouts(db, timeout_seconds=90)
    assert requeued == [command_id]
    assert failed == []

    cmd = _command(command_id)
    # Same command_id (idempotent re-send), back to queued, attempt counted.
    assert cmd.status == "queued"
    assert cmd.sent_at is None
    assert cmd.send_attempts == 1
    # Assignment untouched — still in-flight delivering.
    a = _assignment(tenant_id, assignment_id)
    assert a.state == "delivering"
    assert a.pending_command_id == command_id


def test_fresh_sent_is_not_swept(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("d2fresh@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, assignment_id, "pending")
    with get_sessionmaker()() as db:
        commands.request_deliver(db, tenant_id, assignment_id)
        command_id = db.scalar(
            select(AgentCommand.command_id).where(
                AgentCommand.assignment_id == assignment_id
            )
        )
    _age_sent(command_id, seconds=5)  # well within the 90s timeout

    with get_sessionmaker()() as db:
        requeued, failed = commands.sweep_sent_timeouts(db, timeout_seconds=90)
    assert requeued == [] and failed == []
    assert _command(command_id).status == "sent"


# -- fail + revert path -------------------------------------------------------
def test_sent_timeout_deliver_over_cap_fails_and_reverts_to_pending(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("d2cap@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, assignment_id, "pending")
    with get_sessionmaker()() as db:
        commands.request_deliver(db, tenant_id, assignment_id)
        command_id = db.scalar(
            select(AgentCommand.command_id).where(
                AgentCommand.assignment_id == assignment_id
            )
        )
    _age_sent(command_id, seconds=1000, send_attempts=5)

    with get_sessionmaker()() as db:
        requeued, failed = commands.sweep_sent_timeouts(
            db, timeout_seconds=90, max_attempts=5
        )
    assert requeued == [] and failed == [command_id]

    assert _command(command_id).status == "failed"
    a = _assignment(tenant_id, assignment_id)
    assert a.state == "pending"  # re-issuable
    assert a.pending_command_id is None
    assert a.last_error == "sent_ack_timeout"


def test_sent_timeout_recall_over_cap_lands_settled_recalling(app_env):
    # recall failure reverts to the D1 settled-recalling state, not pending.
    tenant_id, account_id, server_id = _seed_tenant_account_server("d2recall@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, assignment_id, "active")
    with get_sessionmaker()() as db:
        commands.request_recall(db, tenant_id, assignment_id)
        command_id = db.scalar(
            select(AgentCommand.command_id).where(
                AgentCommand.assignment_id == assignment_id
            )
        )
    _age_sent(command_id, seconds=1000, send_attempts=5)

    with get_sessionmaker()() as db:
        commands.sweep_sent_timeouts(db, timeout_seconds=90, max_attempts=5)

    assert _command(command_id).status == "failed"
    a = _assignment(tenant_id, assignment_id)
    assert a.state == "recalling"  # stays recalling for the D1 path
    assert a.pending_command_id is None  # ...but settled (not in-flight)
    assert a.last_error == "sent_ack_timeout"


def test_sent_timeout_server_scoped_over_cap_only_marks_failed(app_env):
    # A server-scoped command (assignment_id NULL) has no assignment to revert.
    tenant_id, _account_id, server_id = _seed_tenant_account_server("d2srv@ex.com")
    with get_sessionmaker()() as db:
        commands.request_switch_mode(db, tenant_id, server_id, mode="manual")
        command_id = db.scalar(
            select(AgentCommand.command_id).where(
                AgentCommand.command_type == "set_mode"
            )
        )
    _age_sent(command_id, seconds=1000, send_attempts=5)

    with get_sessionmaker()() as db:
        _, failed = commands.sweep_sent_timeouts(db, timeout_seconds=90, max_attempts=5)
    assert failed == [command_id]
    assert _command(command_id).status == "failed"


# -- cap-exhausted alerts (D2 fix) --------------------------------------------
def test_sent_timeout_deliver_over_cap_opens_command_send_failed(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("d2alertdel@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, assignment_id, "pending")
    with get_sessionmaker()() as db:
        commands.request_deliver(db, tenant_id, assignment_id)
        command_id = db.scalar(
            select(AgentCommand.command_id).where(
                AgentCommand.assignment_id == assignment_id
            )
        )
    _age_sent(command_id, seconds=1000, send_attempts=5)

    with get_sessionmaker()() as db:
        commands.sweep_sent_timeouts(db, timeout_seconds=90, max_attempts=5)

    # A non-recall final failure opens command_send_failed (account-scoped),
    # never recall_failed.
    opened = _open_alerts(server_id, "command_send_failed")
    assert len(opened) == 1
    alert = opened[0]
    assert alert.account_id == account_id
    assert alert.severity == "warning"
    assert alert.detail["command_type"] == "deliver"
    assert alert.detail["last_error"] == "sent_ack_timeout"
    # No credential/payload leak in the detail.
    assert set(alert.detail) == {"reason", "command_type", "account_id", "last_error"}
    assert _open_alerts(server_id, "recall_failed") == []


def test_sent_timeout_recall_over_cap_opens_recall_failed(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("d2alertrec@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, assignment_id, "active")
    with get_sessionmaker()() as db:
        commands.request_recall(db, tenant_id, assignment_id)
        command_id = db.scalar(
            select(AgentCommand.command_id).where(
                AgentCommand.assignment_id == assignment_id
            )
        )
    _age_sent(command_id, seconds=1000, send_attempts=5)

    with get_sessionmaker()() as db:
        commands.sweep_sent_timeouts(db, timeout_seconds=90, max_attempts=5)

    # recall re-uses the D1 kind; no command_send_failed for a recall.
    recall = _open_alerts(server_id, "recall_failed")
    assert len(recall) == 1
    assert recall[0].account_id == account_id
    assert _open_alerts(server_id, "command_send_failed") == []


def test_sent_timeout_server_scoped_over_cap_opens_no_alert(app_env):
    # Server-scoped commands self-heal via the next session's policy re-assertion,
    # so their silent final failure opens NO alert (would accumulate a manual
    # alert on a self-healing condition and double-open against server_offline).
    tenant_id, _account_id, server_id = _seed_tenant_account_server("d2alertsrv@ex.com")
    with get_sessionmaker()() as db:
        commands.request_switch_mode(db, tenant_id, server_id, mode="manual")
        command_id = db.scalar(
            select(AgentCommand.command_id).where(
                AgentCommand.command_type == "set_mode"
            )
        )
    _age_sent(command_id, seconds=1000, send_attempts=5)

    with get_sessionmaker()() as db:
        _, failed = commands.sweep_sent_timeouts(db, timeout_seconds=90, max_attempts=5)

    assert failed == [command_id]  # still marked failed
    assert _open_alerts(server_id, "command_send_failed") == []


def test_sent_timeout_switch_now_over_cap_opens_command_send_failed(app_env):
    # switch_now sets no pending marker and has no successor, so it is not
    # "superseded" — its cap-exhausted final failure must still alert.
    tenant_id, account_id, server_id = _seed_tenant_account_server("d2switch@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, assignment_id, "active")
    with get_sessionmaker()() as db:
        commands.request_switch_now(db, tenant_id, assignment_id)
        command_id = db.scalar(
            select(AgentCommand.command_id).where(
                AgentCommand.command_type == "switch_now"
            )
        )
    _age_sent(command_id, seconds=1000, send_attempts=5)

    with get_sessionmaker()() as db:
        _, failed = commands.sweep_sent_timeouts(db, timeout_seconds=90, max_attempts=5)

    assert failed == [command_id]
    opened = _open_alerts(server_id, "command_send_failed")
    assert len(opened) == 1
    assert opened[0].account_id == account_id
    assert opened[0].detail["command_type"] == "switch_now"


def test_settled_recall_final_failure_opens_no_alert(app_env):
    # A recall whose account was already detached/recovered by
    # _settle_recall_detached has pending_command_id=None while its command sits
    # sent. Its cap-exhausted failure must NOT open a misleading recall_failed.
    tenant_id, account_id, server_id = _seed_tenant_account_server("d2settled@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, assignment_id, "active")
    with get_sessionmaker()() as db:
        commands.request_recall(db, tenant_id, assignment_id)
        command_id = db.scalar(
            select(AgentCommand.command_id).where(
                AgentCommand.assignment_id == assignment_id
            )
        )
    # Simulate the settle: marker cleared while the command is still sent.
    _set_state(tenant_id, assignment_id, "detached", pending_command_id=None)
    _age_sent(command_id, seconds=1000, send_attempts=5)

    with get_sessionmaker()() as db:
        _, failed = commands.sweep_sent_timeouts(db, timeout_seconds=90, max_attempts=5)

    assert failed == [command_id]
    assert _open_alerts(server_id, "recall_failed") == []
    assert _open_alerts(server_id, "command_send_failed") == []


def test_superseded_command_final_failure_opens_no_alert(app_env):
    # A stuck sent command whose assignment a newer command already owns must not
    # alert — the successor reports its own result (review B guard 4).
    tenant_id, account_id, server_id = _seed_tenant_account_server("d2super@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, assignment_id, "pending")
    with get_sessionmaker()() as db:
        commands.request_deliver(db, tenant_id, assignment_id)
        old_command_id = db.scalar(
            select(AgentCommand.command_id).where(
                AgentCommand.assignment_id == assignment_id
            )
        )
    # A newer command takes over the assignment's pending marker.
    with get_sessionmaker()() as db:
        a = inventory.get_assignment(db, tenant_id, assignment_id)
        a.pending_command_id = "cmd_newer_owner"
        db.commit()
    _age_sent(old_command_id, seconds=1000, send_attempts=5)

    with get_sessionmaker()() as db:
        _, failed = commands.sweep_sent_timeouts(db, timeout_seconds=90, max_attempts=5)

    assert failed == [old_command_id]
    assert _open_alerts(server_id, "command_send_failed") == []
    # The superseded command did not clobber the newer owner's marker.
    a = _assignment(tenant_id, assignment_id)
    assert a.pending_command_id == "cmd_newer_owner"


def test_repeated_cap_exhaust_dedupes_to_one_alert(app_env):
    # Two failing deliver commands for the same assignment collapse onto one open
    # command_send_failed alert (partial-unique dedupe on account scope).
    tenant_id, account_id, server_id = _seed_tenant_account_server("d2dedupe@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    for _ in range(2):
        _set_state(tenant_id, assignment_id, "pending", pending_command_id=None)
        with get_sessionmaker()() as db:
            commands.request_deliver(db, tenant_id, assignment_id)
            command_id = db.scalar(
                select(AgentCommand.command_id).where(
                    AgentCommand.assignment_id == assignment_id,
                    AgentCommand.status == "queued",
                )
            )
        _age_sent(command_id, seconds=1000, send_attempts=5)
        with get_sessionmaker()() as db:
            commands.sweep_sent_timeouts(db, timeout_seconds=90, max_attempts=5)

    assert len(_open_alerts(server_id, "command_send_failed")) == 1


def test_converged_followup_auto_resolves_command_send_failed(app_env):
    # After a send failure opens the alert, a later deliver that acks CONVERGED
    # resolves it (the send path recovered).
    tenant_id, account_id, server_id = _seed_tenant_account_server("d2resolve@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, assignment_id, "pending")
    with get_sessionmaker()() as db:
        commands.request_deliver(db, tenant_id, assignment_id)
        command_id = db.scalar(
            select(AgentCommand.command_id).where(
                AgentCommand.assignment_id == assignment_id
            )
        )
    _age_sent(command_id, seconds=1000, send_attempts=5)
    with get_sessionmaker()() as db:
        commands.sweep_sent_timeouts(db, timeout_seconds=90, max_attempts=5)
    assert len(_open_alerts(server_id, "command_send_failed")) == 1

    # A fresh deliver that converges.
    _set_state(tenant_id, assignment_id, "pending", pending_command_id=None)
    with get_sessionmaker()() as db:
        commands.request_deliver(db, tenant_id, assignment_id)
        new_command_id = db.scalar(
            select(AgentCommand.command_id).where(
                AgentCommand.assignment_id == assignment_id,
                AgentCommand.status == "queued",
            )
        )
    with get_sessionmaker()() as db:
        reconcile.apply_ack(
            db, tenant_id=tenant_id, command_id=new_command_id, convergence="converged"
        )

    assert _open_alerts(server_id, "command_send_failed") == []


# -- no contention with reconcile ---------------------------------------------
def test_reconcile_ignores_inflight_delivering_assignment(app_env):
    # Ownership split: reconcile acts on resting-state assignments only, so a
    # delivering assignment with a live sent command gets no second command from
    # reconcile — the D2 sweep is the only writer for it.
    tenant_id, account_id, server_id = _seed_tenant_account_server("d2split@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, assignment_id, "pending")
    with get_sessionmaker()() as db:
        commands.request_deliver(db, tenant_id, assignment_id)  # -> delivering
        # Report says the account is absent locally; a resting assignment would be
        # redelivered, but a delivering one must not be.
        drift = reconcile.reconcile_from_report(
            db, tenant_id=tenant_id, server_id=server_id, reported={}
        )
        db.commit()
    assert drift == []
    # Only the original deliver command exists — reconcile queued nothing.
    with get_sessionmaker()() as db:
        rows = db.scalars(
            select(AgentCommand).where(AgentCommand.assignment_id == assignment_id)
        ).all()
    assert len(rows) == 1 and rows[0].command_type == "deliver"


# -- D3 queued-never-sent recovery ---------------------------------------------
def _deliver_and_age_queued(email, *, last_seen_at, age_seconds):
    """Seed, deliver, then force the server's last_seen_at, then age the command.

    ``last_seen_at`` is stamped only *after* ``request_deliver`` runs, not
    before: ``request_deliver`` now 409s outright when it is NULL
    (D3, commands.py), so a deliver command can never realistically be queued
    for a never-connected server in the first place — this helper reaches
    that DB state directly (server connected once, then rewound) purely to
    exercise the sweep's own query branching in isolation. The sweep's short
    tier chiefly matters for server-scoped commands (set_mode/req_report/
    set_policy/self_update), which have no such precondition and genuinely can
    be queued against a server that has never connected.
    """
    tenant_id, account_id, server_id = _seed_tenant_account_server(email)
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, assignment_id, "pending")
    with get_sessionmaker()() as db:
        commands.request_deliver(db, tenant_id, assignment_id)
        command_id = db.scalar(
            select(AgentCommand.command_id).where(
                AgentCommand.assignment_id == assignment_id
            )
        )
    with get_sessionmaker()() as db:
        server = inventory.get_server(db, tenant_id, server_id)
        server.last_seen_at = last_seen_at
        db.commit()
    _age_queued(command_id, seconds=age_seconds)
    return tenant_id, assignment_id, command_id


def test_queued_timeout_never_connected_server_fails_at_short_tier(app_env):
    # A command a poll loop never claimed for a server whose agent has never
    # once connected stays ``queued`` forever without this sweep:
    # pending_command_id never clears and the console shows "동기화중"
    # indefinitely. This tier ages out fast (180s default) — nothing has ever
    # been "in progress" for this server, so there is nothing to wait out.
    tenant_id, assignment_id, command_id = _deliver_and_age_queued(
        "d3never-queued@ex.com", last_seen_at=None, age_seconds=1000
    )
    assert _command(command_id).status == "queued"

    with get_sessionmaker()() as db:
        failed = commands.sweep_queued_timeouts(
            db, timeout_seconds=180, stale_seconds=1800
        )
    assert failed == [command_id]

    cmd = _command(command_id)
    assert cmd.status == "failed"
    assert cmd.detail == "queued_timeout"
    a = _assignment(tenant_id, assignment_id)
    assert a.state == "pending"  # re-issuable
    assert a.pending_command_id is None
    assert a.last_error == "queued_timeout"


def test_queued_timeout_previously_connected_server_waits_for_stale_tier(app_env):
    # A server that has connected before (just between sessions — restart,
    # maintenance) must NOT lose a queued deliver to the short tier: an
    # ordinary reconnect within that window would otherwise race the sweep and
    # sometimes lose. 1000s clears the 180s never-connected tier but must stay
    # queued until the much longer stale tier (1800s default) actually passes.
    now = datetime.now(UTC)
    tenant_id, assignment_id, command_id = _deliver_and_age_queued(
        "d3stale-wait@ex.com", last_seen_at=now, age_seconds=1000
    )

    with get_sessionmaker()() as db:
        failed = commands.sweep_queued_timeouts(
            db, timeout_seconds=180, stale_seconds=1800
        )
    assert failed == []
    assert _command(command_id).status == "queued"


def test_queued_timeout_previously_connected_server_fails_past_stale_tier(app_env):
    now = datetime.now(UTC)
    tenant_id, assignment_id, command_id = _deliver_and_age_queued(
        "d3stale-fail@ex.com", last_seen_at=now, age_seconds=2000
    )

    with get_sessionmaker()() as db:
        failed = commands.sweep_queued_timeouts(
            db, timeout_seconds=180, stale_seconds=1800
        )
    assert failed == [command_id]

    cmd = _command(command_id)
    assert cmd.status == "failed"
    a = _assignment(tenant_id, assignment_id)
    assert a.state == "pending"
    assert a.pending_command_id is None


def test_fresh_queued_is_not_swept(app_env):
    tenant_id, assignment_id, command_id = _deliver_and_age_queued(
        "d3fresh@ex.com", last_seen_at=None, age_seconds=5
    )  # well within the 180s never-connected timeout

    with get_sessionmaker()() as db:
        failed = commands.sweep_queued_timeouts(db, timeout_seconds=180)
    assert failed == []
    assert _command(command_id).status == "queued"


def test_sweep_sent_timeouts_also_sweeps_queued(app_env):
    # sweep_sent_timeouts is the only sweep the gRPC sweeper loop registers, so
    # it must fold the queued-timeout sweep into its own return value, in the
    # same transaction (before its own db.commit(), so it stays under the
    # advisory lock's transaction scope), or D3 never actually runs against a
    # live deployment.
    tenant_id, assignment_id, command_id = _deliver_and_age_queued(
        "d3folded@ex.com", last_seen_at=None, age_seconds=1000
    )

    with get_sessionmaker()() as db:
        requeued, failed = commands.sweep_sent_timeouts(
            db, timeout_seconds=90, queued_timeout_seconds=180, queued_stale_seconds=1800
        )
    assert requeued == []
    assert failed == [command_id]
    assert _command(command_id).status == "failed"


def test_deliver_to_never_connected_server_is_refused(app_env):
    # D3: a server whose agent has never once connected (last_seen_at NULL)
    # would never poll for a queued deliver command; reject up front instead
    # of letting it sit queued until the sweep ages it out. This does NOT
    # cover a too-old agent: an incompatible agent that got as far as
    # authenticating (enroll_token / server_credential) already has
    # last_seen_at stamped before its session aborts on the version/KEK check
    # (grpc/server.py _authenticate/_touch_server), so it passes this check —
    # only the queued sweep's stale tier catches that case.
    tenant_id, account_id, server_id = _seed_tenant_account_server("d3never@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, assignment_id, "pending")
    with get_sessionmaker()() as db:
        server = inventory.get_server(db, tenant_id, server_id)
        server.last_seen_at = None
        db.commit()

    with get_sessionmaker()() as db:
        with pytest.raises(ApiError) as exc_info:
            commands.request_deliver(db, tenant_id, assignment_id)
    assert exc_info.value.status == 409
    assert exc_info.value.code == "assignment.server_never_connected"
    # The assignment was not touched — still pending, no command enqueued.
    a = _assignment(tenant_id, assignment_id)
    assert a.state == "pending"
    assert a.pending_command_id is None
    with get_sessionmaker()() as db:
        rows = db.scalars(
            select(AgentCommand).where(AgentCommand.assignment_id == assignment_id)
        ).all()
    assert rows == []


def test_deliver_to_previously_connected_offline_server_is_allowed(app_env):
    # status == "offline" alone must NOT be rejected — that also covers an
    # agent that connected before and is merely between sessions, which
    # recovers on its own and should not block deliver.
    tenant_id, account_id, server_id = _seed_tenant_account_server("d3offline@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, assignment_id, "pending")
    with get_sessionmaker()() as db:
        server = inventory.get_server(db, tenant_id, server_id)
        server.status = "offline"
        db.commit()

    with get_sessionmaker()() as db:
        commands.request_deliver(db, tenant_id, assignment_id)

    a = _assignment(tenant_id, assignment_id)
    assert a.state == "delivering"
    assert a.pending_command_id is not None
