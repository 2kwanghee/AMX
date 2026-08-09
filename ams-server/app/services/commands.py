"""Command outbox service — design note §2, §5.

The REST transition actions call the ``request_*`` helpers here. Each one
re-checks the tenant (via :func:`inventory.get_assignment`, the service-layer
half of §7's defence in depth), validates the current assignment state, writes
one ``agent_commands`` row, and advances the assignment. The gRPC session
process drains the row and :mod:`app.services.reconcile` closes the loop on the
agent's ack.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.errors import conflict
from app.models import Account, AgentCommand, Assignment, Server
from app.services import alerts, inventory

# D2 sent-未ack recovery (recovery-architecture §2). A command the poll loop
# pushed but the agent never acked stays ``sent`` forever, stranding its
# assignment in delivering/recalling. The sent-timeout sweeper re-queues it (same
# command_id, idempotent) up to MAX_SEND_ATTEMPTS, then fails it and reverts the
# assignment to a re-issuable resting state. Timeout is 2–3× the AMA heartbeat
# (default 90s = 3×30s) so an ordinary in-flight ack is never mistaken for a loss.
SENT_ACK_TIMEOUT_SECONDS = float(os.environ.get("AMX_SENT_ACK_TIMEOUT", "90"))
MAX_SEND_ATTEMPTS = int(os.environ.get("AMX_MAX_SEND_ATTEMPTS", "5"))

# D1 recall-failure recovery (recovery-architecture §1). A failed recall settles
# to ``recalling`` (pending_command_id NULL) and REST ``:recall`` re-arms it. This
# caps the manual re-arms so a permanently-failing recall cannot be re-issued
# forever; past the cap the action 409s and only opens a ``recall_failed`` alert.
MAX_RECALL_RETRIES = int(os.environ.get("AMX_MAX_RECALL_RETRIES", "3"))


def _now() -> datetime:
    return datetime.now(UTC)


def _new_command_id() -> str:
    return "cmd_" + crypto.new_token(16)


def enqueue(
    db: Session,
    *,
    assignment: Assignment,
    command_type: str,
    payload: dict,
) -> AgentCommand:
    """Insert a queued outbox row for ``assignment``'s server and tenant."""
    command = AgentCommand(
        tenant_id=assignment.tenant_id,
        server_id=assignment.server_id,
        assignment_id=assignment.id,
        command_id=_new_command_id(),
        command_type=command_type,
        payload=payload,
        status="queued",
    )
    db.add(command)
    return command


def enqueue_server(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    server_id: uuid.UUID,
    command_type: str,
    payload: dict,
) -> AgentCommand:
    """Insert a queued server-scoped outbox row (assignment_id NULL).

    For the non-state session-control commands (set_mode / req_report /
    set_policy) that name a server, not an assignment. The tenant tie is carried
    structurally by the composite ``(server_id, tenant_id)`` foreign key.
    """
    command = AgentCommand(
        tenant_id=tenant_id,
        server_id=server_id,
        assignment_id=None,
        command_id=_new_command_id(),
        command_type=command_type,
        payload=payload,
        status="queued",
    )
    db.add(command)
    return command


# -- REST-facing transitions --------------------------------------------------
def request_deliver(
    db: Session, tenant_id: uuid.UUID, assignment_id: uuid.UUID
) -> Assignment:
    assignment = inventory.get_assignment(db, tenant_id, assignment_id)
    if assignment.state != "pending":
        raise conflict(
            "assignment.not_deliverable",
            f"deliver requires state 'pending'; assignment is '{assignment.state}'.",
        )
    # `pinned` is AMS-internal and is translated into desired_status here rather
    # than shipped on the wire (proto DeliverAccount §5.2): a pinned assignment
    # is delivered inactive so it is installed but excluded from rotation.
    desired_status = "inactive" if assignment.pinned else "active"
    command = enqueue(
        db,
        assignment=assignment,
        command_type="deliver",
        payload={"desired_status": desired_status},
    )
    assignment.state = "delivering"
    assignment.pending_command_id = command.command_id
    assignment.last_error = None
    assignment.updated_at = _now()
    db.commit()
    db.refresh(assignment)
    return assignment


