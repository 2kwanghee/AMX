"""Account CRUD and central OAuth enrollment — §5.3, §5.5.

`secret` is accepted, never returned. The response model carries only
`secretMasked`, so there is no code path on which an account response could
contain credential material even if a caller asked for it.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Request, Response, status

from app import schemas
from app.api.deps import AdminAuth, DbSession, PageSize, PageToken, next_page_token, offset_from_token
from app.config import get_settings
from app.core import crypto
from app.core.errors import bad_request
from app.services import inventory
from app.services import oauth_enroll

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["accounts"], dependencies=[AdminAuth])


@router.get("/accounts", response_model=schemas.AccountPage)
def list_accounts(
    tenant_id: uuid.UUID,
    db: DbSession,
    status_filter: schemas.AccountStatus | None = Query(default=None, alias="status"),
    pageSize: PageSize = 50,  # noqa: N803
    pageToken: PageToken = None,  # noqa: N803
):
    offset = offset_from_token(pageToken)
    items, total = inventory.list_accounts(
        db, tenant_id, status=status_filter, limit=pageSize, offset=offset
    )
    return schemas.AccountPage(
        items=[schemas.Account.model_validate(a) for a in items],
        page_info=schemas.PageInfo(
            next_page_token=next_page_token(offset, pageSize, total), total_size=total
        ),
    )


@router.post("/accounts", response_model=schemas.Account, status_code=status.HTTP_201_CREATED)
def create_account(tenant_id: uuid.UUID, body: schemas.AccountCreate, db: DbSession):
    account = inventory.create_account(
        db,
        tenant_id,
        email=str(body.email),
        credential_type=body.credential_type,
        secret=body.secret,
    )
    return schemas.Account.model_validate(account)


@router.get("/accounts/{account_id}", response_model=schemas.Account)
def get_account(tenant_id: uuid.UUID, account_id: uuid.UUID, db: DbSession):
    return schemas.Account.model_validate(inventory.get_account(db, tenant_id, account_id))


@router.patch("/accounts/{account_id}", response_model=schemas.Account)
def update_account(
    tenant_id: uuid.UUID, account_id: uuid.UUID, body: schemas.AccountUpdate, db: DbSession
):
    account = inventory.update_account(
        db,
        tenant_id,
        account_id,
        email=str(body.email) if body.email else None,
        status=body.status,
        secret=body.secret,
    )
    return schemas.Account.model_validate(account)


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(tenant_id: uuid.UUID, account_id: uuid.UUID, db: DbSession) -> Response:
    inventory.delete_account(db, tenant_id, account_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/accounts:oauth-start",
    response_model=schemas.OauthStartResponse,
    status_code=status.HTTP_201_CREATED,
)
def start_oauth(
    tenant_id: uuid.UUID, body: schemas.OauthStartRequest, db: DbSession, request: Request
):
    inventory.get_tenant(db, tenant_id)
    settings = get_settings()
    flow = request.app.state.oauth_flows.create(
        tenant_id, settings.oauth_flow_ttl_seconds, body.label
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
    tenant_id: uuid.UUID, body: schemas.OauthCompleteRequest, db: DbSession, request: Request
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
        credential_type="oauth",
        secret=crypto.dumps_credential(credential_set),
    )
    return schemas.Account.model_validate(account)
