"""FastAPI application factory.

Configuration is read at construction time, so a missing `AMX_ADMIN_TOKEN` or
`AMX_ENCRYPTION_KEY` fails here rather than on the first request (§7).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from app.api import download
from app.api.audit import install_audit_middleware
from app.api.v1 import (
    accounts,
    admins,
    alerts,
    assignments,
    audit,
    auth,
    billing,
    ingest,
    langfuse,
    pool,
    servers,
    stats,
    tenants,
    usage,
)
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
    # Records every mutating admin call after the response (app/api/audit.py).
    install_audit_middleware(app)

    app.include_router(auth.router, prefix=API_PREFIX)
    app.include_router(admins.router, prefix=API_PREFIX)
    app.include_router(tenants.router, prefix=API_PREFIX)
    app.include_router(accounts.router, prefix=API_PREFIX)
    app.include_router(servers.router, prefix=API_PREFIX)
    app.include_router(assignments.router, prefix=API_PREFIX)
    app.include_router(audit.router, prefix=API_PREFIX)
    app.include_router(alerts.router, prefix=API_PREFIX)
    app.include_router(billing.router, prefix=API_PREFIX)
    app.include_router(usage.router, prefix=API_PREFIX)
    app.include_router(langfuse.router, prefix=API_PREFIX)
    # 대시보드 집계 통계(dashboard-redesign-plan.md 부록 A) — 읽기 전용, 조작 없음.
    app.include_router(stats.router, prefix=API_PREFIX)
    # 계정 풀 P1(관측만): 조회·정책·수동 개입만이고 명령은 내지 않는다.
    app.include_router(pool.router, prefix=API_PREFIX)
    # Token-gated, no admin bearer and no TenantScope: an unattended agent hook
    # posts here (app/api/v1/ingest.py). Disabled (404) unless a token is set.
    app.include_router(ingest.router, prefix=API_PREFIX)
    # Root paths, no prefix and no auth: the install one-liner runs before the
    # machine has any credential (app/api/download.py).
    app.include_router(download.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


# Credential material must never reach a log line (§7). urllib3/httpx debug
# logging would put the token-exchange request body there, so it is capped
# regardless of the root logger's level.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
