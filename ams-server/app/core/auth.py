"""Admin bearer authentication.

P1 is a single administrator (§7: "P1은 단일 관리자로 시작 가능"). The token
comes from `AMX_ADMIN_TOKEN`; `app.config` refuses to start without it, so
there is no default and no in-code fallback to fall back to.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from fastapi import Header

from app.config import get_settings
from app.core.errors import ApiError

# Sentinel for a principal that reaches every tenant. P1/P5-S1 is a single
# administrator, so the only principal today carries this. S2 (F1 RBAC) narrows
# this to an explicit tenant set and enforces it from the shared dependency.
ALL_TENANTS = "*"


@dataclass(frozen=True)
class Principal:
    """The authenticated caller behind a request.

    P5-S1 introduces the type only; it is not yet read by any endpoint (scoping
    is S2). `tenant_ids` is `"*"` (ALL_TENANTS) for the single admin today; S2
    will widen it to a concrete tenant set.
    """

    kind: str
    tenant_ids: str


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
    return Principal(kind="admin", tenant_ids=ALL_TENANTS)
