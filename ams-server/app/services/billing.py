"""F5 internal billing sweep — usage_snapshots ledger → billing_events outbox.

The ``usage_snapshots`` table is the ledger. Once a UTC day is *closed* (past
the grace window, so late reports have arrived), this sweep aggregates that
day's ``report_type == "usage"`` snapshots into one ``billing_events`` row per
tenant and advances a watermark. It is internal charging only — no external
payment integration — and touches no proto/contract.

Idempotency has two layers: a transaction-scoped advisory lock so only one
instance runs the sweep per tick (matching the offline/sent sweeps), and the
``UNIQUE (tenant_id, kind, period_start)`` anchor with ``ON CONFLICT DO
NOTHING`` so a re-run over the same day never duplicates a row.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import not_found
from app.models import BillingCursor, BillingEvent, UsageSnapshot
from app.services.inventory import get_tenant

KIND = "usage_daily"
# Distinct from the offline (…01) and sent-ack (…02) sweep locks so all three
# can run concurrently on different instances.
_BILLING_SWEEP_LOCK_KEY = 0x414D580F03
# MessageToDict(preserving_proto_field_name=True) renders the enum by NAME, so a
# detached/never-delivered account appears verbatim as this string in payload.
_ABSENT_STATUS = "ALLOCATION_STATUS_ABSENT"


def _now() -> datetime:
    return datetime.now(UTC)


def _floor_day(dt: datetime) -> datetime:
    dt = dt.astimezone(UTC)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _try_advisory_xact_lock(db: Session, key: int) -> bool:
    return bool(db.scalar(select(func.pg_try_advisory_xact_lock(key))))


def _aggregate_day(rows: list[tuple[dict, uuid.UUID]]) -> dict:
    """Build one internal-billing payload from a day's usage snapshot rows.

    Defensive throughout: a malformed row (payload not an object) is skipped and
    excluded from ``snapshot_count``; a missing/garbage sub-field never raises.
    """
    account_ids: set[str] = set()
    server_ids: set[uuid.UUID] = set()
    snapshot_count = 0
    max_util: float | None = None
    for payload, server_id in rows:
        if not isinstance(payload, dict):
            continue
        snapshot_count += 1
        server_ids.add(server_id)
        accounts = payload.get("accounts")
        if isinstance(accounts, list):
            for acc in accounts:
                if not isinstance(acc, dict):
                    continue
                if acc.get("allocation_status") == _ABSENT_STATUS:
                    continue
                ref = acc.get("account")
                aid = ref.get("ams_account_id") if isinstance(ref, dict) else None
                if aid:
                    account_ids.add(aid)
        pool = payload.get("pool_summary")
        if isinstance(pool, dict):
            mu = pool.get("max_utilization_pct")
            if isinstance(mu, (int, float)) and not isinstance(mu, bool):
                max_util = mu if max_util is None else max(max_util, mu)
    return {
        "account_days": len(account_ids),
        "account_ids": sorted(account_ids),
        "server_count": len(server_ids),
        "snapshot_count": snapshot_count,
        "max_utilization_pct": max_util,
    }


def sweep_billing(db: Session) -> int:
    """Aggregate every newly-closed UTC day into billing_events. Returns rows created.

    One transaction: the advisory lock, the ON CONFLICT inserts, and the
    watermark advance all commit together (or roll back together). An instance
    that cannot take the lock returns 0 without touching anything.
    """
    if not _try_advisory_xact_lock(db, _BILLING_SWEEP_LOCK_KEY):
        return 0

    grace = get_settings().billing_close_grace_seconds
    # Last closed day's exclusive end = the newest UTC midnight <= (now - grace).
    last_closed_end = _floor_day(_now() - timedelta(seconds=grace))

    cursor = db.get(BillingCursor, KIND)
    if cursor is not None:
        start = cursor.watermark.astimezone(UTC)
    else:
        first = db.scalar(
            select(func.min(UsageSnapshot.reported_at)).where(
                UsageSnapshot.report_type == "usage"
            )
        )
        if first is None:
            return 0  # no usage ledger yet — nothing to bill
        start = _floor_day(first)

    if start >= last_closed_end:
        return 0  # no fully-closed day beyond the watermark

    rows = db.execute(
        select(
            UsageSnapshot.reported_at,
            UsageSnapshot.tenant_id,
            UsageSnapshot.server_id,
            UsageSnapshot.payload,
        ).where(
            UsageSnapshot.report_type == "usage",
            UsageSnapshot.reported_at >= start,
            UsageSnapshot.reported_at < last_closed_end,
        )
    ).all()

    # Group by (tenant_id, UTC day).
    grouped: dict[tuple[uuid.UUID, datetime], list[tuple[dict, uuid.UUID]]] = (
        defaultdict(list)
    )
    for reported_at, tenant_id, server_id, payload in rows:
        day = _floor_day(reported_at)
        grouped[(tenant_id, day)].append((payload, server_id))

    values = []
    for (tenant_id, day), day_rows in grouped.items():
        agg = _aggregate_day(day_rows)
        if agg["snapshot_count"] == 0:
            continue  # every row for this tenant/day was malformed
        values.append(
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "kind": KIND,
                "period_start": day,
                "period_end": day + timedelta(days=1),
                "payload": agg,
            }
        )

    created = 0
    if values:
        # RETURNING yields only the rows actually inserted — ON CONFLICT skips are
        # omitted — which is a reliable count where rowcount is not.
        inserted = db.execute(
            pg_insert(BillingEvent)
            .values(values)
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "kind", "period_start"]
            )
            .returning(BillingEvent.id)
        ).all()
        created = len(inserted)

    # Advance the watermark to the end of the last closed day, even when no rows
    # were created (e.g. a closed day with only switch_event snapshots).
    now = _now()
    db.execute(
        pg_insert(BillingCursor)
        .values(kind=KIND, watermark=last_closed_end, updated_at=now)
        .on_conflict_do_update(
            index_elements=["kind"],
            set_={"watermark": last_closed_end, "updated_at": now},
        )
    )
    db.commit()
    return created


# -- REST (design note p5 §6) -------------------------------------------------
def list_billing_events(
    db: Session,
    tenant_id: uuid.UUID,
    *,
    status: str | None,
    limit: int,
    offset: int,
) -> tuple[list[BillingEvent], int]:
    get_tenant(db, tenant_id)
    where = [BillingEvent.tenant_id == tenant_id]
    if status:
        where.append(BillingEvent.status == status)
    total = db.scalar(select(func.count()).select_from(BillingEvent).where(*where)) or 0
    rows = db.scalars(
        select(BillingEvent)
        .where(*where)
        .order_by(BillingEvent.period_start.desc(), BillingEvent.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return list(rows), total


def get_billing_event(
    db: Session, tenant_id: uuid.UUID, event_id: uuid.UUID
) -> BillingEvent:
    event = db.get(BillingEvent, event_id)
    # Same-tenant re-check (§7 defence in depth): a cross-tenant id is a 404, not
    # a leak that the event exists.
    if event is None or event.tenant_id != tenant_id:
        raise not_found("billing event")
    return event


def export_billing_event(
    db: Session, tenant_id: uuid.UUID, event_id: uuid.UUID
) -> BillingEvent:
    event = get_billing_event(db, tenant_id, event_id)
    # pending -> exported once; re-exporting an already-exported event is a
    # no-op 200 (idempotent).
    if event.status == "pending":
        event.status = "exported"
        event.exported_at = _now()
        db.commit()
        db.refresh(event)
    return event
