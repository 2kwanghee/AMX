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

import logging
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import conflict, not_found
from app.db import try_advisory_xact_lock as _try_advisory_xact_lock
from app.models import BillingCursor, BillingEvent, UsageSnapshot
from app.services.inventory import get_tenant

_logger = logging.getLogger("ams.billing")

KIND = "usage_daily"
# G26 post-export correction. The original row is immutable; a void is a separate
# reversal event and the corrected re-aggregation is a third event. All three
# share (tenant_id, period_start) but differ in ``kind``, so they coexist under
# the UNIQUE (tenant_id, kind, period_start) anchor without touching the original.
VOID_KIND = "usage_daily_void"
REAGG_KIND = "usage_daily_reagg"
# Distinct from the offline (…01) and sent-ack (…02) sweep locks so all three
# can run concurrently on different instances.
_BILLING_SWEEP_LOCK_KEY = 0x414D580F03
# MessageToDict(preserving_proto_field_name=True) renders the enum by NAME, so a
# detached/never-delivered account appears verbatim as this string in payload.
_ABSENT_STATUS = "ALLOCATION_STATUS_ABSENT"
# G27 visibility: a healthy sweep advances the watermark one closed day per run.
# A larger jump means either a long outage recovery or a wall-clock forward step;
# either way it is worth surfacing in the log (no alert row, log-only).
_MAX_NORMAL_ADVANCE = timedelta(days=2)


def _now() -> datetime:
    return datetime.now(UTC)


def _floor_day(dt: datetime) -> datetime:
    dt = dt.astimezone(UTC)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


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

    # G27 visibility (log-only, no alert row). A forward wall-clock step
    # (VM resume / NTP jump) pushes the watermark ahead of real time; usage that
    # then lands with ``reported_at`` below the watermark is skipped forever by
    # the range query below. Count what sits in [last_closed_end, start) — a
    # window that is empty in the healthy case (watermark tracks last_closed_end)
    # and only fills when the watermark has run ahead — so the warning stays
    # quiet in normal operation. Never logs payload content.
    if start > last_closed_end:
        skipped = db.scalar(
            select(func.count()).select_from(UsageSnapshot).where(
                UsageSnapshot.report_type == "usage",
                UsageSnapshot.reported_at >= last_closed_end,
                UsageSnapshot.reported_at < start,
            )
        )
        if skipped:
            min_reported = db.scalar(
                select(func.min(UsageSnapshot.reported_at)).where(
                    UsageSnapshot.report_type == "usage",
                    UsageSnapshot.reported_at >= last_closed_end,
                    UsageSnapshot.reported_at < start,
                )
            )
            _logger.warning(
                "billing sweep: %d usage snapshot(s) below the watermark will "
                "not be billed (watermark ahead of real time; min reported_at=%s)",
                skipped,
                min_reported.isoformat() if min_reported else None,
            )
        # G27 self-heal: the watermark is parked in the future (a forward
        # wall-clock step). Rewind the cursor to last_closed_end and COMMIT it with
        # the same upsert used on a normal advance, so the ``return 0`` just below
        # cannot leave the cursor stranded ahead of real time. This tick idles; the
        # next tick re-bills the reopened days — the per-day insert is ON CONFLICT
        # DO NOTHING on the (tenant, kind, period_start) anchor, so no double-bill.
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
        _logger.warning(
            "billing sweep: watermark rewound from %s to %s (was parked ahead of "
            "real time)",
            start.isoformat(),
            last_closed_end.isoformat(),
        )
        start = last_closed_end

    if start >= last_closed_end:
        return 0  # no fully-closed day beyond the watermark

    # G27 visibility: a healthy run advances one closed day. An abnormally large
    # advance (outage recovery or a forward clock step) is surfaced, but only
    # once a watermark exists — a first run legitimately backfills from the
    # earliest ledger row. Log-only.
    if cursor is not None and last_closed_end - start > _MAX_NORMAL_ADVANCE:
        _logger.warning(
            "billing sweep: watermark advancing %s in one run (>%s) "
            "from %s to %s",
            last_closed_end - start,
            _MAX_NORMAL_ADVANCE,
            start.isoformat(),
            last_closed_end.isoformat(),
        )

    # G28: load the ledger one closed UTC day at a time instead of one bulk
    # ``.all()`` over the whole [start, last_closed_end) span, so a first run or
    # a long-downtime recovery never materialises the entire backlog at once.
    # Results and idempotency are unchanged: aggregation is already per
    # (tenant, day), and each day boundary is an aligned UTC midnight.
    values = []
    day = start
    while day < last_closed_end:
        next_day = day + timedelta(days=1)
        day_rows_all = db.execute(
            select(
                UsageSnapshot.tenant_id,
                UsageSnapshot.server_id,
                UsageSnapshot.payload,
            ).where(
                UsageSnapshot.report_type == "usage",
                UsageSnapshot.reported_at >= day,
                UsageSnapshot.reported_at < next_day,
            )
        ).all()
        # Group this day's rows by tenant.
        by_tenant: dict[uuid.UUID, list[tuple[dict, uuid.UUID]]] = defaultdict(list)
        for tenant_id, server_id, payload in day_rows_all:
            by_tenant[tenant_id].append((payload, server_id))
        for tenant_id, day_rows in by_tenant.items():
            agg = _aggregate_day(day_rows)
            if agg["snapshot_count"] == 0:
                continue  # every row for this tenant/day was malformed
            values.append(
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "kind": KIND,
                    "period_start": day,
                    "period_end": next_day,
                    "payload": agg,
                }
            )
        day = next_day

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


