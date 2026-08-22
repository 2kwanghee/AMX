"""D1 recall-failure recovery (recovery-architecture §1).

A recall whose ack was DIVERGED/REJECTED (or lost) sits ``recalling`` with
``pending_command_id`` NULL — the *settled* recalling state. Before D1 nothing
picked it up and it was stranded forever. Now reconcile-on-report re-recalls it
(if still present) or settles it to ``detached`` (if already gone), and the REST
recall action re-arms it manually. In-flight recalls (pending_command_id set) are
untouched — their command is still live.
"""

from __future__ import annotations

import pytest

from sqlalchemy import select

from app.core.errors import ApiError
from app.db import get_sessionmaker
from app.models import Account, AgentCommand, Alert
from app.services import admins, commands, inventory, reconcile

from tests.test_grpc_channel import (
    _create_assignment,
    _seed_tenant_account_server,
)
from tests.test_sent_recovery import _age_queued, _age_sent, _open_alerts


# -- helpers ------------------------------------------------------------------
def _settle_recalling(tenant_id, assignment_id, *, pending_command_id=None):
    with get_sessionmaker()() as db:
        a = inventory.get_assignment(db, tenant_id, assignment_id)
        a.state = "recalling"
        a.pending_command_id = pending_command_id
        db.commit()


def _set_account_status(tenant_id, account_id, status):
    with get_sessionmaker()() as db:
        acct = db.scalar(
            select(Account).where(
                Account.id == account_id, Account.tenant_id == tenant_id
            )
        )
        acct.status = status
        db.commit()


def _correction_rows(assignment_id, correction):
    with get_sessionmaker()() as db:
        return db.scalars(
            select(AgentCommand).where(
                AgentCommand.assignment_id == assignment_id,
                AgentCommand.payload["reconcile_correction"].astext == correction,
            )
        ).all()


def _set_state(tenant_id, assignment_id, state):
    with get_sessionmaker()() as db:
        a = inventory.get_assignment(db, tenant_id, assignment_id)
        a.state = state
        db.commit()


def _set_retry_count(tenant_id, assignment_id, n):
    with get_sessionmaker()() as db:
        a = inventory.get_assignment(db, tenant_id, assignment_id)
        a.recall_retry_count = n
        db.commit()


def _open_recall_failed(server_id):
    with get_sessionmaker()() as db:
        return db.scalars(
            select(Alert).where(
                Alert.server_id == server_id,
                Alert.kind == "recall_failed",
                Alert.status == "open",
            )
        ).all()


def _fail_recall(tenant_id, account_id, server_id, assignment_id, convergence):
    """Drive an installed assignment through recall then a failing ack."""
    _set_state(tenant_id, assignment_id, "active")
    with get_sessionmaker()() as db:
        a = commands.request_recall(db, tenant_id, assignment_id)
        command_id = a.pending_command_id
    with get_sessionmaker()() as db:
        reconcile.apply_ack(
            db,
            tenant_id=tenant_id,
            command_id=command_id,
            convergence=convergence,
            error_code="tsamx_disable_failed",
        )
    return command_id


def _assignment(tenant_id, assignment_id):
    with get_sessionmaker()() as db:
        return inventory.get_assignment(db, tenant_id, assignment_id)


def _account(tenant_id, account_id):
    with get_sessionmaker()() as db:
        return db.scalar(
            select(Account).where(
                Account.id == account_id, Account.tenant_id == tenant_id
            )
        )


