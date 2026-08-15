"""Usage-cost allocation — usage-cost PR2 (design: spread each account's whole
subscription price across the servers that used it, by time-weighted utilization).

Two layers, mirroring the F5 billing sweep but on a *separate* cursor and lock so
the two never couple:

* ``sweep_usage_rollup`` compacts each newly-closed UTC day of ``usage_snapshots``
  into ``usage_daily_rollup`` — one row per (tenant, day, server, account) holding
  the utilization integral (``held_util_seconds``) and the observed coverage
  (``observed_seconds``). Idempotent: a re-run over a sealed (immutable) day upserts
  the identical values.
* ``compute_month_cost`` answers a month query by summing the sealed rollup rows and
  integrating the still-open tail from ``usage_snapshots`` on the fly, then spreading
  each account's ``monthly_price`` across its servers by held-utilization share.

Allocation definition (not a usage *discount* — the whole price is always spread):

    Cost(s, a) = monthly_price(a) x held_util(s, a) / Sum_s' held_util(s', a)

with a fallback to ``observed_seconds`` share when an account was observed but never
held current (Sum held == 0), and left unallocated when neither basis exists. A NULL
``monthly_price`` carries no cost and is skipped. Amounts are per-account ``currency``;
different currencies are never summed, only subtotalled separately.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import try_advisory_xact_lock as _try_advisory_xact_lock
from app.models import (
    Account,
    BillingCursor,
    Server,
    Tenant,
    UsageDailyRollup,
    UsageSnapshot,
)
from app.services import alerts, billing

_logger = logging.getLogger("ams.usage_cost")

# Separate cursor kind + advisory-lock key from the F5 billing sweep (…0F03) so
# the rollup and the billing sweep run independently and never block each other.
ROLLUP_KIND = "usage_rollup"
_ROLLUP_SWEEP_LOCK_KEY = 0x414D580F04

# 적분에서 제외하는 상태는 ABSENT만이다. 회수(recall)는 항상 purge라(O2 변경,
# 2026-08-14) 회수된 계정은 tsamx 풀에서 사라져 애초에 보고에 실리지 않으므로
# 별도 제외가 필요 없다. INACTIVE는 '일시 비활성(deactivate)' — 자격증명을 그
# 서버에 점유한 채 로테이션에서만 빠진 상태다. INACTIVE를 제외하면 그 구독료가
# 어느 서버에도 배분되지 않고 증발해 '전액 배분' 불변식을 깨므로, INACTIVE는
# 적분에 포함한다(배분 유지). 과도기 INACTIVE 잔재(구버전 에이전트·기왕의
# disable)는 reconcile의 purge 재발행으로 청소되고, cap(재시도 3회) 소진분은
# 수동 정리 대상이다(DEV-TEST-GUIDE 참고).
_ABSENT_STATUS = "ALLOCATION_STATUS_ABSENT"
# A healthy sweep advances the watermark one closed day per run; a larger jump is
# an outage recovery or a forward wall-clock step, worth surfacing (log-only).
_MAX_NORMAL_ADVANCE = timedelta(days=2)

# Seconds are stored/aggregated at the rollup column's scale (NUMERIC(20,6)); money
# is quantized to two places. Integration runs in float and is quantized to these
# grids at the storage / aggregation boundary so sealed and live tails combine on
# identical rounding.
_SEC_Q = Decimal("0.000001")
_MONEY_Q = Decimal("0.01")


def _now() -> datetime:
    return datetime.now(UTC)


def _floor_day(dt: datetime) -> datetime:
    dt = dt.astimezone(UTC)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _ceil_day(dt: datetime) -> datetime:
    """The smallest UTC midnight >= ``dt`` (``dt`` itself if already a midnight)."""
    dt = dt.astimezone(UTC)
    floored = _floor_day(dt)
    return floored if floored == dt else floored + timedelta(days=1)


def _num(v: object) -> float | None:
    """A finite numeric pct, or None. ``bool`` is not a number here."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _sec(x: float) -> Decimal:
    return Decimal(str(x)).quantize(_SEC_Q)


