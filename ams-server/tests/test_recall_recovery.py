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
from app.services import commands, inventory, reconcile

from tests.test_grpc_channel import (
    _create_assignment,
    _seed_tenant_account_server,
)


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
