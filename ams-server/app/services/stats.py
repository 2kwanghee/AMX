"""대시보드 집계 통계 — 계약은 docs/design-notes/dashboard-redesign-plan.md 부록 A.

시간 구간 계산·버킷 나누기·상위 8개+other 조립처럼 DB를 몰라도 되는 부분은 순수
함수로 떼어 두고, 실제 조회는 그 아래 DB 함수들이 SQL GROUP BY로 한다(행을 통째로
끌어와 파이썬에서 집계하지 않는다). 라우터(app/api/v1/stats.py)는 이 모듈의
결과를 그대로 응답 스키마에 옮겨 담기만 한다.

두 축의 "사용량"을 섞지 않는다: 서버 축(occupancy)은 ``usage_daily_rollup``의
``held_util_seconds``(일 단위 적분, 단위 seconds), 모델·계정 축은
``session_usage``의 토큰 합(단위 tokens)이다. 토큰 합의 정의는
``input + output + cache_read + cache_create_1h + cache_create_5m`` — thinking은
output의 부분집합이라 더하지 않는다(세션 비용구조 수집과 동일 규칙).

계약이 필드 존재만 정하고 세부 판정 기준까지는 정하지 않은 곳은 아래처럼 좁혔다:
* ``alerts_opened``는 "그 구간에 생성된(created_at) 경보 수, 상태 무관"이다 —
  다른 summary 필드와 같은 [range, prev] 창 규칙을 따른다. 지금 열려 있는 경보
  총수는 시간창과 무관한 별도 값 ``alerts_open_now``로 낸다(경보 상태 이력
  테이블이 없어 "그 시점에 열려 있었는지"는 복원할 수 없기 때문에 두 값을 분리).
* ``accounts_active``는 ``accounts.status == "assigned"``(지금 서버에 붙어 실제
  쓰이는 계정)로 센다.
* 서버 쪽 ``cost``는 usage_cost가 통화별로 나눠 주는 값 중 금액이 가장 큰 통화
  하나를 대표로 쓴다(한 서버에 통화가 다른 계정이 섞여 있는 드문 경우의 단순화).
* ``summary``의 ``cost.prev``(직전 달)는 ``usage_cost.compute_month_cost``를 다시
  부르지 않고 rollup만으로 어림한다 — 그 무거운 함수는 이 모듈에서 ``value``용
  한 번(당월)과 ``servers()``의 한 번, 합쳐 최대 두 번만 부른다(``_prev_month_cost_estimate``
  문서 참고).
* ``stats/servers``의 행 집합은 지금 존재하는 ``servers`` 테이블만이 아니라 그
  기간에 활동이 있었던(session_usage·usage_daily_rollup) 서버까지 포함한다.
  지워진 서버는 ``name="(삭제된 서버)"``·``status="deleted"``로, server_id가
  NULL인(미귀속) 세션은 ``server_id=None``·``name="(미귀속)"`` 한 행으로 합친다
  (unassigned 행도 StatsServerRow.status 타입에 "unassigned"가 없어 같은
  "deleted" 코드를 공유한다 — "지금 살아 있는 서버가 아니다"라는 점은 같다).
  timeseries·flows의 라벨도 못 찾은 id는 원본 uuid 대신 "(삭제된 서버)"/
  "(삭제된 계정)"으로 채운다.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.orm import Session

from app.models import Account, AccountUsageWindow, Alert, Server, SessionUsage, UsageDailyRollup
from app.services import usage_cost

# -- 구간 상수 ------------------------------------------------------------------
_RANGE_SECONDS: dict[str, int] = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
# by=model|account(session_usage) 타임시리즈 버킷 폭. by=server(rollup)는 항상
# 일 단위라 이 표를 타지 않는다(_day_buckets를 따로 쓴다).
_SESSION_BUCKET_SECONDS: dict[str, int] = {"24h": 3600, "7d": 21600, "30d": 86400}
_SPARKLINE_BUCKETS = 12
_TOP_N = 8
_ACCOUNTS_LIMIT = 50
# account_usage_windows의 window_minutes 매칭값 — accounts.py의 5시간/7일 규약과 동일.
_FIVE_HOUR_MINUTES = 300
_SEVEN_DAY_MINUTES = 10080

_TOKEN_SUM = (
    SessionUsage.input_tokens
    + SessionUsage.output_tokens
    + SessionUsage.cache_read_tokens
    + SessionUsage.cache_create_1h_tokens
    + SessionUsage.cache_create_5m_tokens
)


# -- 순수 함수: 구간·버킷 계산 --------------------------------------------------
def range_start(range_: str, now: datetime) -> datetime:
    return now - timedelta(seconds=_RANGE_SECONDS[range_])


def prev_window(range_: str, now: datetime) -> tuple[datetime, datetime]:
    """직전 같은 길이 구간 ``[prev_start, start)``. summary의 prev 필드가 쓴다."""
    start = range_start(range_, now)
    span = now - start
    return start - span, start


def day_buckets(start: datetime, now: datetime) -> list[date]:
    """start가 속한 UTC 날짜부터 now가 속한 날짜까지, 하루 단위 목록.

    range=24h면 보통 어제·오늘 2개가 나온다(부록 A 공통 규칙 — rollup은 일
    단위라 24h 요청도 당일·전일 2행을 그대로 쓴다).
    """
    d0 = start.astimezone(UTC).date()
    d1 = now.astimezone(UTC).date()
    out = [d0]
    d = d0
    while d < d1:
        d += timedelta(days=1)
        out.append(d)
    return out


def session_bucket_seconds(range_: str) -> int:
    return _SESSION_BUCKET_SECONDS[range_]


def bucket_starts(start: datetime, now: datetime, step_seconds: int) -> list[datetime]:
    """start부터 step 간격으로 now를 덮을 때까지의 버킷 시작 시각 목록."""
    span = (now - start).total_seconds()
    n = max(1, math.ceil(span / step_seconds))
    return [start + timedelta(seconds=i * step_seconds) for i in range(n)]


def split_n(start: datetime, now: datetime, n: int) -> list[datetime]:
    """``[start, now]``를 n개의 동일 폭 구간으로 나눈 시작 시각들(스파크라인용).

    summary()의 스파크라인 버킷 폭 계산과 같은 나눗셈이다 — DB 함수는 이 폭을
    SQL의 ``floor(epoch 차 / 폭)``으로 그대로 재현하므로(파이썬 루프로 세션 행을
    버킷팅하지 않는다), 그 나눗셈 규칙 자체를 여기서 순수 함수로 고정해 테스트한다.
    """
    step = (now - start) / n
    return [start + step * i for i in range(n)]


def bucket_index(ts: datetime, start: datetime, step_seconds: int, n: int) -> int:
    """ts가 몇 번째 버킷에 속하는지. 범위를 벗어나면 가장 가까운 끝으로 접는다.

    timeseries()가 SQL로 계산하는 ``floor((epoch(ts) - epoch(start)) / step)``과
    같은 식이다 — 그 SQL 버킷 규칙을 순수 파이썬으로 고정해 테스트하기 위한 것.
    """
    idx = int((ts - start).total_seconds() // step_seconds)
    return max(0, min(n - 1, idx))


@dataclass
class SeriesRow:
    key: str
    label: str
    values: list[float]


def assemble_series(
    totals: dict[str, float], by_bucket: dict[tuple[str, int], float], labels: dict[str, str], n: int
) -> list[SeriesRow]:
    """이미 (key, bucket) -> 합계로 집계된 결과를 상위 8개 + other 시리즈로 조립한다.

    순위는 전체 구간 합(totals) 기준 내림차순, 동점은 key 문자열로 결정성을 준다.
    9번째부터는 버킷별로 합쳐 "other" 한 줄로 묶되, 그 합이 전부 0이면 아예 뺀다.
    """
    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    top_keys = [k for k, _ in ranked[:_TOP_N]]
    rest_keys = [k for k, _ in ranked[_TOP_N:]]

    out = [
        SeriesRow(
            key=k, label=labels.get(k, k), values=[by_bucket.get((k, i), 0.0) for i in range(n)]
        )
        for k in top_keys
    ]
    if rest_keys:
        other = [0.0] * n
        for k in rest_keys:
            for i in range(n):
                other[i] += by_bucket.get((k, i), 0.0)
        if any(other):
            out.append(SeriesRow(key="other", label="기타", values=other))
    return out


def _pick_top(rows: list[tuple], *, key_of_group, key_of_candidate, value) -> dict:
    """(group, candidate, value) 꼴로 이미 집계된 행들에서 group별 최댓값 candidate를 고른다.

    candidate가 None인 행(서버·모델·프로젝트 미상)은 후보에서 뺀다. 동점은 후보
    문자열로 결정성을 준다.
    """
    best: dict = {}
    best_val: dict = {}
    for row in rows:
        g = key_of_group(row)
        k = key_of_candidate(row)
        if k is None:
            continue
        v = value(row) or 0
        if g not in best_val or v > best_val[g] or (v == best_val[g] and str(k) < str(best[g])):
            best_val[g] = v
            best[g] = k
    return best


# -- DB 함수 ----------------------------------------------------------------


@dataclass
class ValuePrev:
    value: int
    prev: int


@dataclass
class CostValue:
    value: Decimal
    currency: str
    prev: Decimal


@dataclass
class Sparkline:
    tokens: list[int]
    sessions: list[int]


@dataclass
class Summary:
    tokens: ValuePrev
    cost: CostValue
    sessions: ValuePrev
    alerts_opened: ValuePrev
    # 시간창과 무관한 "지금" 상태값 — alerts_opened(그 구간에 생성된 수)와는
    # 다른 질문에 답한다.
    alerts_open_now: int
    servers_online: int
    accounts_active: int
    sparkline: Sparkline


def _session_totals(db: Session, tenant_id: uuid.UUID, start: datetime, end: datetime) -> tuple[int, int]:
    """``[start, end)`` 구간의 (토큰 합, 세션 수). session_usage는 (세션,모델) 단위라 세션
    수는 distinct session_id로 센다."""
    row = db.execute(
        select(func.coalesce(func.sum(_TOKEN_SUM), 0), func.count(func.distinct(SessionUsage.session_id)))
        .where(
            SessionUsage.tenant_id == tenant_id,
            SessionUsage.ended_at >= start,
            SessionUsage.ended_at < end,
        )
    ).one()
    return int(row[0]), int(row[1])


def _alerts_opened_count(db: Session, tenant_id: uuid.UUID, start: datetime, end: datetime) -> int:
    """그 구간에 생성된(``created_at``) 경보 수 — 지금 상태(open/acked/resolved)는 무관."""
    return int(
        db.scalar(
            select(func.count())
            .select_from(Alert)
            .where(
                Alert.tenant_id == tenant_id,
                Alert.created_at >= start,
                Alert.created_at < end,
            )
        )
        or 0
    )


def _alerts_open_now_count(db: Session, tenant_id: uuid.UUID) -> int:
    """생성 시각과 무관하게 지금 status="open"인 경보 총수."""
    return int(
        db.scalar(
            select(func.count())
            .select_from(Alert)
            .where(Alert.tenant_id == tenant_id, Alert.status == "open")
        )
        or 0
    )


def _month_before(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _prev_month_cost_estimate(db: Session, tenant_id: uuid.UUID, year: int, month: int) -> tuple[Decimal, str]:
    """직전 달 배분 비용의 어림값 — ``usage_cost.compute_month_cost``를 또 부르지
    않고 이미 봉인됐을 그 달의 rollup만으로 낸다.

    ``usage_cost._distribute``는 계정 가격 전액을 반올림 나머지까지 정확히
    나누므로, held(또는 관측만 있고 held가 0인 경우 observed)가 하나라도 있는
    계정은 그 가격 전액이 ``compute_month_cost``의 ``subtotal.allocated_cost``에
    들어간다 — 서버별 분배 내역 없이 계정 합계만으로도 이 총액은 재현된다. 그
    달이 아직 안 봉인됐으면(라이브 테일이 남아 있으면) 실제보다 작게 잡힐 수
    있는데, prev는 추세 참고용 값이라 이 오차는 감수한다.
    """
    month_start = date(year, month, 1)
    month_end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    rows = db.execute(
        select(
            UsageDailyRollup.account_id,
            func.sum(UsageDailyRollup.held_util_seconds),
            func.sum(UsageDailyRollup.observed_seconds),
        )
        .where(
            UsageDailyRollup.tenant_id == tenant_id,
            UsageDailyRollup.day >= month_start,
            UsageDailyRollup.day < month_end,
        )
        .group_by(UsageDailyRollup.account_id)
    ).all()
    active_ids = [aid for aid, held, observed in rows if (held or 0) > 0 or (observed or 0) > 0]
    if not active_ids:
        return Decimal("0.00"), "USD"

    totals: dict[str, Decimal] = {}
    for _aid, price, currency in db.execute(
        select(Account.id, Account.monthly_price, Account.currency).where(
            Account.tenant_id == tenant_id, Account.id.in_(active_ids)
        )
    ).all():
        if price is None:
            continue
        totals[currency] = totals.get(currency, Decimal("0.00")) + price
    if not totals:
        return Decimal("0.00"), "USD"
    currency, amount = max(totals.items(), key=lambda kv: (kv[1], kv[0]))
    return amount, currency


def summary(db: Session, tenant_id: uuid.UUID, range_: str, now: datetime) -> Summary:
    start = range_start(range_, now)
    prev_start, prev_end = prev_window(range_, now)

    tokens_value, sessions_value = _session_totals(db, tenant_id, start, now)
    tokens_prev, sessions_prev = _session_totals(db, tenant_id, prev_start, prev_end)
    alerts_value = _alerts_opened_count(db, tenant_id, start, now)
    alerts_prev = _alerts_opened_count(db, tenant_id, prev_start, prev_end)
    alerts_open_now = _alerts_open_now_count(db, tenant_id)

    # cost.value는 당월 합계(usage_cost.compute_month_cost, 기간과 무관, 부록 A) —
    # 이 무거운 함수는 여기서 이 한 번만 부른다. prev(바로 전 달)는 같은 함수를
    # 다시 부르는 대신 _prev_month_cost_estimate의 rollup 기반 어림값을 쓴다.
    this_month = usage_cost.compute_month_cost(db, tenant_id, now.year, now.month)
    cost_value, currency = _dominant_currency_total(this_month.subtotals)
    prev_year, prev_month = _month_before(now.year, now.month)
    cost_prev, prev_currency = _prev_month_cost_estimate(db, tenant_id, prev_year, prev_month)
    # 대표 통화가 다르면(설정 변경 등, 드묾) 이번 달 통화를 우선한다.
    if cost_value == Decimal("0.00") and cost_prev != Decimal("0.00"):
        currency = prev_currency

    servers_online = int(
        db.scalar(
            select(func.count()).select_from(Server).where(Server.tenant_id == tenant_id, Server.status == "online")
        )
        or 0
    )
    accounts_active = int(
        db.scalar(
            select(func.count())
            .select_from(Account)
            .where(Account.tenant_id == tenant_id, Account.status == "assigned")
        )
        or 0
    )

    # 스파크라인은 [start, now]를 12구간으로 균등 분할한다(부록 A). 경계 자체는
    # 응답에 나가지 않고 각 구간의 토큰·세션 합만 나간다.
    spark_tokens = [0] * _SPARKLINE_BUCKETS
    spark_sessions = [0] * _SPARKLINE_BUCKETS
    step_seconds = (now - start).total_seconds() / _SPARKLINE_BUCKETS
    bucket_col = cast(
        func.floor((func.extract("epoch", SessionUsage.ended_at) - start.timestamp()) / step_seconds), Integer
    )
    rows = db.execute(
        select(bucket_col.label("bucket"), func.sum(_TOKEN_SUM), func.count(func.distinct(SessionUsage.session_id)))
        .where(SessionUsage.tenant_id == tenant_id, SessionUsage.ended_at >= start, SessionUsage.ended_at < now)
        .group_by(bucket_col)
    ).all()
    for bucket, tok, sess in rows:
        i = max(0, min(_SPARKLINE_BUCKETS - 1, int(bucket)))
        spark_tokens[i] += int(tok or 0)
        spark_sessions[i] += int(sess or 0)

    return Summary(
        tokens=ValuePrev(value=tokens_value, prev=tokens_prev),
        cost=CostValue(value=cost_value, currency=currency, prev=cost_prev),
        sessions=ValuePrev(value=sessions_value, prev=sessions_prev),
        alerts_opened=ValuePrev(value=alerts_value, prev=alerts_prev),
        alerts_open_now=alerts_open_now,
        servers_online=servers_online,
        accounts_active=accounts_active,
        sparkline=Sparkline(tokens=spark_tokens, sessions=spark_sessions),
    )


def _dominant_currency_total(subtotals: list) -> tuple[Decimal, str]:
    """usage_cost의 통화별 subtotal 중 allocated_cost가 가장 큰 통화 하나를 고른다.
    아무것도 없으면 (0.00, "USD")."""
    if not subtotals:
        return Decimal("0.00"), "USD"
    best = max(subtotals, key=lambda s: (s.allocated_cost, s.currency))
    return best.allocated_cost, best.currency


@dataclass
class Timeseries:
    unit: str
    buckets: list[datetime]
    series: list[SeriesRow]


def _timeseries_server(db: Session, tenant_id: uuid.UUID, start: datetime, now: datetime) -> Timeseries:
    days = day_buckets(start, now)
    day_index = {d: i for i, d in enumerate(days)}
    rows = db.execute(
        select(UsageDailyRollup.day, UsageDailyRollup.server_id, func.sum(UsageDailyRollup.held_util_seconds))
        .where(
            UsageDailyRollup.tenant_id == tenant_id,
            UsageDailyRollup.day >= days[0],
            UsageDailyRollup.day <= days[-1],
        )
        .group_by(UsageDailyRollup.day, UsageDailyRollup.server_id)
    ).all()
    totals: dict[str, float] = {}
    by_bucket: dict[tuple[str, int], float] = {}
    server_ids: set[uuid.UUID] = set()
    for day, server_id, held in rows:
        key = str(server_id)
        server_ids.add(server_id)
        v = float(held or 0)
        totals[key] = totals.get(key, 0.0) + v
        i = day_index[day]
        by_bucket[(key, i)] = by_bucket.get((key, i), 0.0) + v
    # 못 찾은(지워진) 서버는 원본 uuid 대신 자리표시자로 — 먼저 전부 채우고 실제
    # 이름으로 덮어써서, assemble_series의 labels.get(k, k) 폴백이 원본 id로
    # 떨어지는 경우가 없게 한다.
    labels: dict[str, str] = dict.fromkeys(totals, "(삭제된 서버)")
    labels.update(_server_names(db, tenant_id, server_ids))
    series = assemble_series(totals, by_bucket, labels, len(days))
    buckets = [datetime(d.year, d.month, d.day, tzinfo=UTC) for d in days]
    return Timeseries(unit="seconds", buckets=buckets, series=series)


def timeseries(db: Session, tenant_id: uuid.UUID, range_: str, by: str, now: datetime) -> Timeseries:
    start = range_start(range_, now)
    if by == "server":
        return _timeseries_server(db, tenant_id, start, now)
    step = session_bucket_seconds(range_)
    edges = bucket_starts(start, now, step)
    n = len(edges)
    key_col = SessionUsage.model if by == "model" else SessionUsage.account_id
    bucket_col = cast(
        func.floor((func.extract("epoch", SessionUsage.ended_at) - start.timestamp()) / step), Integer
    )
    rows = db.execute(
        select(bucket_col.label("bucket"), key_col, func.sum(_TOKEN_SUM))
        .where(SessionUsage.tenant_id == tenant_id, SessionUsage.ended_at >= start, SessionUsage.ended_at < now)
        .group_by(bucket_col, key_col)
    ).all()
    totals: dict[str, float] = {}
    by_bucket: dict[tuple[str, int], float] = {}
    account_ids: set[uuid.UUID] = set()
    for bucket, raw_key, tok in rows:
        i = max(0, min(n - 1, int(bucket)))
        if by == "account":
            key = "unknown" if raw_key is None else str(raw_key)
            if raw_key is not None:
                account_ids.add(raw_key)
        else:
            key = raw_key or "unknown"
        v = float(tok or 0)
        totals[key] = totals.get(key, 0.0) + v
        by_bucket[(key, i)] = by_bucket.get((key, i), 0.0) + v
    if by == "account":
        # "unknown"(계정 미귀속, raw_key가 애초에 None)과 "삭제된 계정"(계정 id는
        # 있었지만 지금 accounts에 없음)은 다른 개념이라 순서를 지킨다: 전부
        # "삭제된 계정"으로 채운 뒤 실제 이메일로 덮어쓰고, "unknown"만 마지막에
        # "(미상)"으로 되돌린다.
        labels: dict[str, str] = dict.fromkeys(totals, "(삭제된 계정)")
        labels.update(_account_emails(db, tenant_id, account_ids))
    else:
        labels = {k: k for k in totals}
    labels["unknown"] = "(미상)"
    series = assemble_series(totals, by_bucket, labels, n)
    return Timeseries(unit="tokens", buckets=edges, series=series)


def _server_names(db: Session, tenant_id: uuid.UUID, ids: set[uuid.UUID]) -> dict[str, str]:
    if not ids:
        return {}
    rows = db.execute(select(Server.id, Server.name).where(Server.tenant_id == tenant_id, Server.id.in_(ids))).all()
    return {str(i): name for i, name in rows}


def _account_emails(db: Session, tenant_id: uuid.UUID, ids: set[uuid.UUID]) -> dict[str, str]:
    if not ids:
        return {}
    rows = db.execute(
        select(Account.id, Account.email).where(Account.tenant_id == tenant_id, Account.id.in_(ids))
    ).all()
    return {str(i): email for i, email in rows}


@dataclass
class FlowNode:
    id: str
    kind: str
    label: str


@dataclass
class FlowLink:
    source: str
    target: str
    value: float


@dataclass
class Flows:
    nodes: list[FlowNode]
    links: list[FlowLink]


# 삭제된 서버·계정을 모으는 합성 노드 id. 실제 uuid와 겹치지 않는다.
_DELETED_SERVER_NODE = "server:deleted"
_DELETED_ACCOUNT_NODE = "account:deleted"


def flows(db: Session, tenant_id: uuid.UUID, range_: str, now: datetime) -> Flows:
    start = range_start(range_, now)
    days = day_buckets(start, now)
    rows = db.execute(
        select(UsageDailyRollup.server_id, UsageDailyRollup.account_id, func.sum(UsageDailyRollup.held_util_seconds))
        .where(
            UsageDailyRollup.tenant_id == tenant_id,
            UsageDailyRollup.day >= days[0],
            UsageDailyRollup.day <= days[-1],
        )
        .group_by(UsageDailyRollup.server_id, UsageDailyRollup.account_id)
    ).all()
    server_ids = {r[0] for r in rows}
    account_ids = {r[1] for r in rows}
    names = _server_names(db, tenant_id, server_ids)
    emails = _account_emails(db, tenant_id, account_ids)

    # 지금 accounts/servers 테이블에 없는 id는 종류별로 노드 하나에 몰아넣고
    # 링크 값을 합산한다. 각각을 노드로 두면 라벨이 전부 "(삭제된 계정)"으로
    # 같아서, 서로 구별되지 않는 노드만 늘어나고 그래프가 그만큼 빽빽해진다.
    nodes: list[FlowNode] = []
    for sid in sorted(server_ids, key=str):
        if str(sid) in names:
            nodes.append(FlowNode(id=f"server:{sid}", kind="server", label=names[str(sid)]))
    if any(str(sid) not in names for sid in server_ids):
        nodes.append(FlowNode(id=_DELETED_SERVER_NODE, kind="server", label="(삭제된 서버)"))
    for aid in sorted(account_ids, key=str):
        if str(aid) in emails:
            nodes.append(FlowNode(id=f"account:{aid}", kind="account", label=emails[str(aid)]))
    if any(str(aid) not in emails for aid in account_ids):
        nodes.append(FlowNode(id=_DELETED_ACCOUNT_NODE, kind="account", label="(삭제된 계정)"))

    totals: dict[tuple[str, str], float] = {}
    for sid, aid, held in rows:
        if not held:
            continue
        source = f"server:{sid}" if str(sid) in names else _DELETED_SERVER_NODE
        target = f"account:{aid}" if str(aid) in emails else _DELETED_ACCOUNT_NODE
        totals[(source, target)] = totals.get((source, target), 0.0) + float(held)

    links = [FlowLink(source=source, target=target, value=value) for (source, target), value in totals.items()]
    links.sort(key=lambda link: (-link.value, link.source, link.target))
    return Flows(nodes=nodes, links=links)


@dataclass
class AccountRow:
    account_id: uuid.UUID
    email: str | None
    provider: str | None
    tokens: int
    sessions: int
    messages: int
    top_model: str | None
    top_server_id: uuid.UUID | None
    top_server_name: str | None
    top_project: str | None
    held_seconds: float
    remaining_5h_pct: float | None
    remaining_7d_pct: float | None


def accounts(db: Session, tenant_id: uuid.UUID, range_: str, now: datetime) -> list[AccountRow]:
    start = range_start(range_, now)
    days = day_buckets(start, now)

    base_rows = db.execute(
        select(
            SessionUsage.account_id,
            func.sum(_TOKEN_SUM),
            func.count(func.distinct(SessionUsage.session_id)),
            func.sum(SessionUsage.message_count),
        )
        .where(
            SessionUsage.tenant_id == tenant_id,
            SessionUsage.ended_at >= start,
            SessionUsage.ended_at < now,
            SessionUsage.account_id.isnot(None),
        )
        .group_by(SessionUsage.account_id)
        .order_by(func.sum(_TOKEN_SUM).desc())
        .limit(_ACCOUNTS_LIMIT)
    ).all()
    if not base_rows:
        return []
    account_ids = [r[0] for r in base_rows]

    model_rows = db.execute(
        select(SessionUsage.account_id, SessionUsage.model, func.sum(_TOKEN_SUM))
        .where(
            SessionUsage.tenant_id == tenant_id,
            SessionUsage.ended_at >= start,
            SessionUsage.ended_at < now,
            SessionUsage.account_id.in_(account_ids),
        )
        .group_by(SessionUsage.account_id, SessionUsage.model)
    ).all()
    top_model = _pick_top(model_rows, key_of_group=lambda r: r[0], key_of_candidate=lambda r: r[1], value=lambda r: r[2])

    server_rows = db.execute(
        select(SessionUsage.account_id, SessionUsage.server_id, func.sum(_TOKEN_SUM))
        .where(
            SessionUsage.tenant_id == tenant_id,
            SessionUsage.ended_at >= start,
            SessionUsage.ended_at < now,
            SessionUsage.account_id.in_(account_ids),
        )
        .group_by(SessionUsage.account_id, SessionUsage.server_id)
    ).all()
    top_server = _pick_top(server_rows, key_of_group=lambda r: r[0], key_of_candidate=lambda r: r[1], value=lambda r: r[2])
    top_server_ids = {sid for sid in top_server.values() if sid is not None}
    top_server_names = _server_names(db, tenant_id, top_server_ids)

    project_rows = db.execute(
        select(SessionUsage.account_id, SessionUsage.project, func.sum(_TOKEN_SUM))
        .where(
            SessionUsage.tenant_id == tenant_id,
            SessionUsage.ended_at >= start,
            SessionUsage.ended_at < now,
            SessionUsage.account_id.in_(account_ids),
        )
        .group_by(SessionUsage.account_id, SessionUsage.project)
    ).all()
    top_project = _pick_top(project_rows, key_of_group=lambda r: r[0], key_of_candidate=lambda r: r[1], value=lambda r: r[2])

    held_rows = db.execute(
        select(UsageDailyRollup.account_id, func.sum(UsageDailyRollup.held_util_seconds))
        .where(
            UsageDailyRollup.tenant_id == tenant_id,
            UsageDailyRollup.day >= days[0],
            UsageDailyRollup.day <= days[-1],
            UsageDailyRollup.account_id.in_(account_ids),
        )
        .group_by(UsageDailyRollup.account_id)
    ).all()
    held_by_account = {aid: float(held or 0) for aid, held in held_rows}

    meta_rows = db.execute(
        select(Account.id, Account.email, Account.provider).where(
            Account.tenant_id == tenant_id, Account.id.in_(account_ids)
        )
    ).all()
    meta = {r[0]: (r[1], r[2]) for r in meta_rows}

    windows_rows = db.execute(
        select(AccountUsageWindow.account_id, AccountUsageWindow.window_minutes, AccountUsageWindow.pct).where(
            AccountUsageWindow.tenant_id == tenant_id,
            AccountUsageWindow.account_id.in_(account_ids),
            AccountUsageWindow.window_minutes.in_([_FIVE_HOUR_MINUTES, _SEVEN_DAY_MINUTES]),
        )
    ).all()
    windows: dict[uuid.UUID, dict[int, float | None]] = {}
    for aid, minutes, pct in windows_rows:
        windows.setdefault(aid, {})[minutes] = pct

    out = []
    for aid, tokens, sessions, messages in base_rows:
        email, provider = meta.get(aid, (None, None))
        w = windows.get(aid, {})
        out.append(
            AccountRow(
                account_id=aid,
                email=email,
                provider=provider,
                tokens=int(tokens or 0),
                sessions=int(sessions or 0),
                messages=int(messages or 0),
                top_model=top_model.get(aid),
                top_server_id=top_server.get(aid),
                top_server_name=(
                    None
                    if top_server.get(aid) is None
                    else top_server_names.get(str(top_server.get(aid)), "(삭제된 서버)")
                ),
                top_project=top_project.get(aid),
                held_seconds=held_by_account.get(aid, 0.0),
                remaining_5h_pct=w.get(_FIVE_HOUR_MINUTES),
                remaining_7d_pct=w.get(_SEVEN_DAY_MINUTES),
            )
        )
    return out


@dataclass
class ServerRow:
    # None은 "미귀속"(session_usage.server_id가 NULL인 세션들을 한 행으로 합친
    # 것) — 실서버가 아니라 status는 "deleted"를 공유한다(스키마에 별도
    # "unassigned" 상태가 없다).
    server_id: uuid.UUID | None
    name: str
    status: str
    held_seconds: float
    tokens: int
    sessions: int
    messages: int
    top_model: str | None
    top_account_id: uuid.UUID | None
    top_account_email: str | None
    cost_amount: Decimal
    cost_currency: str


_DELETED_SERVER_NAME = "(삭제된 서버)"
_UNASSIGNED_SERVER_NAME = "(미귀속)"


def servers(db: Session, tenant_id: uuid.UUID, range_: str, now: datetime) -> list[ServerRow]:
    """서버별 집계. 행 집합은 지금 존재하는 ``servers``만이 아니라 그 기간에
    활동이 있었던(session_usage·usage_daily_rollup) 서버 id까지 합친 합집합이다
    — 지워진 서버도 지난 활동은 대시보드에 남아야 하기 때문. ``servers``에
    없는 id는 "(삭제된 서버)"/``status="deleted"``로, server_id가 NULL인(훅이
    hostname을 못 넘겼거나 매칭 실패) 세션들은 ``server_id=None``·
    "(미귀속)" 한 행으로 합친다.
    """
    start = range_start(range_, now)
    days = day_buckets(start, now)

    real_servers = {
        r[0]: (r[1], r[2])
        for r in db.execute(
            select(Server.id, Server.name, Server.status).where(Server.tenant_id == tenant_id)
        ).all()
    }
    session_server_ids = set(
        db.execute(
            select(SessionUsage.server_id)
            .where(
                SessionUsage.tenant_id == tenant_id,
                SessionUsage.ended_at >= start,
                SessionUsage.ended_at < now,
            )
            .distinct()
        )
        .scalars()
        .all()
    )
    rollup_server_ids = set(
        db.execute(
            select(UsageDailyRollup.server_id)
            .where(
                UsageDailyRollup.tenant_id == tenant_id,
                UsageDailyRollup.day >= days[0],
                UsageDailyRollup.day <= days[-1],
            )
            .distinct()
        )
        .scalars()
        .all()
    )
    has_unassigned = None in session_server_ids
    session_server_ids.discard(None)

    all_ids: set[uuid.UUID] = set(real_servers) | session_server_ids | rollup_server_ids
    if not all_ids and not has_unassigned:
        return []

    held_rows = db.execute(
        select(UsageDailyRollup.server_id, func.sum(UsageDailyRollup.held_util_seconds))
        .where(
            UsageDailyRollup.tenant_id == tenant_id,
            UsageDailyRollup.day >= days[0],
            UsageDailyRollup.day <= days[-1],
        )
        .group_by(UsageDailyRollup.server_id)
    ).all()
    held_by_server = {sid: float(held or 0) for sid, held in held_rows}

    # server_id가 NULL인 행도 자기 그룹으로 그대로 집계되게 isnot(None) 필터를
    # 걷어냈다 — 미귀속 세션의 토큰·top_model·top_account를 여기서 함께 낸다.
    session_rows = db.execute(
        select(
            SessionUsage.server_id,
            func.sum(_TOKEN_SUM),
            func.count(func.distinct(SessionUsage.session_id)),
            func.sum(SessionUsage.message_count),
        )
        .where(
            SessionUsage.tenant_id == tenant_id,
            SessionUsage.ended_at >= start,
            SessionUsage.ended_at < now,
        )
        .group_by(SessionUsage.server_id)
    ).all()
    session_by_server = {sid: (int(tok or 0), int(sess or 0), int(msg or 0)) for sid, tok, sess, msg in session_rows}

    model_rows = db.execute(
        select(SessionUsage.server_id, SessionUsage.model, func.sum(_TOKEN_SUM))
        .where(
            SessionUsage.tenant_id == tenant_id,
            SessionUsage.ended_at >= start,
            SessionUsage.ended_at < now,
        )
        .group_by(SessionUsage.server_id, SessionUsage.model)
    ).all()
    top_model = _pick_top(model_rows, key_of_group=lambda r: r[0], key_of_candidate=lambda r: r[1], value=lambda r: r[2])

    account_rows = db.execute(
        select(SessionUsage.server_id, SessionUsage.account_id, func.sum(_TOKEN_SUM))
        .where(
            SessionUsage.tenant_id == tenant_id,
            SessionUsage.ended_at >= start,
            SessionUsage.ended_at < now,
        )
        .group_by(SessionUsage.server_id, SessionUsage.account_id)
    ).all()
    top_account = _pick_top(account_rows, key_of_group=lambda r: r[0], key_of_candidate=lambda r: r[1], value=lambda r: r[2])
    account_ids = {aid for aid in top_account.values() if aid is not None}
    emails = _account_emails(db, tenant_id, account_ids)

    month_cost = usage_cost.compute_month_cost(db, tenant_id, now.year, now.month)
    cost_by_server: dict[uuid.UUID | None, dict[str, Decimal]] = {}
    for acc in month_cost.accounts:
        for line in acc.servers:
            bucket = cost_by_server.setdefault(line.server_id, {})
            bucket[acc.currency] = bucket.get(acc.currency, Decimal("0.00")) + line.cost

    def _row_for(sid: uuid.UUID | None) -> ServerRow:
        if sid is None:
            name, status = _UNASSIGNED_SERVER_NAME, "deleted"
        elif sid in real_servers:
            name, status = real_servers[sid]
        else:
            name, status = _DELETED_SERVER_NAME, "deleted"
        tokens, sessions, messages = session_by_server.get(sid, (0, 0, 0))
        by_currency = cost_by_server.get(sid, {})
        if by_currency:
            currency, amount = max(by_currency.items(), key=lambda kv: (kv[1], kv[0]))
        else:
            currency, amount = "USD", Decimal("0.00")
        top_aid = top_account.get(sid)
        return ServerRow(
            server_id=sid,
            name=name,
            status=status,
            held_seconds=held_by_server.get(sid, 0.0),
            tokens=tokens,
            sessions=sessions,
            messages=messages,
            top_model=top_model.get(sid),
            top_account_id=top_aid,
            top_account_email=emails.get(str(top_aid)) if top_aid else None,
            cost_amount=amount,
            cost_currency=currency,
        )

    out = [_row_for(sid) for sid in all_ids]
    if has_unassigned:
        out.append(_row_for(None))
    out.sort(key=lambda r: (-r.held_seconds, str(r.server_id)))
    return out


def heatmap(db: Session, tenant_id: uuid.UUID, range_: str, now: datetime) -> list[list[int]]:
    """7×24 세션 수 격자. 요일 인덱스는 월요일=0(ISO, isodow-1), 시간은 UTC 0~23시."""
    start = range_start(range_, now)
    weekday_col = cast(func.extract("isodow", func.timezone("UTC", SessionUsage.ended_at)), Integer) - 1
    hour_col = cast(func.extract("hour", func.timezone("UTC", SessionUsage.ended_at)), Integer)
    rows = db.execute(
        select(weekday_col.label("wd"), hour_col.label("hr"), func.count(func.distinct(SessionUsage.session_id)))
        .where(SessionUsage.tenant_id == tenant_id, SessionUsage.ended_at >= start, SessionUsage.ended_at < now)
        .group_by(weekday_col, hour_col)
    ).all()
    cells = [[0] * 24 for _ in range(7)]
    for wd, hr, count in rows:
        cells[int(wd)][int(hr)] = int(count or 0)
    return cells