# -- pure integration ---------------------------------------------------------
def account_utilization(acc: dict) -> float:
    """Instantaneous global utilization u_a(t) for one AccountUsage dict.

    Provider-agnostic: the self-describing ``windows`` list wins when present
    (max pct across windows); otherwise the legacy positional ``five_hour`` /
    ``seven_day`` are the fallback. A missing pct reads as 0.0 (proto3 drops a
    0.0 scalar from MessageToDict), never as an error.
    """
    windows = acc.get("windows")
    if isinstance(windows, list) and windows:
        best: float | None = None
        for w in windows:
            if isinstance(w, dict):
                pct = _num(w.get("pct"))
                best = pct if best is None else max(best, pct) if pct is not None else best
        if best is not None:
            return best
        # windows present but no usable pct in any of them -> treat as 0.0.
        return 0.0
    best = 0.0
    for key in ("five_hour", "seven_day"):
        w = acc.get(key)
        if isinstance(w, dict):
            pct = _num(w.get("pct"))
            if pct is not None:
                best = max(best, pct)
    return best


def _account_uuid(acc: dict) -> uuid.UUID | None:
    ref = acc.get("account")
    raw = ref.get("ams_account_id") if isinstance(ref, dict) else None
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError):
        return None


# (server_id, account_id) -> [held_util_seconds, observed_seconds, snapshot_count]
_Acc = dict[tuple[uuid.UUID, uuid.UUID], list]


def integrate_day(
    rows: list[tuple[datetime, uuid.UUID, dict]],
    day_start: datetime,
    horizon: datetime,
    gap_seconds: float,
) -> _Acc:
    """Step-integrate one day's usage snapshots into per-(server, account) sums.

    ``rows`` are ``(reported_at, server_id, payload)`` inside ``[day_start,
    horizon)``, one payload per snapshot. Each snapshot's state holds until the
    same server's next snapshot; that interval is clamped to ``gap_seconds`` (so a
    long report gap cannot let a stale value dominate) and to ``horizon`` (so a
    day-boundary crossing is charged to whichever day owns it, and the still-open
    tail stops at ``now``). ABSENT and un-reported intervals contribute nothing;
    INACTIVE (deactivated but credential-resident) still allocates.
    """
    by_server: dict[uuid.UUID, list[tuple[datetime, dict]]] = defaultdict(list)
    for reported_at, server_id, payload in rows:
        if isinstance(payload, dict):
            by_server[server_id].append((reported_at.astimezone(UTC), payload))

    out: _Acc = {}
    for server_id, ticks in by_server.items():
        ticks.sort(key=lambda t: t[0])
        for i, (t_i, payload) in enumerate(ticks):
            span = min(gap_seconds, (horizon - t_i).total_seconds())
            if i + 1 < len(ticks):
                span = min(span, (ticks[i + 1][0] - t_i).total_seconds())
            if span <= 0:
                continue
            accounts = payload.get("accounts")
            if not isinstance(accounts, list):
                continue
            for acc in accounts:
                if not isinstance(acc, dict):
                    continue
                if acc.get("allocation_status") == _ABSENT_STATUS:
                    continue
                aid = _account_uuid(acc)
                if aid is None:
                    continue
                cell = out.setdefault((server_id, aid), [0.0, 0.0, 0])
                cell[1] += span
                cell[2] += 1
                if acc.get("is_current"):
                    cell[0] += account_utilization(acc) * span
    return out


