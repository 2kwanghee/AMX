"""계정 풀 컨트롤러 — P0 정규화 + P1 관측(기획서 §2, docs/design-notes/account-pool-automation-plan.md).

세 가지 일을 한다.

1. **ingest** — UsageReport 가 도착할 때 계정별 창(pct·resets_at)을
   ``account_usage_windows`` 에 upsert 하고, 창 하나라도 고사용 임계에 닿으면
   ``account_window_high`` 경보를 연다. 스냅샷 INSERT 와 **같은 트랜잭션**이므로
   보고가 롤백되면 창 값도 함께 사라진다.
2. **compute_states** — 배정과 창 관측으로부터 ``accounts.pool_state`` 를 계산한다.
3. **build_recommendations** — ``mode=auto`` 서버에 대해 교체 권고를 만든다.

P1 은 **명령을 내지 않는다**. deliver/switch_now/recall 은 한 줄도 발행하지 않고,
컨트롤러가 "지금이라면 이렇게 하겠다"를 권고 행으로만 남긴다. 관측이 틀렸을 때
잘못 움직이는 것보다, 운영자가 며칠 지켜보고 나서 P2 로 넘어가는 편이 싸다.

상태 계산에서 가장 조심한 지점은 **충전소 복귀**다. ``cooling_until`` 만 믿고
되돌리면 리셋 직후 한 번 더 소진되는 계정을 못 막으므로, 시각 경과 **그리고**
복귀 임계 이하의 관측을 함께 요구한다. 다만 에이전트가 죽어 관측이 영영 안 오면
계정이 충전소에 갇히므로, ``cooling_until`` + 유예(기본 15분)를 넘기면 관측 없이도
풀어 준다 — 갇힌 계정은 잘못된 관측보다 확실한 손해다.

``pinned`` / ``held`` 는 운영자가 설정하는 값이고 스윕은 절대 덮어쓰지 않는다.
자동화가 사람의 결정을 되돌리는 순간 이 기능은 신뢰를 잃는다.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import try_advisory_xact_lock as _try_advisory_xact_lock
from app.core.errors import ApiError, conflict, not_found
from app.models import (
    POOL_CONTROLLER_ACTOR,
    POOL_OPERATOR_STATES,
    Account,
    AccountUsageWindow,
    AgentCommand,
    Alert,
    Assignment,
    PoolChain,
    PoolEvent,
    PoolRecommendation,
    Server,
    Tenant,
)
from app.services import alerts as alerts_service, commands, inventory

# 보존 스윕 …0C 다음 번호. 한 인스턴스만 이번 틱의 풀 계산을 소유한다(F3 다중 인스턴스).
_POOL_SWEEP_LOCK_KEY = 0x414D580F0D
# 체인 실행은 별도 락이다. 관측 스윕과 달리 명령 발행 서비스(create_assignment /
# request_deliver / …)가 **스스로 커밋**하는데, 트랜잭션 범위 락은 그 커밋에서
# 풀린다. 그래서 체인 한 건을 전진시킬 때마다 락을 새로 잡는다 — 보호해야 하는
# 것은 "결정하고 명령을 내는" 한 걸음이지 스윕 전체가 아니다.
_POOL_CHAIN_LOCK_KEY = 0x414D580F0E
# pool_events 보존 purge. 배치마다 커밋하므로 자체 키로 따로 잡는다(…0C 관례).
_POOL_EVENT_RETENTION_LOCK_KEY = 0x414D580F0F
_POOL_EVENT_RETENTION_BATCH = 5000

_logger = logging.getLogger(__name__)

# 배정이 살아 있다고 보는 상태 — detached 만 이력이다(models.Assignment 의 부분 유니크
# 인덱스가 쓰는 것과 같은 기준).
_DETACHED = "detached"
# 서버에 전달이 진행 중이면 컨트롤러는 그 서버에 아무 권고도 내지 않는다. reconcile 의
# CORRECTION_CAP 과 부딪히면 둘 다 in-flight 스킵으로 서로를 막기 때문이다(기획서 §4.2).
_IN_FLIGHT_STATES = ("pending", "delivering", "recalling")

# 후보에서 아예 빼는 재고 상태.
_UNUSABLE_STATUSES = ("disabled", "quarantined")

WINDOW_FIVE_HOUR = "five_hour"
WINDOW_SEVEN_DAY = "seven_day"

# servers.pool_policy 가 비어 있을 때의 값(기획서 §2.2). mode=manual 이 기본이라
# 마이그레이션만으로는 어떤 서버도 자동화에 들어오지 않는다.
DEFAULT_POLICY: dict[str, Any] = {
    "mode": "manual",
    "target_leases": 1,
    "swap_at_pct": 85,
    "prefetch_at_pct": 70,
    "min_lease_minutes": 30,
    "ready_return_pct": 20,
}

WINDOW_HIGH_KIND = "account_window_high"


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    """DB 에서 온 값은 tz-aware 지만, 테스트가 넘긴 naive 값도 UTC 로 받아 준다."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def resolve_policy(server: Server | None) -> dict[str, Any]:
    """서버의 풀 정책을 기본값 위에 얹어 완전한 dict 로 만든다.

    부분 저장을 허용하므로(PATCH 는 준 필드만 쓴다) 읽는 쪽은 항상 이걸 거친다.
    알 수 없는 키는 무시한다 — 옛 배포가 남긴 필드가 계산에 끼어들지 않게.
    """
    policy = dict(DEFAULT_POLICY)
    stored = getattr(server, "pool_policy", None) if server is not None else None
    if isinstance(stored, dict):
        for key, value in stored.items():
            if key in DEFAULT_POLICY and value is not None:
                policy[key] = value
    return policy


# -- P0 ingest ---------------------------------------------------------------
def _window_rows(acc: dict) -> list[dict]:
    """AccountUsage dict 하나에서 창 목록을 뽑는다.

    자기서술적인 ``windows`` 가 있으면 그게 정본이고, 없으면 구형 에이전트가 보낸
    위치 필드 ``five_hour``/``seven_day`` 를 같은 모양으로 접어 준다. 두 경로 모두
    ``id`` 가 없으면 그 창은 식별할 수 없으므로 버린다 — PK 의 일부다.
    """
    rows: list[dict] = []
    windows = acc.get("windows")
    if isinstance(windows, list) and windows:
        for w in windows:
            if not isinstance(w, dict):
                continue
            window_id = str(w.get("id") or "").strip()
            if not window_id:
                continue
            rows.append(
                {
                    "window_id": window_id,
                    "pct": _pct(w.get("pct")),
                    "resets_at": _ts(w.get("resets_at")),
                    "window_minutes": _int(w.get("window_minutes")),
                }
            )
        # 항목이 하나라도 살아남았으면 그게 정본이다. 전부 id 가 없어 한 줄도 못
        # 건졌다면 그건 "자기서술적 창을 보냈다"가 아니라 그 필드가 쓸모없다는
        # 뜻이므로, 같은 보고 안의 위치 필드로 폴백한다 — 창을 통째로 잃는 것보다
        # 낫다(구형 필드는 채우면서 windows 껍데기만 보내는 에이전트가 있다).
        if rows:
            return rows
    for legacy_id in (WINDOW_FIVE_HOUR, WINDOW_SEVEN_DAY):
        w = acc.get(legacy_id)
        if isinstance(w, dict):
            rows.append(
                {
                    "window_id": legacy_id,
                    "pct": _pct(w.get("pct")),
                    "resets_at": _ts(w.get("resets_at")),
                    "window_minutes": _int(w.get("window_minutes")),
                }
            )
    return rows


def _pct(value: object) -> float | None:
    """창의 사용률. **누락은 0.0, 읽을 수 없는 값은 미상(None)** 이다.

    proto3 은 0.0 스칼라를 MessageToDict 에서 아예 빼므로 키가 없는 것은 진짜
    0% 다. 하지만 값이 있는데 숫자로 읽히지 않는 경우까지 0.0 으로 접으면 안
    된다 — 0% 는 "여유가 가득하다"는 가장 강한 주장이라, 파싱 실패가 곧 그
    계정을 최우선 후보로 밀어 올린다. 미상은 미상으로 남겨 두고, 미상인 계정은
    트리거에도 후보에도 쓰지 않는다.
    """
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _ts(value: object) -> datetime | None:
    """MessageToDict 의 Timestamp 는 RFC3339 문자열이다. 못 읽으면 NULL."""
    if isinstance(value, datetime):
        return _aware(value)
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        return _aware(datetime.fromisoformat(raw))
    except ValueError:
        return None


