"""Tenant CRUD — contracts/openapi.yaml `/tenants`."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response, status

from app import schemas
from app.api.deps import AdminAuth, AdminPrincipal, DbSession, PageSize, PageToken, next_page_token, offset_from_token
from app.services import inventory

router = APIRouter(prefix="/tenants", tags=["tenants"], dependencies=[AdminAuth])


@router.get("", response_model=schemas.TenantPage)
def list_tenants(db: DbSession, principal: AdminPrincipal, pageSize: PageSize = 50, pageToken: PageToken = None):  # noqa: N803
    offset = offset_from_token(pageToken)
    items, total = inventory.list_tenants(db, pageSize, offset)
    return schemas.TenantPage(
        items=[schemas.Tenant.model_validate(t) for t in items],
        page_info=schemas.PageInfo(
            next_page_token=next_page_token(offset, pageSize, total), total_size=total
        ),
    )


@router.post("", response_model=schemas.Tenant, status_code=status.HTTP_201_CREATED)
def create_tenant(body: schemas.TenantCreate, db: DbSession, principal: AdminPrincipal):
    return schemas.Tenant.model_validate(inventory.create_tenant(db, body.name))


@router.get("/{tenant_id}", response_model=schemas.Tenant)
def get_tenant(tenant_id: uuid.UUID, db: DbSession, principal: AdminPrincipal):
    return schemas.Tenant.model_validate(inventory.get_tenant(db, tenant_id))


@router.patch("/{tenant_id}", response_model=schemas.Tenant)
def update_tenant(tenant_id: uuid.UUID, body: schemas.TenantUpdate, db: DbSession, principal: AdminPrincipal):
    return schemas.Tenant.model_validate(
        inventory.update_tenant(db, tenant_id, name=body.name, status=body.status)
    )


@router.delete("/{tenant_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tenant(tenant_id: uuid.UUID, db: DbSession, principal: AdminPrincipal) -> Response:
    inventory.delete_tenant(db, tenant_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