def request_recall(
    db: Session, tenant_id: uuid.UUID, assignment_id: uuid.UUID, *, force: bool = False
) -> Assignment:
    assignment = inventory.get_assignment(db, tenant_id, assignment_id)
    # D1 manual escape hatch (recovery-architecture §1): a settled recalling
    # (ack lost, pending_command_id NULL) is a stranded recall an operator must be
    # able to re-arm. An in-flight recalling (pending_command_id set) stays
    # rejected — its command is still live.
    settled_recalling = (
        assignment.state == "recalling" and assignment.pending_command_id is None
    )
    if (
        assignment.state not in ("delivering", "active", "inactive", "quarantined")
        and not settled_recalling
    ):
        raise conflict(
            "assignment.not_recallable",
            f"recall requires an installed assignment; state is '{assignment.state}'.",
        )
    # D1 retry cap: a settled recalling is a re-arm of a recall that already failed
    # once, so bound it. Past MAX_RECALL_RETRIES the recall is treated as durably
    # failed — 409 and a ``recall_failed`` alert, no new command. A first recall
    # from an installed state is attempt 0 and resets any stale counter.
    # ``force`` (global-admin, gated at the route) is the escape hatch: it bypasses
    # the cap and resets the counter so a permanently-stranded recall — otherwise
    # blocked from recall *and* from account/server deletion (state != detached) —
    # can be re-armed and driven to detached.
    if settled_recalling and force:
        assignment.recall_retry_count = 0
    elif settled_recalling:
        if assignment.recall_retry_count >= MAX_RECALL_RETRIES:
            alerts.open_alert(
                db,
                tenant_id=tenant_id,
                server_id=assignment.server_id,
                account_id=assignment.account_id,
                kind="recall_failed",
                severity="warning",
                detail={
                    "reason": "recall retries exhausted",
                    "retries": assignment.recall_retry_count,
                    "last_error": assignment.last_error,
                },
            )
            db.commit()
            raise conflict(
                "assignment.recall_retries_exhausted",
                f"recall has failed {assignment.recall_retry_count} times "
                f"(cap {MAX_RECALL_RETRIES}); a recall_failed alert is open for "
                "operator intervention.",
            )
        assignment.recall_retry_count += 1
    else:
        assignment.recall_retry_count = 0
    # O2: recall keeps the local credential record and only disables it; a full
    # wipe would set purge_local_copy=true. Default is preservation.
    command = enqueue(
        db,
        assignment=assignment,
        command_type="recall",
        payload={"purge_local_copy": False},
    )
    assignment.state = "recalling"
    assignment.pending_command_id = command.command_id
    assignment.last_error = None
    assignment.updated_at = _now()
    db.commit()
    db.refresh(assignment)
    return assignment


def request_activate(
    db: Session, tenant_id: uuid.UUID, assignment_id: uuid.UUID
) -> Assignment:
    assignment = inventory.get_assignment(db, tenant_id, assignment_id)
    if assignment.state != "inactive":
        raise conflict(
            "assignment.not_activatable",
            f"activate requires state 'inactive'; assignment is '{assignment.state}'.",
        )
    command = enqueue(
        db,
        assignment=assignment,
        command_type="activate",
        payload={"active": True},
    )
    assignment.pending_command_id = command.command_id
    assignment.last_error = None
    assignment.updated_at = _now()
    db.commit()
    db.refresh(assignment)
    return assignment


def request_deactivate(
    db: Session, tenant_id: uuid.UUID, assignment_id: uuid.UUID
) -> Assignment:
    assignment = inventory.get_assignment(db, tenant_id, assignment_id)
    if assignment.state != "active":
        raise conflict(
            "assignment.not_deactivatable",
            f"deactivate requires state 'active'; assignment is '{assignment.state}'.",
        )
    command = enqueue(
        db,
        assignment=assignment,
        command_type="deactivate",
        payload={"active": False},
    )
    assignment.pending_command_id = command.command_id
    assignment.last_error = None
    assignment.updated_at = _now()
    db.commit()
    db.refresh(assignment)
    return assignment


