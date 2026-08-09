"""Admin authentication and session service (F1 RBAC, §3).

The single place that turns credentials into sessions and sessions into a
`Principal`. Email is normalised (trim + lower) on every read and write so the
functional unique index and login agree on identity. Passwords are bcrypt via
`app.core.crypto`; only session-token *hashes* are ever stored.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.auth import Principal
from app.core.errors import ApiError, conflict, not_found
from app.models import Admin, AdminSession, Tenant

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
    if tenant_id is not None and db.get(Tenant, tenant_id) is None:
        # Without this, a non-existent tenant_id trips the FK on commit and would
        # surface below as the misleading duplicate_email conflict. Hidden as 404
        # exactly like every other unknown-tenant reference (errors.not_found).
        raise not_found("tenant")
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


# -- Admin management CRUD (F1 RBAC, S2b) -------------------------------------
def _enabled_global_admin_count(db: Session, *, exclude_id: uuid.UUID | None = None) -> int:
    """How many non-disabled global-admins exist in the admins table.

    Used to keep at least one usable human global-admin. The root
    AMX_ADMIN_TOKEN is not counted — it lives outside this table and is the
    break-glass path, so a structural lockout is impossible regardless; this is
    a safety rail against accidentally emptying the human admin plane.
    """
    q = select(func.count()).select_from(Admin).where(
        Admin.role == "global-admin", Admin.disabled.is_(False)
    )
    if exclude_id is not None:
        q = q.where(Admin.id != exclude_id)
    return db.scalar(q) or 0


def list_admins(db: Session, limit: int, offset: int) -> tuple[list[Admin], int]:
    total = db.scalar(select(func.count()).select_from(Admin)) or 0
    rows = db.scalars(
        select(Admin).order_by(Admin.created_at, Admin.id).limit(limit).offset(offset)
    ).all()
    return list(rows), total


def get_admin(db: Session, admin_id: uuid.UUID) -> Admin:
    admin = db.get(Admin, admin_id)
    if admin is None:
        raise not_found("admin")
    return admin


def update_admin(
    db: Session,
    admin_id: uuid.UUID,
    *,
    disabled: bool | None = None,
    password: str | None = None,
) -> Admin:
    """Toggle `disabled` and/or reset the password. Role/tenant are immutable.

    Disabling takes effect immediately: `resolve_session` filters on
    `Admin.disabled`, so every live session of a just-disabled admin stops
    authenticating on its next use without touching the session rows.
    """
    admin = get_admin(db, admin_id)
    if (
        disabled is True
        and not admin.disabled
        and admin.role == "global-admin"
        and _enabled_global_admin_count(db, exclude_id=admin.id) == 0
    ):
        raise conflict(
            "admin.last_global_admin",
            "Cannot disable the last enabled global-admin.",
        )
    if disabled is not None:
        admin.disabled = disabled
    if password is not None:
        admin.password_hash = crypto.hash_password(password)
    admin.updated_at = _now()
    db.commit()
    db.refresh(admin)
    return admin


def delete_admin(db: Session, admin_id: uuid.UUID) -> None:
    """Delete an admin; its sessions cascade away (admin_sessions FK CASCADE).

    Refuses to remove the last enabled global-admin for the same reason
    `update_admin` refuses to disable it.
    """
    admin = get_admin(db, admin_id)
    if (
        admin.role == "global-admin"
        and not admin.disabled
        and _enabled_global_admin_count(db, exclude_id=admin.id) == 0
    ):
        raise conflict(
            "admin.last_global_admin",
            "Cannot delete the last enabled global-admin.",
        )
    db.delete(admin)
    db.commit()
