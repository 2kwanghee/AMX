#!/usr/bin/env python3
"""session_usage_hook.py — Claude Code Stop 훅. 세션의 **비용 구조**만 AMS로 보낸다.

Claude Code는 세션 트랜스크립트(``~/.claude*/projects/<프로젝트>/<세션ID>.jsonl``)의
``assistant`` 레코드마다 ``message.usage`` 전체를 적어 둔다. 그 안의
``cache_creation.ephemeral_1h_input_tokens`` / ``ephemeral_5m_input_tokens``는 가격이
서로 다른 두 캐시 쓰기인데, Langfuse Metrics API는 ``usageByType``에서 둘을 합쳐 보고해
구분이 사라진다. 그래서 이 훅이 로컬에서 직접 집계해 AMS로 POST 한다.

설계 불변식은 ``danger_hook.py``와 같다(우선순위 순):
  1. **Claude 동작을 절대 바꾸지 않는다.** 무슨 일이 있어도 exit 0으로 끝난다 —
     차단하지 않고, 예외를 밖으로 던지지 않으며, stdout에 아무 것도 쓰지 않는다.
  2. **Claude를 절대 느리게/실패하게 하지 않는다.** 통보 HTTP는 하드 2초 데드라인,
     tsamx 조회는 2초 타임아웃이고 모든 실패는 조용히 삼킨다(상태 파일에 마지막 1줄만).
  3. **원문을 전송하지 않는다.** 프롬프트·응답 본문·툴 입출력은 어떤 필드에도 담기지
     않는다. 페이로드는 숫자 집계와 식별자(세션 id·모델명·계정 이메일·호스트·cwd)뿐이고,
     트랜스크립트의 ``message.content``는 아예 읽지 않는다.

표준 라이브러리만 사용한다(외부 의존성 없음 — ``python3 session_usage_hook.py`` 로 바로
실행 가능). 트랜스크립트는 수 MB가 되므로 한 줄씩 스트리밍으로 읽는다.

수집 항목(비용 구조 축만):
  input/output/cache_read 토큰, 1h·5m 캐시 쓰기 토큰, thinking 토큰,
  server_tool_use 웹 검색·페치 횟수, service_tier별·stop_reason별 메시지 수,
  메시지 수, 세션 시작·종료 시각. 모두 **모델별**로 나눈다(한 세션이 주 모델과
  서브에이전트 모델을 섞는다). 툴별 호출 횟수·파일 변경·PR 링크 같은 작업 행태는
  수집하지 않는다.

설정(환경변수):
  AMX_SESSION_INGEST_URL     통보 대상 URL. **미설정이면 즉시 no-op(exit 0).**
  AMX_SESSION_INGEST_TOKEN   정적 토큰(X-AMX-Ingest-Token 헤더). 미설정이면 no-op.
  CC_SESSION_USAGE_STATE_FILE 마지막 실패 1줄을 기록할 경로
                             (기본: 훅 옆 .session_usage_hook.state).
  LANGFUSE_USER_ID           설정돼 있으면 계정 이메일로 그대로 쓴다(tsamx 조회 생략).

집계 단위(실측 근거): ``message.usage.iterations``는 대개 1개이고, 키가 없는 레코드와
**2개인 레코드가 함께 존재한다**. 다중 iteration에서 최상위 값은 자기 안에서 모순된다 —
최상위 ``message.model``과 flat 카운터는 **마지막** iteration을 반영하는데 최상위
``cache_creation`` 분리값만 iteration[0]의 것이 새어 있다. 관측된 다중 iteration 레코드는
**전부** 최상위에 없는 모델을 iteration에 담고 있고, 그 절반 가량에서 최상위 1h값이
마지막 iteration의 1h값과 다르다. 최상위만 읽으면 서브에이전트가 쓴 캐시 생성이 주 모델
이름표를 달고 저장된다 — 하필 이 훅이 존재하는 이유인 그 필드다. 그래서 ``iterations``가
비어 있지 않으면 **각 iteration을 순회**해 토큰 카운터를 ``iteration.model``(없으면 최상위
모델)에 귀속시키고, 부재 시에만 최상위를 쓴다.

  절대 건수는 코퍼스가 자라면 반드시 거짓이 되므로 적지 않는다. 재측정은 세션 기록
  루트에서 ``type == "assistant"`` 레코드의 ``usage.iterations`` 길이 분포를 세고, 길이가
  2 이상인 레코드에서 최상위 ``cache_creation.ephemeral_1h_input_tokens``를 마지막
  iteration의 같은 값과, 최상위 ``message.model``을 각 ``iteration.model``과 비교하면 된다.

중복 제거는 **메시지 단위**다: 트랜스크립트는 한 API 응답을 content 블록마다 한 줄씩
반복해 적으므로 ``message.id``로 접는다(제거하지 않으면 집계가 거의 2배 — 935줄 /
477 메시지가 실측치다). 접히지 않은 메시지에 대해서만 iteration을 순회한다.

지연 실행(자기 자신을 분리 프로세스로 재실행): Claude Code는 Stop 훅을 부른 **뒤에**
트랜스크립트에 assistant 레코드를 쓴다(실측: Stop 시점 8줄·assistant 0건 → 세션 종료
후 12줄·assistant 2건, ``-p`` 배치 모드). 그래서 Stop 훅이 그 자리에서 읽으면 매번
빈 집계라 ``build_models``가 빈 리스트를 돌려주고 조용히 ``return 0``으로 끝난다 —
서버 문제가 아니라 타이밍 문제다. ``AMX_SESSION_USAGE_DEFERRED``가 없는 1차 호출은
url·token·payload만 파싱한 뒤 자기 자신을 ``AMX_SESSION_USAGE_DEFERRED=1``로 분리
프로세스 실행하고 즉시 0을 반환한다(Stop 훅을 붙잡지 않는다). 분리된 자식은 트랜스
크립트에 assistant 레코드가 나타날 때까지 최대 ``_DEFER_MAX_SECONDS`` 초를 폴링한
뒤 기존 집계·전송 로직을 그대로 수행한다.
"""