# -- P3 switching-control transitions -----------------------------------------
def request_recover(
    db: Session, tenant_id: uuid.UUID, assignment_id: uuid.UUID
) -> Assignment:
    """§5.2 recover: quarantined -> active via SetAccountActive(activate).

    Carries ``clear_quarantine`` so the agent lifts the tsamx quarantine as it
    re-activates; convergence lands the assignment back on ``active``.
    """
    assignment = inventory.get_assignment(db, tenant_id, assignment_id)
    if assignment.state != "quarantined":
        raise conflict(
            "assignment.not_recoverable",
            f"recover requires state 'quarantined'; assignment is '{assignment.state}'.",
        )
    command = enqueue(
        db,
        assignment=assignment,
        command_type="activate",
        payload={"active": True, "clear_quarantine": True},
    )
    assignment.pending_command_id = command.command_id
    assignment.last_error = None
    assignment.updated_at = _now()
    db.commit()
    db.refresh(assignment)
    return assignment


def request_switch_now(
    db: Session,
    tenant_id: uuid.UUID,
    assignment_id: uuid.UUID,
    *,
    strategy: str | None = None,
) -> Assignment:
    """Manual switch (§6.3). Non-state command: it moves no assignment state,
    only ``last_switched_at`` on the origin account. With ``strategy`` the agent
    lets tsamx rank candidates; without it, it switches to this assignment's
    account."""
    assignment = inventory.get_assignment(db, tenant_id, assignment_id)
    if assignment.state not in ("active", "inactive"):
        raise conflict(
            "assignment.not_switchable",
            f"switch-now requires an installed account (active/inactive); "
            f"state is '{assignment.state}'.",
        )
    payload: dict = {}
    if strategy is not None:
        payload["strategy"] = strategy
    enqueue(db, assignment=assignment, command_type="switch_now", payload=payload)
    account = db.scalar(
        select(Account).where(
            Account.id == assignment.account_id, Account.tenant_id == tenant_id
        )
    )
    if account is not None:
        account.last_switched_at = _now()
    assignment.updated_at = _now()
    db.commit()
    db.refresh(assignment)
    return assignment


def request_switch_mode(
    db: Session, tenant_id: uuid.UUID, server_id: uuid.UUID, *, mode: str
) -> Server:
    """Set a server's switch mode: persist ``servers.switch_mode`` and, so a
    connected agent applies it immediately, queue a SetSwitchMode. A restart
    recovers it from the column via session re-assertion."""
    server = inventory.get_server(db, tenant_id, server_id)
    server.switch_mode = mode
    server.updated_at = _now()
    enqueue_server(
        db,
        tenant_id=tenant_id,
        server_id=server_id,
        command_type="set_mode",
        payload={"mode": mode},
    )
    db.commit()
    db.refresh(server)
    return server


def request_refresh_usage(
    db: Session, tenant_id: uuid.UUID, server_id: uuid.UUID
) -> None:
    """Ask the agent for an immediate usage report (§6.3 req_report)."""
    inventory.get_server(db, tenant_id, server_id)
    enqueue_server(
        db,
        tenant_id=tenant_id,
        server_id=server_id,
        command_type="req_report",
        payload={"reason": "console refresh"},
    )
    db.commit()


def request_set_policy(
    db: Session, tenant_id: uuid.UUID, server_id: uuid.UUID
) -> None:
    """Re-deliver the server's stored O4-C policy to a connected agent (§O4-C).

    Snapshots the current columns into the command payload; NULL columns push no
    value (the agent keeps its local default)."""
    server = inventory.get_server(db, tenant_id, server_id)
    enqueue_server(
        db,
        tenant_id=tenant_id,
        server_id=server_id,
        command_type="set_policy",
        payload={
            "threshold_pct": server.threshold_pct,
            "default_strategy": server.default_strategy,
            "cooldown_seconds": server.cooldown_seconds,
            "hysteresis_pct": server.hysteresis_pct,
        },
    )
    db.commit()


