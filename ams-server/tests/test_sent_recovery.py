"""D2 sent-未ack recovery (recovery-architecture §2).

The poll loop marks a command ``sent`` but nothing re-sends it if the agent never
acks; :func:`commands.sweep_sent_timeouts` re-queues a stuck ``sent`` command
(idempotent, same command_id) up to MAX_SEND_ATTEMPTS, then fails it and reverts
its assignment. These drive the sweep directly with an aged ``sent_at`` — the
same primitive the offline-sweeper sibling calls on its timer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db import get_sessionmaker
from app.models import AgentCommand
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


def _command(command_id):
    with get_sessionmaker()() as db:
        return db.scalar(
            select(AgentCommand).where(AgentCommand.command_id == command_id)
        )


def _assignment(tenant_id, assignment_id):
    with get_sessionmaker()() as db:
        return inventory.get_assignment(db, tenant_id, assignment_id)


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