# -- rollup sweep -------------------------------------------------------------
def sweep_usage_rollup(db: Session) -> int:
    """Compact every newly-closed UTC day into ``usage_daily_rollup``. Returns rows upserted.

    One transaction: the advisory lock, the per-day upserts, and the watermark
    advance commit together. Mirrors ``billing.sweep_billing`` but on
    ``ROLLUP_KIND`` / ``_ROLLUP_SWEEP_LOCK_KEY`` so the two sweeps never couple.
    """
    if not _try_advisory_xact_lock(db, _ROLLUP_SWEEP_LOCK_KEY):
        return 0

    settings = get_settings()
    grace = settings.billing_close_grace_seconds
    gap = float(settings.usage_max_gap_seconds)
    last_closed_end = _floor_day(_now() - timedelta(seconds=grace))

    cursor = db.get(BillingCursor, ROLLUP_KIND)
    if cursor is not None:
        start = cursor.watermark.astimezone(UTC)
    else:
        first = db.scalar(
            select(func.min(UsageSnapshot.reported_at)).where(
                UsageSnapshot.report_type == "usage"
            )
        )
        if first is None:
            return 0  # no usage ledger yet
        start = _floor_day(first)

    # G27 visibility (log-only, mirrors billing.sweep_billing). A forward
    # wall-clock step parks the watermark ahead of real time; snapshots that then
    # land below it are skipped forever by the range query below. Count what sits
    # in [last_closed_end, start) — empty in the healthy case — and warn if any.
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
                "usage-rollup sweep: %d usage snapshot(s) below the watermark will "
                "not be rolled up (watermark ahead of real time; min reported_at=%s)",
                skipped,
                min_reported.isoformat() if min_reported else None,
            )
        # G27 self-heal: the watermark is parked in the future (a forward
        # wall-clock step). Rewind the cursor and COMMIT it with the same upsert
        # used on a normal advance, so the ``return 0`` just below cannot leave the
        # cursor stranded ahead of real time. This tick idles; the next tick
        # re-rolls the reopened days from real snapshots — the rollup is ON CONFLICT
        # DO UPDATE recompute-replace (idempotent), so no double-count.
        #
        # Clamp the rewind target UP to the retention purge cursor: a fake-future
        # tick can seal a day from its morning snapshots, then retention (its
        # boundary guard also fooled by the fake clock) can delete those morning
        # rows. Rewinding below such a day would let the next tick recompute it from
        # the afternoon survivors and REPLACE the sealed row with a partial value.
        # Keeping the cursor at/above the purged bound leaves those days sealed; the
        # surviving snapshots below the cursor are surfaced by the below-watermark
        # warning above rather than silently recomputed.
        purge = db.get(BillingCursor, billing.PURGE_KIND)
        rewind_to = last_closed_end
        if purge is not None:
            rewind_to = max(rewind_to, purge.watermark.astimezone(UTC))
        now = _now()
        db.execute(
            pg_insert(BillingCursor)
            .values(kind=ROLLUP_KIND, watermark=rewind_to, updated_at=now)
            .on_conflict_do_update(
                index_elements=["kind"],
                set_={"watermark": rewind_to, "updated_at": now},
            )
        )
        # Invariant: this commit releases the sweep's xact advisory lock early, but
        # the ``return 0`` below means no further work happens this tick, so the
        # early release is safe. This holds ONLY because rewind_to >= last_closed_end
        # (the day loop range [rewind_to, last_closed_end) is empty and we return).
        # Lowering rewind_to below last_closed_end would both re-open purged/sealed
        # days for partial recompute AND make this early lock release unsafe.
        db.commit()
        _logger.warning(
            "usage-rollup sweep: watermark rewound from %s to %s (was parked ahead "
            "of real time)",
            start.isoformat(),
            rewind_to.isoformat(),
        )
        start = rewind_to

    if start >= last_closed_end:
        return 0  # no fully-closed day beyond the watermark

    # G27 visibility: a healthy run advances one closed day. An abnormally large
    # advance (outage recovery or a forward clock step) is surfaced, but only once
    # a watermark exists — a first run legitimately backfills from the earliest
    # ledger row. Log-only.
    if cursor is not None and last_closed_end - start > _MAX_NORMAL_ADVANCE:
        _logger.warning(
            "usage-rollup sweep: watermark advancing %s in one run (>%s) from %s to %s",
            last_closed_end - start,
            _MAX_NORMAL_ADVANCE,
            start.isoformat(),
            last_closed_end.isoformat(),
        )

    upserted = 0
    day = start
    while day < last_closed_end:
        next_day = day + timedelta(days=1)
        rows = db.execute(
            select(
                UsageSnapshot.reported_at,
                UsageSnapshot.server_id,
                UsageSnapshot.tenant_id,
                UsageSnapshot.payload,
            ).where(
                UsageSnapshot.report_type == "usage",
                UsageSnapshot.reported_at >= day,
                UsageSnapshot.reported_at < next_day,
            )
        ).all()
        server_tenant = {r.server_id: r.tenant_id for r in rows}
        sums = integrate_day(
            [(r.reported_at, r.server_id, r.payload) for r in rows], day, next_day, gap
        )
        values = []
        for (server_id, account_id), (held, observed, count) in sums.items():
            values.append(
                {
                    "tenant_id": server_tenant[server_id],
                    "day": day.date(),
                    "server_id": server_id,
                    "account_id": account_id,
                    "held_util_seconds": _sec(held),
                    "observed_seconds": _sec(observed),
                    "snapshot_count": count,
                }
            )
        if values:
            # A sealed day's INPUTS (snapshots) are immutable, so re-running the
            # same integration logic reproduces the same row — ON CONFLICT DO
            # UPDATE keeps the sweep idempotent (recompute-replace) rather than
            # duplicating. Idempotence is over the code version, not absolute: a
            # change to the integration rules (e.g. an allocation_status exclusion)
            # intentionally REPLACES prior rows with values under the new logic on
            # the next recompute of that day.
            stmt = pg_insert(UsageDailyRollup).values(values)
            db.execute(
                stmt.on_conflict_do_update(
                    constraint="pk_usage_daily_rollup",
                    set_={
                        "held_util_seconds": stmt.excluded.held_util_seconds,
                        "observed_seconds": stmt.excluded.observed_seconds,
                        "snapshot_count": stmt.excluded.snapshot_count,
                    },
                )
            )
            upserted += len(values)
        day = next_day

    now = _now()
    db.execute(
        pg_insert(BillingCursor)
        .values(kind=ROLLUP_KIND, watermark=last_closed_end, updated_at=now)
        .on_conflict_do_update(
            index_elements=["kind"],
            set_={"watermark": last_closed_end, "updated_at": now},
        )
    )
    db.commit()
    return upserted


