"""Langfuse Metrics API roll-up — P4 console monitoring (server side).

A periodic sweep polls the external Langfuse Metrics API and compacts recent UTC
days into ``langfuse_usage_rollup`` so the console reads a local aggregate instead
of proxying every console request to Langfuse. It mirrors the F5/usage-cost sweeps'
shape — its own advisory-lock key (…07) lives in ``grpc/server.py`` — but the input
is an HTTP API rather than the local ledger, so there is no watermark/cursor:

* the roll-up re-aggregates a fixed sliding window of the most recent
  ``langfuse_metrics_window_days`` (default 3) days every tick. Each row is an
  idempotent upsert on ``(tenant_id, day, dimension, key)``, so re-rolling recent
  days (whose Langfuse totals are still settling) is a recompute-replace, never a
  duplicate.

Two axes per day, both from the ``observations`` view:

* ``dimension="model"`` — one query grouped by ``providedModelName`` (Langfuse's
  null model → ``key="unknown"``, kept rather than dropped).
* ``dimension="user"`` — ``userId`` is high-cardinality and cannot be a Metrics API
  dimension, only a filter, so the sweep loops the tenant's account emails and
  fixes each as a ``userId`` equality filter, summing that account's tokens.

Metrics API measures (verified against the live server): the observations view
exposes ``inputTokens`` / ``outputTokens`` / ``totalTokens`` and ``count``, but **no
cache-token measure** — so ``cache_read_tokens`` / ``cache_creation_tokens`` are
left at 0 (the schema keeps them for a future backfill). Cost measures exist too
but are out of scope here (token/observation volume only).

Activation is all-or-nothing: the sweep is a no-op unless base_url / public_key /
secret_key / tenant_id are all configured. HTTP failures are logged (never with the
secret) and abort the current tick without propagating, exactly like the sibling
sweeps' isolated error handling.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import try_advisory_xact_lock as _try_advisory_xact_lock
from app.models import Account, LangfuseUsageRollup

_logger = logging.getLogger("ams.langfuse")

# Sweep advisory-lock key, the seventh sibling after offline (…01), sent-ack (…02),
# billing (…03), rollup (…04), snapshot-retention (…05) and watermark-future (…06).
# Transaction-scoped: released on the final commit/rollback below.
_LANGFUSE_SWEEP_LOCK_KEY = 0x414D580F07

_METRICS_PATH = "/api/public/v2/metrics"

# observations-view measures the sweep pulls. Response keys are "<aggregation>_<measure>";
# count uses the "count" aggregation, so its key is "count_count".
_TOKEN_MEASURES = ("inputTokens", "outputTokens", "totalTokens")


def _now() -> datetime:
    # Indirected so tests can pin the sliding window.
    return datetime.now(UTC)


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
    """One Metrics API call. Raises httpx.HTTPError on transport or >=400 status."""
    response = client.get(
        settings.langfuse_base_url + _METRICS_PATH,
        params={"query": json.dumps(query)},
        headers=_auth_header(settings.langfuse_public_key, settings.langfuse_secret_key),
        timeout=settings.http_timeout_seconds,
    )
    response.raise_for_status()
    body = response.json()
    return body.get("data", []) if isinstance(body, dict) else []


def _base_query(from_ts: str, to_ts: str) -> dict:
    return {
        "view": "observations",
        "metrics": [{"measure": m, "aggregation": "sum"} for m in _TOKEN_MEASURES]
        + [{"measure": "count", "aggregation": "count"}],
        "fromTimestamp": from_ts,
        "toTimestamp": to_ts,
    }


def _row_values(tenant_id: uuid.UUID, day, dimension: str, key: str, src: dict) -> dict:
    return {
        "tenant_id": tenant_id,
        "day": day,
        "dimension": dimension,
        "key": key,
        "input_tokens": _int(src, "sum_inputTokens"),
        "output_tokens": _int(src, "sum_outputTokens"),
        # No cache-token measure on the Metrics API — left at 0 (see module docstring).
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "total_tokens": _int(src, "sum_totalTokens"),
        "observation_count": _int(src, "count_count"),
    }


def _has_activity(values: dict) -> bool:
    return bool(
        values["input_tokens"]
        or values["output_tokens"]
        or values["total_tokens"]
        or values["observation_count"]
    )


def _rollup_one_day(
    db: Session,
    client: httpx.Client,
    settings,
    tenant_id: uuid.UUID,
    day: datetime,
    emails: list[str],
) -> int:
    """Aggregate one UTC day (model + user axes) and stage the upserts. No commit."""
    from_ts = _iso(day)
    to_ts = _iso(day + timedelta(days=1))
    day_date = day.date()
    values: list[dict] = []

    # Model axis — one grouped query.
    model_query = _base_query(from_ts, to_ts)
    model_query["dimensions"] = [{"field": "providedModelName"}]
    for row in _query_metrics(client, settings, model_query):
        key = row.get("providedModelName") or "unknown"
        v = _row_values(tenant_id, day_date, "model", str(key), row)
        if _has_activity(v):
            values.append(v)

    # User axis — one filtered query per account email (userId is filter-only).
    for email in emails:
        user_query = _base_query(from_ts, to_ts)
        user_query["filters"] = [
            {"column": "userId", "operator": "=", "value": email, "type": "string"}
        ]
        data = _query_metrics(client, settings, user_query)
        agg = data[0] if data else {}
        v = _row_values(tenant_id, day_date, "user", email, agg)
        if _has_activity(v):
            values.append(v)

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


def sweep_langfuse_metrics(db: Session, *, client: httpx.Client | None = None) -> int:
    """Re-roll the recent Langfuse window into ``langfuse_usage_rollup``. Returns rows upserted.

    A no-op (returns 0) unless all four Langfuse settings are present. One
    transaction guarded by the …07 advisory lock: at most one instance runs the
    sweep per tick, and the lock releases on the final commit. An HTTP failure
    aborts the remaining days without propagating; the days already staged are
    still committed (idempotent, so a partial tick is re-rolled next tick).
    """
    settings = get_settings()
    if not settings.langfuse_enabled:
        return 0

    if not _try_advisory_xact_lock(db, _LANGFUSE_SWEEP_LOCK_KEY):
        return 0

    try:
        tenant_id = uuid.UUID(settings.langfuse_tenant_id)
    except (ValueError, TypeError):
        _logger.warning(
            "langfuse metrics sweep: AMX_LANGFUSE_TENANT_ID is not a valid UUID; skipping"
        )
        return 0

    window = max(1, settings.langfuse_metrics_window_days)
    today = _floor_day(_now())
    days = [today - timedelta(days=i) for i in range(window)]

    emails = sorted(
        set(db.scalars(select(Account.email).where(Account.tenant_id == tenant_id)))
    )

    owns_client = client is None
    client = client or httpx.Client(timeout=settings.http_timeout_seconds)
    upserted = 0
    try:
        for day in days:
            try:
                upserted += _rollup_one_day(db, client, settings, tenant_id, day, emails)
            except httpx.HTTPError as exc:
                # Bare class name: an httpx error string can echo the request URL,
                # and the request carries the Basic-auth secret key (§7). The day
                # that failed never reached its insert (values are staged only after
                # the whole day is fetched), so committing here persists exactly the
                # days that fully succeeded.
                _logger.warning(
                    "langfuse metrics sweep: HTTP error on %s (%s); aborting tick",
                    day.date(),
                    type(exc).__name__,
                )
                break
        db.commit()
    finally:
        if owns_client:
            client.close()
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


def ui_url() -> str | None:
    """Console deep-link base: explicit UI URL, else the API base, else null."""
    settings = get_settings()
    return settings.langfuse_ui_url or settings.langfuse_base_url