from __future__ import annotations

import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone

# 통보 HTTP 타임아웃(초). Claude를 붙잡지 않도록 짧게 고정한다.
_HTTP_TIMEOUT_SECONDS = 2.0
# 분리된 자식이 assistant 레코드가 나타날 때까지 기다리는 최대 시간·폴링 간격(초).
# Claude Code가 Stop 훅 발화 뒤에 트랜스크립트를 쓰는 실측 지연을 흡수하기 위함이다
# (위 모듈 docstring "지연 실행" 참조). 부모(1차 호출) 쪽은 이 대기를 절대 하지 않는다
# — Stop 훅 자체가 이 시간만큼 느려지면 안 되므로 자식으로 넘긴다.
_DEFER_MAX_SECONDS = 15.0
_DEFER_POLL_INTERVAL_SECONDS = 0.5
# tsamx 조회 타임아웃(초). 실패하면 계정 없이 보낸다(엔드포인트가 NULL을 받는다).
_TSAMX_TIMEOUT_SECONDS = 2.0
# 트랜스크립트 스캔 줄 수 상한. 병리적으로 긴 파일이 훅 실행 시간을 좌우하지 못하게
# 한다(초과분은 버리고 상태 파일에 1줄 남긴다).
_MAX_LINES = 500_000
# 한 줄 바이트 상한과 파일 전체 바이트 예산. 줄 수 상한만으로는 메모리를 막지 못한다 —
# ``transcript_path``가 /dev/zero 로 향하는 심볼릭 링크이면 개행 없는 한 줄이 RSS를
# 수백 MB까지 밀어올린다(실측 0.6초에 593MB). 그래서 줄 단위 iteration이 아니라 고정
# 크기 청크로 읽어 두 상한을 함께 건다. 어느 쪽이든 넘기면 상태 파일에 1줄 남기고
# 조용히 끝낸다(exit 0 유지).
_MAX_LINE_BYTES = 1 << 20  # 1MB. 실측 최대 줄은 수십 KB 수준이다.
_MAX_TOTAL_BYTES = 64 << 20  # 64MB. 실측 최대 트랜스크립트는 7.4MB다.
_READ_CHUNK_BYTES = 1 << 16
# 페이로드에 담을 모델 수 상한(서버 스키마 상한과 동일). **집계 중** 삽입 시점에 걸어야
# 한다 — build_models 에서 사후에 자르면 by_model 이 이미 커진 뒤라 메모리 증폭을 막지
# 못한다(실측 8MB 입력 -> RSS +127MB). 카운트 키와 같은 이유로 한 자리를 _OTHER 몫으로
# 비워 둔다.
_MAX_MODELS = 50
_MAX_DISTINCT_MODELS = _MAX_MODELS - 1
# 카운트 맵의 키 개수 상한 — 서버 스키마(_COUNT_MAP_MAX_KEYS)와 짝을 맞춘다. 넘치는
# 키는 버리지 않고 _OTHER 로 접어, 짝이 어긋나 422가 되는 일이 없게 한다. 실제로 담는
# 고유 키는 한 자리 적게 잡는다 — 마지막 자리를 _OTHER 몫으로 비워 두지 않으면, 상한을
# 채운 뒤 접어 넣은 _OTHER 가 21번째 키가 되어 서버 상한을 넘긴다.
_MAX_COUNT_KEYS = 20
_MAX_DISTINCT_COUNT_KEYS = _MAX_COUNT_KEYS - 1
# 토큰 카운터 상한 — 서버 스키마(_Count 의 le=2**53)와 짝을 맞춘다.
_MAX_TOKEN_VALUE = 2**53

