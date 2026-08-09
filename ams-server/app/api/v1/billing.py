"""F5 internal billing endpoints — list + export (design note p5 §6).

Tenant-scoped exactly like the rest of ``/api/v1`` (``/tenants/{tenant_id}/…``):
the path carries the tenant and the service layer re-checks it (§7 defence in
depth), so a cross-tenant event id is a 404 and a cross-tenant list is empty.

Export is an internal-only state transition (pending -> exported); it is
additionally gated by ``GlobalAdmin``, ordered after the router's ``TenantScope``
so a cross-tenant caller sees 404 before a same-tenant tenant-admin sees 403.

These paths are documented in ``contracts/openapi.yaml`` under the ``billing``
tag (design note p5 §6).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query

from app import schemas
from app.api.deps import (
    AdminPrincipal,
    DbSession,
    GlobalAdmin,
    PageSize,
    PageToken,
    TenantScope,
    next_page_token,
    offset_from_token,
)
from app.services import billing

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["billing"], dependencies=[TenantScope])


@router.get("/billing/events", response_model=schemas.BillingEventPage)
def list_billing_events(
    tenant_id: uuid.UUID,
    db: DbSession,
    principal: AdminPrincipal,
    status_filter: schemas.BillingStatus | None = Query(default=None, alias="status"),
    pageSize: PageSize = 50,  # noqa: N803
    pageToken: PageToken = None,  # noqa: N803
):
    offset = offset_from_token(pageToken)
    items, total = billing.list_billing_events(
        db, tenant_id, status=status_filter, limit=pageSize, offset=offset
    )
    return schemas.BillingEventPage(
        items=[schemas.BillingEvent.model_validate(e) for e in items],
        page_info=schemas.PageInfo(
            next_page_token=next_page_token(offset, pageSize, total), total_size=total
        ),
    )


@router.post(
    "/billing/events/{event_id}/export",
    response_model=schemas.BillingEvent,
    dependencies=[GlobalAdmin],
)
def export_billing_event(
    tenant_id: uuid.UUID,
    event_id: uuid.UUID,
    db: DbSession,
    principal: AdminPrincipal,
):
    event = billing.export_billing_event(db, tenant_id, event_id)
    return schemas.BillingEvent.model_validate(event)


@router.post(
    "/billing/events/{event_id}/void",
    response_model=schemas.BillingEvent,
    dependencies=[GlobalAdmin],
)
def void_billing_event(
    tenant_id: uuid.UUID,
    event_id: uuid.UUID,
    db: DbSession,
    principal: AdminPrincipal,
):
    # G26: reverse an exported event and re-aggregate the day. Returns the void
    # (reversal) event. Cross-tenant id -> 404 (before GlobalAdmin's 403);
    # pending target -> 409; re-void -> idempotent 200 with the existing void.
    event = billing.void_billing_event(db, tenant_id, event_id)
    return schemas.BillingEvent.model_validate(event)
