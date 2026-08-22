"""프로바이더별 능력 선언 (시트 엔진 P1 정책 축, docs/design-notes/seat-engine-plan.md §P1/§5).

``app.services.pool`` 여기저기 흩어져 있던 ``provider == "codex"`` 리터럴을 한
표로 모은다. 새 프로바이더가 추가될 때 이 표만 바꾸면 되고, 아직 선언되지
않은 프로바이더는 가장 보수적인 값(자동 전환 불가·서버당 1개)으로 취급한다 —
모르는 프로바이더를 낙관적으로 풀어주면 §1 법적 판정(약관 위반 경계)을 넘을
수 있어서다.

이 표가 실제로 바꾸는 동작은 없다. ``pool.py`` 의 리터럴을 조회로 바꿔도
오늘 선언된 두 프로바이더(claude/codex)에서는 수치적으로 동일한 결과가 나오도록
맞췄다 — 리팩터링이라 기존 테스트가 그대로 통과해야 한다.

설계 리뷰 반영(08-23): ``auto_switch`` 는 처음엔 선언만 있고 소비처가 없었다
— codex 가 hot-swap 대상에서 빠지는 것이 per_server_limit=1 의 부작용일
뿐이었고, ``_candidates(replacing=True)`` 경로(대체 상대를 찾는 중)는 그 필터를
일부러 건너뛰어 codex 도 swap 권고의 대상이 될 수 있었다. 지금은
``pool._auto_eligible`` 이 kind=swap/prefetch 권고의 대상 계정에 대해 이 값을
직접 조회해, auto_switch=False 인 프로바이더는 **자동 착수만** 막는다(권고
자체·배포·회수는 그대로 — 기획서 §5 결정 3).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderCapabilities:
    # 자동 전환(사람 개입 없는 로테이션) 대상인가. False 는 배포·회수·가시성까지만
    # 허용한다는 뜻이다(기획서 §5 결정 3, Codex) — pool._auto_eligible 이 kind=
    # swap/prefetch 권고의 자동 착수를 이 값으로 막는다. 권고 생성 자체나
    # 배포(lease)·회수(recall_idle)는 이 값을 보지 않는다.
    auto_switch: bool
    # 서버 하나가 동시에 보유할 수 있는 이 프로바이더 계정 수. None 은 무제한.
    per_server_limit: int | None


# 기획서 §5 결정: claude 는 자동 전환 대상, 서버당 제한 없음. codex 는 호스트당
# CODEX_HOME 하나뿐이라(§1 리서치) 배포·회수·가시성까지만 — auto_switch=False,
# per_server_limit=1.
CAPABILITIES: dict[str, ProviderCapabilities] = {
    "claude": ProviderCapabilities(auto_switch=True, per_server_limit=None),
    "codex": ProviderCapabilities(auto_switch=False, per_server_limit=1),
}

# 선언되지 않은 프로바이더의 기본값 — 가장 보수적으로 취급한다.
_UNKNOWN = ProviderCapabilities(auto_switch=False, per_server_limit=1)


def capabilities(provider: str) -> ProviderCapabilities:
    return CAPABILITIES.get(provider, _UNKNOWN)


def auto_switch_enabled(provider: str) -> bool:
    return capabilities(provider).auto_switch


def per_server_limit(provider: str) -> int | None:
    return capabilities(provider).per_server_limit