# -- watermark-future guard (G27) ---------------------------------------------
def sweep_watermark_future(db: Session) -> bool:
    """Raise/resolve ``billing_watermark_future`` from the rollup watermark.

    A forward wall-clock step can park the ``ROLLUP_KIND`` cursor ahead of real
    time; snapshots that then land below it are skipped forever by the rollup
    range query and go silently unbilled (BACKLOG G27). This reads the cursor
    only — never rewinds it — and delegates the per-tenant alert lifecycle to
    ``alerts.sync_watermark_future``. Self-commits; safe to run every sweep tick.

    Returns True while the watermark sits beyond the skew tolerance in the future.
    """
    cursor = db.get(BillingCursor, ROLLUP_KIND)
    watermark = cursor.watermark.astimezone(UTC) if cursor is not None else None
    skew = float(get_settings().billing_watermark_skew_seconds)
    tenant_ids = list(db.scalars(select(Tenant.id)))
    future = alerts.sync_watermark_future(
        db, watermark=watermark, now=_now(), skew_seconds=skew, tenant_ids=tenant_ids
    )
    db.commit()
    return future


# -- snapshot retention purge -------------------------------------------------
# The retention sweep's own advisory-lock key (…05), distinct from the offline
# (…01), sent-ack (…02), billing (…03) and rollup (…04) sweeps so one instance
# owning the purge for a tick never blocks the others.
_RETENTION_SWEEP_LOCK_KEY = 0x414D580F05
# Rows deleted per statement. The purge loops fixed-size batches (never one bulk
# DELETE) so a first run over a large backlog never pins a table-wide row-lock
# set in one long transaction; each batch commits on its own.
_RETENTION_BATCH = 5000


def _settlement_boundary(db: Session) -> datetime | None:
    """Instant strictly-before which every snapshot is fully settled, or None.

    Both the rollup sweep (``ROLLUP_KIND``) and the billing sweep (``billing.KIND``)
    integrate the raw ``usage_snapshots``; a snapshot is only past settlement once
    BOTH watermarks have advanced beyond it, so the boundary is the MIN of the two.
    A missing cursor means that sweep has sealed nothing yet — no snapshot is
    settled — so return None (purge nothing).
    """
    marks = {
        kind: watermark
        for kind, watermark in db.execute(
            select(BillingCursor.kind, BillingCursor.watermark).where(
                BillingCursor.kind.in_([ROLLUP_KIND, billing.KIND])
            )
        ).all()
    }
    rollup = marks.get(ROLLUP_KIND)
    billing_wm = marks.get(billing.KIND)
    if rollup is None or billing_wm is None:
        return None
    return min(rollup.astimezone(UTC), billing_wm.astimezone(UTC))


