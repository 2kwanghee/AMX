"""Langfuse Metrics API roll-up — P4 console monitoring (server side).

A periodic sweep polls the external Langfuse Metrics API and compacts recent UTC
days into ``langfuse_usage_rollup`` so the console reads a local aggregate instead
of proxying every console request to Langfuse. It mirrors the F5/usage-cost sweeps'
shape — its own advisory-lock key (…07) — but the input is an HTTP API rather than
the local ledger, so there is no watermark/cursor:

* the roll-up re-aggregates a fixed sliding window of the most recent
  ``langfuse_metrics_window_days`` (default 3, floor 2) days. Each row is an
  idempotent upsert on ``(tenant_id, day, dimension, key)``, so re-rolling recent
  days (whose Langfuse totals are still settling) is a recompute-replace, never a
  duplicate. The floor of 2 is a correctness bound: a window of 1 rolls only today
  (always partial), so a day's finalised total would never be re-fetched after it
  closed — covering today+yesterday guarantees each day is re-rolled once closed.

Cadence and locking are two stages, deliberately decoupled:

* the sweep is driven by the shared 30s offline-sweeper tick, but runs its own work
  only every ``langfuse_poll_seconds`` (default 300, floor 60) — a process-local
  monotonic gate returns immediately on an under-cadence tick, so the external API
  is not hammered every 30s.
* all HTTP GETs run FIRST, outside any lock or transaction, collecting rows into
  memory; only then is the …07 advisory lock taken for a short upsert+commit. This
  keeps a slow/blocked Langfuse from holding a DB transaction or the cross-instance
  lock open. The lock still makes exactly one instance apply the write per cadence.

Two axes per day, both from the ``observations`` view:

* ``dimension="model"`` — one query grouped by ``providedModelName`` (Langfuse's
  null model → ``key="unknown"``, kept rather than dropped).
* ``dimension="user"`` — ``userId`` is high-cardinality and cannot be a Metrics API
  dimension, only a filter, so the sweep loops the tenant's account emails and
  fixes each as a ``userId`` equality filter, summing that account's tokens.

Token collection uses the ``usageByType`` measure crossed with the ``usageType``
dimension (verified against the live server), so each token class lands in its own
column — cache tokens included. ``usageType`` values map: ``input``→input_tokens,
``output``→output_tokens, ``cache_read_input_tokens``→cache_read_tokens,
``cache_creation_input_tokens``→cache_creation_tokens, ``total``→total_tokens; an
unknown value is ignored with a warning. Because ``usageType`` is a cross dimension,
each axis returns one row per (group × usageType) and the sweep re-assembles them
per group. ``count`` repeats identically across a group's usageType rows, so the
observation count is taken only from the ``total`` row — never summed across types.
Cost measures exist too but are out of scope here (token/observation volume only).

Activation is all-or-nothing: the sweep is a no-op unless base_url / public_key /
secret_key / tenant_id are all configured. HTTP failures are logged (never with the
secret) and abort the current tick without propagating, exactly like the sibling
sweeps' isolated error handling.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import try_advisory_xact_lock as _try_advisory_xact_lock
from app.models import Account, BillingCursor, LangfuseUsageRollup

_logger = logging.getLogger("ams.langfuse")

# Sweep advisory-lock key, the seventh sibling after offline (…01), sent-ack (…02),
# billing (…03), rollup (…04), snapshot-retention (…05) and watermark-future (…06).
# Transaction-scoped: released on the upsert commit below.
_LANGFUSE_SWEEP_LOCK_KEY = 0x414D580F07

_METRICS_PATH = "/api/public/v2/metrics"

# Metrics API row cap. The API defaults to config.row_limit=100 (max 1000) and has
# NO pagination, so every query MUST pin the max or the grouped model axis
# (providedModelName × usageType) is silently truncated. 1000 rows ÷ 5 usageType
# values ≈ 200 distinct models per day — ample headroom. A response whose length
# reaches this cap is logged as possibly-truncated (see _query_metrics); the rows
# received are still loaded normally.
_ROW_LIMIT = 1000

# billing_cursors 관례를 재사용한 "마지막 정상 스윕" 마커의 kind. langfuse_stale 경보가
# 롤업 max(updated_at)(무활동 주말이면 오발) 대신 이 마커의 신선도를 판정 소스로 쓴다.
_METRICS_SYNC_CURSOR_KIND = "langfuse_metrics_sync"

# Process-local cadence gate: the monotonic time of the last run that passed the
# gate, or None before the first. Shared across instances is unnecessary — the
# advisory lock already coordinates the write; this only throttles one process.
_LAST_POLL_MONOTONIC: float | None = None

# Process-local set of unknown usageType values already warned about. The unknown
# warning below fires once per value per process — same convention as the cadence
# gate — so a new permanent usageType doesn't spam the log every group×day×poll.
_WARNED_UNKNOWN_USAGE_TYPES: set = set()

# observations-view measures the sweep pulls: usageByType (summed) crossed with the
# usageType dimension, plus count. Response keys are "<aggregation>_<measure>", so
# the token sum is "sum_usageByType" and the observation count is "count_count".
_MEASURES = ("usageByType",)

# Langfuse usageType dimension value -> roll-up token column. An unseen value is
# ignored with a warning (see _assemble); "total" also carries observation_count.
_USAGE_TYPE_COLUMNS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read_input_tokens": "cache_read_tokens",
    "cache_creation_input_tokens": "cache_creation_tokens",
    "total": "total_tokens",
}


def _now() -> datetime:
    # Indirected so tests can pin the sliding window.
    return datetime.now(UTC)


def _monotonic() -> float:
    # Indirected so tests can drive the cadence gate.
    return time.monotonic()


def _floor_day(dt: datetime) -> datetime:
    dt = dt.astimezone(UTC)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _auth_header(public_key: str, secret_key: str) -> dict[str, str]:
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _int(row: dict, key: str) -> int:
    value = row.get(key)
    if value is None:
        return 0
    return int(round(float(value)))


def _query_metrics(client: httpx.Client, settings, query: dict) -> list[dict]:
    """One Metrics API call. Raises httpx.HTTPError on transport or >=400 status.

    Pins ``config.row_limit`` to the API max on every query (single choke point for
    the model, user and latency axes) because the Metrics API defaults to 100 rows
    and has no pagination. If the response length reaches the cap the result may be
    truncated — logged as a warning — but the received rows are returned as usual.
    """
    query = {**query, "config": {"row_limit": _ROW_LIMIT}}
    response = client.get(
        settings.langfuse_base_url + _METRICS_PATH,
        params={"query": json.dumps(query)},
        headers=_auth_header(settings.langfuse_public_key, settings.langfuse_secret_key),
        timeout=settings.http_timeout_seconds,
    )
    response.raise_for_status()
    body = response.json()
    data = body.get("data", []) if isinstance(body, dict) else []
    if len(data) >= _ROW_LIMIT:
        _logger.warning(
            "langfuse metrics: 응답 행 수가 row_limit(%d)에 도달 (view=%s dims=%s) — "
            "결과가 잘렸을 수 있음; 받은 행은 정상 적재",
            _ROW_LIMIT,
            query.get("view"),
            [d.get("field") for d in query.get("dimensions", [])],
        )
    return data


def _base_query(from_ts: str, to_ts: str) -> dict:
    return {
        "view": "observations",
        "metrics": [{"measure": m, "aggregation": "sum"} for m in _MEASURES]
        + [{"measure": "count", "aggregation": "count"}],
        "fromTimestamp": from_ts,
        "toTimestamp": to_ts,
    }


def _assemble(
    tenant_id: uuid.UUID, day, dimension: str, key: str, rows: list[dict]
) -> dict:
    """Fold a group's (usageType-crossed) rows into one roll-up value dict.

    Each ``usageType`` row contributes its ``sum_usageByType`` to the mapped token
    column; the ``total`` row alone supplies ``observation_count`` (``count_count``
    repeats across a group's usageType rows, so summing it would double-count).
    Unknown ``usageType`` values are ignored with a warning.
    """
    values = {
        "tenant_id": tenant_id,
        "day": day,
        "dimension": dimension,
        "key": key,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "total_tokens": 0,
        "observation_count": 0,
    }
    for row in rows:
        usage_type = row.get("usageType")
        column = _USAGE_TYPE_COLUMNS.get(usage_type)
        if column is None:
            if usage_type not in _WARNED_UNKNOWN_USAGE_TYPES:
                _WARNED_UNKNOWN_USAGE_TYPES.add(usage_type)
                _logger.warning(
                    "langfuse metrics sweep: unknown usageType %r (%s/%s); ignoring "
                    "(이후 동일 값 경고는 억제됨)",
                    usage_type,
                    dimension,
                    key,
                )
            continue
        values[column] += _int(row, "sum_usageByType")
        if usage_type == "total":
            values["observation_count"] = _int(row, "count_count")
    return values


def _has_activity(values: dict) -> bool:
    return bool(
        values["input_tokens"]
        or values["output_tokens"]
        or values["cache_read_tokens"]
        or values["cache_creation_tokens"]
        or values["total_tokens"]
        or values["observation_count"]
    )


def _fetch_one_day(
    client: httpx.Client,
    settings,
    tenant_id: uuid.UUID,
    day: datetime,
    emails: list[str],
) -> list[dict]:
    """Fetch one UTC day (model + user axes) from the Metrics API into row dicts.

    Pure I/O — no DB. Raises ``httpx.HTTPError`` on transport/status failure or
    ``ValueError`` (incl. ``json.JSONDecodeError``) on an unparseable body.
    """
    from_ts = _iso(day)
    to_ts = _iso(day + timedelta(days=1))
    day_date = day.date()
    values: list[dict] = []

    # Model axis — one grouped query, crossed with usageType. Rows arrive as
    # (providedModelName × usageType); re-group per model preserving first-seen order.
    model_query = _base_query(from_ts, to_ts)
    model_query["dimensions"] = [{"field": "providedModelName"}, {"field": "usageType"}]
    groups: dict[str, list[dict]] = {}
    for row in _query_metrics(client, settings, model_query):
        key = str(row.get("providedModelName") or "unknown")
        groups.setdefault(key, []).append(row)
    for key, rows in groups.items():
        v = _assemble(tenant_id, day_date, "model", key, rows)
        if _has_activity(v):
            values.append(v)

    # User axis — one filtered query per account email (userId is filter-only),
    # grouped by usageType so cache tokens land per account too.
    for email in emails:
        user_query = _base_query(from_ts, to_ts)
        user_query["filters"] = [
            {"column": "userId", "operator": "=", "value": email, "type": "string"}
        ]
        user_query["dimensions"] = [{"field": "usageType"}]
        data = _query_metrics(client, settings, user_query)
        v = _assemble(tenant_id, day_date, "user", email, data)
        if _has_activity(v):
            values.append(v)

    return values


def _upsert(db: Session, values: list[dict]) -> int:
    """Idempotent recompute-replace of the collected rows. Caller owns the txn."""
    if not values:
        return 0
    now = _now()
    stmt = pg_insert(LangfuseUsageRollup).values(
        [{**v, "updated_at": now} for v in values]
    )
    db.execute(
        stmt.on_conflict_do_update(
            constraint="pk_langfuse_usage_rollup",
            set_={
                "input_tokens": stmt.excluded.input_tokens,
                "output_tokens": stmt.excluded.output_tokens,
                "cache_read_tokens": stmt.excluded.cache_read_tokens,
                "cache_creation_tokens": stmt.excluded.cache_creation_tokens,
                "total_tokens": stmt.excluded.total_tokens,
                "observation_count": stmt.excluded.observation_count,
                "updated_at": stmt.excluded.updated_at,
            },
        )
    )
    return len(values)


def _mark_metrics_sync(db: Session, when: datetime) -> None:
    """"마지막 정상 스윕" 마커를 ``when``으로 상향한다. caller가 커밋한다.

    billing_cursors(kind PK)에 대한 idempotent upsert. 되감지 않고 갱신만 한다.
    """
    stmt = pg_insert(BillingCursor).values(
        kind=_METRICS_SYNC_CURSOR_KIND, watermark=when, updated_at=when
    )
    db.execute(
        stmt.on_conflict_do_update(
            index_elements=["kind"],
            set_={"watermark": stmt.excluded.watermark, "updated_at": stmt.excluded.updated_at},
        )
    )


def last_metrics_sync_at(db: Session) -> datetime | None:
    """마지막 정상 스윕 시각. 한 번도 성공한 적 없으면 ``None``(langfuse_stale 판정 소스)."""
    cursor = db.get(BillingCursor, _METRICS_SYNC_CURSOR_KIND)
    return cursor.watermark if cursor is not None else None


def sweep_langfuse_metrics(db: Session, *, client: httpx.Client | None = None) -> int:
    """Re-roll the recent Langfuse window into ``langfuse_usage_rollup``. Returns rows upserted.

    A no-op (returns 0) unless all four Langfuse settings are present and the
    process-local cadence gate (``langfuse_poll_seconds``) has elapsed. Two stages:
    every HTTP GET runs first, outside any lock/transaction, into memory; then the
    …07 advisory lock guards a short upsert+commit (one instance applies the write
    per cadence). A fetch failure (HTTP or bad JSON) aborts the remaining days
    without propagating; the days already fetched are still upserted (idempotent, so
    a partial tick is re-rolled next cadence).
    """
    global _LAST_POLL_MONOTONIC

    settings = get_settings()
    if not settings.langfuse_enabled:
        return 0

    # Cadence gate (process-local): skip a tick that arrives sooner than the poll
    # interval since the last run, so the shared 30s sweeper does not re-poll every
    # tick. Recorded on entry (when due), independent of the run's outcome.
    poll_seconds = max(60, settings.langfuse_poll_seconds)
    now_m = _monotonic()
    if _LAST_POLL_MONOTONIC is not None and now_m - _LAST_POLL_MONOTONIC < poll_seconds:
        return 0
    _LAST_POLL_MONOTONIC = now_m

    try:
        tenant_id = uuid.UUID(settings.langfuse_tenant_id)
    except (ValueError, TypeError):
        _logger.warning(
            "langfuse metrics sweep: AMX_LANGFUSE_TENANT_ID is not a valid UUID; skipping"
        )
        return 0

    # Floor of 2 (see module docstring): a window of 1 never re-rolls a closed day.
    window = max(2, settings.langfuse_metrics_window_days)
    today = _floor_day(_now())
    days = [today - timedelta(days=i) for i in range(window)]

    emails = sorted(
        set(db.scalars(select(Account.email).where(Account.tenant_id == tenant_id)))
    )
    cap = settings.langfuse_max_accounts
    if len(emails) > cap:
        _logger.warning(
            "langfuse metrics sweep: tenant has %d accounts, exceeding the cap of "
            "%d; rolling only the first %d (sorted)",
            len(emails),
            cap,
            cap,
        )
        emails = emails[:cap]

    # Stage 1 — all HTTP, no lock, no transaction. Collect into memory.
    owns_client = client is None
    client = client or httpx.Client(timeout=settings.http_timeout_seconds)
    collected: list[dict] = []
    had_error = False
    try:
        for day in days:
            try:
                collected.extend(_fetch_one_day(client, settings, tenant_id, day, emails))
            except (httpx.HTTPError, ValueError) as exc:
                # Bare class name: an httpx error string can echo the request URL,
                # and the request carries the Basic-auth secret key (§7). The days
                # already fetched are kept; the rest of this cadence is skipped.
                had_error = True
                _logger.warning(
                    "langfuse metrics sweep: fetch error on %s (%s); aborting tick",
                    day.date(),
                    type(exc).__name__,
                )
                break
    finally:
        if owns_client:
            client.close()

    # Nothing to write and the round-trip errored → skip entirely (the sync marker
    # must NOT advance on a failed tick; that is exactly what langfuse_stale watches).
    if had_error and not collected:
        return 0

    # Stage 2 — short lock + upsert + (on a clean round-trip) sync-marker + commit.
    if not _try_advisory_xact_lock(db, _LANGFUSE_SWEEP_LOCK_KEY):
        return 0
    upserted = _upsert(db, collected)
    if not had_error:
        # Freshness marker for langfuse_stale: advanced on every clean sweep,
        # activity or not — an idle weekend keeps the pipeline "fresh", a stalled
        # pipeline lets it age. A partial (error-truncated) tick does not advance it.
        _mark_metrics_sync(db, _now())
    db.commit()
    return upserted


def read_rollup(
    db: Session, tenant_id: uuid.UUID, from_day, to_day
) -> list[LangfuseUsageRollup]:
    """Roll-up rows for one tenant over an inclusive [from_day, to_day] UTC-day range.

    Ordered (day, dimension, key) for a stable wire response. A tenant other than
    the configured Langfuse tenant matches nothing — the sweep only ever writes the
    configured tenant's id — so a cross-tenant caller gets an empty result.
    """
    return list(
        db.scalars(
            select(LangfuseUsageRollup)
            .where(
                LangfuseUsageRollup.tenant_id == tenant_id,
                LangfuseUsageRollup.day >= from_day,
                LangfuseUsageRollup.day <= to_day,
            )
            .order_by(
                LangfuseUsageRollup.day,
                LangfuseUsageRollup.dimension,
                LangfuseUsageRollup.key,
            )
        )
    )


def last_synced_at(db: Session, tenant_id: uuid.UUID) -> datetime | None:
    """Freshness signal: the newest ``updated_at`` across this tenant's roll-up rows.

    ``None`` when the sweep has never written a row for the tenant — the console
    reads it as "not yet synced" rather than a hard error.
    """
    return db.scalar(
        select(func.max(LangfuseUsageRollup.updated_at)).where(
            LangfuseUsageRollup.tenant_id == tenant_id
        )
    )


def ui_url() -> str | None:
    """Console deep-link base: explicit UI URL, else the API base, else null."""
    settings = get_settings()
    return settings.langfuse_ui_url or settings.langfuse_base_url
