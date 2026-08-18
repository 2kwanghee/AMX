"""Alert lifecycle — open (dedup), auto-resolve, ack, and the offline sweeper.

Design note §4 / decision 3. Every writer here **stages** rows on the caller's
session and lets the caller commit, so an alert opened off a usage report lands
in the SAME transaction as the snapshot insert and the reconcile corrections
(atomic: either the report, its drift marks, its corrections and its alerts all
commit, or none do).

Concurrency (R3): the gRPC process runs many sessions across a thread pool, so
two reports for the same server can open the same alert at once. Opening goes
through a single PostgreSQL ``INSERT ... ON CONFLICT`` against the partial unique
index ``uq_alerts_open_dedupe`` (``WHERE status = 'open'``); the database, not
application code, guarantees at most one open alert per ``dedupe_key``.

``dedupe_key`` is derived, never client-supplied:
    server-scoped  (all_exhausted, server_offline)                        -> ``{server_id}:{kind}``
    account-scoped (drift, quarantine, recall_failed, command_send_failed) -> ``{server_id}:{kind}:{account_id}``
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Alert, AlertWebhookOutbox, Server

_ACTIVE_STATUSES = ("open", "acked")

# G27. The usage-rollup watermark is a single global cursor, but a forward
# wall-clock step that parks it in the future silently strands every tenant's
# below-watermark snapshots as unbilled, so the alert is raised per tenant with a
# tenant-scoped dedupe key and a NULL server_id (not a per-server condition).
WATERMARK_FUTURE_KIND = "billing_watermark_future"


def _now() -> datetime:
    return datetime.now(UTC)


def dedupe_key(server_id, kind: str, account_id=None) -> str:
    base = f"{server_id}:{kind}"
    return f"{base}:{account_id}" if account_id is not None else base


def _stage_webhook(
    db: Session,
    *,
    alert_id: uuid.UUID,
    tenant_id: uuid.UUID,
    server_id: uuid.UUID | None,
    kind: str,
    status: str,
    detail: dict | None,
) -> None:
    """P5: 경보 open/resolve 전이 한 건을 웹훅 아웃박스에 스테이징한다(G41).

    caller의 세션에 ``add``만 하고 커밋은 하지 않는다 — 경보를 여닫는 것과 **같은
    트랜잭션**에서 함께 커밋/롤백되므로, 커밋되지 않은 경보의 유령 웹훅이 나가지
    않는다. 웹훅이 비활성(URL/시크릿 미설정)이면 아무 것도 하지 않아 완전 무부작용이다.
    발송 시점의 alerts 테이블 상태와 무관하도록 전이 스냅샷을 행에 그대로 담는다.
    """
    if not get_settings().alert_webhook_enabled:
        return
    db.add(
        AlertWebhookOutbox(
            alert_id=alert_id,
            tenant_id=tenant_id,
            server_id=server_id,
            kind=kind,
            status=status,
            detail=detail,
            occurred_at=_now(),
        )
    )


# RETURNING에 실을 전이 스냅샷 컬럼 — resolve 계열이 실제로 닫은 행만 골라 스테이징한다.
_RESOLVE_RETURNING = (Alert.id, Alert.tenant_id, Alert.server_id, Alert.kind, Alert.detail)


def _stage_resolved(db: Session, rows) -> None:
    for row in rows:
        _stage_webhook(
            db,
            alert_id=row.id,
            tenant_id=row.tenant_id,
            server_id=row.server_id,
            kind=row.kind,
            status="resolved",
            detail=row.detail,
        )


def open_alert(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    server_id: uuid.UUID,
    kind: str,
    severity: str,
    account_id: uuid.UUID | None = None,
    detail: dict | None = None,
    source_snapshot_id: uuid.UUID | None = None,
) -> None:
    """Open (or refresh) one alert, idempotent by ``dedupe_key``.

    * An already-open alert is refreshed with the latest detail/snapshot via the
      ON CONFLICT arbiter — no duplicate, no flood.
    * An **acked** alert of the same key is left acked and only refreshed: an
      operator who acknowledged a persistent condition is not re-alarmed every
      5 minutes. It reverts to a fresh open only after auto-resolve closes it and
      the condition later recurs.
    """
    key = dedupe_key(server_id, kind, account_id)

    acked = db.scalar(
        select(Alert).where(Alert.dedupe_key == key, Alert.status == "acked")
    )
    if acked is not None:
        acked.detail = detail
        if source_snapshot_id is not None:
            acked.source_snapshot_id = source_snapshot_id
        return

    stmt = pg_insert(Alert).values(
        tenant_id=tenant_id,
        server_id=server_id,
        account_id=account_id,
        kind=kind,
        severity=severity,
        status="open",
        dedupe_key=key,
        detail=detail,
        source_snapshot_id=source_snapshot_id,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["dedupe_key"],
        index_where=text("status = 'open'"),
        set_={
            "detail": stmt.excluded.detail,
            "source_snapshot_id": stmt.excluded.source_snapshot_id,
        },
    )
    # RETURNING의 (xmax = 0)은 이 문장이 실제 INSERT였는지(신규 open 전이) ON CONFLICT
    # UPDATE였는지(이미 열린 경보의 refresh) 구분한다 — DB가 결정하므로 동시 두 리포트가
    # 같은 키를 열어도 웹훅은 정확히 한 번만 스테이징된다.
    row = db.execute(stmt.returning(Alert.id, text("xmax = 0"))).first()
    if row is not None and row[1]:
        _stage_webhook(
            db,
            alert_id=row[0],
            tenant_id=tenant_id,
            server_id=server_id,
            kind=kind,
            status="open",
            detail=detail,
        )


def resolve(
    db: Session,
    *,
    server_id: uuid.UUID,
    kind: str,
    account_id: uuid.UUID | None = None,
) -> None:
    """Resolve every active (open/acked) alert matching the key. Idempotent."""
    where = [
        Alert.server_id == server_id,
        Alert.kind == kind,
        Alert.status.in_(_ACTIVE_STATUSES),
    ]
    if account_id is not None:
        where.append(Alert.account_id == account_id)
    rows = db.execute(
        update(Alert)
        .where(*where)
        .values(status="resolved", resolved_at=_now())
        .returning(*_RESOLVE_RETURNING)
    ).all()
    _stage_resolved(db, rows)


def resolve_account_alerts(
    db: Session, *, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """Resolve every active alert naming one account, on every server it touched.

    ``alerts.account_id`` carries no foreign key — only ``(server_id, tenant_id)``
    does — so nothing cascades when the account row goes away, and an
    account-scoped alert left open then survives forever: the condition it
    describes can no longer recur, so no auto-resolve path (report reconcile,
    heartbeat, a stored cred_update) will ever close it. Called from
    ``inventory.delete_account`` inside the delete's own transaction, so the
    alerts close exactly when the account does or not at all.

    Keyed on the account rather than on ``(server_id, kind)`` like ``resolve``:
    the caller knows only the account, and a deleted account's alerts must go
    regardless of which server or kind opened them. Tenant-scoped, so it can
    never reach across tenants even if two rows shared an id.
    """
    rows = db.execute(
        update(Alert)
        .where(
            Alert.tenant_id == tenant_id,
            Alert.account_id == account_id,
            Alert.status.in_(_ACTIVE_STATUSES),
        )
        .values(status="resolved", resolved_at=_now())
        .returning(*_RESOLVE_RETURNING)
    ).all()
    _stage_resolved(db, rows)


def _resolve_drift_except(
    db: Session, *, server_id: uuid.UUID, keep_account_ids: set[str]
) -> None:
    """Resolve drift alerts for accounts that are no longer drifting on this
    report (auto-resolve). Accounts still in drift keep their open alert."""
    where = [
        Alert.server_id == server_id,
        Alert.kind == "drift",
        Alert.status.in_(_ACTIVE_STATUSES),
    ]
    if keep_account_ids:
        where.append(Alert.account_id.notin_([uuid.UUID(a) for a in keep_account_ids]))
    rows = db.execute(
        update(Alert)
        .where(*where)
        .values(status="resolved", resolved_at=_now())
        .returning(*_RESOLVE_RETURNING)
    ).all()
    _stage_resolved(db, rows)


def sync_from_report(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    server_id: uuid.UUID,
    all_exhausted: bool,
    drift_entries: list[dict],
    source_snapshot_id: uuid.UUID | None = None,
) -> None:
    """Reconcile alerts against one usage report (design note §4 auto-resolve).

    all_exhausted true opens (or refreshes) the server-scoped critical alert;
    false resolves it. Each drift entry opens an account-scoped warning; drift
    that has cleared for an account resolves that account's alert. Runs inside
    the report's transaction — the caller commits.
    """
    if all_exhausted:
        open_alert(
            db,
            tenant_id=tenant_id,
            server_id=server_id,
            kind="all_exhausted",
            severity="critical",
            detail={"source": "usage_report"},
            source_snapshot_id=source_snapshot_id,
        )
    else:
        resolve(db, server_id=server_id, kind="all_exhausted")

    drifting: set[str] = set()
    for entry in drift_entries:
        account_id = entry.get("account_id")
        if account_id is None:
            continue
        drifting.add(str(account_id))
        open_alert(
            db,
            tenant_id=tenant_id,
            server_id=server_id,
            account_id=uuid.UUID(str(account_id)),
            kind="drift",
            severity="warning",
            detail=entry,
            source_snapshot_id=source_snapshot_id,
        )
    _resolve_drift_except(db, server_id=server_id, keep_account_ids=drifting)


def sweep_offline(db: Session, *, stale_after_seconds: float) -> list[uuid.UUID]:
    """Mark servers whose heartbeat has lapsed offline and open an alert.

    The gRPC stream can stay half-open (design note §8): the session never ends,
    ``_mark_offline`` never fires, and ``last_seen_at`` silently ages while the
    server row is stuck ``online``. This sweeper closes that gap — any server
    last seen more than ``stale_after_seconds`` (3x heartbeat) ago is forced
    offline and gets a ``server_offline`` alert. The caller commits.

    Returns the ids swept (for logging/tests).
    """
    cutoff = _now() - timedelta(seconds=stale_after_seconds)
    stale = list(
        db.scalars(
            select(Server).where(
                Server.status != "offline",
                Server.last_seen_at.is_not(None),
                Server.last_seen_at < cutoff,
            )
        ).all()
    )
    for server in stale:
        server.status = "offline"
        server.updated_at = _now()
        open_alert(
            db,
            tenant_id=server.tenant_id,
            server_id=server.id,
            kind="server_offline",
            severity="warning",
            detail={"reason": "last_seen_at stale", "last_seen_at": str(server.last_seen_at)},
        )
    db.commit()
    return [s.id for s in stale]


def _watermark_dedupe_key(tenant_id) -> str:
    return f"{tenant_id}:{WATERMARK_FUTURE_KIND}"


def _open_watermark_future(db: Session, *, tenant_id: uuid.UUID, detail: dict) -> None:
    """Open/refresh one tenant's watermark-future alert, idempotent by key.

    Mirrors ``open_alert`` (acked-aware, ``INSERT ... ON CONFLICT`` against the
    partial unique index) but for a system-scoped condition: ``server_id`` is
    NULL and the key is tenant-scoped rather than derived from a server.
    """
    key = _watermark_dedupe_key(tenant_id)
    acked = db.scalar(
        select(Alert).where(Alert.dedupe_key == key, Alert.status == "acked")
    )
    if acked is not None:
        acked.detail = detail
        return
    stmt = pg_insert(Alert).values(
        tenant_id=tenant_id,
        server_id=None,
        account_id=None,
        kind=WATERMARK_FUTURE_KIND,
        severity="warning",
        status="open",
        dedupe_key=key,
        detail=detail,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["dedupe_key"],
        index_where=text("status = 'open'"),
        set_={"detail": stmt.excluded.detail},
    )
    row = db.execute(stmt.returning(Alert.id, text("xmax = 0"))).first()
    if row is not None and row[1]:
        _stage_webhook(
            db,
            alert_id=row[0],
            tenant_id=tenant_id,
            server_id=None,
            kind=WATERMARK_FUTURE_KIND,
            status="open",
            detail=detail,
        )


def sync_watermark_future(
    db: Session,
    *,
    watermark: datetime | None,
    now: datetime,
    skew_seconds: float,
    tenant_ids,
) -> bool:
    """G27: raise/resolve ``billing_watermark_future`` from the rollup watermark.

    A forward wall-clock step (VM resume / NTP step) can advance the usage-rollup
    watermark past real time; usage snapshots that then arrive below it are never
    rolled up and go silently unbilled. While the watermark sits more than
    ``skew_seconds`` ahead of ``now`` (skew tolerance guards benign clock jitter),
    open a per-tenant warning; once it is back at/below now, auto-resolve (same
    pattern as ``sync_from_report``). This only reads the watermark — it never
    rewinds or mutates it. The caller commits.

    Returns True while the future condition holds.
    """
    future = watermark is not None and watermark > now + timedelta(seconds=skew_seconds)
    detail = (
        {
            "watermark": watermark.isoformat(),
            "now": now.isoformat(),
            "skew_seconds": skew_seconds,
        }
        if future
        else None
    )
    for tenant_id in tenant_ids:
        if future:
            _open_watermark_future(db, tenant_id=tenant_id, detail=detail)
        else:
            key = _watermark_dedupe_key(tenant_id)
            rows = db.execute(
                update(Alert)
                .where(Alert.dedupe_key == key, Alert.status.in_(_ACTIVE_STATUSES))
                .values(status="resolved", resolved_at=_now())
                .returning(*_RESOLVE_RETURNING)
            ).all()
            _stage_resolved(db, rows)
    return future


def _system_dedupe_key(tenant_id, kind: str) -> str:
    return f"{tenant_id}:{kind}"


def open_system_alert(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    kind: str,
    severity: str,
    detail: dict | None = None,
    stage_webhook: bool = True,
) -> None:
    """Open/refresh a system-scoped alert (``server_id`` NULL, tenant-scoped key).

    P5 Langfuse 임계값 경보(usage_spike/stale/latency)처럼 특정 서버가 아니라 테넌트
    전체를 대상으로 하는 경보용. ``_open_watermark_future``와 같은 구조(acked-aware,
    부분 유니크 인덱스에 대한 ``INSERT ... ON CONFLICT``)이며, 신규 open 전이면 웹훅
    아웃박스에 스테이징한다. caller가 커밋한다.

    ``stage_webhook=False``는 웹훅 발송 실패로 폐기될 때 여는 ``alert_webhook_dropped``
    셀프 경보 전용이다 — 그 경보까지 웹훅 아웃박스에 실으면 발송 실패→셀프 경보→발송
    실패의 무한 재귀가 되므로 스테이징을 끊는다.
    """
    key = _system_dedupe_key(tenant_id, kind)
    acked = db.scalar(
        select(Alert).where(Alert.dedupe_key == key, Alert.status == "acked")
    )
    if acked is not None:
        acked.detail = detail
        return
    stmt = pg_insert(Alert).values(
        tenant_id=tenant_id,
        server_id=None,
        account_id=None,
        kind=kind,
        severity=severity,
        status="open",
        dedupe_key=key,
        detail=detail,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["dedupe_key"],
        index_where=text("status = 'open'"),
        set_={"detail": stmt.excluded.detail},
    )
    row = db.execute(stmt.returning(Alert.id, text("xmax = 0"))).first()
    if stage_webhook and row is not None and row[1]:
        _stage_webhook(
            db,
            alert_id=row[0],
            tenant_id=tenant_id,
            server_id=None,
            kind=kind,
            status="open",
            detail=detail,
        )


def open_event_alert(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    kind: str,
    severity: str,
    dedupe_key: str,
    detail: dict | None = None,
) -> None:
    """Open/refresh a system-scoped **event** alert keyed by an explicit dedupe_key.

    ``open_system_alert``와 같은 구조(acked-aware, 부분 유니크 인덱스에 대한
    ``INSERT ... ON CONFLICT``, 신규 open 전이만 웹훅 스테이징)이지만 dedupe 키를
    ``{tenant_id}:{kind}`` 로 고정하지 않고 caller가 그대로 넘긴다 — P5 위험명령 경보
    처럼 한 kind 안에서 (host, pattern, 명령 해시)별로 여러 경보가 공존해야 하는
    이벤트성 경보용이다. auto-resolve 짝은 없다(관리자가 ack/resolve). caller가 커밋.
    """
    acked = db.scalar(
        select(Alert).where(Alert.dedupe_key == dedupe_key, Alert.status == "acked")
    )
    if acked is not None:
        acked.detail = detail
        return
    stmt = pg_insert(Alert).values(
        tenant_id=tenant_id,
        server_id=None,
        account_id=None,
        kind=kind,
        severity=severity,
        status="open",
        dedupe_key=dedupe_key,
        detail=detail,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["dedupe_key"],
        index_where=text("status = 'open'"),
        set_={"detail": stmt.excluded.detail},
    )
    row = db.execute(stmt.returning(Alert.id, text("xmax = 0"))).first()
    if row is not None and row[1]:
        _stage_webhook(
            db,
            alert_id=row[0],
            tenant_id=tenant_id,
            server_id=None,
            kind=kind,
            status="open",
            detail=detail,
        )


def resolve_system_alert(db: Session, *, tenant_id: uuid.UUID, kind: str) -> None:
    """Resolve a system-scoped alert by its tenant-scoped key. Idempotent.

    ``resolve`` is server-scoped (``server_id == NULL`` never matches), so
    system-scoped kinds resolve by ``dedupe_key`` instead. Each closed row stages
    a ``resolved`` webhook event.
    """
    key = _system_dedupe_key(tenant_id, kind)
    rows = db.execute(
        update(Alert)
        .where(Alert.dedupe_key == key, Alert.status.in_(_ACTIVE_STATUSES))
        .values(status="resolved", resolved_at=_now())
        .returning(*_RESOLVE_RETURNING)
    ).all()
    _stage_resolved(db, rows)
