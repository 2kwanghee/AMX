"""Usage-cost / session-usage query endpoints.

One read-only endpoint over ``services.usage_cost.compute_month_cost``. The
service answers account-first (each account's price spread across the servers
that used it); the console reads per-server, so the regrouping to server-first
happens here, in the serialisation layer, and the allocation itself is left
untouched.

Tenant-scoped like the rest of ``/api/v1``: the router carries ``TenantScope``,
so a caller with no reach to the path's tenant gets 404 rather than 403.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Query

from app import schemas
from app.api.deps import AdminPrincipal, DbSession, TenantScope
from app.services import session_usage as session_usage_svc, usage_cost

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["usage"], dependencies=[TenantScope])

# YYYY-MM. The month alternation rejects 00 and 13 in the same pass as the
# shape, so a bad value is a 422 from FastAPI's own validation rather than
# something the handler has to check. The year is a 4-digit 1000–9999 (leading
# digit 1-9), which stays inside datetime.date's valid range — no year-0 value
# reaches the service to raise there.
_MONTH_PATTERN = r"^[1-9][0-9]{3}-(0[1-9]|1[0-2])$"
MonthQuery = Annotated[str | None, Query(pattern=_MONTH_PATTERN)]

_PCT_Q = Decimal("0.01")

# 세션 실측 조회의 기본 창(일)과 행 상한. 세션은 하루 수십 건 수준이라 최근 7일이
# 기본이고, 상한은 응답 크기를 묶기 위한 것이다(초과분은 최근순으로 잘린다).
_SESSION_DEFAULT_DAYS = 7
_SESSION_MAX_ROWS = 500


def _pct(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator <= 0:
        return Decimal("0.00")
    return (numerator / denominator).quantize(_PCT_Q)


@router.get("/usage/cost", response_model=schemas.UsageCostResponse)
def get_usage_cost(
    tenant_id: uuid.UUID,
    db: DbSession,
    principal: AdminPrincipal,
    month: MonthQuery = None,
):
    """Per-server cost allocation for one month. Defaults to the current UTC month.

    A month with no usage — including any month in the future — is an empty
    200, not a 404: "nothing was used" is a real answer.
    """
    if month is None:
        today = datetime.now(UTC).date()
        year, month_no = today.year, today.month
        month = f"{year:04d}-{month_no:02d}"
    else:
        year, month_no = int(month[:4]), int(month[5:])

    result = usage_cost.compute_month_cost(db, tenant_id, year, month_no)

    # server_id -> (name, [held, observed], lines, {currency: amount})
    names: dict[uuid.UUID, str | None] = {}
    seconds: dict[uuid.UUID, list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
    lines: dict[uuid.UUID, list[schemas.UsageCostAccountLine]] = defaultdict(list)
    costs: dict[uuid.UUID, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))

    for account in result.accounts:
        for line in account.servers:
            names.setdefault(line.server_id, line.server_name)
            cell = seconds[line.server_id]
            cell[0] += line.held_util_seconds
            cell[1] += line.observed_seconds
            costs[line.server_id][account.currency] += line.cost
            lines[line.server_id].append(
                schemas.UsageCostAccountLine(
                    account_id=account.account_id,
                    email=account.email,
                    provider=account.provider,
                    monthly_price=account.monthly_price,
                    currency=account.currency,
                    basis=account.basis,
                    utilization_pct=_pct(line.held_util_seconds, line.observed_seconds),
                    share_pct=(line.share * 100).quantize(_PCT_Q),
                    cost=line.cost,
                )
            )

    servers = []
    for server_id, account_lines in lines.items():
        account_lines.sort(key=lambda ln: (-ln.cost, str(ln.account_id)))
        held, observed = seconds[server_id]
        by_currency = costs[server_id]
        servers.append(
            schemas.UsageCostServerLine(
                server_id=server_id,
                name=names.get(server_id),
                utilization_pct=_pct(held, observed),
                # A zero total is not a cost line: an account with no price (or
                # one whose share rounded to nothing here) would otherwise hang a
                # "0.00 USD" row off a server that was never charged.
                costs=[
                    schemas.UsageCostAmount(currency=c, amount=by_currency[c])
                    for c in sorted(by_currency)
                    if by_currency[c] != 0
                ],
                accounts=account_lines,
            )
        )
    # Ordering only: the cross-currency sum is not a money figure and is never
    # shown — it just puts the expensive servers first, ties broken on id.
    servers.sort(key=lambda s: (-sum(a.amount for a in s.costs), str(s.server_id)))

    return schemas.UsageCostResponse(
        month=month,
        as_of=result.as_of,
        watermark=result.watermark,
        is_partial=result.is_partial,
        servers=servers,
        subtotals=[
            schemas.UsageCostSubtotal(
                currency=s.currency,
                allocated_cost=s.allocated_cost,
                unallocated_cost=s.unallocated_cost,
            )
            for s in result.subtotals
        ],
    )


@router.get("/usage/sessions", response_model=schemas.SessionUsageResponse)
def get_session_usage(
    tenant_id: uuid.UUID,
    db: DbSession,
    principal: AdminPrincipal,
    days: Annotated[int, Query(ge=1, le=90)] = _SESSION_DEFAULT_DAYS,
    limit: Annotated[int, Query(ge=1, le=_SESSION_MAX_ROWS)] = 200,
):
    """세션 실측 비용구조 — 최근 ``days``일의 (세션, 모델) 행을 최근순으로.

    Stop 훅(``deploy/langfuse/session_usage_hook.py``)이 채우는 ``session_usage``의
    읽기 전용 창이다. 훅을 설치하지 않은 테넌트는 빈 200을 받는다("아직 수집 없음"이
    오류가 아니라 정상 상태다). 창 기준은 세션의 마지막 assistant 레코드 시각이다.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    rows = session_usage_svc.read_session_usage(db, tenant_id, since=since, limit=limit)
    return schemas.SessionUsageResponse(
        rows=[
            schemas.SessionUsageRow(
                session_id=r.session_id,
                model=r.model,
                account_email=email,
                input_tokens=r.input_tokens,
                output_tokens=r.output_tokens,
                cache_read_tokens=r.cache_read_tokens,
                cache_create_1h_tokens=r.cache_create_1h_tokens,
                cache_create_5m_tokens=r.cache_create_5m_tokens,
                thinking_tokens=r.thinking_tokens,
                web_search_requests=r.web_search_requests,
                web_fetch_requests=r.web_fetch_requests,
                message_count=r.message_count,
                truncated=r.truncated,
                service_tier_counts=r.service_tier_counts or {},
                stop_reason_counts=r.stop_reason_counts or {},
                started_at=r.started_at,
                ended_at=r.ended_at,
            )
            for r, email in rows
        ],
        last_reported_at=session_usage_svc.last_reported_at(db, tenant_id),
    )
