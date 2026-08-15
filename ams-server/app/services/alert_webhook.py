"""경보 웹훅 발송 계층 — 아웃박스 드레인 스윕(BACKLOG G41 / P5).

``services.alerts``가 경보 open/resolve 전이를 여닫을 때 같은 트랜잭션으로
``alert_webhook_outbox``에 행을 스테이징한다(유령 알림 방지). 이 스윕은 그 아웃박스를
HTTP POST로 드레인하는 형제 스윕이다 — 자체 advisory 락(…08)으로 한 틱에 한 인스턴스만
발송하고, F5/Langfuse 스윕과 같은 2단계 구조를 따른다:

* 발송(HTTP)은 **락·트랜잭션 밖에서** 한다(P4 교훈). 만기 행을 짧은 트랜잭션에서
  예약(reserve — ``next_attempt_at``를 리스 창만큼 앞당김)하고 커밋해 락을 놓은 뒤,
  락 없이 POST하고, 결과를 다시 짧은 트랜잭션으로 반영한다. 리스 덕분에 발송 중인 행을
  다른 인스턴스(또는 다음 틱)가 다시 집지 않는다.
* 성공 → 행 삭제. 실패 → ``attempt`` 증가 + 지수 백오프로 ``next_attempt_at`` 재설정.
  상한(``_MAX_ATTEMPTS``) 초과 → 경고 로그 후 행 폐기(무한 적재 금지).

서명: 본문은 결정적 JSON(정렬·compact)이고, 헤더 ``X-AMS-Timestamp``(유닉스 초)와
``X-AMS-Signature: sha256=HMAC-SHA256(시크릿, 타임스탬프 + 본문)``으로 무결성·리플레이를
막는다. 시크릿은 서명 계산에만 쓰이고 로그·에러 문자열에 절대 남기지 않는다.

웹훅이 비활성(URL/시크릿 미설정)이면 스윕은 즉시 no-op이다(아웃박스도 애초에 비어 있음).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import try_advisory_xact_lock as _try_advisory_xact_lock
from app.models import AlertWebhookOutbox
from app.services import alerts

# 웹훅 발송이 상한을 넘겨 폐기될 때 여는 관측용 셀프 경보 kind.
_DROPPED_KIND = "alert_webhook_dropped"

_logger = logging.getLogger("ams.alert_webhook")

# 형제 스윕 락 키 — offline(…01)·sent(…02)·billing(…03)·rollup(…04)·retention(…05)·
# watermark(…06)·langfuse-metrics(…07)에 이은 여덟 번째. 트랜잭션 범위(예약 커밋에서 해제).
_ALERT_WEBHOOK_DRAIN_LOCK_KEY = 0x414D580F08

# 한 틱에 발송을 시도하는 행 수 상한. 불량 수신자로 태스크가 오래 붙잡히지 않도록 작게.
_BATCH_SIZE = 20
# 이 횟수만큼 실패하면(= attempt가 이 값에 도달) 행을 폐기한다.
_MAX_ATTEMPTS = 8
# 지수 백오프: base * 2^(attempt-1), 상한 cap.
_BACKOFF_BASE_SECONDS = 60
_BACKOFF_CAP_SECONDS = 3600
# 예약 리스 창 — 발송이 진행되는 동안 다른 인스턴스가 같은 행을 집지 못하게 한다.
# HTTP 타임아웃보다 넉넉히 크게 둔다.
_LEASE_SECONDS = 300


def _now() -> datetime:
    # 테스트가 시각을 고정할 수 있도록 간접화.
    return datetime.now(UTC)


def _backoff_seconds(attempt: int) -> int:
    """실패 ``attempt``회 이후의 다음 재시도까지 대기 초. 지수 증가 후 cap."""
    delay = _BACKOFF_BASE_SECONDS * (2 ** max(0, attempt - 1))
    return min(delay, _BACKOFF_CAP_SECONDS)


def _payload(row: dict) -> dict:
    return {
        "alertId": str(row["alert_id"]),
        "kind": row["kind"],
        "status": row["status"],
        "tenantId": str(row["tenant_id"]),
        "serverId": str(row["server_id"]) if row["server_id"] is not None else None,
        "detail": row["detail"],
        "occurredAt": row["occurred_at"].astimezone(UTC).isoformat(),
    }


def _sign(secret: str, timestamp: str, body: str) -> str:
    mac = hmac.new(
        secret.encode(), (timestamp + body).encode(), hashlib.sha256
    ).hexdigest()
    return f"sha256={mac}"


def _deliver(client: httpx.Client, settings, row: dict) -> bool:
    """한 아웃박스 행을 POST한다. 2xx면 True, 그 외/예외면 False.

    실패 로그에 시크릿이나 요청 URL(쿼리에 자격이 실릴 수 있음)을 남기지 않는다.
    """
    body = json.dumps(_payload(row), separators=(",", ":"), sort_keys=True)
    timestamp = str(int(_now().timestamp()))
    headers = {
        "Content-Type": "application/json",
        "X-AMS-Timestamp": timestamp,
        "X-AMS-Signature": _sign(settings.alert_webhook_secret, timestamp, body),
    }
    try:
        response = client.post(
            settings.alert_webhook_url,
            content=body,
            headers=headers,
            timeout=settings.alert_webhook_timeout_seconds,
        )
        response.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 - 어떤 예외든 백오프 경로로 보내 발송이 멈추지 않게
        # httpx.HTTPError 외의 예상 못 한 예외(직렬화·런타임)도 여기서 삼켜 _finalize의
        # 백오프/폐기 경로에 반드시 도달하게 한다. 시크릿/URL은 로그에 남기지 않는다.
        _logger.warning(
            "alert webhook delivery failed for alert %s (%s/%s attempt %d): %s",
            row["alert_id"],
            row["kind"],
            row["status"],
            row["attempt"],
            type(exc).__name__,
        )
        return False


def _reserve_due(db: Session) -> tuple[list[dict], uuid.UUID | None]:
    """만기 행을 고유 리스 토큰으로 예약하고 (스냅샷 목록, 리스 토큰)을 돌려준다.

    락을 잡고, ``next_attempt_at <= now`` 행을 ``FOR UPDATE SKIP LOCKED``로 집어
    ``next_attempt_at``를 리스 창만큼 앞으로 밀고 이번 배치의 ``lease_token``을 심은 뒤
    커밋한다(락 해제). 커밋 후에는 이 행들이 만기에서 빠지므로 발송을 락 밖에서 안전하게
    진행할 수 있고, finalize는 이 토큰으로 소유를 확인한다.
    """
    if not _try_advisory_xact_lock(db, _ALERT_WEBHOOK_DRAIN_LOCK_KEY):
        return [], None
    now = _now()
    due = list(
        db.scalars(
            select(AlertWebhookOutbox)
            .where(AlertWebhookOutbox.next_attempt_at <= now)
            .order_by(AlertWebhookOutbox.next_attempt_at)
            .limit(_BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )
    )
    snapshots = [
        {
            "id": r.id,
            "alert_id": r.alert_id,
            "tenant_id": r.tenant_id,
            "server_id": r.server_id,
            "kind": r.kind,
            "status": r.status,
            "detail": r.detail,
            "occurred_at": r.occurred_at,
            "attempt": r.attempt,
        }
        for r in due
    ]
    lease_token: uuid.UUID | None = None
    if snapshots:
        lease_token = uuid.uuid4()
        lease_until = now + timedelta(seconds=_LEASE_SECONDS)
        db.execute(
            update(AlertWebhookOutbox)
            .where(AlertWebhookOutbox.id.in_([s["id"] for s in snapshots]))
            .values(next_attempt_at=lease_until, lease_token=lease_token)
        )
    db.commit()
    return snapshots, lease_token


def _finalize(
    db: Session,
    delivered: list[uuid.UUID],
    failed: list[dict],
    lease_token: uuid.UUID,
) -> None:
    """성공 행 삭제, 실패 행 백오프/폐기. 리스 토큰이 일치하는(자신이 예약한) 행만 손댄다.

    리스가 만료돼 다른 인스턴스가 재예약(새 토큰)했다면 그 행은 소유가 아니므로 여기서
    건드리지 않는다 — 이중 삭제/이중 폐기를 막는 소유 검증. caller와 무관한 짧은 txn.
    """
    all_ids = list(delivered) + [r["id"] for r in failed]
    owned = (
        set(
            db.scalars(
                select(AlertWebhookOutbox.id).where(
                    AlertWebhookOutbox.id.in_(all_ids),
                    AlertWebhookOutbox.lease_token == lease_token,
                )
            )
        )
        if all_ids
        else set()
    )
    now = _now()
    del_ids = [i for i in delivered if i in owned]
    if del_ids:
        db.execute(delete(AlertWebhookOutbox).where(AlertWebhookOutbox.id.in_(del_ids)))
    discarded: list[dict] = []
    for row in failed:
        if row["id"] not in owned:
            continue  # 소유 아님 → no-op
        attempt = row["attempt"] + 1
        if attempt >= _MAX_ATTEMPTS:
            discarded.append(row)
            _logger.warning(
                "alert webhook for alert %s (%s/%s) discarded after %d attempts",
                row["alert_id"],
                row["kind"],
                row["status"],
                attempt,
            )
            continue
        db.execute(
            update(AlertWebhookOutbox)
            .where(AlertWebhookOutbox.id == row["id"])
            .values(
                attempt=attempt,
                next_attempt_at=now + timedelta(seconds=_backoff_seconds(attempt)),
            )
        )
    if discarded:
        db.execute(
            delete(AlertWebhookOutbox).where(
                AlertWebhookOutbox.id.in_([r["id"] for r in discarded])
            )
        )
        # 폐기는 조용한 유실이므로 관측용 셀프 경보를 연다. 이 경보는 웹훅 아웃박스에
        # 스테이징하지 않는다(stage_webhook=False) — 발송 실패→셀프 경보→발송 실패의
        # 무한 재귀를 끊기 위해서다.
        for row in discarded:
            alerts.open_system_alert(
                db,
                tenant_id=row["tenant_id"],
                kind=_DROPPED_KIND,
                severity="warning",
                detail={
                    "dropped_alert_id": str(row["alert_id"]),
                    "dropped_kind": row["kind"],
                    "dropped_status": row["status"],
                    "attempts": row["attempt"] + 1,
                },
                stage_webhook=False,
            )
    db.commit()


def sweep_alert_webhook(db: Session, *, client: httpx.Client | None = None) -> int:
    """아웃박스를 드레인한다. 성공적으로 발송한 행 수를 반환한다.

    웹훅 미설정이면 no-op(0). 만기 행을 락 안에서 예약·커밋(락 해제)한 뒤, 락 밖에서
    POST하고, 결과를 다시 커밋한다. 발송 중 실패는 격리돼 나머지 행에 전파되지 않는다.
    """
    settings = get_settings()
    if not settings.alert_webhook_enabled:
        return 0

    snapshots, lease_token = _reserve_due(db)
    if not snapshots or lease_token is None:
        return 0

    owns_client = client is None
    client = client or httpx.Client(timeout=settings.alert_webhook_timeout_seconds)
    delivered: list[uuid.UUID] = []
    failed: list[dict] = []
    try:
        for row in snapshots:
            if _deliver(client, settings, row):
                delivered.append(row["id"])
            else:
                failed.append(row)
    finally:
        if owns_client:
            client.close()

    _finalize(db, delivered, failed, lease_token)
    return len(delivered)
