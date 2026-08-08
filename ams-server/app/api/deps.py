"""Shared router dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.db import get_session

DbSession = Annotated[Session, Depends(get_session)]
AdminAuth = Depends(require_admin)

PageSize = Annotated[int, Query(alias="pageSize", ge=1, le=200)]
PageToken = Annotated[str | None, Query(alias="pageToken")]


def offset_from_token(page_token: str | None) -> int:
    """P1 pagination is a plain offset carried in the opaque page token.

    The contract only promises the token is opaque, so this can become a
    keyset cursor later without a contract change. A token that is not a
    non-negative integer is treated as the first page rather than an error.
    """
    if not page_token:
        return 0
    try:
        offset = int(page_token)
    except ValueError:
        return 0
    return max(0, offset)


def next_page_token(offset: int, limit: int, total: int) -> str | None:
    nxt = offset + limit
    return str(nxt) if nxt < total else None
