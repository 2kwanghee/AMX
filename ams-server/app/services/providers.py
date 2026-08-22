"""프로바이더별 능력 선언 (시트 엔진 P1 정책 축, docs/design-notes/seat-engine-plan.md §P1/§5).

``app.services.pool`` 여기저기 흩어져 있던 ``provider == "codex"`` 리터럴을 한
표로 모은다. 새 프로바이더가 추가될 때 이 표만 바꾸면 되고, 아직 선언되지
않은 프로바이더는 가장 보수적인 값(자동 전환 불가·풀 미관리·서버당 1개)으로
취급한다 — 모르는 프로바이더를 낙관적으로 풀어주면 §1 법적 판정(약관 위반
경계)을 넘을 수 있어서다.

이 표가 실제로 바꾸는 동작은 없다. ``pool.py`` 의 리터럴을 조회로 바꿔도
오늘 선언된 두 프로바이더(claude/codex)에서는 수치적으로 동일한 결과가
나오도록 맞췄다 — 리팩터링이라 기존 테스트가 그대로 통과해야 한다.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderCapabilities:
    # 자동 전환(사람 개입 없는 로테이션) 대상인가. False 는 배포·회수·가시성까지만
    # 허용한다는 뜻이다(기획서 §5 결정 3, Codex). ams-server 안에는 이 값을 직접
    # 소비하는 "자동 전환 스케줄러"가 없다 — 그건 ama-agent 의 tsamx AutoSwitch고
    # P1 범위(서버+웹) 밖이다. 여기서는 선언만 두고, codex 가 자동 전환 후보에서
    # 실질적으로 빠지는 것은 아래 per_server_limit 을 통해서다(같은 호스트에 두
    # 벌을 동시에 못 두므로 hot-swap 대상이 될 여지가 구조적으로 없다).
    auto_switch: bool
    # 계정 풀 상태(pool_state) 계산·풀 컨트롤러 대상인가.
    pool_managed: bool
    # 서버 하나가 동시에 보유할 수 있는 이 프로바이더 계정 수. None 은 무제한.
    per_server_limit: int | None


# 기획서 §5 결정: claude 는 자동 전환 대상, 서버당 제한 없음. codex 는 호스트당
# CODEX_HOME 하나뿐이라(§1 리서치) 배포·회수·가시성까지만 — auto_switch=False,
# per_server_limit=1.
CAPABILITIES: dict[str, ProviderCapabilities] = {
    "claude": ProviderCapabilities(auto_switch=True, pool_managed=True, per_server_limit=None),
    "codex": ProviderCapabilities(auto_switch=False, pool_managed=True, per_server_limit=1),
}

# 선언되지 않은 프로바이더의 기본값 — 가장 보수적으로 취급한다.
_UNKNOWN = ProviderCapabilities(auto_switch=False, pool_managed=False, per_server_limit=1)


def capabilities(provider: str) -> ProviderCapabilities:
    return CAPABILITIES.get(provider, _UNKNOWN)


def auto_switch_enabled(provider: str) -> bool:
    return capabilities(provider).auto_switch


def pool_managed(provider: str) -> bool:
    return capabilities(provider).pool_managed


def per_server_limit(provider: str) -> int | None:
    return capabilities(provider).per_server_limit
