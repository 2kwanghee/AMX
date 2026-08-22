"""시트 엔진 P1 — rotation_scope 정책과 소유자 범위 후보 필터.

docs/design-notes/seat-engine-plan.md §5 결정 1. (a)~(e) 는 ``pool._candidates``
순수 함수 단위 테스트(DB 없이 메모리 Account 로 구성) — 정규화·필터 로직만 본다.
(f) 는 기존 데이터(전부 owner 빈 값)에서 build_recommendations 가 P1 이전과
동일하게 동작하는 회귀 테스트다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models import Account
from app.services import pool
from tests.test_pool_hardening import _account, _build, _observe, _server, _std, _tenant


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


def _candidates(accounts, *, rotation_scope="owner", server_owner=None):
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
    assert _candidates([acc], server_owner="ops-team") == [acc]
    assert _candidates([acc], server_owner=None) == [acc]


# -- (b) owner 있는 계정은 같은 owner 서버에만 후보 ---------------------------
def test_owned_account_only_candidate_on_same_owner_server():
    acc = _acc(owner="Ops")
    assert _candidates([acc], server_owner="Ops") == [acc]
    assert _candidates([acc], server_owner="Other") == []


# -- (c) owner 있는 계정 + owner 없는 서버 = 후보 아님 ------------------------
def test_owned_account_not_candidate_on_blank_owner_server():
    acc = _acc(owner="Ops")
    assert _candidates([acc], server_owner=None) == []


# -- (d) rotation_scope="server" 면 현행처럼 전부 후보 ------------------------
def test_server_scope_ignores_owner_mismatch():
    acc = _acc(owner="Ops")
    out = _candidates([acc], rotation_scope="server", server_owner="Other")
    assert out == [acc]


# -- (e) 정규화(대소문자·공백) ------------------------------------------------
def test_owner_comparison_is_case_and_whitespace_insensitive():
    acc = _acc(owner="  Ops  ")
    assert _candidates([acc], server_owner="ops") == [acc]
    assert _candidates([acc], server_owner="OPS") == [acc]
    assert _candidates([acc], server_owner="other") == []


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
