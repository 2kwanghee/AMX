"""Shared router dependencies."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.orm import Session

from app.core.auth import Principal, require_admin
from app.core.errors import ApiError, not_found
from app.db import get_session

DbSession = Annotated[Session, Depends(get_session)]
# Router-level enforcement: raises 401 before any handler runs, and no handler
# can be added without it. Its resolved value (a Principal) is discarded here.
AdminAuth = Depends(require_admin)
# Endpoint-level access to the same authenticated Principal. `require_admin` is
# the identical callable, so FastAPI's per-request dependency cache runs it once
# whether a handler takes this or only AdminAuth applies. Handlers declare it to
# reserve the slot P5-S2 reads for tenant scoping; S1 does not use the value.
AdminPrincipal = Annotated[Principal, Depends(require_admin)]


def require_tenant_scope(
    tenant_id: uuid.UUID, principal: Principal = Depends(require_admin)
) -> Principal:
    """Authorize the caller for the path's tenant (F1 RBAC, §4).

    A global-admin (`all_tenants`) always passes; a tenant-admin passes only for
    its own tenant. A caller with no reach to `tenant_id` gets 404, not 403: a
    403 would confirm the tenant exists. Returns the Principal so handlers that
    also declare AdminPrincipal reuse the one cached resolution.

    This is the router-level anchor for every `/tenants/{tenant_id}` sub-router
    (accounts/servers/assignments/alerts): put it in the router `dependencies`
    and no endpoint can be added without it. The meta test asserts exactly that.
    """
    if principal.all_tenants or str(tenant_id) in principal.tenant_ids:
        return principal
    raise not_found("tenant")


def require_global_admin(principal: Principal = Depends(require_admin)) -> Principal:
    """Capability gate for global-admin-only operations (tenant create/rename/delete).

    Ordered *after* `require_tenant_scope` on a `/tenants/{tenant_id}` route so a
    cross-tenant caller sees 404 (hidden) before this can turn a same-tenant
    tenant-admin away with 403 (a real capability refusal, tenant is theirs).
    """
    if principal.role != "global-admin":
        raise ApiError(
            403, "Forbidden", "auth.forbidden", "This operation requires a global-admin."
        )
    return principal


# Router-level dependency shorthands.
TenantScope = Depends(require_tenant_scope)
GlobalAdmin = Depends(require_global_admin)

PageSize = Annotated[int, Query(alias="pageSize", ge=1, le=200)]
PageToken = Annotated[str | None, Query(alias="pageToken")]


def offset_from_token(page_token: str | None) -> int:
    """P1 pagination is a plain offset carried in the opaque page token.

    The contract only promises the token is opaque, so this can become a
    keyset cursor later without a contract change. A token that is not a
    non-negative integer is treated as the first page rather than an error.
    """
    if not page_token:
        return 0
    try:
        offset = int(page_token)
    except ValueError:
        return 0
    return max(0, offset)


def next_page_token(offset: int, limit: int, total: int) -> str | None:
    nxt = offset + limit
    return str(nxt) if nxt < total else None
