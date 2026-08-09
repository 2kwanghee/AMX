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

from app.models import Alert, Server

_ACTIVE_STATUSES = ("open", "acked")


def _now() -> datetime:
    return datetime.now(UTC)


def dedupe_key(server_id, kind: str, account_id=None) -> str:
    base = f"{server_id}:{kind}"
    return f"{base}:{account_id}" if account_id is not None else base


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
    db.execute(stmt)


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
    db.execute(
        update(Alert).where(*where).values(status="resolved", resolved_at=_now())
    )


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
    db.execute(
        update(Alert).where(*where).values(status="resolved", resolved_at=_now())
    )


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