# -- gRPC-side outbox helpers -------------------------------------------------
def fetch_queued(db: Session, server_id: uuid.UUID) -> list[AgentCommand]:
    """Queued commands for one online server, oldest first.

    F3 multi-instance: ``FOR UPDATE SKIP LOCKED`` row-locks each returned row so
    two AMS instances polling the same server (a split-brain reconnect, or plain
    horizontal scale) never hand the same queued command to both agent sessions.
    The lock is only meaningful while it is held: the caller (``_build_queued_commands``)
    claims each row with :func:`claim_sent` and commits in *this same transaction*,
    so a concurrent poller either skips the locked row or, once we commit, sees it
    as ``sent`` and no longer ``queued``. A row this poller skips (build returned
    None, or it was locked) is left ``queued`` and retried on the next tick.
    """
    return list(
        db.scalars(
            select(AgentCommand)
            .where(AgentCommand.server_id == server_id, AgentCommand.status == "queued")
            .order_by(AgentCommand.created_at, AgentCommand.id)
            .with_for_update(skip_locked=True)
        ).all()
    )


def claim_sent(command: AgentCommand) -> None:
    """Mark a fetched, still-locked queued command ``sent`` — no commit.

    Called on rows returned by :func:`fetch_queued` (which holds their ``FOR
    UPDATE SKIP LOCKED`` locks) so the claim lands in the same transaction as the
    fetch; the caller commits once, atomically releasing the locks with the rows
    now ``sent``. A push that then fails on the wire is not lost: the command_id
    is unchanged and the D2 sent-ack sweeper re-queues it (idempotent)."""
    if command.status == "queued":
        command.status = "sent"
        command.sent_at = _now()
        command.updated_at = _now()


# -- D2 sent-未ack recovery ----------------------------------------------------
def sweep_sent_timeouts(
    db: Session,
    *,
    timeout_seconds: float = SENT_ACK_TIMEOUT_SECONDS,
    max_attempts: int = MAX_SEND_ATTEMPTS,
) -> tuple[list[str], list[str]]:
    """Re-queue or fail commands stuck in ``sent`` past the ack timeout.

    A command the poll loop pushed (``claim_sent``) but the agent never acked ages
    silently: ``fetch_queued`` only re-sends ``queued`` rows, so a ``sent`` row is
    never retried and its assignment stays delivering/recalling forever
    (recovery-architecture §2). This sweep, a sibling of the offline sweeper (no
    new timer), closes that gap:

    * ``send_attempts < max_attempts`` -> ``sent`` back to ``queued`` (``sent_at``
      cleared, ``send_attempts`` incremented). The command_id is unchanged, so the
      re-send is idempotent: the agent dedupes on it and re-acks CONVERGED.
    * otherwise -> ``failed``, and the assignment is reverted to a resting state so
      it can be re-issued (deliver -> pending; recall -> settled recalling for the
      D1 path; activate/deactivate -> clear the pending marker; server-scoped and
      switch_now -> marked only, healed by the next session's re-assertion).

    No contention with reconcile-on-report: reconcile acts only on resting-state
    assignments (active/inactive/quarantined/detached) while every command swept
    here belongs to an in-flight assignment (delivering/recalling) or is
    server-scoped, so the two never move the same row. The caller need not commit —
    this commits its own transaction. Returns ``(requeued_ids, failed_ids)``.
    """
    cutoff = _now() - timedelta(seconds=timeout_seconds)
    stuck = db.scalars(
        select(AgentCommand).where(
            AgentCommand.status == "sent",
            AgentCommand.sent_at.is_not(None),
            AgentCommand.sent_at < cutoff,
        )
    ).all()
    requeued: list[str] = []
    failed: list[str] = []
    for command in stuck:
        if command.send_attempts < max_attempts:
            command.status = "queued"
            command.sent_at = None
            command.send_attempts += 1
            command.updated_at = _now()
            requeued.append(command.command_id)
        else:
            command.status = "failed"
            command.detail = "sent_ack_timeout"
            command.updated_at = _now()
            assignment = _revert_assignment_on_send_failure(db, command)
            _open_send_failure_alert(db, command, assignment)
            failed.append(command.command_id)
    db.commit()
    return requeued, failed


