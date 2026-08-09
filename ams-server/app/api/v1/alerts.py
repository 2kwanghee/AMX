"""Alert console endpoints — list + acknowledge (design note §4).

Tenant-scoped exactly like the rest of ``/api/v1`` (``/tenants/{tenant_id}/…``),
so the path carries the tenant and the service layer re-checks it (§7 defence in
depth) — a cross-tenant alert id is a 404, and a cross-tenant list is empty.

These paths are new in P4 and are NOT yet in ``contracts/openapi.yaml`` (the
design note §5.3/openapi addition is a separate, contracts-owned change).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query

from app import schemas
from app.api.deps import AdminPrincipal, DbSession, PageSize, PageToken, TenantScope, next_page_token, offset_from_token
from app.services import inventory

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["alerts"], dependencies=[TenantScope])


@router.get("/alerts", response_model=schemas.AlertPage)
def list_alerts(
    tenant_id: uuid.UUID,
    db: DbSession,
    principal: AdminPrincipal,
    status_filter: schemas.AlertStatus | None = Query(default=None, alias="status"),
    kind: schemas.AlertKind | None = Query(default=None),
    pageSize: PageSize = 50,  # noqa: N803
    pageToken: PageToken = None,  # noqa: N803
):
    offset = offset_from_token(pageToken)
    items, total = inventory.list_alerts(
        db, tenant_id, status=status_filter, kind=kind, limit=pageSize, offset=offset
    )
    return schemas.AlertPage(
        items=[schemas.Alert.model_validate(a) for a in items],
        page_info=schemas.PageInfo(
            next_page_token=next_page_token(offset, pageSize, total), total_size=total
        ),
    )


@router.post("/alerts/{alert_id}:ack", response_model=schemas.Alert)
def ack_alert(
    tenant_id: uuid.UUID,
    alert_id: uuid.UUID,
    db: DbSession,
    principal: AdminPrincipal,
    body: schemas.AlertAckRequest | None = None,
):
    request = body or schemas.AlertAckRequest()
    alert = inventory.ack_alert(db, tenant_id, alert_id, acked_by=request.acked_by)
    return schemas.Alert.model_validate(alert)
