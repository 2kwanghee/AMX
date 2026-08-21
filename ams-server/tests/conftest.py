"""Test fixtures.

A real PostgreSQL is not optional here. The central claim of §5.1 — that a
cross-tenant assignment is rejected structurally — is a claim about composite
foreign keys and a partial unique index. SQLite has neither, so testing against
it would prove nothing about the invariant the design rests on.

The container is started through the docker CLI and torn down at session end.
No test in this suite touches the network beyond that container: the OAuth
token endpoint is always a stub.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
import uuid

import pytest

from cryptography.fernet import Fernet

POSTGRES_IMAGE = os.environ.get("AMX_TEST_POSTGRES_IMAGE", "postgres:16-alpine")
# Generated per run rather than written into the repo: a checked-in key would
# be a real Fernet key sitting in version control, and nothing here needs it to
# survive the process.
TEST_ENCRYPTION_KEY = Fernet.generate_key().decode()
TEST_ADMIN_TOKEN = "test-admin-token-" + uuid.uuid4().hex


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    port = _free_port()
    name = f"amx-test-pg-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "-e", "POSTGRES_PASSWORD=amx-test",
            "-e", "POSTGRES_USER=amx",
            "-e", "POSTGRES_DB=amx",
            "-p", f"127.0.0.1:{port}:5432",
            POSTGRES_IMAGE,
        ],
        check=True,
        capture_output=True,
    )
    dsn = f"postgresql+psycopg://amx:amx-test@127.0.0.1:{port}/amx"
    try:
        _wait_ready(name, timeout_s=60)
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)


def _wait_ready(container: str, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        probe = subprocess.run(
            ["docker", "exec", container, "pg_isready", "-U", "amx", "-d", "amx"],
            capture_output=True,
        )
        if probe.returncode == 0:
            # pg_isready goes green while the entrypoint is still restarting the
            # server for its init pass; a connection accepted in that window is
            # dropped moments later. One extra beat is cheaper than a flake.
            time.sleep(1.0)
            return
        time.sleep(0.5)
    raise RuntimeError(f"PostgreSQL container {container} never became ready")


@pytest.fixture(scope="session")
def app_env(postgres_dsn: str):
    os.environ["AMX_DATABASE_URL"] = postgres_dsn
    os.environ["AMX_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    os.environ["AMX_ADMIN_TOKEN"] = TEST_ADMIN_TOKEN
    os.environ.setdefault("AMX_OAUTH_FLOW_TTL_SECONDS", "600")
    # C2: the legacy gRPC suite opens sessions without an agent_public_key and
    # reads the raw KEK straight out of SessionSetup. That path is now refused in
    # production; the dev raw-KEK fallback keeps those tests valid. Sealed-box
    # behaviour and the keyless-refusal are covered explicitly in test_kek_wrap.py,
    # which overrides this env per-test.
    os.environ.setdefault("AMX_ALLOW_RAW_KEK", "1")

    from alembic import command
    from alembic.config import Config

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(root, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(root, "alembic"))
    command.upgrade(cfg, "head")
    return postgres_dsn


@pytest.fixture()
def engine(app_env):
    from app.db import get_engine

    return get_engine()


@pytest.fixture(autouse=True)
def clean_tables(app_env, engine):
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE billing_events, billing_cursors, admin_audit_logs, "
                "admin_sessions, admins, alert_webhook_outbox, alerts, "
                "pool_events, pool_chains, pool_recommendations, "
                "account_usage_windows, "
                "usage_snapshots, assignments, accounts, servers, tenant_deks, "
                "tenants RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest.fixture()
def app(app_env):
    from app.main import create_app

    return create_app()


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {TEST_ADMIN_TOKEN}"})
        yield test_client


@pytest.fixture()
def db(app_env):
    from app.db import get_sessionmaker

    with get_sessionmaker()() as session:
        yield session
