"""계정 풀 P1 리뷰 반영분 — 격리·신선도·사용 불가·보존·입력 파싱.

검증: 창 정규화가 터져도 UsageReport 는 저장된다 / 한 테넌트의 스윕 실패가 다른
테넌트를 롤백하지 않는다 / 낡은 관측(기본 30분)은 미상이라 교체를 트리거하지 않는다 /
pct 를 못 읽은 계정은 후보에서 빠진다 / 오프라인 서버에는 권고가 없다 / 사용 불가
계정은 즉시 교체 권고 대상이고 회수 뒤 held 로 남는다 / 사라진 권고가 이벤트로 남고
보존 스윕이 낡은 이벤트를 지운다 / pin 은 ready·cooling 에서만, hold 는 어디서나 /
pct 문자열 파싱 / windows 가 전부 id 없으면 레거시 필드로 폴백 / Codex 서버당 1개
제한은 교체 상대에는 걸리지 않는다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db import get_sessionmaker
from app.models import (
    Account,
    AccountUsageWindow,
    Alert,
    Assignment,
    PoolEvent,
    PoolRecommendation,
    UsageSnapshot,
)
from app.services import alerts as alerts_service, inventory, pool


def _now() -> datetime:
    return datetime.now(UTC)


def _db():
    return get_sessionmaker()()


def _tenant() -> uuid.UUID:
    with _db() as db:
        return inventory.create_tenant(db, "hard-" + uuid.uuid4().hex[:8]).id


# Codex 계정은 auth.json 모양의 자격증명만 받는다(inventory._validate_codex_secret).
CODEX_SECRET = '{"tokens": {"refresh_token": "rt-test"}}'


def _account(tenant_id, email, **over) -> uuid.UUID:
    secret = CODEX_SECRET if over.get("provider") == "codex" else "k"
    with _db() as db:
        account = inventory.create_account(
            db, tenant_id, email=email, credential_type="api_key", secret=secret, **over
        )
        db.commit()
        return account.id


def _server(tenant_id, name, policy=None, status="online") -> uuid.UUID:
    with _db() as db:
        server = inventory.create_server(
            db, tenant_id, name=name, hostname=None, switch_mode="auto"
        )
        server.status = status
        if policy is not None:
            server.pool_policy = policy
        db.commit()
        return server.id


def _assign(tenant_id, account_id, server_id, state="active") -> uuid.UUID:
    with _db() as db:
        assignment = inventory.create_assignment(
            db, tenant_id, account_id=account_id, server_id=server_id, pinned=False
        )
        assignment.state = state
        assignment.delivered_at = _now() - timedelta(hours=4)
        db.commit()
        return assignment.id


def _payload(email, windows) -> dict:
    return {
        "accounts": [
            {
                "account": {"ams_account_id": "", "email": email},
                "usage_fetched_at": _now().isoformat().replace("+00:00", "Z"),
                **windows,
            }
        ]
    }


def _std(email, *, five, seven, resets_at=None) -> dict:
    stamp = (resets_at or (_now() + timedelta(hours=2))).isoformat().replace("+00:00", "Z")
    return _payload(
        email,
        {
            "windows": [
                {"id": "five_hour", "pct": five, "resets_at": stamp},
                {"id": "seven_day", "pct": seven, "resets_at": stamp},
            ]
        },
    )


def _ingest(tenant_id, server_id, payload, reported_at=None) -> int:
    with _db() as db:
        touched = pool.ingest_usage_report(
            db,
            tenant_id=tenant_id,
            server_id=server_id,
            payload=payload,
            reported_at=reported_at or _now(),
        )
        db.commit()
        return touched


def _windows(tenant_id, account_id) -> dict[str, AccountUsageWindow]:
    with _db() as db:
        return {
            w.window_id: w
            for w in db.scalars(
                select(AccountUsageWindow).where(
                    AccountUsageWindow.tenant_id == tenant_id,
                    AccountUsageWindow.account_id == account_id,
                )
            ).all()
        }


def _build(tenant_id, *, now=None) -> list[PoolRecommendation]:
    with _db() as db:
        pool.build_recommendations(db, tenant_id, now=now)
        db.commit()
        return list(
            db.scalars(
                select(PoolRecommendation).where(PoolRecommendation.tenant_id == tenant_id)
            ).all()
        )


def _compute(tenant_id, *, now=None) -> int:
    with _db() as db:
        changed = pool.compute_states(db, tenant_id, now=now)
        db.commit()
        return changed


def _events(tenant_id, kind) -> list[PoolEvent]:
    with _db() as db:
        return list(
            db.scalars(
                select(PoolEvent).where(
                    PoolEvent.tenant_id == tenant_id, PoolEvent.kind == kind
                )
            ).all()
        )


# -- 1) 창 정규화 실패가 보고를 데려가지 않는다 -------------------------------
def test_window_ingest_failure_keeps_the_usage_report(app_env, monkeypatch):
    from app.grpc import server as grpc_server
    from app.grpc.proto import pb
    from app.grpc.signing import Signer

    from tests.test_grpc_channel import AGENT_ID, _seed_tenant_account_server

    tenant_id, account_id, server_id = _seed_tenant_account_server("savepoint@ex.com")

    def _boom(*args, **kwargs):
        raise RuntimeError("창 파싱이 터졌다")

    monkeypatch.setattr(grpc_server.pool, "ingest_usage_report", _boom)

    report = pb.UsageReport(schema_version=1, agent_id=AGENT_ID)
    report.pool_summary.all_exhausted = True
    au = report.accounts.add()
    au.account.ams_account_id = str(account_id)
    au.allocation_status = pb.ALLOCATION_STATUS_ACTIVE

    servicer = grpc_server.ControlPlaneServicer(
        Signer.from_env_or_generate(), session_factory=get_sessionmaker()
    )
    servicer._store_usage(server_id, tenant_id, report, report_type="usage")

    with _db() as db:
        # 스냅샷은 남는다 — 정규화는 보고의 부수 효과이지 보고가 아니다.
        assert (
            db.scalars(
                select(UsageSnapshot).where(UsageSnapshot.server_id == server_id)
            ).one()
            is not None
        )
        # 같은 트랜잭션의 다른 산출물(경보·liveness)도 살아 있다.
        assert db.scalars(
            select(Alert).where(
                Alert.server_id == server_id, Alert.kind == "all_exhausted"
            )
        ).first() is not None
        from app.models import Server

        assert db.get(Server, server_id).status == "online"
    # 창은 이번 틱만 잃는다. upsert 라 다음 보고가 같은 자리를 덮는다.
    assert _windows(tenant_id, account_id) == {}


# -- 2) 테넌트 격리 -----------------------------------------------------------
def test_one_tenant_failure_does_not_roll_back_another(app_env, monkeypatch):
    from app.models import PoolChain

    healthy = _tenant()
    broken = _tenant()
    healthy_server = _server(healthy, "ok", {"mode": "auto"})
    broken_server = _server(broken, "bad", {"mode": "auto"})
    healthy_account = _account(healthy, "ok@x.example.com")
    broken_account = _account(broken, "bad@x.example.com")
    _ingest(healthy, healthy_server, _std("ok@x.example.com", five=1, seven=1))
    _ingest(broken, broken_server, _std("bad@x.example.com", five=1, seven=1))
    # 이 배정이 있으면 compute_states 가 broken 계정을 leased 로 적는다 — 그 변경이
    # 롤백됐는지가 격리의 증거다.
    _assign(broken, broken_account, broken_server)

    real = pool.build_recommendations

    def _selective(db, tenant_id, **kwargs):
        if tenant_id == broken:
            raise RuntimeError("이 테넌트만 터진다")
        return real(db, tenant_id, **kwargs)

    monkeypatch.setattr(pool, "build_recommendations", _selective)
    with _db() as db:
        pool.sweep_pool(db)
    monkeypatch.undo()

    # 건강한 테넌트는 한 틱을 끝까지 돌았다: 상태 계산 → 권고 → 자동 착수.
    with _db() as db:
        chain = db.scalars(
            select(PoolChain).where(PoolChain.tenant_id == healthy)
        ).first()
        assert chain is not None
        assert chain.to_account_id == healthy_account
        assert chain.actor == "pool-controller"
        # 고장 난 테넌트의 그 틱은 통째로 롤백됐다(기본값 그대로).
        assert db.get(Account, broken_account).pool_state == "ready"
        assert db.scalars(select(PoolChain).where(PoolChain.tenant_id == broken)).first() is None

    # 다음 틱에서는 고장 난 테넌트도 정상적으로 계산된다.
    with _db() as db:
        pool.sweep_pool(db)
    with _db() as db:
        assert db.get(Account, broken_account).pool_state == "leased"


# -- 3a) 신선도 ---------------------------------------------------------------
def test_stale_observation_does_not_trigger_a_swap(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", {"mode": "auto", "target_leases": 2})
    hot_id = _account(tenant_id, "stalehot@x.example.com")
    cool_id = _account(tenant_id, "stalecool@x.example.com")
    _assign(tenant_id, hot_id, server_id)
    _assign(tenant_id, cool_id, server_id)
    # 40분 전의 93% 는 이미 리셋됐을 수도 있는 값이다.
    _ingest(
        tenant_id,
        server_id,
        _std("stalehot@x.example.com", five=93, seven=40),
        reported_at=_now() - timedelta(minutes=40),
    )
    _ingest(tenant_id, server_id, _std("stalecool@x.example.com", five=5, seven=5))

    assert _build(tenant_id) == []

    # 같은 값이 지금 관측으로 다시 들어오면 그때 교체한다.
    _ingest(tenant_id, server_id, _std("stalehot@x.example.com", five=93, seven=40))
    recs = _build(tenant_id)
    assert [r.kind for r in recs] == ["swap"]
    assert recs[0].from_account_id == hot_id


def test_unreadable_pct_is_unknown_not_zero(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", {"mode": "auto"})
    good_id = _account(tenant_id, "good@x.example.com")
    murky_id = _account(tenant_id, "murky@x.example.com")

    # 문자열 숫자는 읽는다(_int 와 대칭).
    _ingest(
        tenant_id,
        server_id,
        _payload(
            "good@x.example.com",
            {"windows": [{"id": "five_hour", "pct": "12.5"}, {"id": "seven_day", "pct": "3"}]},
        ),
    )
    assert _windows(tenant_id, good_id)["five_hour"].pct == 12.5
    assert _windows(tenant_id, good_id)["seven_day"].pct == 3.0

    # 읽을 수 없으면 0.0 이 아니라 NULL 이다.
    _ingest(
        tenant_id,
        server_id,
        _payload(
            "murky@x.example.com",
            {"windows": [{"id": "five_hour", "pct": "n/a"}, {"id": "seven_day", "pct": {}}]},
        ),
    )
    assert _windows(tenant_id, murky_id)["five_hour"].pct is None

    # 0% 로 접혔다면 murky 가 최우선 후보가 됐을 것이다. 미상은 후보에서 빠진다.
    recs = _build(tenant_id)
    assert [r.to_account_id for r in recs] == [good_id]
    # 미상 계정에는 고사용 경보도 열리지 않는다(모르는 것으로 경보하지 않는다).
    with _db() as db:
        assert (
            db.scalars(
                select(Alert).where(
                    Alert.account_id == murky_id, Alert.kind == "account_window_high"
                )
            ).first()
            is None
        )


def test_missing_pct_key_is_still_zero(app_env):
    """proto3 은 0.0 스칼라를 빼므로 **키 없음**은 미상이 아니라 0.0 이다."""
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1")
    account_id = _account(tenant_id, "zero@x.example.com")
    _ingest(
        tenant_id,
        server_id,
        _payload("zero@x.example.com", {"windows": [{"id": "five_hour"}]}),
    )
    assert _windows(tenant_id, account_id)["five_hour"].pct == 0.0


# -- 3b) 오프라인 서버 --------------------------------------------------------
def test_offline_server_gets_no_recommendation(app_env):
    tenant_id = _tenant()
    offline_id = _server(tenant_id, "off", {"mode": "auto"}, status="offline")
    _account(tenant_id, "waiting@x.example.com")
    _ingest(tenant_id, offline_id, _std("waiting@x.example.com", five=1, seven=1))

    assert _build(tenant_id) == []

    with _db() as db:
        from app.models import Server

        db.get(Server, offline_id).status = "online"
        db.commit()
    assert [r.kind for r in _build(tenant_id)] == ["lease"]


# -- 3c) 사용 불가 계정 -------------------------------------------------------
def test_unusable_leased_account_is_swapped_and_ends_held(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", {"mode": "auto", "target_leases": 2})
    sick_id = _account(tenant_id, "sick@x.example.com")
    spare_id = _account(tenant_id, "spare@x.example.com")
    sick_assignment = _assign(tenant_id, sick_id, server_id)
    _assign(tenant_id, spare_id, server_id)
    # 사용률은 멀쩡하다 — 교체 이유는 오직 "쓸 수 없다"다.
    _ingest(tenant_id, server_id, _std("sick@x.example.com", five=4, seven=4))
    _ingest(tenant_id, server_id, _std("spare@x.example.com", five=6, seven=6))
    assert _build(tenant_id) == []

    with _db() as db:
        alerts_service.open_alert(
            db,
            tenant_id=tenant_id,
            server_id=server_id,
            account_id=sick_id,
            kind="credential_unusable",
            severity="critical",
            detail={},
        )
        db.commit()

    recs = _build(tenant_id)
    assert [r.kind for r in recs] == ["swap"]
    assert recs[0].from_account_id == sick_id
    assert recs[0].to_account_id == spare_id
    assert "unusable" in recs[0].reason
    # 소진 교체와 달리 min_lease 나 임계를 기다리지 않는다.
    assert recs[0].trigger_pct is None

    # 회수가 끝나면 충전소가 아니라 보류다 — 시간이 해결해 주지 않는 고장이다.
    with _db() as db:
        db.get(Assignment, sick_assignment).state = "detached"
        db.commit()
    _compute(tenant_id)
    assert _state(sick_id).pool_state == "held"
    # held 는 운영자 상태라 다음 스윕이 되돌리지 않는다.
    _compute(tenant_id)
    assert _state(sick_id).pool_state == "held"


def test_quarantined_account_is_excluded_from_candidates(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", {"mode": "auto"})
    bad_id = _account(tenant_id, "quar@x.example.com")
    good_id = _account(tenant_id, "fine@x.example.com")
    _ingest(tenant_id, server_id, _std("quar@x.example.com", five=1, seven=1))
    _ingest(tenant_id, server_id, _std("fine@x.example.com", five=50, seven=50))

    with _db() as db:
        alerts_service.open_alert(
            db,
            tenant_id=tenant_id,
            server_id=server_id,
            account_id=bad_id,
            kind="quarantine",
            severity="warning",
            detail={},
        )
        db.commit()

    # 잔여량만 보면 bad 가 1순위지만 격리 신고가 열려 있으므로 뽑히지 않는다.
    recs = _build(tenant_id)
    assert [r.to_account_id for r in recs] == [good_id]


def _state(account_id) -> Account:
    with _db() as db:
        return db.get(Account, account_id)


# -- 4) 권고 소멸 이벤트 + 보존 -----------------------------------------------
def test_dropped_recommendation_is_recorded_and_events_are_purged(app_env, monkeypatch):
    from app.config import get_settings

    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", {"mode": "auto"})
    account_id = _account(tenant_id, "drop@x.example.com")
    _ingest(tenant_id, server_id, _std("drop@x.example.com", five=1, seven=1))
    recs = _build(tenant_id)
    assert len(recs) == 1

    # 조건 해소: 그 계정을 서버에 배정하면 lease 권고가 참이 아니게 된다.
    _assign(tenant_id, account_id, server_id)
    assert _build(tenant_id) == []
    dropped = _events(tenant_id, "recommendation_dropped")
    assert len(dropped) == 1
    assert dropped[0].detail["recommendation_id"] == str(recs[0].id)
    assert dropped[0].detail["kind"] == "lease"
    assert dropped[0].detail["replaced_by"] is None

    # 보존 스윕은 나이만 본다.
    with _db() as db:
        for row in db.scalars(select(PoolEvent).where(PoolEvent.tenant_id == tenant_id)).all():
            row.created_at = _now() - timedelta(days=200)
        db.commit()

    monkeypatch.setenv("AMX_POOL_EVENT_RETENTION_DAYS", "0")
    get_settings.cache_clear()
    try:
        with _db() as db:
            assert pool.sweep_pool_event_retention(db) == 0  # 0 이하면 영구 보존
        monkeypatch.setenv("AMX_POOL_EVENT_RETENTION_DAYS", "90")
        get_settings.cache_clear()
        with _db() as db:
            purged = pool.sweep_pool_event_retention(db)
        assert purged >= 1
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
    assert _events(tenant_id, "recommendation_dropped") == []


# -- 5) pin / hold 의 허용 상태 ------------------------------------------------
def test_pin_is_limited_to_ready_and_cooling_while_hold_is_not(app_env, client):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1")
    leased_id = _account(tenant_id, "leased@x.example.com")
    ready_id = _account(tenant_id, "ready@x.example.com")
    _assign(tenant_id, leased_id, server_id)
    _compute(tenant_id)
    assert _state(leased_id).pool_state == "leased"

    leased_base = f"/api/v1/tenants/{tenant_id}/accounts/{leased_id}"
    ready_base = f"/api/v1/tenants/{tenant_id}/accounts/{ready_id}"

    # 대여 중인 계정에 pin 은 409 — 배정은 살아 있는데 상태만 자동화 밖이 된다.
    pinned = client.post(f"{leased_base}/pool:pin")
    assert pinned.status_code == 409
    assert pinned.json()["code"] == "pool.state_conflict"
    # 같은 계정에 hold 는 통한다(사고 대응 수단이라 상태를 따지지 않는다).
    assert client.post(f"{leased_base}/pool:hold").json()["poolState"] == "held"
    # 이미 held 면 다시 걸 수 없다.
    assert client.post(f"{leased_base}/pool:hold").status_code == 409

    assert client.post(f"{ready_base}/pool:pin").json()["poolState"] == "pinned"
    # pinned 에서 hold 로 넘어가는 것은 허용(고정 → 격리).
    assert client.post(f"{ready_base}/pool:hold").json()["poolState"] == "held"
    assert client.post(f"{ready_base}/pool:release").json()["poolState"] == "ready"


# -- 7) windows 폴백 ----------------------------------------------------------
def test_windows_without_ids_falls_back_to_legacy_fields(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1")
    account_id = _account(tenant_id, "legacy@x.example.com")

    # windows 껍데기는 왔지만 항목에 id 가 없다 — 그 필드는 쓸모가 없으므로
    # 같은 보고 안의 위치 필드로 폴백한다. 창을 통째로 잃는 것보다 낫다.
    payload = _payload(
        "legacy@x.example.com",
        {
            "windows": [{"pct": 11.0}, {"pct": 22.0}],
            "five_hour": {"pct": 33.0, "window_minutes": 300},
            "seven_day": {"pct": 44.0, "window_minutes": 10080},
        },
    )
    assert _ingest(tenant_id, server_id, payload) == 2
    rows = _windows(tenant_id, account_id)
    assert rows["five_hour"].pct == 33.0
    assert rows["seven_day"].pct == 44.0

    # 하나라도 id 가 살아 있으면 그게 정본이고 폴백하지 않는다.
    mixed = _payload(
        "legacy@x.example.com",
        {
            "windows": [{"pct": 11.0}, {"id": "five_hour", "pct": 55.0}],
            "seven_day": {"pct": 99.0},
        },
    )
    assert _ingest(tenant_id, server_id, mixed) == 1
    rows = _windows(tenant_id, account_id)
    assert rows["five_hour"].pct == 55.0
    assert rows["seven_day"].pct == 44.0  # 옛 값 그대로, 99 로 덮이지 않았다


# -- 8) Codex 서버당 1개 제한과 교체 상대 --------------------------------------
def test_codex_single_slot_still_allows_a_replacement_candidate(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "codex", {"mode": "auto"})
    hot_id = _account(tenant_id, "codexhot@x.example.com", provider="codex")
    spare_id = _account(tenant_id, "codexspare@x.example.com", provider="codex")
    _assign(tenant_id, hot_id, server_id)
    _ingest(tenant_id, server_id, _std("codexhot@x.example.com", five=94, seven=20))
    _ingest(tenant_id, server_id, _std("codexspare@x.example.com", five=2, seven=2))

    # 예열(prefetch)은 여전히 막힌다 — 호스트에 자격증명은 하나뿐이다.
    # 교체(swap)의 상대로는 뽑힌다: 안 그러면 소진된 Codex 서버가 영영 못 바뀐다.
    recs = _build(tenant_id)
    assert [r.kind for r in recs] == ["swap"]
    assert recs[0].from_account_id == hot_id
    assert recs[0].to_account_id == spare_id
