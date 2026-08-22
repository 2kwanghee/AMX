"""대시보드 집계 통계 엔드포인트 — dashboard-redesign-plan.md 부록 A.

집계 로직 자체는 services/stats.py에 있고, 여기는 쿼리 파싱과 그 결과를 응답
스키마로 옮겨 담는 얇은 층이다. ``range``·``as_of``는 요청마다 한 번만 시각을
고정해(``now``) 서비스 호출과 응답 스탬프가 어긋나지 않게 한다.

쿼리 파라미터 이름은 부록 A 그대로 ``range``·``by``다. 파이썬 쪽 매개변수는
``range``가 내장 함수와 겹치므로(accounts.py의 ``status`` → ``status_filter``
관례와 같은 이유) ``range_``로 받고 ``Query(alias="range")``로 이름만 맞춘다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app import schemas
from app.api.deps import AdminPrincipal, DbSession, TenantScope
from app.services import stats

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["stats"], dependencies=[TenantScope])

RangeQuery = Annotated[schemas.StatsRange, Query(alias="range")]


def _now() -> datetime:
    return datetime.now(UTC)


@router.get("/stats/summary", response_model=schemas.StatsSummaryResponse)
def get_stats_summary(
    tenant_id: uuid.UUID,
    db: DbSession,
    principal: AdminPrincipal,
    range_: RangeQuery = "7d",
):
    now = _now()
    result = stats.summary(db, tenant_id, range_, now)
    return schemas.StatsSummaryResponse(
        range=range_,
        as_of=now,
        tokens=schemas.StatsValuePrev(value=result.tokens.value, prev=result.tokens.prev),
        cost=schemas.StatsCostValue(
            value=result.cost.value, currency=result.cost.currency, prev=result.cost.prev
        ),
        sessions=schemas.StatsValuePrev(value=result.sessions.value, prev=result.sessions.prev),
        alerts_opened=schemas.StatsValuePrev(
            value=result.alerts_opened.value, prev=result.alerts_opened.prev
        ),
        alerts_open_now=result.alerts_open_now,
        servers_online=result.servers_online,
        accounts_active=result.accounts_active,
        sparkline=schemas.StatsSparkline(
            tokens=result.sparkline.tokens, sessions=result.sparkline.sessions
        ),
    )


@router.get("/stats/timeseries", response_model=schemas.StatsTimeseriesResponse)
def get_stats_timeseries(
    tenant_id: uuid.UUID,
    db: DbSession,
    principal: AdminPrincipal,
    by: schemas.StatsTimeseriesBy,
    range_: RangeQuery = "7d",
):
    now = _now()
    result = stats.timeseries(db, tenant_id, range_, by, now)
    return schemas.StatsTimeseriesResponse(
        range=range_,
        as_of=now,
        unit=result.unit,
        buckets=result.buckets,
        series=[
            schemas.StatsSeries(key=s.key, label=s.label, values=s.values) for s in result.series
        ],
    )


@router.get("/stats/flows", response_model=schemas.StatsFlowsResponse)
def get_stats_flows(
    tenant_id: uuid.UUID,
    db: DbSession,
    principal: AdminPrincipal,
    range_: RangeQuery = "7d",
):
    now = _now()
    result = stats.flows(db, tenant_id, range_, now)
    return schemas.StatsFlowsResponse(
        range=range_,
        as_of=now,
        nodes=[schemas.StatsFlowNode(id=n.id, kind=n.kind, label=n.label) for n in result.nodes],
        links=[
            schemas.StatsFlowLink(source=link.source, target=link.target, value=link.value)
            for link in result.links
        ],
    )


@router.get("/stats/accounts", response_model=schemas.StatsAccountsResponse)
def get_stats_accounts(
    tenant_id: uuid.UUID,
    db: DbSession,
    principal: AdminPrincipal,
    range_: RangeQuery = "7d",
):
    now = _now()
    rows = stats.accounts(db, tenant_id, range_, now)
    return schemas.StatsAccountsResponse(
        range=range_,
        as_of=now,
        rows=[
            schemas.StatsAccountRow(
                account_id=r.account_id,
                email=r.email,
                provider=r.provider,
                tokens=r.tokens,
                sessions=r.sessions,
                messages=r.messages,
                top_model=r.top_model,
                top_server_id=r.top_server_id,
                top_server_name=r.top_server_name,
                top_project=r.top_project,
                held_seconds=r.held_seconds,
                remaining_5h_pct=r.remaining_5h_pct,
                remaining_7d_pct=r.remaining_7d_pct,
            )
            for r in rows
        ],
    )


@router.get("/stats/servers", response_model=schemas.StatsServersResponse)
def get_stats_servers(
    tenant_id: uuid.UUID,
    db: DbSession,
    principal: AdminPrincipal,
    range_: RangeQuery = "7d",
):
    now = _now()
    rows = stats.servers(db, tenant_id, range_, now)
    return schemas.StatsServersResponse(
        range=range_,
        as_of=now,
        rows=[
            schemas.StatsServerRow(
                server_id=r.server_id,
                name=r.name,
                status=r.status,
                held_seconds=r.held_seconds,
                tokens=r.tokens,
                sessions=r.sessions,
                messages=r.messages,
                top_model=r.top_model,
                top_account_id=r.top_account_id,
                top_account_email=r.top_account_email,
                cost=schemas.StatsServerCost(amount=r.cost_amount, currency=r.cost_currency),
            )
            for r in rows
        ],
    )


@router.get("/stats/heatmap", response_model=schemas.StatsHeatmapResponse)
def get_stats_heatmap(
    tenant_id: uuid.UUID,
    db: DbSession,
    principal: AdminPrincipal,
    range_: RangeQuery = "7d",
):
    now = _now()
    cells = stats.heatmap(db, tenant_id, range_, now)
    return schemas.StatsHeatmapResponse(range=range_, as_of=now, cells=cells)
