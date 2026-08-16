"""Audit trail for mutating admin REST calls (console-test gap G53).

An HTTP middleware records one ``admin_audit_logs`` row per POST/PATCH/DELETE
that reaches the API, *after* the response so the real ``status_code`` is
captured — a request rejected with 4xx/5xx is logged exactly like a 2xx, since
"who tried to do what, and did it succeed" is the whole point.

Read-only verbs (GET/HEAD/OPTIONS) are never recorded. A small exclusion set is
skipped: ``/auth/login`` carries a password, ``/ingest/danger-command`` is an
unattended agent call authenticated by a static token rather than an admin, and
``/healthz`` is an unauthenticated liveness probe.

The request body is deliberately never read or stored (§7): it can hold a
credential set or an OAuth authorization code. Only routing metadata — method,
path, matched route template, the trailing UUID, and the caller's email — is
kept. A failure to write the row is swallowed with a warning: the audit trail
must never turn a successful admin action into a 500.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request, Response

from app.core.auth import Principal
from app.db import get_sessionmaker
from app.models import AdminAuditLog

_logger = logging.getLogger(__name__)

# Verbs that change state. PUT is included for completeness though the API uses
# PATCH today; GET/HEAD/OPTIONS are read-only and never recorded.
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Full request paths (with the /api/v1 prefix where the router carries one) that
# are never audited — see the module docstring for why each is here.
_EXCLUDED_PATHS = frozenset(
    {
        "/api/v1/auth/login",
        "/api/v1/ingest/danger-command",
        "/healthz",
    }
)


def _parse_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        return None


def _target_id_from_path(path: str) -> uuid.UUID | None:
    """The last UUID path segment, or None. The action's object (§ audit)."""
    for segment in reversed(path.strip("/").split("/")):
        # A trailing action verb (`…/{id}:deliver`) rides on the same segment as
        # the id, so split the `:verb` off before parsing.
        candidate = _parse_uuid(segment.split(":", 1)[0])
        if candidate is not None:
            return candidate
    return None


def _record(request: Request, status_code: int) -> None:
    """Write one audit row for the just-served mutating request. Never raises."""
    try:
        path = request.url.path
        route = request.scope.get("route")
        route_path = getattr(route, "path", None)
        action = f"{request.method} {route_path}" if route_path else f"{request.method} {path}"

        path_params = request.scope.get("path_params") or {}
        tenant_id = _parse_uuid(path_params.get("tenant_id"))

        principal = getattr(request.state, "principal", None)
        admin_email = principal.email if isinstance(principal, Principal) else None

        with get_sessionmaker()() as session:
            session.add(
                AdminAuditLog(
                    tenant_id=tenant_id,
                    admin_email=admin_email,
                    method=request.method,
                    path=path,
                    action=action,
                    target_id=_target_id_from_path(path),
                    status_code=status_code,
                )
            )
            session.commit()
    except Exception:  # noqa: BLE001 - auditing must not break the request
        _logger.warning("failed to write audit log for %s %s", request.method, request.url.path)


def install_audit_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def _audit(request: Request, call_next):
        if request.method not in _MUTATING_METHODS or request.url.path in _EXCLUDED_PATHS:
            return await call_next(request)
        try:
            response: Response = await call_next(request)
        except Exception:
            # An unhandled error still surfaces as a 500 to the caller; record the
            # attempt before re-raising so a failed mutation is not invisible.
            _record(request, 500)
            raise
        _record(request, response.status_code)
        return response
