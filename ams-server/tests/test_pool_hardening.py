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
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import get_sessionmaker
from app.models import (
    Account,
    AccountUsageWindow,
    AgentCommand,
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
    credential_type = over.pop("credential_type", "oauth")
    with _db() as db:
        account = inventory.create_account(
            db,
            tenant_id,
            email=email,
            credential_type=credential_type,
            secret=secret,
            **over,
        )
        db.commit()
        return account.id


def _server(tenant_id, name, policy=None, status="online") -> uuid.UUID:
    with _db() as db:
        server = inventory.create_server(
            db, tenant_id, name=name, hostname=None, switch_mode="auto"
        )
        server.status = status
        # D3 (feat/sync-queued-timeout): request_deliver now 409s a server whose
        # agent has never connected (last_seen_at NULL). This helper already
        # simulates an online/offline server via `status`; last_seen_at must
        # agree or the pool chain's deliver step 409s on the first tick.
        server.last_seen_at = _now()
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


def _detach(tenant_id, account_id) -> None:
    """대여가 끝난 상태로 되돌린다 — 창 값은 남고 계정은 배급처로 돌아간다."""
    with _db() as db:
        for assignment in db.scalars(
            select(Assignment).where(
                Assignment.tenant_id == tenant_id,
                Assignment.account_id == account_id,
                Assignment.state != "detached",
            )
        ).all():
            assignment.state = "detached"
        account = db.get(Account, account_id)
        if account is not None and account.status == "assigned":
            account.status = "available"
        db.commit()


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


def _observe(tenant_id, server_id, payload, reported_at=None) -> int:
    """창 관측을 그 계정의 **마지막으로 알려진 값**으로 직접 적어 둔다.

    ``ingest_usage_report`` 는 이제 보고한 서버가 라이브 배정을 들고 있는 계정의
    창만 받는다(에이전트는 자기가 들고 있는 계정만 보고하고, 그 사실만이 서버
    쪽에서 검증된다). 그런데 배급처에서 대기 중인 계정에는 배정이 없다 — 실제
    운영에서 그 계정의 창 값은 **대여 중이던 시절에** 기록된 것이 남아 있는
    것이고, 이 헬퍼가 만드는 상태가 바로 그 상태다.
    """
    stamp = reported_at or _now()
    rows = 0
    with _db() as db:
        known = set(db.scalars(select(Account.id).where(Account.tenant_id == tenant_id)).all())
        index = {
            a.email.strip().lower(): a.id
            for a in db.scalars(select(Account).where(Account.tenant_id == tenant_id)).all()
            if a.email
        }
        for acc in payload.get("accounts", []):
            account_id = pool._resolve_account_id(acc, email_index=index, known=known)
            if account_id is None:
                continue
            for w in pool._window_rows(acc):
                stmt = pg_insert(AccountUsageWindow).values(
                    tenant_id=tenant_id,
                    account_id=account_id,
                    window_id=w["window_id"],
                    pct=w["pct"],
                    resets_at=w["resets_at"],
                    window_minutes=w["window_minutes"],
                    usage_fetched_at=pool._ts(acc.get("usage_fetched_at")),
                    reported_at=stamp,
                    server_id=server_id,
                )
                db.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["tenant_id", "account_id", "window_id"],
                        set_={
                            "pct": stmt.excluded.pct,
                            "resets_at": stmt.excluded.resets_at,
                            "reported_at": stmt.excluded.reported_at,
                            "server_id": stmt.excluded.server_id,
                        },
                    )
                )
                rows += 1
        db.commit()
    return rows




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
    _observe(healthy, healthy_server, _std("ok@x.example.com", five=1, seven=1))
    _observe(broken, broken_server, _std("bad@x.example.com", five=1, seven=1))
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
    _observe(
        tenant_id,
        server_id,
        _std("stalehot@x.example.com", five=93, seven=40),
        reported_at=_now() - timedelta(minutes=40),
    )
    _observe(tenant_id, server_id, _std("stalecool@x.example.com", five=5, seven=5))

    assert _build(tenant_id) == []

    # 같은 값이 지금 관측으로 다시 들어오면 그때 교체한다.
    _observe(tenant_id, server_id, _std("stalehot@x.example.com", five=93, seven=40))
    recs = _build(tenant_id)
    assert [r.kind for r in recs] == ["swap"]
    assert recs[0].from_account_id == hot_id


