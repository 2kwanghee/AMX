"""Admin bearer authentication.

P1 is a single administrator (§7: "P1은 단일 관리자로 시작 가능"). The token
comes from `AMX_ADMIN_TOKEN`; `app.config` refuses to start without it, so
there is no default and no in-code fallback to fall back to.
"""

from __future__ import annotations

import secrets

from fastapi import Header

from app.config import get_settings
from app.core.errors import ApiError


def require_admin(authorization: str | None = Header(default=None)) -> None:
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