# 라벨(모델명·카운트 맵 키) 검증. **절단이 아니라 검증**이다: 트랜스크립트에 한 줄 쓸 수
# 있는 주체는 누구나 이 훅의 페이로드에 값을 실을 수 있고, 절단은 상한일 뿐 내용을 막지
# 못한다. 모델 50종 x 200자 + 카운트 키 20종 x 64자면 임의 텍스트 131KB가 서버 상한
# 아래로 통과해 저장되고 콘솔에 그대로 표시됐다. 이 두 값은 사실상 열거값이므로 보수적
# 문자셋으로 검증하고, 어긋나면 버리는 대신 하나의 버킷(_OTHER)으로 접는다 — 정상 값은
# 그대로 통과하고 임의 텍스트는 통로가 되지 못한다.
#
# 문자셋 근거(기본 프로필 440개 파일 실측): 모델명은 claude-opus-5 /
# claude-haiku-4-5-20251001 / <synthetic>, service_tier 는 standard, stop_reason 은
# tool_use / end_turn / max_tokens / stop_sequence 다. 실측 값 전체가 이 패턴을
# 통과하며 최대 길이는 25자(claude-haiku-4-5-20251001)다.
_LABEL_RE = re.compile(r"^[A-Za-z0-9._<>-]+$")
_OTHER = "<other>"
_MAX_MODEL_CHARS = 48
_MAX_COUNT_KEY_CHARS = 32
# 페이로드 전체의 라벨 문자 총합 상한. **문자셋 검증만으로는 부족하다**: 위 문자셋은
# ``-``와 ``_``를 허용하는데 그것이 정확히 **base64url 알파벳**이라, 임의 바이너리를
# base64url로 인코딩하면 개별 값 검증을 그대로 통과한다. 개별 값의 형식을 보는 것과
# 채널 전체의 폭을 묶는 것은 다른 문제다 — 모델 49종 x 48자에 모델마다 티어·stop 키
# 각 19종 x 32자를 붙이면 6만 자가 넘는 임의 바이트가 서버 상한 아래로 통과했다.
# 그래서 문자셋 검증(내용) 위에 총량 상한(폭)을 한 겹 더 둔다. **둘 중 하나만 남기면
# 통로가 다시 열린다 — 중복이 아니다.**
#
# 256자 근거: 실측 모델명 최장 25자, 한 세션의 모델은 보통 1~4종이고 카운트 키는
# standard/tool_use/end_turn/max_tokens/stop_sequence 수준(최장 13자)이다. 정상 페이로드는
# 100자 내외라 여유가 크다. 총합을 넘긴 뒤로는 새 라벨을 만들지 않고 이미 있는 _OTHER
# 버킷에 접으므로 키 수도 늘지 않는다.
_MAX_LABEL_CHARS = 256
# 레코드당 iterations 항목 수 상한. 정상 값은 1~2개다. 넘는 항목은 버리고 잘림으로 표시한다
# — 상한이 없으면 한 줄이 by_model 을 10만 키로 불려 RSS를 수백 MB 밀어올린다(실측).
_MAX_ITERATIONS = 16

