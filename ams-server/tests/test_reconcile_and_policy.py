"""P3 track AMS: reconcile-on-report, SetPolicy session re-assertion, and the
REST switching-control wiring (design note decisions 3, 5, 7; §5, O4-C).

The reconcile tests drive :func:`reconcile.reconcile_from_report` directly with a
synthetic actual-state map (the same primitives the gRPC layer feeds it). The
re-assertion tests stand up the real gRPC server and read the command stream, as
the AMA daemon will.
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import func, select

from app.db import get_sessionmaker
from app.grpc import signing
from app.grpc.proto import pb, pb_grpc
from app.grpc.server import command_signature_valid, create_server
from app.models import AgentCommand, Assignment, Server, UsageSnapshot
from app.services import commands, inventory, reconcile

from tests.test_grpc_channel import (
    AGENT_ID,
    _Harness,
    _create_assignment,
    _issue_enroll,
    _read,
    _seed_tenant_account_server,
)


# -- helpers ------------------------------------------------------------------
def _set_state(tenant_id, assignment_id, state):
    with get_sessionmaker()() as db:
        a = inventory.get_assignment(db, tenant_id, assignment_id)
        a.state = state
        db.commit()


def _correction_rows(tenant_id, assignment_id, correction):
    with get_sessionmaker()() as db:
        return db.scalars(
            select(AgentCommand).where(
                AgentCommand.assignment_id == assignment_id,
                AgentCommand.payload["reconcile_correction"].astext == correction,
            )
        ).all()


def _ack_queued(server_id):
    """Mark every queued/sent command for a server as acked (simulate delivery
    + convergence) so it stops counting as in-flight for the loop guard."""
    with get_sessionmaker()() as db:
        rows = db.scalars(
            select(AgentCommand).where(
                AgentCommand.server_id == server_id,
                AgentCommand.status.in_(("queued", "sent")),
            )
        ).all()
        for r in rows:
            r.status = "acked"
        db.commit()


# -- reconcile-on-report ------------------------------------------------------
def test_reconcile_marks_drift_and_redelivers_absent_account(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("drift@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, assignment_id, "active")

    with get_sessionmaker()() as db:
        snap = UsageSnapshot(
            tenant_id=tenant_id, server_id=server_id, report_type="usage", payload={}
        )
        db.add(snap)
        db.flush()
        # Actual report names no accounts: the active assignment's account is
        # absent locally -> drift + narrow redeliver correction (rule 3).
        drift = reconcile.reconcile_from_report(
            db, tenant_id=tenant_id, server_id=server_id, reported={}, snapshot=snap
        )
        db.commit()
        assert len(drift) == 1
        assert drift[0]["correction"] == "redeliver"
        assert drift[0]["corrected"] is True
        assert snap.drift is not None and len(snap.drift) == 1

    rows = _correction_rows(tenant_id, assignment_id, "redeliver")
    assert len(rows) == 1
    assert rows[0].command_type == "deliver"
    assert rows[0].payload["desired_status"] == "active"


def test_reconcile_no_drift_when_actual_matches_and_ignores_is_current(app_env):
    # is_current is deliberately not part of `reported`; a manual/auto switch that
    # only flips is_current must not read as drift. Actual status == desired.
    tenant_id, account_id, server_id = _seed_tenant_account_server("match@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, assignment_id, "active")

    with get_sessionmaker()() as db:
        drift = reconcile.reconcile_from_report(
            db,
            tenant_id=tenant_id,
            server_id=server_id,
            reported={str(account_id): reconcile.ACTUAL_ACTIVE},
        )
        db.commit()
    assert drift == []
    assert _correction_rows(tenant_id, assignment_id, "redeliver") == []


def test_reconcile_mismatch_without_safe_correction_is_alarm_only(app_env):
    # active assignment but actual INACTIVE: real drift, but not one of the two
    # safe idempotent cases -> alarmed, no command queued (전면 교정 금지).
    tenant_id, account_id, server_id = _seed_tenant_account_server("mismatch@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, assignment_id, "active")

    with get_sessionmaker()() as db:
        drift = reconcile.reconcile_from_report(
            db,
            tenant_id=tenant_id,
            server_id=server_id,
            reported={str(account_id): reconcile.ACTUAL_INACTIVE},
        )
        db.commit()
    assert len(drift) == 1
    assert drift[0]["correction"] is None
    with get_sessionmaker()() as db:
        assert db.scalar(select(func.count()).select_from(AgentCommand)) == 0


def test_reconcile_detached_but_present_rerecalls(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("detach@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, assignment_id, "detached")

    with get_sessionmaker()() as db:
        drift = reconcile.reconcile_from_report(
            db,
            tenant_id=tenant_id,
            server_id=server_id,
            reported={str(account_id): reconcile.ACTUAL_ACTIVE},
        )
        db.commit()
    assert len(drift) == 1
    assert drift[0]["correction"] == "recall"
    rows = _correction_rows(tenant_id, assignment_id, "recall")
    assert len(rows) == 1 and rows[0].command_type == "recall"


def test_reconcile_correction_honours_loop_cap(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("loop@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, assignment_id, "active")
    cap = 2

    for _ in range(cap + 3):
        with get_sessionmaker()() as db:
            reconcile.reconcile_from_report(
                db,
                tenant_id=tenant_id,
                server_id=server_id,
                reported={},
                correction_cap=cap,
            )
            db.commit()
        # converge the just-queued correction so the next round is not blocked by
        # the in-flight guard and instead counts against the cap.
        _ack_queued(server_id)

    rows = _correction_rows(tenant_id, assignment_id, "redeliver")
    assert len(rows) == cap  # never exceeds the cap despite persistent drift


def test_reconcile_detached_covered_by_live_assignment_is_not_drift(app_env):
    # A detached history row whose account was legitimately re-installed by a live
    # assignment must not trigger a recall.
    tenant_id, account_id, server_id = _seed_tenant_account_server("cover@ex.com")
    detached_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, detached_id, "detached")
    live_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, live_id, "active")

    with get_sessionmaker()() as db:
        drift = reconcile.reconcile_from_report(
            db,
            tenant_id=tenant_id,
            server_id=server_id,
            reported={str(account_id): reconcile.ACTUAL_ACTIVE},
        )
        db.commit()
    assert drift == []


def test_reconcile_scoped_to_tenant_and_server(app_env):
    # Another tenant's identical drift must be invisible to this reconcile call.
    tenant_a, account_a, server_a = _seed_tenant_account_server("a@iso.com")
    assignment_a = _create_assignment(tenant_a, account_a, server_a)
    _set_state(tenant_a, assignment_a, "active")
    tenant_b, account_b, server_b = _seed_tenant_account_server("b@iso.com")
    assignment_b = _create_assignment(tenant_b, account_b, server_b)
    _set_state(tenant_b, assignment_b, "active")

    with get_sessionmaker()() as db:
        drift = reconcile.reconcile_from_report(
            db, tenant_id=tenant_a, server_id=server_a, reported={}
        )
        db.commit()
    assert len(drift) == 1
    assert drift[0]["assignment_id"] == str(assignment_a)
    # B untouched: no correction queued for B.
    assert _correction_rows(tenant_b, assignment_b, "redeliver") == []


# -- SetPolicy / SetSwitchMode session re-assertion ---------------------------
def _set_policy_columns(server_id, *, switch_mode, threshold_pct, default_strategy):
    with get_sessionmaker()() as db:
        server = db.get(Server, server_id)
        server.switch_mode = switch_mode
        server.threshold_pct = threshold_pct
        server.default_strategy = default_strategy
        db.commit()


def test_session_reasserts_set_mode_and_set_policy_bound_to_agent(app_env):
    signer = signing.Signer.from_env_or_generate()
    tenant_id, _account_id, server_id = _seed_tenant_account_server("reassert@ex.com")
    _set_policy_columns(
        server_id, switch_mode="manual", threshold_pct=90.0, default_strategy="best"
    )
    token = _issue_enroll(tenant_id, server_id)

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            stub = pb_grpc.AmxControlPlaneStub(channel)
            call = stub.Session()
            await call.write(
                pb.AmaMessage(register=pb.Register(agent_id=AGENT_ID, enroll_token=token))
            )
            setup = await _read(call)
            assert setup.WhichOneof("cmd") == "session_setup"
            set_mode = await _read(call)
            set_policy = await _read(call)
            await call.done_writing()
            return setup, set_mode, set_policy

    setup, set_mode, set_policy = asyncio.run(scenario())

    # Order SessionSetup -> SetSwitchMode -> SetPolicy, all signed + agent-bound.
    assert set_mode.WhichOneof("cmd") == "set_mode"
    assert set_mode.target_agent_id == AGENT_ID
    assert command_signature_valid(signer.public_key(), set_mode)
    assert set_mode.set_mode.mode == pb.SWITCH_MODE_MANUAL

    assert set_policy.WhichOneof("cmd") == "set_policy"
    assert set_policy.target_agent_id == AGENT_ID
    assert command_signature_valid(signer.public_key(), set_policy)
    assert set_policy.set_policy.threshold_pct == 90.0
    assert set_policy.set_policy.default_strategy == pb.SwitchNow.SWITCH_STRATEGY_BEST


def test_session_reasserts_policy_even_when_columns_null(app_env):
    # NULL columns still re-assert the commands (무조건 포함) but push no value:
    # threshold 0 + strategy UNSPECIFIED = keep local default.
    signer = signing.Signer.from_env_or_generate()
    tenant_id, _a, server_id = _seed_tenant_account_server("nullpol@ex.com")
    token = _issue_enroll(tenant_id, server_id)

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            stub = pb_grpc.AmxControlPlaneStub(channel)
            call = stub.Session()
            await call.write(
                pb.AmaMessage(register=pb.Register(agent_id=AGENT_ID, enroll_token=token))
            )
            await _read(call)  # session_setup
            set_mode = await _read(call)
            set_policy = await _read(call)
            await call.done_writing()
            return set_mode, set_policy

    set_mode, set_policy = asyncio.run(scenario())
    assert set_mode.WhichOneof("cmd") == "set_mode"
    # default switch_mode on a fresh server is auto.
    assert set_mode.set_mode.mode == pb.SWITCH_MODE_AUTO
    assert set_policy.WhichOneof("cmd") == "set_policy"
    assert set_policy.set_policy.threshold_pct == 0.0
    assert (
        set_policy.set_policy.default_strategy
        == pb.SwitchNow.SWITCH_STRATEGY_UNSPECIFIED
    )


# -- REST switching-control wiring queues the right commands ------------------
def test_rest_switch_now_queues_account_switch(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("sw@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, assignment_id, "active")
    with get_sessionmaker()() as db:
        commands.request_switch_now(db, tenant_id, assignment_id, strategy=None)
        row = db.scalars(
            select(AgentCommand).where(AgentCommand.assignment_id == assignment_id)
        ).one()
        assert row.command_type == "switch_now"
        assert "strategy" not in row.payload


def test_rest_switch_now_queues_strategy(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("swstrat@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, assignment_id, "active")
    with get_sessionmaker()() as db:
        commands.request_switch_now(db, tenant_id, assignment_id, strategy="best")
        row = db.scalars(
            select(AgentCommand).where(AgentCommand.assignment_id == assignment_id)
        ).one()
        assert row.payload["strategy"] == "best"


def test_rest_switch_mode_persists_and_queues_set_mode(app_env):
    tenant_id, _a, server_id = _seed_tenant_account_server("mode@ex.com")
    with get_sessionmaker()() as db:
        commands.request_switch_mode(db, tenant_id, server_id, mode="manual")
    with get_sessionmaker()() as db:
        assert db.get(Server, server_id).switch_mode == "manual"
        row = db.scalars(
            select(AgentCommand).where(AgentCommand.command_type == "set_mode")
        ).one()
        assert row.assignment_id is None
        assert row.payload["mode"] == "manual"


def test_rest_refresh_usage_queues_req_report(app_env):
    tenant_id, _a, server_id = _seed_tenant_account_server("refresh@ex.com")
    with get_sessionmaker()() as db:
        commands.request_refresh_usage(db, tenant_id, server_id)
        row = db.scalars(
            select(AgentCommand).where(AgentCommand.command_type == "req_report")
        ).one()
        assert row.assignment_id is None


def test_rest_recover_requires_quarantined_and_queues_activate(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("rec@ex.com")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _set_state(tenant_id, assignment_id, "quarantined")
    with get_sessionmaker()() as db:
        commands.request_recover(db, tenant_id, assignment_id)
        row = db.scalars(
            select(AgentCommand).where(AgentCommand.assignment_id == assignment_id)
        ).one()
        assert row.command_type == "activate"
        assert row.payload["clear_quarantine"] is True


def test_rest_set_policy_snapshot_builds_signed_command(app_env):
    # A queued set_policy (from a PATCH) is built into a signed, agent-bound
    # SetPolicy carrying the stored columns.
    signer = signing.Signer.from_env_or_generate()
    tenant_id, _a, server_id = _seed_tenant_account_server("setpol@ex.com")
    _set_policy_columns(
        server_id, switch_mode="auto", threshold_pct=80.0, default_strategy="next_available"
    )
    with get_sessionmaker()() as db:
        commands.request_set_policy(db, tenant_id, server_id)

    from app.grpc.server import ControlPlaneServicer

    servicer = ControlPlaneServicer(signer, session_factory=get_sessionmaker())
    with get_sessionmaker()() as db:
        row = commands.fetch_queued(db, server_id)[0]
        cmd = servicer._build_command(db, row, AGENT_ID, b"", "kid")
    assert cmd.WhichOneof("cmd") == "set_policy"
    assert cmd.target_agent_id == AGENT_ID
    assert command_signature_valid(signer.public_key(), cmd)
    assert cmd.set_policy.threshold_pct == 80.0
    assert (
        cmd.set_policy.default_strategy == pb.SwitchNow.SWITCH_STRATEGY_NEXT_AVAILABLE
    )