def sweep_snapshot_retention(db: Session) -> int:
    """Purge settled ``report_type == "usage"`` snapshots past the retention window.

    Deletes only ``usage`` rows. ``switch_event`` rows are the sole source of the
    console event timeline (``inventory.list_switch_events`` ->
    ``GET /servers/{id}/switch-events``), so they are retained regardless of age.
    A usage snapshot is deleted only when its ``reported_at`` is before BOTH
    ``now - retention`` and the settlement boundary — the settlement guard is
    absolute, so the rollup/billing integrals can never lose an input.

    Two safety cut-offs return 0 without deleting anything:
    ``usage_snapshot_retention_days`` <= 0 (purge disabled), and a settlement
    boundary parked in the FUTURE. A G27 wall-clock jump can strand a watermark
    ahead of real time (usage_cost.py / billing.py); snapshots below such a
    watermark are permanently un-settled, so treating the boundary as "settled"
    would delete live data — the purge halts and warns instead. Returns rows deleted.
    """
    days = get_settings().usage_snapshot_retention_days
    if days <= 0:
        return 0
    boundary = _settlement_boundary(db)
    if boundary is None:
        return 0
    now = _now()
    if boundary > now:
        _logger.warning(
            "snapshot retention halted: settlement watermark %s is in the future "
            "(> now %s); snapshots below it are un-settled and must not be purged",
            boundary.isoformat(),
            now.isoformat(),
        )
        return 0
    delete_before = min(now - timedelta(days=days), boundary)

    total = 0
    while True:
        # Lock scope (option a): each batch is its OWN transaction that re-acquires
        # the transaction-scoped advisory lock, because the previous batch's commit
        # released it. One bulk delete under a single held lock is exactly what we
        # avoid — a large first run would pin the lock and its row-lock set for the
        # whole duration. Failing to re-acquire means another instance took over the
        # purge this tick; yield the remaining batches to it.
        if not _try_advisory_xact_lock(db, _RETENTION_SWEEP_LOCK_KEY):
            break
        ids = db.execute(
            select(UsageSnapshot.id)
            .where(
                UsageSnapshot.report_type == "usage",
                UsageSnapshot.reported_at < delete_before,
            )
            .limit(_RETENTION_BATCH)
        ).scalars().all()
        if not ids:
            db.rollback()  # release the lock; nothing left to delete
            break
        db.execute(delete(UsageSnapshot).where(UsageSnapshot.id.in_(ids)))
        db.commit()  # releases the advisory lock until the next batch re-takes it
        total += len(ids)
        if len(ids) < _RETENTION_BATCH:
            break
    if total:
        # Record how far the purge reached so the G27 rewind clamp (usage_cost /
        # billing) never rewinds a cursor below deleted data and recomputes a sealed
        # day from the partial survivors. Store the day CEILING of delete_before,
        # not delete_before itself: a mid-day delete_before still partially empties
        # the day that contains it, so the first fully-intact day is the next
        # midnight — and a day-aligned cursor keeps the rollup day-loop boundaries
        # aligned. Monotonic: the ``where`` guard only advances the cursor (a
        # backward clock step cannot lower the purged bound).
        purge_bound = _ceil_day(delete_before)
        db.execute(
            pg_insert(BillingCursor)
            .values(kind=billing.PURGE_KIND, watermark=purge_bound, updated_at=now)
            .on_conflict_do_update(
                index_elements=["kind"],
                set_={"watermark": purge_bound, "updated_at": now},
                where=BillingCursor.watermark < purge_bound,
            )
        )
        db.commit()
        _logger.info(
            "snapshot retention purged %d expired usage snapshot(s) before %s",
            total,
            delete_before.isoformat(),
        )
    return total


# -- month cost (PR3 consumes this) -------------------------------------------
@dataclass
class ServerCostLine:
    server_id: uuid.UUID
    server_name: str | None
    held_util_seconds: Decimal
    observed_seconds: Decimal
    share: Decimal  # fraction of the account's allocation basis, 0..1
    cost: Decimal


@dataclass
class AccountCost:
    account_id: uuid.UUID
    email: str | None
    provider: str | None
    monthly_price: Decimal | None
    currency: str
    # "held": spread by held-utilization; "observed": Sum held == 0 fallback;
    # "unallocated": observed & held both 0 (price could not be placed);
    # "no_price": monthly_price is NULL (nothing to spread).
    basis: str
    total_held_util_seconds: Decimal
    total_observed_seconds: Decimal
    servers: list[ServerCostLine] = field(default_factory=list)


@dataclass
class CurrencySubtotal:
    currency: str
    allocated_cost: Decimal
    unallocated_cost: Decimal  # price of accounts with a basis of "unallocated"


