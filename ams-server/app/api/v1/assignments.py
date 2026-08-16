"""Assignment CRUD and the §5.2 transition stubs.

`deliverImmediately` on create is accepted by the contract but cannot be
honoured in P1 — delivery needs the channel. Rather than silently ignore it (a
caller would believe the account was pushed) the request is rejected.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Response, status

from app import schemas
from app.api.deps import AdminPrincipal, DbSession, PageSize, PageToken, TenantScope, next_page_token, offset_from_token
from app.core.errors import ApiError, bad_request
from app.services import commands, inventory

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["assignments"], dependencies=[TenantScope])


@router.get("/assignments", response_model=schemas.AssignmentPage)
def list_assignments(
    tenant_id: uuid.UUID,
    db: DbSession,
    principal: AdminPrincipal,
    serverId: uuid.UUID | None = Query(default=None),  # noqa: N803
    accountId: uuid.UUID | None = Query(default=None),  # noqa: N803
    state: schemas.AssignmentState | None = Query(default=None),
    pageSize: PageSize = 50,  # noqa: N803
    pageToken: PageToken = None,  # noqa: N803
):
    offset = offset_from_token(pageToken)
    items, total = inventory.list_assignments(
        db,
        tenant_id,
        server_id=serverId,
        account_id=accountId,
        state=state,
        limit=pageSize,
        offset=offset,
    )
    return schemas.AssignmentPage(
        items=[schemas.Assignment.model_validate(a) for a in items],
        page_info=schemas.PageInfo(
            next_page_token=next_page_token(offset, pageSize, total), total_size=total
        ),
    )


@router.post("/assignments", response_model=schemas.Assignment, status_code=201)
def create_assignment(tenant_id: uuid.UUID, body: schemas.AssignmentCreate, db: DbSession, principal: AdminPrincipal):
    if body.deliver_immediately:
        raise bad_request(
            "assignment.deliver_immediately_unsupported",
            "deliverImmediately requires the AMS↔AMA channel (P2). Create the "
            "assignment without it; it stays in `pending`.",
        )
    assignment = inventory.create_assignment(
        db, tenant_id, account_id=body.account_id, server_id=body.server_id, pinned=body.pinned
    )
    return schemas.Assignment.model_validate(assignment)


@router.get("/assignments/{assignment_id}", response_model=schemas.Assignment)
def get_assignment(tenant_id: uuid.UUID, assignment_id: uuid.UUID, db: DbSession, principal: AdminPrincipal):
    return schemas.Assignment.model_validate(
        inventory.get_assignment(db, tenant_id, assignment_id)
    )


@router.patch("/assignments/{assignment_id}", response_model=schemas.Assignment)
def update_assignment(
    tenant_id: uuid.UUID,
    assignment_id: uuid.UUID,
    body: schemas.AssignmentUpdate,
    db: DbSession,
    principal: AdminPrincipal,
):
    # The contract notes that `pinned` also issues a SetAccountActive command;
    # in P1 only the AMS-side flag is recorded, and it converges on nothing.
    return schemas.Assignment.model_validate(
        inventory.update_assignment(db, tenant_id, assignment_id, pinned=body.pinned)
    )


@router.delete("/assignments/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_assignment(
    tenant_id: uuid.UUID, assignment_id: uuid.UUID, db: DbSession, principal: AdminPrincipal
) -> Response:
    # Only a detached history row is deletable; a live assignment is 409
    # (assignment.not_deletable). The deletion itself is preserved by the audit
    # trail, so the row is removed outright rather than tombstoned.
    inventory.delete_assignment(db, tenant_id, assignment_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# The transition actions below enqueue a signed command on the outbox and move
# the assignment; the gRPC session process delivers it and reconciles the ack
# (design note §2, §5). Each resolves the assignment first, so a cross-tenant id
# gets 404 before any state change (P1 defence-in-depth pattern).
@router.post(
    "/assignments/{assignment_id}:deliver",
    summary="deliver",
    response_model=schemas.Assignment,
)
def deliver_assignment(tenant_id: uuid.UUID, assignment_id: uuid.UUID, db: DbSession, principal: AdminPrincipal):
    return schemas.Assignment.model_validate(
        commands.request_deliver(db, tenant_id, assignment_id)
    )


@router.post(
    "/assignments/{assignment_id}:recall",
    summary="recall",
    response_model=schemas.Assignment,
)
def recall_assignment(
    tenant_id: uuid.UUID,
    assignment_id: uuid.UUID,
    db: DbSession,
    principal: AdminPrincipal,
    body: schemas.RecallRequest | None = None,
):
    # D1 escape hatch: `force` bypasses the retry cap on a stranded recall, so it
    # is a global-admin-only capability (a tenant-admin gets a real 403 here — the
    # cross-tenant caller was already hidden as 404 by TenantScope).
    force = body.force if body is not None else False
    if force and principal.role != "global-admin":
        raise ApiError(
            403, "Forbidden", "auth.forbidden", "force recall requires a global-admin."
        )
    return schemas.Assignment.model_validate(
        commands.request_recall(db, tenant_id, assignment_id, force=force)
    )


@router.post(
    "/assignments/{assignment_id}:activate",
    summary="activate",
    response_model=schemas.Assignment,
)
def activate_assignment(tenant_id: uuid.UUID, assignment_id: uuid.UUID, db: DbSession, principal: AdminPrincipal):
    return schemas.Assignment.model_validate(
        commands.request_activate(db, tenant_id, assignment_id)
    )


@router.post(
    "/assignments/{assignment_id}:deactivate",
    summary="deactivate",
    response_model=schemas.Assignment,
)
def deactivate_assignment(tenant_id: uuid.UUID, assignment_id: uuid.UUID, db: DbSession, principal: AdminPrincipal):
    return schemas.Assignment.model_validate(
        commands.request_deactivate(db, tenant_id, assignment_id)
    )


@router.post(
    "/assignments/{assignment_id}:recover",
    summary="recover",
    response_model=schemas.Assignment,
)
def recover_assignment(tenant_id: uuid.UUID, assignment_id: uuid.UUID, db: DbSession, principal: AdminPrincipal):
    # §5.2 quarantined -> active via SetAccountActive(activate, clear_quarantine).
    return schemas.Assignment.model_validate(
        commands.request_recover(db, tenant_id, assignment_id)
    )


@router.post(
    "/assignments/{assignment_id}:switch-now",
    summary="switch-now",
    response_model=schemas.Assignment,
)
def switch_now(
    tenant_id: uuid.UUID,
    assignment_id: uuid.UUID,
    db: DbSession,
    principal: AdminPrincipal,
    body: schemas.SwitchNowRequest | None = None,
):
    strategy = body.strategy if body is not None else None
    return schemas.Assignment.model_validate(
        commands.request_switch_now(db, tenant_id, assignment_id, strategy=strategy)
    )
