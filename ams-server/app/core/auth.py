"""Admin bearer authentication.

P1 is a single administrator (§7: "P1은 단일 관리자로 시작 가능"). The token
comes from `AMX_ADMIN_TOKEN`; `app.config` refuses to start without it, so
there is no default and no in-code fallback to fall back to.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Literal

from fastapi import Header

from app.config import get_settings
from app.core.errors import ApiError

# The 2-role vocabulary S2 (F1 RBAC) enforces. The single administrator today is
# a global-admin; S2 adds tenant-admin. Kept as a Literal so a stray role value
# is a type error rather than a silent authorization gap.
Role = Literal["global-admin", "tenant-admin"]


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


def require_admin(authorization: str | None = Header(default=None)) -> Principal:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise ApiError(
            401, "Unauthorized", "auth.missing_bearer", "Bearer token required."
        )
    # Compared as UTF-8 bytes, not as str: compare_digest raises TypeError on a
    # str containing any code point above U+00FF, so a non-ASCII Authorization
    # header would otherwise leave an unhandled 500 and a traceback where a 401
    # belongs. Encoding both sides also keeps the comparison constant-time.
    if not secrets.compare_digest(token.encode("utf-8"), get_settings().admin_token.encode("utf-8")):
        raise ApiError(401, "Unauthorized", "auth.invalid_token", "Invalid admin token.")
    return Principal(role="global-admin", all_tenants=True)