def test_unreadable_pct_is_unknown_not_zero(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", {"mode": "auto"})
    good_id = _account(tenant_id, "good@x.example.com")
    murky_id = _account(tenant_id, "murky@x.example.com")
    # 보고는 그 서버가 실제로 들고 있는 계정만 반영된다. 관측을 남긴 뒤 회수하면
    # 창 값은 남고 계정은 배급처로 돌아간다 — 실제 순환에서 후보가 갖는 상태다.
    _assign(tenant_id, good_id, server_id)
    _assign(tenant_id, murky_id, server_id)

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

    _detach(tenant_id, good_id)
    _detach(tenant_id, murky_id)
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
    _assign(tenant_id, account_id, server_id)
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
    _observe(tenant_id, offline_id, _std("waiting@x.example.com", five=1, seven=1))

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
    _observe(tenant_id, server_id, _std("sick@x.example.com", five=4, seven=4))
    _observe(tenant_id, server_id, _std("spare@x.example.com", five=6, seven=6))
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
    _observe(tenant_id, server_id, _std("quar@x.example.com", five=1, seven=1))
    _observe(tenant_id, server_id, _std("fine@x.example.com", five=50, seven=50))

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
    _observe(tenant_id, server_id, _std("drop@x.example.com", five=1, seven=1))
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
    _assign(tenant_id, account_id, server_id)

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
    _observe(tenant_id, server_id, _std("codexhot@x.example.com", five=94, seven=20))
    _observe(tenant_id, server_id, _std("codexspare@x.example.com", five=2, seven=2))

    # 예열(prefetch)은 여전히 막힌다 — 호스트에 자격증명은 하나뿐이다.
    # 교체(swap)의 상대로는 뽑힌다: 안 그러면 소진된 Codex 서버가 영영 못 바뀐다.
    recs = _build(tenant_id)
    assert [r.kind for r in recs] == ["swap"]
    assert recs[0].from_account_id == hot_id
    assert recs[0].to_account_id == spare_id


# -- B) 자동화 부적격 사유가 응답에 드러난다 ----------------------------------
def test_api_key_accounts_are_out_of_automation_and_say_why(app_env, client):
    """창 개념이 없는 자격증명은 자동화 밖이다(기획서 §4.7).

    사용률을 물어볼 수 없으니 언제 거둘지 판단할 근거가 없고, 창이 없으면 pct 가
    미상이라 정렬에서도 의미가 없다. 화면이 그 이유를 말해 주지 않으면 운영자는
    "왜 이 계정만 안 뽑히지"를 로그에서 찾게 된다.
    """
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", {"mode": "auto"})
    key_id = _account(tenant_id, "apikey@x.example.com", credential_type="api_key")
    oauth_id = _account(tenant_id, "oauth@x.example.com")
    excluded_id = _account(tenant_id, "excluded@x.example.com", assignment_excluded=True)
    _observe(tenant_id, server_id, _std("apikey@x.example.com", five=0, seven=0))
    _observe(tenant_id, server_id, _std("oauth@x.example.com", five=40, seven=40))
    _observe(tenant_id, server_id, _std("excluded@x.example.com", five=0, seven=0))

    # 잔여량만 보면 api_key 와 excluded 가 앞서지만 둘 다 후보가 아니다.
    recs = _build(tenant_id)
    assert [r.to_account_id for r in recs] == [oauth_id]

    body = client.get(f"/api/v1/tenants/{tenant_id}/pool").json()
    by_id = {a["accountId"]: a for a in body["accounts"]}
    assert by_id[str(key_id)]["autoEligible"] is False
    assert by_id[str(key_id)]["ineligibleReason"] == "api_key"
    assert by_id[str(excluded_id)]["ineligibleReason"] == "excluded"
    assert by_id[str(oauth_id)]["autoEligible"] is True
    assert by_id[str(oauth_id)]["ineligibleReason"] is None

    # 관측을 못 읽은 계정도 이유를 말한다.
    murky_id = _account(tenant_id, "murky2@x.example.com")
    _observe(
        tenant_id,
        server_id,
        _payload("murky2@x.example.com", {"windows": [{"id": "five_hour", "pct": "??"}]}),
    )
    body = client.get(f"/api/v1/tenants/{tenant_id}/pool").json()
    murky = next(a for a in body["accounts"] if a["accountId"] == str(murky_id))
    assert murky["ineligibleReason"] == "no_observation"
    # 미상은 0.0 이 아니라 null 로 나간다 — 0% 를 그리면 여유가 가득해 보인다.
    assert murky["windows"][0]["pct"] is None


# -- C) Codex 는 자리를 비우고 나서 올린다 -------------------------------------
def test_codex_swap_recalls_before_delivering(app_env):
    """Codex 호스트는 자격증명을 하나만 갖는다 — 핸드오프를 만들 수 없다.

    그래서 이 조합만 recall → deliver → switch 순서로 돈다. 그 사이 서버에는 활성
    계정이 없고, 그 대가는 권고 문장에 적힌다. 대안은 "소진된 Codex 서버를 영영
    못 바꾼다"이므로 공백을 감수한다.
    """
    from app.models import PoolChain

    tenant_id = _tenant()
    server_id = _server(tenant_id, "codex", {"mode": "auto"})
    hot_id = _account(tenant_id, "cxhot@x.example.com", provider="codex")
    next_id = _account(tenant_id, "cxnext@x.example.com", provider="codex")
    hot_assignment = _assign(tenant_id, hot_id, server_id)
    _observe(tenant_id, server_id, _std("cxhot@x.example.com", five=95, seven=30))
    _observe(tenant_id, server_id, _std("cxnext@x.example.com", five=3, seven=3))

    recs = _build(tenant_id)
    assert [r.kind for r in recs] == ["swap"]
    assert "Codex 는 호스트당 계정이 하나뿐" in recs[0].reason

    with _db() as db:
        chain = pool.start_chain(
            db, db.get(PoolRecommendation, recs[0].id), actor="op@x.example.com"
        )
        chain_id = chain.id
    # 전달이 아니라 회수부터다.
    with _db() as db:
        assert db.get(PoolChain, chain_id).step == "recall"
        assert db.get(Assignment, hot_assignment).state == "recalling"
        assert (
            db.scalars(
                select(Assignment).where(
                    Assignment.account_id == next_id, Assignment.state != "detached"
                )
            ).first()
            is None
        )

    # 자리가 비면 그때 올린다.
    with _db() as db:
        db.get(Assignment, hot_assignment).state = "detached"
        db.commit()
    with _db() as db:
        assert pool.advance_chains(db) == 1
    with _db() as db:
        assert db.get(PoolChain, chain_id).step == "deliver"
    with _db() as db:
        assert pool.advance_chains(db) == 1
    incoming = _assignment_of(tenant_id, next_id)
    assert incoming is not None
    assert incoming.state == "delivering"

    # 설치가 끝나면 전환하고, 회수는 이미 끝났으므로 그대로 완료된다.
    with _db() as db:
        db.get(Assignment, incoming.id).state = "active"
        db.commit()
    with _db() as db:
        assert pool.advance_chains(db) == 1
        assert db.get(PoolChain, chain_id).step == "switch"
    with _db() as db:
        pool.advance_chains(db)
        command_id = db.get(PoolChain, chain_id).command_id
    assert command_id is not None
    with _db() as db:
        command = db.scalars(
            select(AgentCommand).where(AgentCommand.command_id == command_id)
        ).one()
        assert command.command_type == "switch_now"
        command.status = "acked"
        db.commit()
    with _db() as db:
        assert pool.advance_chains(db) == 1
        assert db.get(PoolChain, chain_id).step == "done"


def _assignment_of(tenant_id, account_id):
    with _db() as db:
        return db.scalars(
            select(Assignment).where(
                Assignment.tenant_id == tenant_id,
                Assignment.account_id == account_id,
                Assignment.state != "detached",
            )
        ).first()


# -- H2) 잔여 pending 배정이 서버를 영구 in-flight 로 만들지 않는다 ------------
def test_pending_assignment_without_a_live_command_is_not_in_flight(app_env):
    """명령이 하나도 큐에 없는 pending 은 "움직이는 중"이 아니다.

    그걸 in-flight 로 세면 그 서버는 사람이 손대기 전까지 자동화에서 영구히
    제외된다 — 아무도 그 배정을 밀고 있지 않은데도.
    """
    from app.core.errors import ApiError

    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", {"mode": "auto", "target_leases": 2})
    stray_id = _account(tenant_id, "stray@x.example.com")
    free_id = _account(tenant_id, "free@x.example.com")
    # 배정 행만 만들고 전달 명령은 내지 않는다(수동 API 의 정상 경로이자, 실패한
    # 체인이 정리되기 전에 남길 수 있는 모양이기도 하다).
    with _db() as db:
        inventory.create_assignment(
            db, tenant_id, account_id=stray_id, server_id=server_id, pinned=False
        )
    _observe(tenant_id, server_id, _std("free@x.example.com", five=2, seven=2))
    rec_id = _mk_rec(tenant_id, server_id, "prefetch", from_id=stray_id, to_id=free_id)

    # 아무 명령도 큐에 없으므로 이 서버는 in-flight 가 아니다.
    with _db() as db:
        chain = pool.start_chain(
            db, db.get(PoolRecommendation, rec_id), actor="op@x.example.com"
        )
        assert chain.to_account_id == free_id

    # 반대로 명령이 실제로 큐에 있으면 그 서버는 in-flight 다.
    other_id = _account(tenant_id, "other@x.example.com")
    second = _mk_rec(tenant_id, server_id, "prefetch", from_id=stray_id, to_id=other_id)
    with _db() as db:
        try:
            pool.start_chain(
                db, db.get(PoolRecommendation, second), actor="op@x.example.com"
            )
        except ApiError as exc:
            # 앞 체인이 이미 돌고 있으므로 chain_active 가 먼저 걸린다.
            assert exc.code in ("pool.chain_active", "pool.server_in_flight")
        else:  # pragma: no cover
            raise AssertionError("두 번째 체인이 착수됐다")


# -- M4) 한 계정을 두 서버가 동시에 노리지 않는다 ------------------------------
def test_one_account_is_never_targeted_by_two_servers(app_env):
    from app.core.errors import ApiError
    from app.models import PoolChain

    tenant_id = _tenant()
    server_a = _server(tenant_id, "a", {"mode": "auto"})
    server_b = _server(tenant_id, "b", {"mode": "auto"})
    only_id = _account(tenant_id, "only@x.example.com")
    _observe(tenant_id, server_a, _std("only@x.example.com", five=1, seven=1))

    # 계정이 하나면 권고도 하나다. 둘을 만들면 배정 유니크 제약이 반드시 한쪽을
    # 실패시키고, 그 서버는 운영자 확인을 기다리며 멈춘다.
    recs = _build(tenant_id)
    assert len(recs) == 1
    owner = recs[0].server_id
    other = server_b if owner == server_a else server_a

    with _db() as db:
        pool.start_chain(db, db.get(PoolRecommendation, recs[0].id), actor="op@x.example.com")

    # 체인이 도는 동안에도 예약은 유효하다 — 다른 서버에 손으로 권고를 놓고
    # 실행하려 해도 막힌다.
    rec_id = _mk_rec(tenant_id, other, "lease", to_id=only_id)
    with _db() as db:
        try:
            pool.start_chain(db, db.get(PoolRecommendation, rec_id), actor="op@x.example.com")
        except ApiError as exc:
            assert exc.status == 409
            assert exc.code == "pool.target_reserved"
        else:  # pragma: no cover
            raise AssertionError("두 서버가 같은 계정을 잡았다")
    with _db() as db:
        assert (
            len(db.scalars(select(PoolChain).where(PoolChain.tenant_id == tenant_id)).all())
            == 1
        )


def _mk_rec(tenant_id, server_id, kind, *, from_id=None, to_id=None) -> uuid.UUID:
    with _db() as db:
        row = PoolRecommendation(
            tenant_id=tenant_id,
            server_id=server_id,
            kind=kind,
            from_account_id=from_id,
            to_account_id=to_id,
            reason="테스트가 놓은 권고",
        )
        db.add(row)
        db.commit()
        return row.id


# -- M5) 도는 체인 위로 수동 조작이 끼어들지 못한다 ----------------------------
def test_manual_assignment_actions_are_blocked_while_a_chain_runs(app_env, client):
    from app.models import PoolChain

    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", {"mode": "auto", "target_leases": 2})
    held_id = _account(tenant_id, "held@x.example.com")
    _account(tenant_id, "incoming@x.example.com")
    held_assignment = _assign(tenant_id, held_id, server_id)
    _observe(tenant_id, server_id, _std("held@x.example.com", five=96, seven=50))
    _observe(tenant_id, server_id, _std("incoming@x.example.com", five=2, seven=2))

    recs = _build(tenant_id)
    assert [r.kind for r in recs] == ["swap"]
    with _db() as db:
        chain = pool.start_chain(
            db, db.get(PoolRecommendation, recs[0].id), actor="op@x.example.com"
        )
        chain_id = chain.id

    base = f"/api/v1/tenants/{tenant_id}/assignments/{held_assignment}"
    for action in (":recall", ":switch-now"):
        response = client.post(f"{base}{action}")
        assert response.status_code == 409, action
        assert response.json()["code"] == "pool.chain_active"

    # 체인은 그대로 살아 있고 배정도 움직이지 않았다.
    with _db() as db:
        assert db.get(PoolChain, chain_id).step in ("deliver", "switch")
        assert db.get(Assignment, held_assignment).state == "active"

    # force 회수는 탈출구다: 막지 않되 체인을 실패로 접는다.
    forced = client.post(f"{base}:recall", json={"force": True})
    assert forced.status_code == 200
    with _db() as db:
        failed = db.get(PoolChain, chain_id)
        assert failed.step == "failed"
        assert "강제" in failed.error
        assert db.get(Assignment, held_assignment).state == "recalling"


def test_manual_actions_are_untouched_without_a_chain(app_env, client):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1")
    account_id = _account(tenant_id, "plain@x.example.com")
    assignment_id = _assign(tenant_id, account_id, server_id)

    base = f"/api/v1/tenants/{tenant_id}/assignments/{assignment_id}"
    assert client.post(f"{base}:switch-now").status_code == 200
    assert client.post(f"{base}:recall").json()["state"] == "recalling"


# -- M6) 초과분 회수도 신선한 관측과 최소 대여를 지킨다 ------------------------
def test_recall_idle_skips_fresh_leases_and_stale_readings(app_env):
    tenant_id = _tenant()
    server_id = _server(
        tenant_id, "s1", {"mode": "auto", "target_leases": 1, "min_lease_minutes": 30}
    )
    old_id = _account(tenant_id, "old@x.example.com")
    fresh_id = _account(tenant_id, "fresh@x.example.com")
    _assign(tenant_id, old_id, server_id)  # delivered_at = 4시간 전
    with _db() as db:
        assignment = inventory.create_assignment(
            db, tenant_id, account_id=fresh_id, server_id=server_id, pinned=False
        )
        assignment.state = "active"
        assignment.delivered_at = _now()  # 방금 올렸다
        db.commit()

    # 방금 올린 계정이 가장 소진돼 보여도 거두지 않는다 — min_lease 안이다.
    _observe(tenant_id, server_id, _std("fresh@x.example.com", five=80, seven=80))
    _observe(tenant_id, server_id, _std("old@x.example.com", five=10, seven=10))
    recs = _build(tenant_id)
    assert [r.kind for r in recs] == ["recall_idle"]
    assert recs[0].from_account_id == old_id
    assert recs[0].trigger_pct == 10.0

    # 낡은 관측은 순위 재료가 아니다: old 의 값이 40분 전 것이면 미상으로 떨어져
    # trigger_pct 가 빈다. 살아 있는 권고 행은 조건이 같으면 갱신되지 않으므로
    # (created_at 이 조건의 시작 시각을 유지한다) 먼저 치운다.
    with _db() as db:
        for row in db.scalars(
            select(PoolRecommendation).where(PoolRecommendation.tenant_id == tenant_id)
        ).all():
            db.delete(row)
        db.commit()
    _observe(
        tenant_id,
        server_id,
        _std("old@x.example.com", five=10, seven=10),
        reported_at=_now() - timedelta(minutes=40),
    )
    recs = _build(tenant_id)
    assert [r.kind for r in recs] == ["recall_idle"]
    assert recs[0].from_account_id == old_id
    assert recs[0].trigger_pct is None
