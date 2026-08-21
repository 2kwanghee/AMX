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

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import try_advisory_xact_lock as _try_advisory_xact_lock
from app.models import (
    POOL_CONTROLLER_ACTOR,
    POOL_OPERATOR_STATES,
    Account,
    AccountUsageWindow,
    Alert,
    Assignment,
    PoolEvent,
    PoolRecommendation,
    Server,
    Tenant,
)
from app.services import alerts as alerts_service

# 보존 스윕 …0C 다음 번호. 한 인스턴스만 이번 틱의 풀 계산을 소유한다(F3 다중 인스턴스).
_POOL_SWEEP_LOCK_KEY = 0x414D580F0D

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


def _pct(value: object) -> float:
    """proto3 은 0.0 스칼라를 MessageToDict 에서 아예 빼므로 누락은 0.0 으로 읽는다."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


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
    worst = max(windows, key=lambda w: w["pct"], default=None)
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
    accounts = db.scalars(
        select(Account).where(Account.tenant_id == tenant_id).order_by(Account.id)
    ).all()
    live = _live_assignments(db, tenant_id)
    windows = _windows_by_account(db, tenant_id)
    servers = _servers_by_id(db, tenant_id)

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

        policy = _observing_policy(account_windows, servers)
        swap_at = float(policy["swap_at_pct"])
        ready_return = float(policy["ready_return_pct"])

        exhausted = [
            w
            for w in account_windows
            if w.pct >= swap_at and _aware(w.resets_at) is not None and _aware(w.resets_at) > stamp
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
        if (_aware(w.reported_at) or now) >= cooling_until
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
    return max((w.pct for w in windows), default=0.0)


def _window_pct(windows: list[AccountUsageWindow], window_id: str) -> float:
    for w in windows:
        if w.window_id == window_id:
            return w.pct
    return 0.0


def _unusable_account_ids(db: Session, tenant_id: uuid.UUID) -> set[uuid.UUID]:
    """최근 ``credential_unusable`` 경보가 열려 있는 계정 — 후보에서 뺀다(§2.4).

    자격증명이 못 쓰는 상태로 신고된 계정을 다시 배급하면 그 서버까지 같이 죽는다.
    """
    rows = db.execute(
        select(Alert.account_id).where(
            Alert.tenant_id == tenant_id,
            Alert.kind == "credential_unusable",
            Alert.status.in_(("open", "acked")),
            Alert.account_id.is_not(None),
        )
    ).all()
    return {r.account_id for r in rows}


def _candidates(
    accounts: list[Account],
    *,
    live: dict[uuid.UUID, Assignment],
    windows: dict[uuid.UUID, list[AccountUsageWindow]],
    unusable: set[uuid.UUID],
    server_has_live: bool,
) -> list[Account]:
    """배급처에서 뽑을 수 있는 계정 목록, 기획서 §2.4 의 순서로 정렬해 반환.

    정렬은 ① 7일 창 잔여 ② 5시간 창 잔여 ③ 마지막 대여 종료가 오래된 순이고, 동점은
    account_id 로 끊는다. 결정적이어야 하는 이유는 스윕이 30초마다 재진입하기 때문이다 —
    타이브레이크가 흔들리면 매 틱 다른 후보를 권고해 권고가 계속 지워졌다 생긴다.
    """
    epoch = datetime.min.replace(tzinfo=UTC)
    out = []
    for account in accounts:
        if account.pool_state in ("pinned", "held", "cooling", "recalling", "leased"):
            continue
        if account.assignment_excluded:
            continue
        if account.status in _UNUSABLE_STATUSES:
            continue
        if account.id in live:
            continue
        if account.id in unusable:
            continue
        # Codex 는 서버당 1개 제한이라, 이미 뭔가 붙어 있는 서버에는 얹지 않는다.
        if account.provider == "codex" and server_has_live:
            continue
        out.append(account)
    out.sort(
        key=lambda a: (
            _window_pct(windows.get(a.id, []), WINDOW_SEVEN_DAY),
            _window_pct(windows.get(a.id, []), WINDOW_FIVE_HOUR),
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
    now: datetime,
) -> dict | None:
    """이 서버에 대해 지금 참인 권고 한 건(또는 없음).

    서버당 최대 한 건이다. 기획서 §4.2 의 in-flight 규칙 때문에 어차피 한 번에 한
    체인만 실행할 수 있으므로, 여러 건을 쌓아 두면 운영자에게 실행할 수 없는 선택지를
    보여 주는 셈이 된다. 판정 순서는 급한 것부터: 빈 서버 → 교체 → 초과분 회수 → 예열.
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

    hot = [
        a
        for a in leased
        if _max_pct(windows.get(a.id, [])) >= swap_at
        and now - _lease_started_at(live[a.id]) >= min_lease
    ]
    if hot:
        worst = max(hot, key=lambda a: _max_pct(windows.get(a.id, [])))
        # 이미 예열해 둔 계정이 서버에 있으면 그쪽으로 넘긴다 — 자격증명 재전송이 없다.
        spare = [
            a
            for a in leased
            if a.id != worst.id and _max_pct(windows.get(a.id, [])) < prefetch_at
        ]
        target_account = spare[0] if spare else (candidates[0] if candidates else None)
        if target_account is None:
            return None
        pct = _max_pct(windows.get(worst.id, []))
        return {
            "kind": "swap",
            "from_account_id": worst.id,
            "to_account_id": target_account.id,
            "trigger_pct": pct,
            "reason": (
                f"{worst.email}이 {pct:.0f}%로 교체 임계({swap_at:.0f}%)를 넘겼다. "
                f"{target_account.email}로 전환한다."
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
        warm = max(leased, key=lambda a: _max_pct(windows.get(a.id, [])))
        pct = _max_pct(windows.get(warm.id, []))
        if pct >= prefetch_at:
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
    tenant = db.get(Tenant, tenant_id)
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
    paused = bool(tenant is not None and tenant.pool_automation_paused)

    created = 0
    for server in servers:
        policy = resolve_policy(server)
        server_live = [a for a in live.values() if a.server_id == server.id]
        desired: dict | None = None
        if policy["mode"] == "auto" and not paused:
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
    """상태 계산 + 권고 재계산을 테넌트마다 한 번씩. 자체 advisory 락(…0D).

    명령은 한 줄도 내지 않는다(P1). 락을 못 잡으면 이번 틱은 다른 인스턴스 몫이므로
    그냥 0 을 돌려준다. 스스로 커밋한다 — 트랜잭션 범위 락이라 커밋이 곧 반납이다.
    """
    if not _try_advisory_xact_lock(db, _POOL_SWEEP_LOCK_KEY):
        return 0
    stamp = _aware(now) or _now()
    tenant_ids = list(db.scalars(select(Tenant.id).order_by(Tenant.id)).all())
    total = 0
    for tenant_id in tenant_ids:
        total += compute_states(db, tenant_id, now=stamp)
        total += build_recommendations(db, tenant_id, now=stamp)
    db.commit()
    return total
