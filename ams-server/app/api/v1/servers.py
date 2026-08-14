"""AMA server CRUD, enrollment tokens, and the P2 command stubs — §5.3."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Response, status
from sqlalchemy import select

from app import schemas
from app.api.deps import AdminPrincipal, DbSession, PageSize, PageToken, TenantScope, next_page_token, offset_from_token
from app.config import get_settings
from app.core.errors import not_found
from app.models import AgentCommand
from app.services import commands, inventory

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["servers"], dependencies=[TenantScope])


def _to_wire(db, server) -> schemas.Server:
    wire = schemas.Server.model_validate(server)
    wire.enrolled = server.server_cred_hash is not None
    wire.assigned_account_count = inventory.assigned_account_count(db, server.id)
    return wire


@router.get("/servers", response_model=schemas.ServerPage)
def list_servers(
    tenant_id: uuid.UUID,
    db: DbSession,
    principal: AdminPrincipal,
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
def create_server(tenant_id: uuid.UUID, body: schemas.ServerCreate, db: DbSession, principal: AdminPrincipal):
    server = inventory.create_server(
        db, tenant_id, name=body.name, hostname=body.hostname, switch_mode=body.switch_mode
    )
    return _to_wire(db, server)


@router.get("/servers/{server_id}", response_model=schemas.Server)
def get_server(tenant_id: uuid.UUID, server_id: uuid.UUID, db: DbSession, principal: AdminPrincipal):
    return _to_wire(db, inventory.get_server(db, tenant_id, server_id))


@router.patch("/servers/{server_id}", response_model=schemas.Server)
def update_server(
    tenant_id: uuid.UUID, server_id: uuid.UUID, body: schemas.ServerUpdate, db: DbSession, principal: AdminPrincipal
):
    server = inventory.update_server(
        db, tenant_id, server_id, name=body.name, hostname=body.hostname, status=body.status
    )
    # O4-C policy fields are set only when actually present in the PATCH body
    # (so a name-only PATCH does not clear the policy), then re-delivered to a
    # connected agent via the outbox.
    fields = body.model_fields_set
    policy_fields = ("threshold_pct", "default_strategy", "cooldown_seconds", "hysteresis_pct")
    if any(f in fields for f in policy_fields):
        kwargs = {f: getattr(body, f) for f in policy_fields if f in fields}
        inventory.set_server_policy(db, tenant_id, server_id, **kwargs)
        db.commit()
        commands.request_set_policy(db, tenant_id, server_id)
        db.refresh(server)
    return _to_wire(db, server)


@router.delete("/servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_server(tenant_id: uuid.UUID, server_id: uuid.UUID, db: DbSession, principal: AdminPrincipal) -> Response:
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
    principal: AdminPrincipal,
    body: schemas.EnrollTokenRequest | None = None,
):
    request = body or schemas.EnrollTokenRequest()
    token, expires_at = inventory.issue_enroll_token(
        db, tenant_id, server_id, ttl_seconds=request.ttl_seconds
    )
    # The only response in this service that returns a live secret. It is shown
    # once and only its hash is kept (§7). The endpoint/pubkey come from process
    # config so the console can render a paste-ready install command.
    settings = get_settings()
    return schemas.EnrollTokenResponse(
        token=token,
        expires_at=expires_at,
        ams_endpoint=settings.ams_endpoint,
        ams_pubkey=settings.ams_pubkey,
    )


@router.get("/servers/{server_id}/usage", response_model=schemas.UsageSnapshot)
def get_server_usage(tenant_id: uuid.UUID, server_id: uuid.UUID, db: DbSession, principal: AdminPrincipal):
    inventory.get_server(db, tenant_id, server_id)
    # A real read against the snapshot table, not a stub: it serves the DB
    # cache and never reaches out to the agent. Nothing writes to that table
    # until the P2 ingest lands, so in P1 the answer is always the 404 the
    # contract defines for "no report yet".
    snapshot = inventory.latest_usage_snapshot(db, tenant_id, server_id)
    if snapshot is None:
        raise not_found("usage snapshot")
    return schemas.UsageSnapshot.model_validate(snapshot)


@router.get("/servers/{server_id}/events", response_model=schemas.EventPage)
def list_server_events(
    tenant_id: uuid.UUID,
    server_id: uuid.UUID,
    db: DbSession,
    principal: AdminPrincipal,
    pageSize: PageSize = 50,  # noqa: N803
    pageToken: PageToken = None,  # noqa: N803
):
    # Switch/quarantine/all_exhausted timeline from usage_snapshots
    # (report_type="switch_event"); resolves the server first so a cross-tenant
    # id is a 404 before any read.
    offset = offset_from_token(pageToken)
    items, total = inventory.list_switch_events(
        db, tenant_id, server_id, limit=pageSize, offset=offset
    )
    return schemas.EventPage(
        items=[schemas.UsageSnapshot.model_validate(s) for s in items],
        page_info=schemas.PageInfo(
            next_page_token=next_page_token(offset, pageSize, total), total_size=total
        ),
    )


@router.post("/servers/{server_id}:refresh-usage", status_code=status.HTTP_202_ACCEPTED)
def refresh_usage(tenant_id: uuid.UUID, server_id: uuid.UUID, db: DbSession, principal: AdminPrincipal):
    # Queues a RequestReport for the connected agent; the report arrives back on
    # the session stream (§6.3 req_report). Resolves the server first so a
    # cross-tenant id gets 404 before anything is queued.
    commands.request_refresh_usage(db, tenant_id, server_id)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/servers/{server_id}:self-update", status_code=status.HTTP_202_ACCEPTED)
def self_update(
    tenant_id: uuid.UUID,
    server_id: uuid.UUID,
    db: DbSession,
    principal: AdminPrincipal,
    body: schemas.SelfUpdateRequest | None = None,
):
    # Queues a SelfUpdate for the connected agent: it fast-forwards its own
    # working tree, rebuilds and restarts (§6.3 self_update). 202 rather than 200
    # because the outcome only arrives later, as the agent's ack — a failure opens
    # a ``self_update_failed`` alert. Resolves the server first so a cross-tenant
    # id gets 404 before anything is queued.
    commit = body.expected_commit if body is not None else ""
    commands.request_self_update(db, tenant_id, server_id, expected_commit=commit)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.get("/servers/{server_id}/self-update-status", response_model=schemas.SelfUpdateStatus)
def get_self_update_status(
    tenant_id: uuid.UUID, server_id: uuid.UUID, db: DbSession, principal: AdminPrincipal
):
    # Read-only projection of the most recent self_update command so the console
    # can visualise its progress (queued -> sent -> acked/failed). Resolves the
    # server first so a cross-tenant id is a 404 before any read; the query is
    # tenant-scoped on top of that. Returns an all-null 200 when the server has
    # never been asked to self-update.
    inventory.get_server(db, tenant_id, server_id)
    row = db.scalar(
        select(AgentCommand)
        .where(
            AgentCommand.server_id == server_id,
            AgentCommand.tenant_id == tenant_id,
            AgentCommand.command_type == "self_update",
        )
        .order_by(AgentCommand.created_at.desc(), AgentCommand.id.desc())
        .limit(1)
    )
    if row is None:
        return schemas.SelfUpdateStatus()
    return schemas.SelfUpdateStatus.model_validate(row)


@router.post("/servers/{server_id}:switch-mode", response_model=schemas.Server)
def set_switch_mode(
    tenant_id: uuid.UUID, server_id: uuid.UUID, body: schemas.SwitchModeRequest, db: DbSession, principal: AdminPrincipal
):
    server = commands.request_switch_mode(db, tenant_id, server_id, mode=body.mode)
    return _to_wire(db, server)
