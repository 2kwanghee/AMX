"""Admin audit-log read API (console-test gap G53).

`GET /tenants/{tenant_id}/audit-logs` returns the mutating-action trail for a
tenant, newest first. The rows are written by the audit middleware
(`app.api.audit`); this router only reads them. TenantScope hides a foreign
tenant as 404; a global-admin additionally sees the global (tenant-less) rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app import schemas
from app.api.deps import (
    AdminPrincipal,
    DbSession,
    PageToken,
    TenantScope,
    next_page_token,
    offset_from_token,
)
from app.services import audit

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["audit"], dependencies=[TenantScope])

# `from` is a Python keyword, so the wire name is bound through an alias. The
# window is half-open [from, to): `from` inclusive, `to` exclusive.
FromTime = Annotated[datetime | None, Query(alias="from")]
ToTime = Annotated[datetime | None, Query(alias="to")]
AuditLimit = Annotated[int, Query(alias="limit", ge=1, le=200)]


@router.get("/audit-logs", response_model=schemas.AuditLogPage)
def list_audit_logs(
    tenant_id: uuid.UUID,
    db: DbSession,
    principal: AdminPrincipal,
    from_: FromTime = None,
    to: ToTime = None,
    limit: AuditLimit = 50,
    pageToken: PageToken = None,  # noqa: N803
):
    offset = offset_from_token(pageToken)
    items, total = audit.list_audit_logs(
        db,
        tenant_id,
        include_global=principal.role == "global-admin",
        since=from_,
        until=to,
        limit=limit,
        offset=offset,
    )
    return schemas.AuditLogPage(
        items=[schemas.AuditLog.model_validate(row) for row in items],
        page_info=schemas.PageInfo(
            next_page_token=next_page_token(offset, limit, total), total_size=total
        ),
    )
