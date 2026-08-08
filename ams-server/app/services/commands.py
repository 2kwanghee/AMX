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
from app.models import Account, AgentCommand, Assignment, Server
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
        },
    )
    db.commit()


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