@dataclass
class MonthCost:
    tenant_id: uuid.UUID
    year: int
    month: int
    # When the figure was computed (UTC). PR3/REST stamps the response with it.
    as_of: datetime
    # Sealed boundary: days strictly before this UTC date are read from the
    # immutable rollup; on/after it is the live tail integrated on the fly. None
    # means nothing is sealed yet (the rollup sweep has not run).
    watermark: date | None
    # True when the answer includes any not-yet-sealed (live tail) data — i.e. the
    # month is still open or the rollup has not caught up to it, so the figure can
    # still move.
    is_partial: bool
    accounts: list[AccountCost] = field(default_factory=list)
    subtotals: list[CurrencySubtotal] = field(default_factory=list)


def _distribute(total: Decimal, weights: dict[uuid.UUID, Decimal]) -> dict[uuid.UUID, Decimal]:
    """Split ``total`` across ``weights`` so the parts sum exactly to ``total``.

    Each part is ``total * w / Sum w`` floored to a cent; the leftover cents go to
    the largest fractional remainders (largest-remainder apportionment), so money
    is never lost or invented to rounding. Deterministic: ties break on server id.
    """
    total_weight = sum(weights.values())
    if total_weight <= 0:
        return {}
    floors: dict[uuid.UUID, Decimal] = {}
    remainders: list[tuple[Decimal, uuid.UUID]] = []
    for key, w in weights.items():
        exact = total * w / total_weight
        floor = exact.quantize(_MONEY_Q, rounding=ROUND_DOWN)
        floors[key] = floor
        remainders.append((exact - floor, key))
    leftover = int(((total - sum(floors.values())) / _MONEY_Q).to_integral_value(ROUND_HALF_UP))
    remainders.sort(key=lambda r: (-r[0], str(r[1])))
    for i in range(leftover):
        floors[remainders[i][1]] += _MONEY_Q
    return floors


def _load_cursor_day(db: Session) -> date | None:
    cursor = db.get(BillingCursor, ROLLUP_KIND)
    return cursor.watermark.astimezone(UTC).date() if cursor is not None else None


def compute_month_cost(
    db: Session, tenant_id: uuid.UUID, year: int, month: int
) -> MonthCost:
    """Allocate each account's monthly subscription across its servers for a month.

    Sealed days (behind the rollup watermark) are summed from
    ``usage_daily_rollup``; the still-open tail is integrated from
    ``usage_snapshots`` on the fly, so the answer is a sealed history plus a live
    tail. Returns a plain dataclass tree for PR3 to serialize.
    """
    month_start = date(year, month, 1)
    month_end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    gap = float(get_settings().usage_max_gap_seconds)
    now = _now()
    today = now.date()
    watermark_day = _load_cursor_day(db)

    # (server_id, account_id) -> [held Decimal, observed Decimal, count]
    acc: dict[tuple[uuid.UUID, uuid.UUID], list] = defaultdict(
        lambda: [Decimal(0), Decimal(0), 0]
    )

    # Sealed days from the rollup: [month_start, min(month_end, watermark_day)).
    sealed_end = month_start if watermark_day is None else min(month_end, watermark_day)
    if sealed_end > month_start:
        for r in db.execute(
            select(
                UsageDailyRollup.server_id,
                UsageDailyRollup.account_id,
                UsageDailyRollup.held_util_seconds,
                UsageDailyRollup.observed_seconds,
                UsageDailyRollup.snapshot_count,
            ).where(
                UsageDailyRollup.tenant_id == tenant_id,
                UsageDailyRollup.day >= month_start,
                UsageDailyRollup.day < sealed_end,
            )
        ).all():
            cell = acc[(r.server_id, r.account_id)]
            cell[0] += r.held_util_seconds
            cell[1] += r.observed_seconds
            cell[2] += r.snapshot_count

    # Live tail: month days not yet sealed, up to today. Each day is integrated and
    # quantized exactly as the rollup would store it, so the two tails combine cleanly.
    live_start = month_start if watermark_day is None else max(month_start, watermark_day)
    live_end = min(month_end, today + timedelta(days=1))
    d = live_start
    while d < live_end:
        day_start = datetime(d.year, d.month, d.day, tzinfo=UTC)
        horizon = min(day_start + timedelta(days=1), now)
        if horizon > day_start:
            rows = db.execute(
                select(
                    UsageSnapshot.reported_at,
                    UsageSnapshot.server_id,
                    UsageSnapshot.payload,
                ).where(
                    UsageSnapshot.tenant_id == tenant_id,
                    UsageSnapshot.report_type == "usage",
                    UsageSnapshot.reported_at >= day_start,
                    UsageSnapshot.reported_at < horizon,
                )
            ).all()
            sums = integrate_day(
                [(r.reported_at, r.server_id, r.payload) for r in rows],
                day_start,
                horizon,
                gap,
            )
            for (server_id, account_id), (held, observed, count) in sums.items():
                cell = acc[(server_id, account_id)]
                cell[0] += _sec(held)
                cell[1] += _sec(observed)
                cell[2] += count
        d += timedelta(days=1)

    result = _allocate(db, tenant_id, year, month, acc)
    result.as_of = now
    result.watermark = watermark_day
    # A live tail was folded in whenever there was an unsealed day to integrate.
    result.is_partial = live_start < live_end
    return result