# 집계 카운터 이름 → 페이로드 키(서버 스키마의 camelCase 별칭과 **정확히** 일치해야 한다).
_TOKEN_KEYS = (
    ("input_tokens", "inputTokens"),
    ("output_tokens", "outputTokens"),
    ("cache_read_tokens", "cacheReadTokens"),
    ("cache_create_1h_tokens", "cacheCreate1HTokens"),
    ("cache_create_5m_tokens", "cacheCreate5MTokens"),
    ("thinking_tokens", "thinkingTokens"),
    ("web_search_requests", "webSearchRequests"),
    ("web_fetch_requests", "webFetchRequests"),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_file() -> str:
    override = os.environ.get("CC_SESSION_USAGE_STATE_FILE", "").strip()
    if override:
        return override
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".session_usage_hook.state"
    )


def _record_failure(reason: str) -> None:
    """마지막 실패 1줄만 상태 파일에 남긴다. 이 기록 자체의 실패도 삼킨다."""
    try:
        with open(_state_file(), "w", encoding="utf-8") as fh:
            fh.write(f"{_now_iso()} {reason}\n")
    except Exception:
        pass


def _int(value: object) -> int:
    """usage 필드를 음수 없는 정수로. 모든 이상값을 **예외 없이** 0으로 흡수한다.

    bool·문자열·None·음수·NaN 뿐 아니라 ``Infinity``와 범위를 넘는 값도 0이다. JSON은
    기본으로 ``Infinity``를 허용하므로(json.loads 가 float('inf')로 읽는다) 그 값 하나가
    ``int()`` 에서 OverflowError 를 던지면 main 이 예외를 삼켜 **그 세션 전체가 조용히
    사라진다**. 범위 초과(서버 스키마의 le=2**53)도 클램프가 아니라 0이다 — 조작된 거대
    값을 진짜 숫자처럼 저장하는 것보다 "읽을 수 없는 값"으로 두는 편이 정직하고, 페이로드
    가 상한 위반으로 422가 되는 일도 없다.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not math.isfinite(value):
            return 0
        number = int(value)
    else:
        return 0
    if number <= 0 or number > _MAX_TOKEN_VALUE:
        return 0
    return number


def _label(value: object, limit: int) -> str | None:
    """열거값 라벨을 검증한다. 정상이면 그대로, 어긋나면 ``_OTHER``, 값이 없으면 None.

    절단하지 않는다(위 _LABEL_RE 주석 참조). 상한을 넘거나 문자셋을 벗어나면 원문 대신
    단일 버킷을 돌려주므로, 이 경로로는 임의 길이·임의 내용의 텍스트가 나갈 수 없다.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > limit or _LABEL_RE.match(text) is None:
        return _OTHER
    return text


def _new_state() -> dict:
    """집계 1회분의 가변 상태. 라벨 예산 잔량과 잘림 여부를 담는다.

    ``truncated``는 **조용한 과소집계를 드러내기 위한** 플래그다. 줄 수·바이트·iterations
    상한에 걸려 일부를 버리면 그때까지의 집계를 그대로 보내는데(부분 데이터도 값이 있다),
    표시가 없으면 조작된 패딩으로 과소집계를 조용히 유도할 수 있다. 라벨을 _OTHER 로 접는
    것은 합계를 보존하므로 잘림이 아니다.
    """
    return {"label_chars": 0, "truncated": False}


def _spend_labels(state: dict, label: str) -> bool:
    """새 라벨에 예산을 쓴다. 남아 있으면 True(차감), 없으면 False(호출부가 _OTHER 로 접는다)."""
    if state["label_chars"] + len(label) > _MAX_LABEL_CHARS:
        return False
    state["label_chars"] += len(label)
    return True


def _new_model_bucket() -> dict:
    bucket = {name: 0 for name, _ in _TOKEN_KEYS}
    bucket["message_count"] = 0
    bucket["service_tier_counts"] = {}
    bucket["stop_reason_counts"] = {}
    bucket["started_at"] = None
    bucket["ended_at"] = None
    return bucket


def _bump(counts: dict, key: object, state: dict) -> None:
    """{키: 횟수} 맵을 1 올린다. 값이 없으면 무시하고, 그 밖의 이상값은 _OTHER 로 접는다.

    키 개수가 서버 상한에 닿거나 페이로드 라벨 예산이 바닥나면 새 키도 _OTHER 로 접는다 —
    훅과 서버의 상한이 어긋나 정상 보고가 422로 거절되는 일도, 라벨이 반출 통로가 되는
    일도 없게 한다. 이미 있는 키는 예산을 다시 쓰지 않는다.
    """
    label = _label(key, _MAX_COUNT_KEY_CHARS)
    if label is None:
        return
    if label not in counts and (
        len(counts) >= _MAX_DISTINCT_COUNT_KEYS or not _spend_labels(state, label)
    ):
        label = _OTHER
    counts[label] = counts.get(label, 0) + 1


