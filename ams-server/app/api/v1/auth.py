"""Session authentication — `/auth/login`, `/auth/logout` (F1 RBAC, §3).

`login` is the one unauthenticated endpoint in the API: it exchanges an
email+password for an opaque session token. `logout` revokes the presented
session. Neither endpoint logs the password or the token.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, status

from app import schemas
from app.api.deps import AdminAuth, DbSession
from app.core.errors import ApiError
from app.services import admins

# No router-level auth: login must be reachable without a token. logout adds
# AdminAuth per-route.
router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=schemas.LoginResponse)
def login(body: schemas.LoginRequest, db: DbSession):
    admin = admins.authenticate(db, body.email, body.password)
    raw_token, session = admins.create_session(db, admin)
    principal = admins.principal_for_admin(admin)
    return schemas.LoginResponse(
        session_token=raw_token,
        role=admin.role,
        tenant_ids=sorted(principal.tenant_ids),
        expires_at=session.expires_at,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, dependencies=[AdminAuth])
def logout(db: DbSession, authorization: str | None = Header(default=None)):
    # AdminAuth already accepted this Bearer (a live session or the root token).
    # Delete the row for whatever token was presented; idempotent when the token
    # is the root token or an already-revoked session.
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise ApiError(
            401, "Unauthorized", "auth.missing_bearer", "Bearer token required."
        )
    admins.delete_session(db, token)
    return None
