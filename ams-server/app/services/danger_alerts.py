"""위험명령 통보 수신 → 경보(services.danger_alerts, P5 경로 d).

Claude Code PreToolUse 훅(``deploy/langfuse/danger_hook.py``)이 Bash 명령의 위험
패턴을 감지하면 원문 대신 마스킹본을 AMS로 POST 한다. 이 서비스는 그 한 건을
``dangerous_command`` 경보로 open 한다 — 웹훅 아웃박스를 통과하므로(알림이 목적)
설정된 수신자에게 그대로 전달된다.

특성:
* **system 범위**(server_id NULL). 무인 에이전트 발이라 특정 서버/테넌트에 매이지
  않으므로 고정 시스템 테넌트(``SYSTEM_TENANT_ID``, nil UUID)에 귀속시킨다.
* **dedupe = (hostname, patternName, commandSha256)**. 같은 호스트에서 같은 위험
  명령이 반복되면 새 경보가 폭주하지 않고 기존 open 경보의 detail만 갱신된다.
* **auto-resolve 없음**. 이벤트성이라 관리자가 ack/resolve 한다.
* **원문 저장 금지**. detail에는 마스킹본·해시·세션·호스트·cwd만 담는다.

레이트 제한은 테넌트가 아닌 **전역 고정창**(분당 상한)이다 — 무인 호출이라 테넌트
축이 없고, 폭주하는 훅이 경보 테이블을 채우지 못하게 막는 것이 목적이다. 프로세스
로컬 상태이므로 다중 인스턴스에서는 인스턴스당 상한이다(단순함 우선).
"""

from __future__ import annotations

import threading
import time
import uuid

from sqlalchemy.orm import Session

from app import schemas
from app.services import alerts

# 무인 에이전트 발 경보의 귀속 테넌트. alerts.tenant_id 에는 FK가 없고 server_id가
# NULL이면 복합 FK도 강제되지 않으므로, 실제 tenants 행 없이 안전하게 쓸 수 있다.
SYSTEM_TENANT_ID = uuid.UUID(int=0)

_KIND = "dangerous_command"

# 전역 고정창 레이트 리미터 상태(프로세스 로컬).
_rl_lock = threading.Lock()
_rl_window_start: float = 0.0
_rl_count: int = 0


def _monotonic() -> float:
    return time.monotonic()


def reset_rate_limit() -> None:
    """테스트용 — 고정창 카운터를 비운다."""
    global _rl_window_start, _rl_count
    with _rl_lock:
        _rl_window_start = 0.0
        _rl_count = 0


def allow_request(limit_per_min: int) -> bool:
    """이번 호출이 분당 상한 안이면 True. 상한 이하로 소진되면 카운트를 올린다.

    창(60초)이 지나면 카운터를 리셋한다. ``limit_per_min <= 0`` 이면 제한 없음.
    """
    if limit_per_min <= 0:
        return True
    global _rl_window_start, _rl_count
    now = _monotonic()
    with _rl_lock:
        if now - _rl_window_start >= 60.0:
            _rl_window_start = now
            _rl_count = 0
        if _rl_count >= limit_per_min:
            return False
        _rl_count += 1
        return True


def dedupe_key(hostname: str, pattern_name: str, command_sha256: str) -> str:
    return f"{_KIND}:{hostname}:{pattern_name}:{command_sha256}"


def record_danger_command(db: Session, payload: schemas.DangerCommandIngest) -> None:
    """위험명령 통보 1건을 ``dangerous_command`` 경보로 open 한다. caller가 커밋한다.

    원문 명령은 저장하지 않는다 — detail에는 마스킹본·sha256·세션·호스트·cwd·유저·
    타임스탬프만 담는다.
    """
    detail = {
        "patternName": payload.pattern_name,
        "commandSha256": payload.command_sha256,
        "commandMasked": payload.command_masked,
        "sessionId": payload.session_id,
        "cwd": payload.cwd,
        "hostname": payload.hostname,
        "userId": payload.user_id,
        "ts": payload.ts,
    }
    alerts.open_event_alert(
        db,
        tenant_id=SYSTEM_TENANT_ID,
        kind=_KIND,
        severity="critical",
        dedupe_key=dedupe_key(payload.hostname, payload.pattern_name, payload.command_sha256),
        detail=detail,
    )