# -- reconcile picks up settled recalling -------------------------------------
def test_settled_recalling_still_present_rerecalls(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("d1present@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _settle_recalling(tenant_id, assignment_id)

    with get_sessionmaker()() as db:
        drift = reconcile.reconcile_from_report(
            db,
            tenant_id=tenant_id,
            server_id=server_id,
            reported={str(account_id): reconcile.ACTUAL_ACTIVE},  # still present
        )
        db.commit()
    assert len(drift) == 1
    assert drift[0]["correction"] == "recall"
    assert drift[0]["corrected"] is True
    rows = _correction_rows(assignment_id, "recall")
    assert len(rows) == 1 and rows[0].command_type == "recall"


def test_settled_recalling_already_absent_settles_detached(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("d1absent@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _settle_recalling(tenant_id, assignment_id)
    _set_account_status(tenant_id, account_id, "assigned")

    with get_sessionmaker()() as db:
        drift = reconcile.reconcile_from_report(
            db,
            tenant_id=tenant_id,
            server_id=server_id,
            reported={},  # account already gone locally
        )
        db.commit()
    # Convergence, not drift: no entry, no command.
    assert drift == []
    assert _correction_rows(assignment_id, "recall") == []
    a = _assignment(tenant_id, assignment_id)
    assert a.state == "detached"
    assert a.pending_command_id is None
    assert a.last_error is None
    # account returned to the pool (mirror of recall CONVERGED).
    assert _account(tenant_id, account_id).status == "available"


def test_inflight_recalling_is_not_reconciled(app_env):
    # pending_command_id set => a live recall command; reconcile must skip it.
    tenant_id, account_id, server_id = _seed_tenant_account_server("d1inflight@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _settle_recalling(tenant_id, assignment_id, pending_command_id="cmd_live")

    with get_sessionmaker()() as db:
        drift = reconcile.reconcile_from_report(
            db,
            tenant_id=tenant_id,
            server_id=server_id,
            reported={str(account_id): reconcile.ACTUAL_ACTIVE},
        )
        db.commit()
    assert drift == []
    assert _correction_rows(assignment_id, "recall") == []


def test_settled_recalling_rerecall_honours_cap(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("d1cap@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _settle_recalling(tenant_id, assignment_id)
    cap = 2

    for _ in range(cap + 3):
        with get_sessionmaker()() as db:
            reconcile.reconcile_from_report(
                db,
                tenant_id=tenant_id,
                server_id=server_id,
                reported={str(account_id): reconcile.ACTUAL_ACTIVE},
                correction_cap=cap,
            )
            db.commit()
        # converge the queued correction so the next round counts against the cap
        # rather than being blocked by the in-flight guard.
        with get_sessionmaker()() as db:
            for r in db.scalars(
                select(AgentCommand).where(
                    AgentCommand.server_id == server_id,
                    AgentCommand.status.in_(("queued", "sent")),
                )
            ).all():
                r.status = "acked"
            db.commit()

    assert len(_correction_rows(assignment_id, "recall")) == cap


# -- REST escape hatch --------------------------------------------------------
def test_rest_recall_accepts_settled_recalling(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("d1rest@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _settle_recalling(tenant_id, assignment_id)

    with get_sessionmaker()() as db:
        a = commands.request_recall(db, tenant_id, assignment_id)
        assert a.state == "recalling"
        assert a.pending_command_id is not None  # re-armed as in-flight
    # request_recall does not tag reconcile_correction; assert the raw recall row.
    with get_sessionmaker()() as db:
        raw = db.scalars(
            select(AgentCommand).where(
                AgentCommand.assignment_id == assignment_id,
                AgentCommand.command_type == "recall",
            )
        ).all()
    assert len(raw) == 1


def test_rest_recall_rejects_inflight_recalling(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("d1restrej@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _settle_recalling(tenant_id, assignment_id, pending_command_id="cmd_live")

    with get_sessionmaker()() as db:
        with pytest.raises(ApiError):
            commands.request_recall(db, tenant_id, assignment_id)


# -- recall_failed alert on the confirm path ----------------------------------
@pytest.mark.parametrize("convergence", [reconcile.DIVERGED, reconcile.REJECTED])
def test_recall_ack_failure_opens_alert_and_settles(app_env, convergence):
    tenant_id, account_id, server_id = _seed_tenant_account_server(
        f"d1fail{convergence}@ex.com"
    )
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _fail_recall(tenant_id, account_id, server_id, assignment_id, convergence)

    # Settled recalling: state kept, pending marker cleared, error recorded.
    a = _assignment(tenant_id, assignment_id)
    assert a.state == "recalling"
    assert a.pending_command_id is None
    assert a.last_error == "tsamx_disable_failed"
    # An account-scoped recall_failed alert is opened for the operator.
    alerts_open = _open_recall_failed(server_id)
    assert len(alerts_open) == 1
    assert alerts_open[0].account_id == account_id
    assert alerts_open[0].severity == "warning"


def test_recall_failure_alert_dedupes(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("d1dedupe@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _fail_recall(tenant_id, account_id, server_id, assignment_id, reconcile.DIVERGED)
    # A second failing recall cycle on the same account must not stack a 2nd alert.
    _fail_recall(tenant_id, account_id, server_id, assignment_id, reconcile.REJECTED)
    assert len(_open_recall_failed(server_id)) == 1


def test_recall_converged_resolves_alert_and_resets_counter(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("d1resolve@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _fail_recall(tenant_id, account_id, server_id, assignment_id, reconcile.DIVERGED)
    assert len(_open_recall_failed(server_id)) == 1

    # Operator re-arms; this time the recall converges.
    with get_sessionmaker()() as db:
        a = commands.request_recall(db, tenant_id, assignment_id)
        command_id = a.pending_command_id
        assert a.recall_retry_count == 1  # the re-arm counted
    with get_sessionmaker()() as db:
        reconcile.apply_ack(
            db, tenant_id=tenant_id, command_id=command_id, convergence=reconcile.CONVERGED
        )

    a = _assignment(tenant_id, assignment_id)
    assert a.state == "detached"
    assert a.recall_retry_count == 0  # reset on success
    assert _open_recall_failed(server_id) == []  # alert auto-resolved


# -- REST manual-retry cap ----------------------------------------------------
def test_rest_recall_retry_cap_returns_409_and_alerts(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("d1cap409@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    # Settled recalling already at the retry cap.
    _settle_recalling(tenant_id, assignment_id)
    _set_retry_count(tenant_id, assignment_id, commands.MAX_RECALL_RETRIES)

    with get_sessionmaker()() as db:
        with pytest.raises(ApiError) as excinfo:
            commands.request_recall(db, tenant_id, assignment_id)
    assert excinfo.value.status == 409
    # No new recall command was queued past the cap.
    with get_sessionmaker()() as db:
        rows = db.scalars(
            select(AgentCommand).where(
                AgentCommand.assignment_id == assignment_id,
                AgentCommand.command_type == "recall",
            )
        ).all()
    assert rows == []
    # A recall_failed alert is opened for operator intervention.
    assert len(_open_recall_failed(server_id)) == 1


def test_rest_recall_retry_increments_under_cap(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("d1capinc@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _settle_recalling(tenant_id, assignment_id)
    _set_retry_count(tenant_id, assignment_id, commands.MAX_RECALL_RETRIES - 1)

    with get_sessionmaker()() as db:
        a = commands.request_recall(db, tenant_id, assignment_id)
        assert a.recall_retry_count == commands.MAX_RECALL_RETRIES
        assert a.state == "recalling"
        assert a.pending_command_id is not None


# -- force escape hatch past the cap (global-admin only) ----------------------
def test_force_recall_global_admin_bypasses_cap(app_env, client):
    tenant_id, account_id, server_id = _seed_tenant_account_server("d1force@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _settle_recalling(tenant_id, assignment_id)
    _set_retry_count(tenant_id, assignment_id, commands.MAX_RECALL_RETRIES)
    base = f"/api/v1/tenants/{tenant_id}/assignments/{assignment_id}"

    # At the cap the plain recall is blocked...
    assert client.post(f"{base}:recall").status_code == 409
    # ...but the bootstrap client is a global-admin; force bypasses and re-arms.
    r = client.post(f"{base}:recall", json={"force": True})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "recalling"
    a = _assignment(tenant_id, assignment_id)
    assert a.recall_retry_count == 0
    assert a.pending_command_id is not None


def test_force_recall_tenant_admin_forbidden(app_env, client, db):
    tenant_id, account_id, server_id = _seed_tenant_account_server("d1forcerbac@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _settle_recalling(tenant_id, assignment_id)
    _set_retry_count(tenant_id, assignment_id, commands.MAX_RECALL_RETRIES)
    # A tenant-admin scoped to THIS tenant (so TenantScope passes and the 403 is a
    # real capability refusal, not a hidden 404).
    admins.create_admin(
        db,
        email="ta-d1@ex.com",
        password="pw-correct-horse",
        role="tenant-admin",
        tenant_id=tenant_id,
    )
    db.commit()
    token = client.post(
        "/api/v1/auth/login",
        json={"email": "ta-d1@ex.com", "password": "pw-correct-horse"},
    ).json()["sessionToken"]
    base = f"/api/v1/tenants/{tenant_id}/assignments/{assignment_id}"

    r = client.post(
        f"{base}:recall",
        json={"force": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403, r.text
    # No new recall command was queued for the refused force.
    with get_sessionmaker()() as db2:
        rows = db2.scalars(
            select(AgentCommand).where(
                AgentCommand.assignment_id == assignment_id,
                AgentCommand.command_type == "recall",
            )
        ).all()
    assert rows == []


# -- pending recall (never delivered) settles straight to detached -----------
# A ``pending`` assignment has never been delivered, so recall has nothing to
# undo remotely — request_recall settles it to ``detached`` in place instead of
# enqueueing a command.
def test_rest_recall_pending_settles_to_detached_no_command(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("d1pending@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    assert _assignment(tenant_id, assignment_id).state == "pending"

    with get_sessionmaker()() as db:
        a = commands.request_recall(db, tenant_id, assignment_id)

    assert a.state == "detached"
    assert a.pending_command_id is None
    assert a.last_error is None
    assert a.recall_retry_count == 0
    assert _account(tenant_id, account_id).status == "available"
    with get_sessionmaker()() as db:
        rows = db.scalars(
            select(AgentCommand).where(
                AgentCommand.assignment_id == assignment_id,
                AgentCommand.command_type == "recall",
            )
        ).all()
    assert rows == []  # no command was ever issued to undo


def test_rest_recall_after_queued_timeout_pending_settles_to_detached(app_env):
    # D3: a queued deliver that never got polled ages out and reverts the
    # assignment to 'pending' (commands._fail_queued_rows). Recall from that
    # resting state must take the same no-command detach path as a freshly
    # created pending assignment.
    tenant_id, account_id, server_id = _seed_tenant_account_server("d1qtpending@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    with get_sessionmaker()() as db:
        commands.request_deliver(db, tenant_id, assignment_id)
        command_id = db.scalar(
            select(AgentCommand.command_id).where(
                AgentCommand.assignment_id == assignment_id
            )
        )
    # _seed_tenant_account_server stamps last_seen_at so request_deliver's
    # never-connected guard passes; null it back out here so the short
    # never-connected sweep tier (rather than the long stale tier) applies —
    # matching test_sent_recovery's own D3 fixture pattern.
    with get_sessionmaker()() as db:
        server = inventory.get_server(db, tenant_id, server_id)
        server.last_seen_at = None
        db.commit()
    _age_queued(command_id, seconds=1000)
    with get_sessionmaker()() as db:
        failed = commands.sweep_queued_timeouts(db, timeout_seconds=180, stale_seconds=1800)
    assert failed == [command_id]
    reverted = _assignment(tenant_id, assignment_id)
    assert reverted.state == "pending"
    assert reverted.last_error == "queued_timeout"

    with get_sessionmaker()() as db:
        a = commands.request_recall(db, tenant_id, assignment_id)

    assert a.state == "detached"
    assert a.pending_command_id is None
    assert a.last_error is None
    assert _account(tenant_id, account_id).status == "available"
    with get_sessionmaker()() as db:
        rows = db.scalars(
            select(AgentCommand).where(
                AgentCommand.assignment_id == assignment_id,
                AgentCommand.command_type == "recall",
            )
        ).all()
    assert rows == []


def test_rest_recall_after_sent_ack_timeout_pending_preserves_alert_and_error(app_env):
    # D2: a deliver stuck 'sent' past its ack timeout means the agent may have
    # already picked up and installed the payload before the ack was lost — a
    # stranded remote install is possible. sweep_sent_timeouts reverts this to
    # 'pending' (last_error='sent_ack_timeout') and opens command_send_failed.
    # Recall from THIS pending must still detach + free the account (the
    # operator explicitly asked), but — unlike the plain-pending and
    # queued_timeout cases — must NOT wipe the only visible signal of a
    # possible remnant: last_error stays, and command_send_failed stays open.
    tenant_id, account_id, server_id = _seed_tenant_account_server("d1sentpending@ex.com")
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

    reverted = _assignment(tenant_id, assignment_id)
    assert reverted.state == "pending"
    assert reverted.last_error == "sent_ack_timeout"
    assert len(_open_alerts(server_id, "command_send_failed")) == 1

    with get_sessionmaker()() as db:
        a = commands.request_recall(db, tenant_id, assignment_id)

    assert a.state == "detached"
    assert a.pending_command_id is None
    assert a.acked_at is None  # never actually acked
    # last_error preserved — the only visible sign of a possible remnant.
    assert a.last_error == "sent_ack_timeout"
    # command_send_failed stays open; only a fresh reconcile drift check (the
    # agent reporting the account still present) should ever clear a genuine
    # remnant.
    assert len(_open_alerts(server_id, "command_send_failed")) == 1
    assert _account(tenant_id, account_id).status == "available"
    with get_sessionmaker()() as db:
        rows = db.scalars(
            select(AgentCommand).where(
                AgentCommand.assignment_id == assignment_id,
                AgentCommand.command_type == "recall",
            )
        ).all()
    assert rows == []


@pytest.mark.parametrize("state", ["delivering", "active", "inactive", "quarantined"])
def test_rest_recall_installed_states_unchanged(app_env, state):
    # Sanity: the new pending fast-path must not disturb the existing in-flight
    # recall behaviour for every other recallable state — a real command is
    # still enqueued and the assignment moves to 'recalling'.
    tenant_id, account_id, server_id = _seed_tenant_account_server(
        f"d1installed{state}@ex.com"
    )
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, assignment_id, state)

    with get_sessionmaker()() as db:
        a = commands.request_recall(db, tenant_id, assignment_id)

    assert a.state == "recalling"
    assert a.pending_command_id is not None
    with get_sessionmaker()() as db:
        rows = db.scalars(
            select(AgentCommand).where(
                AgentCommand.assignment_id == assignment_id,
                AgentCommand.command_type == "recall",
            )
        ).all()
    assert len(rows) == 1


def test_new_assignment_after_pending_recall_detach(app_env):
    # The account freed by a pending-recall detach must be immediately
    # re-assignable, exactly like a normal recall-to-detached.
    tenant_id, account_id, server_id = _seed_tenant_account_server("d1reassign@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    with get_sessionmaker()() as db:
        commands.request_recall(db, tenant_id, assignment_id)
    assert _account(tenant_id, account_id).status == "available"

    new_id = _create_assignment(tenant_id, account_id, server_id)
    assert new_id != assignment_id
    assert _assignment(tenant_id, new_id).state == "pending"
    assert _account(tenant_id, account_id).status == "assigned"


# -- stale recall ack does not resurrect a bogus alert ------------------------
def test_stale_recall_ack_after_settle_opens_no_alert(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("d1stale@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, assignment_id, "active")
    with get_sessionmaker()() as db:
        a = commands.request_recall(db, tenant_id, assignment_id)
        command_id = a.pending_command_id
    # The assignment settles elsewhere (recall converged / re-delivered) before the
    # stale failing ack arrives.
    _set_state(tenant_id, assignment_id, "detached")
    with get_sessionmaker()() as db:
        reconcile.apply_ack(
            db,
            tenant_id=tenant_id,
            command_id=command_id,
            convergence=reconcile.DIVERGED,
            error_code="stale",
        )
    assert _open_recall_failed(server_id) == []
