"""계정 풀 P0+P1 — 창 정규화, 상태 계산, 권고, 조회·정책 API.

검증: UsageReport 창 upsert(재전송이 행을 늘리지 않고 값만 교체) / 계정 창 고사용
경보 open·auto-resolve / leased→cooling→ready 전이 / 관측 없을 때 15분 유예 /
복귀 임계 위 관측이면 안 풀림 / 권고 생성·중복 방지·조건 해소 시 삭제 /
manual 서버·in-flight 배정·자동화 일시정지에는 권고 없음 / 후보 제외·정렬 규칙 /
pin·hold 가 스윕에 덮이지 않음 / 정책 PATCH 부분 병합과 범위 검증 / 테넌트 격리.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models import (
    Account,
    AccountUsageWindow,
    Alert,
    PoolEvent,
    PoolRecommendation,
    Server,
)
from app.services import inventory, pool


def _now() -> datetime:
    return datetime.now(UTC)


def _db():
    from app.db import get_sessionmaker

    return get_sessionmaker()()


def _tenant(name: str | None = None) -> uuid.UUID:
    with _db() as db:
        return inventory.create_tenant(db, name or "pool-" + uuid.uuid4().hex[:8]).id


def _account(tenant_id: uuid.UUID, email: str, **over) -> uuid.UUID:
    with _db() as db:
        account = inventory.create_account(
            db,
            tenant_id,
            email=email,
            credential_type="api_key",
            secret="k",
            **over,
        )
        db.commit()
        return account.id


def _server(tenant_id: uuid.UUID, name: str, policy: dict | None = None) -> uuid.UUID:
    with _db() as db:
        server = inventory.create_server(
            db, tenant_id, name=name, hostname=None, switch_mode="auto"
        )
        if policy is not None:
            server.pool_policy = policy
        db.commit()
        return server.id


def _assign(tenant_id: uuid.UUID, account_id: uuid.UUID, server_id: uuid.UUID, state="active"):
    with _db() as db:
        assignment = inventory.create_assignment(
            db, tenant_id, account_id=account_id, server_id=server_id, pinned=False
        )
        assignment.state = state
        assignment.delivered_at = _now() - timedelta(hours=4)
        db.commit()
        return assignment.id


def _report(email: str, *, five: float, seven: float, resets_at: datetime | None = None) -> dict:
    """UsageReport 를 MessageToDict(preserving_proto_field_name=True) 한 모양."""
    stamp = (resets_at or (_now() + timedelta(hours=2))).isoformat().replace("+00:00", "Z")
    return {
        "accounts": [
            {
                "account": {"ams_account_id": "", "email": email},
                "usage_fetched_at": _now().isoformat().replace("+00:00", "Z"),
                "windows": [
                    {"id": "five_hour", "pct": five, "resets_at": stamp, "window_minutes": 300},
                    {"id": "seven_day", "pct": seven, "resets_at": stamp, "window_minutes": 10080},
                ],
            }
        ]
    }


def _ingest(tenant_id, server_id, payload, reported_at=None) -> int:
    with _db() as db:
        touched = pool.ingest_usage_report(
            db,
            tenant_id=tenant_id,
            server_id=server_id,
            payload=payload,
            reported_at=reported_at,
        )
        db.commit()
        return touched


def _windows(tenant_id, account_id) -> list[AccountUsageWindow]:
    with _db() as db:
        return list(
            db.scalars(
                select(AccountUsageWindow).where(
                    AccountUsageWindow.tenant_id == tenant_id,
                    AccountUsageWindow.account_id == account_id,
                )
            ).all()
        )


def _state(account_id) -> Account:
    with _db() as db:
        return db.get(Account, account_id)


def _compute(tenant_id, *, now=None) -> int:
    with _db() as db:
        changed = pool.compute_states(db, tenant_id, now=now)
        db.commit()
        return changed


def _build(tenant_id, *, now=None) -> int:
    with _db() as db:
        made = pool.build_recommendations(db, tenant_id, now=now)
        db.commit()
        return made


def _recs(tenant_id) -> list[PoolRecommendation]:
    with _db() as db:
        return list(
            db.scalars(
                select(PoolRecommendation).where(PoolRecommendation.tenant_id == tenant_id)
            ).all()
        )


# -- P0 ingest ---------------------------------------------------------------
def test_ingest_upserts_latest_window_values(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1")
    account_id = _account(tenant_id, "a@x.example.com")

    assert _ingest(tenant_id, server_id, _report("a@x.example.com", five=10, seven=20)) == 2
    rows = {w.window_id: w for w in _windows(tenant_id, account_id)}
    assert rows["five_hour"].pct == 10
    assert rows["seven_day"].pct == 20
    assert rows["five_hour"].server_id == server_id
    assert rows["five_hour"].resets_at is not None

    # 재전송은 행을 늘리지 않고 값만 바꾼다 — 이 테이블은 이력이 아니라 최신값이다.
    _ingest(tenant_id, server_id, _report("a@x.example.com", five=44, seven=55))
    rows = {w.window_id: w for w in _windows(tenant_id, account_id)}
    assert len(rows) == 2
    assert rows["five_hour"].pct == 44
    assert rows["seven_day"].pct == 55


def test_ingest_maps_by_ams_account_id_and_ignores_unknown_accounts(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1")
    account_id = _account(tenant_id, "known@x.example.com")

    payload = _report("someone-else@x.example.com", five=5, seven=5)
    payload["accounts"][0]["account"]["ams_account_id"] = str(account_id)
    assert _ingest(tenant_id, server_id, payload) == 2
    assert len(_windows(tenant_id, account_id)) == 2

    # id 도 이메일도 이 테넌트의 것이 아니면 조용히 버린다(남의 계정에 쓰지 않는다).
    stray = _report("nobody@x.example.com", five=5, seven=5)
    assert _ingest(tenant_id, server_id, stray) == 0


def test_ingest_opens_and_resolves_account_window_high_alert(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1")
    account_id = _account(tenant_id, "hot@x.example.com")

    _ingest(tenant_id, server_id, _report("hot@x.example.com", five=91, seven=10))
    with _db() as db:
        alert = db.scalar(
            select(Alert).where(
                Alert.tenant_id == tenant_id, Alert.kind == "account_window_high"
            )
        )
    assert alert is not None
    assert alert.status == "open"
    assert alert.severity == "warning"
    assert alert.account_id == account_id
    assert alert.detail["window_id"] == "five_hour"

    # 창이 풀리면 같은 경보가 스스로 닫힌다.
    _ingest(tenant_id, server_id, _report("hot@x.example.com", five=3, seven=4))
    with _db() as db:
        alert = db.scalar(
            select(Alert).where(
                Alert.tenant_id == tenant_id, Alert.kind == "account_window_high"
            )
        )
    assert alert.status == "resolved"


# -- P1 상태 전이 -------------------------------------------------------------
def test_leased_then_cooling_then_ready(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1")
    account_id = _account(tenant_id, "cycle@x.example.com")
    assignment_id = _assign(tenant_id, account_id, server_id)

    _compute(tenant_id)
    assert _state(account_id).pool_state == "leased"

    # 회수 후(detached) 창이 막혀 있으면 충전소로 간다.
    resets_at = _now() + timedelta(hours=3)
    _ingest(
        tenant_id, server_id, _report("cycle@x.example.com", five=97, seven=30, resets_at=resets_at)
    )
    with _db() as db:
        from app.models import Assignment

        db.get(Assignment, assignment_id).state = "detached"
        db.commit()

    _compute(tenant_id)
    account = _state(account_id)
    assert account.pool_state == "cooling"
    assert account.cooling_window_id == "five_hour"
    assert account.cooling_until is not None
    assert account.last_lease_ended_at is not None

    # 리셋 이후의 관측이라도 pct 가 높으면 안 풀린다 — 시각만 믿으면 리셋 직후 다시
    # 소진되는 계정을 못 막는다.
    later = resets_at + timedelta(minutes=1)
    _ingest(
        tenant_id,
        server_id,
        _report("cycle@x.example.com", five=90, seven=30, resets_at=resets_at),
        reported_at=later,
    )
    _compute(tenant_id, now=later)
    assert _state(account_id).pool_state == "cooling"

    # 복귀 임계 이하 관측이 오면 배급처로.
    _ingest(
        tenant_id,
        server_id,
        _report("cycle@x.example.com", five=4, seven=6, resets_at=resets_at),
        reported_at=later,
    )
    _compute(tenant_id, now=later)
    assert _state(account_id).pool_state == "ready"


def test_cooling_releases_without_observation_after_grace(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1")
    account_id = _account(tenant_id, "stuck@x.example.com")

    resets_at = _now() + timedelta(hours=1)
    _ingest(
        tenant_id, server_id, _report("stuck@x.example.com", five=99, seven=1, resets_at=resets_at)
    )
    _compute(tenant_id)
    assert _state(account_id).pool_state == "cooling"

    # 관측이 끊긴 상태(마지막 보고는 cooling_until 이전). 만료 직후에는 아직 안 푼다.
    _compute(tenant_id, now=resets_at + timedelta(minutes=1))
    assert _state(account_id).pool_state == "cooling"

    # 유예(기본 15분)를 넘기면 관측 없이도 풀어 준다 — 갇힌 계정이 더 나쁘다.
    _compute(tenant_id, now=resets_at + timedelta(minutes=16))
    account = _state(account_id)
    assert account.pool_state == "ready"
    assert account.cooling_until is None


def test_pinned_and_held_survive_the_sweep(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1")
    pinned_id = _account(tenant_id, "pin@x.example.com")
    held_id = _account(tenant_id, "hold@x.example.com")
    # 배정이 살아 있어도(=leased 로 계산될 상황) 운영자 값이 이긴다.
    _assign(tenant_id, pinned_id, server_id)
    with _db() as db:
        db.get(Account, pinned_id).pool_state = "pinned"
        db.get(Account, held_id).pool_state = "held"
        db.commit()

    _compute(tenant_id)
    assert _state(pinned_id).pool_state == "pinned"
    assert _state(held_id).pool_state == "held"


def test_state_changes_are_recorded_as_pool_events(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1")
    account_id = _account(tenant_id, "audit@x.example.com")
    _assign(tenant_id, account_id, server_id)

    _compute(tenant_id)
    with _db() as db:
        events = list(
            db.scalars(
                select(PoolEvent).where(
                    PoolEvent.tenant_id == tenant_id, PoolEvent.kind == "state_changed"
                )
            ).all()
        )
    assert events
    assert events[0].actor == "pool-controller"
    assert events[0].detail["to"] == "leased"


# -- P1 권고 ------------------------------------------------------------------
def _auto(target_leases=1, **over) -> dict:
    return {"mode": "auto", "target_leases": target_leases, **over}


def test_lease_recommendation_for_empty_auto_server(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", _auto())
    account_id = _account(tenant_id, "free@x.example.com")

    assert _build(tenant_id) == 1
    recs = _recs(tenant_id)
    assert len(recs) == 1
    assert recs[0].kind == "lease"
    assert recs[0].to_account_id == account_id
    assert recs[0].server_id == server_id

    # 같은 조건이 계속 참이어도 두 번째 행이 생기지 않는다.
    assert _build(tenant_id) == 0
    assert len(_recs(tenant_id)) == 1
    first_created = recs[0].created_at
    assert _recs(tenant_id)[0].created_at == first_created


def test_recommendation_disappears_when_the_condition_clears(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", _auto())
    account_id = _account(tenant_id, "free@x.example.com")
    _build(tenant_id)
    assert _recs(tenant_id)

    _assign(tenant_id, account_id, server_id)
    _compute(tenant_id)
    _build(tenant_id)
    assert _recs(tenant_id) == []


def test_manual_server_gets_no_recommendation(app_env):
    tenant_id = _tenant()
    _server(tenant_id, "s1")  # 기본 정책 = manual
    _account(tenant_id, "free@x.example.com")
    assert _build(tenant_id) == 0
    assert _recs(tenant_id) == []


def test_in_flight_assignment_suppresses_recommendations(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", _auto())
    account_id = _account(tenant_id, "moving@x.example.com")
    _account(tenant_id, "spare@x.example.com")
    _assign(tenant_id, account_id, server_id, state="delivering")

    assert _build(tenant_id) == 0
    assert _recs(tenant_id) == []


def test_paused_tenant_gets_no_recommendation(app_env):
    tenant_id = _tenant()
    _server(tenant_id, "s1", _auto())
    _account(tenant_id, "free@x.example.com")
    with _db() as db:
        from app.models import Tenant

        db.get(Tenant, tenant_id).pool_automation_paused = True
        db.commit()
    assert _build(tenant_id) == 0


def test_swap_recommendation_and_candidate_exclusions(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", _auto(swap_at_pct=85, min_lease_minutes=30))
    hot_id = _account(tenant_id, "hot@x.example.com")
    excluded_id = _account(tenant_id, "excluded@x.example.com", assignment_excluded=True)
    fresh_id = _account(tenant_id, "fresh@x.example.com")
    _assign(tenant_id, hot_id, server_id)
    _ingest(tenant_id, server_id, _report("hot@x.example.com", five=92, seven=40))
    _ingest(tenant_id, server_id, _report("fresh@x.example.com", five=1, seven=2))
    _ingest(tenant_id, server_id, _report("excluded@x.example.com", five=0, seven=0))
    _compute(tenant_id)

    _build(tenant_id)
    recs = _recs(tenant_id)
    assert len(recs) == 1
    assert recs[0].kind == "swap"
    assert recs[0].from_account_id == hot_id
    # assignment_excluded 계정은 잔여가 가장 많아도 후보가 아니다.
    assert recs[0].to_account_id == fresh_id
    assert recs[0].to_account_id != excluded_id
    assert recs[0].trigger_pct == 92


def test_min_lease_minutes_blocks_a_fresh_swap(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", _auto(swap_at_pct=85, min_lease_minutes=30))
    hot_id = _account(tenant_id, "hot@x.example.com")
    _account(tenant_id, "fresh@x.example.com")
    _assign(tenant_id, hot_id, server_id)
    with _db() as db:
        from app.models import Assignment

        row = db.scalar(select(Assignment).where(Assignment.account_id == hot_id))
        row.delivered_at = _now() - timedelta(minutes=5)
        db.commit()
    _ingest(tenant_id, server_id, _report("hot@x.example.com", five=95, seven=40))
    _compute(tenant_id)

    _build(tenant_id)
    # 방금 대여한 계정을 바로 갈아치우면 플래핑이 된다. 교체(swap)는 막히되, 활성을
    # 건드리지 않는 예열(prefetch)은 그대로 남는다 — min_lease 가 지나는 순간 바로
    # 넘길 수 있도록 미리 올려 두는 편이 공백을 없앤다.
    assert [r.kind for r in _recs(tenant_id)] == ["prefetch"]


def test_candidate_order_prefers_the_most_remaining_seven_day_window(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", _auto())
    busy_id = _account(tenant_id, "busy@x.example.com")
    idle_id = _account(tenant_id, "idle@x.example.com")
    _ingest(tenant_id, server_id, _report("busy@x.example.com", five=1, seven=60))
    _ingest(tenant_id, server_id, _report("idle@x.example.com", five=50, seven=5))
    _compute(tenant_id)

    _build(tenant_id)
    rec = _recs(tenant_id)[0]
    # 7일 잔여가 1순위이므로 5시간이 더 나쁜 idle 이 뽑힌다.
    assert rec.to_account_id == idle_id
    assert rec.to_account_id != busy_id


# -- API ---------------------------------------------------------------------
def test_pool_overview_and_tenant_isolation(app_env, client):
    tenant_a = _tenant()
    tenant_b = _tenant()
    server_id = _server(tenant_a, "s1", _auto())
    account_id = _account(tenant_a, "a@x.example.com")
    _account(tenant_b, "b@x.example.com")
    _assign(tenant_a, account_id, server_id)
    _ingest(tenant_a, server_id, _report("a@x.example.com", five=12, seven=34))
    _compute(tenant_a)

    body = client.get(f"/api/v1/tenants/{tenant_a}/pool").json()
    assert body["automationPaused"] is False
    assert [a["email"] for a in body["accounts"]] == ["a@x.example.com"]
    account = body["accounts"][0]
    assert account["poolState"] == "leased"
    assert account["leasedServerId"] == str(server_id)
    assert {w["windowId"] for w in account["windows"]} == {"five_hour", "seven_day"}
    assert body["servers"][0]["leasedAccountIds"] == [str(account_id)]
    assert body["servers"][0]["activeAccountId"] == str(account_id)
    assert body["servers"][0]["maxPct"] == 34
    assert body["servers"][0]["poolPolicy"]["mode"] == "auto"

    # 다른 테넌트의 조회에는 이 계정이 없다.
    other = client.get(f"/api/v1/tenants/{tenant_b}/pool").json()
    assert [a["email"] for a in other["accounts"]] == ["b@x.example.com"]


def test_pool_policy_patch_merges_and_validates(app_env, client):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1")

    r = client.patch(
        f"/api/v1/tenants/{tenant_id}/servers/{server_id}/pool-policy",
        json={"mode": "auto", "swapAtPct": 90},
    )
    assert r.status_code == 200
    policy = r.json()["poolPolicy"]
    assert policy["mode"] == "auto"
    assert policy["swapAtPct"] == 90
    assert policy["targetLeases"] == 1  # 안 준 필드는 기본값 그대로.

    # 부분 병합: 다른 필드만 바꿔도 앞서 저장한 값이 살아 있다.
    r = client.patch(
        f"/api/v1/tenants/{tenant_id}/servers/{server_id}/pool-policy",
        json={"prefetchAtPct": 60},
    )
    policy = r.json()["poolPolicy"]
    assert policy["swapAtPct"] == 90
    assert policy["prefetchAtPct"] == 60

    for bad in ({"swapAtPct": 101}, {"targetLeases": 0}, {"targetLeases": 9}, {"mode": "semi"}):
        r = client.patch(
            f"/api/v1/tenants/{tenant_id}/servers/{server_id}/pool-policy", json=bad
        )
        assert r.status_code == 422, bad

    with _db() as db:
        assert db.get(Server, server_id).pool_policy["swap_at_pct"] == 90
    with _db() as db:
        kinds = [
            e.kind
            for e in db.scalars(
                select(PoolEvent).where(PoolEvent.tenant_id == tenant_id)
            ).all()
        ]
    assert kinds.count("policy_changed") == 2


def test_pin_hold_release_actions(app_env, client):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1")
    account_id = _account(tenant_id, "op@x.example.com")
    base = f"/api/v1/tenants/{tenant_id}/accounts/{account_id}"

    assert client.post(f"{base}/pool:pin").json()["poolState"] == "pinned"
    # 고정된 계정은 스윕이 건드리지 않는다.
    _assign(tenant_id, account_id, server_id)
    _compute(tenant_id)
    assert _state(account_id).pool_state == "pinned"

    assert client.post(f"{base}/pool:unpin").json()["poolState"] == "ready"
    # 고정돼 있지 않은 계정에 unpin 은 409 — 조용히 성공한 척하지 않는다.
    assert client.post(f"{base}/pool:unpin").status_code == 409

    assert client.post(f"{base}/pool:hold").json()["poolState"] == "held"
    assert client.post(f"{base}/pool:release").json()["poolState"] == "ready"

    # 대여 중인 계정은 release 로 풀 수 없다(다음 스윕이 곧바로 되돌린다).
    _compute(tenant_id)
    assert _state(account_id).pool_state == "leased"
    r = client.post(f"{base}/pool:release")
    assert r.status_code == 409
    assert r.json()["code"] == "pool.state_conflict"


def test_recommendations_and_events_endpoints(app_env, client):
    tenant_id = _tenant()
    _server(tenant_id, "s1", _auto())
    account_id = _account(tenant_id, "free@x.example.com")
    _build(tenant_id)

    recs = client.get(f"/api/v1/tenants/{tenant_id}/pool/recommendations").json()
    assert len(recs) == 1
    assert recs[0]["kind"] == "lease"
    assert recs[0]["toAccountId"] == str(account_id)

    events = client.get(f"/api/v1/tenants/{tenant_id}/pool/events?limit=10").json()
    assert any(e["kind"] == "recommendation_created" for e in events)
    assert all(e["actor"] == "pool-controller" for e in events)


def test_sweep_pool_is_a_no_op_without_data(app_env):
    tenant_id = _tenant()
    with _db() as db:
        assert pool.sweep_pool(db) == 0
    assert _recs(tenant_id) == []