def _aggregate_period(
    db: Session, tenant_id: uuid.UUID, period_start: datetime
) -> dict:
    """Re-aggregate one (tenant, UTC day) from the *current* usage_snapshots.

    Same shape as the sweep's ``_aggregate_day``, but reads live rows so a late
    snapshot that landed after the watermark passed is now included — that is the
    whole point of a post-export re-aggregation.
    """
    day = _floor_day(period_start)
    rows = db.execute(
        select(UsageSnapshot.payload, UsageSnapshot.server_id).where(
            UsageSnapshot.tenant_id == tenant_id,
            UsageSnapshot.report_type == "usage",
            UsageSnapshot.reported_at >= day,
            UsageSnapshot.reported_at < day + timedelta(days=1),
        )
    ).all()
    return _aggregate_day([(payload, server_id) for payload, server_id in rows])


def void_billing_event(
    db: Session, tenant_id: uuid.UUID, event_id: uuid.UUID
) -> BillingEvent:
    """G26 post-export correction — reverse an exported event, then re-aggregate.

    The original row is never mutated. A ``usage_daily_void`` event records the
    reversal (its payload references the original id and aggregate), and the day
    is atomically re-aggregated into a fresh pending ``usage_daily_reagg`` event
    from current snapshots. Net over the day = original − void + reagg = reagg.

    Only an *exported* ``usage_daily`` event is voidable: a pending event is
    corrected by re-sweeping before export, not by a void (409). A second void of
    the same event is an idempotent no-op (200) returning the existing void row —
    the UNIQUE anchor makes the re-insert a conflict, which we treat as success.

    Re-aggregation is folded into the void (not a separate endpoint, not the
    sweep) so the reversal and its replacement commit atomically, and the sweep's
    watermark idempotency is left completely untouched — the day sits behind the
    watermark and the original ``usage_daily`` row still guards its slot, so a
    re-run never re-creates it.
    """
    original = get_billing_event(db, tenant_id, event_id)
    if original.kind != KIND:
        raise conflict(
            "billing.void_not_applicable",
            "Only a usage_daily billing event can be voided.",
        )
    if original.status != "exported":
        raise conflict(
            "billing.void_requires_exported",
            "Only an exported billing event can be voided; a pending one is "
            "corrected by re-sweeping before export.",
        )

    void_id = uuid.uuid4()
    inserted = db.execute(
        pg_insert(BillingEvent)
        .values(
            id=void_id,
            tenant_id=tenant_id,
            kind=VOID_KIND,
            period_start=original.period_start,
            period_end=original.period_end,
            payload={
                "voids_event_id": str(original.id),
                "voided_payload": original.payload,
            },
            status="pending",
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "kind", "period_start"])
        .returning(BillingEvent.id)
    ).all()

    if not inserted:
        # Already voided — idempotent no-op. Return the existing void row.
        db.rollback()
        return db.scalar(
            select(BillingEvent).where(
                BillingEvent.tenant_id == tenant_id,
                BillingEvent.kind == VOID_KIND,
                BillingEvent.period_start == original.period_start,
            )
        )

    agg = _aggregate_period(db, tenant_id, original.period_start)
    if agg["snapshot_count"] > 0:
        db.execute(
            pg_insert(BillingEvent)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                kind=REAGG_KIND,
                period_start=original.period_start,
                period_end=original.period_end,
                payload=agg,
                status="pending",
            )
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "kind", "period_start"]
            )
        )
    db.commit()
    return db.get(BillingEvent, void_id)
