"""Admin bearer authentication.

P1 is a single administrator (§7: "P1은 단일 관리자로 시작 가능"). The token
comes from `AMX_ADMIN_TOKEN`; `app.config` refuses to start without it, so
there is no default and no in-code fallback to fall back to.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Literal

from fastapi import Header, Request

from app.config import get_settings
from app.core.errors import ApiError

# The 2-role vocabulary S2 (F1 RBAC) enforces. The single administrator today is
# a global-admin; S2 adds tenant-admin. Kept as a Literal so a stray role value
# is a type error rather than a silent authorization gap.
Role = Literal["global-admin", "tenant-admin"]

# Audit-trail identity for the bootstrap root token (AMX_ADMIN_TOKEN). It has no
# admins-table row and therefore no email; this sentinel makes a root-token
# action legible in admin_audit_logs.admin_email as a break-glass call rather
# than a blank field. The angle brackets keep it out of the email value space so
# it can never collide with a real admin address.
ROOT_PRINCIPAL_EMAIL = "<root-token>"


@dataclass(frozen=True)
class Principal:
    """The authenticated caller behind a request.

    P5-S1 introduces the type only; it is not yet read by any endpoint (scoping
    is S2). Tenant reach is modelled so S2 changes only *values*, never this
    type: `all_tenants=True` is the global-admin's every-tenant reach, and
    `tenant_ids` is the explicit allow-set an S2 tenant-admin carries
    (`all_tenants=False, tenant_ids=frozenset({tid})`). The two are kept
    separate so no code is tempted to compare a tenant id against a `"*"`
    string.
    """

    role: Role
    all_tenants: bool
    tenant_ids: frozenset[str] = field(default_factory=frozenset)
    # The caller's identity for the audit trail (admin_audit_logs.admin_email).
    # A DB-backed session carries the admin's email; the bootstrap root token has
    # no row, so it is stamped with the ROOT_PRINCIPAL_EMAIL sentinel instead of a
    # real address. Kept out of any authorization decision — reach is decided by
    # `all_tenants`/`tenant_ids` alone.
    email: str | None = None


def require_admin(
    request: Request = None,  # type: ignore[assignment]
    authorization: str | None = Header(default=None),
) -> Principal:
    # `request` is injected by FastAPI when this runs as a dependency, and is
    # None when called directly (e.g. unit tests). The resolved Principal is
    # stashed on request.state so the audit middleware can record admin_email
    # without re-resolving the token; authorization is unaffected by it.
    def _resolved(principal: Principal) -> Principal:
        if request is not None:
            request.state.principal = principal
        return principal

    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise ApiError(
            401, "Unauthorized", "auth.missing_bearer", "Bearer token required."
        )
    # Priority 1 — the bootstrap root token (AMX_ADMIN_TOKEN). Unchanged from
    # P1: a matching root Bearer is always a global-admin reaching every tenant,
    # independent of the admins table, so the system can never lock itself out
    # (break-glass / M2M). Compared as UTF-8 bytes, not str: compare_digest
    # raises TypeError on a code point above U+00FF, so a non-ASCII header would
    # otherwise 500 where a 401 belongs. Encoding both sides is constant-time.
    if secrets.compare_digest(token.encode("utf-8"), get_settings().admin_token.encode("utf-8")):
        return _resolved(
            Principal(
                role="global-admin",
                all_tenants=True,
                tenant_ids=frozenset(),
                email=ROOT_PRINCIPAL_EMAIL,
            )
        )
    # Priority 2 — a DB-backed opaque session (F1 RBAC, §3). Looked up by token
    # hash, expiry-checked, and rejected for a disabled admin. Imported lazily to
    # keep app.core.auth free of a service-layer import cycle.
    from app.db import get_sessionmaker
    from app.services.admins import resolve_session

    with get_sessionmaker()() as session:
        principal = resolve_session(session, token)
    if principal is not None:
        return _resolved(principal)
    # Priority 3 — neither the root token nor a live session matched.
    raise ApiError(401, "Unauthorized", "auth.invalid_token", "Invalid admin token.")