def _open_send_failure_alert(
    db: Session, command: AgentCommand, assignment: Assignment | None
) -> None:
    """Open the operator alert for a command that exhausted its send retries.

    Only account-scoped commands whose revert actually applied
    (``assignment`` non-None — see :func:`_revert_assignment_on_send_failure`)
    alert; the failure leaves the intent unrecoverable without an operator
    (deliver -> pending, activate/deactivate -> marker dropped, recall -> settled
    recalling), so it must surface:

    * ``recall`` re-uses the D1 ``recall_failed`` kind, so a re-armed recall that
      fails on the wire and one that fails on a DIVERGED ack dedupe onto the SAME
      open alert (``{server_id}:recall_failed:{account_id}``).
    * every other account-scoped type opens ``command_send_failed`` per account
      (``{server_id}:command_send_failed:{account_id}``).

    **No alert for server-scoped commands** (set_mode/set_policy/req_report,
    assignment_id NULL): their intent is not lost — the next session's policy
    re-assertion re-applies it — so a silent final failure is by design. Alerting
    them would accumulate a manual alert on a self-healing condition, share one
    dedupe key across the three types, and double-open against ``server_offline``.
    A **superseded** command (a newer command already owns the assignment) also
    does not alert: the successor reports its own result.

    ``detail`` carries only command_type / account_id / last_error — never the
    payload, which may hold credential material.
    """
    if assignment is None:
        return
    account_id = assignment.account_id
    kind = "recall_failed" if command.command_type == "recall" else "command_send_failed"
    alerts.open_alert(
        db,
        tenant_id=command.tenant_id,
        server_id=command.server_id,
        account_id=account_id,
        kind=kind,
        severity="warning",
        detail={
            "reason": "send retries exhausted",
            "command_type": command.command_type,
            "account_id": str(account_id) if account_id is not None else None,
            "last_error": command.detail,
        },
    )


def _revert_assignment_on_send_failure(
    db: Session, command: AgentCommand
) -> Assignment | None:
    """Revert the assignment of a permanently-failed ``sent`` command.

    Mirrors the DIVERGED/REJECTED ack handling in ``reconcile.apply_ack``: only
    the assignment still pointing at this command (``pending_command_id`` match) is
    touched, so a newer command is never clobbered. deliver reverts to ``pending``;
    recall is left ``recalling`` with the pending marker cleared — the settled,
    non-in-flight state the D1 reconcile/REST recall path recovers; activate/
    deactivate keep their resting state and only drop the marker. Server-scoped
    commands (assignment_id NULL) and switch_now (never sets a pending marker)
    revert nothing — marking the command ``failed`` is enough, and a session
    re-assertion re-applies server-scoped policy on the next connect.

    Returns the reverted assignment, or None when nothing was reverted (no
    assignment, or a newer command already owns it). The caller opens the
    send-failure alert only for a non-None return, so server-scoped and
    superseded commands never alert.
    """
    if command.assignment_id is None:
        return None
    assignment = db.scalar(
        select(Assignment).where(
            Assignment.id == command.assignment_id,
            Assignment.tenant_id == command.tenant_id,
        )
    )
    if assignment is None:
        return None
    if assignment.pending_command_id != command.command_id:
        # A newer command already owns the assignment; do not clobber it, and do
        # not alert — the successor reports its own result (return None so the
        # caller opens no alert for this superseded command).
        return None
    assignment.last_error = "sent_ack_timeout"
    assignment.pending_command_id = None
    if command.command_type == "deliver":
        assignment.state = "pending"
    # recall: leave state 'recalling' (pending marker now NULL) -> settled recalling
    #   the D1 path re-recalls or settles to detached.
    # activate/deactivate/recover: leave the resting state (inactive/active/
    #   quarantined); dropping the marker re-opens it to a fresh command.
    assignment.updated_at = _now()
    return assignment