def _span(bucket: dict, stamp: object) -> None:
    """레코드 타임스탬프로 세션 구간을 넓힌다. 파싱 불가한 값은 무시한다.

    문자열 비교가 아니라 **파싱한 datetime**으로 비교한다: 소수점 초의 유무나 오프셋
    표기가 섞이면 사전순 비교가 실제 시각 순서와 어긋난다. tz 정보가 없는 값은 UTC로
    해석한다(Claude Code는 UTC 'Z'로 적는다).
    """
    if not isinstance(stamp, str) or not stamp:
        return
    text = stamp[:-1] + "+00:00" if stamp.endswith(("Z", "z")) else stamp
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if bucket["started_at"] is None or moment < bucket["started_at"]:
        bucket["started_at"] = moment
    if bucket["ended_at"] is None or moment > bucket["ended_at"]:
        bucket["ended_at"] = moment


def _bucket_for(by_model: dict, model: object, state: dict) -> dict:
    """모델 이름을 검증해 해당 버킷을 얻는다. 이름이 없으면 "unknown".

    새 키는 **삽입 시점에** 두 상한을 통과해야 한다: 모델 수 상한과 페이로드 라벨 예산.
    어느 쪽이든 넘으면 _OTHER 버킷으로 접는다 — 사후에 자르는 것으로는 집계 중 메모리
    증폭도, 반출 통로도 막지 못한다. 접기는 합계를 보존하므로 잘림으로 표시하지 않는다.
    """
    name = _label(model, _MAX_MODEL_CHARS) or "unknown"
    if name not in by_model and (
        len(by_model) >= _MAX_DISTINCT_MODELS or not _spend_labels(state, name)
    ):
        name = _OTHER
    bucket = by_model.get(name)
    if bucket is None:
        bucket = by_model[name] = _new_model_bucket()
    return bucket


def _token_slices(message: dict, usage: dict, state: dict) -> list[tuple[object, dict]]:
    """토큰 카운터를 읽을 (모델, usage) 조각 목록.

    ``iterations``가 비어 있지 않은 리스트면 각 iteration이 한 조각이고 모델은
    ``iteration.model``이다 — 단일 iteration에는 그 키가 없으므로(실측 27,094건 중
    ``model`` 보유는 다중 iteration의 94건뿐) 최상위 ``message.model``로 떨어뜨린다.
    ``iterations``가 없으면(실측 157건) 최상위 usage 하나가 조각이다.

    iteration 조각에는 토큰 5종만 들어 있다(실측 키: input_tokens, output_tokens,
    cache_read_input_tokens, cache_creation_input_tokens, cache_creation). thinking·
    server_tool_use·service_tier·stop_reason 은 iteration에 없어 최상위에서 읽고
    최상위 모델에 귀속시킨다 — 호출부가 그렇게 나눈다.
    """
    top_model = message.get("model")
    iterations = usage.get("iterations")
    if isinstance(iterations, list) and iterations:
        if len(iterations) > _MAX_ITERATIONS:
            # 넘는 항목은 버린다 → 과소집계이므로 잘림으로 표시한다.
            state["truncated"] = True
            iterations = iterations[:_MAX_ITERATIONS]
        slices = [
            (it.get("model") if it.get("model") else top_model, it)
            for it in iterations
            if isinstance(it, dict)
        ]
        if slices:
            return slices
    return [(top_model, usage)]


