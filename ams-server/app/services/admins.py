"""Admin authentication and session service (F1 RBAC, §3).

The single place that turns credentials into sessions and sessions into a
`Principal`. Email is normalised (trim + lower) on every read and write so the
functional unique index and login agree on identity. Passwords are bcrypt via
`app.core.crypto`; only session-token *hashes* are ever stored.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.auth import Principal
from app.core.errors import ApiError, conflict
from app.models import Admin, AdminSession

# §3: opaque session TTL. Short enough that a leaked token expires on its own;
# revocation (logout / admin delete) is immediate regardless.
SESSION_TTL = timedelta(hours=8)


def _now() -> datetime:
    return datetime.now(UTC)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def principal_for_admin(admin: Admin) -> Principal:
    """Map a stored admin row to the request Principal (§3).

    global-admin → every tenant (`all_tenants=True`, empty allow-set);
    tenant-admin → exactly its one tenant as a string allow-set. The two shapes
    mirror the CHECK constraint on `admins`, so a malformed row cannot produce
    an over-broad Principal.
    """
    if admin.role == "global-admin":
        return Principal(role="global-admin", all_tenants=True, tenant_ids=frozenset())
    return Principal(
        role="tenant-admin",
        all_tenants=False,
        tenant_ids=frozenset({str(admin.tenant_id)}),
    )


def authenticate(db: Session, email: str, password: str) -> Admin:
    """Return the admin for valid credentials, else raise 401.

    Same 401 for unknown email, wrong password and disabled account — none of
    the three tells an attacker which admins exist. bcrypt is skipped when the
    email is unknown (no row, no hash); the resulting timing signal reveals only
    email existence, not password correctness, and is accepted for this stage.
    """
    unauthorized = ApiError(
        401, "Unauthorized", "auth.invalid_credentials", "Invalid email or password."
    )
    admin = db.scalars(
        select(Admin).where(Admin.email == normalize_email(email))
    ).one_or_none()
    if admin is None or admin.disabled:
        raise unauthorized
    if not crypto.verify_password(password, admin.password_hash):
        raise unauthorized
    return admin


def create_session(db: Session, admin: Admin) -> tuple[str, AdminSession]:
    """Issue an opaque session token; store only its hash. Returns (raw, row)."""
    raw_token = crypto.new_token()
    session = AdminSession(
        admin_id=admin.id,
        token_hash=crypto.hash_token(raw_token),
        expires_at=_now() + SESSION_TTL,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return raw_token, session


def resolve_session(db: Session, raw_token: str) -> Principal | None:
    """A live, unexpired session for a non-disabled admin → its Principal.

    Expiry is enforced in the query. A disabled admin's sessions stop working
    immediately even before they are deleted.
    """
    row = db.scalars(
        select(AdminSession)
        .join(Admin, Admin.id == AdminSession.admin_id)
        .where(
            AdminSession.token_hash == crypto.hash_token(raw_token),
            AdminSession.expires_at > _now(),
            Admin.disabled.is_(False),
        )
    ).one_or_none()
    if row is None:
        return None
    admin = db.get(Admin, row.admin_id)
    if admin is None:
        return None
    return principal_for_admin(admin)


def delete_session(db: Session, raw_token: str) -> None:
    """Idempotent logout: delete the row if it exists, else do nothing."""
    row = db.scalars(
        select(AdminSession).where(
            AdminSession.token_hash == crypto.hash_token(raw_token)
        )
    ).one_or_none()
    if row is not None:
        db.delete(row)
        db.commit()


def create_admin(
    db: Session,
    *,
    email: str,
    password: str,
    role: str,
    tenant_id: uuid.UUID | None,
) -> Admin:
    """Create an admin (bootstrap CLI path). Enforces the role/tenant pairing."""
    if role not in ("global-admin", "tenant-admin"):
        raise ApiError(400, "Bad Request", "admin.invalid_role", f"Unknown role {role!r}.")
    if role == "global-admin" and tenant_id is not None:
        raise ApiError(
            400, "Bad Request", "admin.role_tenant_mismatch",
            "A global-admin must not be pinned to a tenant.",
        )
    if role == "tenant-admin" and tenant_id is None:
        raise ApiError(
            400, "Bad Request", "admin.role_tenant_mismatch",
            "A tenant-admin requires a tenant_id.",
        )
    admin = Admin(
        email=normalize_email(email),
        password_hash=crypto.hash_password(password),
        role=role,
        tenant_id=tenant_id,
        disabled=False,
    )
    db.add(admin)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise conflict(
            "admin.duplicate_email", f"An admin with email {email!r} already exists."
        ) from exc
    db.refresh(admin)
    return admin
