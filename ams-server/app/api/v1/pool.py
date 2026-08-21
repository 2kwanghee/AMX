"""계정 풀 조회·정책·수동 개입 — 기획서 §2, P1(관측만).

이 라우터는 명령을 발행하지 않는다. 읽는 쪽(GET /pool, /pool/recommendations,
/pool/events)과, 자동화가 손대면 안 되는 값을 사람이 세우는 쪽(pin/unpin/hold/
release, PATCH pool-policy)뿐이다. deliver/switch_now/recall 로 이어지는 실행은
P2 의 ``:apply`` 몫이라 여기에는 없다.

pin/hold 가 별도 컬럼이 아니라 ``pool_state`` 의 값인 이유는 스윕과 한 자리에서
겨루게 하기 위해서다 — 별도 불리언이었다면 스윕이 상태를 덮어쓴 뒤 "그런데 pinned
였다"를 따로 기억해야 하고, 그 두 기억이 어긋나는 순간 자동화가 사람의 결정을
되돌린다. ``services.pool.compute_states`` 는 ``POOL_OPERATOR_STATES`` 를 만나면
그 계정을 아예 건드리지 않는다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Query
from sqlalchemy import select

from app import schemas
from app.api.deps import AdminPrincipal, DbSession, TenantScope
from app.core.errors import conflict
from app.models import (
    Account,
    AccountUsageWindow,
    Assignment,
    PoolEvent,
    PoolRecommendation,
    Server,
    Tenant,
)
from app.services import inventory, pool

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["pool"], dependencies=[TenantScope])

_DETACHED = "detached"
_IN_FLIGHT = ("pending", "delivering", "recalling")


def _now() -> datetime:
    return datetime.now(UTC)


def _actor(principal: AdminPrincipal) -> str:
    return principal.email or "admin"


def _server_wire(db, server: Server) -> schemas.Server:
    wire = schemas.Server.model_validate(server)
    wire.enrolled = server.server_cred_hash is not None
    wire.assigned_account_count = inventory.assigned_account_count(db, server.id)
    wire.pool_policy = schemas.PoolPolicy(**pool.resolve_policy(server))
    return wire


def _account_wire(account: Account) -> schemas.Account:
    return schemas.Account.model_validate(account)


@router.get("/pool", response_model=schemas.PoolOverview)
def get_pool(tenant_id: uuid.UUID, db: DbSession, principal: AdminPrincipal):
    """배급처·대여중·충전소를 한 번에 보여 주는 읽기 모델.

    콘솔이 계정/서버/배정/창을 각각 부르고 조합하는 대신 서버가 조합해 준다 — 세 열의
    합이 항상 전체 계정 수라는 불변식이 클라이언트마다 다르게 깨지지 않도록.
    """
    tenant = db.get(Tenant, tenant_id)
    accounts = list(
        db.scalars(
            select(Account).where(Account.tenant_id == tenant_id).order_by(Account.email)
        ).all()
    )
    servers = list(
        db.scalars(
            select(Server).where(Server.tenant_id == tenant_id).order_by(Server.name)
        ).all()
    )
    live = list(
        db.scalars(
            select(Assignment).where(
                Assignment.tenant_id == tenant_id, Assignment.state != _DETACHED
            )
        ).all()
    )
    by_account = {a.account_id: a for a in live}
    windows: dict[uuid.UUID, list[AccountUsageWindow]] = {}
    for row in db.scalars(
        select(AccountUsageWindow)
        .where(AccountUsageWindow.tenant_id == tenant_id)
        .order_by(AccountUsageWindow.window_id)
    ).all():
        windows.setdefault(row.account_id, []).append(row)

    pool_accounts = []
    for account in accounts:
        assignment = by_account.get(account.id)
        pool_accounts.append(
            schemas.PoolAccount(
                account_id=account.id,
                email=account.email,
                provider=account.provider,
                pool_state=account.pool_state,
                cooling_until=account.cooling_until,
                cooling_window_id=account.cooling_window_id,
                leased_server_id=assignment.server_id if assignment else None,
                lease_started_at=(
                    (assignment.delivered_at or assignment.created_at) if assignment else None
                ),
                last_lease_ended_at=account.last_lease_ended_at,
                pool_state_changed_at=account.pool_state_changed_at,
                windows=[
                    schemas.WindowState(
                        window_id=w.window_id,
                        pct=w.pct,
                        resets_at=w.resets_at,
                        usage_fetched_at=w.usage_fetched_at,
                        reported_at=w.reported_at,
                        server_id=w.server_id,
                    )
                    for w in windows.get(account.id, [])
                ],
            )
        )

    account_by_id = {a.id: a for a in accounts}
    pool_servers = []
    for server in servers:
        held = [a for a in live if a.server_id == server.id]
        leased_ids = [a.account_id for a in held]
        # UI 불변식과 같은 정의: state=active 중 계정의 last_switched_at 이 가장 최신인 것
        # (ams-web/src/lib/assignment-active.ts). 서버가 같은 규칙으로 답해야 콘솔의
        # "활성" 표시가 두 곳에서 갈리지 않는다.
        actives = [a for a in held if a.state == "active"]
        active_account_id = None
        if actives:
            epoch = datetime.min.replace(tzinfo=UTC)
            best = max(
                actives,
                key=lambda a: (
                    (account_by_id[a.account_id].last_switched_at or epoch)
                    if a.account_id in account_by_id
                    else epoch,
                    str(a.account_id),
                ),
            )
            active_account_id = best.account_id
        pcts = [
            w.pct for a in held for w in windows.get(a.account_id, [])
        ]
        pool_servers.append(
            schemas.PoolServer(
                server_id=server.id,
                name=server.name,
                status=server.status,
                pool_policy=schemas.PoolPolicy(**pool.resolve_policy(server)),
                leased_account_ids=leased_ids,
                active_account_id=active_account_id,
                in_flight=any(a.state in _IN_FLIGHT for a in held),
                max_pct=max(pcts) if pcts else None,
            )
        )

    return schemas.PoolOverview(
        automation_paused=bool(tenant is not None and tenant.pool_automation_paused),
        accounts=pool_accounts,
        servers=pool_servers,
        recommendations=[
            schemas.PoolRecommendation.model_validate(r) for r in _recommendations(db, tenant_id)
        ],
    )


def _recommendations(db, tenant_id: uuid.UUID) -> list[PoolRecommendation]:
    return list(
        db.scalars(
            select(PoolRecommendation)
            .where(PoolRecommendation.tenant_id == tenant_id)
            .order_by(PoolRecommendation.created_at.desc())
        ).all()
    )


@router.get("/pool/recommendations", response_model=list[schemas.PoolRecommendation])
def list_recommendations(tenant_id: uuid.UUID, db: DbSession, principal: AdminPrincipal):
    return [schemas.PoolRecommendation.model_validate(r) for r in _recommendations(db, tenant_id)]


@router.get("/pool/events", response_model=list[schemas.PoolEvent])
def list_pool_events(
    tenant_id: uuid.UUID,
    db: DbSession,
    principal: AdminPrincipal,
    limit: int = Query(default=100, ge=1, le=500),
):
    rows = db.scalars(
        select(PoolEvent)
        .where(PoolEvent.tenant_id == tenant_id)
        .order_by(PoolEvent.created_at.desc(), PoolEvent.id.desc())
        .limit(limit)
    ).all()
    return [schemas.PoolEvent.model_validate(e) for e in rows]


@router.patch("/servers/{server_id}/pool-policy", response_model=schemas.Server)
def update_pool_policy(
    tenant_id: uuid.UUID,
    server_id: uuid.UUID,
    body: schemas.PoolPolicyUpdate,
    db: DbSession,
    principal: AdminPrincipal,
):
    """준 필드만 병합 저장한다(ServerUpdate 와 같은 부분 수정 규약).

    범위 검증은 스키마의 Field 제약이 한다 — pct 0~100, target_leases 1~5. 여기서
    다시 세지 않는 이유는 두 곳에 임계가 생기면 언젠가 갈라지기 때문이다.
    """
    server = inventory.get_server(db, tenant_id, server_id)
    fields = body.model_fields_set
    merged = dict(server.pool_policy or {})
    changed = {}
    for name in schemas.PoolPolicy.model_fields:
        if name in fields:
            value = getattr(body, name)
            if value is None:
                merged.pop(name, None)
            else:
                merged[name] = value
            changed[name] = value
    if changed:
        server.pool_policy = merged
        server.updated_at = _now()
        pool.record_event(
            db,
            tenant_id=tenant_id,
            kind="policy_changed",
            server_id=server_id,
            actor=_actor(principal),
            detail={"changed": {k: v for k, v in changed.items()}},
        )
        db.commit()
        db.refresh(server)
    return _server_wire(db, server)


def _set_state(
    db,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    new_state: str,
    *,
    principal: AdminPrincipal,
    reason: str,
    allowed_from: tuple[str, ...] | None = None,
) -> schemas.Account:
    account = inventory.get_account(db, tenant_id, account_id)
    if allowed_from is not None and account.pool_state not in allowed_from:
        raise conflict(
            "pool.state_conflict",
            f"계정이 {account.pool_state} 상태라 이 동작을 적용할 수 없다 "
            f"(허용: {', '.join(allowed_from)}).",
        )
    pool.set_pool_state(
        db,
        account,
        new_state,
        actor=_actor(principal),
        reason=reason,
    )
    db.commit()
    db.refresh(account)
    return _account_wire(account)


@router.post("/accounts/{account_id}/pool:pin", summary="pool:pin", response_model=schemas.Account)
def pin_account(
    tenant_id: uuid.UUID, account_id: uuid.UUID, db: DbSession, principal: AdminPrincipal
):
    """계정을 자동화 밖으로 고정한다. 스윕은 이 계정의 상태를 다시 계산하지 않는다."""
    return _set_state(db, tenant_id, account_id, "pinned", principal=principal, reason="operator_pin")


@router.post(
    "/accounts/{account_id}/pool:unpin", summary="pool:unpin", response_model=schemas.Account
)
def unpin_account(
    tenant_id: uuid.UUID, account_id: uuid.UUID, db: DbSession, principal: AdminPrincipal
):
    """고정을 풀어 배급처로 되돌린다. 다음 스윕이 실제 상태를 다시 계산한다."""
    return _set_state(
        db,
        tenant_id,
        account_id,
        "ready",
        principal=principal,
        reason="operator_unpin",
        allowed_from=("pinned",),
    )


@router.post(
    "/accounts/{account_id}/pool:hold", summary="pool:hold", response_model=schemas.Account
)
def hold_account(
    tenant_id: uuid.UUID, account_id: uuid.UUID, db: DbSession, principal: AdminPrincipal
):
    """사용 불가로 묶는다(격리·점검). pinned 와 마찬가지로 스윕이 덮지 않는다."""
    return _set_state(
        db, tenant_id, account_id, "held", principal=principal, reason="operator_hold"
    )


@router.post(
    "/accounts/{account_id}/pool:release", summary="pool:release", response_model=schemas.Account
)
def release_account(
    tenant_id: uuid.UUID, account_id: uuid.UUID, db: DbSession, principal: AdminPrincipal
):
    """held/cooling 을 강제로 배급처로 되돌린다.

    대여 중(leased/recalling)인 계정에는 쓸 수 없다 — 배정이 살아 있는데 상태만 ready 로
    적어 두면 다음 스윕이 즉시 leased 로 되돌리므로, 운영자에게 거짓을 보여 주게 된다.
    """
    return _set_state(
        db,
        tenant_id,
        account_id,
        "ready",
        principal=principal,
        reason="operator_release",
        allowed_from=("held", "cooling"),
    )
