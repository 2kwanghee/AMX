"""Read side of the admin audit trail (console-test gap G53).

The write side is the middleware in ``app.api.audit``; this module only serves
the query behind ``GET /tenants/{tenant_id}/audit-logs``. Rows are returned
newest-first, optionally bounded by a ``created_at`` half-open window
``[from, to)``, with the same opaque-offset pagination as the rest of the API.

Scope: a tenant's own rows are always included. The global (``tenant_id IS
NULL``) rows — tenant-create, admin CRUD and any other tenant-less action — are
included only for a global-admin caller, because a tenant-admin has no business
seeing actions that belong to no tenant of theirs.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import AdminAuditLog
from app.services.inventory import get_tenant


def list_audit_logs(
    db: Session,
    tenant_id: uuid.UUID,
    *,
    include_global: bool,
    since: datetime | None,
    until: datetime | None,
    limit: int,
    offset: int,
) -> tuple[list[AdminAuditLog], int]:
    get_tenant(db, tenant_id)  # 404 for an unknown tenant, exactly like list_assignments
    scope = AdminAuditLog.tenant_id == tenant_id
    if include_global:
        scope = or_(scope, AdminAuditLog.tenant_id.is_(None))
    where = [scope]
    if since is not None:
        where.append(AdminAuditLog.created_at >= since)
    if until is not None:
        where.append(AdminAuditLog.created_at < until)

    total = db.scalar(select(func.count()).select_from(AdminAuditLog).where(*where)) or 0
    rows = db.scalars(
        select(AdminAuditLog)
        .where(*where)
        # Newest first; id breaks ties so the offset window is deterministic
        # across rows sharing a created_at instant.
        .order_by(AdminAuditLog.created_at.desc(), AdminAuditLog.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return list(rows), total
