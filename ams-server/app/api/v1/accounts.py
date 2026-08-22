"""Account CRUD and central OAuth enrollment — §5.3, §5.5.

`secret` is accepted, never returned. The response model carries only
`secretMasked`, so there is no code path on which an account response could
contain credential material even if a caller asked for it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query, Request, Response, status
from sqlalchemy import select

from app import models, schemas
from app.api.deps import AdminPrincipal, DbSession, PageSize, PageToken, TenantScope, next_page_token, offset_from_token
from app.config import get_settings
from app.core import crypto
from app.core.errors import bad_request
from app.models import AccountUsageWindow
from app.services import inventory
from app.services import oauth_enroll

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["accounts"], dependencies=[TenantScope])

# window_minutes 값 → 요약 슬롯. tsamx 가 실제로 보내는 값(five_hour=300,
# seven_day=10080, pool.py:107-108 의 WINDOW_FIVE_HOUR/WINDOW_SEVEN_DAY 와 같은
# 창)만 매칭하고 그 외 창(window_id 가 무엇이든)은 요약에서 뺀다.
_FIVE_HOUR_MINUTES = 300
_SEVEN_DAY_MINUTES = 10080


def _usage_window_summary(window: AccountUsageWindow) -> schemas.AccountUsageWindowSummary:
    return schemas.AccountUsageWindowSummary(pct=window.pct, resets_at=window.resets_at)


def _usage_summary_for(
    windows: list[AccountUsageWindow], *, now: datetime, stale_after: timedelta
) -> schemas.AccountUsageSummary:
    """계정 하나의 잔여 요약. 규칙 SSOT는 app/services/pool.py:412-426 ``_fresh_pct``
    — 여기서는 그 함수를 그대로 부르는 대신(순환 임포트는 없지만, 풀 자동화
    모듈 전체를 계정 조회 경로에 끌어들이지 않도록) 같은 상수(pool_window_stale_minutes)
    를 참조하는 국소 판정을 쓴다."""
    five = next((w for w in windows if w.window_minutes == _FIVE_HOUR_MINUTES), None)
    seven = next((w for w in windows if w.window_minutes == _SEVEN_DAY_MINUTES), None)
    matched = [w for w in (five, seven) if w is not None]
    fetched_at = max(
        (w.usage_fetched_at for w in matched if w.usage_fetched_at is not None),
        default=None,
    )
    # 매칭된 창 중 하나라도 "신선"하면(값이 있고, 보고 시각이 stale_after 이내)
    # 전체를 신선으로 본다 — pool.py._fresh_pct 와 같은 두 조건.
    fresh = any(
        w.pct is not None and w.reported_at is not None and now - w.reported_at <= stale_after
        for w in matched
    )
    return schemas.AccountUsageSummary(
        five_hour=_usage_window_summary(five) if five is not None else None,
        seven_day=_usage_window_summary(seven) if seven is not None else None,
        fetched_at=fetched_at,
        stale=not fresh,
    )


def _attach_usage(db: DbSession, tenant_id: uuid.UUID, accounts: list[schemas.Account]) -> None:
    if not accounts:
        return
    account_ids = [a.id for a in accounts]
    stale_after = timedelta(minutes=get_settings().pool_window_stale_minutes)
    now = datetime.now(UTC)
    by_account: dict[uuid.UUID, list[AccountUsageWindow]] = {}
    for row in db.scalars(
        select(AccountUsageWindow).where(
            AccountUsageWindow.tenant_id == tenant_id,
            AccountUsageWindow.account_id.in_(account_ids),
        )
    ).all():
        by_account.setdefault(row.account_id, []).append(row)
    for account in accounts:
        account.usage = _usage_summary_for(
            by_account.get(account.id, []), now=now, stale_after=stale_after
        )


def _validate_provider(provider: str) -> str:
    if provider not in models.PROVIDERS:
        raise bad_request(
            "account.provider_unsupported",
            f"Unsupported provider '{provider}'. Supported: {', '.join(models.PROVIDERS)}.",
        )
    return provider


@router.get("/accounts", response_model=schemas.AccountPage)
def list_accounts(
    tenant_id: uuid.UUID,
    db: DbSession,
    principal: AdminPrincipal,
    status_filter: schemas.AccountStatus | None = Query(default=None, alias="status"),
    pageSize: PageSize = 50,  # noqa: N803
    pageToken: PageToken = None,  # noqa: N803
):
    offset = offset_from_token(pageToken)
    items, total = inventory.list_accounts(
        db, tenant_id, status=status_filter, limit=pageSize, offset=offset
    )
    wire_items = [schemas.Account.model_validate(a) for a in items]
    _attach_usage(db, tenant_id, wire_items)
    return schemas.AccountPage(
        items=wire_items,
        page_info=schemas.PageInfo(
            next_page_token=next_page_token(offset, pageSize, total), total_size=total
        ),
    )


@router.post("/accounts", response_model=schemas.Account, status_code=status.HTTP_201_CREATED)
def create_account(tenant_id: uuid.UUID, body: schemas.AccountCreate, db: DbSession, principal: AdminPrincipal):
    account = inventory.create_account(
        db,
        tenant_id,
        email=str(body.email),
        provider=_validate_provider(body.provider),
        credential_type=body.credential_type,
        secret=body.secret,
        owner=body.owner,
        monthly_price=body.monthly_price,
        currency=body.currency,
        assignment_excluded=body.assignment_excluded,
    )
    return schemas.Account.model_validate(account)


@router.get("/accounts/{account_id}", response_model=schemas.Account)
def get_account(tenant_id: uuid.UUID, account_id: uuid.UUID, db: DbSession, principal: AdminPrincipal):
    wire = schemas.Account.model_validate(inventory.get_account(db, tenant_id, account_id))
    _attach_usage(db, tenant_id, [wire])
    return wire


@router.patch("/accounts/{account_id}", response_model=schemas.Account)
def update_account(
    tenant_id: uuid.UUID, account_id: uuid.UUID, body: schemas.AccountUpdate, db: DbSession, principal: AdminPrincipal
):
    account = inventory.update_account(
        db,
        tenant_id,
        account_id,
        email=str(body.email) if body.email else None,
        status=body.status,
        secret=body.secret,
        owner=body.owner,
        # An explicit `"monthlyPrice": null` clears the price, while omitting the
        # key leaves it alone — the two are only distinguishable through
        # model_fields_set, since both arrive as None.
        monthly_price=(
            body.monthly_price
            if "monthly_price" in body.model_fields_set
            else inventory.UNSET
        ),
        currency=body.currency,
        assignment_excluded=body.assignment_excluded,
    )
    return schemas.Account.model_validate(account)


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(tenant_id: uuid.UUID, account_id: uuid.UUID, db: DbSession, principal: AdminPrincipal) -> Response:
    inventory.delete_account(db, tenant_id, account_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/accounts:oauth-start",
    response_model=schemas.OauthStartResponse,
    status_code=status.HTTP_201_CREATED,
)
def start_oauth(
    tenant_id: uuid.UUID, body: schemas.OauthStartRequest, db: DbSession, request: Request, principal: AdminPrincipal
):
    inventory.get_tenant(db, tenant_id)
    settings = get_settings()
    # A provider AMS knows about is not necessarily one it can run a flow for:
    # codex is import-only. Resolve the profile before minting anything, so a
    # rejected provider does not leave an unusable flow (and its verifier)
    # sitting in the store until TTL.
    provider = _validate_provider(body.provider)
    oauth_enroll.profile_for(provider)
    flow = request.app.state.oauth_flows.create(
        tenant_id, settings.oauth_flow_ttl_seconds, body.label, provider=provider,
    )
    return schemas.OauthStartResponse(
        flow_id=flow.flow_id,
        authorize_url=oauth_enroll.authorize_url(flow),
        expires_at=flow.expires_at,
    )


@router.post(
    "/accounts:oauth-complete",
    response_model=schemas.Account,
    status_code=status.HTTP_201_CREATED,
)
def complete_oauth(
    tenant_id: uuid.UUID, body: schemas.OauthCompleteRequest, db: DbSession, request: Request, principal: AdminPrincipal
):
    inventory.get_tenant(db, tenant_id)
    # take() consumes the flow before the exchange, so the verifier is gone
    # whether the exchange below succeeds or raises (§7 single-use).
    flow = request.app.state.oauth_flows.take(body.flow_id, tenant_id)
    settings = get_settings()
    credential_set = oauth_enroll.exchange_code(
        flow,
        body.code,
        timeout_s=settings.http_timeout_seconds,
        client=getattr(request.app.state, "oauth_http_client", None),
    )
    email = (
        str(body.email)
        if body.email
        else oauth_enroll.email_from_credential_set(credential_set)
    )
    if not email:
        raise bad_request(
            "oauth.email_unknown",
            "The credential set carries no email; supply one in the request.",
        )
    account = inventory.create_account(
        db,
        tenant_id,
        email=email,
        provider=flow.provider,
        credential_type="oauth",
        secret=crypto.dumps_credential(credential_set),
        assignment_excluded=body.assignment_excluded,
    )
    return schemas.Account.model_validate(account)
