"""Fixtures for the P2 end-to-end suite (design note §8).

This suite runs the whole control plane as separate processes talking to one
PostgreSQL, because the P2 completion criterion is a claim about what three
independent hosts end up holding — and nothing short of three real agent
processes, each with its own Claude config home and its own real tsamx pool,
can settle that.

What is real here: the ``agent_commands`` outbox in PostgreSQL, the ``grpc.aio``
control-plane process, three compiled ``ama`` daemons over gRPC, and the actual
``tsamx`` CLI installed into a throwaway virtualenv.

What is mocked: the credential sets. They are synthetic OAuth documents that
carry no ``accessToken``, which is also how this suite keeps its promise never
to touch the network — tsamx derives a static "no credentials" usage state for
such an account and never reaches its fetch path, so no request is made to the
Anthropic usage API. No real login, no real token, ever.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO_ROOT = Path(__file__).resolve().parent.parent
AMS_SERVER = REPO_ROOT / "ams-server"
AMA_AGENT = REPO_ROOT / "ama-agent"
TSAMX_SRC = REPO_ROOT / "tsamx"

POSTGRES_IMAGE = os.environ.get("AMX_TEST_POSTGRES_IMAGE", "postgres:16-alpine")
TEST_ENCRYPTION_KEY = Fernet.generate_key().decode()
TEST_ADMIN_TOKEN = "e2e-admin-" + uuid.uuid4().hex

# Go is often installed outside PATH (an SDK directory, a toolchain download).
# AMX_GO_BIN wins; these are the conventional fallbacks.
GO_CANDIDATES = (
    Path.home() / "go-sdk" / "go" / "bin" / "go",
    Path.home() / "go-toolchain" / "go" / "bin" / "go",
    Path("/usr/local/go/bin/go"),
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


# -- Infrastructure -----------------------------------------------------------
@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    port = _free_port()
    name = f"amx-e2e-pg-{uuid.uuid4().hex[:8]}"
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
            # server for its init pass; one extra beat is cheaper than a flake.
            time.sleep(1.0)
            return
        time.sleep(0.5)
    raise RuntimeError(f"PostgreSQL container {container} never became ready")


@pytest.fixture(scope="session")
def signing_keys() -> dict[str, str]:
    """One Ed25519 keypair: the seed AMS signs with, the public key AMA verifies.

    A generated-per-run key is what makes the signature check meaningful — the
    agents only accept commands from the AMS process this run started.
    """
    private = Ed25519PrivateKey.generate()
    seed = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return {"signing_key": _b64url(seed), "public_key": base64.b64encode(public).decode()}


@pytest.fixture(scope="session")
def app_env(postgres_dsn: str, signing_keys: dict[str, str]) -> str:
    """Configure this process for the AMS app and bring the schema to head."""
    os.environ["AMX_DATABASE_URL"] = postgres_dsn
    os.environ["AMX_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    os.environ["AMX_ADMIN_TOKEN"] = TEST_ADMIN_TOKEN
    os.environ["AMX_SIGNING_KEY"] = signing_keys["signing_key"]
    os.environ.setdefault("AMX_OAUTH_FLOW_TTL_SECONDS", "600")

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(AMS_SERVER / "alembic.ini"))
    cfg.set_main_option("script_location", str(AMS_SERVER / "alembic"))
    command.upgrade(cfg, "head")
    return postgres_dsn


@pytest.fixture(scope="session")
def workdir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("amx-e2e")


# -- Built artifacts ----------------------------------------------------------
@pytest.fixture(scope="session")
def tsamx_bin(workdir: Path) -> Path:
    """Install the repo's tsamx into a throwaway venv and return its entrypoint.

    Installed rather than run through ``uv run`` per invocation: the agents call
    the CLI dozens of times in one run, and a resolved venv keeps each call to
    tens of milliseconds.
    """
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required to install tsamx for the E2E suite")
    venv = workdir / "tsamx-venv"
    subprocess.run([uv, "venv", str(venv)], check=True, capture_output=True)
    subprocess.run(
        [uv, "pip", "install", "--python", str(venv / "bin" / "python"), str(TSAMX_SRC)],
        check=True,
        capture_output=True,
    )
    binary = venv / "bin" / "tsamx"
    assert binary.exists(), "tsamx entrypoint missing from the E2E venv"
    return binary


@pytest.fixture(scope="session")
def ama_binary(workdir: Path) -> Path:
    """Compile the Go AMA daemon once for the whole session."""
    go = os.environ.get("AMX_GO_BIN") or shutil.which("go")
    if go is None:
        go = next((str(c) for c in GO_CANDIDATES if c.exists()), None)
    if go is None:
        pytest.skip("no Go toolchain found; set AMX_GO_BIN to build the AMA daemon")
    out = workdir / "ama"
    subprocess.run(
        [go, "build", "-o", str(out), "./cmd/ama"],
        cwd=str(AMA_AGENT),
        check=True,
        capture_output=True,
    )
    return out


# -- Running processes --------------------------------------------------------
class Process:
    """A subprocess whose output is captured to a file for failure diagnosis."""

    def __init__(self, name: str, argv: list[str], env: dict[str, str], cwd: Path, log_dir: Path):
        self.name = name
        self.log_path = log_dir / f"{name}.log"
        self._log = self.log_path.open("wb")
        self.popen = subprocess.Popen(
            argv, env=env, cwd=str(cwd), stdout=self._log, stderr=subprocess.STDOUT
        )

    def logs(self) -> str:
        self._log.flush()
        try:
            return self.log_path.read_text(errors="replace")
        except OSError:
            return ""

    def stop(self) -> None:
        if self.popen.poll() is None:
            self.popen.terminate()
            try:
                self.popen.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.popen.kill()
                self.popen.wait(timeout=10)
        self._log.close()


@pytest.fixture(scope="session")
def grpc_server(app_env: str, signing_keys: dict[str, str], workdir: Path):
    """The AMS gRPC control plane, as its own process on its own port (§1)."""
    port = _free_port()
    env = dict(os.environ)
    env.update(
        {
            "AMX_DATABASE_URL": app_env,
            "AMX_ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
            "AMX_ADMIN_TOKEN": TEST_ADMIN_TOKEN,
            "AMX_SIGNING_KEY": signing_keys["signing_key"],
            "AMX_GRPC_PORT": str(port),
            "AMX_GRPC_POLL_INTERVAL": "0.2",
            # Local E2E runs plaintext; opt in explicitly since the server now
            # refuses to start without TLS otherwise (§7 in-transit).
            "AMX_GRPC_ALLOW_INSECURE": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    log_dir = workdir / "logs"
    log_dir.mkdir(exist_ok=True)
    proc = Process("ams-grpc", [sys.executable, "-m", "app.grpc.server"], env, AMS_SERVER, log_dir)
    try:
        _wait_port(port, proc, timeout_s=30)
        yield f"127.0.0.1:{port}"
    finally:
        proc.stop()


def _wait_port(port: int, proc: Process, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.popen.poll() is not None:
            raise RuntimeError(f"{proc.name} exited early:\n{proc.logs()}")
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"{proc.name} never listened on :{port}\n{proc.logs()}")


@pytest.fixture(scope="session")
def client(app_env: str):
    """Admin-authenticated REST client, in-process against the same database."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {TEST_ADMIN_TOKEN}"})
        yield test_client