def aggregate(lines, state: dict | None = None) -> dict[str, dict]:
    """트랜스크립트 줄 이터러블을 모델별 비용구조 집계로 접는다.

    ``type == "assistant"`` 레코드의 ``message.usage``만 본다. 다른 레코드는 건너뛴다.
    같은 ``message.id``가 여러 줄에 반복되면(한 응답이 content 블록마다 한 줄씩 적힌다)
    첫 줄만 센다 — 그러지 않으면 토큰이 이중 계산된다. ``message.id``가 **없는** 레코드는
    접을 키가 없으므로 아예 건너뛴다: 같은 응답이 5줄로 적혀 있으면 5배로 계산되고, 비용
    데이터에서 과대집계는 과소집계보다 해롭다(실측 빈도 0건).

    토큰 카운터는 ``_token_slices``가 나눈 iteration 단위로 각 모델에 귀속시킨다.
    ``message.content``는 읽지 않는다. 깨진 줄·잘린 마지막 줄은 조용히 건너뛴다.

    ``state``는 라벨 예산·잘림 플래그를 담는 가변 상태다(``_new_state``). 생략하면 자체
    생성하므로 단독 호출도 되지만, 잘림 여부를 읽어야 하는 호출부는 직접 넘긴다.
    """
    if state is None:
        state = _new_state()
    by_model: dict[str, dict] = {}
    seen_ids: set[str] = set()
    for n, raw in enumerate(lines):
        if n >= _MAX_LINES:
            _record_failure(f"transcript scan truncated at {_MAX_LINES} lines")
            state["truncated"] = True
            break
        if not raw or not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except Exception:  # noqa: BLE001 - 깨진/잘린 줄 하나가 전체를 막지 않는다.
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        message_id = message.get("id")
        if not isinstance(message_id, str) or not message_id:
            continue  # 접을 키가 없는 레코드는 버린다(위 docstring).
        if message_id in seen_ids:
            continue
        seen_ids.add(message_id)

        # 토큰 카운터: iteration 단위로 각자의 모델에 귀속.
        for model, slice_usage in _token_slices(message, usage, state):
            bucket = _bucket_for(by_model, model, state)
            bucket["input_tokens"] += _int(slice_usage.get("input_tokens"))
            bucket["output_tokens"] += _int(slice_usage.get("output_tokens"))
            bucket["cache_read_tokens"] += _int(slice_usage.get("cache_read_input_tokens"))
            creation = slice_usage.get("cache_creation")
            if isinstance(creation, dict):
                bucket["cache_create_1h_tokens"] += _int(
                    creation.get("ephemeral_1h_input_tokens")
                )
                bucket["cache_create_5m_tokens"] += _int(
                    creation.get("ephemeral_5m_input_tokens")
                )
            # 시각은 모든 참여 모델에 넓힌다. iteration에만 등장하는 모델(서브에이전트)의
            # started_at/ended_at 이 NULL로 남으면, 조회 창이 ended_at 기준이라 그 행이
            # 콘솔에서 사라진다 — 이 훅이 바로잡으려는 그 모델이 안 보이게 된다.
            _span(bucket, record.get("timestamp"))

        # iteration에 없는 항목은 최상위에서 읽어 최상위 모델에 귀속시킨다. 메시지 수는
        # 메시지 단위이므로 여기서 한 번만 센다(시각은 위에서 참여 모델 전체에 넓혔다).
        top = _bucket_for(by_model, message.get("model"), state)
        details = usage.get("output_tokens_details")
        if isinstance(details, dict):
            top["thinking_tokens"] += _int(details.get("thinking_tokens"))
        server_tools = usage.get("server_tool_use")
        if isinstance(server_tools, dict):
            top["web_search_requests"] += _int(server_tools.get("web_search_requests"))
            top["web_fetch_requests"] += _int(server_tools.get("web_fetch_requests"))
        _bump(top["service_tier_counts"], usage.get("service_tier"), state)
        _bump(top["stop_reason_counts"], message.get("stop_reason"), state)
        top["message_count"] += 1
        _span(top, record.get("timestamp"))
    return by_model


def build_models(by_model: dict[str, dict]) -> list[dict]:
    """집계를 페이로드의 ``models`` 리스트로 변환한다(토큰 많은 모델 우선, 상한 적용)."""
    ordered = sorted(
        by_model.items(),
        key=lambda kv: -(kv[1]["input_tokens"] + kv[1]["output_tokens"]
                         + kv[1]["cache_read_tokens"] + kv[1]["cache_create_1h_tokens"]
                         + kv[1]["cache_create_5m_tokens"]),
    )
    out = []
    for model, bucket in ordered[:_MAX_MODELS]:
        item = {"model": model}
        for name, wire in _TOKEN_KEYS:
            item[wire] = bucket[name]
        item["messageCount"] = bucket["message_count"]
        item["serviceTierCounts"] = bucket["service_tier_counts"]
        item["stopReasonCounts"] = bucket["stop_reason_counts"]
        started, ended = bucket["started_at"], bucket["ended_at"]
        item["startedAt"] = started.isoformat() if started is not None else None
        item["endedAt"] = ended.isoformat() if ended is not None else None
        out.append(item)
    return out


