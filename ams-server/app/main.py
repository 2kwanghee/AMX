"""FastAPI application factory.

Configuration is read at construction time, so a missing `AMX_ADMIN_TOKEN` or
`AMX_ENCRYPTION_KEY` fails here rather than on the first request (§7).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from app.api.v1 import accounts, alerts, assignments, auth, servers, tenants
from app.config import get_settings
from app.core.errors import install_error_handlers
from app.services.oauth_enroll import PkceFlowStore

API_PREFIX = "/api/v1"


def create_app() -> FastAPI:
    get_settings()  # fail fast on missing/invalid configuration

    app = FastAPI(
        title="AMX — Account Management Server API",
        version="0.1.0",
        summary="Tenant, account, server and assignment management for AMX (P1 inventory).",
    )
    app.state.oauth_flows = PkceFlowStore()
    install_error_handlers(app)

    app.include_router(auth.router, prefix=API_PREFIX)
    app.include_router(tenants.router, prefix=API_PREFIX)
    app.include_router(accounts.router, prefix=API_PREFIX)
    app.include_router(servers.router, prefix=API_PREFIX)
    app.include_router(assignments.router, prefix=API_PREFIX)
    app.include_router(alerts.router, prefix=API_PREFIX)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


# Credential material must never reach a log line (§7). urllib3/httpx debug
# logging would put the token-exchange request body there, so it is capped
# regardless of the root logger's level.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
