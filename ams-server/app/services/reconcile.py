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

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, AgentCommand, Assignment

CONVERGED = "converged"
PENDING = "pending"
DIVERGED = "diverged"
REJECTED = "rejected"


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
        db.commit()
        return

    # DIVERGED or REJECTED.
    command.status = "failed"
    command.detail = (error_code or detail or convergence)[:2000]
    command.updated_at = _now()
    if assignment is not None:
        assignment.last_error = (error_code or detail or convergence)[:2000]
        assignment.pending_command_id = None
        if command.command_type == "deliver":
            # §5.2 ack.fail -> pending: re-eligible for a fresh deliver.
            assignment.state = "pending"
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
    assignment.acked_at = _now()
    assignment.pending_command_id = None
    assignment.last_error = None
    assignment.updated_at = _now()


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
