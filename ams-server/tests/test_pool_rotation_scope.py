"""시트 엔진 P1 — rotation_scope 정책과 소유자 범위 후보 필터.

docs/design-notes/seat-engine-plan.md §5 결정 1, 08-23 설계 리뷰(F1·F2·F3-b) 반영.

(a)~(e) 는 ``pool._candidates`` 순수 함수 단위 테스트(DB 없이 메모리 Account 로
구성) — owner 동작을 검증할 때는 매번 ``rotation_scope="owner"`` 를 명시한다
(리뷰 지시: 기본값에 의존하지 않는다). (f)·(g) 는 실제 기본값(rotation_scope
미지정 시 "server")이 P1 이전과 동일하게 동작한다는 회귀 테스트 — (g) 는 리뷰
F1 이 실측한 사고 시나리오(owner 라벨이 있는 계정 + owner 없는 서버)를 그대로
재현한다. (h) 는 F3-b(정규화가 내부 공백도 지운다), (i)·(j) 는 F2(auto_switch=
False 프로바이더의 swap/prefetch 는 권고는 남고 자동 착수만 막힌다)다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models import Account, PoolRecommendation
from app.services import pool, providers
from tests.test_pool_hardening import (
    _account,
    _assign,
    _build,
    _db,
    _observe,
    _server,
    _std,
    _tenant,
)


def _acc(*, owner: str | None, provider: str = "claude") -> Account:
    """DB 없이 만든 계정 — ``_candidates`` 는 순수 함수라 영속화가 필요 없다."""
    return Account(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        provider=provider,
        email="x@example.com",
        credential_type="oauth",
        status="available",
        pool_state="ready",
        assignment_excluded=False,
        owner=owner,
    )


def _candidates(accounts, *, rotation_scope, server_owner=None):
    now = datetime.now(UTC)
    return pool._candidates(
        accounts,
        live={},
        windows={},
        unusable=set(),
        server_has_live=False,
        now=now,
        stale_after=timedelta(minutes=30),
        rotation_scope=rotation_scope,
        server_owner=server_owner,
    )


# -- (a) owner 없는 계정은 어느 서버에도 후보 --------------------------------
def test_blank_owner_account_is_candidate_everywhere():
    acc = _acc(owner=None)
    assert _candidates([acc], rotation_scope="owner", server_owner="ops-team") == [acc]
    assert _candidates([acc], rotation_scope="owner", server_owner=None) == [acc]


# -- (b) owner 있는 계정은 같은 owner 서버에만 후보 ---------------------------
def test_owned_account_only_candidate_on_same_owner_server():
    acc = _acc(owner="Ops")
    assert _candidates([acc], rotation_scope="owner", server_owner="Ops") == [acc]
    assert _candidates([acc], rotation_scope="owner", server_owner="Other") == []


# -- (c) owner 있는 계정 + owner 없는 서버 = 후보 아님 ------------------------
def test_owned_account_not_candidate_on_blank_owner_server():
    acc = _acc(owner="Ops")
    assert _candidates([acc], rotation_scope="owner", server_owner=None) == []


# -- (d) rotation_scope="server" 면 현행처럼 전부 후보 ------------------------
def test_server_scope_ignores_owner_mismatch():
    acc = _acc(owner="Ops")
    out = _candidates([acc], rotation_scope="server", server_owner="Other")
    assert out == [acc]


# -- (e) 정규화(대소문자·공백) ------------------------------------------------
def test_owner_comparison_is_case_and_whitespace_insensitive():
    acc = _acc(owner="  Ops  ")
    assert _candidates([acc], rotation_scope="owner", server_owner="ops") == [acc]
    assert _candidates([acc], rotation_scope="owner", server_owner="OPS") == [acc]
    assert _candidates([acc], rotation_scope="owner", server_owner="other") == []


# -- (f) 회귀 — 기존 데이터(전부 owner 빈 값)는 P1 이전과 동일하게 동작한다 ---
def test_blank_owner_regression_matches_pre_p1_behavior(app_env):
    """서버·계정 모두 owner 가 없는(=마이그레이션 직후 실제 데이터) 상태에서
    build_recommendations 가 P1 이전과 같은 권고를 낸다 — 빈 서버에 계정을 배급."""
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", {"mode": "auto"})
    account_id = _account(tenant_id, "plain@x.example.com")
    _observe(tenant_id, server_id, _std("plain@x.example.com", five=10, seven=10))

    recs = _build(tenant_id)
    assert [r.kind for r in recs] == ["lease"]
    assert recs[0].to_account_id == account_id


# -- (g) 08-23 리뷰 F1 — 기본 정책은 "server", 실측 사고 시나리오가 재현되지
#    않는다 ---------------------------------------------------------------
def test_default_policy_rotation_scope_is_server():
    """DEFAULT_POLICY·schemas.PoolPolicy 양쪽 다 rotation_scope 기본값이
    "server"다 — "owner"였다면 이 테스트가 (h)처럼 실패했을 것이다."""
    assert pool.DEFAULT_POLICY["rotation_scope"] == "server"
    from app import schemas

    assert schemas.PoolPolicy().rotation_scope == "server"


def test_owner_labeled_account_still_a_candidate_under_default_policy(app_env):
    """리뷰 F1 이 dev DB에서 실측한 상황을 그대로 재현한다: 계정에는 이미
    owner 라벨("이광희")이 붙어 있고 서버 owner는 비어 있다. rotation_scope
    를 명시하지 않은 기본 정책(mode=auto)에서 이 계정이 여전히 후보이고
    lease 권고가 뜬다 — "owner"가 기본값이었다면 후보를 잃었을 조합이다.
    """
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", {"mode": "auto"})  # rotation_scope 미지정
    account_id = _account(tenant_id, "modarra9@example.com", owner="이광희")
    _observe(tenant_id, server_id, _std("modarra9@example.com", five=5, seven=5))

    recs = _build(tenant_id)
    assert [r.kind for r in recs] == ["lease"]
    assert recs[0].to_account_id == account_id


# -- (h) 08-23 리뷰 F3-b — 정규화가 내부 공백도 지운다 ------------------------
def test_owner_normalization_strips_internal_whitespace_too():
    acc = _acc(owner="이 광희")
    assert _candidates([acc], rotation_scope="owner", server_owner="이광희") == [acc]
    assert pool._normalize_owner_label("이 광희") == pool._normalize_owner_label("이광희")


# -- (i)·(j) 08-23 리뷰 F2 — auto_switch=False 프로바이더는 자동 착수만 막힌다,
#    권고는 남는다. claude 는 종전대로 자동 착수된다 ---------------------------
def test_codex_swap_is_recommended_but_not_auto_started(app_env):
    assert providers.auto_switch_enabled("codex") is False
    tenant_id = _tenant()
    # _server 는 last_seen_at 을 이미 채워 준다(D3 — deliver 가 미접속 서버를
    # 409로 거부하지 않도록).
    server_id = _server(tenant_id, "s1", {"mode": "auto"})
    hot_id = _account(tenant_id, "cxhot@x.example.com", provider="codex")
    _account(tenant_id, "cxnext@x.example.com", provider="codex")
    _assign(tenant_id, hot_id, server_id)
    _observe(tenant_id, server_id, _std("cxhot@x.example.com", five=95, seven=30))
    _observe(tenant_id, server_id, _std("cxnext@x.example.com", five=3, seven=3))

    recs = _build(tenant_id)
    assert [r.kind for r in recs] == ["swap"]

    with _db() as db:
        started = pool.start_auto_chains(db)
    assert started == 0
    with _db() as db:
        remaining = list(
            db.scalars(
                select(PoolRecommendation).where(PoolRecommendation.tenant_id == tenant_id)
            ).all()
        )
    assert len(remaining) == 1  # 권고는 지워지지 않고 남는다


def test_claude_swap_is_still_auto_started(app_env):
    assert providers.auto_switch_enabled("claude") is True
    tenant_id = _tenant()
    server_id = _server(tenant_id, "s1", {"mode": "auto"})
    hot_id = _account(tenant_id, "hot@x.example.com")
    _account(tenant_id, "spare@x.example.com")
    _assign(tenant_id, hot_id, server_id)
    _observe(tenant_id, server_id, _std("hot@x.example.com", five=95, seven=30))
    _observe(tenant_id, server_id, _std("spare@x.example.com", five=3, seven=3))

    recs = _build(tenant_id)
    assert [r.kind for r in recs] == ["swap"]

    with _db() as db:
        started = pool.start_auto_chains(db)
    assert started == 1
