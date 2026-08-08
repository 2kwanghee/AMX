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
    credential_type: CredentialType
    secret: str = Field(min_length=1)


class AccountUpdate(Wire):
    email: EmailStr | None = None
    status: AccountStatus | None = None
    secret: str | None = Field(default=None, min_length=1)


class Account(Wire):
    id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
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


class Server(Wire):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    hostname: str | None = None
    switch_mode: SwitchMode
    threshold_pct: float | None = None
    default_strategy: SwitchStrategy | None = None
    status: ServerStatus
    agent_id: str | None = None
    agent_version: str | None = None
    tsamx_version: str | None = None
    enrolled: bool = False
    last_seen_at: datetime | None = None
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


class UsageSnapshot(Wire):
    id: uuid.UUID
    server_id: uuid.UUID
    account_id: uuid.UUID | None = None
    report_type: Literal["usage", "switch_event"]
    reported_at: datetime
    payload: dict
