"""Inventory service layer — tenant/account/server/assignment lifecycle.

Every lookup here re-checks that the row's `tenant_id` equals the tenant_id
from the request path. That check is redundant with the composite foreign keys
of §5.1 for assignments, and deliberately so: §7 calls for defence in depth,
and for accounts and servers (which the database cannot cross-check on their
own) it is the only thing standing between a guessed UUID and another tenant's
data.
"""

from __future__ import annotations

import base64
import json
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
    provider: str = "claude",
    owner: str | None = None,
) -> Account:
    get_tenant(db, tenant_id)
    if provider == "codex":
        _validate_codex_secret(secret)
        # A Codex credential set is always the product of a ChatGPT OAuth login
        # (auth_mode "chatgpt"); the request's credential_type is not trusted to
        # describe it, so the stored value is fixed here. It also keeps
        # mask_secret's prefix honest for the console.
        credential_type = "oauth"
    account = Account(
        tenant_id=tenant_id,
        provider=provider,
        email=email,
        owner=owner,
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


# A Codex auth.json is a handful of tokens — a few kilobytes at most. The cap is
# checked before parsing, so an oversized body never reaches the JSON parser or
# the encryptor.
_CODEX_SECRET_MAX_BYTES = 64 * 1024


def _codex_invalid(detail: str):
    """400 for a malformed Codex credential.

    `detail` names the offending KEY and nothing else. This text goes back to
    the caller and into whatever logs the response, while the payload it
    describes holds a live refresh token — so no value from the credential may
    appear in it (§7).
    """
    return bad_request("account.codex_credential_invalid", detail)


def _reject_json_constant(literal: str):
    """`parse_constant` hook: refuse NaN / Infinity / -Infinity.

    Python's JSON parser accepts these three by extension; Go's encoding/json,
    which the agent uses to read the staged auth.json, does not. Accepting one
    here would store a credential that every downstream reader rejects. The
    literal is named in the message because it can only be one of those three
    strings — it is never credential content.
    """
    raise _codex_invalid(f"The credential contains a non-JSON literal: {literal}.")


def _loads_codex_json(secret: str):
    """`json.loads` with every failure mode turned into a 400.

    Deeply nested input is the reason `RecursionError` is caught: it is not a
    `ValueError`, it costs nothing to send (a few kilobytes of '[' passes the
    size cap), and uncaught it is a 500.
    """
    try:
        return json.loads(secret, parse_constant=_reject_json_constant)
    except (ValueError, TypeError, RecursionError):
        # `_reject_json_constant` raises ApiError, which is none of these and
        # so keeps its own more specific message.
        raise _codex_invalid(
            "The credential is not valid JSON; supply the contents of Codex's auth.json."
        ) from None


def _validate_codex_secret(secret: str) -> None:
    """Reject anything that is not a usable Codex `auth.json` before it is stored.

    The agent's CodexBridge stages this blob verbatim as the runner's auth.json,
    and a Codex session that cannot refresh is indistinguishable from a healthy
    one until the access token expires hours later. `tokens.refresh_token` is
    the field that decides that, so it is the one field required here. Anything
    else in the file (auth_mode, OPENAI_API_KEY, last_refresh) varies by login
    method and is left alone.

    Everything this function rejects, it rejects BEFORE the value reaches
    `encrypt_secret`/`mask_secret`, both of which encode strictly and would turn
    a hostile string into a 500 rather than a 400.
    """
    if not isinstance(secret, str):
        raise _codex_invalid("The credential must be the text of a Codex auth.json.")
    try:
        encoded_size = len(secret.encode("utf-8"))
    except UnicodeEncodeError:
        # Lone surrogates survive JSON parsing (\ud800 decodes to one) but blow
        # up the strict .encode() in crypto — a 500 on caller-supplied input.
        raise _codex_invalid(
            "The credential contains characters that are not valid UTF-8 "
            "(unpaired surrogate); re-export the auth.json."
        ) from None
    if encoded_size > _CODEX_SECRET_MAX_BYTES:
        raise _codex_invalid(
            f"The credential exceeds the {_CODEX_SECRET_MAX_BYTES}-byte limit for a Codex auth.json."
        )
    payload = _loads_codex_json(secret)
    if not isinstance(payload, dict):
        raise _codex_invalid("The credential must be a JSON object (Codex's auth.json).")
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        raise _codex_invalid("Missing or malformed key: tokens.")
    refresh_token = tokens.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        raise _codex_invalid("Missing or empty key: tokens.refresh_token.")


def _email_from_id_token(id_token: object) -> str | None:
    """The `email` claim of a Codex id_token, or None. The signature is NOT verified.

    Nothing here holds the issuer's signing key, and it does not need to: the
    authoritative email is the one the administrator typed. This claim is used
    for exactly one thing — refusing an auth.json that plainly belongs to some
    other account — so an unparseable or unsigned token simply yields None and
    the cross-check is skipped.
    """
    if not isinstance(id_token, str):
        return None
    parts = id_token.split(".")
    if len(parts) != 3:
        return None
    segment = parts[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(claims, dict):
        return None
    email = claims.get("email")
    if isinstance(email, str) and email.strip():
        return email.strip()
    return None


def _apply_codex_metadata(account: Account, secret: str) -> None:
    """Codex half of `_apply_credential_metadata`.

    Lifts `tokens.account_id` into `account_uuid` (the same column Claude fills
    from `accountUuid`) and refuses a credential whose id_token names a
    different mailbox than the one being registered — the cheap guard against
    pasting the wrong operator's auth.json into an account row.
    """
    try:
        payload = json.loads(secret)
    except (ValueError, TypeError, RecursionError):
        return
    if not isinstance(payload, dict):
        return
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        return
    account_id = tokens.get("account_id")
    if isinstance(account_id, str) and account_id:
        account.account_uuid = account_id
    claimed_email = _email_from_id_token(tokens.get("id_token"))
    if claimed_email and account.email:
        if claimed_email.casefold() != str(account.email).strip().casefold():
            # Neither address is echoed: one is credential content, and quoting
            # the other invites a caller to diff them into an oracle.
            raise bad_request(
                "account.codex_email_mismatch",
                "The credential's id_token identifies a different account than "
                "the email supplied. Check which auth.json belongs to this account.",
            )


def _apply_credential_metadata(account: Account, secret: str) -> None:
    """Lift the non-secret fields of a credential set onto the row.

    Best effort by design: an `api_key` secret is an opaque string with no
    metadata to lift, and an OAuth set from an import may be shaped differently
    from the one `:oauth-complete` builds. Failure to parse is not an error —
    it just leaves the metadata columns empty. The Codex branch is the one
    exception: a parsed-but-contradictory credential is rejected there.
    """
    if account.provider == "codex":
        _apply_codex_metadata(account, secret)
        return
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
    owner: str | None = None,
) -> Account:
    account = get_account(db, tenant_id, account_id)
    if (
        account.provider == "codex"
        and email is not None
        and email != account.email
        and secret is None
    ):
        # Registration cross-checks the email against the id_token's claim. A
        # bare email edit would walk the row away from the credential it was
        # checked against, leaving the account:credential pairing unverifiable
        # for audit. Re-point it and re-prove it in the same request.
        raise bad_request(
            "account.codex_email_requires_credential",
            "Changing a Codex account's email requires the matching auth.json in "
            "the same request, so the credential's identity can be re-checked.",
        )
    if email is not None:
        account.email = email
    if status is not None:
        account.status = status
    if owner is not None:
        account.owner = owner
    if secret is not None:
        # A rotated credential re-enters through the same door as the first one,
        # so it faces the same check — otherwise PATCH would be a way to park an
        # unusable auth.json on an already-registered Codex account.
        if account.provider == "codex":
            _validate_codex_secret(secret)
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
def _reject_second_codex_account(db: Session, tenant_id: uuid.UUID, server_id: uuid.UUID) -> None:
    """One Codex account per server — enforced here because delivery cannot.

    Codex keeps its credential in a single `auth.json` under the runner's config
    home. Delivering a second Codex account to the same server overwrites the
    first one's file, silently unseating an account that AMS still believes is
    delivered. The agent refuses that with `codex_single_account`, but by then
    the assignment row already exists and the operator is looking at a failed
    command; the contract is that AMS never creates the assignment at all.

    Claude is unaffected: its accounts live side by side under distinct config
    entries, and a server holding several of them is the normal case.

    The count below is a plain SELECT, so two simultaneous POSTs would both read
    zero and both insert. There is no unique index that can express the rule
    (the deciding column, `provider`, lives on `accounts`, not `assignments`),
    so the server row is locked FOR UPDATE first and held until commit: the
    check and the insert become atomic against each other. The lock is taken
    only on the Codex path and only on the one row, so concurrent Claude
    assignments — and assignments to any other server — are unaffected.
    """
    db.execute(select(Server.id).where(Server.id == server_id).with_for_update())
    existing = db.scalar(
        select(func.count())
        .select_from(Assignment)
        .join(Account, Account.id == Assignment.account_id)
        .where(
            Assignment.tenant_id == tenant_id,
            Assignment.server_id == server_id,
            Assignment.state != "detached",
            Account.provider == "codex",
        )
    )
    if existing:
        raise conflict(
            "assignment.server_codex_capacity",
            "This server already holds a Codex account. Codex stores one "
            "credential per host, so recall the current one before assigning "
            "another.",
        )


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
    account = get_account(db, tenant_id, account_id)
    get_server(db, tenant_id, server_id)
    if account.provider == "codex":
        _reject_second_codex_account(db, tenant_id, server_id)

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
