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

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import try_advisory_xact_lock as _try_advisory_xact_lock
from app.models import AdminAuditLog
from app.services.inventory import get_tenant

_logger = logging.getLogger(__name__)

# Audit-log retention sweep (G53). Its own advisory-lock key, next after the
# assignment-retention sweep (…0A), so one instance owning the purge for a tick
# never blocks the others.
_AUDIT_RETENTION_SWEEP_LOCK_KEY = 0x414D580F0B
# Rows deleted per statement — fixed-size batches (never one bulk DELETE) so a
# first run over a large backlog never pins a table-wide row-lock set in one
# long transaction (snapshot/assignment retention convention).
_AUDIT_RETENTION_BATCH = 5000


def _as_utc(value: datetime | None) -> datetime | None:
    """Force a query-bound to UTC so the filter never depends on session TZ.

    A naive datetime from the query string is interpreted as UTC (the trail is
    stored UTC-aware); an aware one is converted. Without this a naive bound
    would compare under the database session's timezone.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


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
    since = _as_utc(since)
    until = _as_utc(until)
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


def sweep_audit_retention(db: Session) -> int:
    """Purge audit rows older than the retention window (G53). Returns rows deleted.

    Opt-in only: `audit_retention_days <= 0` (the default) keeps the trail
    forever and returns 0. When set, rows whose `created_at` has aged past the
    window are batch-deleted, each batch its own transaction re-acquiring the
    transaction-scoped advisory lock the previous commit released; failing to
    re-acquire means another instance took over this tick, so we yield.
    """
    days = get_settings().audit_retention_days
    if days <= 0:
        return 0
    delete_before = datetime.now(UTC) - timedelta(days=days)

    total = 0
    while True:
        if not _try_advisory_xact_lock(db, _AUDIT_RETENTION_SWEEP_LOCK_KEY):
            break
        ids = db.execute(
            select(AdminAuditLog.id)
            .where(AdminAuditLog.created_at < delete_before)
            .limit(_AUDIT_RETENTION_BATCH)
        ).scalars().all()
        if not ids:
            db.rollback()  # release the lock; nothing left to delete
            break
        db.execute(delete(AdminAuditLog).where(AdminAuditLog.id.in_(ids)))
        db.commit()  # releases the advisory lock until the next batch re-takes it
        total += len(ids)
        if len(ids) < _AUDIT_RETENTION_BATCH:
            break
    if total:
        _logger.info(
            "audit retention purged %d audit log(s) older than %s",
            total,
            delete_before.isoformat(),
        )
    return total
