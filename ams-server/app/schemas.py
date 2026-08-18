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
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field
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
    created_at: datetime


class ServerPage(Wire):
    items: list[Server]
    page_info: PageInfo | None = None


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