def _resolve_account_id(
    acc: dict, *, email_index: dict[str, uuid.UUID], known: set[uuid.UUID]
) -> uuid.UUID | None:
    """보고서의 계정 참조를 이 테넌트의 accounts.id 로 되돌린다.

    ``ams_account_id`` 가 정본이고(에이전트가 배정 매니페스트에서 그대로 되돌려 준다),
    비어 있거나 이 테넌트 것이 아니면 이메일로 역매핑한다 —
    ``usage_cost.account_utilization`` 경로가 쓰는 것과 같은 재료다. 둘 다 실패하면
    None: 남의 테넌트 계정 id 를 그대로 쓰는 일은 없어야 한다.
    """
    ref = acc.get("account")
    if not isinstance(ref, dict):
        return None
    raw = ref.get("ams_account_id")
    if raw:
        try:
            candidate = uuid.UUID(str(raw))
        except (ValueError, TypeError):
            candidate = None
        if candidate is not None and candidate in known:
            return candidate
    email = ref.get("email")
    if isinstance(email, str) and email.strip():
        return email_index.get(email.strip().lower())
    return None


def ingest_usage_report(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    server_id: uuid.UUID,
    payload: dict,
    reported_at: datetime | None = None,
    source_snapshot_id: uuid.UUID | None = None,
) -> int:
    """UsageReport 한 건의 계정별 창을 정규화 테이블에 upsert 한다(P0).

    caller 의 세션에 스테이징만 하고 커밋하지 않는다 — 스냅샷/드리프트/경보와 같은
    트랜잭션에 들어가야 "보고는 남았는데 창 값은 없다" 같은 반쪽 상태가 안 생긴다.
    반환값은 갱신한 창의 수(로그·테스트용).
    """
    accounts = payload.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        return 0

    rows = db.execute(
        select(Account.id, Account.email).where(Account.tenant_id == tenant_id)
    ).all()
    known = {r.id for r in rows}
    email_index = {r.email.strip().lower(): r.id for r in rows if r.email}

    stamp = _aware(reported_at) or _now()
    high_pct = get_settings().pool_window_high_pct
    touched = 0
    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        account_id = _resolve_account_id(acc, email_index=email_index, known=known)
        if account_id is None:
            continue
        fetched_at = _ts(acc.get("usage_fetched_at"))
        windows = _window_rows(acc)
        if not windows:
            continue
        for w in windows:
            stmt = pg_insert(AccountUsageWindow).values(
                tenant_id=tenant_id,
                account_id=account_id,
                window_id=w["window_id"],
                pct=w["pct"],
                resets_at=w["resets_at"],
                window_minutes=w["window_minutes"],
                usage_fetched_at=fetched_at,
                reported_at=stamp,
                server_id=server_id,
            )
            db.execute(
                stmt.on_conflict_do_update(
                    index_elements=["tenant_id", "account_id", "window_id"],
                    set_={
                        "pct": stmt.excluded.pct,
                        "resets_at": stmt.excluded.resets_at,
                        "window_minutes": stmt.excluded.window_minutes,
                        "usage_fetched_at": stmt.excluded.usage_fetched_at,
                        "reported_at": stmt.excluded.reported_at,
                        "server_id": stmt.excluded.server_id,
                    },
                )
            )
            touched += 1
        _sync_window_alert(
            db,
            tenant_id=tenant_id,
            server_id=server_id,
            account_id=account_id,
            windows=windows,
            high_pct=high_pct,
            source_snapshot_id=source_snapshot_id,
        )
    return touched


def _sync_window_alert(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    server_id: uuid.UUID,
    account_id: uuid.UUID,
    windows: list[dict],
    high_pct: float,
    source_snapshot_id: uuid.UUID | None,
) -> None:
    """계정 창 고사용 경보를 이 보고에 맞춰 열거나 닫는다(auto-resolve 포함).

    서버 범위 ``all_exhausted`` 는 전원이 막힌 뒤에야 울린다. 그 사이 한 계정만
    한계에 가까워지는 흔한 경우가 관측되지 않으므로, 계정 범위로 따로 연다.
    """
    # pct 를 못 읽은 창은 "0% 였다"가 아니라 "모른다"이므로 경보 판단에서 뺀다.
    known = [w for w in windows if w["pct"] is not None]
    worst = max(known, key=lambda w: w["pct"], default=None)
    if worst is not None and worst["pct"] >= high_pct:
        alerts_service.open_alert(
            db,
            tenant_id=tenant_id,
            server_id=server_id,
            account_id=account_id,
            kind=WINDOW_HIGH_KIND,
            severity="warning",
            detail={
                "window_id": worst["window_id"],
                "pct": worst["pct"],
                "threshold_pct": high_pct,
                "resets_at": str(worst["resets_at"]) if worst["resets_at"] else None,
            },
            source_snapshot_id=source_snapshot_id,
        )
    else:
        alerts_service.resolve(
            db, server_id=server_id, kind=WINDOW_HIGH_KIND, account_id=account_id
        )


def _stale_after() -> timedelta:
    return timedelta(minutes=get_settings().pool_window_stale_minutes)


def _fresh_pct(
    window: AccountUsageWindow, now: datetime, stale_after: timedelta
) -> float | None:
    """이 창의 pct 를 **지금 믿어도 되는 값**으로 돌려준다. 아니면 None.

    값 자체가 미상이거나(파싱 실패) 관측이 낡았으면 None 이다. 낡은 관측으로
    교체를 트리거하면 이미 리셋된 계정을 거두거나 소진된 계정을 계속 물린 채
    둔다 — 둘 다 관측이 없을 때보다 나쁘다.
    """
    if window.pct is None:
        return None
    reported = _aware(window.reported_at)
    if reported is None or now - reported > stale_after:
        return None
    return float(window.pct)


def _max_fresh_pct(
    windows: list[AccountUsageWindow], now: datetime, stale_after: timedelta
) -> float | None:
    values = [
        p
        for p in (_fresh_pct(w, now, stale_after) for w in windows)
        if p is not None
    ]
    return max(values) if values else None


# -- P1 상태 계산 -------------------------------------------------------------
def record_event(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    kind: str,
    account_id: uuid.UUID | None = None,
    server_id: uuid.UUID | None = None,
    detail: dict | None = None,
    actor: str = POOL_CONTROLLER_ACTOR,
) -> None:
    db.add(
        PoolEvent(
            tenant_id=tenant_id,
            account_id=account_id,
            server_id=server_id,
            kind=kind,
            detail=detail or {},
            actor=actor,
        )
    )


def set_pool_state(
    db: Session,
    account: Account,
    new_state: str,
    *,
    actor: str,
    reason: str,
    server_id: uuid.UUID | None = None,
    detail: dict | None = None,
    cooling_until: datetime | None = None,
    cooling_window_id: str | None = None,
    now: datetime | None = None,
) -> bool:
    """계정의 풀 상태를 바꾸고 ``pool_events`` 에 한 줄 남긴다. 바뀌었으면 True.

    스윕과 REST 가 같은 프리미티브를 쓰는 이유는 감사 때문이다 — REST 미들웨어는
    자동 변경을 못 잡고, 스윕은 REST 를 안 타므로, 두 경로가 같은 자리에 남지 않으면
    "누가 이 상태를 바꿨나"에 답할 수 없다.
    """
    stamp = _aware(now) or _now()
    previous = account.pool_state
    changed = (
        previous != new_state
        or account.cooling_until != cooling_until
        or account.cooling_window_id != cooling_window_id
    )
    if not changed:
        return False
    account.pool_state = new_state
    account.cooling_until = cooling_until
    account.cooling_window_id = cooling_window_id
    account.pool_state_changed_at = stamp
    account.updated_at = stamp
    record_event(
        db,
        tenant_id=account.tenant_id,
        kind="state_changed",
        account_id=account.id,
        server_id=server_id,
        actor=actor,
        detail={
            "from": previous,
            "to": new_state,
            "reason": reason,
            **(detail or {}),
        },
    )
    return True


def _live_assignments(db: Session, tenant_id: uuid.UUID) -> dict[uuid.UUID, Assignment]:
    """계정 -> 살아 있는(비-detached) 배정. 부분 유니크 인덱스가 최대 1개를 보장한다."""
    rows = db.scalars(
        select(Assignment).where(
            Assignment.tenant_id == tenant_id, Assignment.state != _DETACHED
        )
    ).all()
    return {a.account_id: a for a in rows}


def _windows_by_account(
    db: Session, tenant_id: uuid.UUID
) -> dict[uuid.UUID, list[AccountUsageWindow]]:
    grouped: dict[uuid.UUID, list[AccountUsageWindow]] = {}
    for row in db.scalars(
        select(AccountUsageWindow).where(AccountUsageWindow.tenant_id == tenant_id)
    ).all():
        grouped.setdefault(row.account_id, []).append(row)
    return grouped


def _servers_by_id(db: Session, tenant_id: uuid.UUID) -> dict[uuid.UUID, Server]:
    return {
        s.id: s
        for s in db.scalars(select(Server).where(Server.tenant_id == tenant_id)).all()
    }


