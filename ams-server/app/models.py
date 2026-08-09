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
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
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
)
COMMAND_STATUSES = ("queued", "sent", "acked", "failed")
SWITCH_STRATEGIES = ("best", "next_available")
ALERT_KINDS = ("all_exhausted", "drift", "server_offline", "quarantine")
ALERT_SEVERITIES = ("critical", "warning")
ALERT_STATUSES = ("open", "acked", "resolved")


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


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        # ★ isolation anchor referenced by assignments' composite FK.
        UniqueConstraint("id", "tenant_id", name="uq_accounts_id_tenant"),
        UniqueConstraint("tenant_id", "email", name="uq_accounts_tenant_email"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    credential_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Fernet ciphertext of the complete credential-set JSON (§5.5). Never
    # returned by any endpoint, never logged.
    encrypted_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Non-reversible display hint only; derived at write time.
    secret_masked: Mapped[str | None] = mapped_column(Text, nullable=True)
    account_uuid: Mapped[str | None] = mapped_column(Text, nullable=True)
    organization_name: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="offline")
    last_seen_at: Mapped[datetime | None] = mapped_column(
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
