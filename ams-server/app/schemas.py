"""Pydantic wire models — shapes come from contracts/openapi.yaml.

Field names are camelCase on the wire, snake_case in Python; `alias_generator`
does the translation so a rename in the contract is a one-line change here.
`secret` is write-only in the contract and therefore appears only on request
models — there is no response model in this file with a field that could carry
credential material.
"""

from __future__ import annotations

import uuid
from datetime import datetime
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


class AccountUpdate(Wire):
    email: EmailStr | None = None
    status: AccountStatus | None = None
    secret: str | None = Field(default=None, min_length=1)
    owner: str | None = Field(default=None, max_length=200)


class Account(Wire):
    id: uuid.UUID
    tenant_id: uuid.UUID
    provider: Provider = "claude"
    email: str
    owner: str | None = None
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


class EventPage(Wire):
    items: list[UsageSnapshot]
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
