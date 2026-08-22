"""계정 API의 잔여 사용량 요약 — design-notes/account-remaining-usage-plan.md 1단계.

검증: 창 있음(신선) / 창 없음 / stale 세 케이스로 list_accounts, get_account 응답의
usage 필드를 확인한다. 신선도 판정 SSOT는 app/services/pool.py:412-426
(``_fresh_pct``) — 이 테스트는 그 규칙과 같은 결과가 나오는지를 본다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import get_settings
from app.models import AccountUsageWindow

CREDENTIAL_SET = (
    '{"claudeAiOauth": {"accessToken": "at-test", "refreshToken": "rt-test", '
    '"expiresAt": 4102444800000, "scopes": ["user:inference", "user:profile"], '
    '"emailAddress": "a@example.com", "organizationName": "Acme"}}'
)


def _now() -> datetime:
    return datetime.now(UTC)


def _make_tenant(client) -> str:
    response = client.post("/api/v1/tenants", json={"name": "acme-" + uuid.uuid4().hex[:8]})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _make_account(client, tenant_id: str, email: str) -> dict:
    response = client.post(
        f"/api/v1/tenants/{tenant_id}/accounts",
        json={"email": email, "credentialType": "oauth", "secret": CREDENTIAL_SET},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _insert_window(
    db,
    tenant_id: str,
    account_id: str,
    *,
    window_id: str,
    window_minutes: int,
    pct: float | None,
    resets_at: datetime | None,
    reported_at: datetime,
    server_id: uuid.UUID,
) -> None:
    stmt = pg_insert(AccountUsageWindow).values(
        tenant_id=uuid.UUID(tenant_id),
        account_id=uuid.UUID(account_id),
        window_id=window_id,
        pct=pct,
        resets_at=resets_at,
        window_minutes=window_minutes,
        usage_fetched_at=reported_at,
        reported_at=reported_at,
        server_id=server_id,
    )
    db.execute(stmt)
    db.commit()


def test_account_without_any_window_has_null_usage_slots(client):
    tenant_id = _make_tenant(client)
    account = _make_account(client, tenant_id, "no-window@example.com")

    listed = client.get(f"/api/v1/tenants/{tenant_id}/accounts").json()["items"][0]
    fetched = client.get(f"/api/v1/tenants/{tenant_id}/accounts/{account['id']}").json()

    for body in (listed, fetched):
        assert body["usage"]["fiveHour"] is None
        assert body["usage"]["sevenDay"] is None
        assert body["usage"]["fetchedAt"] is None
        assert body["usage"]["stale"] is True


def test_account_with_fresh_windows_reports_pct_and_reset(client, db):
    tenant_id = _make_tenant(client)
    account = _make_account(client, tenant_id, "fresh@example.com")
    server_id = uuid.uuid4()
    now = _now()
    resets_at = now + timedelta(hours=2)

    _insert_window(
        db, tenant_id, account["id"],
        window_id="five_hour", window_minutes=300, pct=62.0,
        resets_at=resets_at, reported_at=now, server_id=server_id,
    )
    _insert_window(
        db, tenant_id, account["id"],
        window_id="seven_day", window_minutes=10080, pct=41.0,
        resets_at=resets_at, reported_at=now, server_id=server_id,
    )

    for body in (
        client.get(f"/api/v1/tenants/{tenant_id}/accounts").json()["items"][0],
        client.get(f"/api/v1/tenants/{tenant_id}/accounts/{account['id']}").json(),
    ):
        assert body["usage"]["fiveHour"]["pct"] == 62.0
        assert body["usage"]["sevenDay"]["pct"] == 41.0
        assert body["usage"]["fetchedAt"] is not None
        assert body["usage"]["stale"] is False


def test_account_with_old_report_is_stale_but_keeps_last_known_pct(client, db):
    tenant_id = _make_tenant(client)
    account = _make_account(client, tenant_id, "stale@example.com")
    server_id = uuid.uuid4()
    stale_after = get_settings().pool_window_stale_minutes
    old = _now() - timedelta(minutes=stale_after + 5)
    resets_at = old + timedelta(hours=5)

    _insert_window(
        db, tenant_id, account["id"],
        window_id="five_hour", window_minutes=300, pct=87.0,
        resets_at=resets_at, reported_at=old, server_id=server_id,
    )

    for body in (
        client.get(f"/api/v1/tenants/{tenant_id}/accounts").json()["items"][0],
        client.get(f"/api/v1/tenants/{tenant_id}/accounts/{account['id']}").json(),
    ):
        # 마지막 관측값은 그대로 보인다 — stale 이 값을 숨기지 않는다.
        assert body["usage"]["fiveHour"]["pct"] == 87.0
        assert body["usage"]["sevenDay"] is None
        assert body["usage"]["stale"] is True


def test_window_with_unmatched_minutes_is_ignored(client, db):
    tenant_id = _make_tenant(client)
    account = _make_account(client, tenant_id, "odd-window@example.com")
    server_id = uuid.uuid4()
    now = _now()

    _insert_window(
        db, tenant_id, account["id"],
        window_id="one_hour", window_minutes=60, pct=99.0,
        resets_at=now + timedelta(hours=1), reported_at=now, server_id=server_id,
    )

    body = client.get(f"/api/v1/tenants/{tenant_id}/accounts/{account['id']}").json()
    assert body["usage"]["fiveHour"] is None
    assert body["usage"]["sevenDay"] is None
    assert body["usage"]["stale"] is True
