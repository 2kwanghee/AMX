"""Admin management API — `/admins` (F1 RBAC, §7 S2b).

global-admin-only CRUD over the `admins` table. Every route sits behind
`GlobalAdmin`, so a tenant-admin session is 403 and an anonymous request is 401
before any handler runs.

The bootstrap **root** token (`AMX_ADMIN_TOKEN`) is intentionally *not* managed
here: it has no row in `admins`, is always a global-admin, and remains the
break-glass / M2M path independent of whatever this endpoint does. A root Bearer
may *call* these endpoints (it is a global-admin) but cannot list, mutate or
delete itself, because it simply is not in the table.

No handler ever returns `password_hash`: the `Admin` response schema has no such
field, and the plaintext password is only ever an input.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response, status

from app import schemas
from app.api.deps import (
    AdminAuth,
    DbSession,
    GlobalAdmin,
    PageSize,
    PageToken,
    next_page_token,
    offset_from_token,
)
from app.services import admins

# Router-level: AdminAuth (401 first) then GlobalAdmin (403 for a tenant-admin).
# Applied to every route, so no /admins endpoint can be added without the gate.
router = APIRouter(
    prefix="/admins", tags=["admins"], dependencies=[AdminAuth, GlobalAdmin]
)


@router.post("", response_model=schemas.Admin, status_code=status.HTTP_201_CREATED)
def create_admin(body: schemas.AdminCreate, db: DbSession):
    admin = admins.create_admin(
        db,
        email=body.email,
        password=body.password,
        role=body.role,
        tenant_id=body.tenant_id,
    )
    return schemas.Admin.model_validate(admin)


@router.get("", response_model=schemas.AdminPage)
def list_admins(db: DbSession, pageSize: PageSize = 50, pageToken: PageToken = None):  # noqa: N803
    offset = offset_from_token(pageToken)
    items, total = admins.list_admins(db, pageSize, offset)
    return schemas.AdminPage(
        items=[schemas.Admin.model_validate(a) for a in items],
        page_info=schemas.PageInfo(
            next_page_token=next_page_token(offset, pageSize, total), total_size=total
        ),
    )


@router.get("/{admin_id}", response_model=schemas.Admin)
def get_admin(admin_id: uuid.UUID, db: DbSession):
    return schemas.Admin.model_validate(admins.get_admin(db, admin_id))


@router.patch("/{admin_id}", response_model=schemas.Admin)
def update_admin(admin_id: uuid.UUID, body: schemas.AdminUpdate, db: DbSession):
    return schemas.Admin.model_validate(
        admins.update_admin(
            db, admin_id, disabled=body.disabled, password=body.password
        )
    )


@router.delete("/{admin_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_admin(admin_id: uuid.UUID, db: DbSession) -> Response:
    admins.delete_admin(db, admin_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
