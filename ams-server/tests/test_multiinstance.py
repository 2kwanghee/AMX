"""F3 multi-instance safety (design note §F3, §5.4).

Two claims, both about two AMS instances sharing one PostgreSQL:

* ``fetch_queued`` uses ``FOR UPDATE SKIP LOCKED`` so a queued command a first
  instance has locked is invisible to a second instance's poll — the same row is
  never handed to two agent sessions. The claim (mark ``sent``) lands in the same
  transaction as the fetch, so once committed the row is no longer ``queued``.
* the offline / sent-ack sweepers take a transaction-scoped advisory lock, so
  only one instance runs each sweep per tick; an instance that cannot acquire the
  lock skips the tick, and the lock frees the moment the holder's transaction ends.

Everything here runs against the real PostgreSQL container (SQLite has neither
row-level ``SKIP LOCKED`` nor advisory locks).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.db import get_engine, get_sessionmaker
from app.grpc.server import (
    _OFFLINE_SWEEP_LOCK_KEY,
    _SENT_SWEEP_LOCK_KEY,
    _sweep_once,
    _sweep_sent_once,
)
from app.models import AgentCommand, Server
from app.services import commands

from tests.test_grpc_channel import _seed_tenant_account_server


def _enqueue_queued(tenant_id, server_id, command_type="req_report") -> str:
    with get_sessionmaker()() as db:
        cmd = commands.enqueue_server(
            db,
            tenant_id=tenant_id,
            server_id=server_id,
            command_type=command_type,
            payload={},
        )
        db.commit()
        return cmd.command_id


# -- SKIP LOCKED --------------------------------------------------------------
def test_fetch_queued_skips_a_row_locked_by_another_session(app_env):
    """A row a first session has locked is skipped by a second session's fetch."""
    tenant_id, _account_id, server_id = _seed_tenant_account_server()
    command_id = _enqueue_queued(tenant_id, server_id)

    sm = get_sessionmaker()
    db1 = sm()
    try:
        locked = commands.fetch_queued(db1, server_id)  # holds FOR UPDATE lock
        assert [c.command_id for c in locked] == [command_id]

        db2 = sm()
        try:
            # Second instance polling the same server sees nothing — the only
            # queued row is locked, so SKIP LOCKED omits it (no duplicate send).
            assert commands.fetch_queued(db2, server_id) == []
        finally:
            db2.rollback()
            db2.close()
    finally:
        db1.rollback()  # release the lock
        db1.close()

    # Once the first session lets go, the row is grabbable again.
    db3 = sm()
    try:
        assert [c.command_id for c in commands.fetch_queued(db3, server_id)] == [
            command_id
        ]
    finally:
        db3.rollback()
        db3.close()


def test_claim_sent_in_fetch_transaction_hides_row_from_the_next_poll(app_env):
    """fetch -> claim_sent -> commit in one transaction: the row leaves 'queued'.

    This is the atomic hand-off _build_queued_commands performs. After it, a
    concurrent poller finds the row 'sent', never 'queued', so it is not re-sent.
    """
    tenant_id, _account_id, server_id = _seed_tenant_account_server()
    command_id = _enqueue_queued(tenant_id, server_id)

    sm = get_sessionmaker()
    with sm() as db:
        rows = commands.fetch_queued(db, server_id)
        assert len(rows) == 1
        commands.claim_sent(rows[0])
        db.commit()

    with sm() as db:
        assert commands.fetch_queued(db, server_id) == []
        claimed = db.scalar(
            select(AgentCommand).where(AgentCommand.command_id == command_id)
        )
        assert claimed.status == "sent"
        assert claimed.sent_at is not None


# -- sweeper single-instance (advisory lock) ----------------------------------
def _age_server_offline(server_id) -> None:
    with get_sessionmaker()() as db:
        server = db.get(Server, server_id)
        server.status = "online"
        server.last_seen_at = datetime.now(UTC) - timedelta(hours=1)
        db.commit()


def test_offline_sweep_skips_when_advisory_lock_is_held(app_env):
    """A second instance's offline sweep is a no-op while the lock is held, then
    runs normally once it is released."""
    tenant_id, _account_id, server_id = _seed_tenant_account_server()
    _age_server_offline(server_id)

    engine = get_engine()
    conn = engine.connect()
    trans = conn.begin()
    try:
        # Simulate instance A owning the offline-sweep lock for this tick.
        held = conn.scalar(select(func.pg_try_advisory_xact_lock(_OFFLINE_SWEEP_LOCK_KEY)))
        assert held is True

        # Instance B cannot acquire it -> skips, sweeps nothing, server stays online.
        assert _sweep_once(get_sessionmaker(), 90.0) == []
        with get_sessionmaker()() as db:
            assert db.get(Server, server_id).status == "online"
    finally:
        trans.rollback()  # instance A's tick ends -> lock released
        conn.close()

    # Next tick, the lock is free: the stale server is swept offline.
    swept = _sweep_once(get_sessionmaker(), 90.0)
    assert server_id in swept
    with get_sessionmaker()() as db:
        assert db.get(Server, server_id).status == "offline"


def test_sent_sweep_skips_when_advisory_lock_is_held(app_env):
    """The sent-ack sweep is independently gated by its own advisory lock."""
    tenant_id, _account_id, server_id = _seed_tenant_account_server()
    command_id = _enqueue_queued(tenant_id, server_id)
    # Age the command into a stuck 'sent' the sweeper would re-queue.
    with get_sessionmaker()() as db:
        cmd = db.scalar(
            select(AgentCommand).where(AgentCommand.command_id == command_id)
        )
        cmd.status = "sent"
        cmd.sent_at = datetime.now(UTC) - timedelta(seconds=600)
        db.commit()

    engine = get_engine()
    conn = engine.connect()
    trans = conn.begin()
    try:
        held = conn.scalar(select(func.pg_try_advisory_xact_lock(_SENT_SWEEP_LOCK_KEY)))
        assert held is True

        requeued, failed = _sweep_sent_once(get_sessionmaker())
        assert (requeued, failed) == ([], [])
        with get_sessionmaker()() as db:
            assert db.scalar(
                select(AgentCommand).where(AgentCommand.command_id == command_id)
            ).status == "sent"  # untouched: sweep was skipped
    finally:
        trans.rollback()
        conn.close()

    requeued, failed = _sweep_sent_once(get_sessionmaker())
    assert command_id in requeued
    with get_sessionmaker()() as db:
        assert db.scalar(
            select(AgentCommand).where(AgentCommand.command_id == command_id)
        ).status == "queued"


def test_offline_and_sent_locks_are_independent(app_env):
    """Distinct keys: holding the offline lock must not block the sent sweep."""
    tenant_id, _account_id, server_id = _seed_tenant_account_server()

    engine = get_engine()
    conn = engine.connect()
    trans = conn.begin()
    try:
        assert conn.scalar(
            select(func.pg_try_advisory_xact_lock(_OFFLINE_SWEEP_LOCK_KEY))
        ) is True
        # A different key -> the sent sweep still acquires and runs (no rows, ok).
        assert _sweep_sent_once(get_sessionmaker()) == ([], [])
    finally:
        trans.rollback()
        conn.close()
