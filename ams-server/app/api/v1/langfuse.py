"""Langfuse usage query endpoint — P4 console monitoring.

One read-only endpoint over the ``langfuse_usage_rollup`` table the metrics sweep
(``services.langfuse_metrics``) fills. The roll-up already splits each day into a
``model`` axis and a ``user`` axis; this handler regroups those rows into the two
wire lists the console renders and attaches the Langfuse deep-link base.

Tenant-scoped like the rest of ``/api/v1``: the router carries ``TenantScope``, so a
caller with no reach to the path's tenant gets 404. The sweep only ever writes the
configured ``AMX_LANGFUSE_TENANT_ID``'s rows, so any other tenant's query is an
empty 200 — a real "no monitoring data for this tenant" answer, not an error.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Query

from app import schemas
from app.api.deps import AdminPrincipal, DbSession, TenantScope
from app.services import langfuse_metrics

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["usage"], dependencies=[TenantScope])

# Default range when the caller omits from/to: the trailing week, inclusive.
_DEFAULT_WINDOW_DAYS = 7


@router.get("/usage/langfuse", response_model=schemas.LangfuseUsageResponse)
def get_langfuse_usage(
    tenant_id: uuid.UUID,
    db: DbSession,
    principal: AdminPrincipal,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: Annotated[date | None, Query()] = None,
):
    """Langfuse token roll-up for one tenant over an inclusive [from, to] UTC-day range.

    Both bounds default to the trailing week ending today (UTC). An empty range or
    a tenant with no monitoring data is an empty 200.
    """
    to_day = to or datetime.now(UTC).date()
    from_day = from_ or (to_day - timedelta(days=_DEFAULT_WINDOW_DAYS - 1))

    rows = langfuse_metrics.read_rollup(db, tenant_id, from_day, to_day)

    model_rows = [
        schemas.LangfuseModelRow(
            day=r.day,
            model=r.key,
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            cache_read_tokens=r.cache_read_tokens,
            cache_creation_tokens=r.cache_creation_tokens,
            total_tokens=r.total_tokens,
            observations=r.observation_count,
        )
        for r in rows
        if r.dimension == "model"
    ]
    user_rows = [
        schemas.LangfuseUserRow(
            day=r.day,
            user_id=r.key,
            total_tokens=r.total_tokens,
            observations=r.observation_count,
        )
        for r in rows
        if r.dimension == "user"
    ]

    return schemas.LangfuseUsageResponse(
        model_rows=model_rows,
        user_rows=user_rows,
        ui_url=langfuse_metrics.ui_url(),
    )