def _observing_policy(
    windows: list[AccountUsageWindow], servers: dict[uuid.UUID, Server]
) -> dict[str, Any]:
    """배정이 없는 계정에는 "이 관측을 올린 서버"의 정책을 쓴다.

    계정이 어느 서버에도 안 붙어 있으면 임계를 물어볼 서버가 없다. 마지막으로 그 계정을
    보고한 서버가 유일하게 의미 있는 출처이고, 그마저 없으면 기본값이다.
    """
    if not windows:
        return dict(DEFAULT_POLICY)
    latest = max(windows, key=lambda w: _aware(w.reported_at) or datetime.min.replace(tzinfo=UTC))
    return resolve_policy(servers.get(latest.server_id))


def compute_states(
    db: Session, tenant_id: uuid.UUID, *, now: datetime | None = None
) -> int:
    """한 테넌트의 모든 계정에 대해 ``pool_state`` 를 다시 계산한다(계약의 상태 규칙).

    caller 가 커밋한다. 반환값은 실제로 바뀐 계정 수.
    """
    stamp = _aware(now) or _now()
    grace = timedelta(minutes=get_settings().pool_observation_grace_minutes)
    stale_after = _stale_after()
    accounts = db.scalars(
        select(Account).where(Account.tenant_id == tenant_id).order_by(Account.id)
    ).all()
    live = _live_assignments(db, tenant_id)
    windows = _windows_by_account(db, tenant_id)
    servers = _servers_by_id(db, tenant_id)
    unusable = _unusable_account_ids(db, tenant_id)

    changed = 0
    for account in accounts:
        # 운영자가 세운 값은 스윕이 절대 건드리지 않는다.
        if account.pool_state in POOL_OPERATOR_STATES:
            continue
        assignment = live.get(account.id)
        account_windows = windows.get(account.id, [])

        if assignment is not None:
            state = "recalling" if assignment.state == "recalling" else "leased"
            if set_pool_state(
                db,
                account,
                state,
                actor=POOL_CONTROLLER_ACTOR,
                reason="live_assignment",
                server_id=assignment.server_id,
                detail={"assignment_state": assignment.state},
                now=stamp,
            ):
                changed += 1
            continue

        # 여기부터는 배정이 없는 계정. 방금 대여가 끝났으면 공평 순환의 기준점을 찍는다.
        if account.pool_state in ("leased", "recalling"):
            account.last_lease_ended_at = stamp

        # 사용 불가로 신고된 계정은 충전소가 아니라 **보류**로 간다. cooling 은
        # "시간이 지나면 스스로 돌아온다"는 뜻인데, 자격증명이 죽었거나 격리된
        # 계정은 시간이 해결해 주지 않는다. held 는 운영자 상태라 스윕이 다시
        # 건드리지 않으므로, 사람이 원인을 없애고 release 할 때까지 머문다.
        if account.id in unusable or account.status in _UNUSABLE_STATUSES:
            if set_pool_state(
                db,
                account,
                "held",
                actor=POOL_CONTROLLER_ACTOR,
                reason="account_unusable",
                detail={"account_status": account.status},
                now=stamp,
            ):
                changed += 1
            continue

        policy = _observing_policy(account_windows, servers)
        swap_at = float(policy["swap_at_pct"])
        ready_return = float(policy["ready_return_pct"])

        # 소진 판정에는 **신선한** 관측만 쓴다. 30분 전의 95% 는 리셋이 지났을 수도
        # 있는 값이라, 그걸로 충전소에 넣으면 멀쩡한 계정이 갇힌다.
        exhausted = [
            w
            for w in account_windows
            if (_fresh_pct(w, stamp, stale_after) or -1.0) >= swap_at
            and _aware(w.resets_at) is not None
            and _aware(w.resets_at) > stamp
        ]
        if exhausted:
            worst = max(exhausted, key=lambda w: _aware(w.resets_at))
            if set_pool_state(
                db,
                account,
                "cooling",
                actor=POOL_CONTROLLER_ACTOR,
                reason="window_exhausted",
                server_id=worst.server_id,
                detail={
                    "window_id": worst.window_id,
                    "pct": worst.pct,
                    "swap_at_pct": swap_at,
                },
                cooling_until=_aware(worst.resets_at),
                cooling_window_id=worst.window_id,
                now=stamp,
            ):
                changed += 1
            continue

        if account.pool_state == "cooling":
            cooling_until = _aware(account.cooling_until)
            if cooling_until is not None and stamp < cooling_until:
                continue  # 아직 충전 중.
            release, reason, detail = _cooling_release(
                account_windows, cooling_until, ready_return, stamp, grace
            )
            if not release:
                continue
            if set_pool_state(
                db,
                account,
                "ready",
                actor=POOL_CONTROLLER_ACTOR,
                reason=reason,
                detail=detail,
                now=stamp,
            ):
                changed += 1
            continue

        if set_pool_state(
            db,
            account,
            "ready",
            actor=POOL_CONTROLLER_ACTOR,
            reason="no_assignment",
            now=stamp,
        ):
            changed += 1
    return changed


