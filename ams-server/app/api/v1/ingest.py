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
import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app import schemas
from app.api.deps import DbSession
from app.config import get_settings
from app.core.errors import ApiError
from app.services import danger_alerts

_logger = logging.getLogger("ams.ingest")

# 무자격으로도 도달 가능한 경로라, 본문을 읽기 전에 Content-Length로 큰 요청을 값싸게
# 거른다. 정상 통보는 수백 바이트 수준이므로 64KB면 넉넉하다.
_MAX_INGEST_BYTES = 64 * 1024

router = APIRouter(prefix="/ingest", tags=["ingest"])


def _reject_oversized(content_length: str | None, actual_len: int | None = None) -> None:
    """Content-Length(또는 실제 바이트 수)가 상한을 넘으면 413. 본문 파싱보다 먼저 부른다."""
    for value in (content_length, actual_len):
        if value is None:
            continue
        try:
            n = int(value)
        except (ValueError, TypeError):
            continue
        if n > _MAX_INGEST_BYTES:
            raise ApiError(413, "Payload Too Large", "ingest.too_large")


@router.post("/danger-command", response_model=schemas.DangerCommandIngestAck)
async def ingest_danger_command(
    request: Request,
    db: DbSession,
    x_amx_ingest_token: Annotated[str | None, Header()] = None,
) -> schemas.DangerCommandIngestAck:
    # 본문을 읽기/파싱하기 전에 Content-Length 상한을 값싸게 검사한다(FastAPI의 자동
    # 본문 검증은 이 지점보다 뒤라, 크기 가드를 핸들러 초입에서 직접 수행한다).
    _reject_oversized(request.headers.get("content-length"))

    settings = get_settings()
    # 토큰·귀속 테넌트가 없으면 엔드포인트 비활성(경로가 없는 것처럼 404).
    if not settings.danger_ingest_enabled:
        raise ApiError(404, "Not Found", "ingest.disabled")
    # 상수시간 비교. 헤더 부재/불일치 모두 401.
    supplied = (x_amx_ingest_token or "").encode("utf-8")
    expected = (settings.danger_ingest_token or "").encode("utf-8")
    if not secrets.compare_digest(supplied, expected):
        raise ApiError(401, "Unauthorized", "ingest.invalid_token", "Invalid ingest token.")
    # 귀속 테넌트 해석(danger_ingest_enabled가 존재를 보장하나, UUID 형식은 여기서 검증).
    try:
        tenant_id = uuid.UUID(settings.danger_tenant)
    except (ValueError, TypeError):
        _logger.warning("danger ingest: attribution tenant is not a valid UUID; disabled")
        raise ApiError(404, "Not Found", "ingest.disabled")
    # 전역 고정창 레이트 제한(테넌트 축 없음).
    if not danger_alerts.allow_request(settings.danger_rate_limit_per_min):
        _logger.warning(
            "danger ingest rate limit exceeded (limit=%d/min); dropping notification",
            settings.danger_rate_limit_per_min,
        )
        raise ApiError(429, "Too Many Requests", "ingest.rate_limited")

    # 본문 로드·검증(수동). Content-Length 없는 청크 전송 대비로 실제 바이트도 재검사한다.
    raw = await request.body()
    _reject_oversized(None, len(raw))
    try:
        body = schemas.DangerCommandIngest.model_validate_json(raw)
    except ValidationError as exc:
        # 스키마 위반은 FastAPI 기본과 동일하게 422(입력 스크럽은 에러 핸들러가 한다).
        raise RequestValidationError(exc.errors()) from exc

    danger_alerts.record_danger_command(db, tenant_id, body)
    db.commit()
    return schemas.DangerCommandIngestAck(accepted=True)
