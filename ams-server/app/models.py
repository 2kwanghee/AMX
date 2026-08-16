"""SQLAlchemy models — docs/AMX-DESIGN.md §5.1.

The tenant boundary is structural. `accounts` and `servers` each carry a
`UNIQUE (id, tenant_id)` anchor, and `assignments` reaches both through
composite foreign keys that include its own `tenant_id`. An assignment row has
exactly one tenant_id, so an account and a server from different tenants can
never satisfy both keys at once and the INSERT dies in the database, with no
application code involved.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TENANT_STATUSES = ("active", "suspended")
ACCOUNT_STATUSES = ("available", "assigned", "disabled", "quarantined")
CREDENTIAL_TYPES = ("oauth", "api_key")
SERVER_STATUSES = ("online", "offline", "degraded")
SWITCH_MODES = ("auto", "manual")
ASSIGNMENT_STATES = (
    "pending",
    "delivering",
    "active",
    "inactive",
    "quarantined",
    "recalling",
    "detached",
)
COMMAND_TYPES = (
    "deliver",
    "recall",
    "activate",
    "deactivate",
    "set_mode",
    "switch_now",
    "req_report",
    "set_policy",
    "self_update",
)
COMMAND_STATUSES = ("queued", "sent", "acked", "failed")
SWITCH_STRATEGIES = ("best", "next_available")
ALERT_KINDS = (
    "all_exhausted",
    "drift",
    "server_offline",
    "quarantine",
    "recall_failed",
    "command_send_failed",
    "self_update_failed",
    "billing_watermark_future",
    # P5 Langfuse 실측 임계값 경보(langfuse_alerts 스윕, 시스템 범위·server_id NULL).
    "langfuse_usage_spike",
    "langfuse_stale",
    "langfuse_latency",
    # 웹훅 발송이 재시도 상한을 넘겨 폐기될 때 여는 셀프 경보(관측용, system 범위).
    "alert_webhook_dropped",
    # P5 위험명령 감지(danger_hook.py 발). system 범위·server_id NULL, auto-resolve
    # 없음(이벤트성 — 관리자가 ack/resolve). dedupe는 host+pattern+명령 sha256 기반.
    "dangerous_command",
)
ALERT_SEVERITIES = ("critical", "warning")
ALERT_STATUSES = ("open", "acked", "resolved")
ADMIN_ROLES = ("global-admin", "tenant-admin")


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TenantDek(Base):
    """Per-tenant data-encryption key, wrapped by a KEK provider (F2, §5.1).

    Each row is one version of a tenant's DEK. The DEK never appears in
    plaintext at rest: ``wrapped_dek`` is the DEK sealed by the KEK provider
    named in ``kek_provider`` (local AES-256-GCM MVP, or a KMS once a vendor is
    chosen), with the tenant_id bound as AAD so a wrapped DEK cannot be
    unwrapped under another tenant. The active DEK is the highest ``version``
    whose ``retired_at`` is NULL; older versions are retained so ciphertext
    written under them (v2 tag carries the version) still opens after rotation.
    """

    __tablename__ = "tenant_deks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "version", name="uq_tenant_deks_tenant_version"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    kek_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    kek_key_id: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm: Mapped[str] = mapped_column(
        String(32), nullable=False, default="AES-256-GCM"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Admin(Base):
    """A human administrator (F1 RBAC, §5.1).

    `tenant_id` is the isolation anchor: a `tenant-admin` carries exactly one
    non-null tenant_id and reaches only that tenant; a `global-admin` carries
    NULL and reaches every tenant. The CHECK constraint makes the two roles the
    only representable shapes, so a row can never be a tenant-admin with no
    tenant (unbounded) nor a global-admin pinned to one tenant. The FK is
    RESTRICT so a tenant with live admins cannot be deleted out from under them.
    """

    __tablename__ = "admins"
    __table_args__ = (
        # Case-insensitive uniqueness: login normalises email to lower() before
        # lookup, so the DB must reject `A@x`/`a@x` as the same principal.
        Index("uq_admins_email_lower", text("lower(email)"), unique=True),
        CheckConstraint(
            "(role = 'global-admin' AND tenant_id IS NULL) OR "
            "(role = 'tenant-admin' AND tenant_id IS NOT NULL)",
            name="ck_admins_role_tenant",
        ),
        CheckConstraint(
            "role IN ('global-admin','tenant-admin')", name="ck_admins_role"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(Text, nullable=False)
    # bcrypt hash of a sha256+base64 pre-hash of the password (never the raw
    # password; the pre-hash sidesteps bcrypt's 72-byte truncation). No endpoint
    # or log ever returns this.
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=True
    )
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AdminSession(Base):
    """An opaque bearer session issued by `/auth/login` (§3).

    Only the hash of the token is stored, so a database disclosure does not hand
    out live sessions. CASCADE from `admins` means disabling-then-deleting an
    admin removes their sessions with them; expiry is enforced in the query
    (`expires_at > now`), not by a sweeper.
    """

    __tablename__ = "admin_sessions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    admin_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admins.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


# Known account providers. "claude" enrolls either by credential import or by
# the central OAuth flow; "codex" is import-only (there is no OAuth profile for
# it — see oauth_enroll.OAUTH_PROFILES) and its credential is a Codex
# `auth.json`, validated in inventory._validate_codex_secret.
PROVIDERS = ("claude", "codex")


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        # ★ isolation anchor referenced by assignments' composite FK.
        UniqueConstraint("id", "tenant_id", name="uq_accounts_id_tenant"),
        UniqueConstraint("tenant_id", "provider", "email", name="uq_accounts_tenant_email"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False, server_default="claude")
    email: Mapped[str] = mapped_column(Text, nullable=False)
    credential_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Fernet ciphertext of the complete credential-set JSON (§5.5). Never
    # returned by any endpoint, never logged.
    encrypted_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Non-reversible display hint only; derived at write time.
    secret_masked: Mapped[str | None] = mapped_column(Text, nullable=True)
    account_uuid: Mapped[str | None] = mapped_column(Text, nullable=True)
    organization_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Free-text label for whoever the account belongs to, for the console and
    # for audit. Deliberately not a foreign key to admins: the owner is often a
    # person or team with no login here, and an FK would make deleting that
    # admin either fail or rewrite history.
    owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Subscription price of this account per month, in `currency`. NULL means
    # "no price recorded" — such an account carries no cost to spread and is
    # skipped by the allocation, which is why it stays nullable rather than
    # defaulting to 0 (a real 0 would be a genuinely free plan).
    monthly_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    # ISO 4217 alphabetic code for monthly_price. NOT NULL with a 'USD' default
    # so the amount is never stored without its unit; only the format is
    # enforced (3 uppercase letters), not membership of the real code list.
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="USD")
    scopes: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    credential_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="available")
    # O9 credential re-sync monotonicity (§5.7). The agent's local observation
    # time of the last accepted refresh. An upstream CredentialUpdate only wins
    # when its observed_at is strictly newer (or this is NULL), so a delayed or
    # duplicated re-sync cannot roll encrypted_secret back.
    credential_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_switched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Server(Base):
    __tablename__ = "servers"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_servers_id_tenant"),
        UniqueConstraint("tenant_id", "name", name="uq_servers_tenant_name"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    hostname: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Only the hash of the one-shot enrollment token is stored (§7).
    enroll_token_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    enroll_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    server_cred_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    tsamx_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    switch_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    # O4-C hybrid switching policy (design note O4-C). NULL means "not centrally
    # set" — AMS delivers no value and the tsamx-local default stays in force.
    # Re-asserted every session via SetPolicy (proto cmd 17); AMA keeps it in
    # memory only, so a restart recovers it from here, not from an agent sidecar.
    threshold_pct: Mapped[float | None] = mapped_column(nullable=True)
    default_strategy: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # F4 (O4-B) full central policy. NULL means "not centrally set" — AMS delivers
    # the negative "unset" sentinel and the tsamx-local default stays in force. A
    # stored 0 is a real value (e.g. cooldown_seconds=0 disables the cooldown),
    # unlike threshold_pct where 0 itself means "unset" (proto SetPolicy 1 vs 3/4).
    cooldown_seconds: Mapped[float | None] = mapped_column(nullable=True)
    hysteresis_pct: Mapped[float | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="offline")
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Latest host utilization from the heartbeat's SystemMetrics (proto §8), each
    # 0..100. NULL until a metrics-bearing heartbeat arrives; a heartbeat without
    # the field (old agent / non-Linux / failed sample) leaves these untouched, so
    # NULL means "never reported", never "reported 0%". metrics_reported_at is the
    # freshness stamp for the trio.
    cpu_pct: Mapped[float | None] = mapped_column(nullable=True)
    mem_pct: Mapped[float | None] = mapped_column(nullable=True)
    disk_pct: Mapped[float | None] = mapped_column(nullable=True)
    metrics_reported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "tenant_id"],
            ["accounts.id", "accounts.tenant_id"],
            name="fk_assignments_account_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["server_id", "tenant_id"],
            ["servers.id", "servers.tenant_id"],
            name="fk_assignments_server_tenant",
            ondelete="RESTRICT",
        ),
        # One account is installed on at most one server at a time; detached
        # rows are audit history and are exempt.
        Index(
            "uq_assignments_active_account",
            "tenant_id",
            "account_id",
            unique=True,
            postgresql_where=text("state <> 'detached'"),
        ),
        Index("ix_assignments_server", "tenant_id", "server_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    server_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    pending_command_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # D1 recall-failure recovery (recovery-architecture §1): how many times a
    # failed/stranded recall (settled ``recalling``, pending_command_id NULL) has
    # been manually re-armed via REST ``:recall``. Capped by AMX_MAX_RECALL_RETRIES
    # so a permanently-failing recall cannot be re-issued forever; reset to 0 when
    # the recall finally converges (detached) or a fresh recall cycle begins.
    recall_retry_count: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AgentCommand(Base):
    """Command outbox (design note §2).

    REST transition actions INSERT a row here; the gRPC session process polls
    for ``status='queued'`` rows belonging to an online server, signs and pushes
    the command, then marks it ``acked``/``failed`` on the agent's CommandAck.

    The tenant boundary is structural, exactly as for the other tables: the row
    reaches ``servers`` through the composite ``(server_id, tenant_id)`` foreign
    key, so a command can never name a server in another tenant. ``command_id``
    is globally unique and is the idempotency key carried on the wire (§6.3).
    """

    __tablename__ = "agent_commands"
    __table_args__ = (
        ForeignKeyConstraint(
            ["server_id", "tenant_id"],
            ["servers.id", "servers.tenant_id"],
            name="fk_agent_commands_server_tenant",
            ondelete="CASCADE",
        ),
        UniqueConstraint("command_id", name="uq_agent_commands_command_id"),
        Index("ix_agent_commands_dispatch", "server_id", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    server_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    # Nullable: server-scoped commands (e.g. set_mode) name no assignment. The
    # account-scoped commands this phase issues always set it. No composite FK to
    # assignments — that table has no (id, tenant_id) anchor — so the tenant tie
    # is carried by server_id above and the service-layer tenant re-check.
    assignment_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    command_id: Mapped[str] = mapped_column(Text, nullable=False)
    command_type: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # D2 sent-未ack recovery (recovery-architecture §2): how many times the poll
    # loop has pushed this command. The sent-timeout sweeper re-queues a stuck
    # ``sent`` command (same command_id, idempotent) and increments this; once it
    # reaches MAX_SEND_ATTEMPTS the command is failed and its assignment reverted.
    send_attempts: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UsageSnapshot(Base):
    __tablename__ = "usage_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["server_id", "tenant_id"],
            ["servers.id", "servers.tenant_id"],
            name="fk_usage_snapshots_server_tenant",
            ondelete="CASCADE",
        ),
        Index("ix_usage_snapshots_server_time", "server_id", "reported_at"),
        # Usage-cost rollup sweep + live-tail integration scan by tenant +
        # report_type over a reported_at day range (migration 0018).
        Index(
            "ix_usage_snapshots_tenant_type_time",
            "tenant_id", "report_type", "reported_at",
        ),
        # Retention sweep (usage_cost.sweep_snapshot_retention) deletes by the
        # exact predicate ``report_type = 'usage' AND reported_at < X`` — no
        # tenant_id, so the composite above (tenant_id-leading) cannot serve it
        # and a large first purge falls back to a seq-scan. A PARTIAL index is
        # chosen over a plain ``(report_type, reported_at)`` composite because the
        # sweep's report_type filter is a constant equality: folding it into the
        # index WHERE clause leaves reported_at as the sole ordered key (a tight
        # range scan) and keeps the index to usage rows only — smaller than a
        # composite that would also carry switch_event rows the sweep never touches.
        # migration 0020.
        Index(
            "ix_usage_snapshots_usage_reported_at",
            "reported_at",
            postgresql_where=text("report_type = 'usage'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    server_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    report_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Reconcile-on-report drift marker (design note §5). NULL = no drift found on
    # this report; otherwise a list of {assignment_id, expected, actual,
    # correction, corrected} entries recorded by reconcile_from_report.
    drift: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UsageDailyRollup(Base):
    """Per-(tenant, UTC day, server, account) usage aggregate — cost-allocation input.

    A compaction of the ``usage_snapshots`` ledger: the raw snapshots stay the
    source of truth, this table holds only what spreading an account's
    subscription price across the servers that used it needs. The composite
    primary key ``(tenant_id, day, server_id, account_id)`` is the idempotency
    anchor — recomputing a day upserts in place instead of duplicating it.

    ``account_id`` is a UUID because the reports' ``ams_account_id`` is
    ``str(Account.id)`` (grpc/server.py builds it that way), the same value
    ``usage_snapshots.account_id`` already stores as a UUID. It carries no
    foreign key on purpose, so the raw ``held_util_seconds`` / ``observed_seconds``
    history survives the deletion of the account it names. That preservation is
    only partial today: ``usage_cost.compute_month_cost`` joins ``accounts`` for
    the price and currency, so a deleted account's rows still exist but drop out
    of the priced allocation (the price is gone). Full price-preserving history
    (snapshotting monthly_price into the rollup) is a follow-up.
    """

    __tablename__ = "usage_daily_rollup"
    __table_args__ = (
        ForeignKeyConstraint(
            ["server_id", "tenant_id"],
            ["servers.id", "servers.tenant_id"],
            name="fk_usage_daily_rollup_server_tenant",
            ondelete="CASCADE",
        ),
        Index("ix_usage_daily_rollup_tenant_day", "tenant_id", "day"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # UTC calendar day the aggregate covers.
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    server_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    # Numerator of the allocation weight: utilization integrated over the time
    # this server held the account (utilization x seconds).
    held_util_seconds: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), nullable=False, server_default=text("0")
    )
    # Total seconds actually observed for this pair. Denominator-side quantity:
    # it bounds held_util_seconds and marks how much of the day was covered.
    observed_seconds: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), nullable=False, server_default=text("0")
    )
    # How many raw snapshots the two sums were integrated from — a coverage
    # signal for the day, not a billed quantity.
    snapshot_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LangfuseUsageRollup(Base):
    """Per-(tenant, UTC day, dimension, key) token aggregate from the Langfuse Metrics API.

    P4 console monitoring. A periodic sweep (``services.langfuse_metrics``) polls
    the external Langfuse Metrics API and compacts each day into this table, so the
    console reads a local roll-up instead of proxying every request to Langfuse.

    The composite primary key ``(tenant_id, day, dimension, key)`` is the
    idempotency anchor — re-aggregating a day upserts each row in place. ``dimension``
    is ``"model"`` (``key`` = ``providedModelName``, or ``"unknown"`` when Langfuse
    reports it null) or ``"user"`` (``key`` = the account email fixed as the
    Metrics API ``userId`` filter). ``tenant_id`` is the operator-configured
    ``AMX_LANGFUSE_TENANT_ID``, carried under a tenant-scoped ``ON DELETE CASCADE``
    foreign key (mirroring ``usage_daily_rollup``) so a deleted tenant's monitoring
    rows are dropped with it — the roll-up is worthless once its tenant is gone.

    Token columns are ``BigInteger`` (a busy tenant's monthly totals exceed a 32-bit
    int). ``cache_read_tokens`` / ``cache_creation_tokens`` are part of the schema
    but always 0 today: the Metrics API exposes no cache-token measure (only
    input/output/total), so the sweep leaves them at the server default. They stay
    in place so cache detail can be backfilled if a future measure appears without a
    migration.
    """

    __tablename__ = "langfuse_usage_rollup"
    __table_args__ = (
        Index("ix_langfuse_usage_rollup_tenant_day", "tenant_id", "day"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # UTC calendar day the aggregate covers.
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    # "model" | "user".
    dimension: Mapped[str] = mapped_column(String(16), primary_key=True)
    # providedModelName (or "unknown") for dimension="model"; userId email for "user".
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    output_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    cache_read_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    cache_creation_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    total_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    observation_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Alert(Base):
    """Operational alerts (design note §4, decision 3).

    Sourced entirely inside the gRPC/service layer — no new scheduler beyond the
    offline sweeper: all_exhausted + quarantine events (``_store_event``),
    reconcile drift (``reconcile_from_report`` path, same transaction as the
    snapshot), and server offline (``_mark_offline`` + the last_seen_at sweeper).

    Tenant isolation reuses the P1 anchor: ``(server_id, tenant_id)`` is a
    composite foreign key into ``servers(id, tenant_id)``, so an alert can never
    name a server in another tenant. ``server_id`` is nullable for forward
    compatibility, but every P4 trigger sets it.

    ``dedupe_key`` = ``server_id:kind`` (server-scoped: all_exhausted,
    server_offline) or ``server_id:kind:account_id`` (account-scoped: drift,
    quarantine). A partial unique index keeps at most one **open** alert per
    key, so a persistent condition reported every 5 minutes never floods.
    """

    __tablename__ = "alerts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["server_id", "tenant_id"],
            ["servers.id", "servers.tenant_id"],
            name="fk_alerts_server_tenant",
            ondelete="CASCADE",
        ),
        # At most one OPEN alert per dedupe_key (design note §4). acked/resolved
        # rows are exempt, so history accumulates but live noise cannot.
        Index(
            "uq_alerts_open_dedupe",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        Index("ix_alerts_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    server_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # The usage_snapshots row that produced this alert (drift/all_exhausted from a
    # report, or the switch_event). Plain column, not an FK: the snapshot cascades
    # away with the server exactly as the alert does, so no dangling reference.
    source_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    acked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acked_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AlertWebhookOutbox(Base):
    """P5 경보 웹훅 아웃박스 — 경보 open/resolve 전이의 발송 대기 큐.

    아웃박스 패턴(BACKLOG G41): ``services.alerts``의 open/resolve 프리미티브가
    경보를 여는/닫는 것과 **같은 트랜잭션**으로 여기에 행을 스테이징한다(caller가
    커밋). 트랜잭션이 롤백되면 아웃박스 행도 함께 사라지므로, 실제로 커밋되지 않은
    경보에 대한 유령 웹훅이 나가지 않는다. 발송은 형제 스윕(``alert_webhook``,
    자체 advisory 락 …08)이 HTTP POST로 드레인한다 — 성공 시 행 삭제, 실패 시
    ``attempt``/``next_attempt_at`` 지수 백오프, 상한 초과 시 폐기.

    발송 시점에 경보 자체는 이미 다른 상태로 바뀌었을 수 있으므로, 페이로드에 필요한
    전이 스냅샷(kind·status·tenant·server·detail·occurred_at)을 행에 그대로 담는다 —
    드레인은 alerts 테이블을 다시 읽지 않는다. 웹훅이 비활성(URL/시크릿 미설정)이면
    스테이징 자체를 건너뛰므로 이 테이블은 비어 있고 완전 무부작용이다.
    """

    __tablename__ = "alert_webhook_outbox"
    __table_args__ = (
        Index("ix_alert_webhook_outbox_due", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    # 전이한 경보의 id. FK 아님: 경보 행이 지워져도(테넌트/서버 CASCADE) 발송 대기 중인
    # 전이 스냅샷은 자립적으로 남아 발송을 마칠 수 있어야 하고, occurredAt 시점의 값을
    # 이미 복제해 두었으므로 원본 행에 대한 참조 무결성이 필요 없다.
    alert_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    server_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # "open" | "resolved" — 전이의 방향.
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # 리스 소유 토큰: 드레인 스윕이 만기 행을 예약할 때 부여한다. finalize는 이 토큰이
    # 자신이 부여한 값과 일치하는 행만 삭제/백오프한다 — 리스가 만료돼 다른 인스턴스가
    # 재예약(새 토큰)했다면 소유가 아니므로 no-op이 되어 이중 처리를 막는다.
    lease_token: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    # 발송 시도 횟수와 다음 시도 가능 시각(지수 백오프의 앵커). 신규 행은 즉시 발송
    # 대상이 되도록 next_attempt_at 기본값을 now()로 둔다.
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BillingEvent(Base):
    """F5 internal billing outbox — one aggregated row per (tenant, closed UTC day).

    Derived from the ``usage_snapshots`` ledger by ``services.billing.sweep_billing``
    (design note p5 §6). This is an *internal* charging schema; there is no
    external payment integration. The ``UniqueConstraint(tenant_id, kind,
    period_start)`` is the idempotency anchor — the sweep re-runs safely and an
    ``ON CONFLICT DO NOTHING`` insert never duplicates a day.
    """

    __tablename__ = "billing_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "kind", "period_start",
            name="uq_billing_events_tenant_kind_period",
        ),
        Index("ix_billing_events_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    exported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AdminAuditLog(Base):
    """Append-only trail of every mutating admin REST call (console-test gap G53).

    One row per POST/PATCH/DELETE that reaches the API, written by the audit
    middleware (``app.api.audit``) *after* the response so the real
    ``status_code`` is recorded — a rejected 4xx/5xx is logged exactly like a
    successful 2xx, since "who tried to do what" is the point.

    ``tenant_id`` is nullable and carries **no** foreign key on purpose: a
    global action (e.g. ``POST /tenants``, an admin CRUD call) belongs to no
    tenant, and the trail must outlive the tenant it references — deleting a
    tenant must never cascade its audit history away. ``admin_email`` is the
    caller's identity (``Principal.email``; the root token stamps the
    ``ROOT_PRINCIPAL_EMAIL`` sentinel), NULL only when the request failed auth
    before a principal was resolved.

    The request body is deliberately never stored: it carries credential sets
    (``POST /accounts``) and authorization codes (``:oauth-complete``), and §7
    forbids credential material in any at-rest record or log. ``action`` is the
    matched route template (``"{METHOD} {route.path}"``) and ``target_id`` is the
    last UUID path segment when present, so the trail stays queryable without the
    body.
    """

    __tablename__ = "admin_audit_logs"
    __table_args__ = (
        # The read endpoint filters by tenant (a tenant's rows, plus the global
        # NULL-tenant rows for a global-admin) and orders by created_at; the
        # composite serves both the equality and the NULL scan with the ordering.
        Index("ix_admin_audit_logs_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    # No FK (see class docstring): a global action has no tenant, and audit
    # history must survive the deletion of the tenant it names.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    admin_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BillingCursor(Base):
    """Watermark for the F5 billing sweep — one row per ``kind`` ("usage_daily").

    ``watermark`` is the exclusive end (a UTC day boundary) of the last day the
    sweep has already aggregated, so the next run starts exactly there.
    """

    __tablename__ = "billing_cursors"

    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    watermark: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