def active_account_email() -> str | None:
    """현재 활성 계정 이메일. ``tsamx status --json``의 ``active.email``.

    설치 시점의 값을 박아두지 않고 매번 물어보는 이유: 계정은 전환되고, 박아둔 값은
    전환 즉시 거짓이 된다. ``CLAUDE_CONFIG_DIR``이 이미 설정 홈을 가리키므로 tsamx는
    같은 홈을 본다(``deploy/amx-claude``와 같은 조회다).

    실패(tsamx 없음·타임아웃·JSON 깨짐·필드 없음)는 모두 ``None``이다 — 계정 없이
    보내면 서버가 ``account_id`` NULL로 받아들인다.
    """
    pinned = os.environ.get("LANGFUSE_USER_ID", "").strip()
    if pinned:
        return pinned[:320]
    try:
        proc = subprocess.run(
            ["tsamx", "status", "--json"],
            capture_output=True,
            timeout=_TSAMX_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - tsamx 부재·타임아웃 모두 계정 없음으로 취급.
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    active = data.get("active")
    if isinstance(active, dict):
        email = active.get("email")
        if isinstance(email, str) and email.strip():
            return email.strip()[:320]
    # 폴백: 일부 tsamx 버전은 status에 active 블록 없이 accounts 목록만 준다.
    accounts = data.get("accounts")
    if isinstance(accounts, list):
        for account in accounts:
            if not isinstance(account, dict) or not account.get("active"):
                continue
            email = account.get("email")
            if isinstance(email, str) and email.strip():
                return email.strip()[:320]
    return None


def _notify(url: str, token: str, payload: dict) -> None:
    """통보 POST를 **하드 2초 데드라인**으로 경계한다(danger_hook과 같은 방식).

    urllib의 socket timeout만으로는 DNS 해석 단계 지연을 못 막는 플랫폼이 있어, POST
    전체를 데몬 스레드에서 돌리고 메인은 join(2초)으로만 기다린다. 데드라인을 넘기면
    스레드를 그대로 두고 반환한다 — 데몬 스레드라 프로세스 종료 시 함께 사라진다.
    """
    body = json.dumps(payload).encode("utf-8")

    def _do() -> None:
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-AMX-Ingest-Token": token,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
                if resp.status >= 300:
                    _record_failure(f"ingest HTTP {resp.status}")
        except Exception as exc:  # noqa: BLE001 - 절대 밖으로 던지지 않는다.
            _record_failure(f"ingest error: {type(exc).__name__}")

    worker = threading.Thread(target=_do, daemon=True)
    worker.start()
    worker.join(_HTTP_TIMEOUT_SECONDS)
    if worker.is_alive():
        _record_failure("ingest timeout (hard 2s deadline)")


def _read_lines(path: str, state: dict | None = None):
    """트랜스크립트를 바이트 상한 아래에서 한 줄씩 흘려준다(제너레이터).

    파일 객체를 그대로 순회하지 않는 이유: 그 경우 한 줄의 길이에 상한이 없어, 개행 없는
    거대 스트림(예: /dev/zero 를 가리키는 심볼릭 링크)이 메모리를 그대로 밀어올린다.
    고정 크기 청크로 읽어 ``_MAX_LINE_BYTES``(한 줄)와 ``_MAX_TOTAL_BYTES``(전체)를 함께
    건다. 전체 예산을 넘으면 상태 파일에 1줄 남기고 **정상 종료**한다 — 그때까지 모은
    줄은 이미 호출부가 집계했다. 한 줄 상한을 넘으면 **그 줄만 다음 개행까지 버리고
    계속 읽는다**(실측 최대 줄이 상한의 93%라, 평범한 세션 한 줄이 넘었다고 나머지
    전체를 버리면 조용한 과소집계가 된다). 어느 경우든 ``state["truncated"]``를 세워
    부분 집계임을 페이로드에 싣는다.
    """
    total = 0
    buffer = b""
    skipping = False  # 상한을 넘긴 줄의 나머지를 다음 개행까지 버리는 중.
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_TOTAL_BYTES:
                _record_failure(f"transcript exceeds {_MAX_TOTAL_BYTES} byte budget")
                if state is not None:
                    state["truncated"] = True
                return
            buffer += chunk
            while True:
                index = buffer.find(b"\n")
                if index < 0:
                    break
                line, buffer = buffer[:index], buffer[index + 1 :]
                if skipping:
                    skipping = False
                    continue
                yield line.decode("utf-8", "replace")
            if not skipping and len(buffer) > _MAX_LINE_BYTES:
                _record_failure(f"transcript line exceeds {_MAX_LINE_BYTES} bytes")
                if state is not None:
                    state["truncated"] = True
                skipping = True
            if skipping:
                buffer = b""
    if buffer and not skipping:
        yield buffer.decode("utf-8", "replace")


def _aggregate_with_deferred_wait(transcript_path: str) -> tuple[dict[str, dict], dict]:
    """트랜스크립트에 assistant 레코드가 생길 때까지 폴링한 뒤 집계한다(자식 프로세스 전용).

    파일이 존재하고 집계 결과가 비어 있지 않으면 그 즉시 반환한다(대기 없음). 그렇지
    않으면 ``_DEFER_POLL_INTERVAL_SECONDS`` 간격으로 최대 ``_DEFER_MAX_SECONDS``까지
    재시도한다. 매 시도마다 상태를 새로 만든다 — 실패한 시도의 라벨 예산이 다음 시도로
    새면 정상적으로 채워질 예산이 폴링 횟수만큼 조기에 바닥난다. 끝까지 비면 마지막
    시도의 (빈) 집계와 상태를 그대로 돌려준다 — 실패 기록은 남기지 않는다(assistant가
    끝내 없는 세션은 정상 경로다).
    """
    deadline = time.monotonic() + _DEFER_MAX_SECONDS
    by_model: dict[str, dict] = {}
    state = _new_state()
    while True:
        if os.path.exists(transcript_path):
            state = _new_state()
            by_model = aggregate(_read_lines(transcript_path, state), state)
            if by_model:
                return by_model, state
        if time.monotonic() >= deadline:
            return by_model, state
        time.sleep(_DEFER_POLL_INTERVAL_SECONDS)


def _session_id(payload: dict, transcript_path: str) -> str | None:
    """세션 id: payload의 값이 우선, 없으면 트랜스크립트 파일명 stem."""
    for key in ("session_id", "sessionId"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    stem = os.path.splitext(os.path.basename(transcript_path))[0]
    return stem[:200] or None


def main() -> int:
    url = os.environ.get("AMX_SESSION_INGEST_URL", "").strip()
    token = os.environ.get("AMX_SESSION_INGEST_TOKEN", "").strip()
    # 미설정이면 아무 것도 하지 않는다(가장 흔한 경로 — 즉시 종료).
    if not url or not token:
        return 0

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return 0
    except Exception:  # noqa: BLE001 - 깨진 stdin에도 exit 0.
        return 0

    if not os.environ.get("AMX_SESSION_USAGE_DEFERRED"):
        # 1차 호출(Stop 훅 본체): 트랜스크립트를 기다리지 않는다 — 자기 자신을 분리
        # 프로세스로 재실행해 그쪽이 기다리게 하고 여기서는 즉시 반환한다(위 모듈
        # docstring "지연 실행" 참조).
        try:
            child = subprocess.Popen(
                [sys.executable, os.path.abspath(__file__)],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env={**os.environ, "AMX_SESSION_USAGE_DEFERRED": "1"},
            )
            if child.stdin is not None:
                child.stdin.write(raw.encode("utf-8"))
                child.stdin.close()
        except Exception as exc:  # noqa: BLE001 - 절대 밖으로 던지지 않는다.
            _record_failure(f"defer spawn failed: {type(exc).__name__}")
        return 0

    transcript_path = payload.get("transcript_path") or payload.get("transcriptPath")
    if not isinstance(transcript_path, str) or not transcript_path:
        return 0

    try:
        session_id = _session_id(payload, transcript_path)
        if not session_id:
            return 0
        by_model, state = _aggregate_with_deferred_wait(transcript_path)
        models = build_models(by_model)
        if not models:
            return 0  # assistant 레코드가 없는 세션은 보낼 것이 없다.
        out = {
            "sessionId": session_id,
            "hostname": socket.gethostname()[:253],
            # 상한에 걸려 일부를 버렸으면 True. 콘솔이 그 행을 부분 집계로 표시한다.
            "truncated": state["truncated"],
            "models": models,
        }
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd:
            out["cwd"] = cwd[:1024]
        email = active_account_email()
        if email:
            out["accountEmail"] = email
        _notify(url, token, out)
    except Exception as exc:  # noqa: BLE001 - 어떤 내부 오류도 Claude에 영향 주지 않는다.
        _record_failure(f"internal error: {type(exc).__name__}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