def _cooling_release(
    account_windows: list[AccountUsageWindow],
    cooling_until: datetime | None,
    ready_return: float,
    now: datetime,
    grace: timedelta,
) -> tuple[bool, str, dict]:
    """``cooling_until`` 이 지난 계정을 배급처로 돌려보낼지 판정한다.

    시각만 믿으면 리셋 직후 다시 소진되는 계정을 못 막고, 관측만 믿으면 에이전트가
    죽었을 때 계정이 영영 갇힌다. 그래서 둘 다 본다 — 리셋 이후에 찍힌 관측이 복귀
    임계 이하면 즉시, 그런 관측이 없으면 유예를 넘긴 뒤에 풀어 준다.
    """
    if cooling_until is None:
        return True, "cooling_until_missing", {}
    fresh = [
        w
        for w in account_windows
        if (_aware(w.reported_at) or now) >= cooling_until and w.pct is not None
    ]
    if fresh:
        worst = max(w.pct for w in fresh)
        if worst <= ready_return:
            return True, "cooling_expired_observed", {
                "observed_pct": worst,
                "ready_return_pct": ready_return,
            }
        return False, "", {}
    if now >= cooling_until + grace:
        return True, "cooling_expired_unobserved", {
            "grace_minutes": int(grace.total_seconds() // 60)
        }
    return False, "", {}


# -- P1 권고 ------------------------------------------------------------------
def _max_pct(windows: list[AccountUsageWindow]) -> float:
    """마지막으로 알려진 최대 pct. 미상 창은 없는 셈 친다(표시·정렬용)."""
    return max((w.pct for w in windows if w.pct is not None), default=0.0)


def _window_pct(windows: list[AccountUsageWindow], window_id: str) -> float | None:
    for w in windows:
        if w.window_id == window_id:
            return w.pct
    return None


def _sort_pct(windows: list[AccountUsageWindow], window_id: str) -> tuple[int, float]:
    """정렬 키: 미상은 항상 뒤로. 0.0 으로 접으면 미상이 최우선 후보가 된다."""
    value = _window_pct(windows, window_id)
    return (1, 0.0) if value is None else (0, value)


def _all_pct_unknown(windows: list[AccountUsageWindow]) -> bool:
    """창 행이 있는데 그 pct 를 하나도 못 읽었는가.

    창이 아예 없는 계정(아직 한 번도 배급된 적 없는 새 계정)과 구별한다. 전자는
    "읽었는데 모르겠다"라 후보에서 빼야 하고, 후자는 "볼 기회가 없었다"라
    빼면 배급처가 영영 비지 않는다.
    """
    return bool(windows) and all(w.pct is None for w in windows)


def _unusable_account_ids(db: Session, tenant_id: uuid.UUID) -> set[uuid.UUID]:
    """``credential_unusable`` 또는 ``quarantine`` 경보가 열려 있는 계정(§2.4).

    자격증명이 못 쓰는 상태로 신고된 계정을 다시 배급하면 그 서버까지 같이 죽는다.
    격리(quarantine)를 같이 보는 이유도 같다 — 에이전트가 그 계정을 이미 쓰지
    못하고 있다는 1차 신고이므로, 컨트롤러가 그걸 무시하고 계속 물려 두면 서버는
    자격증명이 있는 채로 아무 일도 못 한다.
    """
    rows = db.execute(
        select(Alert.account_id).where(
            Alert.tenant_id == tenant_id,
            Alert.kind.in_(("credential_unusable", "quarantine")),
            Alert.status.in_(("open", "acked")),
            Alert.account_id.is_not(None),
        )
    ).all()
    return {r.account_id for r in rows}


# 창 개념이 없는 자격증명 유형. 사용률을 물어볼 수 없으니 컨트롤러가 언제 거둘지
# 판단할 근거가 없다(기획서 §4.7 — 자동화 밖에 둔다).
_WINDOWLESS_CREDENTIAL_TYPES = ("api_key",)


def ineligible_reason(
    account: Account,
    *,
    unusable: set[uuid.UUID],
    windows: list[AccountUsageWindow],
) -> str | None:
    """이 계정이 자동화 대상이 **될 수 없는** 이유. 될 수 있으면 None.

    후보 선정과 콘솔 표시가 같은 함수를 쓴다. 둘이 갈라지면 운영자는 "왜 안 뽑히지"를
    화면에서 알아낼 수 없고, 그 질문에 답하려고 로그를 뒤지게 된다.

    여기 담기는 것은 **지속적인** 사유뿐이다. 대여 중이거나 충전 중인 것은 순환의
    정상 국면이지 부적격이 아니므로, 그 판단은 ``_candidates`` 의 상태 필터가 한다.
    """
    if account.credential_type in _WINDOWLESS_CREDENTIAL_TYPES:
        return "api_key"
    if account.assignment_excluded:
        return "excluded"
    if account.status in _UNUSABLE_STATUSES or account.id in unusable:
        return "unusable"
    if account.pool_state == "pinned":
        return "pinned"
    if account.pool_state == "held":
        return "held"
    if _all_pct_unknown(windows):
        return "no_observation"
    return None


def _candidates(
    accounts: list[Account],
    *,
    live: dict[uuid.UUID, Assignment],
    windows: dict[uuid.UUID, list[AccountUsageWindow]],
    unusable: set[uuid.UUID],
    server_has_live: bool,
    replacing: bool = False,
) -> list[Account]:
    """배급처에서 뽑을 수 있는 계정 목록, 기획서 §2.4 의 순서로 정렬해 반환.

    정렬은 ① 7일 창 잔여 ② 5시간 창 잔여 ③ 마지막 대여 종료가 오래된 순이고, 동점은
    account_id 로 끊는다. 결정적이어야 하는 이유는 스윕이 30초마다 재진입하기 때문이다 —
    타이브레이크가 흔들리면 매 틱 다른 후보를 권고해 권고가 계속 지워졌다 생긴다.

    ``replacing`` 은 "나가는 계정을 대체할 자리를 찾는 중"이라는 뜻이다. 그때는
    Codex 의 서버당 1개 제한을 후보 필터로 쓰지 않는다 — 그 제한 때문에 후보가
    비면, 이미 소진된 Codex 계정을 물고 있는 서버는 영영 교체 상대를 못 찾는다.
    """
    epoch = datetime.min.replace(tzinfo=UTC)
    out = []
    for account in accounts:
        # 지속적인 부적격 사유(자격증명 유형·제외 플래그·사용 불가·운영자 상태·
        # 미상 관측)는 한 곳에서 판정한다 — 콘솔이 보여 주는 이유와 같은 함수다.
        if ineligible_reason(
            account, unusable=unusable, windows=windows.get(account.id, [])
        ) is not None:
            continue
        # 순환의 정상 국면(대여 중·회수 중·충전 중)은 부적격이 아니라 "지금은 아님"이다.
        if account.pool_state in ("cooling", "recalling", "leased"):
            continue
        if account.id in live:
            continue
        # Codex 는 서버당 1개 제한이라, 이미 뭔가 붙어 있는 서버에는 얹지 않는다.
        # 다만 대체 상대를 찾는 중이면 이 필터를 걸지 않는다(§8 — 나가는 계정의
        # 자리를 물려받을 후보까지 막으면 교체 자체가 불가능해진다).
        if account.provider == "codex" and server_has_live and not replacing:
            continue
        out.append(account)
    out.sort(
        key=lambda a: (
            _sort_pct(windows.get(a.id, []), WINDOW_SEVEN_DAY),
            _sort_pct(windows.get(a.id, []), WINDOW_FIVE_HOUR),
            _aware(a.last_lease_ended_at) or epoch,
            str(a.id),
        )
    )
    return out


def _lease_started_at(assignment: Assignment) -> datetime:
    return _aware(assignment.delivered_at) or _aware(assignment.created_at) or _now()


def _desired_recommendation(
    server: Server,
    *,
    policy: dict[str, Any],
    leased: list[Account],
    live: dict[uuid.UUID, Assignment],
    windows: dict[uuid.UUID, list[AccountUsageWindow]],
    candidates: list[Account],
    replacements: list[Account],
    unusable: set[uuid.UUID],
    stale_after: timedelta,
    now: datetime,
) -> dict | None:
    """이 서버에 대해 지금 참인 권고 한 건(또는 없음).

    서버당 최대 한 건이다. 기획서 §4.2 의 in-flight 규칙 때문에 어차피 한 번에 한
    체인만 실행할 수 있으므로, 여러 건을 쌓아 두면 운영자에게 실행할 수 없는 선택지를
    보여 주는 셈이 된다. 판정 순서는 급한 것부터: 빈 서버 → **사용 불가 계정 교체**
    → 소진 교체 → 초과분 회수 → 예열.

    사용 불가 교체가 소진 교체보다 앞인 이유는, 소진은 시간이 지나면 풀리지만 죽은
    자격증명은 풀리지 않기 때문이다. 그 계정을 물고 있는 서버는 지금 이 순간
    아무것도 못 하고 있고, ``min_lease_minutes`` 같은 안정화 장치도 여기서는
    적용하지 않는다 — 붙잡아 둘 이유가 없다.

    임계 판정에는 **신선한** 관측만 쓴다. 낡은 pct 로 교체를 걸면 이미 리셋된
    계정을 거두거나, 반대로 소진된 계정을 계속 물린 채 두게 된다.
    """
    target = int(policy["target_leases"])
    swap_at = float(policy["swap_at_pct"])
    prefetch_at = float(policy["prefetch_at_pct"])
    min_lease = timedelta(minutes=float(policy["min_lease_minutes"]))

    if not leased:
        if not candidates:
            return None
        pick = candidates[0]
        return {
            "kind": "lease",
            "from_account_id": None,
            "to_account_id": pick.id,
            "trigger_pct": None,
            "reason": f"서버에 대여 중인 계정이 없다. 배급처에서 {pick.email}을 내보낸다.",
        }

    def _fresh(account: Account) -> float | None:
        return _max_fresh_pct(windows.get(account.id, []), now, stale_after)

    def _gap_note(target_account: Account) -> str:
        """이 교체가 무자격 공백을 만들면 그 사실을 권고 문장에 적는다.

        Codex 는 호스트당 자격증명이 하나라(에이전트 bridge 의 identity sidecar가
        두 번째 email 을 거부한다) 새 계정을 미리 올려 둘 수 없다. 그래서 이
        조합만 회수부터 하고, 그 사이 서버는 잠깐 아무 계정도 갖지 않는다.
        운영자가 버튼을 누르기 전에 알아야 하는 대가다.
        """
        if target_account.id in {a.id for a in leased}:
            return ""
        if target_account.provider != "codex":
            return ""
        if not any(a.provider == "codex" for a in leased):
            return ""
        return (
            " Codex 는 호스트당 계정이 하나뿐이라 먼저 거두고 나서 올린다 — "
            "그 사이 이 서버에는 활성 계정이 없다."
        )

    def _handover_target(exclude: set[uuid.UUID]) -> Account | None:
        """넘겨받을 계정. 이미 서버에 올라와 있는 여유분이 1순위다.

        서버에 설치된 계정으로 넘기면 자격증명 재전송이 없고, 체인도 전환부터
        시작한다. 없으면 배급처에서 뽑는다 — 그 경우 체인이 전달부터 돈다.
        """
        spare = [
            a
            for a in leased
            if a.id not in exclude and (_fresh(a) is not None and _fresh(a) < prefetch_at)
        ]
        if spare:
            return spare[0]
        return replacements[0] if replacements else None

    broken = sorted(
        (a for a in leased if a.id in unusable or a.status in _UNUSABLE_STATUSES),
        key=lambda a: str(a.id),
    )
    if broken:
        worst = broken[0]
        target_account = _handover_target({a.id for a in broken})
        if target_account is None:
            return None
        return {
            "kind": "swap",
            "from_account_id": worst.id,
            "to_account_id": target_account.id,
            "trigger_pct": None,
            "reason": (
                f"{worst.email}이 사용 불가(unusable) 상태다 — 격리됐거나 자격증명이 "
                f"죽었다. {target_account.email}로 전환하고 거둔다."
                + _gap_note(target_account)
            ),
        }

    hot = [
        a
        for a in leased
        if (_fresh(a) is not None and _fresh(a) >= swap_at)
        and now - _lease_started_at(live[a.id]) >= min_lease
    ]
    if hot:
        worst = max(hot, key=lambda a: _fresh(a) or 0.0)
        target_account = _handover_target({worst.id})
        if target_account is None:
            return None
        pct = _fresh(worst) or 0.0
        return {
            "kind": "swap",
            "from_account_id": worst.id,
            "to_account_id": target_account.id,
            "trigger_pct": pct,
            "reason": (
                f"{worst.email}이 {pct:.0f}%로 교체 임계({swap_at:.0f}%)를 넘겼다. "
                f"{target_account.email}로 전환한다."
                + _gap_note(target_account)
            ),
        }

    if len(leased) > target:
        # 초과분은 남은 여유가 가장 적은 계정부터 거둔다.
        drop = max(leased, key=lambda a: (_max_pct(windows.get(a.id, [])), str(a.id)))
        if now - _lease_started_at(live[drop.id]) < min_lease:
            return None
        return {
            "kind": "recall_idle",
            "from_account_id": drop.id,
            "to_account_id": None,
            "trigger_pct": _max_pct(windows.get(drop.id, [])),
            "reason": (
                f"대여 계정이 {len(leased)}개로 목표({target}개)를 넘겼다. "
                f"{drop.email}을 배급처로 거둔다."
            ),
        }

    if len(leased) == target and candidates:
        warm = max(leased, key=lambda a: _fresh(a) or -1.0)
        pct = _fresh(warm)
        if pct is not None and pct >= prefetch_at:
            pick = candidates[0]
            return {
                "kind": "prefetch",
                "from_account_id": warm.id,
                "to_account_id": pick.id,
                "trigger_pct": pct,
                "reason": (
                    f"{warm.email}이 {pct:.0f}%로 예열 임계({prefetch_at:.0f}%)에 닿았다. "
                    f"{pick.email}을 미리 올려 둔다."
                ),
            }
    return None


def build_recommendations(
    db: Session, tenant_id: uuid.UUID, *, now: datetime | None = None
) -> int:
    """한 테넌트의 ``mode=auto`` 서버에 대해 권고를 재계산한다.

    권고는 조건의 투영이므로 매 틱 "지금 참인 것"과 저장된 것을 맞춘다 — 참인 것이
    없거나 달라졌으면 기존 행을 지우고, 같으면 그대로 둔다(``created_at`` 이 조건의
    시작 시각을 유지해야 하므로 갱신도 하지 않는다).

    caller 가 커밋한다. 반환값은 새로 만든 권고 수.
    """
    stamp = _aware(now) or _now()
    servers = list(
        db.scalars(select(Server).where(Server.tenant_id == tenant_id).order_by(Server.id)).all()
    )
    accounts = list(
        db.scalars(select(Account).where(Account.tenant_id == tenant_id).order_by(Account.id)).all()
    )
    by_id = {a.id: a for a in accounts}
    live = _live_assignments(db, tenant_id)
    windows = _windows_by_account(db, tenant_id)
    unusable = _unusable_account_ids(db, tenant_id)
    # 체인이 도는 서버에는 권고를 만들지 않는다. 실행 중인 계획과 "지금이라면
    # 이렇게 하겠다"가 나란히 보이면 운영자는 실행할 수 없는 버튼을 누르게 된다.
    busy = _servers_with_active_chain(db, tenant_id)
    stale_after = _stale_after()

    created = 0
    for server in servers:
        policy = resolve_policy(server)
        server_live = [a for a in live.values() if a.server_id == server.id]
        desired: dict | None = None
        # 관측과 권고는 **항상** 한다. mode=manual 이든 테넌트가 일시정지 중이든
        # "지금이라면 이렇게 하겠다"는 여전히 참이고, 오히려 자동 실행이 꺼져 있을
        # 때야말로 운영자가 그 판단을 손으로 실행할 수 있어야 한다. mode/paused 는
        # 실행 게이트(start_auto_chains)에서만 본다.
        #
        # 오프라인 서버만 예외다. 명령은 큐에 쌓일 뿐 전달되지 않는데 운영자에게는
        # "누르면 된다"처럼 보이는 버튼이 떠 있게 된다. 이미 도는 체인은 여기서
        # 건드리지 않는다 — 단계 타임아웃이 접는다.
        if server.id not in busy and server.status != "offline":
            in_flight = any(a.state in _IN_FLIGHT_STATES for a in server_live)
            if not in_flight:
                leased = [
                    by_id[a.account_id]
                    for a in server_live
                    if a.account_id in by_id
                ]
                leased.sort(key=lambda a: str(a.id))
                desired = _desired_recommendation(
                    server,
                    policy=policy,
                    leased=leased,
                    live=live,
                    windows=windows,
                    candidates=_candidates(
                        accounts,
                        live=live,
                        windows=windows,
                        unusable=unusable,
                        server_has_live=bool(server_live),
                    ),
                    replacements=_candidates(
                        accounts,
                        live=live,
                        windows=windows,
                        unusable=unusable,
                        server_has_live=bool(server_live),
                        replacing=True,
                    ),
                    unusable=unusable,
                    stale_after=stale_after,
                    now=stamp,
                )
        created += _reconcile_recommendation(
            db, tenant_id=tenant_id, server_id=server.id, desired=desired
        )
    return created


def _reconcile_recommendation(
    db: Session, *, tenant_id: uuid.UUID, server_id: uuid.UUID, desired: dict | None
) -> int:
    """이 서버의 저장된 권고를 ``desired`` 한 건(또는 없음)에 맞춘다."""
    existing = list(
        db.scalars(
            select(PoolRecommendation).where(PoolRecommendation.server_id == server_id)
        ).all()
    )
    match = None
    if desired is not None:
        for row in existing:
            if (
                row.kind == desired["kind"]
                and row.from_account_id == desired["from_account_id"]
                and row.to_account_id == desired["to_account_id"]
            ):
                match = row
                break
    stale = [r for r in existing if r is not match]
    if stale:
        # 권고가 사라지는 것도 사건이다. 운영자가 화면에서 본 버튼이 다음 새로고침에
        # 없어졌을 때, 조건이 해소된 것인지 다른 조건으로 바뀐 것인지 여기 말고는
        # 답할 곳이 없다(체인으로 소비된 경우는 start_chain 이 chain_started 를 남긴다).
        for row in stale:
            record_event(
                db,
                tenant_id=tenant_id,
                kind="recommendation_dropped",
                server_id=server_id,
                account_id=row.to_account_id or row.from_account_id,
                detail={
                    "recommendation_id": str(row.id),
                    "kind": row.kind,
                    "from_account_id": str(row.from_account_id)
                    if row.from_account_id
                    else None,
                    "to_account_id": str(row.to_account_id) if row.to_account_id else None,
                    "trigger_pct": row.trigger_pct,
                    "replaced_by": desired["kind"] if desired is not None else None,
                },
            )
        db.execute(
            delete(PoolRecommendation).where(
                PoolRecommendation.id.in_([r.id for r in stale])
            )
        )
    if desired is None or match is not None:
        return 0
    db.add(
        PoolRecommendation(
            tenant_id=tenant_id,
            server_id=server_id,
            kind=desired["kind"],
            from_account_id=desired["from_account_id"],
            to_account_id=desired["to_account_id"],
            reason=desired["reason"],
            trigger_pct=desired["trigger_pct"],
        )
    )
    record_event(
        db,
        tenant_id=tenant_id,
        kind="recommendation_created",
        server_id=server_id,
        account_id=desired["to_account_id"] or desired["from_account_id"],
        detail={
            "kind": desired["kind"],
            "from_account_id": str(desired["from_account_id"])
            if desired["from_account_id"]
            else None,
            "to_account_id": str(desired["to_account_id"])
            if desired["to_account_id"]
            else None,
            "trigger_pct": desired["trigger_pct"],
            "reason": desired["reason"],
        },
    )
    return 1


# -- 30초 형제 스윕 -----------------------------------------------------------
def sweep_pool(db: Session, *, now: datetime | None = None) -> int:
    """관측(상태·권고) → 체인 전진 → 자동 착수, 이 순서로 한 틱.

    관측 구간만 …0D 락 안의 한 트랜잭션이다. 그 뒤 두 구간은 명령 발행 서비스가
    스스로 커밋하므로 같은 트랜잭션에 담기지 않고, …0E 락을 걸음마다 새로 잡는다.

    순서에는 이유가 있다. 권고를 먼저 갱신해야 조건이 사라진 권고로 체인이
    시작되지 않고, 체인을 먼저 전진시켜야 방금 끝난 체인의 서버가 같은 틱에
    다음 체인을 받을 수 있다.
    """
    stamp = _aware(now) or _now()
    total = 0
    if _try_advisory_xact_lock(db, _POOL_SWEEP_LOCK_KEY):
        tenant_ids = list(db.scalars(select(Tenant.id).order_by(Tenant.id)).all())
        for tenant_id in tenant_ids:
            # 테넌트마다 자기 트랜잭션이다. 한 테넌트의 데이터가 계산을 터뜨렸을 때
            # 그 틱의 다른 테넌트 계산까지 롤백되면, 고장 난 테넌트 하나가 전체
            # 자동화를 인질로 잡는다. 락은 커밋에서 놓이므로 다음 테넌트 앞에서
            # 다시 잡되, 못 잡으면 이번 틱의 나머지는 다른 인스턴스 몫이다.
            if not _try_advisory_xact_lock(db, _POOL_SWEEP_LOCK_KEY):
                break
            try:
                total += compute_states(db, tenant_id, now=stamp)
                total += build_recommendations(db, tenant_id, now=stamp)
                db.commit()
            except Exception:  # noqa: BLE001 - 한 테넌트의 실패가 나머지를 막지 않는다
                db.rollback()
                _logger.warning("pool sweep failed for tenant %s", tenant_id, exc_info=False)
    total += advance_chains(db, now=stamp)
    total += start_auto_chains(db, now=stamp)
    return total


# -- P2 체인 실행기 -----------------------------------------------------------
# 체인이 아직 진행 중인 단계들. done/failed 는 종착이라 스윕이 다시 보지 않는다.
CHAIN_ACTIVE_STEPS = ("deliver", "switch", "recall")
# 에이전트에 설치가 끝난 배정 — switch_now 와 recall 이 요구하는 상태(§6.3).
_INSTALLED_STATES = ("active", "inactive")


def _chain_timeout() -> timedelta:
    return timedelta(minutes=get_settings().pool_chain_step_timeout_minutes)


def _servers_with_active_chain(db: Session, tenant_id: uuid.UUID) -> set[uuid.UUID]:
    return set(
        db.scalars(
            select(PoolChain.server_id).where(
                PoolChain.tenant_id == tenant_id,
                PoolChain.step.in_(CHAIN_ACTIVE_STEPS),
            )
        ).all()
    )


def active_chain_for_server(db: Session, server_id: uuid.UUID) -> PoolChain | None:
    """이 서버에서 지금 도는 체인. 서버당 최대 하나라는 규칙의 판정자다.

    둘 이상이 돌면 두 체인이 같은 배정을 서로 다른 방향으로 밀게 되고, reconcile 의
    CORRECTION_CAP 이 그 싸움을 3회에서 끊어 버려 어느 쪽도 수렴하지 않는다.
    """
    return db.scalars(
        select(PoolChain)
        .where(PoolChain.server_id == server_id, PoolChain.step.in_(CHAIN_ACTIVE_STEPS))
        .order_by(PoolChain.started_at)
        .limit(1)
    ).first()


def unacked_failed_chain(db: Session, server_id: uuid.UUID) -> PoolChain | None:
    """운영자가 아직 확인하지 않은 실패 체인.

    이게 남아 있는 동안 이 서버의 **자동** 실행은 멈춘다. 실패의 원인이 관측이든
    에이전트든 그대로인 채 컨트롤러가 30초마다 같은 계획을 다시 밀면, 실패가
    쌓이는 속도만 빨라지고 사람이 볼 것은 늘지 않는다.
    """
    return db.scalars(
        select(PoolChain)
        .where(
            PoolChain.server_id == server_id,
            PoolChain.step == "failed",
            PoolChain.acked_at.is_(None),
        )
        .order_by(PoolChain.started_at.desc())
        .limit(1)
    ).first()


def _live_assignment(
    db: Session, tenant_id: uuid.UUID, account_id: uuid.UUID | None
) -> Assignment | None:
    if account_id is None:
        return None
    return db.scalars(
        select(Assignment).where(
            Assignment.tenant_id == tenant_id,
            Assignment.account_id == account_id,
            Assignment.state != _DETACHED,
        )
    ).first()


def _chain_detail(chain: PoolChain, **extra) -> dict:
    detail = {
        "chain_id": str(chain.id),
        "kind": chain.kind,
        "step": chain.step,
        "recommendation_id": str(chain.recommendation_id)
        if chain.recommendation_id
        else None,
        "from_account_id": str(chain.from_account_id) if chain.from_account_id else None,
        "to_account_id": str(chain.to_account_id) if chain.to_account_id else None,
    }
    detail.update(extra)
    return detail


def _touch(db: Session, chain: PoolChain, *, step: str, now: datetime, **detail) -> bool:
    """체인을 다음 단계로 옮기고 감사 한 줄을 남긴다. 항상 True(무언가 일어났다).

    ``updated_at`` 은 여기서만 움직인다 — 그래서 단계 타임아웃이 "이 단계에 들어온
    뒤 흐른 시간"을 정확히 뜻하고, 아무 일도 없는 틱이 시계를 되감지 못한다.
    """
    chain.step = step
    chain.updated_at = now
    record_event(
        db,
        tenant_id=chain.tenant_id,
        kind="chain_step",
        server_id=chain.server_id,
        account_id=chain.to_account_id or chain.from_account_id,
        actor=chain.actor,
        detail=_chain_detail(chain, **detail),
    )
    return True


def _finish(db: Session, chain: PoolChain, *, now: datetime, **detail) -> bool:
    chain.step = "done"
    chain.updated_at = now
    record_event(
        db,
        tenant_id=chain.tenant_id,
        kind="chain_done",
        server_id=chain.server_id,
        account_id=chain.to_account_id or chain.from_account_id,
        actor=chain.actor,
        detail=_chain_detail(chain, **detail),
    )
    return True


def _fail(db: Session, chain: PoolChain, reason: str, *, now: datetime, **detail) -> bool:
    """체인을 실패로 접고 경보를 연다. **롤백 명령은 내지 않는다.**

    되돌리는 판단은 사람 몫이다. 실패한 이유가 "에이전트가 응답하지 않는다"라면
    자동 롤백은 응답하지 않는 에이전트에 명령을 하나 더 쌓을 뿐이고, 실패가 부분
    성공(예: deliver 는 됐고 switch 만 안 됨)이라면 되돌리는 쪽이 오히려 더 큰
    변경이다. 컨트롤러가 할 수 있는 정직한 일은 멈추고 알리는 것뿐이다.
    """
    failed_at_step = chain.step
    chain.step = "failed"
    chain.error = reason
    chain.updated_at = now
    record_event(
        db,
        tenant_id=chain.tenant_id,
        kind="chain_failed",
        server_id=chain.server_id,
        account_id=chain.to_account_id or chain.from_account_id,
        actor=chain.actor,
        detail=_chain_detail(chain, failed_step=failed_at_step, error=reason, **detail),
    )
    alerts_service.open_alert(
        db,
        tenant_id=chain.tenant_id,
        server_id=chain.server_id,
        kind="pool_chain_failed",
        severity="warning",
        detail=_chain_detail(chain, failed_step=failed_at_step, error=reason, **detail),
    )
    return True


def _expired(chain: PoolChain, now: datetime) -> bool:
    return now - (_aware(chain.updated_at) or now) >= _chain_timeout()


def _deliver_already_issued(db: Session, chain: PoolChain) -> bool:
    """이 체인의 대상 계정에 전달 명령이 이미 나간 적이 있는가.

    배정이 없을 때 그것이 *아직 안 만든* 것인지 *만들었다가 사라진* 것인지를
    가르는 증거다. 시각 비교로는 못 가른다 — Codex 변형은 회수를 끝낸 **뒤에**
    전달 단계로 들어오므로 "체인이 막 시작했는가"가 답이 되지 않는다. 발행된
    명령 행은 배정이 detached 로 지워져도 남으므로 이 질문에 정확히 답한다.
    """
    assignment_ids = list(
        db.scalars(
            select(Assignment.id).where(
                Assignment.tenant_id == chain.tenant_id,
                Assignment.server_id == chain.server_id,
                Assignment.account_id == chain.to_account_id,
            )
        ).all()
    )
    if not assignment_ids:
        return False
    return (
        db.scalars(
            select(AgentCommand.id).where(
                AgentCommand.assignment_id.in_(assignment_ids),
                AgentCommand.command_type == "deliver",
            )
        ).first()
        is not None
    )


# -- 단계별 전진 --------------------------------------------------------------
def _advance_deliver(db: Session, chain: PoolChain, now: datetime) -> bool:
    assignment = _live_assignment(db, chain.tenant_id, chain.to_account_id)

    if assignment is None:
        if _deliver_already_issued(db, chain):
            return _fail(db, chain, "전달한 배정이 사라졌다(회수 또는 삭제).", now=now)
        try:
            assignment = inventory.create_assignment(
                db,
                chain.tenant_id,
                account_id=chain.to_account_id,
                server_id=chain.server_id,
                pinned=False,
            )
        except ApiError as exc:
            # 배정 생성이 거부되는 이유(다른 서버에 이미 붙음, assignment_excluded,
            # Codex 서버당 1개)는 재시도로 풀리지 않는다 — 즉시 실패로 접는다.
            return _fail(db, chain, f"배정 생성 거부: {exc.code}", now=now)
        if _expired(chain, now):
            return _fail(db, chain, "deliver 단계 제한 시간 초과", now=now)

    if assignment.server_id != chain.server_id:
        return _fail(db, chain, "대상 계정이 다른 서버에 배정돼 있다.", now=now)

    if assignment.state == "pending":
        commands.request_deliver(db, chain.tenant_id, assignment.id)
        return _touch(db, chain, step="deliver", now=now, assignment_id=str(assignment.id))

    if assignment.state in _INSTALLED_STATES:
        if chain.kind == "prefetch":
            return _finish(db, chain, now=now, assignment_id=str(assignment.id))
        return _touch(db, chain, step="switch", now=now, assignment_id=str(assignment.id))

    if assignment.state == "delivering":
        if _expired(chain, now):
            return _fail(db, chain, "deliver 단계 제한 시간 초과", now=now)
        return False

    return _fail(db, chain, f"배정이 예상 밖 상태다: {assignment.state}", now=now)


def _newest_switch_command(db: Session, assignment_id: uuid.UUID) -> AgentCommand | None:
    return db.scalars(
        select(AgentCommand)
        .where(
            AgentCommand.assignment_id == assignment_id,
            AgentCommand.command_type == "switch_now",
        )
        .order_by(AgentCommand.created_at.desc(), AgentCommand.id.desc())
        .limit(1)
    ).first()


def _advance_switch(db: Session, chain: PoolChain, now: datetime) -> bool:
    assignment = _live_assignment(db, chain.tenant_id, chain.to_account_id)
    if assignment is None or assignment.state not in _INSTALLED_STATES:
        state = assignment.state if assignment is not None else "없음"
        return _fail(db, chain, f"전환 대상 배정이 설치 상태가 아니다: {state}", now=now)

    if chain.command_id is None:
        commands.request_switch_now(db, chain.tenant_id, assignment.id)
        issued = _newest_switch_command(db, assignment.id)
        if issued is None:  # pragma: no cover - enqueue 가 항상 한 줄을 남긴다
            return _fail(db, chain, "switch_now 명령을 찾을 수 없다.", now=now)
        chain.command_id = issued.command_id
        return _touch(db, chain, step="switch", now=now, command_id=issued.command_id)

    command = db.scalars(
        select(AgentCommand).where(AgentCommand.command_id == chain.command_id)
    ).first()
    if command is None:
        return _fail(db, chain, "발행한 switch_now 명령이 사라졌다.", now=now)
    if command.status == "acked":
        # lease 는 여기서 끝이고, swap 은 이제 이전 계정을 거둔다 — 다만 회수부터
        # 시작한 변형에서는 이미 거둔 뒤이므로 여기서 끝난다.
        if (
            chain.kind == "swap"
            and _live_assignment(db, chain.tenant_id, chain.from_account_id) is not None
        ):
            return _touch(db, chain, step="recall", now=now, command_id=command.command_id)
        return _finish(db, chain, now=now, command_id=command.command_id)
    if command.status == "failed":
        return _fail(db, chain, f"switch_now 실패: {command.detail or '사유 없음'}", now=now)
    if _expired(chain, now):
        return _fail(db, chain, "switch 단계 제한 시간 초과", now=now)
    return False


def _advance_recall(db: Session, chain: PoolChain, now: datetime) -> bool:
    assignment = _live_assignment(db, chain.tenant_id, chain.from_account_id)
    if assignment is None:
        # detached 까지 갔다. 공평 순환의 기준점을 찍는다 — compute_states 도 같은
        # 값을 쓰지만, 그쪽은 다음 스윕에나 돌므로 여기서 먼저 확정한다.
        account = db.get(Account, chain.from_account_id) if chain.from_account_id else None
        if account is not None and account.tenant_id == chain.tenant_id:
            account.last_lease_ended_at = now
            account.updated_at = now
        # 회수부터 시작한 swap(Codex 변형)이면 이제서야 자리가 비었다 — 올린다.
        # 대상이 이미 설치돼 있으면 이 회수가 마지막 단계였다는 뜻이라 끝난다.
        if chain.kind == "swap" and _live_assignment(db, chain.tenant_id, chain.to_account_id) is None:
            return _touch(db, chain, step="deliver", now=now, after="recall_first")
        return _finish(db, chain, now=now)

    if assignment.state == "recalling":
        if _expired(chain, now):
            return _fail(db, chain, "recall 단계 제한 시간 초과", now=now)
        return False

    if assignment.state in _INSTALLED_STATES or assignment.state == "delivering":
        try:
            commands.request_recall(db, chain.tenant_id, assignment.id)
        except ApiError as exc:
            return _fail(db, chain, f"회수 거부: {exc.code}", now=now)
        return _touch(db, chain, step="recall", now=now, assignment_id=str(assignment.id))

    return _fail(db, chain, f"회수 대상이 예상 밖 상태다: {assignment.state}", now=now)


_STEP_HANDLERS = {
    "deliver": _advance_deliver,
    "switch": _advance_switch,
    "recall": _advance_recall,
}


def advance_chain(db: Session, chain: PoolChain, *, now: datetime | None = None) -> bool:
    """체인 하나를 한 걸음 전진시킨다. 무언가 바뀌었으면 True.

    커밋하지 않는다 — 다만 이 안에서 부르는 명령 서비스(create_assignment /
    request_deliver / request_recall / request_switch_now)가 스스로 커밋하므로,
    체인 행 변경은 그 커밋에 함께 실린다. 명령을 낸 뒤 체인 단계를 못 적는 창을
    없애려는 순서다.
    """
    stamp = _aware(now) or _now()
    handler = _STEP_HANDLERS.get(chain.step)
    if handler is None:
        return False
    return handler(db, chain, stamp)


def advance_chains(db: Session, *, now: datetime | None = None) -> int:
    """활성 체인 전부를 한 걸음씩. 자체 advisory 락(…0E), 걸음마다 새로 잡는다.

    락을 한 번만 잡을 수 없는 이유는 명령 서비스가 커밋하기 때문이다 — 트랜잭션
    범위 락은 그 순간 풀린다. 그래서 보호 단위를 "체인 한 걸음"으로 잡는다.
    한 걸음 안에서는 판단(배정 상태 읽기)과 발행(명령 INSERT)이 같은 트랜잭션에
    있으므로, 두 인스턴스가 같은 체인에 명령을 겹쳐 내는 일은 없다.
    """
    stamp = _aware(now) or _now()
    chain_ids = [
        c
        for c in db.scalars(
            select(PoolChain.id)
            .where(PoolChain.step.in_(CHAIN_ACTIVE_STEPS))
            .order_by(PoolChain.started_at, PoolChain.id)
        ).all()
    ]
    db.commit()
    moved = 0
    for chain_id in chain_ids:
        if not _try_advisory_xact_lock(db, _POOL_CHAIN_LOCK_KEY):
            break
        db.expire_all()
        chain = db.get(PoolChain, chain_id)
        if chain is None or chain.step not in CHAIN_ACTIVE_STEPS:
            db.commit()
            continue
        if advance_chain(db, chain, now=stamp):
            moved += 1
        db.commit()
    return moved


# -- 착수(수동 :apply / 자동) --------------------------------------------------
def start_chain(
    db: Session,
    recommendation: PoolRecommendation,
    *,
    actor: str,
    now: datetime | None = None,
) -> PoolChain:
    """권고 한 건을 체인으로 바꾸고 첫 단계를 발행한다. 커밋한다.

    권고 행은 여기서 지운다. 권고는 "조건의 투영"이고 그 조건에 대해 사람이 이미
    결정을 내렸으므로, 남겨 두면 같은 계획을 두 번 실행할 수 있는 버튼이 콘솔에
    계속 떠 있게 된다.
    """
    stamp = _aware(now) or _now()
    tenant_id = recommendation.tenant_id
    server_id = recommendation.server_id

    if active_chain_for_server(db, server_id) is not None:
        raise conflict(
            "pool.chain_active",
            "이 서버에는 이미 실행 중인 체인이 있다. 끝나기를 기다려라.",
        )
    live = _live_assignments(db, tenant_id)
    server_live = [a for a in live.values() if a.server_id == server_id]
    if any(a.state in _IN_FLIGHT_STATES for a in server_live):
        raise conflict(
            "pool.server_in_flight",
            "이 서버에 전달·회수가 진행 중인 배정이 있다.",
        )

    kind = recommendation.kind
    if kind in ("lease", "prefetch", "swap") and recommendation.to_account_id is None:
        raise conflict("pool.recommendation_invalid", f"{kind} 권고에 대상 계정이 없다.")
    if kind in ("swap", "recall_idle") and recommendation.from_account_id is None:
        raise conflict("pool.recommendation_invalid", f"{kind} 권고에 회수 계정이 없다.")
    swap_first_step = "switch"
    if kind == "swap":
        target = live.get(recommendation.to_account_id)
        if target is not None and target.server_id != server_id:
            raise conflict(
                "pool.swap_target_elsewhere",
                "교체 대상 계정이 다른 서버에 배정돼 있다. 그 배정을 먼저 회수하라.",
            )
        if target is None:
            # 아직 서버에 없는 계정으로 넘긴다 — 먼저 올려야 한다. Codex 만 예외로
            # 자리를 비우고 나서 올린다(호스트당 자격증명 하나). 그 순서에서는
            # 서버가 잠깐 무자격이 되는데, 대안이 "영영 못 바꾼다"이므로 감수한다.
            to_account = db.get(Account, recommendation.to_account_id)
            held_accounts = [db.get(Account, a.account_id) for a in server_live]
            server_has_codex = any(
                acc is not None and acc.provider == "codex" for acc in held_accounts
            )
            if (
                to_account is not None
                and to_account.provider == "codex"
                and server_has_codex
            ):
                swap_first_step = "recall"
            else:
                swap_first_step = "deliver"
        elif target.state not in _INSTALLED_STATES:
            raise conflict(
                "pool.swap_target_not_installed",
                f"교체 대상 배정이 아직 설치되지 않았다(state={target.state}). "
                "전달이 끝난 뒤에 다시 실행하라.",
            )
    if kind == "recall_idle":
        source = live.get(recommendation.from_account_id)
        if source is None or source.server_id != server_id:
            raise conflict(
                "pool.recall_source_missing",
                "회수 대상 계정이 이 서버에 배정돼 있지 않다.",
            )

    first_step = {"lease": "deliver", "prefetch": "deliver", "swap": swap_first_step}.get(
        kind, "recall"
    )
    chain = PoolChain(
        tenant_id=tenant_id,
        server_id=server_id,
        recommendation_id=recommendation.id,
        kind=kind,
        from_account_id=recommendation.from_account_id,
        to_account_id=recommendation.to_account_id,
        step=first_step,
        actor=actor,
        started_at=stamp,
        updated_at=stamp,
    )
    db.add(chain)
    db.flush()
    record_event(
        db,
        tenant_id=tenant_id,
        kind="chain_started",
        server_id=server_id,
        account_id=recommendation.to_account_id or recommendation.from_account_id,
        actor=actor,
        detail=_chain_detail(
            chain,
            reason=recommendation.reason,
            trigger_pct=recommendation.trigger_pct,
        ),
    )
    db.execute(
        delete(PoolRecommendation).where(PoolRecommendation.id == recommendation.id)
    )
    db.commit()

    advance_chain(db, chain, now=stamp)
    db.commit()
    db.refresh(chain)
    return chain


def ack_chain(
    db: Session, chain: PoolChain, *, actor: str, now: datetime | None = None
) -> PoolChain:
    """실패한 체인을 운영자가 확인했다고 표시하고, 그 서버의 자동 실행 빗장을 푼다."""
    stamp = _aware(now) or _now()
    if chain.step != "failed":
        raise conflict(
            "pool.chain_not_failed",
            f"확인은 실패한 체인에만 쓴다. 이 체인은 {chain.step} 이다.",
        )
    if chain.acked_at is None:
        chain.acked_at = stamp
        chain.updated_at = stamp
        record_event(
            db,
            tenant_id=chain.tenant_id,
            kind="chain_step",
            server_id=chain.server_id,
            account_id=chain.to_account_id or chain.from_account_id,
            actor=actor,
            detail=_chain_detail(chain, action="ack"),
        )
        alerts_service.resolve(db, server_id=chain.server_id, kind="pool_chain_failed")
        db.commit()
        db.refresh(chain)
    return chain


# -- P3 자동 모드 -------------------------------------------------------------
def start_auto_chains(db: Session, *, now: datetime | None = None) -> int:
    """``mode=auto`` 서버의 권고를 컨트롤러 이름으로 착수한다. 착수한 수를 돌려준다.

    세 개의 빗장을 모두 지나야 한 건이 시작된다. 테넌트가 일시정지되지 않았을 것,
    그 서버에 도는 체인도 미확인 실패 체인도 없을 것, 그리고 테넌트의 동시 자동
    체인이 상한 미만일 것. 상한은 "관측이 한꺼번에 틀렸을 때 몇 대까지 흔들려도
    되는가"에 대한 답이고, 운영자가 손으로 연 체인은 세지 않는다 — 사람이 누른
    버튼까지 컨트롤러의 예산으로 묶으면 사고 대응이 막힌다.
    """
    stamp = _aware(now) or _now()
    cap = get_settings().pool_max_concurrent_chains
    started = 0
    tenant_ids = list(db.scalars(select(Tenant.id).order_by(Tenant.id)).all())
    db.commit()
    for tenant_id in tenant_ids:
        db.expire_all()
        tenant = db.get(Tenant, tenant_id)
        if tenant is None or tenant.pool_automation_paused:
            db.commit()
            continue
        running = _auto_chain_count(db, tenant_id)
        recommendations = list(
            db.scalars(
                select(PoolRecommendation)
                .where(PoolRecommendation.tenant_id == tenant_id)
                .order_by(PoolRecommendation.created_at, PoolRecommendation.id)
            ).all()
        )
        db.commit()
        for recommendation in recommendations:
            if running >= cap:
                break
            if not _try_advisory_xact_lock(db, _POOL_CHAIN_LOCK_KEY):
                return started
            db.expire_all()
            fresh = db.get(PoolRecommendation, recommendation.id)
            if fresh is None or not _auto_eligible(db, fresh, tenant_id):
                db.commit()
                continue
            try:
                start_chain(db, fresh, actor=POOL_CONTROLLER_ACTOR, now=stamp)
            except ApiError:
                # 착수 조건이 방금 깨졌다(다른 인스턴스가 먼저 잡았거나 배정이
                # 움직였다). 권고는 그대로 두고 다음 틱에 다시 본다.
                db.rollback()
                continue
            running += 1
            started += 1
    return started


def _auto_chain_count(db: Session, tenant_id: uuid.UUID) -> int:
    return len(
        db.scalars(
            select(PoolChain.id).where(
                PoolChain.tenant_id == tenant_id,
                PoolChain.step.in_(CHAIN_ACTIVE_STEPS),
                PoolChain.actor == POOL_CONTROLLER_ACTOR,
            )
        ).all()
    )


def _auto_eligible(
    db: Session, recommendation: PoolRecommendation, tenant_id: uuid.UUID
) -> bool:
    server = db.get(Server, recommendation.server_id)
    if server is None or server.tenant_id != tenant_id:
        return False
    if resolve_policy(server)["mode"] != "auto":
        return False
    if active_chain_for_server(db, server.id) is not None:
        return False
    if unacked_failed_chain(db, server.id) is not None:
        return False
    return True


def set_automation_paused(
    db: Session,
    tenant_id: uuid.UUID,
    *,
    paused: bool,
    actor: str,
    now: datetime | None = None,  # noqa: ARG001 - 호출부 대칭용, 이 전이는 시각을 안 쓴다
) -> bool:
    """테넌트의 자동 실행 스위치. 반환값은 적용 후의 값.

    일시정지는 **신규 착수만** 막는다. 이미 도는 체인은 끝까지 간다 — deliver 만
    되고 switch 가 안 된 서버, 전환은 됐는데 이전 계정을 못 거둔 서버는 어느 쪽도
    정상 상태가 아니고, 그 중간에서 멈추는 것이 계속 가는 것보다 위험하다.
    """
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise not_found("tenant")
    if bool(tenant.pool_automation_paused) != paused:
        tenant.pool_automation_paused = paused
        record_event(
            db,
            tenant_id=tenant_id,
            kind="automation_paused" if paused else "automation_resumed",
            actor=actor,
            detail={"paused": paused},
        )
        db.commit()
    return bool(paused)


# -- pool_events 보존 --------------------------------------------------------
def sweep_pool_event_retention(db: Session) -> int:
    """보존 창을 넘긴 ``pool_events`` 행을 purge 한다. 삭제 행 수 반환.

    자동 변경 감사 행이라 이 위에서 도는 적분이 없고(정산은 usage 쪽 원장이 한다)
    나이만 보고 지운다. 배치마다 자기 트랜잭션에서 트랜잭션 범위 advisory lock 을
    다시 잡는다 — 앞 배치의 커밋이 락을 놓기 때문이고, 재획득 실패는 다른
    인스턴스가 이번 틱의 purge 를 가져갔다는 뜻이므로 남은 배치를 양보한다.
    ``pool_event_retention_days <= 0`` 이면 영구 보존이라 0 을 돌려준다.
    """
    days = get_settings().pool_event_retention_days
    if days <= 0:
        return 0
    delete_before = _now() - timedelta(days=days)

    total = 0
    while True:
        if not _try_advisory_xact_lock(db, _POOL_EVENT_RETENTION_LOCK_KEY):
            break
        ids = list(
            db.scalars(
                select(PoolEvent.id)
                .where(PoolEvent.created_at < delete_before)
                .limit(_POOL_EVENT_RETENTION_BATCH)
            ).all()
        )
        if not ids:
            db.rollback()  # 락 반납; 지울 게 없다.
            break
        db.execute(delete(PoolEvent).where(PoolEvent.id.in_(ids)))
        db.commit()  # 다음 배치가 다시 잡을 때까지 advisory lock 반납.
        total += len(ids)
        if len(ids) < _POOL_EVENT_RETENTION_BATCH:
            break
    if total:
        _logger.info(
            "pool event retention purged %d row(s) older than %s",
            total,
            delete_before.isoformat(),
        )
    return total
