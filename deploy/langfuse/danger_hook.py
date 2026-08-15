#!/usr/bin/env python3
"""danger_hook.py — Claude Code PreToolUse 경량 위험명령 감지 훅 (P5, 경로 d).

Claude Code가 Bash 툴을 실행하기 **직전**에 이 훅을 호출한다(PreToolUse 규약:
훅 payload를 stdin JSON으로 받는다). 훅은 ``tool_input.command``를 보수적인 위험
패턴 목록과 대조하고, 매치하면 AMS 수신 엔드포인트로 통보한다. 그게 전부다.

설계 불변식(우선순위 순):
  1. **Claude 동작을 절대 바꾸지 않는다.** 무슨 일이 있어도 exit 0으로 끝난다 —
     차단(exit 2)하지 않고, 예외를 밖으로 던지지 않으며, stdout에 아무 것도 쓰지
     않는다. 감지·통보 전용이지 게이트가 아니다.
  2. **Claude를 절대 느리게/실패하게 하지 않는다.** 통보 HTTP는 2초 타임아웃이고
     모든 실패는 조용히 삼킨다(로컬 상태 파일에 마지막 실패 1줄만 남긴다).
  3. **원문 명령을 전송하지 않는다.** 페이로드에는 sha256 다이제스트와, 패턴에
     매치된 부분만 남기고 나머지를 마스킹한 축약본(최대 200자)만 담는다.

이 파일은 자체 작성이며 vendored langfuse_hook.py 와 무관하다(표준 라이브러리만
사용, 외부 의존성 없음 — ``python3 danger_hook.py`` 로 바로 실행 가능).

설정(환경변수):
  AMX_DANGER_INGEST_URL    통보 대상 URL. **미설정이면 즉시 no-op(exit 0).**
  AMX_DANGER_INGEST_TOKEN  정적 토큰(X-AMX-Ingest-Token 헤더). 미설정이면 no-op.
  CC_DANGER_PATTERNS_FILE  추가 패턴 파일(줄당 정규식). 소유자 전용(0600 계열)이
                           아니면 신뢰하지 않고 무시한다.
  CC_DANGER_STATE_FILE     마지막 실패 1줄을 기록할 경로(기본: 훅 옆 .danger_hook.state).
  LANGFUSE_USER_ID         있으면 payload.userId 로 함께 보낸다.

한계(명시): 정규식은 셸을 파싱하지 않는다. 인용 문자열·주석 안의 위험 문자열은
오탐할 수 있고, 난독화된 위험 명령은 놓칠 수 있다. 단어 경계·명령 구분자 앵커로
오탐을 줄이되 완벽히 막지는 못한다 — 이 훅은 조기경보이지 방어선이 아니다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sys
import urllib.request
from datetime import datetime, timezone

# 통보 HTTP 타임아웃(초). Claude를 붙잡지 않도록 짧게 고정한다.
_HTTP_TIMEOUT_SECONDS = 2.0
# commandMasked 상한.
_MASK_MAX_CHARS = 200

# 명령 구분자 — 한 명령 토큰 경계를 넘지 않도록 룩어헤드에서 사용한다.
_SEP = r"[^|&;\n]"

# 기본 내장 패턴(보수적). (이름, 컴파일된 정규식) 순서대로 첫 매치를 채택한다.
# 룩어헤드는 같은 명령 조각(구분자 이전) 안에서 플래그/인자 존재를 확인한다.
_BUILTIN_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    # rm 재귀+강제 삭제: -rf / -fr / -r -f / --recursive --force 등.
    (
        "rm_recursive_force",
        re.compile(
            rf"\brm\b(?=(?:{_SEP}*\s)-{_SEP}*r)(?=(?:{_SEP}*\s)-{_SEP}*f)",
            re.IGNORECASE,
        ),
    ),
    # 권한 상승. 뒤 인자를 소비하지 않도록 룩어헤드로만 확인한다.
    ("sudo", re.compile(r"\bsudo\b(?=\s+\S)")),
    # 파일시스템 생성(포맷).
    ("mkfs", re.compile(r"\bmkfs(?:\.\w+)?\b")),
    # 블록 디바이스로의 dd 기록. of=/dev/ 존재만 룩어헤드로 확인(경로 미소비).
    ("dd_to_device", re.compile(rf"\bdd\b(?={_SEP}*\bof=/dev/)")),
    # chmod 재귀 777.
    (
        "chmod_recursive_777",
        re.compile(rf"\bchmod\b(?={_SEP}*-{_SEP}*R)(?={_SEP}*\b777\b)"),
    ),
    # 네트워크에서 받아 셸로 바로 실행(curl|sh / wget|bash). URL은 소비하지 않는다.
    (
        "curl_pipe_shell",
        re.compile(rf"\b(?:curl|wget)\b(?={_SEP}*\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b)"),
    ),
    # main/master 브랜치로의 강제 push.
    (
        "git_push_force_main",
        re.compile(
            rf"\bgit\s+push\b(?={_SEP}*(?:--force(?:-with-lease)?|-f)\b)"
            rf"(?={_SEP}*\b(?:main|master)\b)"
        ),
    ),
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_file() -> str:
    override = os.environ.get("CC_DANGER_STATE_FILE", "").strip()
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".danger_hook.state")


def _record_failure(reason: str) -> None:
    """마지막 실패 1줄만 상태 파일에 남긴다. 이 기록 자체의 실패도 삼킨다."""
    try:
        with open(_state_file(), "w", encoding="utf-8") as fh:
            fh.write(f"{_now_iso()} {reason}\n")
    except Exception:
        pass


def _load_extra_patterns() -> list[tuple[str, "re.Pattern[str]"]]:
    """CC_DANGER_PATTERNS_FILE 의 줄당 정규식을 로드한다.

    파일이 소유자 외에게 쓰기/읽기 가능하면(퍼미션이 0600 계열이 아니면) 신뢰하지
    않고 무시한다 — 남이 심은 정규식으로 훅 동작을 좌우당하지 않기 위함이다.
    """
    path = os.environ.get("CC_DANGER_PATTERNS_FILE", "").strip()
    if not path:
        return []
    try:
        st = os.stat(path)
    except OSError:
        return []
    # 소유자 외 비트(group/other의 rwx)가 하나라도 서 있으면 거부.
    if st.st_mode & 0o077:
        _record_failure(f"patterns file rejected (mode {oct(st.st_mode & 0o777)}): {path}")
        return []
    out: list[tuple[str, "re.Pattern[str]"]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for i, raw in enumerate(fh):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    out.append((f"custom_{i + 1}", re.compile(line)))
                except re.error:
                    # 잘못된 정규식 한 줄이 전체를 막지 않는다.
                    continue
    except OSError:
        return []
    return out


def _match(command: str) -> tuple[str, int, int] | None:
    """첫 매치의 (패턴이름, start, end). 매치 없으면 None."""
    for name, pat in _BUILTIN_PATTERNS + _load_extra_patterns():
        m = pat.search(command)
        if m:
            # 매치 스팬은 명령 키워드만 덮는다(룩어헤드로 인자/경로/URL 미소비).
            # 0폭 매치(순수 룩어헤드 커스텀 패턴 등)면 전부 마스킹되어 원문이 새지
            # 않는다 — 안전 우선이므로 스팬을 확장하지 않는다.
            return name, m.start(), m.end()
    return None


def _mask(command: str, start: int, end: int) -> str:
    """매치 구간만 원문을 남기고 나머지를 '*'로 가린 뒤 최대 길이로 자른다.

    원문 전체는 절대 나가지 않는다: 매치 밖 문자는 전부 '*'가 된다. 명령이 상한보다
    길면 매치 구간이 반드시 보이도록 매치 앞 20자부터 창을 잡아 자른다.
    """
    masked = "".join(
        ch if start <= i < end else "*" for i, ch in enumerate(command)
    )
    if len(masked) <= _MASK_MAX_CHARS:
        return masked
    win_start = max(0, start - 20)
    return masked[win_start : win_start + _MASK_MAX_CHARS]


def _notify(url: str, token: str, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
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
            # 응답 본문은 읽지 않는다(불필요). 2xx 아니면 실패로 기록.
            if resp.status >= 300:
                _record_failure(f"ingest HTTP {resp.status}")
    except Exception as exc:  # noqa: BLE001 - 절대 밖으로 던지지 않는다.
        _record_failure(f"ingest error: {type(exc).__name__}")


def main() -> int:
    url = os.environ.get("AMX_DANGER_INGEST_URL", "").strip()
    token = os.environ.get("AMX_DANGER_INGEST_TOKEN", "").strip()
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

    if payload.get("tool_name") != "Bash":
        return 0

    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command:
        return 0

    try:
        hit = _match(command)
        if hit is None:
            return 0
        pattern_name, start, end = hit
        out = {
            "patternName": pattern_name,
            "commandSha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
            "commandMasked": _mask(command, start, end),
            "sessionId": payload.get("session_id"),
            "cwd": payload.get("cwd"),
            "hostname": socket.gethostname(),
            "ts": _now_iso(),
        }
        user_id = os.environ.get("LANGFUSE_USER_ID", "").strip()
        if user_id:
            out["userId"] = user_id
        _notify(url, token, out)
    except Exception as exc:  # noqa: BLE001 - 어떤 내부 오류도 Claude에 영향 주지 않는다.
        _record_failure(f"internal error: {type(exc).__name__}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