def _allocate(
    db: Session,
    tenant_id: uuid.UUID,
    year: int,
    month: int,
    acc: dict[tuple[uuid.UUID, uuid.UUID], list],
) -> MonthCost:
    account_ids = {a for (_s, a) in acc}
    server_ids = {s for (s, _a) in acc}
    meta = {
        r.id: r
        for r in db.execute(
            select(
                Account.id, Account.email, Account.provider,
                Account.monthly_price, Account.currency,
            ).where(Account.tenant_id == tenant_id, Account.id.in_(account_ids or {uuid.uuid4()}))
        ).all()
    }
    names = {
        r.id: r.name
        for r in db.execute(
            select(Server.id, Server.name).where(
                Server.tenant_id == tenant_id, Server.id.in_(server_ids or {uuid.uuid4()})
            )
        ).all()
    }

    # Regroup by account: account_id -> {server_id: [held, observed, count]}.
    by_account: dict[uuid.UUID, dict[uuid.UUID, list]] = defaultdict(dict)
    for (server_id, account_id), cell in acc.items():
        by_account[account_id][server_id] = cell

    # as_of / watermark / is_partial are stamped by the caller (compute_month_cost),
    # which holds the clock and the sealed-boundary context.
    result = MonthCost(
        tenant_id=tenant_id, year=year, month=month,
        as_of=_now(), watermark=None, is_partial=False,
    )
    subtotals: dict[str, list[Decimal]] = defaultdict(lambda: [Decimal("0.00"), Decimal("0.00")])

    for account_id in sorted(by_account, key=str):
        servers = by_account[account_id]
        m = meta.get(account_id)
        email = m.email if m else None
        provider = m.provider if m else None
        price = m.monthly_price if m else None
        currency = (m.currency if m else None) or "USD"

        total_held = sum((c[0] for c in servers.values()), Decimal(0))
        total_observed = sum((c[1] for c in servers.values()), Decimal(0))

        if price is None:
            basis = "no_price"
            weights: dict[uuid.UUID, Decimal] = {}
        elif total_held > 0:
            basis = "held"
            weights = {s: c[0] for s, c in servers.items()}
        elif total_observed > 0:
            basis = "observed"
            weights = {s: c[1] for s, c in servers.items()}
        else:
            basis = "unallocated"
            weights = {}

        costs = _distribute(price, weights) if (price is not None and weights) else {}
        basis_total = total_held if basis == "held" else total_observed

        lines = []
        for server_id, c in servers.items():
            weight = weights.get(server_id, Decimal(0))
            share = (weight / basis_total) if basis_total > 0 and server_id in weights else Decimal(0)
            lines.append(
                ServerCostLine(
                    server_id=server_id,
                    server_name=names.get(server_id),
                    held_util_seconds=c[0],
                    observed_seconds=c[1],
                    share=share,
                    cost=costs.get(server_id, Decimal("0.00")),
                )
            )
        lines.sort(key=lambda ln: (-ln.cost, str(ln.server_id)))

        result.accounts.append(
            AccountCost(
                account_id=account_id,
                email=email,
                provider=provider,
                monthly_price=price,
                currency=currency,
                basis=basis,
                total_held_util_seconds=total_held,
                total_observed_seconds=total_observed,
                servers=lines,
            )
        )
        if basis in ("held", "observed"):
            subtotals[currency][0] += sum(costs.values())
        elif basis == "unallocated" and price is not None:
            subtotals[currency][1] += price

    for currency in sorted(subtotals):
        allocated, unallocated = subtotals[currency]
        result.subtotals.append(
            CurrencySubtotal(
                currency=currency,
                allocated_cost=allocated,
                unallocated_cost=unallocated,
            )
        )
    return result
