"""대시보드 집계 통계 — dashboard-redesign-plan.md 부록 A.

순수 함수(구간·버킷 계산, 상위 8개+other 조립, argmax 선택)는 DB 없이 직접
검증하고, 나머지는 경로마다 최소 1개씩 실제 PostgreSQL을 통해 확인한다. 계정·
서버는 client(글로벌 관리자 root 토큰, 모든 테넌트에 닿는다)로 API를 거치지 않고
inventory/ORM으로 직접 심는다 — 상태 필드(status="assigned"/"online")까지 만들
때는 API보다 직접 대입이 간단하다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import get_sessionmaker
from app.models import AccountUsageWindow, Alert, Server, SessionUsage, UsageDailyRollup
from app.services import inventory, stats

from tests.test_grpc_channel import _oauth_secret
from tests.test_usage_cost import _acc, _plant

API = "/api/v1"


# -- 시딩 헬퍼 ------------------------------------------------------------------
def _seed_tenant() -> uuid.UUID:
    with get_sessionmaker()() as db:
        return inventory.create_tenant(db, "stats-" + uuid.uuid4().hex[:6]).id


def _seed_server(tenant_id: uuid.UUID, *, name: str | None = None, status: str = "offline") -> uuid.UUID:
    with get_sessionmaker()() as db:
        server = inventory.create_server(
            db, tenant_id, name=name or "srv-" + uuid.uuid4().hex[:8], hostname="h", switch_mode="auto"
        )
        if status != "offline":
            server.status = status
        db.commit()
        return server.id


def _seed_account(
    tenant_id: uuid.UUID,
    email: str,
    *,
    status: str = "available",
    monthly_price: str | None = None,
) -> uuid.UUID:
    with get_sessionmaker()() as db:
        account = inventory.create_account(
            db, tenant_id, email=email, credential_type="oauth", secret=_oauth_secret(email)
        )
        if status != "available":
            account.status = status
        if monthly_price is not None:
            account.monthly_price = Decimal(monthly_price)
        db.commit()
        return account.id


def _add_rollup(
    tenant_id: uuid.UUID,
    day: date,
    server_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    held: float,
    observed: float | None = None,
) -> None:
    with get_sessionmaker()() as db:
        db.add(
            UsageDailyRollup(
                tenant_id=tenant_id,
                day=day,
                server_id=server_id,
                account_id=account_id,
                held_util_seconds=Decimal(str(held)),
                observed_seconds=Decimal(str(observed if observed is not None else held)),
                snapshot_count=1,
            )
        )
        db.commit()


def _add_session(
    tenant_id: uuid.UUID,
    *,
    ended_at: datetime,
    session_id: str | None = None,
    model: str = "claude-opus-5",
    account_id: uuid.UUID | None = None,
    server_id: uuid.UUID | None = None,
    project: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_create_1h_tokens: int = 0,
    cache_create_5m_tokens: int = 0,
    message_count: int = 1,
) -> None:
    with get_sessionmaker()() as db:
        db.add(
            SessionUsage(
                tenant_id=tenant_id,
                session_id=session_id or uuid.uuid4().hex,
                model=model,
                account_id=account_id,
                server_id=server_id,
                project=project,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_create_1h_tokens=cache_create_1h_tokens,
                cache_create_5m_tokens=cache_create_5m_tokens,
                thinking_tokens=0,
                web_search_requests=0,
                web_fetch_requests=0,
                message_count=message_count,
                truncated=False,
                service_tier_counts={},
                stop_reason_counts={},
                started_at=ended_at,
                ended_at=ended_at,
            )
        )
        db.commit()


def _add_alert(tenant_id: uuid.UUID, *, created_at: datetime, status: str = "open") -> None:
    with get_sessionmaker()() as db:
        db.add(
            Alert(
                tenant_id=tenant_id,
                # ck_alerts_kind는 models.ALERT_KINDS로 제한돼 있다 — 임의 문자열은
                # 못 쓰고 실제 종류 중 하나를 빌린다(여기서는 값 자체는 무의미).
                kind="drift",
                severity="warning",
                status=status,
                dedupe_key="test:" + uuid.uuid4().hex,
                created_at=created_at,
            )
        )
        db.commit()


def _add_window(
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    window_minutes: int,
    pct: float,
    server_id: uuid.UUID,
    reported_at: datetime,
) -> None:
    with get_sessionmaker()() as db:
        db.execute(
            pg_insert(AccountUsageWindow).values(
                tenant_id=tenant_id,
                account_id=account_id,
                window_id=str(window_minutes),
                pct=pct,
                resets_at=None,
                window_minutes=window_minutes,
                usage_fetched_at=reported_at,
                reported_at=reported_at,
                server_id=server_id,
            )
        )
        db.commit()


# -- 순수 함수 ------------------------------------------------------------------
def test_range_start_and_prev_window():
    now = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
    assert stats.range_start("24h", now) == now - timedelta(hours=24)
    assert stats.range_start("7d", now) == now - timedelta(days=7)
    assert stats.range_start("30d", now) == now - timedelta(days=30)

    prev_start, prev_end = stats.prev_window("7d", now)
    assert prev_end == now - timedelta(days=7)
    assert prev_start == now - timedelta(days=14)


def test_day_buckets_24h_gives_yesterday_and_today():
    now = datetime(2026, 3, 10, 15, 30, tzinfo=UTC)
    start = stats.range_start("24h", now)
    assert stats.day_buckets(start, now) == [date(2026, 3, 9), date(2026, 3, 10)]


def test_bucket_index_matches_split_n_edges():
    start = datetime(2026, 3, 10, tzinfo=UTC)
    now = start + timedelta(hours=12)
    edges = stats.split_n(start, now, 12)
    assert len(edges) == 12
    assert edges[0] == start
    # 1시간 30분 지난 시각은 두 번째 버킷(index 1)에 속한다(폭 1시간=3600초).
    assert stats.bucket_index(start + timedelta(hours=1, minutes=30), start, 3600, 12) == 1
    # 범위를 벗어난 시각은 가장 가까운 끝으로 접힌다.
    assert stats.bucket_index(start - timedelta(hours=1), start, 3600, 12) == 0
    assert stats.bucket_index(start + timedelta(hours=100), start, 3600, 12) == 11


def test_assemble_series_keeps_top8_and_merges_rest_into_other():
    totals = {f"k{i}": float(10 - i) for i in range(10)}  # k0=10 .. k9=1
    by_bucket = {(f"k{i}", 0): float(10 - i) for i in range(10)}
    labels = {f"k{i}": f"label-{i}" for i in range(10)}
    series = stats.assemble_series(totals, by_bucket, labels, 1)
    assert [s.key for s in series[:8]] == [f"k{i}" for i in range(8)]
    assert series[8].key == "other"
    assert series[8].values[0] == by_bucket[("k8", 0)] + by_bucket[("k9", 0)]


def test_assemble_series_omits_other_when_zero_or_absent():
    totals = {"a": 5.0, "b": 3.0}
    series = stats.assemble_series(totals, {("a", 0): 5.0, ("b", 0): 3.0}, {}, 1)
    assert [s.key for s in series] == ["a", "b"]


def test_pick_top_ignores_none_candidates_and_breaks_ties_on_candidate_string():
    rows = [("g1", "b", 5), ("g1", "a", 5), ("g1", None, 99), ("g2", "x", 1)]
    picked = stats._pick_top(
        rows, key_of_group=lambda r: r[0], key_of_candidate=lambda r: r[1], value=lambda r: r[2]
    )
    assert picked["g1"] == "a"  # 5 vs 5 동점 -> 문자열이 작은 "a"
    assert picked["g2"] == "x"


# -- DB 경유: 경로마다 최소 1개 -------------------------------------------------
def test_summary_prev_window_and_state_counts(client, app_env):
    tenant_id = _seed_tenant()
    server_online = _seed_server(tenant_id, status="online")
    _seed_server(tenant_id, status="offline")
    account_assigned = _seed_account(tenant_id, "sum-a@ex.com", status="assigned")
    _seed_account(tenant_id, "sum-b@ex.com", status="available")

    now = datetime.now(UTC)
    _add_session(
        tenant_id, model="claude-opus-5", account_id=account_assigned, server_id=server_online,
        input_tokens=60, output_tokens=40, ended_at=now - timedelta(days=1),
    )
    _add_session(
        tenant_id, model="claude-opus-5", account_id=account_assigned, server_id=server_online,
        input_tokens=50, ended_at=now - timedelta(days=8),
    )
    _add_alert(tenant_id, created_at=now - timedelta(days=1))
    _add_alert(tenant_id, created_at=now - timedelta(days=8))

    r = client.get(f"{API}/tenants/{tenant_id}/stats/summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["range"] == "7d"
    assert body["tokens"] == {"value": 100, "prev": 50}
    assert body["sessions"] == {"value": 1, "prev": 1}
    assert body["alertsOpened"] == {"value": 1, "prev": 1}
    assert body["alertsOpenNow"] == 2  # 상태 무관하게 둘 다 open인 채로 남아 있다.
    assert body["serversOnline"] == 1
    assert body["accountsActive"] == 1
    assert body["cost"]["value"] == "0.00"  # 가격 미설정 계정만 있어 배분 대상이 없다.
    assert body["cost"]["currency"] == "USD"
    assert len(body["sparkline"]["tokens"]) == 12
    assert sum(body["sparkline"]["tokens"]) == 100
    assert sum(body["sparkline"]["sessions"]) == 1


def test_timeseries_by_server_seconds_vs_by_model_tokens(client, app_env):
    tenant_id = _seed_tenant()
    server_a = _seed_server(tenant_id, name="ts-srv-a")
    account_a = _seed_account(tenant_id, "ts-a@ex.com")
    today = datetime.now(UTC).date()
    yesterday = today - timedelta(days=1)
    _add_rollup(tenant_id, yesterday, server_a, account_a, held=1000)
    _add_rollup(tenant_id, today, server_a, account_a, held=2000)

    r = client.get(f"{API}/tenants/{tenant_id}/stats/timeseries?by=server&range=7d")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unit"] == "seconds"
    series = next(s for s in body["series"] if s["key"] == str(server_a))
    assert len(series["values"]) == len(body["buckets"])
    assert sum(series["values"]) == 3000.0

    now = datetime.now(UTC)
    _add_session(
        tenant_id, model="claude-opus-5", account_id=account_a, server_id=server_a,
        input_tokens=10, output_tokens=5, ended_at=now - timedelta(hours=1),
    )
    r2 = client.get(f"{API}/tenants/{tenant_id}/stats/timeseries?by=model&range=24h")
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["unit"] == "tokens"
    model_series = next(s for s in body2["series"] if s["key"] == "claude-opus-5")
    assert sum(model_series["values"]) == 15


def test_timeseries_ranks_top8_and_merges_rest_into_other(client, app_env):
    tenant_id = _seed_tenant()
    account_a = _seed_account(tenant_id, "ts-other@ex.com")
    now = datetime.now(UTC)
    for i in range(9):
        _add_session(
            tenant_id, model=f"model-{i}", account_id=account_a, input_tokens=9 - i,
            ended_at=now - timedelta(hours=1),
        )

    r = client.get(f"{API}/tenants/{tenant_id}/stats/timeseries?by=model&range=24h")
    body = r.json()
    keys = [s["key"] for s in body["series"]]
    assert len(keys) == 9
    assert keys[-1] == "other"
    other = next(s for s in body["series"] if s["key"] == "other")
    # model-8(값 1)만 9번째로 밀려 other로 들어간다.
    assert sum(other["values"]) == 1


def test_flows_links_server_to_account(client, app_env):
    tenant_id = _seed_tenant()
    server_a = _seed_server(tenant_id, name="flow-srv-a")
    account_a = _seed_account(tenant_id, "flow-a@ex.com")
    today = datetime.now(UTC).date()
    _add_rollup(tenant_id, today, server_a, account_a, held=500)

    r = client.get(f"{API}/tenants/{tenant_id}/stats/flows?range=7d")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unit"] == "seconds"
    node_ids = {n["id"] for n in body["nodes"]}
    assert f"server:{server_a}" in node_ids
    assert f"account:{account_a}" in node_ids
    link = next(
        link for link in body["links"]
        if link["source"] == f"server:{server_a}" and link["target"] == f"account:{account_a}"
    )
    assert link["value"] == 500.0


def test_flows_collapses_deleted_accounts_into_one_node(client, app_env):
    """지워진 계정들은 노드 하나로 합치고 링크 값을 더한다.

    usage_daily_rollup.account_id에는 FK가 없어서(모델 docstring 참조) 계정이
    지워져도 행이 남는다. 그 행들을 계정마다 노드로 펼치면 라벨이 전부
    "(삭제된 계정)"이라 서로 구별되지 않는 노드만 늘어난다.
    """
    tenant_id = _seed_tenant()
    server_a = _seed_server(tenant_id, name="flow-del-srv")
    account_live = _seed_account(tenant_id, "flow-live@ex.com")
    gone_1, gone_2 = uuid.uuid4(), uuid.uuid4()
    today = datetime.now(UTC).date()
    _add_rollup(tenant_id, today, server_a, account_live, held=100)
    _add_rollup(tenant_id, today, server_a, gone_1, held=30)
    _add_rollup(tenant_id, today, server_a, gone_2, held=20)

    r = client.get(f"{API}/tenants/{tenant_id}/stats/flows?range=7d")
    assert r.status_code == 200, r.text
    body = r.json()

    account_nodes = [n for n in body["nodes"] if n["kind"] == "account"]
    assert {n["id"] for n in account_nodes} == {f"account:{account_live}", "account:deleted"}
    assert next(n for n in account_nodes if n["id"] == "account:deleted")["label"] == "(삭제된 계정)"

    by_target = {link["target"]: link["value"] for link in body["links"]}
    assert by_target[f"account:{account_live}"] == 100.0
    # 30 + 20 — 합산된 링크 하나로만 나온다.
    assert by_target["account:deleted"] == 50.0
    assert len([link for link in body["links"] if link["target"] == "account:deleted"]) == 1


def test_accounts_top_model_top_project_top_server_and_windows(client, app_env):
    tenant_id = _seed_tenant()
    server_a = _seed_server(tenant_id, name="acc-srv-a")
    server_b = _seed_server(tenant_id, name="acc-srv-b")
    account_a = _seed_account(tenant_id, "acc-top@ex.com")
    now = datetime.now(UTC)
    _add_session(
        tenant_id, model="claude-opus-5", project="AMX", server_id=server_a, account_id=account_a,
        input_tokens=100, ended_at=now - timedelta(hours=1),
    )
    _add_session(
        tenant_id, model="claude-haiku", project="other-repo", server_id=server_b, account_id=account_a,
        input_tokens=10, ended_at=now - timedelta(hours=2),
    )
    today = now.date()
    _add_rollup(tenant_id, today, server_a, account_a, held=300)
    _add_window(tenant_id, account_a, window_minutes=300, pct=42.5, server_id=server_a, reported_at=now)
    _add_window(tenant_id, account_a, window_minutes=10080, pct=12.0, server_id=server_a, reported_at=now)

    r = client.get(f"{API}/tenants/{tenant_id}/stats/accounts?range=7d")
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    row = next(x for x in rows if x["accountId"] == str(account_a))
    assert row["tokens"] == 110
    assert row["sessions"] == 2
    assert row["topModel"] == "claude-opus-5"
    assert row["topServerId"] == str(server_a)
    assert row["topServerName"] == "acc-srv-a"
    assert row["topProject"] == "AMX"
    assert row["heldSeconds"] == 300.0
    assert row["remaining5HPct"] == 42.5
    assert row["remaining7DPct"] == 12.0


def test_servers_top_account_and_monthly_cost(client, app_env):
    tenant_id = _seed_tenant()
    server_a = _seed_server(tenant_id, name="cost-srv", status="online")
    account_a = _seed_account(tenant_id, "cost-acc@ex.com", monthly_price="90")
    now = datetime.now(UTC)
    _add_session(
        tenant_id, model="claude-opus-5", server_id=server_a, account_id=account_a,
        input_tokens=40, ended_at=now - timedelta(hours=1),
    )
    today = now.date()
    _add_rollup(tenant_id, today, server_a, account_a, held=600)
    # 당월 라이브 테일(usage_cost.compute_month_cost가 읽는 usage_snapshots) — 이번
    # 달 안의 시각이어야 한다.
    _plant(tenant_id, server_a, now - timedelta(hours=1), [_acc(account_a, current=True, positional=(50.0, 0.0))])

    r = client.get(f"{API}/tenants/{tenant_id}/stats/servers?range=7d")
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    row = next(x for x in rows if x["serverId"] == str(server_a))
    assert row["status"] == "online"
    assert row["topAccountId"] == str(account_a)
    assert row["topAccountEmail"] == "cost-acc@ex.com"
    assert row["heldSeconds"] == 600.0
    assert row["cost"]["currency"] == "USD"
    assert Decimal(row["cost"]["amount"]) > 0
    # held_seconds 내림차순 — 다른 서버(활동 없음)가 있다면 뒤로 밀려야 한다.
    assert rows[0]["heldSeconds"] >= rows[-1]["heldSeconds"]


def test_heatmap_shape_and_monday_is_index_zero(client, app_env):
    tenant_id = _seed_tenant()
    account_a = _seed_account(tenant_id, "heat@ex.com")
    now = datetime.now(UTC)
    back = now.weekday()  # 월요일이면 0
    monday_3am = (now - timedelta(days=back)).replace(hour=3, minute=0, second=0, microsecond=0)
    if monday_3am > now:
        monday_3am -= timedelta(days=7)
    _add_session(tenant_id, account_id=account_a, ended_at=monday_3am, session_id="s-heat-mon")

    r = client.get(f"{API}/tenants/{tenant_id}/stats/heatmap?range=7d")
    assert r.status_code == 200, r.text
    cells = r.json()["cells"]
    assert len(cells) == 7
    assert all(len(row) == 24 for row in cells)
    # 요일 인덱스는 월요일=0 고정(schemas.StatsHeatmapResponse 문서화). isodow-1이
    # 파이썬 datetime.weekday()와 같은 값이 되도록 서버가 변환한다.
    assert cells[0][3] == 1
    assert sum(sum(row) for row in cells) == 1


def test_alerts_opened_counts_all_statuses_and_open_now_is_separate(client, app_env):
    tenant_id = _seed_tenant()
    now = datetime.now(UTC)
    # 이번 7일 구간에 생성됐고 상태가 다른 두 경보 — alertsOpened는 상태 무관하게
    # 생성 시각만으로 센다.
    _add_alert(tenant_id, created_at=now - timedelta(hours=1), status="open")
    _add_alert(tenant_id, created_at=now - timedelta(hours=2), status="resolved")
    # 훨씬 전에 생성됐지만 지금도 open인 경보 — alertsOpenNow에는 잡히지만
    # alertsOpened(이번 구간 생성 수)에는 안 잡혀야 한다.
    _add_alert(tenant_id, created_at=now - timedelta(days=30), status="open")

    r = client.get(f"{API}/tenants/{tenant_id}/stats/summary?range=7d")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["alertsOpened"]["value"] == 2  # 상태 무관, 구간 내 생성 2건(open+resolved)
    assert body["alertsOpenNow"] == 2  # 시간창 무관, 지금 open인 것: 최근 1건 + 오래된 1건


def test_servers_includes_deleted_server_and_unassigned_row(client, app_env):
    tenant_id = _seed_tenant()
    server_a = _seed_server(tenant_id, name="to-delete", status="online")
    account_a = _seed_account(tenant_id, "del-srv@ex.com")
    now = datetime.now(UTC)
    _add_session(
        tenant_id, model="claude-opus-5", server_id=server_a, account_id=account_a,
        input_tokens=30, ended_at=now - timedelta(hours=1),
    )
    # server_id가 NULL인(hostname 미매칭 등) 미귀속 세션.
    _add_session(
        tenant_id, model="claude-haiku", server_id=None, account_id=account_a,
        input_tokens=7, ended_at=now - timedelta(hours=1),
    )
    with get_sessionmaker()() as session:
        session.execute(delete(Server).where(Server.id == server_a))
        session.commit()

    r = client.get(f"{API}/tenants/{tenant_id}/stats/servers?range=7d")
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]

    deleted_row = next(x for x in rows if x["serverId"] == str(server_a))
    assert deleted_row["name"] == "(삭제된 서버)"
    assert deleted_row["status"] == "deleted"
    assert deleted_row["tokens"] == 30

    unassigned_row = next(x for x in rows if x["serverId"] is None)
    assert unassigned_row["name"] == "(미귀속)"
    assert unassigned_row["status"] == "deleted"
    assert unassigned_row["tokens"] == 7


def test_empty_tenant_all_paths_return_200_with_empty_shape(client, app_env):
    tenant_id = _seed_tenant()

    r = client.get(f"{API}/tenants/{tenant_id}/stats/summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tokens"] == {"value": 0, "prev": 0}
    assert body["sessions"] == {"value": 0, "prev": 0}
    assert body["alertsOpened"] == {"value": 0, "prev": 0}
    assert body["alertsOpenNow"] == 0
    assert body["serversOnline"] == 0
    assert body["accountsActive"] == 0
    assert body["cost"] == {"value": "0.00", "currency": "USD", "prev": "0.00"}
    assert body["sparkline"]["tokens"] == [0] * 12
    assert body["sparkline"]["sessions"] == [0] * 12

    for by in ("model", "server", "account"):
        r = client.get(f"{API}/tenants/{tenant_id}/stats/timeseries?by={by}")
        assert r.status_code == 200, r.text
        assert r.json()["series"] == []

    r = client.get(f"{API}/tenants/{tenant_id}/stats/flows")
    assert r.status_code == 200, r.text
    assert r.json()["nodes"] == []
    assert r.json()["links"] == []

    r = client.get(f"{API}/tenants/{tenant_id}/stats/accounts")
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == []

    r = client.get(f"{API}/tenants/{tenant_id}/stats/servers")
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == []

    r = client.get(f"{API}/tenants/{tenant_id}/stats/heatmap")
    assert r.status_code == 200, r.text
    cells = r.json()["cells"]
    assert len(cells) == 7
    assert all(row == [0] * 24 for row in cells)


# -- 422 ------------------------------------------------------------------------
def test_invalid_range_is_422(client, app_env):
    tenant_id = _seed_tenant()
    assert client.get(f"{API}/tenants/{tenant_id}/stats/summary?range=90d").status_code == 422


def test_timeseries_missing_by_is_422(client, app_env):
    tenant_id = _seed_tenant()
    assert client.get(f"{API}/tenants/{tenant_id}/stats/timeseries?range=7d").status_code == 422


def test_timeseries_invalid_by_is_422(client, app_env):
    tenant_id = _seed_tenant()
    r = client.get(f"{API}/tenants/{tenant_id}/stats/timeseries?by=project&range=7d")
    assert r.status_code == 422
