"""Usage-cost allocation — hand-calculated assertions (PR2).

Pure-function tests (no DB) pin the integration and apportionment math with
by-hand expected values; DB-backed tests drive the real rollup sweep and
``compute_month_cost`` against the suite's PostgreSQL, reusing the billing test's
seeding pattern.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.config import get_settings as _get_settings
from app.db import get_sessionmaker
from app.models import Account, Server, UsageSnapshot
from app.services import inventory, usage_cost
from app.services.usage_cost import (
    account_utilization,
    compute_month_cost,
    integrate_day,
    sweep_usage_rollup,
    _distribute,
)

from tests.test_grpc_channel import _oauth_secret, _seed_tenant_account_server


# -- payload builders ---------------------------------------------------------
def _acc(account_id, *, current=False, absent=False, inactive=False, positional=None, windows=None):
    """One AccountUsage dict as MessageToDict(preserving_proto_field_name) renders it."""
    entry: dict = {"account": {"ams_account_id": str(account_id)}}
    if absent:
        entry["allocation_status"] = "ALLOCATION_STATUS_ABSENT"
    elif inactive:
        entry["allocation_status"] = "ALLOCATION_STATUS_INACTIVE"
    else:
        entry["allocation_status"] = "ALLOCATION_STATUS_ACTIVE"
    if current:
        entry["is_current"] = True
    if positional is not None:  # (five_hour_pct, seven_day_pct); None omits that field
        if positional[0] is not None:
            entry["five_hour"] = {"pct": positional[0]}
        if positional[1] is not None:
            entry["seven_day"] = {"pct": positional[1]}
    if windows is not None:  # list of (id, pct, window_minutes)
        entry["windows"] = [
            {"id": wid, "pct": pct, "window_minutes": wm} for (wid, pct, wm) in windows
        ]
    return entry


_DAY = datetime(2026, 3, 10, tzinfo=UTC)
_HORIZON = _DAY + timedelta(days=1)
_GAP = 600.0


def _rows(server_id, *ticks):
    """(offset_seconds, [accounts]) ticks -> integrate_day row tuples."""
    return [(_DAY + timedelta(seconds=off), server_id, {"accounts": accs}) for off, accs in ticks]


# -- (u) utilization: windows-first, positional fallback ----------------------
def test_utilization_windows_and_positional():
    # positional only -> max of the two windows.
    assert account_utilization(_acc("x", positional=(40.0, 30.0))) == 40.0
    # windows present -> wins over positional (codex path / dual-record).
    assert account_utilization(
        _acc("x", positional=(99.0, 99.0), windows=[("five_hour", 20.0, 300), ("seven_day", 65.0, 10080)])
    ) == 65.0
    # missing pct reads as 0.0, never raises.
    assert account_utilization(_acc("x")) == 0.0


# -- (1) held = Sum u*dt, observed = Sum dt, with step integration ------------
def test_integration_held_and_observed():
    s = uuid.uuid4()
    a = uuid.uuid4()
    rows = _rows(
        s,
        (0, [_acc(a, current=True, positional=(40.0, 0.0))]),
        (300, [_acc(a, current=True, positional=(60.0, 0.0))]),
        (600, [_acc(a, current=True, positional=(20.0, 0.0))]),
    )
    out = integrate_day(rows, _DAY, _HORIZON, _GAP)
    held, observed, count = out[(s, a)]
    # dt = 300, 300, 600 (last clamped to gap=600). observed = 1200.
    assert observed == 1200.0
    # held = 40*300 + 60*300 + 20*600 = 12000 + 18000 + 12000 = 42000.
    assert held == 42000.0
    assert count == 3


# -- (3) gap clamp: a long report gap cannot let a stale value dominate --------
def test_gap_clamp():
    s, a = uuid.uuid4(), uuid.uuid4()
    rows = _rows(
        s,
        (0, [_acc(a, current=True, positional=(80.0, 0.0))]),     # stale high
        (5000, [_acc(a, current=True, positional=(10.0, 0.0))]),  # 5000s later
    )
    out = integrate_day(rows, _DAY, _HORIZON, _GAP)
    held, observed, _ = out[(s, a)]
    # Both dt clamp to 600 (not 5000): observed = 1200, not 5600.
    assert observed == 1200.0
    # held = 80*600 + 10*600 = 54000 (unclamped would be 80*5000=400000, dominated).
    assert held == 54000.0


# -- (4) ABSENT and un-reported intervals are excluded ------------------------
def test_absent_excluded():
    s, a, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    rows = _rows(
        s,
        (0, [_acc(a, current=True, positional=(50.0, 0.0)), _acc(b, absent=True)]),
        (300, [_acc(a, absent=True), _acc(b, current=True, positional=(70.0, 0.0))]),
    )
    out = integrate_day(rows, _DAY, _HORIZON, _GAP)
    # dt: tick0 = 300, tick1 (last) = 600.
    held_a, obs_a, cnt_a = out[(s, a)]
    assert (held_a, obs_a, cnt_a) == (50.0 * 300, 300.0, 1)  # 15000, present only at tick0
    held_b, obs_b, cnt_b = out[(s, b)]
    assert (held_b, obs_b, cnt_b) == (70.0 * 600, 600.0, 1)  # 42000, present only at tick1


# -- (4b) INACTIVE (deactivated, credential-resident) STILL allocates ---------
def test_inactive_included():
    # deactivate는 자격증명을 서버에 점유한 채 로테이션에서만 빠진 상태다. 그
    # 구독료가 증발하지 않도록(전액 배분 불변식) INACTIVE는 ABSENT와 달리 적분에
    # 포함된다. held는 is_current일 때만 쌓이므로 INACTIVE 구간의 held는 0이지만
    # observed(=배분 근거 dt)는 정상 누적된다.
    s, a = uuid.uuid4(), uuid.uuid4()
    rows = _rows(
        s,
        (0, [_acc(a, current=True, positional=(50.0, 0.0))]),
        (300, [_acc(a, inactive=True)]),  # deactivated but still reported/resident
    )
    out = integrate_day(rows, _DAY, _HORIZON, _GAP)
    # tick0 dt=300 (ACTIVE, current) -> held 15000; tick1 dt=600 (INACTIVE, not
    # current) -> held +0 but observed +600. Both intervals count toward observed.
    held, observed, count = out[(s, a)]
    assert held == 50.0 * 300      # 15000; INACTIVE interval adds no held (not current)
    assert observed == 900.0       # 300 + 600: INACTIVE still observed (allocates)
    assert count == 2


# -- (5) codex windows[] path -------------------------------------------------
def test_codex_windows_path():
    s, a = uuid.uuid4(), uuid.uuid4()
    rows = _rows(
        s,
        (0, [_acc(a, current=True, windows=[("5h", 20.0, 300), ("7d", 65.0, 10080)])]),
    )
    out = integrate_day(rows, _DAY, _HORIZON, _GAP)
    held, observed, _ = out[(s, a)]
    assert observed == 600.0            # single tick, dt clamped to gap
    assert held == 65.0 * 600           # u = max(20, 65) = 65 -> 39000


# -- day-boundary clamp: last tick charges only up to the horizon -------------
def test_horizon_clamp():
    s, a = uuid.uuid4(), uuid.uuid4()
    rows = _rows(s, (86400 - 100, [_acc(a, current=True, positional=(50.0, 0.0))]))
    out = integrate_day(rows, _DAY, _HORIZON, _GAP)
    _held, observed, _ = out[(s, a)]
    assert observed == 100.0            # 100s to midnight, not the full 600 gap


# -- (2) apportionment: 60/40 split of a whole price --------------------------
def test_distribute_60_40():
    a, b = uuid.uuid4(), uuid.uuid4()
    out = _distribute(Decimal("110.00"), {a: Decimal("54000"), b: Decimal("36000")})
    assert out[a] == Decimal("66.00")
    assert out[b] == Decimal("44.00")
    assert sum(out.values()) == Decimal("110.00")


# -- apportionment loses no cent: largest-remainder sums exactly to the total --
def test_distribute_three_way_no_penny_lost():
    keys = [uuid.uuid4() for _ in range(3)]
    out = _distribute(Decimal("100.00"), {k: Decimal("1") for k in keys})
    assert sum(out.values()) == Decimal("100.00")
    assert sorted(out.values()) == [Decimal("33.33"), Decimal("33.33"), Decimal("33.34")]


# ============================ DB-backed ======================================
def _sm():
    return get_sessionmaker()()


def _set_price(account_id, price, currency="USD"):
    with _sm() as db:
        acc = db.get(Account, account_id)
        acc.monthly_price = None if price is None else Decimal(price)
        acc.currency = currency
        db.commit()


def _add_server(tenant_id):
    with _sm() as db:
        s = inventory.create_server(
            db, tenant_id, name="s-" + uuid.uuid4().hex[:8], hostname="h", switch_mode="auto"
        )
        return s.id


def _add_account(tenant_id, email, price, currency="USD", provider="claude"):
    with _sm() as db:
        a = inventory.create_account(
            db, tenant_id, email=email, credential_type="oauth",
            secret=_oauth_secret(email), provider=provider,
        )
        a.monthly_price = None if price is None else Decimal(price)
        a.currency = currency
        db.commit()
        return a.id


def _plant(tenant_id, server_id, reported_at, accounts):
    with _sm() as db:
        db.add(
            UsageSnapshot(
                tenant_id=tenant_id, server_id=server_id, account_id=None,
                report_type="usage", payload={"accounts": accounts}, reported_at=reported_at,
            )
        )
        db.commit()


def _yesterday_base():
    """A fully-elapsed, not-yet-sealed UTC day inside a live month tail."""
    return usage_cost._floor_day(datetime.now(UTC)) - timedelta(days=1)


def _month_cost(tenant_id, base):
    with _sm() as db:
        return compute_month_cost(db, tenant_id, base.year, base.month)


def _find(mc, account_id):
    return next(a for a in mc.accounts if a.account_id == account_id)


# -- single server takes the whole price --------------------------------------
def test_single_server_full_allocation(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("uc-1@ex.com")
    _set_price(account_id, "100", "USD")
    base = _yesterday_base()
    _plant(tenant_id, server_id, base + timedelta(seconds=3600),
           [_acc(account_id, current=True, positional=(50.0, 0.0))])
    _plant(tenant_id, server_id, base + timedelta(seconds=3900),
           [_acc(account_id, current=True, positional=(50.0, 0.0))])

    mc = _month_cost(tenant_id, base)
    ac = _find(mc, account_id)
    assert ac.basis == "held"
    assert len(ac.servers) == 1
    assert ac.servers[0].cost == Decimal("100.00")
    assert ac.servers[0].share == Decimal(1)
    assert [(s.currency, s.allocated_cost) for s in mc.subtotals] == [("USD", Decimal("100.00"))]


# -- two servers split by held-utilization share (60/40 of $110) --------------
def test_two_server_held_split(app_env):
    tenant_id, account_id, server_a = _seed_tenant_account_server("uc-2@ex.com")
    server_b = _add_server(tenant_id)
    _set_price(account_id, "110", "USD")
    base = _yesterday_base()
    for srv, u in ((server_a, 60.0), (server_b, 40.0)):
        _plant(tenant_id, srv, base + timedelta(seconds=3600),
               [_acc(account_id, current=True, positional=(u, 0.0))])
        _plant(tenant_id, srv, base + timedelta(seconds=3900),
               [_acc(account_id, current=True, positional=(u, 0.0))])

    mc = _month_cost(tenant_id, base)
    ac = _find(mc, account_id)
    assert ac.basis == "held"
    costs = {s.server_id: s.cost for s in ac.servers}
    assert costs[server_a] == Decimal("66.00")
    assert costs[server_b] == Decimal("44.00")
    assert mc.subtotals[0].allocated_cost == Decimal("110.00")


# -- (6) monthly_price NULL is skipped ----------------------------------------
def test_null_price_skipped(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("uc-3@ex.com")
    _set_price(account_id, None)
    base = _yesterday_base()
    _plant(tenant_id, server_id, base + timedelta(seconds=3600),
           [_acc(account_id, current=True, positional=(50.0, 0.0))])

    mc = _month_cost(tenant_id, base)
    ac = _find(mc, account_id)
    assert ac.basis == "no_price"
    assert all(s.cost == Decimal("0.00") for s in ac.servers)
    assert mc.subtotals == []  # nothing to spread, no currency subtotal


# -- (7) Sum held == 0 -> observed-share fallback -----------------------------
def test_observed_fallback(app_env):
    tenant_id, account_id, server_a = _seed_tenant_account_server("uc-4@ex.com")
    server_b = _add_server(tenant_id)
    _set_price(account_id, "100", "USD")
    base = _yesterday_base()
    # Observed on both servers but never is_current -> held total 0.
    for srv in (server_a, server_b):
        _plant(tenant_id, srv, base + timedelta(seconds=3600),
               [_acc(account_id, current=False, positional=(50.0, 0.0))])

    mc = _month_cost(tenant_id, base)
    ac = _find(mc, account_id)
    assert ac.basis == "observed"
    assert ac.total_held_util_seconds == Decimal("0.000000")
    costs = {s.server_id: s.cost for s in ac.servers}
    assert costs[server_a] == Decimal("50.00")  # equal observed -> 50/50
    assert costs[server_b] == Decimal("50.00")
    assert mc.subtotals[0].allocated_cost == Decimal("100.00")


# -- an account that is only ever ABSENT contributes no cell, no cost line -----
def test_all_absent_account_excluded(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("uc-5@ex.com")
    _set_price(account_id, "100", "USD")
    base = _yesterday_base()
    # ABSENT ticks are skipped entirely, so the account is never observed: it
    # produces no (server, account) cell and thus no account line or subtotal.
    # (The "unallocated" basis in _allocate is a defensive tail for a held==0 &&
    #  observed==0 cell, which integration never emits — a present cell always
    #  carries observed > 0.)
    _plant(tenant_id, server_id, base + timedelta(seconds=3600), [_acc(account_id, absent=True)])
    _plant(tenant_id, server_id, base + timedelta(seconds=3900), [_acc(account_id, absent=True)])

    mc = _month_cost(tenant_id, base)
    assert mc.accounts == []
    assert mc.subtotals == []


# -- (8) different currencies are subtotalled separately, never summed --------
def test_multicurrency_subtotals(app_env):
    tenant_id, account_usd, server_usd = _seed_tenant_account_server("uc-6usd@ex.com")
    _set_price(account_usd, "100", "USD")
    server_eur = _add_server(tenant_id)
    account_eur = _add_account(tenant_id, "uc-6eur@ex.com", "200", "EUR")
    base = _yesterday_base()
    _plant(tenant_id, server_usd, base + timedelta(seconds=3600),
           [_acc(account_usd, current=True, positional=(50.0, 0.0))])
    _plant(tenant_id, server_eur, base + timedelta(seconds=3600),
           [_acc(account_eur, current=True, positional=(50.0, 0.0))])

    mc = _month_cost(tenant_id, base)
    subs = {s.currency: s.allocated_cost for s in mc.subtotals}
    assert subs == {"EUR": Decimal("200.00"), "USD": Decimal("100.00")}


# -- rollup sweep: idempotent recompute-replace over a sealed day -------------
def test_rollup_sweep_idempotent(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("uc-roll@ex.com")
    # A fully-closed day (past the grace window), like the billing sweep tests.
    day = (datetime.now(UTC) - timedelta(days=3)).replace(hour=12, minute=0, second=0, microsecond=0)
    _plant(tenant_id, server_id, day, [_acc(account_id, current=True, positional=(50.0, 0.0))])
    _plant(tenant_id, server_id, day + timedelta(seconds=300),
           [_acc(account_id, current=True, positional=(50.0, 0.0))])

    from app.models import BillingCursor, UsageDailyRollup

    with _sm() as db:
        first = sweep_usage_rollup(db)
    assert first == 1

    def _row():
        with _sm() as db:
            return db.get(
                UsageDailyRollup, (tenant_id, day.date(), server_id, account_id)
            )

    r1 = _row()
    # dt = 300 + 600 (gap-clamped last) = 900 observed; held = 50*900 = 45000.
    assert r1.held_util_seconds == Decimal("45000.000000")
    assert r1.observed_seconds == Decimal("900.000000")
    assert r1.snapshot_count == 2

    # Natural re-run: watermark already past the day -> nothing to do.
    with _sm() as db:
        assert sweep_usage_rollup(db) == 0

    # Force a recompute of the same sealed day by rewinding the watermark; the
    # upsert must land the identical values (recompute-replace, not drift/dup).
    with _sm() as db:
        cursor = db.get(BillingCursor, usage_cost.ROLLUP_KIND)
        cursor.watermark = usage_cost._floor_day(day)
        db.commit()
    with _sm() as db:
        assert sweep_usage_rollup(db) == 1
    r2 = _row()
    assert r2.held_util_seconds == r1.held_util_seconds
    assert r2.observed_seconds == r1.observed_seconds
    assert r2.snapshot_count == r1.snapshot_count


# -- MonthCost metadata: as_of / watermark / is_partial -----------------------
def test_month_cost_metadata(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("uc-meta@ex.com")
    _set_price(account_id, "100", "USD")
    base = _yesterday_base()
    _plant(tenant_id, server_id, base + timedelta(seconds=3600),
           [_acc(account_id, current=True, positional=(50.0, 0.0))])

    before = datetime.now(UTC)
    mc = _month_cost(tenant_id, base)
    # No rollup sweep has run: nothing is sealed, and the live tail makes it partial.
    assert mc.watermark is None
    assert mc.is_partial is True
    assert before <= mc.as_of <= datetime.now(UTC)

    # After sweeping a closed day, the watermark is a real sealed boundary date.
    with _sm() as db:
        sweep_usage_rollup(db)
    mc2 = _month_cost(tenant_id, base)
    assert mc2.watermark is not None
    assert isinstance(mc2.watermark, __import__("datetime").date)


# -- sealed rollup + live tail combine in one month total ---------------------
def test_sealed_plus_live_tail(app_env):
    tenant_id, account_id, server_id = _seed_tenant_account_server("uc-seal@ex.com")
    _set_price(account_id, "100", "USD")
    now = datetime.now(UTC)
    # Only meaningful when the closed day and today share a month.
    if now.day <= 3:
        return
    closed = (now - timedelta(days=2)).replace(hour=12, minute=0, second=0, microsecond=0)
    _plant(tenant_id, server_id, closed, [_acc(account_id, current=True, positional=(50.0, 0.0))])
    today_base = usage_cost._floor_day(now)
    _plant(tenant_id, server_id, today_base + timedelta(seconds=60),
           [_acc(account_id, current=True, positional=(50.0, 0.0))])

    with _sm() as db:
        sweep_usage_rollup(db)  # seals the closed day into the rollup

    mc = _month_cost(tenant_id, closed)
    ac = _find(mc, account_id)
    # One account, one server -> whole price regardless of the split of time.
    assert ac.basis == "held"
    assert ac.servers[0].cost == Decimal("100.00")
    # The month total reflects both the sealed day and the live tail (held > the
    # sealed day alone would contribute if the live tick were dropped).
    assert ac.total_held_util_seconds > Decimal("0")


# ============================ retention purge ================================
def _plant_snapshot(tenant_id, server_id, reported_at, report_type="usage", accounts=None):
    """Insert one snapshot of a given report_type; return its id."""
    with _sm() as db:
        row = UsageSnapshot(
            tenant_id=tenant_id, server_id=server_id, account_id=None,
            report_type=report_type, payload={"accounts": accounts or []},
            reported_at=reported_at,
        )
        db.add(row)
        db.commit()
        return row.id


def _set_settlement_cursors(watermark):
    """Park both settlement watermarks (rollup + billing) at ``watermark``."""
    from app.models import BillingCursor
    from app.services import billing

    now = datetime.now(UTC)
    with _sm() as db:
        for kind in (usage_cost.ROLLUP_KIND, billing.KIND):
            db.merge(BillingCursor(kind=kind, watermark=watermark, updated_at=now))
        db.commit()


def _snapshot_exists(snap_id):
    with _sm() as db:
        return db.get(UsageSnapshot, snap_id) is not None


def _use_retention(monkeypatch, days):
    monkeypatch.setattr(
        usage_cost, "get_settings",
        lambda: replace(_get_settings(), usage_snapshot_retention_days=days),
    )


# -- expired+settled is purged; expired+unsettled survives --------------------
def test_retention_purges_settled_expired_keeps_unsettled(app_env, monkeypatch):
    tenant_id, _account_id, server_id = _seed_tenant_account_server("uc-ret1@ex.com")
    now = datetime.now(UTC)
    # Settlement boundary parked 50 days back; retention window 30 days. The
    # effective delete-before is min(now-30d, now-50d) = now-50d.
    _set_settlement_cursors(usage_cost._floor_day(now - timedelta(days=50)))
    _use_retention(monkeypatch, 30)

    # A: expired (60d) AND settled (before the 50d boundary) -> purged.
    a = _plant_snapshot(tenant_id, server_id, now - timedelta(days=60))
    # A switch_event in the same window -> KEPT (console event-timeline source).
    a_switch = _plant_snapshot(
        tenant_id, server_id, now - timedelta(days=61), report_type="switch_event"
    )
    # B: expired (40d > 30d retention) BUT unsettled (after the 50d boundary) -> kept.
    b = _plant_snapshot(tenant_id, server_id, now - timedelta(days=40))
    # C: settled but not expired (10d) -> kept.
    c = _plant_snapshot(tenant_id, server_id, now - timedelta(days=10))

    with _sm() as db:
        purged = usage_cost.sweep_snapshot_retention(db)

    assert purged == 1  # only the usage row A
    assert not _snapshot_exists(a)
    assert _snapshot_exists(a_switch)  # switch_event retained regardless of age
    assert _snapshot_exists(b)  # unsettled is never deleted, however old
    assert _snapshot_exists(c)


# -- switch_event is retained even when expired AND settled -------------------
def test_retention_keeps_switch_events(app_env, monkeypatch):
    tenant_id, _account_id, server_id = _seed_tenant_account_server("uc-ret-sw@ex.com")
    now = datetime.now(UTC)
    # Boundary yesterday: a 100-day-old row is both expired and fully settled.
    _set_settlement_cursors(usage_cost._floor_day(now - timedelta(days=1)))
    _use_retention(monkeypatch, 30)

    sw = _plant_snapshot(
        tenant_id, server_id, now - timedelta(days=100), report_type="switch_event"
    )
    usg = _plant_snapshot(tenant_id, server_id, now - timedelta(days=100))

    with _sm() as db:
        purged = usage_cost.sweep_snapshot_retention(db)

    assert purged == 1  # the usage row only
    assert _snapshot_exists(sw)  # switch_event survives expiry + settlement
    assert not _snapshot_exists(usg)


# -- a future settlement watermark halts the purge entirely -------------------
def test_retention_halts_on_future_watermark(app_env, monkeypatch):
    tenant_id, _account_id, server_id = _seed_tenant_account_server("uc-ret-fut@ex.com")
    now = datetime.now(UTC)
    # G27 clock jump: watermark parked in the future. Rows below it are un-settled,
    # so nothing may be purged however old — even a 100-day-old usage row.
    _set_settlement_cursors(usage_cost._floor_day(now + timedelta(days=5)))
    _use_retention(monkeypatch, 30)

    old = _plant_snapshot(tenant_id, server_id, now - timedelta(days=100))
    with _sm() as db:
        assert usage_cost.sweep_snapshot_retention(db) == 0
    assert _snapshot_exists(old)


# -- no cursor (settlement boundary None) deletes nothing ---------------------
def test_retention_no_cursor_keeps_everything(app_env, monkeypatch):
    tenant_id, _account_id, server_id = _seed_tenant_account_server("uc-ret-nc@ex.com")
    now = datetime.now(UTC)
    # No BillingCursor rows planted -> boundary None -> nothing is settled yet.
    _use_retention(monkeypatch, 30)

    old = _plant_snapshot(tenant_id, server_id, now - timedelta(days=100))
    with _sm() as db:
        assert usage_cost.sweep_snapshot_retention(db) == 0
    assert _snapshot_exists(old)


# -- retention disabled (0) deletes nothing -----------------------------------
def test_retention_disabled_keeps_everything(app_env, monkeypatch):
    tenant_id, _account_id, server_id = _seed_tenant_account_server("uc-ret2@ex.com")
    now = datetime.now(UTC)
    _set_settlement_cursors(usage_cost._floor_day(now - timedelta(days=1)))
    _use_retention(monkeypatch, 0)

    old = _plant_snapshot(tenant_id, server_id, now - timedelta(days=365))
    with _sm() as db:
        assert usage_cost.sweep_snapshot_retention(db) == 0
    assert _snapshot_exists(old)
