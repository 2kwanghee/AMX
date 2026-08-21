"""계정 풀 P2+P3 — 체인 실행기와 자동 모드.

검증: 네 종류(lease/prefetch/swap/recall_idle)의 정상 전진 / 단계 타임아웃이 실패와
``pool_chain_failed`` 경보를 만든다 / 서버당 체인 1개 / 일시정지는 신규 착수만 막고
도는 체인은 끝난다 / 실패한 체인은 확인(:ack) 전까지 그 서버의 자동 실행을 막는다 /
사라진 권고에 :apply 하면 409 / manual 서버는 자동 실행되지 않는다 / 테넌트 격리.

배정의 수렴(delivering→active, recalling→detached)은 reconcile 이 에이전트의 ack 로
하는 일이라 여기서는 테스트가 직접 그 상태를 적어 넣고 스윕을 부른다 — 체인이
검사하는 것은 ack 자체가 아니라 배정이 도달한 상태이기 때문이다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models import (
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
from app.services import inventory, pool

AUTO = {"mode": "auto", "target_leases": 1}


def _now() -> datetime:
    return datetime.now(UTC)


def _db():
    from app.db import get_sessionmaker

    return get_sessionmaker()()


def _tenant() -> uuid.UUID:
    with _db() as db:
        return inventory.create_tenant(db, "chain-" + uuid.uuid4().hex[:8]).id


def _account(tenant_id: uuid.UUID, email: str) -> uuid.UUID:
    with _db() as db:
        account = inventory.create_account(
            db, tenant_id, email=email, credential_type="oauth", secret="k"
        )
        db.commit()
        return account.id


def _server(tenant_id: uuid.UUID, name: str, policy: dict | None = None) -> uuid.UUID:
    with _db() as db:
        server = inventory.create_server(
            db, tenant_id, name=name, hostname=None, switch_mode="auto"
        )
        server.status = "online"
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


def _report(email: str, *, five: float, seven: float) -> dict:
    stamp = (_now() + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    return {
        "accounts": [
            {
                "account": {"ams_account_id": "", "email": email},
                "usage_fetched_at": _now().isoformat().replace("+00:00", "Z"),
                "windows": [
                    {"id": "five_hour", "pct": five, "resets_at": stamp},
                    {"id": "seven_day", "pct": seven, "resets_at": stamp},
                ],
            }
        ]
    }


def _ingest(tenant_id, server_id, email, *, five, seven) -> None:
    with _db() as db:
        pool.ingest_usage_report(
            db,
            tenant_id=tenant_id,
            server_id=server_id,
            payload=_report(email, five=five, seven=seven),
            reported_at=_now(),
        )
        db.commit()


def _observe(tenant_id, server_id, email, *, five, seven, reported_at=None) -> int:
    """이 파일의 ``_ingest`` 와 같은 시그니처를 갖는 얇은 껍데기."""
    return _observe_payload(
        tenant_id, server_id, _report(email, five=five, seven=seven), reported_at
    )


def _observe_payload(tenant_id, server_id, payload, reported_at=None) -> int:
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




def _build(tenant_id) -> list[PoolRecommendation]:
    with _db() as db:
        pool.build_recommendations(db, tenant_id)
        db.commit()
        return list(
            db.scalars(
                select(PoolRecommendation).where(PoolRecommendation.tenant_id == tenant_id)
            ).all()
        )


def _mk_rec(tenant_id, server_id, kind, *, from_id=None, to_id=None) -> uuid.UUID:
    """조건 계산을 거치지 않고 권고 한 줄을 직접 놓는다.

    자동 착수의 빗장(정책·체인·실패 확인·일시정지)만 보고 싶은 테스트가 권고를
    만들어 내려고 창 관측과 임계를 통과시키는 일에 매달릴 이유는 없다.
    """
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


def _clear_recommendations(tenant_id) -> None:
    with _db() as db:
        for row in db.scalars(
            select(PoolRecommendation).where(PoolRecommendation.tenant_id == tenant_id)
        ).all():
            db.delete(row)
        db.commit()


def _start(rec_id: uuid.UUID, *, actor="op@x.example.com", now=None) -> PoolChain:
    with _db() as db:
        rec = db.get(PoolRecommendation, rec_id)
        chain = pool.start_chain(db, rec, actor=actor, now=now)
        db.expunge_all()
        return chain


def _advance(now=None) -> int:
    with _db() as db:
        return pool.advance_chains(db, now=now)


def _auto(now=None) -> int:
    with _db() as db:
        return pool.start_auto_chains(db, now=now)


def _chain(chain_id) -> PoolChain:
    with _db() as db:
        return db.get(PoolChain, chain_id)


def _set_assignment_state(assignment_id, state) -> None:
    with _db() as db:
        assignment = db.get(Assignment, assignment_id)
        assignment.state = state
        if state == "active":
            assignment.delivered_at = _now()
        assignment.pending_command_id = None
        db.commit()


def _assignment_of(tenant_id, account_id) -> Assignment | None:
    with _db() as db:
        return db.scalars(
            select(Assignment).where(
                Assignment.tenant_id == tenant_id,
                Assignment.account_id == account_id,
                Assignment.state != "detached",
            )
        ).first()


def _commands(tenant_id, command_type) -> list[AgentCommand]:
    with _db() as db:
        return list(
            db.scalars(
                select(AgentCommand).where(
                    AgentCommand.tenant_id == tenant_id,
                    AgentCommand.command_type == command_type,
                )
            ).all()
        )


def _ack_command(command_id: str, status="acked") -> None:
    with _db() as db:
        command = db.scalars(
            select(AgentCommand).where(AgentCommand.command_id == command_id)
        ).first()
        command.status = status
        db.commit()


def _events(tenant_id, kind) -> list[PoolEvent]:
    with _db() as db:
        return list(
            db.scalars(
                select(PoolEvent).where(
                    PoolEvent.tenant_id == tenant_id, PoolEvent.kind == kind
                )
            ).all()
        )


def _set_paused(tenant_id, paused: bool) -> None:
    with _db() as db:
        pool.set_automation_paused(db, tenant_id, paused=paused, actor="op@x.example.com")


# -- 종류별 정상 전진 ---------------------------------------------------------
def test_lease_chain_delivers_then_switches(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", AUTO)
    account_id = _account(tenant_id, "lease@x.example.com")
    _observe(tenant_id, server_id, "lease@x.example.com", five=5, seven=5)

    recs = _build(tenant_id)
    assert [r.kind for r in recs] == ["lease"]

    chain = _start(recs[0].id)
    assert chain.kind == "lease"
    assert chain.step == "deliver"
    # 첫 단계는 착수와 함께 나간다: 배정 생성 + deliver 명령.
    assignment = _assignment_of(tenant_id, account_id)
    assert assignment is not None
    assert assignment.state == "delivering"
    assert len(_commands(tenant_id, "deliver")) == 1
    # 권고는 소비됐으므로 사라진다 — 같은 계획을 두 번 누를 수 없다.
    assert _build(tenant_id) == [] or all(r.id != recs[0].id for r in _build(tenant_id))

    # 에이전트가 설치를 마치면(active) 전환 단계로.
    _set_assignment_state(assignment.id, "active")
    assert _advance() == 1
    assert _chain(chain.id).step == "switch"

    # 전환 단계는 switch_now 를 한 번만 낸다.
    assert _advance() == 1
    switches = _commands(tenant_id, "switch_now")
    assert len(switches) == 1
    assert _chain(chain.id).command_id == switches[0].command_id
    assert _advance() == 0  # ack 전에는 재발행 없음
    assert len(_commands(tenant_id, "switch_now")) == 1

    _ack_command(switches[0].command_id)
    assert _advance() == 1
    assert _chain(chain.id).step == "done"
    assert len(_events(tenant_id, "chain_done")) == 1


def test_prefetch_chain_stops_after_delivery(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", AUTO)
    warm_id = _account(tenant_id, "warm@x.example.com")
    spare_id = _account(tenant_id, "spare@x.example.com")
    _assign(tenant_id, warm_id, server_id, "active")
    _observe(tenant_id, server_id, "warm@x.example.com", five=75, seven=10)
    _observe(tenant_id, server_id, "spare@x.example.com", five=1, seven=1)

    recs = _build(tenant_id)
    assert [r.kind for r in recs] == ["prefetch"]
    chain = _start(recs[0].id)
    assert chain.kind == "prefetch"

    assignment = _assignment_of(tenant_id, spare_id)
    assert assignment.state == "delivering"
    _set_assignment_state(assignment.id, "active")
    assert _advance() == 1
    # 예열은 올려 두는 것까지다. 전환도 회수도 하지 않는다.
    assert _chain(chain.id).step == "done"
    assert _commands(tenant_id, "switch_now") == []
    assert _commands(tenant_id, "recall") == []


def test_swap_chain_switches_then_recalls(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", {"mode": "auto", "target_leases": 2})
    hot_id = _account(tenant_id, "hot@x.example.com")
    cool_id = _account(tenant_id, "cool@x.example.com")
    hot_assignment = _assign(tenant_id, hot_id, server_id, "active")
    _assign(tenant_id, cool_id, server_id, "active")
    _observe(tenant_id, server_id, "hot@x.example.com", five=93, seven=40)
    _observe(tenant_id, server_id, "cool@x.example.com", five=5, seven=5)

    recs = _build(tenant_id)
    assert [r.kind for r in recs] == ["swap"]
    assert recs[0].from_account_id == hot_id
    assert recs[0].to_account_id == cool_id

    chain = _start(recs[0].id)
    assert chain.kind == "swap"
    # swap 은 이미 설치된 계정으로 넘기는 것이므로 자격증명 재전송이 없다.
    assert _commands(tenant_id, "deliver") == []
    switches = _commands(tenant_id, "switch_now")
    assert len(switches) == 1
    assert _chain(chain.id).step == "switch"

    _ack_command(switches[0].command_id)
    assert _advance() == 1
    assert _chain(chain.id).step == "recall"

    assert _advance() == 1
    assert len(_commands(tenant_id, "recall")) == 1
    with _db() as db:
        assert db.get(Assignment, hot_assignment).state == "recalling"

    _set_assignment_state(hot_assignment, "detached")
    assert _advance() == 1
    assert _chain(chain.id).step == "done"
    with _db() as db:
        assert db.get(Account, hot_id).last_lease_ended_at is not None


def test_recall_idle_chain(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", AUTO)
    keep_id = _account(tenant_id, "keep@x.example.com")
    drop_id = _account(tenant_id, "drop@x.example.com")
    _assign(tenant_id, keep_id, server_id, "active")
    drop_assignment = _assign(tenant_id, drop_id, server_id, "active")
    _observe(tenant_id, server_id, "keep@x.example.com", five=5, seven=5)
    _observe(tenant_id, server_id, "drop@x.example.com", five=30, seven=10)

    recs = _build(tenant_id)
    assert [r.kind for r in recs] == ["recall_idle"]
    assert recs[0].from_account_id == drop_id

    chain = _start(recs[0].id)
    assert chain.step == "recall"
    assert len(_commands(tenant_id, "recall")) == 1

    _set_assignment_state(drop_assignment, "detached")
    assert _advance() == 1
    assert _chain(chain.id).step == "done"


# -- 실패 경로 ----------------------------------------------------------------
def test_step_timeout_fails_chain_and_opens_alert(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", AUTO)
    account_id = _account(tenant_id, "slow@x.example.com")
    _observe(tenant_id, server_id, "slow@x.example.com", five=1, seven=1)

    chain = _start(_build(tenant_id)[0].id)
    assert chain.step == "deliver"

    # 에이전트가 응답하지 않아 배정이 delivering 에서 멈춘 채 제한 시간이 지났다.
    assert _advance(now=_now() + timedelta(minutes=11)) == 1
    failed = _chain(chain.id)
    assert failed.step == "failed"
    assert "제한 시간" in failed.error
    assert failed.acked_at is None

    with _db() as db:
        alert = db.scalar(
            select(Alert).where(
                Alert.tenant_id == tenant_id, Alert.kind == "pool_chain_failed"
            )
        )
    assert alert is not None
    assert alert.status == "open"
    assert alert.severity == "warning"
    assert alert.server_id == server_id
    assert alert.detail["chain_id"] == str(chain.id)
    assert len(_events(tenant_id, "chain_failed")) == 1

    # 롤백 "명령"은 자동으로 나가지 않는다 — 이미 반영된 것을 되돌릴지는 사람이 정한다.
    assert _commands(tenant_id, "recall") == []
    # 그러나 아직 도착하지 않은 자기 명령은 접는다. 큐에 남겨 두면 몇 시간 뒤
    # 에이전트가 돌아왔을 때 아무도 기다리지 않는 계획이 뒤늦게 실행된다.
    delivers = _commands(tenant_id, "deliver")
    assert [c.status for c in delivers] == ["failed"]
    assert "pool chain aborted" in delivers[0].detail
    # 그 명령이 만든 배정도 되돌린다 — pending 인 채로 남으면 그 서버는 영구히
    # in-flight 로 보여 자동화가 영영 아무것도 못 한다.
    assert _assignment_of(tenant_id, account_id) is None
    with _db() as db:
        assert db.get(Account, account_id).status == "available"


def test_failed_chain_blocks_auto_until_acked(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", AUTO)
    account_id = _account(tenant_id, "blocked@x.example.com")
    _observe(tenant_id, server_id, "blocked@x.example.com", five=1, seven=1)

    chain = _start(_build(tenant_id)[0].id)
    _advance(now=_now() + timedelta(minutes=11))
    assert _chain(chain.id).step == "failed"

    # 실패를 확인하기 전에는 같은 서버에서 자동 착수가 열리지 않는다.
    other_id = _account(tenant_id, "other@x.example.com")
    rec_id = _mk_rec(tenant_id, server_id, "lease", to_id=other_id)
    assert _auto() == 0

    with _db() as db:
        pool.ack_chain(db, db.get(PoolChain, chain.id), actor="op@x.example.com")
    assert _chain(chain.id).acked_at is not None
    with _db() as db:
        alert = db.scalar(
            select(Alert).where(
                Alert.tenant_id == tenant_id, Alert.kind == "pool_chain_failed"
            )
        )
    assert alert.status == "resolved"

    # 확인 뒤에는 다시 열린다. 앞 체인이 만든 배정은 실패와 함께 이미 정리됐다.
    assert _assignment_of(tenant_id, account_id) is None
    assert _auto() == 1
    with _db() as db:
        started = db.scalars(
            select(PoolChain).where(
                PoolChain.tenant_id == tenant_id, PoolChain.recommendation_id == rec_id
            )
        ).first()
    assert started is not None
    assert started.actor == "pool-controller"


# -- 동시성·정책 빗장 ---------------------------------------------------------
def test_one_chain_per_server(app_env):
    from app.core.errors import ApiError

    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", AUTO)
    first_id = _account(tenant_id, "first@x.example.com")
    _observe(tenant_id, server_id, "first@x.example.com", five=1, seven=1)

    # 후보가 하나뿐일 때 권고를 뽑아야 어느 계정이 뽑혔는지가 결정적이다.
    _start(_build(tenant_id)[0].id)
    second_id = _account(tenant_id, "second@x.example.com")
    rec_id = _mk_rec(tenant_id, server_id, "lease", to_id=second_id)

    try:
        _start(rec_id)
    except ApiError as exc:
        assert exc.status == 409
        assert exc.code == "pool.chain_active"
    else:  # pragma: no cover
        raise AssertionError("두 번째 체인이 착수됐다")

    # 자동 경로도 같은 빗장에 걸린다.
    assert _auto() == 0
    assert _assignment_of(tenant_id, second_id) is None
    assert _assignment_of(tenant_id, first_id) is not None


def test_active_chain_suppresses_new_recommendations(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", AUTO)
    _account(tenant_id, "busy@x.example.com")
    _observe(tenant_id, server_id, "busy@x.example.com", five=1, seven=1)

    _start(_build(tenant_id)[0].id)
    # 체인이 도는 동안은 "지금이라면 이렇게 하겠다"를 새로 그리지 않는다.
    assert _build(tenant_id) == []


def test_manual_server_is_never_auto_started(app_env):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", {"mode": "manual"})
    account_id = _account(tenant_id, "manual@x.example.com")
    _mk_rec(tenant_id, server_id, "lease", to_id=account_id)

    assert _auto() == 0
    assert _assignment_of(tenant_id, account_id) is None


def test_pause_blocks_new_chains_but_lets_running_ones_finish(app_env):
    tenant_id = _tenant()
    server_a = _server(tenant_id, "a", AUTO)
    server_b = _server(tenant_id, "b", AUTO)
    running_id = _account(tenant_id, "running@x.example.com")
    _observe(tenant_id, server_a, "running@x.example.com", five=1, seven=1)

    # 계정이 하나뿐이므로 권고도 하나뿐이다 — 한 계정을 두 서버가 동시에 노리면
    # 배정 유니크 제약이 한쪽을 반드시 실패시킨다(예약 규칙).
    recs = _build(tenant_id)
    assert len(recs) == 1
    chain = _start(recs[0].id)
    running_server = recs[0].server_id
    idle_server = server_b if running_server == server_a else server_a

    # 남은 권고를 치우고, 두 번째 서버에 다른 계정으로 하나만 놓는다 — 착수를 막는
    # 것이 일시정지인지 "계정이 이미 물려 있음"인지 헷갈리지 않게.
    _clear_recommendations(tenant_id)
    idle_id = _account(tenant_id, "idle@x.example.com")
    _mk_rec(tenant_id, idle_server, "lease", to_id=idle_id)

    _set_paused(tenant_id, True)
    assert _auto() == 0
    assert _assignment_of(tenant_id, idle_id) is None
    assert len(_events(tenant_id, "automation_paused")) == 1

    # 도는 체인은 끝까지 간다 — 중간에 멈추면 서버가 무자격 상태로 남는다.
    _set_assignment_state(_assignment_of(tenant_id, running_id).id, "active")
    assert _advance() == 1
    assert _chain(chain.id).step == "switch"
    assert _advance() == 1
    _ack_command(_commands(tenant_id, "switch_now")[0].command_id)
    assert _advance() == 1
    assert _chain(chain.id).step == "done"

    _set_paused(tenant_id, False)
    assert len(_events(tenant_id, "automation_resumed")) == 1
    assert _auto() == 1


def test_auto_chain_concurrency_cap(app_env, monkeypatch):
    from app.config import get_settings

    tenant_id = _tenant()
    servers = [_server(tenant_id, f"s{i}", AUTO) for i in range(3)]
    accounts = [_account(tenant_id, f"cap{i}@x.example.com") for i in range(3)]
    for server_id, account_id in zip(servers, accounts, strict=True):
        _mk_rec(tenant_id, server_id, "lease", to_id=account_id)

    monkeypatch.setenv("AMX_POOL_MAX_CONCURRENT_CHAINS", "2")
    get_settings.cache_clear()
    try:
        assert _auto() == 2
        assert _auto() == 0  # 상한에 걸려 세 번째는 대기
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


# -- REST ---------------------------------------------------------------------
def test_apply_stale_recommendation_returns_409(app_env, client):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", AUTO)
    account_id = _account(tenant_id, "rest@x.example.com")
    _observe(tenant_id, server_id, "rest@x.example.com", five=1, seven=1)
    rec = _build(tenant_id)[0]

    base = f"/api/v1/tenants/{tenant_id}"
    body = client.post(f"{base}/pool/recommendations/{rec.id}:apply").json()
    assert body["kind"] == "lease"
    assert body["step"] == "deliver"
    assert body["actor"]

    # 같은 권고를 다시 누르면, 이미 소비됐으므로 409 로 "다시 읽어라".
    again = client.post(f"{base}/pool/recommendations/{rec.id}:apply")
    assert again.status_code == 409
    assert again.json()["code"] == "pool.recommendation_stale"

    missing = client.post(f"{base}/pool/recommendations/{uuid.uuid4()}:apply")
    assert missing.status_code == 409

    active = client.get(f"{base}/pool/chains?status=active").json()
    assert [c["id"] for c in active] == [body["id"]]
    assert client.get(f"{base}/pool/chains?status=all").json()[0]["id"] == body["id"]
    assert client.get(f"{base}/pool/chains?status=bogus").status_code == 422
    assert _assignment_of(tenant_id, account_id) is not None


def test_pause_resume_endpoints_and_chain_ack(app_env, client):
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", AUTO)
    _account(tenant_id, "sw@x.example.com")
    _observe(tenant_id, server_id, "sw@x.example.com", five=1, seven=1)
    base = f"/api/v1/tenants/{tenant_id}"

    assert client.post(f"{base}/pool:pause").json() == {"automationPaused": True}
    assert client.get(f"{base}/pool").json()["automationPaused"] is True
    assert client.post(f"{base}/pool:resume").json() == {"automationPaused": False}
    with _db() as db:
        assert db.get(Tenant, tenant_id).pool_automation_paused is False

    chain = _start(_build(tenant_id)[0].id)
    # 아직 실패하지 않은 체인은 확인할 것이 없다.
    assert client.post(f"{base}/pool/chains/{chain.id}:ack").status_code == 409
    _advance(now=_now() + timedelta(minutes=11))
    acked = client.post(f"{base}/pool/chains/{chain.id}:ack").json()
    assert acked["step"] == "failed"
    assert acked["ackedAt"] is not None
    assert client.post(f"{base}/pool/chains/{uuid.uuid4()}:ack").status_code == 404


def test_chain_tenant_isolation(app_env, client):
    tenant_a = _tenant()
    tenant_b = _tenant()
    server_a = _server(tenant_a, "a", AUTO)
    _account(tenant_a, "a@x.example.com")
    _observe(tenant_a, server_a, "a@x.example.com", five=1, seven=1)
    rec = _build(tenant_a)[0]
    chain = _start(rec.id)

    assert client.get(f"/api/v1/tenants/{tenant_b}/pool/chains?status=all").json() == []
    cross = client.post(f"/api/v1/tenants/{tenant_b}/pool/chains/{chain.id}:ack")
    assert cross.status_code == 404
    cross_apply = client.post(
        f"/api/v1/tenants/{tenant_b}/pool/recommendations/{rec.id}:apply"
    )
    assert cross_apply.status_code == 409

    # B 를 일시정지해도 A 의 체인은 그대로 돈다.
    _set_paused(tenant_b, True)
    with _db() as db:
        assert db.get(Server, server_a).tenant_id == tenant_a
    assert _chain(chain.id).step == "deliver"


# -- 단계 시계는 재발행으로 되감기지 않는다 -----------------------------------
def test_redelivery_does_not_rewind_the_step_clock(app_env):
    """명령 outbox 의 되돌림 주기가 단계 제한 시간보다 짧아도 무한 재발행이 아니다.

    ack 없는 ``sent`` 는 최대 5회 재큐잉되다 실패로 접히고 배정이 ``pending`` 으로
    되돌아온다. 컨트롤러는 그 ``pending`` 을 보고 전달을 다시 내는데, 그때 단계
    시계까지 되감기면 그 단계는 만료에 영영 닿지 못하고 계속 재발행만 한다.
    """
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", AUTO)
    account_id = _account(tenant_id, "redeliver@x.example.com")
    _observe(tenant_id, server_id, "redeliver@x.example.com", five=1, seven=1)

    chain = _start(_build(tenant_id)[0].id)
    first_clock = _chain(chain.id).step_started_at
    assignment = _assignment_of(tenant_id, account_id)
    assert assignment.state == "delivering"

    # outbox 가 명령을 접고 배정을 pending 으로 되돌린 상태를 만든다.
    with _db() as db:
        command = db.scalars(
            select(AgentCommand).where(AgentCommand.command_type == "deliver")
        ).one()
        command.status = "failed"
        target = db.get(Assignment, assignment.id)
        target.state = "pending"
        target.pending_command_id = None
        db.commit()

    assert _advance() == 1  # 전달을 다시 낸다
    assert len(_commands(tenant_id, "deliver")) == 2
    assert _chain(chain.id).step == "deliver"
    # 단계 시계는 그대로다. updated_at 은 감사용이라 움직인다.
    assert _chain(chain.id).step_started_at == first_clock
    assert _chain(chain.id).updated_at > first_clock

    # 그래서 제한 시간이 지나면 확실히 접힌다.
    assert _advance(now=_now() + timedelta(minutes=11)) == 1
    assert _chain(chain.id).step == "failed"
    assert "제한 시간" in _chain(chain.id).error


def test_chain_lifetime_cap_fails_a_slow_walker(app_env):
    """단계마다 조금씩 전진하며 몇 시간을 사는 체인도 결국 접힌다."""
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", AUTO)
    _account(tenant_id, "slowwalk@x.example.com")
    _observe(tenant_id, server_id, "slowwalk@x.example.com", five=1, seven=1)

    chain = _start(_build(tenant_id)[0].id)
    # 단계는 방금 시작했지만 체인 자체는 두 시간째다.
    with _db() as db:
        row = db.get(PoolChain, chain.id)
        row.started_at = _now() - timedelta(hours=2)
        row.step_started_at = _now()
        db.commit()

    assert _advance() == 1
    failed = _chain(chain.id)
    assert failed.step == "failed"
    assert "수명 상한" in failed.error


def test_switch_is_never_issued_twice(app_env):
    """command_id 를 못 적은 창에서도 전환은 한 번만 나간다.

    ``request_switch_now`` 가 스스로 커밋하므로 "명령은 나갔는데 체인이 그
    command_id 를 적기 전"이라는 창이 원리적으로 있다. 다음 틱은 새로 내는 대신
    이미 나간 것을 채택해야 한다 — 전환을 두 번 실행하면 계정이 왕복한다.
    """
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", AUTO)
    account_id = _account(tenant_id, "once@x.example.com")
    _observe(tenant_id, server_id, "once@x.example.com", five=1, seven=1)

    chain = _start(_build(tenant_id)[0].id)
    _set_assignment_state(_assignment_of(tenant_id, account_id).id, "active")
    _advance()  # deliver -> switch
    _advance()  # switch_now 발행
    assert len(_commands(tenant_id, "switch_now")) == 1

    # 커밋 직후 프로세스가 죽어 command_id 를 못 적은 상태를 만든다.
    with _db() as db:
        db.get(PoolChain, chain.id).command_id = None
        db.commit()

    assert _advance() == 1
    assert len(_commands(tenant_id, "switch_now")) == 1  # 두 번째는 없다
    assert _chain(chain.id).command_id == _commands(tenant_id, "switch_now")[0].command_id


def test_database_refuses_a_second_active_chain_on_one_server(app_env):
    """서버당 1체인은 코드 판단이 아니라 스키마가 보장한다."""
    from sqlalchemy.exc import IntegrityError

    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", AUTO)
    _account(tenant_id, "dup@x.example.com")
    _observe(tenant_id, server_id, "dup@x.example.com", five=1, seven=1)
    _start(_build(tenant_id)[0].id)

    with _db() as db:
        db.add(
            PoolChain(
                tenant_id=tenant_id,
                server_id=server_id,
                kind="lease",
                step="deliver",
                actor="op@x.example.com",
            )
        )
        try:
            db.commit()
        except IntegrityError:
            pass
        else:  # pragma: no cover
            raise AssertionError("두 번째 활성 체인이 삽입됐다")

    # 끝난 체인은 몇 개가 쌓여도 막지 않는다.
    with _db() as db:
        db.add(
            PoolChain(
                tenant_id=tenant_id,
                server_id=server_id,
                kind="lease",
                step="done",
                actor="op@x.example.com",
            )
        )
        db.add(
            PoolChain(
                tenant_id=tenant_id,
                server_id=server_id,
                kind="lease",
                step="failed",
                actor="op@x.example.com",
            )
        )
        db.commit()