# -- Agent hosts --------------------------------------------------------------
class AgentHost:
    """One simulated server: an isolated Claude config home plus its ama daemon.

    HOME, CLAUDE_CONFIG_DIR and XDG_DATA_HOME are all redirected under this
    host's directory, so its tsamx pool cannot see or be seen by another host's
    — the isolation the 3/5/2 assertion depends on.
    """

    def __init__(self, label: str, root: Path, tsamx_bin: Path, ama_binary: Path):
        self.label = label
        self.root = root
        self.tsamx_bin = tsamx_bin
        self.ama_binary = ama_binary
        self.home = root / "home"
        self.config_dir = self.home / ".claude"
        self.data_home = root / "share"
        self.state_dir = root / "state"
        for path in (self.home, self.config_dir, self.data_home, self.state_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.agent_id = f"ama-{label}"
        self.server_id: str | None = None
        self.process: Process | None = None

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "HOME": str(self.home),
                "CLAUDE_CONFIG_DIR": str(self.config_dir),
                "XDG_DATA_HOME": str(self.data_home),
                "AMX_TSAMX_BIN": str(self.tsamx_bin),
                # The agent dials plaintext locally; opt in explicitly since the
                # transport now fails closed without TLS (§7 in-transit).
                "AMX_GRPC_ALLOW_INSECURE": "1",
            }
        )
        return env

    def tsamx_accounts(self) -> list[dict]:
        """`tsamx list --json` for this host's pool, as the operator would run it."""
        out = subprocess.run(
            [str(self.tsamx_bin), "list", "--json"],
            env=self.env(),
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(out.stdout)["accounts"]

    def manifest_records(self) -> list[dict]:
        """Plaintext metadata of the AMA manifest (credentials stay sealed)."""
        path = self.state_dir / "manifest.enc"
        if not path.exists():
            return []
        return json.loads(path.read_text())["records"]

    def start(self, ams_addr: str, enroll_token: str, public_key: str, log_dir: Path) -> None:
        env = self.env()
        env.update(
            {
                "AMX_AGENT_ID": self.agent_id,
                "AMX_SERVER_ID": self.server_id or "",
                "AMX_AMS_ADDR": ams_addr,
                "AMX_STATE_DIR": str(self.state_dir),
                "AMX_ENROLL_TOKEN": enroll_token,
                "AMX_AMS_PUBKEY": public_key,
            }
        )
        self.process = Process(f"ama-{self.label}", [str(self.ama_binary)], env, self.root, log_dir)

    def stop(self) -> None:
        if self.process is not None:
            self.process.stop()
