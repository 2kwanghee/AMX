"""Command outbox service — design note §2, §5.

The REST transition actions call the ``request_*`` helpers here. Each one
re-checks the tenant (via :func:`inventory.get_assignment`, the service-layer
half of §7's defence in depth), validates the current assignment state, writes
one ``agent_commands`` row, and advances the assignment. The gRPC session
process drains the row and :mod:`app.services.reconcile` closes the loop on the
agent's ack.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.errors import conflict
from app.models import AgentCommand, Assignment
from app.services import inventory


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
    db: Session, tenant_id: uuid.UUID, assignment_id: uuid.UUID
) -> Assignment:
    assignment = inventory.get_assignment(db, tenant_id, assignment_id)
    if assignment.state not in ("delivering", "active", "inactive", "quarantined"):
        raise conflict(
            "assignment.not_recallable",
            f"recall requires an installed assignment; state is '{assignment.state}'.",
        )
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


# -- gRPC-side outbox helpers -------------------------------------------------
def fetch_queued(db: Session, server_id: uuid.UUID) -> list[AgentCommand]:
    """Queued commands for one online server, oldest first."""
    return list(
        db.scalars(
            select(AgentCommand)
            .where(AgentCommand.server_id == server_id, AgentCommand.status == "queued")
            .order_by(AgentCommand.created_at, AgentCommand.id)
        ).all()
    )


def mark_sent(db: Session, command_id: str) -> None:
    command = db.scalar(select(AgentCommand).where(AgentCommand.command_id == command_id))
    if command is not None and command.status == "queued":
        command.status = "sent"
        command.sent_at = _now()
        command.updated_at = _now()
        db.commit()
