"""Desired-vs-actual reconciliation (design note §3, §5; proto CommandAck).

The agent reports the local reality it converged to; that report is the input
here. This module is transport-agnostic — the gRPC session layer translates a
``CommandAck`` into the primitives below, so nothing here imports the protobuf
types.

Convergence -> assignment state (design note §5):
    deliver  CONVERGED -> active | inactive (per the command's desired_status)
    recall   CONVERGED -> detached (+ account returns to 'available')
    activate CONVERGED -> active
    deactivate CONVERGED -> inactive
    PENDING            -> no terminal move; the agent is still working
    DIVERGED/REJECTED  -> record last_error; a deliver reverts to 'pending' so it
                          can be re-issued (§5.2 ack.fail -> pending)
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models import Account, AgentCommand, Assignment, UsageSnapshot
from app.services import alerts, commands

_logger = logging.getLogger("ams.reconcile")

CONVERGED = "converged"
PENDING = "pending"
DIVERGED = "diverged"
REJECTED = "rejected"

# Actual allocation statuses as reported by the agent (translated from the proto
# AllocationStatus enum at the gRPC boundary, keeping this module protobuf-free).
ACTUAL_ACTIVE = "active"
ACTUAL_INACTIVE = "inactive"
ACTUAL_QUARANTINED = "quarantined"
ACTUAL_ABSENT = "absent"

# assignment.state -> the allocation status the agent should be reporting for it.
# ``delivering`` is deliberately absent: it is in-flight and compared by the
# command-ack path, not the report path (avoids racing a live command). ``recalling``
# is included for its SETTLED variant only (D1, recovery-architecture §1): a recall
# whose ack was lost sits ``recalling`` with ``pending_command_id`` NULL — no longer
# in flight — and its desired end-state is ABSENT. The report query below admits
# only that settled variant; an in-flight recalling (pending_command_id set) is
# still excluded.
_EXPECTED_ACTUAL = {
    "active": ACTUAL_ACTIVE,
    "inactive": ACTUAL_INACTIVE,
    "quarantined": ACTUAL_QUARANTINED,
    "detached": ACTUAL_ABSENT,
    "recalling": ACTUAL_ABSENT,
}

# R3 loop guard: cap how many times reconcile may re-issue the SAME narrow
# correction for the SAME assignment. Beyond it, drift is still alarmed but no
# further command is queued — a feedback loop cannot form.
CORRECTION_CAP = int(os.environ.get("AMX_RECONCILE_CORRECTION_CAP", "3"))

# Narrow, safe, idempotent corrections only (design note decision 7):
CORRECTION_REDELIVER = "redeliver"  # assigned (active/inactive) but locally absent
CORRECTION_RECALL = "recall"  # detached but still present locally


def _now() -> datetime:
    return datetime.now(UTC)


def apply_ack(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    command_id: str,
    convergence: str,
    detail: str = "",
    error_code: str = "",
) -> None:
    """Apply one CommandAck to its outbox row and assignment.

    Scoped to ``tenant_id`` (the session's bound tenant): an ack that names a
    command outside it is ignored, so a compromised session cannot move another
    tenant's rows.
    """
    command = db.scalar(
        select(AgentCommand).where(
            AgentCommand.command_id == command_id,
            AgentCommand.tenant_id == tenant_id,
        )
    )
    if command is None:
        return
    assignment = None
    if command.assignment_id is not None:
        assignment = db.scalar(
            select(Assignment).where(
                Assignment.id == command.assignment_id,
                Assignment.tenant_id == tenant_id,
            )
        )

    if convergence == PENDING:
        # Accepted, still working; leave the intermediate state in place.
        command.updated_at = _now()
        db.commit()
        return

    if convergence == CONVERGED:
        command.status = "acked"
        command.acked_at = _now()
        command.updated_at = _now()
        if assignment is not None:
            _apply_converged(db, command, assignment)
        elif command.command_type == "self_update":
            # The agent rebuilt and restarted; close any alert from an earlier
            # failed attempt (the account-scoped auto-resolves live in
            # _apply_converged, which server-scoped commands never reach).
            alerts.resolve(db, server_id=command.server_id, kind="self_update_failed")
        db.commit()
        return

    # DIVERGED or REJECTED.
    command.status = "failed"
    command.detail = (error_code or detail or convergence)[:2000]
    command.updated_at = _now()
    if command.command_type == "self_update":
        # Server-scoped, so there is no assignment to revert and nothing else
        # would surface this: the agent stayed on its old binary and simply
        # nacked (preflight/pull/build/commit_mismatch — see the AMA handler).
        # Without an alert a self update that never lands is invisible, and the
        # operator would go on believing the fleet is on the new commit.
        alerts.open_alert(
            db,
            tenant_id=tenant_id,
            server_id=command.server_id,
            kind="self_update_failed",
            severity="warning",
            detail={
                "reason": convergence,
                "error_code": error_code or "",
                "last_error": command.detail,
                "command_id": command_id,
                "expected_commit": (command.payload or {}).get("expected_commit") or "",
            },
        )
    if assignment is not None:
        assignment.last_error = (error_code or detail or convergence)[:2000]
        assignment.pending_command_id = None
        if command.command_type == "deliver":
            # §5.2 ack.fail -> pending: re-eligible for a fresh deliver.
            assignment.state = "pending"
        elif command.command_type == "recall" and assignment.state == "recalling":
            # D1 (recovery-architecture §1): the recall failed. The assignment is
            # left settled ``recalling`` (pending marker now NULL) for the manual
            # REST re-arm and reconcile auto-recall, and the operator is alarmed so
            # a stranded recall is visible rather than silently stuck. The state
            # guard drops a stale/duplicate recall ack that arrives after the
            # assignment has already moved on (detached, or re-delivered) — it must
            # not resurrect a bogus recall_failed alert for a settled account.
            alerts.open_alert(
                db,
                tenant_id=tenant_id,
                server_id=command.server_id,
                account_id=assignment.account_id,
                kind="recall_failed",
                severity="warning",
                detail={
                    "reason": convergence,
                    "last_error": assignment.last_error,
                    "command_id": command_id,
                },
            )
        assignment.updated_at = _now()
    db.commit()


def _apply_converged(db: Session, command: AgentCommand, assignment: Assignment) -> None:
    ctype = command.command_type
    if ctype == "deliver":
        desired = command.payload.get("desired_status", "active")
        assignment.state = "inactive" if desired == "inactive" else "active"
        assignment.delivered_at = _now()
    elif ctype == "recall":
        assignment.state = "detached"
        # D1: the recall finally succeeded — clear the retry counter and resolve
        # any standing recall_failed alert for this account.
        assignment.recall_retry_count = 0
        alerts.resolve(
            db,
            server_id=assignment.server_id,
            kind="recall_failed",
            account_id=assignment.account_id,
        )
        account = db.scalar(
            select(Account).where(
                Account.id == assignment.account_id,
                Account.tenant_id == assignment.tenant_id,
            )
        )
        if account is not None and account.status == "assigned":
            account.status = "available"
    elif ctype == "activate":
        assignment.state = "active"
    elif ctype == "deactivate":
        assignment.state = "inactive"
    # D2 auto-resolve: a later command against this account that finally acks
    # CONVERGED means the send path recovered, so clear any standing
    # command_send_failed alert (mirrors the recall_failed resolve above). The
    # recall branch already resolved its own recall_failed alert.
    alerts.resolve(
        db,
        server_id=assignment.server_id,
        kind="command_send_failed",
        account_id=assignment.account_id,
    )
    assignment.acked_at = _now()
    assignment.pending_command_id = None
    assignment.last_error = None
    assignment.updated_at = _now()


def _settle_recall_detached(db: Session, assignment: Assignment) -> None:
    """D1: settle a stranded recalling whose account is already absent locally.

    The recall physically succeeded (the account is gone) but its ack never
    landed, so the row sat ``recalling`` with ``pending_command_id`` NULL. Reaching
    convergence from the report mirrors the recall-CONVERGED branch of
    :func:`_apply_converged` exactly — detach and return the account to the pool —
    without issuing any command (recovery-architecture §1: state-only settle)."""
    assignment.state = "detached"
    assignment.pending_command_id = None
    assignment.last_error = None
    assignment.recall_retry_count = 0
    assignment.acked_at = _now()
    assignment.updated_at = _now()
    alerts.resolve(
        db,
        server_id=assignment.server_id,
        kind="recall_failed",
        account_id=assignment.account_id,
    )
    account = db.scalar(
        select(Account).where(
            Account.id == assignment.account_id,
            Account.tenant_id == assignment.tenant_id,
        )
    )
    if account is not None and account.status == "assigned":
        account.status = "available"


def suppress_applied(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    server_id: uuid.UUID,
    applied_command_ids: list[str],
    reported_account_ids: set[str],
) -> None:
    """Cold-start rule 3 (design note §3): suppress redundant redelivery.

    A queued deliver command is retired on reconnect **only** when both hold:
    its command_id is in the agent's ``applied_command_ids`` **and** the agent
    actually reports that account present. After a restart the agent has no KEK
    yet and reports no accounts (rule 2), so this suppresses nothing and the
    command is redelivered — which is the whole point of the rule.

    Never deletes anything and never acts on an empty report; it only advances a
    command AMS already asked for and the agent confirms it already applied.
    """
    if not applied_command_ids or not reported_account_ids:
        return
    applied = set(applied_command_ids)
    queued = db.scalars(
        select(AgentCommand).where(
            AgentCommand.server_id == server_id,
            AgentCommand.tenant_id == tenant_id,
            AgentCommand.status.in_(("queued", "sent")),
            AgentCommand.command_type == "deliver",
        )
    ).all()
    for command in queued:
        if command.command_id not in applied:
            continue
        assignment = db.scalar(
            select(Assignment).where(
                Assignment.id == command.assignment_id,
                Assignment.tenant_id == tenant_id,
            )
        )
        if assignment is None:
            continue
        if str(assignment.account_id) not in reported_account_ids:
            continue
        command.status = "acked"
        command.acked_at = _now()
        command.updated_at = _now()
        _apply_converged(db, command, assignment)
    db.commit()


# -- reconcile-on-report (design note §5, decision 3 & 7) ---------------------
def reconcile_from_report(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    server_id: uuid.UUID,
    reported: dict[str, str],
    snapshot: UsageSnapshot | None = None,
    correction_cap: int = CORRECTION_CAP,
) -> list[dict]:
    """Compare desired (assignment table) against actual (this report).

    ``reported`` maps ``ams_account_id`` -> the actual allocation status the
    agent reported (translated from the proto enum at the gRPC boundary; an
    account the agent does not mention is treated as ``ACTUAL_ABSENT``). This is
    where rule 2 (the report is the actual authority) and rule 3 (redelivery is
    only suppressed while the account is actually present) are actually fired:

    * ``is_current`` is never compared — a manual/auto switch moves it without
      being drift, so only ``allocation_status`` is checked.
    * Drift is always alarmed and marked on the snapshot.
    * Auto-correction is narrow and idempotent (decision 7): an assigned account
      absent locally is re-delivered; a detached account still present locally
      is re-recalled. Every other mismatch is alarm-only. A per-(assignment,
      correction) cap stops any feedback loop.

    The caller commits; this function only stages rows (so it composes with the
    snapshot insert in one transaction). Scoped to ``(tenant_id, server_id)``.
    """
    assignments = list(
        db.scalars(
            select(Assignment).where(
                Assignment.tenant_id == tenant_id,
                Assignment.server_id == server_id,
                or_(
                    Assignment.state.in_(
                        ("active", "inactive", "quarantined", "detached")
                    ),
                    # D1: settled recalling only (ack lost, no live command). An
                    # in-flight recall (pending_command_id set) is left to the
                    # command-ack path, so the two never race the same row.
                    and_(
                        Assignment.state == "recalling",
                        Assignment.pending_command_id.is_(None),
                    ),
                ),
            )
        ).all()
    )
    # A detached row is history; if the same account has a live (non-detached)
    # assignment on this server it was legitimately re-installed, so the detached
    # row's "present locally" is not drift and must not trigger a recall.
    live_account_ids = {
        str(a.account_id) for a in assignments if a.state != "detached"
    }

    drift_entries: list[dict] = []
    for assignment in assignments:
        expected = _EXPECTED_ACTUAL[assignment.state]
        actual = reported.get(str(assignment.account_id), ACTUAL_ABSENT)

        # D1 settled recalling (recovery-architecture §1): a recall whose ack was
        # lost. If the account is already gone locally the recall in fact succeeded
        # and only the ack dropped -> settle to detached now (mirror of the recall
        # CONVERGED branch), silently, no command and no drift. If it is still
        # present, fall through to the CORRECTION_RECALL path below (re-recall,
        # in-flight-skipped and capped).
        if assignment.state == "recalling" and actual == ACTUAL_ABSENT:
            _settle_recall_detached(db, assignment)
            continue

        if actual == expected:
            continue
        if assignment.state == "detached" and str(assignment.account_id) in live_account_ids:
            continue  # covered by a live assignment; not drift

        correction = None
        if assignment.state in ("active", "inactive") and actual == ACTUAL_ABSENT:
            correction = CORRECTION_REDELIVER
        elif assignment.state in ("detached", "recalling") and actual != ACTUAL_ABSENT:
            correction = CORRECTION_RECALL

        corrected = False
        capped = False
        if correction is not None:
            corrected, capped = _apply_correction(db, assignment, correction, correction_cap)

        entry = {
            "assignment_id": str(assignment.id),
            "account_id": str(assignment.account_id),
            "expected": expected,
            "actual": actual,
            "correction": correction,
            "corrected": corrected,
        }
        drift_entries.append(entry)
        _logger.warning(
            "reconcile drift: assignment=%s account=%s expected=%s actual=%s "
            "correction=%s corrected=%s capped=%s",
            assignment.id,
            assignment.account_id,
            expected,
            actual,
            correction,
            corrected,
            capped,
        )

    if drift_entries and snapshot is not None:
        snapshot.drift = drift_entries
    return drift_entries


def _apply_correction(
    db: Session, assignment: Assignment, correction: str, cap: int
) -> tuple[bool, bool]:
    """Queue one narrow correction, honouring the loop cap. Returns
    ``(corrected, capped)`` — ``capped`` is True when the cap blocked it."""
    marker = AgentCommand.payload["reconcile_correction"].astext
    # Skip if one is already in flight — don't stack a fresh correction on every
    # 5-minute report before the first has had a chance to converge.
    inflight = db.scalar(
        select(func.count())
        .select_from(AgentCommand)
        .where(
            AgentCommand.assignment_id == assignment.id,
            marker == correction,
            AgentCommand.status.in_(("queued", "sent")),
        )
    )
    if inflight:
        return False, False
    # Loop guard: cap total historical re-issues of this correction.
    prior = (
        db.scalar(
            select(func.count())
            .select_from(AgentCommand)
            .where(
                AgentCommand.assignment_id == assignment.id,
                marker == correction,
            )
        )
        or 0
    )
    if prior >= cap:
        return False, True

    if correction == CORRECTION_REDELIVER:
        command_type = "deliver"
        payload = {
            "reconcile_correction": correction,
            "desired_status": assignment.state,  # "active" or "inactive"
        }
    else:  # CORRECTION_RECALL
        command_type = "recall"
        payload = {"reconcile_correction": correction, "purge_local_copy": False}

    commands.enqueue(
        db, assignment=assignment, command_type=command_type, payload=payload
    )
    return True, False
