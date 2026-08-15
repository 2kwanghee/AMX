"""무인 수신 엔드포인트 — 위험명령 통보(P5 경로 d).

``/api/v1/ingest/danger-command`` 하나만 둔다. Claude Code PreToolUse 훅이 Bash
명령의 위험 패턴을 감지했을 때 보내는 마스킹 통보를 받아 ``dangerous_command``
경보로 open 한다(``services.danger_alerts``).

이 라우터는 다른 ``/api/v1`` 과 달리 **TenantScope도 admin bearer도 걸지 않는다** —
호출자가 사람이 아니라 무인 에이전트라서다. 대신 정적 토큰(``X-AMX-Ingest-Token``)
만으로 인증하고, 서버에 토큰이 설정돼 있지 않으면 엔드포인트를 아예 비활성(404)해
설정하지 않은 AMS에서는 이 경로가 존재하지 않는 것처럼 행동한다.
"""

from __future__ import annotations

import logging
import secrets
from typing import Annotated

from fastapi import APIRouter, Header

from app import schemas
from app.api.deps import DbSession
from app.config import get_settings
from app.core.errors import ApiError
from app.services import danger_alerts

_logger = logging.getLogger("ams.ingest")

router = APIRouter(prefix="/ingest", tags=["ingest"])


@router.post("/danger-command", response_model=schemas.DangerCommandIngestAck)
def ingest_danger_command(
    body: schemas.DangerCommandIngest,
    db: DbSession,
    x_amx_ingest_token: Annotated[str | None, Header()] = None,
) -> schemas.DangerCommandIngestAck:
    settings = get_settings()
    # 토큰 미설정 → 엔드포인트 비활성(경로가 없는 것처럼 404).
    if not settings.danger_ingest_enabled:
        raise ApiError(404, "Not Found", "ingest.disabled")
    # 상수시간 비교. 헤더 부재/불일치 모두 401.
    supplied = (x_amx_ingest_token or "").encode("utf-8")
    expected = (settings.danger_ingest_token or "").encode("utf-8")
    if not secrets.compare_digest(supplied, expected):
        raise ApiError(401, "Unauthorized", "ingest.invalid_token", "Invalid ingest token.")
    # 전역 고정창 레이트 제한(테넌트 축 없음).
    if not danger_alerts.allow_request(settings.danger_rate_limit_per_min):
        _logger.warning(
            "danger ingest rate limit exceeded (limit=%d/min); dropping notification",
            settings.danger_rate_limit_per_min,
        )
        raise ApiError(429, "Too Many Requests", "ingest.rate_limited")

    danger_alerts.record_danger_command(db, body)
    db.commit()
    return schemas.DangerCommandIngestAck(accepted=True)
