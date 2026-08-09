"""Tenant CRUD — contracts/openapi.yaml `/tenants`.

The tenant *collection* has no `{tenant_id}` in its path, so it cannot lean on
`require_tenant_scope` the way the sub-routers do. Scoping is applied per route
(F1 RBAC, §4):

- `GET /tenants`   — filtered to the caller's reach (a tenant-admin sees only
                     its own tenant; a global-admin sees all).
- `POST /tenants`  — global-admin only (403 for a tenant-admin).
- `GET /tenants/{id}`    — tenant scope (404 for a foreign id, hidden).
- `PATCH/DELETE /{id}`   — tenant scope *then* capability: a foreign id is 404,
                           a same-tenant tenant-admin is 403 (rename/delete are
                           global-admin operations).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response, status

from app import schemas
from app.api.deps import (
    AdminAuth,
    AdminPrincipal,
    DbSession,
    GlobalAdmin,
    PageSize,
    PageToken,
    TenantScope,
    next_page_token,
    offset_from_token,
)
from app.services import inventory

router = APIRouter(prefix="/tenants", tags=["tenants"], dependencies=[AdminAuth])


@router.get("", response_model=schemas.TenantPage)
def list_tenants(db: DbSession, principal: AdminPrincipal, pageSize: PageSize = 50, pageToken: PageToken = None):  # noqa: N803
    offset = offset_from_token(pageToken)
    # None → every tenant (global-admin); the explicit allow-set otherwise.
    allowed = None if principal.all_tenants else principal.tenant_ids
    items, total = inventory.list_tenants(db, pageSize, offset, allowed_tenant_ids=allowed)
    return schemas.TenantPage(
        items=[schemas.Tenant.model_validate(t) for t in items],
        page_info=schemas.PageInfo(
            next_page_token=next_page_token(offset, pageSize, total), total_size=total
        ),
    )


@router.post(
    "",
    response_model=schemas.Tenant,
    status_code=status.HTTP_201_CREATED,
    dependencies=[GlobalAdmin],
)
def create_tenant(body: schemas.TenantCreate, db: DbSession, principal: AdminPrincipal):
    return schemas.Tenant.model_validate(inventory.create_tenant(db, body.name))


@router.get("/{tenant_id}", response_model=schemas.Tenant, dependencies=[TenantScope])
def get_tenant(tenant_id: uuid.UUID, db: DbSession, principal: AdminPrincipal):
    return schemas.Tenant.model_validate(inventory.get_tenant(db, tenant_id))


@router.patch(
    "/{tenant_id}",
    response_model=schemas.Tenant,
    dependencies=[TenantScope, GlobalAdmin],
)
def update_tenant(tenant_id: uuid.UUID, body: schemas.TenantUpdate, db: DbSession, principal: AdminPrincipal):
    return schemas.Tenant.model_validate(
        inventory.update_tenant(db, tenant_id, name=body.name, status=body.status)
    )


@router.delete(
    "/{tenant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[TenantScope, GlobalAdmin],
)
def delete_tenant(tenant_id: uuid.UUID, db: DbSession, principal: AdminPrincipal) -> Response:
    inventory.delete_tenant(db, tenant_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
