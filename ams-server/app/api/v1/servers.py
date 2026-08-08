"""AMA server CRUD, enrollment tokens, and the P2 command stubs — §5.3."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Response, status

from app import schemas
from app.api.deps import AdminAuth, DbSession, PageSize, PageToken, next_page_token, offset_from_token
from app.core.errors import not_found
from app.services import commands, inventory

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["servers"], dependencies=[AdminAuth])


def _to_wire(db, server) -> schemas.Server:
    wire = schemas.Server.model_validate(server)
    wire.enrolled = server.server_cred_hash is not None
    wire.assigned_account_count = inventory.assigned_account_count(db, server.id)
    return wire


@router.get("/servers", response_model=schemas.ServerPage)
def list_servers(
    tenant_id: uuid.UUID,
    db: DbSession,
    status_filter: schemas.ServerStatus | None = Query(default=None, alias="status"),
    pageSize: PageSize = 50,  # noqa: N803
    pageToken: PageToken = None,  # noqa: N803
):
    offset = offset_from_token(pageToken)
    items, total = inventory.list_servers(
        db, tenant_id, status=status_filter, limit=pageSize, offset=offset
    )
    return schemas.ServerPage(
        items=[_to_wire(db, s) for s in items],
        page_info=schemas.PageInfo(
            next_page_token=next_page_token(offset, pageSize, total), total_size=total
        ),
    )


@router.post("/servers", response_model=schemas.Server, status_code=status.HTTP_201_CREATED)
def create_server(tenant_id: uuid.UUID, body: schemas.ServerCreate, db: DbSession):
    server = inventory.create_server(
        db, tenant_id, name=body.name, hostname=body.hostname, switch_mode=body.switch_mode
    )
    return _to_wire(db, server)


@router.get("/servers/{server_id}", response_model=schemas.Server)
def get_server(tenant_id: uuid.UUID, server_id: uuid.UUID, db: DbSession):
    return _to_wire(db, inventory.get_server(db, tenant_id, server_id))


@router.patch("/servers/{server_id}", response_model=schemas.Server)
def update_server(
    tenant_id: uuid.UUID, server_id: uuid.UUID, body: schemas.ServerUpdate, db: DbSession
):
    server = inventory.update_server(
        db, tenant_id, server_id, name=body.name, hostname=body.hostname, status=body.status
    )
    # O4-C policy fields are set only when actually present in the PATCH body
    # (so a name-only PATCH does not clear the policy), then re-delivered to a
    # connected agent via the outbox.
    fields = body.model_fields_set
    if "threshold_pct" in fields or "default_strategy" in fields:
        kwargs = {}
        if "threshold_pct" in fields:
            kwargs["threshold_pct"] = body.threshold_pct
        if "default_strategy" in fields:
            kwargs["default_strategy"] = body.default_strategy
        inventory.set_server_policy(db, tenant_id, server_id, **kwargs)
        db.commit()
        commands.request_set_policy(db, tenant_id, server_id)
        db.refresh(server)
    return _to_wire(db, server)


@router.delete("/servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_server(tenant_id: uuid.UUID, server_id: uuid.UUID, db: DbSession) -> Response:
    inventory.delete_server(db, tenant_id, server_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/servers/{server_id}/enroll-token",
    response_model=schemas.EnrollTokenResponse,
    status_code=status.HTTP_201_CREATED,
)
def issue_enroll_token(
    tenant_id: uuid.UUID,
    server_id: uuid.UUID,
    db: DbSession,
    body: schemas.EnrollTokenRequest | None = None,
):
    request = body or schemas.EnrollTokenRequest()
    token, expires_at = inventory.issue_enroll_token(
        db, tenant_id, server_id, ttl_seconds=request.ttl_seconds
    )
    # The only response in this service that returns a live secret. It is shown
    # once and only its hash is kept (§7).
    return schemas.EnrollTokenResponse(token=token, expires_at=expires_at)


@router.get("/servers/{server_id}/usage", response_model=schemas.UsageSnapshot)
def get_server_usage(tenant_id: uuid.UUID, server_id: uuid.UUID, db: DbSession):
    inventory.get_server(db, tenant_id, server_id)
    # A real read against the snapshot table, not a stub: it serves the DB
    # cache and never reaches out to the agent. Nothing writes to that table
    # until the P2 ingest lands, so in P1 the answer is always the 404 the
    # contract defines for "no report yet".
    snapshot = inventory.latest_usage_snapshot(db, tenant_id, server_id)
    if snapshot is None:
        raise not_found("usage snapshot")
    return schemas.UsageSnapshot.model_validate(snapshot)


@router.post("/servers/{server_id}:refresh-usage", status_code=status.HTTP_202_ACCEPTED)
def refresh_usage(tenant_id: uuid.UUID, server_id: uuid.UUID, db: DbSession):
    # Queues a RequestReport for the connected agent; the report arrives back on
    # the session stream (§6.3 req_report). Resolves the server first so a
    # cross-tenant id gets 404 before anything is queued.
    commands.request_refresh_usage(db, tenant_id, server_id)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/servers/{server_id}:switch-mode", response_model=schemas.Server)
def set_switch_mode(
    tenant_id: uuid.UUID, server_id: uuid.UUID, body: schemas.SwitchModeRequest, db: DbSession
):
    server = commands.request_switch_mode(db, tenant_id, server_id, mode=body.mode)
    return _to_wire(db, server)
