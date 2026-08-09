"""Inventory service layer — tenant/account/server/assignment lifecycle.

Every lookup here re-checks that the row's `tenant_id` equals the tenant_id
from the request path. That check is redundant with the composite foreign keys
of §5.1 for assignments, and deliberately so: §7 calls for defence in depth,
and for accounts and servers (which the database cannot cross-check on their
own) it is the only thing standing between a guessed UUID and another tenant's
data.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import crypto, kek
from app.core.errors import bad_request, conflict, not_found
from app.models import (
    Account,
    Admin,
    Alert,
    Assignment,
    BillingEvent,
    Server,
    Tenant,
    TenantDek,
    UsageSnapshot,
)

_ACTIVE_ASSIGNMENT_STATES = (
    "pending",
    "delivering",
    "active",
    "inactive",
    "quarantined",
    "recalling",
)


def _now() -> datetime:
    return datetime.now(UTC)


# -- Tenants ------------------------------------------------------------------
def create_tenant(db: Session, name: str) -> Tenant:
    tenant = Tenant(name=name, status="active")
    db.add(tenant)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise conflict("tenant.duplicate_name", f"A tenant named {name!r} already exists.") from exc
    db.refresh(tenant)
    # Provision the tenant's v1 DEK now so the first credential write never races
    # a missing key (F2, §3). Done regardless of AMX_ENVELOPE_WRITE — the key is
    # cheap to hold ready and lets the flag flip without a per-tenant backfill.
    kek.create_tenant_dek(db, tenant.id, version=1)
    db.commit()
    return tenant


def list_tenants(
    db: Session,
    limit: int,
    offset: int,
    allowed_tenant_ids: frozenset[str] | None = None,
) -> tuple[list[Tenant], int]:
    """List tenants, optionally scoped to an allow-set (F1 RBAC, §4).

    `allowed_tenant_ids=None` means every tenant (a global-admin). A non-None
    set is a tenant-admin's own tenant(s); an empty set yields nothing. The
    filter is applied to both the page and the count so `total_size` reflects
    only what the caller may see.
    """
    count_q = select(func.count()).select_from(Tenant)
    rows_q = select(Tenant).order_by(Tenant.created_at, Tenant.id)
    if allowed_tenant_ids is not None:
        allowed_uuids = [uuid.UUID(t) for t in allowed_tenant_ids]
        count_q = count_q.where(Tenant.id.in_(allowed_uuids))
        rows_q = rows_q.where(Tenant.id.in_(allowed_uuids))
    total = db.scalar(count_q) or 0
    rows = db.scalars(rows_q.limit(limit).offset(offset)).all()
    return list(rows), total


def get_tenant(db: Session, tenant_id: uuid.UUID) -> Tenant:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise not_found("tenant")
    return tenant


def update_tenant(
    db: Session, tenant_id: uuid.UUID, *, name: str | None, status: str | None
) -> Tenant:
    tenant = get_tenant(db, tenant_id)
    if name is not None:
        tenant.name = name
    if status is not None:
        tenant.status = status
    tenant.updated_at = _now()
    db.commit()
    db.refresh(tenant)
    return tenant


def delete_tenant(db: Session, tenant_id: uuid.UUID) -> None:
    # G25: billing_events.tenant_id is FK CASCADE, so a delete would silently
    # drop the tenant's billing ledger. A *pending* (un-exported) event is
    # un-recovered revenue and blocks the delete like the other anchors below.
    # An *exported*-only ledger is allowed to go: export is the ledger's terminal
    # role, so those rows may cascade away with the tenant.
    tenant = get_tenant(db, tenant_id)
    live = db.scalar(
        select(func.count())
        .select_from(Assignment)
        .where(Assignment.tenant_id == tenant_id, Assignment.state != "detached")
    )
    if live:
        raise conflict("tenant.has_assignments", "Recall the tenant's assignments first.")
    owned = db.scalar(
        select(func.count()).select_from(Account).where(Account.tenant_id == tenant_id)
    ) or 0
    owned += db.scalar(
        select(func.count()).select_from(Server).where(Server.tenant_id == tenant_id)
    ) or 0
    if owned:
        raise conflict("tenant.not_empty", "Delete the tenant's accounts and servers first.")
    # admins.tenant_id is FK ... ON DELETE RESTRICT (the isolation anchor for a
    # tenant-admin). A pinned admin would otherwise turn this delete into an
    # IntegrityError → 500; check first and return a clean 409 (F1 RBAC, S2b).
    admins = db.scalar(
        select(func.count()).select_from(Admin).where(Admin.tenant_id == tenant_id)
    ) or 0
    if admins:
        raise conflict("tenant.has_admins", "Remove the tenant's admins first.")
    pending_billing = db.scalar(
        select(func.count())
        .select_from(BillingEvent)
        .where(BillingEvent.tenant_id == tenant_id, BillingEvent.status == "pending")
    ) or 0
    if pending_billing:
        raise conflict(
            "tenant.has_pending_billing",
            "Export or void the tenant's pending billing events first.",
        )
    # The tenant's DEKs are FK RESTRICT (an isolation anchor, not a cascade — a
    # tenant with live accounts must never lose its keys out from under their
    # ciphertext). By here accounts and servers are already gone, so no
    # ciphertext references these keys and they can be dropped explicitly.
    db.execute(delete(TenantDek).where(TenantDek.tenant_id == tenant_id))
    db.delete(tenant)
    db.commit()
    kek.invalidate_dek_cache(tenant_id)


# -- Accounts -----------------------------------------------------------------
def create_account(
    db: Session,
    tenant_id: uuid.UUID,
    *,
    email: str,
    credential_type: str,
    secret: str,
) -> Account:
    get_tenant(db, tenant_id)
    account = Account(
        tenant_id=tenant_id,
        email=email,
        credential_type=credential_type,
        encrypted_secret=crypto.encrypt_secret(secret, tenant_id=tenant_id, db=db),
        secret_masked=crypto.mask_secret(credential_type, secret),
        status="available",
    )
    _apply_credential_metadata(account, secret)
    db.add(account)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise conflict(
            "account.duplicate_email", "This tenant already has an account with that email."
        ) from exc
    db.refresh(account)
    return account


def _apply_credential_metadata(account: Account, secret: str) -> None:
    """Lift the non-secret fields of a credential set onto the row.

    Best effort by design: an `api_key` secret is an opaque string with no
    metadata to lift, and an OAuth set from an import may be shaped differently
    from the one `:oauth-complete` builds. Failure to parse is not an error —
    it just leaves the metadata columns empty.
    """
    import json

    try:
        payload = json.loads(secret)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(payload, dict):
        return
    oauth = payload.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return
    scopes = oauth.get("scopes")
    if isinstance(scopes, list):
        account.scopes = [s for s in scopes if isinstance(s, str)]
    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, (int, float)):
        account.credential_expires_at = datetime.fromtimestamp(expires_at / 1000, tz=UTC)
    for key, column in (("accountUuid", "account_uuid"), ("organizationName", "organization_name")):
        value = oauth.get(key)
        if isinstance(value, str) and value:
            setattr(account, column, value)


def list_accounts(
    db: Session, tenant_id: uuid.UUID, *, status: str | None, limit: int, offset: int
) -> tuple[list[Account], int]:
    get_tenant(db, tenant_id)
    where = [Account.tenant_id == tenant_id]
    if status:
        where.append(Account.status == status)
    total = db.scalar(select(func.count()).select_from(Account).where(*where)) or 0
    rows = db.scalars(
        select(Account).where(*where).order_by(Account.created_at, Account.id).limit(limit).offset(offset)
    ).all()
    return list(rows), total


def get_account(db: Session, tenant_id: uuid.UUID, account_id: uuid.UUID) -> Account:
    account = db.get(Account, account_id)
    if account is None or account.tenant_id != tenant_id:
        raise not_found("account")
    return account


def update_account(
    db: Session,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    email: str | None,
    status: str | None,
    secret: str | None,
) -> Account:
    account = get_account(db, tenant_id, account_id)
    if email is not None:
        account.email = email
    if status is not None:
        account.status = status
    if secret is not None:
        account.encrypted_secret = crypto.encrypt_secret(secret, tenant_id=tenant_id, db=db)
        account.secret_masked = crypto.mask_secret(account.credential_type, secret)
        _apply_credential_metadata(account, secret)
    account.updated_at = _now()
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise conflict(
            "account.duplicate_email", "This tenant already has an account with that email."
        ) from exc
    db.refresh(account)
    return account


def delete_account(db: Session, tenant_id: uuid.UUID, account_id: uuid.UUID) -> None:
    account = get_account(db, tenant_id, account_id)
    live = db.scalar(
        select(func.count())
        .select_from(Assignment)
        .where(
            Assignment.tenant_id == tenant_id,
            Assignment.account_id == account_id,
            Assignment.state != "detached",
        )
    )
    if live:
        raise conflict("account.assigned", "Recall the account's assignment first.")
    db.delete(account)
    db.commit()


# -- Servers ------------------------------------------------------------------
def create_server(
    db: Session, tenant_id: uuid.UUID, *, name: str, hostname: str | None, switch_mode: str
) -> Server:
    get_tenant(db, tenant_id)
    server = Server(
        tenant_id=tenant_id,
        name=name,
        hostname=hostname,
        switch_mode=switch_mode,
        status="offline",
    )
    db.add(server)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise conflict(
            "server.duplicate_name", "This tenant already has a server with that name."
        ) from exc
    db.refresh(server)
    return server


def list_servers(
    db: Session, tenant_id: uuid.UUID, *, status: str | None, limit: int, offset: int
) -> tuple[list[Server], int]:
    get_tenant(db, tenant_id)
    where = [Server.tenant_id == tenant_id]
    if status:
        where.append(Server.status == status)
    total = db.scalar(select(func.count()).select_from(Server).where(*where)) or 0
    rows = db.scalars(
        select(Server).where(*where).order_by(Server.created_at, Server.id).limit(limit).offset(offset)
    ).all()
    return list(rows), total


def get_server(db: Session, tenant_id: uuid.UUID, server_id: uuid.UUID) -> Server:
    server = db.get(Server, server_id)
    if server is None or server.tenant_id != tenant_id:
        raise not_found("server")
    return server


def update_server(
    db: Session,
    tenant_id: uuid.UUID,
    server_id: uuid.UUID,
    *,
    name: str | None,
    hostname: str | None,
    status: str | None,
) -> Server:
    server = get_server(db, tenant_id, server_id)
    if name is not None:
        server.name = name
    if hostname is not None:
        server.hostname = hostname
    if status is not None:
        server.status = status
    server.updated_at = _now()
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise conflict(
            "server.duplicate_name", "This tenant already has a server with that name."
        ) from exc
    db.refresh(server)
    return server


def delete_server(db: Session, tenant_id: uuid.UUID, server_id: uuid.UUID) -> None:
    server = get_server(db, tenant_id, server_id)
    live = db.scalar(
        select(func.count())
        .select_from(Assignment)
        .where(
            Assignment.tenant_id == tenant_id,
            Assignment.server_id == server_id,
            Assignment.state != "detached",
        )
    )
    if live:
        raise conflict("server.has_assignments", "Recall the server's assignments first.")
    db.delete(server)
    db.commit()


_UNSET = object()


def set_server_policy(
    db: Session,
    tenant_id: uuid.UUID,
    server_id: uuid.UUID,
    *,
    threshold_pct=_UNSET,
    default_strategy=_UNSET,
    cooldown_seconds=_UNSET,
    hysteresis_pct=_UNSET,
) -> Server:
    """Persist the switching policy columns (O4-C threshold/strategy + F4 O4-B
    cooldown/hysteresis).

    Only fields actually supplied are written, so a PATCH that carries just one
    leaves the others in place. Commit is the caller's; the gRPC re-assertion and
    the outbox SetPolicy read these columns back.
    """
    server = get_server(db, tenant_id, server_id)
    if threshold_pct is not _UNSET:
        server.threshold_pct = threshold_pct
    if default_strategy is not _UNSET:
        server.default_strategy = default_strategy
    if cooldown_seconds is not _UNSET:
        server.cooldown_seconds = cooldown_seconds
    if hysteresis_pct is not _UNSET:
        server.hysteresis_pct = hysteresis_pct
    server.updated_at = _now()
    return server


def issue_enroll_token(
    db: Session, tenant_id: uuid.UUID, server_id: uuid.UUID, *, ttl_seconds: int
) -> tuple[str, datetime]:
    """Mint a one-shot enrollment token; only its hash is persisted (§7).

    Issuing again replaces the previous hash, so a token that was minted but
    never used stops working as soon as its successor exists.
    """
    server = get_server(db, tenant_id, server_id)
    token = crypto.new_token()
    expires_at = _now() + timedelta(seconds=ttl_seconds)
    server.enroll_token_hash = crypto.hash_token(token)
    server.enroll_token_expires_at = expires_at
    server.updated_at = _now()
    db.commit()
    return token, expires_at


def latest_usage_snapshot(
    db: Session, tenant_id: uuid.UUID, server_id: uuid.UUID
) -> UsageSnapshot | None:
    return db.scalars(
        select(UsageSnapshot)
        .where(UsageSnapshot.tenant_id == tenant_id, UsageSnapshot.server_id == server_id)
        .order_by(UsageSnapshot.reported_at.desc())
        .limit(1)
    ).first()


def list_switch_events(
    db: Session, tenant_id: uuid.UUID, server_id: uuid.UUID, *, limit: int, offset: int
) -> tuple[list[UsageSnapshot], int]:
    """Switch/quarantine/all_exhausted events for one server, newest first.

    Reads the ``switch_event`` rows of ``usage_snapshots`` (there is no separate
    event table). Resolves the server first, so a cross-tenant id is a 404."""
    get_server(db, tenant_id, server_id)
    where = [
        UsageSnapshot.tenant_id == tenant_id,
        UsageSnapshot.server_id == server_id,
        UsageSnapshot.report_type == "switch_event",
    ]
    total = db.scalar(select(func.count()).select_from(UsageSnapshot).where(*where)) or 0
    rows = db.scalars(
        select(UsageSnapshot)
        .where(*where)
        .order_by(UsageSnapshot.reported_at.desc(), UsageSnapshot.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return list(rows), total


# -- Alerts -------------------------------------------------------------------
def list_alerts(
    db: Session,
    tenant_id: uuid.UUID,
    *,
    status: str | None,
    kind: str | None,
    limit: int,
    offset: int,
) -> tuple[list[Alert], int]:
    get_tenant(db, tenant_id)
    where = [Alert.tenant_id == tenant_id]
    if status:
        where.append(Alert.status == status)
    if kind:
        where.append(Alert.kind == kind)
    total = db.scalar(select(func.count()).select_from(Alert).where(*where)) or 0
    rows = db.scalars(
        select(Alert)
        .where(*where)
        .order_by(Alert.created_at.desc(), Alert.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return list(rows), total


def get_alert(db: Session, tenant_id: uuid.UUID, alert_id: uuid.UUID) -> Alert:
    alert = db.get(Alert, alert_id)
    # Same tenant re-check as every other lookup here (§7 defence in depth): a
    # guessed id from another tenant is indistinguishable from a missing one.
    if alert is None or alert.tenant_id != tenant_id:
        raise not_found("alert")
    return alert


def ack_alert(
    db: Session, tenant_id: uuid.UUID, alert_id: uuid.UUID, *, acked_by: str | None
) -> Alert:
    alert = get_alert(db, tenant_id, alert_id)
    if alert.status == "resolved":
        raise conflict("alert.resolved", "A resolved alert cannot be acknowledged.")
    if alert.status == "open":
        alert.status = "acked"
        alert.acked_at = _now()
    # Re-acking an already-acked alert refreshes who/when, staying idempotent.
    alert.acked_by = acked_by or "admin"
    if alert.acked_at is None:
        alert.acked_at = _now()
    db.commit()
    db.refresh(alert)
    return alert


def assigned_account_count(db: Session, server_id: uuid.UUID) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(Assignment)
            .where(Assignment.server_id == server_id, Assignment.state != "detached")
        )
        or 0
    )


# -- Assignments --------------------------------------------------------------
def create_assignment(
    db: Session,
    tenant_id: uuid.UUID,
    *,
    account_id: uuid.UUID,
    server_id: uuid.UUID,
    pinned: bool,
) -> Assignment:
    # Service-layer half of the triple defence (§7). The database enforces the
    # same rule structurally; this exists to turn it into a clean 404/409
    # instead of an IntegrityError, and to catch it before the write.
    get_account(db, tenant_id, account_id)
    get_server(db, tenant_id, server_id)

    assignment = Assignment(
        tenant_id=tenant_id,
        account_id=account_id,
        server_id=server_id,
        state="pending",
        pinned=pinned,
    )
    db.add(assignment)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise conflict(
            "assignment.account_already_assigned",
            "The account already has a non-detached assignment, or the account "
            "and server belong to different tenants.",
        ) from exc
    db.refresh(assignment)

    account = get_account(db, tenant_id, account_id)
    account.status = "assigned"
    db.commit()
    db.refresh(assignment)
    return assignment


def list_assignments(
    db: Session,
    tenant_id: uuid.UUID,
    *,
    server_id: uuid.UUID | None,
    account_id: uuid.UUID | None,
    state: str | None,
    limit: int,
    offset: int,
) -> tuple[list[Assignment], int]:
    get_tenant(db, tenant_id)
    where = [Assignment.tenant_id == tenant_id]
    if server_id:
        where.append(Assignment.server_id == server_id)
    if account_id:
        where.append(Assignment.account_id == account_id)
    if state:
        where.append(Assignment.state == state)
    total = db.scalar(select(func.count()).select_from(Assignment).where(*where)) or 0
    rows = db.scalars(
        select(Assignment)
        .where(*where)
        .order_by(Assignment.created_at, Assignment.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return list(rows), total


def get_assignment(db: Session, tenant_id: uuid.UUID, assignment_id: uuid.UUID) -> Assignment:
    assignment = db.get(Assignment, assignment_id)
    if assignment is None or assignment.tenant_id != tenant_id:
        raise not_found("assignment")
    return assignment


def update_assignment(
    db: Session, tenant_id: uuid.UUID, assignment_id: uuid.UUID, *, pinned: bool | None
) -> Assignment:
    assignment = get_assignment(db, tenant_id, assignment_id)
    if assignment.state == "detached":
        raise conflict("assignment.detached", "A detached assignment cannot be modified.")
    if pinned is None:
        raise bad_request("assignment.no_fields", "Nothing to update.")
    assignment.pinned = pinned
    assignment.updated_at = _now()
    db.commit()
    db.refresh(assignment)
    return assignment
