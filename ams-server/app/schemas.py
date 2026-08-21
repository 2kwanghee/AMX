"""Pydantic wire models — shapes come from contracts/openapi.yaml.

Field names are camelCase on the wire, snake_case in Python; `alias_generator`
does the translation so a rename in the contract is a one-line change here.
`secret` is write-only in the contract and therefore appears only on request
models — there is no response model in this file with a field that could carry
credential material.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
)
from pydantic.alias_generators import to_camel

TenantStatus = Literal["active", "suspended"]
AccountStatus = Literal["available", "assigned", "disabled", "quarantined"]
CredentialType = Literal["oauth", "api_key"]
# Response-side mirror of models.PROVIDERS. Requests keep provider as a free
# string (see AccountCreate) so a bad value is a 400, not a 422.
Provider = Literal["claude", "codex"]
ServerStatus = Literal["online", "offline", "degraded"]
SwitchMode = Literal["auto", "manual"]
SwitchStrategy = Literal["best", "next_available"]
AssignmentState = Literal[
    "pending", "delivering", "active", "inactive", "quarantined", "recalling", "detached"
]
# 계정 풀(기획서 §2.1). accounts.status 와 축이 다르다 — 이건 배급 순환의 위치다.
PoolState = Literal["ready", "leased", "recalling", "cooling", "pinned", "held"]
PoolMode = Literal["manual", "auto"]
PoolRecommendationKind = Literal["prefetch", "swap", "recall_idle", "lease"]
PoolChainStep = Literal["deliver", "switch", "recall", "done", "failed"]
PoolEventKind = Literal[
    "state_changed",
    "recommendation_created",
    "chain_started",
    "chain_step",
    "chain_done",
    "chain_failed",
    "policy_changed",
    "automation_paused",
    "automation_resumed",
]


class Wire(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, from_attributes=True
    )


class PageInfo(Wire):
    next_page_token: str | None = None
    total_size: int | None = None


# -- Tenant -------------------------------------------------------------------
class TenantCreate(Wire):
    name: str = Field(min_length=1, max_length=200)


class TenantUpdate(Wire):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    status: TenantStatus | None = None


class Tenant(Wire):
    id: uuid.UUID
    name: str
    status: TenantStatus
    created_at: datetime
    updated_at: datetime | None = None


class TenantPage(Wire):
    items: list[Tenant]
    page_info: PageInfo | None = None


# -- Account ------------------------------------------------------------------
# ISO 4217 alphabetic form only — three uppercase letters. Membership of the
# real code list is not enforced, so a private or newly minted code still
# round-trips.
_CURRENCY_PATTERN = r"^[A-Z]{3}$"


class AccountCreate(Wire):
    email: EmailStr
    # Free string on the wire so an unsupported value yields an explicit 400
    # (account.provider_unsupported) rather than a 422; validated in the API.
    provider: str = "claude"
    credential_type: CredentialType
    secret: str = Field(min_length=1)
    # Free-text label (a person, a team) for the console and for audit; not a
    # reference to an admin.
    owner: str | None = Field(default=None, max_length=200)
    # Subscription price per month; omitted means "no price recorded", which the
    # cost allocation skips. A stored 0 is a real free plan, not an omission.
    monthly_price: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    currency: str = Field(default="USD", pattern=_CURRENCY_PATTERN)
    # Opt-in: an account a person runs directly from their own profile, kept
    # out of new assignments so a server can never race its OAuth refresh.
    assignment_excluded: bool = False


class AccountUpdate(Wire):
    email: EmailStr | None = None
    status: AccountStatus | None = None
    secret: str | None = Field(default=None, min_length=1)
    owner: str | None = Field(default=None, max_length=200)
    monthly_price: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    currency: str | None = Field(default=None, pattern=_CURRENCY_PATTERN)
    # None = leave unchanged. Unlike monthly_price, True/False are the only
    # real values here, so there is no separate "not supplied" state to guard.
    assignment_excluded: bool | None = None


class Account(Wire):
    id: uuid.UUID
    tenant_id: uuid.UUID
    provider: Provider = "claude"
    email: str
    owner: str | None = None
    assignment_excluded: bool = False
    # Serialized by pydantic's Decimal default: a JSON string ("29.00"), which
    # keeps the two stored decimal places exact instead of handing the console a
    # binary float.
    monthly_price: Decimal | None = None
    currency: str = "USD"
    credential_type: CredentialType
    status: AccountStatus
    secret_masked: str | None = None
    account_uuid: str | None = None
    organization_name: str | None = None
    scopes: list[str] | None = None
    credential_expires_at: datetime | None = None
    last_switched_at: datetime | None = None
    # 계정 풀 순환의 위치(기획서 §2.1). `status` 와 독립이며, pin/hold 응답이 이 값을
    # 돌려주므로 계약의 pool 액션들이 별도 스키마 없이 Account 로 답할 수 있다.
    pool_state: PoolState = "ready"
    cooling_until: datetime | None = None
    cooling_window_id: str | None = None
    pool_state_changed_at: datetime | None = None
    last_lease_ended_at: datetime | None = None
    created_at: datetime


class AccountPage(Wire):
    items: list[Account]
    page_info: PageInfo | None = None


class OauthStartRequest(Wire):
    label: str | None = None
    # See AccountCreate.provider — validated in the API for an explicit 400.
    provider: str = "claude"


class OauthStartResponse(Wire):
    flow_id: str
    authorize_url: str
    expires_at: datetime


class OauthCompleteRequest(Wire):
    flow_id: str
    code: str = Field(min_length=1)
    email: EmailStr | None = None
    # See AccountCreate.assignment_excluded.
    assignment_excluded: bool = False


# -- Server -------------------------------------------------------------------
class ServerCreate(Wire):
    name: str = Field(min_length=1)
    hostname: str | None = None
    switch_mode: SwitchMode = "auto"


class ServerUpdate(Wire):
    name: str | None = Field(default=None, min_length=1)
    hostname: str | None = None
    status: ServerStatus | None = None
    # O4-C policy (design note O4-C). Provided fields are written; unset fields
    # are left untouched (detected via model_fields_set). threshold_pct None
    # clears central control back to the tsamx-local default.
    threshold_pct: float | None = Field(default=None, ge=0, le=100)
    default_strategy: SwitchStrategy | None = None
    # F4 (O4-B) full central policy. Ranges mirror tsamx settings validation
    # (cooldown_seconds 0..86400, hysteresis_pct 0..50). A provided None clears
    # central control back to the tsamx-local default; 0 is a real value.
    cooldown_seconds: float | None = Field(default=None, ge=0, le=86400)
    hysteresis_pct: float | None = Field(default=None, ge=0, le=50)


class Server(Wire):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    hostname: str | None = None
    switch_mode: SwitchMode
    threshold_pct: float | None = None
    default_strategy: SwitchStrategy | None = None
    cooldown_seconds: float | None = None
    hysteresis_pct: float | None = None
    status: ServerStatus
    agent_id: str | None = None
    agent_version: str | None = None
    tsamx_version: str | None = None
    enrolled: bool = False
    last_seen_at: datetime | None = None
    # Latest host utilization from the heartbeat (proto §8); NULL until reported.
    cpu_pct: float | None = None
    mem_pct: float | None = None
    disk_pct: float | None = None
    metrics_reported_at: datetime | None = None
    assigned_account_count: int | None = None
    # 계정 풀 슬롯 정책. 저장은 부분 dict 라도 응답은 항상 기본값이 채워진 완전한
    # 형태다(services.pool.resolve_policy) — 콘솔이 "미설정"을 따로 다룰 필요가 없다.
    pool_policy: PoolPolicy | None = None
    created_at: datetime


class ServerPage(Wire):
    items: list[Server]
    page_info: PageInfo | None = None


# -- 계정 풀 (기획서 §2) ------------------------------------------------------
class PoolPolicy(Wire):
    """서버 한 대의 슬롯 정책. 기본값이 곧 "정책 미설정" 의 의미다.

    ``mode=manual`` 이 기본이라 마이그레이션만으로는 어떤 서버도 자동화에 들어오지
    않는다 — "서버당 1계정 귀속"은 manual 또는 ``auto + target_leases=1`` 로 그대로
    표현되므로 별도 기능이 아니다.
    """

    mode: PoolMode = "manual"
    target_leases: int = Field(default=1, ge=1, le=5)
    swap_at_pct: int = Field(default=85, ge=0, le=100)
    prefetch_at_pct: int = Field(default=70, ge=0, le=100)
    min_lease_minutes: int = Field(default=30, ge=0, le=1440)
    ready_return_pct: int = Field(default=20, ge=0, le=100)


class PoolPolicyUpdate(Wire):
    """PATCH 본문 — 준 필드만 저장하고 나머지는 그대로 둔다(ServerUpdate 와 같은 규약)."""

    mode: PoolMode | None = None
    target_leases: int | None = Field(default=None, ge=1, le=5)
    swap_at_pct: int | None = Field(default=None, ge=0, le=100)
    prefetch_at_pct: int | None = Field(default=None, ge=0, le=100)
    min_lease_minutes: int | None = Field(default=None, ge=0, le=1440)
    ready_return_pct: int | None = Field(default=None, ge=0, le=100)


class WindowState(Wire):
    window_id: str
    pct: float
    resets_at: datetime | None = None
    usage_fetched_at: datetime | None = None
    reported_at: datetime
    server_id: uuid.UUID


class PoolAccount(Wire):
    account_id: uuid.UUID
    email: str
    provider: Provider = "claude"
    pool_state: PoolState
    cooling_until: datetime | None = None
    cooling_window_id: str | None = None
    leased_server_id: uuid.UUID | None = None
    lease_started_at: datetime | None = None
    last_lease_ended_at: datetime | None = None
    windows: list[WindowState] = Field(default_factory=list)
    pool_state_changed_at: datetime | None = None


class PoolServer(Wire):
    server_id: uuid.UUID
    name: str
    status: ServerStatus
    pool_policy: PoolPolicy
    leased_account_ids: list[uuid.UUID] = Field(default_factory=list)
    active_account_id: uuid.UUID | None = None
    # 전달이 진행 중인 배정이 하나라도 있으면 True — 이 서버에는 권고가 생기지 않는다.
    in_flight: bool = False
    max_pct: float | None = None


class PoolRecommendation(Wire):
    id: uuid.UUID
    server_id: uuid.UUID
    kind: PoolRecommendationKind
    from_account_id: uuid.UUID | None = None
    to_account_id: uuid.UUID | None = None
    reason: str
    created_at: datetime
    trigger_pct: float | None = None


class PoolChain(Wire):
    id: uuid.UUID
    server_id: uuid.UUID
    recommendation_id: uuid.UUID | None = None
    from_account_id: uuid.UUID | None = None
    to_account_id: uuid.UUID | None = None
    step: PoolChainStep
    error: str | None = None
    started_at: datetime
    updated_at: datetime
    actor: str


class PoolEvent(Wire):
    id: uuid.UUID
    kind: PoolEventKind
    account_id: uuid.UUID | None = None
    server_id: uuid.UUID | None = None
    detail: dict = Field(default_factory=dict)
    created_at: datetime
    actor: str


class PoolOverview(Wire):
    automation_paused: bool = False
    accounts: list[PoolAccount] = Field(default_factory=list)
    servers: list[PoolServer] = Field(default_factory=list)
    recommendations: list[PoolRecommendation] = Field(default_factory=list)


# `Server.pool_policy` 는 여기보다 위에서 선언되므로(파일 순서상 PoolPolicy 가 뒤),
# 전방 참조를 지금 해소한다. 늦은 rebuild 에 기대면 첫 응답에서 터진다.
Server.model_rebuild()


class EnrollTokenRequest(Wire):
    ttl_seconds: int = Field(default=3600, ge=60, le=604800)


class EnrollTokenResponse(Wire):
    token: str
    expires_at: datetime
    ams_endpoint: str | None = None
    # Standard-base64 Ed25519 public key the agent pins (--pubkey). NULL when the
    # server has no signing key configured to derive it from.
    ams_pubkey: str | None = None


class SwitchModeRequest(Wire):
    mode: SwitchMode


class SelfUpdateRequest(Wire):
    """Body of ``POST /servers/{id}:self-update``.

    Only a commit pin, and nothing that could name a source: the agent rebuilds
    from the clone it was configured with (see proto ``SelfUpdate``). Empty means
    "advance to the upstream tip". The pattern keeps a typo from reaching the
    agent as a mismatch that aborts the update after a pull."""

    expected_commit: str = Field(default="", pattern=r"^([0-9a-fA-F]{7,40})?$")


class SelfUpdateStatus(Wire):
    """Read model for the latest self_update command of one server.

    Projected from the most recent ``agent_commands`` row (command_type
    ``self_update``); every field is null when the server has never been asked
    to self-update. ``status`` follows the outbox lifecycle
    (queued -> sent -> acked/failed); ``detail`` carries the failure error_code
    when ``status`` is ``failed``."""

    status: Literal["queued", "sent", "acked", "failed"] | None = None
    detail: str | None = None
    created_at: datetime | None = None
    sent_at: datetime | None = None
    acked_at: datetime | None = None


# -- Assignment ---------------------------------------------------------------
class AssignmentCreate(Wire):
    account_id: uuid.UUID
    server_id: uuid.UUID
    pinned: bool = False
    deliver_immediately: bool = False


class AssignmentUpdate(Wire):
    pinned: bool | None = None


class Assignment(Wire):
    id: uuid.UUID
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    server_id: uuid.UUID
    state: AssignmentState
    pinned: bool = False
    delivered_at: datetime | None = None
    acked_at: datetime | None = None
    last_error: str | None = None
    pending_command_id: str | None = None


class AssignmentPage(Wire):
    items: list[Assignment]
    page_info: PageInfo | None = None


class SwitchNowRequest(Wire):
    strategy: Literal["best", "next_available"] | None = None


class RecallRequest(Wire):
    # D1 operator escape hatch: past the retry cap the plain :recall 409s. A
    # global-admin may set force=true to bypass the cap and re-arm a permanently
    # stranded recall (the route enforces the global-admin gate; the service
    # resets recall_retry_count to 0).
    force: bool = False


class UsageSnapshot(Wire):
    id: uuid.UUID
    server_id: uuid.UUID
    account_id: uuid.UUID | None = None
    report_type: Literal["usage", "switch_event"]
    reported_at: datetime
    payload: dict


# -- Alert --------------------------------------------------------------------
# models.ALERT_KINDS와 반드시 일치할 것 — 여기만 빠지면 해당 kind 행이 존재하는
# 순간 알림 목록 전체가 response validation 500으로 죽는다.
AlertKind = Literal[
    "all_exhausted",
    "drift",
    "server_offline",
    "quarantine",
    "recall_failed",
    "command_send_failed",
    "self_update_failed",
    "billing_watermark_future",
    "langfuse_usage_spike",
    "langfuse_stale",
    "langfuse_latency",
    "alert_webhook_dropped",
    "dangerous_command",
    "credential_unusable",
    "account_window_high",
]
AlertSeverity = Literal["critical", "warning"]
AlertStatus = Literal["open", "acked", "resolved"]


class Alert(Wire):
    id: uuid.UUID
    tenant_id: uuid.UUID
    server_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    kind: AlertKind
    severity: AlertSeverity
    status: AlertStatus
    dedupe_key: str
    detail: dict | None = None
    source_snapshot_id: uuid.UUID | None = None
    created_at: datetime
    acked_at: datetime | None = None
    acked_by: str | None = None
    resolved_at: datetime | None = None


class AlertPage(Wire):
    items: list[Alert]
    page_info: PageInfo | None = None


class AlertAckRequest(Wire):
    acked_by: str | None = Field(default=None, max_length=200)


# -- Danger-command ingest (P5, danger_hook.py 발) ----------------------------
# 훅이 보내는 통보 본문. 원문 명령은 절대 담기지 않는다 — sha256 다이제스트와
# 패턴에 매치된 부분만 남긴 마스킹본(최대 200자)만 온다. 무인 에이전트 발이라
# TenantScope가 아니라 정적 토큰(X-AMX-Ingest-Token)으로만 인증한다.
class DangerCommandIngest(Wire):
    pattern_name: str = Field(min_length=1, max_length=64)
    command_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    command_masked: str = Field(min_length=1, max_length=200)
    session_id: str | None = Field(default=None, max_length=200)
    cwd: str | None = Field(default=None, max_length=1024)
    hostname: str = Field(min_length=1, max_length=253)
    user_id: str | None = Field(default=None, max_length=320)
    ts: str | None = Field(default=None, max_length=64)


class DangerCommandIngestAck(Wire):
    accepted: bool = True


# -- Session cost-structure ingest (session_usage_hook.py 발) ------------------
# Stop 훅이 세션 트랜스크립트의 ``message.usage``만 모델별로 집계해 보낸다. 프롬프트·
# 응답·툴 입출력 원문은 어떤 필드에도 담기지 않는다 — 이 스키마에는 원문을 담을 수 있는
# 자유 텍스트 필드가 세션 id·모델명·계정 이메일·호스트·cwd뿐이고 모두 길이 상한이 있다.
# danger 수신과 같은 무인 경로(정적 토큰, TenantScope 아님).

# 카운트 맵({티어: 횟수}, {stop_reason: 횟수})의 상한. 무자격 도달 가능 경로라 키 개수·
# 키 길이·값 범위를 모두 묶는다 — 상한 없는 dict는 JSONB에 임의 문자열을 적재하는 통로다.
_COUNT_MAP_MAX_KEYS = 20
# 실측 키는 stop_sequence(13자)가 최장이다. 훅도 같은 상한에서 초과분을 하나의 버킷으로
# 접으므로(session_usage_hook._MAX_COUNT_KEY_CHARS), 두 쪽 상한이 어긋나 정상 보고가
# 422가 되는 일은 없다.
_COUNT_MAP_MAX_KEY_LEN = 32
# 값 상한은 토큰 필드(_Count)와 같다. 상한이 없으면 {"end_turn": 10**40} 이 그대로 JSONB에
# 들어가고, 콘솔이 클라이언트에서 합산하므로 JS 정수 정밀도가 무너진다.
_COUNT_MAP_MAX_VALUE = 2**53


def _check_count_map(v: dict[str, int]) -> dict[str, int]:
    if len(v) > _COUNT_MAP_MAX_KEYS:
        raise ValueError(f"at most {_COUNT_MAP_MAX_KEYS} keys")
    for key, count in v.items():
        if not key or len(key) > _COUNT_MAP_MAX_KEY_LEN:
            raise ValueError(f"key length must be 1..{_COUNT_MAP_MAX_KEY_LEN}")
        if count < 0 or count > _COUNT_MAP_MAX_VALUE:
            raise ValueError(f"counts must be 0..{_COUNT_MAP_MAX_VALUE}")
    return v


CountMap = Annotated[dict[str, int], AfterValidator(_check_count_map)]
# 토큰 카운터 공통 제약: 음수 없음, 상한은 int64 안(비정상 큰 값은 422로 거른다).
_Count = Annotated[int, Field(ge=0, le=2**53)]


class SessionUsageModelStat(Wire):
    """한 세션 안에서 **모델 하나**가 쓴 비용 구조. 세션은 모델을 섞는다(주 모델 + 서브에이전트)."""

    model: str = Field(min_length=1, max_length=200)
    input_tokens: _Count = 0
    output_tokens: _Count = 0
    cache_read_tokens: _Count = 0
    # 이 둘이 이 경로의 존재 이유다 — 1시간/5분 캐시 쓰기는 가격이 다르다.
    cache_create_1h_tokens: _Count = 0
    cache_create_5m_tokens: _Count = 0
    # output_tokens의 부분집합(추가 항목이 아니다).
    thinking_tokens: _Count = 0
    web_search_requests: _Count = 0
    web_fetch_requests: _Count = 0
    # provider message id로 중복 제거한 assistant 메시지 수(트랜스크립트는 한 응답을
    # content 블록마다 한 줄씩 반복해 적으므로 줄 수를 그대로 쓰면 이중 계산된다).
    message_count: _Count = 0
    service_tier_counts: CountMap = Field(default_factory=dict)
    stop_reason_counts: CountMap = Field(default_factory=dict)
    started_at: datetime | None = None
    ended_at: datetime | None = None


class SessionUsageIngest(Wire):
    session_id: str = Field(min_length=1, max_length=200)
    # tsamx가 보고한 활성 계정 이메일. 훅 조회가 실패하면 없이 온다 — 그래도 세션
    # 단위 데이터는 유효하므로 서버는 account_id를 NULL로 두고 받아들인다.
    account_email: str | None = Field(default=None, max_length=320)
    # hostname·cwd는 받되 **저장하지 않는다**: 수집 대상은 비용 구조 축뿐이라
    # session_usage에 대응 컬럼이 없다. 훅이 이미 보내는 값이라 거부하면 422가 되므로
    # 받아서 무시하고, 길이 상한만 걸어 둔다(나중에 컬럼이 필요해지면 스키마 변경 없이
    # 마이그레이션만 추가하면 된다).
    hostname: str | None = Field(default=None, max_length=253)
    cwd: str | None = Field(default=None, max_length=1024)
    # 훅이 읽기 상한(줄 수·바이트·레코드당 iterations)에 걸려 일부를 버렸으면 True.
    # 세션 단위 사실이라 그 세션의 모든 모델 행에 같은 값이 들어간다.
    truncated: bool = False
    # 한 세션의 모델 수는 실측 3~5개 수준이다. 상한은 폭주 방지용.
    models: list[SessionUsageModelStat] = Field(min_length=1, max_length=50)


class SessionUsageIngestAck(Wire):
    accepted: bool = True
    # 페이로드에서 upsert된 (session, model) 행 수.
    rows: int = 0
    # 이메일이 이 테넌트의 계정과 매칭됐는지. 훅은 쓰지 않지만 운영자가 귀속 실패를
    # 눈으로 확인할 수 있게 돌려준다(이메일 자체는 되돌려주지 않는다).
    account_resolved: bool = False


class SessionUsageRow(Wire):
    session_id: str
    model: str
    account_email: str | None = None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_create_1h_tokens: int
    cache_create_5m_tokens: int
    thinking_tokens: int
    web_search_requests: int
    web_fetch_requests: int
    message_count: int
    service_tier_counts: dict[str, int]
    stop_reason_counts: dict[str, int]
    # 부분 집계 표시(훅이 읽기 상한에 걸린 세션). 콘솔이 그 행에 표시한다.
    truncated: bool = False
    started_at: datetime | None = None
    ended_at: datetime | None = None


class SessionUsageResponse(Wire):
    rows: list[SessionUsageRow]
    # 가장 최근 보고 시각(행 없으면 null) — 콘솔이 "아직 수집 없음"을 구분하는 신호.
    last_reported_at: datetime | None = None


class EventPage(Wire):
    items: list[UsageSnapshot]
    page_info: PageInfo | None = None


# -- Admin audit log (console-test gap G53) -----------------------------------
class AuditLog(Wire):
    id: uuid.UUID
    admin_email: str | None = None
    method: str
    path: str
    action: str
    target_id: uuid.UUID | None = None
    status_code: int
    created_at: datetime


class AuditLogPage(Wire):
    items: list[AuditLog]
    page_info: PageInfo | None = None


# -- F5 billing ---------------------------------------------------------------
BillingStatus = Literal["pending", "exported"]


class BillingEvent(Wire):
    id: uuid.UUID
    tenant_id: uuid.UUID
    kind: str
    period_start: datetime
    period_end: datetime
    status: BillingStatus
    payload: dict
    exported_at: datetime | None = None
    created_at: datetime


class BillingEventPage(Wire):
    items: list[BillingEvent]
    page_info: PageInfo | None = None


# -- F1 RBAC (P5 S2a) ---------------------------------------------------------
class LoginRequest(Wire):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)


class LoginResponse(Wire):
    session_token: str
    role: Literal["global-admin", "tenant-admin"]
    tenant_ids: list[str]
    expires_at: datetime


# -- Admin management (F1 RBAC, S2b) ------------------------------------------
AdminRole = Literal["global-admin", "tenant-admin"]


class AdminCreate(Wire):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)
    role: AdminRole
    tenant_id: uuid.UUID | None = None


class AdminUpdate(Wire):
    # Deliberately narrow (F1 RBAC §7): only account state (disabled) and a
    # password reset are mutable here. Changing role/tenant_id would move an
    # admin across the isolation boundary, so it is not offered — delete and
    # recreate instead.
    disabled: bool | None = None
    password: str | None = Field(default=None, min_length=1, max_length=1024)


class Admin(Wire):
    # Response model. There is no `password_hash` field, so the bcrypt hash can
    # never be serialised out, whatever the ORM row carries.
    id: uuid.UUID
    email: str
    role: AdminRole
    tenant_id: uuid.UUID | None = None
    disabled: bool
    created_at: datetime
    updated_at: datetime | None = None


class AdminPage(Wire):
    items: list[Admin]
    page_info: PageInfo | None = None


# -- Usage cost (usage-cost PR3) ----------------------------------------------
# Response shape for GET /tenants/{id}/usage/cost. The service
# (services/usage_cost.compute_month_cost) answers account-first; the wire is
# server-first, because the console reads per-server. Money keeps its Decimal
# type and therefore serialises as a JSON string, exact to the cent.
class UsageCostAccountLine(Wire):
    account_id: uuid.UUID
    email: str | None = None
    provider: str | None = None
    monthly_price: Decimal | None = None
    currency: str
    # "held" | "observed" | "unallocated" | "no_price" — how the account's price
    # was placed (see services/usage_cost). "no_price"/"unallocated" lines carry
    # cost 0; the unallocated money shows up in the subtotal, not here.
    basis: str
    # Mean utilization this account held on this server over the observed time,
    # i.e. held_util_seconds / observed_seconds. 0 when nothing was observed.
    utilization_pct: Decimal
    # This server's share of the account's allocation basis, in percent (0..100).
    share_pct: Decimal
    cost: Decimal


class UsageCostAmount(Wire):
    currency: str
    amount: Decimal


class UsageCostServerLine(Wire):
    server_id: uuid.UUID
    name: str | None = None
    utilization_pct: Decimal
    # Per-currency, never a single number: one server can host accounts priced
    # in different currencies, and those are never summed.
    costs: list[UsageCostAmount]
    accounts: list[UsageCostAccountLine]


class UsageCostSubtotal(Wire):
    currency: str
    allocated_cost: Decimal
    # Price of accounts that were observed but could not be placed on any
    # server (no held and no observed time to weight by).
    unallocated_cost: Decimal


class UsageCostResponse(Wire):
    month: str  # YYYY-MM
    as_of: datetime
    # Days strictly before this UTC date are sealed (immutable rollup); null
    # when the rollup sweep has not run yet.
    watermark: date | None = None
    # True when the figure still includes an unsealed live tail and can move.
    is_partial: bool
    servers: list[UsageCostServerLine]
    subtotals: list[UsageCostSubtotal]


# -- Langfuse usage (P4 console monitoring) -----------------------------------
class LangfuseModelRow(Wire):
    day: date
    model: str  # providedModelName, or "unknown" when Langfuse reports it null
    input_tokens: int
    output_tokens: int
    # Always 0 today: the Metrics API exposes no cache-token measure.
    cache_read_tokens: int
    cache_creation_tokens: int
    total_tokens: int
    observations: int


class LangfuseUserRow(Wire):
    day: date
    user_id: str  # the account email fixed as the Metrics API userId filter
    total_tokens: int
    observations: int


class LangfuseUsageResponse(Wire):
    # ``model_rows`` collides with Pydantic's ``model_`` protected namespace; the
    # field is a domain name (rows grouped by model), so the guard is cleared here.
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        protected_namespaces=(),
    )

    model_rows: list[LangfuseModelRow]
    user_rows: list[LangfuseUserRow]
    # Console deep-link base (AMX_LANGFUSE_UI_URL, else the API base, else null).
    ui_url: str | None = None
    # Freshness: newest roll-up updated_at for this tenant; null before any sync.
    last_synced_at: datetime | None = None
