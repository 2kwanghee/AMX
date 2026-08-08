"""AMS gRPC control-plane channel tests (design note §8 regression invariants).

Each test stands up the real ``grpc.aio`` server in-process on an ephemeral
port and drives it with a real aio client stub over the same PostgreSQL the REST
suite uses. The client plays the part of the AMA agent: it verifies command
signatures with the server's public key and opens credential envelopes with the
session KEK, exactly as the Go daemon will.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import grpc
import pytest

from app.core import crypto
from app.db import get_sessionmaker
from app.grpc import signing
from app.grpc.proto import pb, pb_grpc
from app.grpc.server import command_signature_valid, create_server
from app.services import commands, inventory, reconcile

AGENT_ID = "ama_test"


def _oauth_secret(email: str) -> str:
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "at-" + uuid.uuid4().hex,
                "refreshToken": "rt-" + uuid.uuid4().hex,
                "scopes": ["user:inference"],
                "emailAddress": email,
                "organizationName": "Acme",
            }
        }
    )


def _seed_tenant_account_server(email: str = "a@example.com"):
    with get_sessionmaker()() as db:
        tenant = inventory.create_tenant(db, "t-" + uuid.uuid4().hex[:8])
        account = inventory.create_account(
            db, tenant.id, email=email, credential_type="oauth", secret=_oauth_secret(email)
        )
        server = inventory.create_server(
            db, tenant.id, name="s-" + uuid.uuid4().hex[:8], hostname="h", switch_mode="auto"
        )
        return tenant.id, account.id, server.id


def _issue_enroll(tenant_id: uuid.UUID, server_id: uuid.UUID) -> str:
    with get_sessionmaker()() as db:
        token, _ = inventory.issue_enroll_token(db, tenant_id, server_id, ttl_seconds=3600)
        return token


def _create_assignment(tenant_id: uuid.UUID, account_id: uuid.UUID, server_id: uuid.UUID):
    with get_sessionmaker()() as db:
        a = inventory.create_assignment(
            db, tenant_id, account_id=account_id, server_id=server_id, pinned=False
        )
        return a.id


def _assignment_state(tenant_id: uuid.UUID, assignment_id: uuid.UUID) -> str:
    with get_sessionmaker()() as db:
        return inventory.get_assignment(db, tenant_id, assignment_id).state


class _Harness:
    def __init__(self, signer: signing.Signer):
        self.signer = signer
        self.server = None
        self.port = None

    async def __aenter__(self):
        self.server, _ = create_server(
            self.signer, session_factory=get_sessionmaker(), poll_interval=0.05
        )
        self.port = self.server.add_insecure_port("127.0.0.1:0")
        await self.server.start()
        return self

    async def __aexit__(self, *exc):
        await self.server.stop(None)

    def channel(self) -> grpc.aio.Channel:
        return grpc.aio.insecure_channel(f"127.0.0.1:{self.port}")


async def _read(call, timeout=10.0):
    return await asyncio.wait_for(call.read(), timeout=timeout)


def _wait_state(tenant_id, assignment_id, expected, timeout=8.0) -> str:
    deadline = time.monotonic() + timeout
    state = _assignment_state(tenant_id, assignment_id)
    while state != expected and time.monotonic() < deadline:
        time.sleep(0.1)
        state = _assignment_state(tenant_id, assignment_id)
    return state


# -- Tests --------------------------------------------------------------------
def test_enroll_promotes_and_sends_signed_session_setup(app_env):
    signer = signing.Signer.from_env_or_generate()
    tenant_id, _account_id, server_id = _seed_tenant_account_server()
    token = _issue_enroll(tenant_id, server_id)

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            stub = pb_grpc.AmxControlPlaneStub(channel)
            call = stub.Session()
            await call.write(
                pb.AmaMessage(register=pb.Register(agent_id=AGENT_ID, enroll_token=token))
            )
            cmd = await _read(call)
            assert cmd.WhichOneof("cmd") == "session_setup"
            assert command_signature_valid(signer.public_key(), cmd)
            # Enroll path returns a long-lived credential and a KEK.
            assert cmd.session_setup.server_credential
            assert len(cmd.session_setup.keys) == 1
            assert cmd.session_setup.keys[0].wrapped_key
            await call.done_writing()
            return cmd.session_setup.server_credential

    credential = asyncio.run(scenario())
    # The one-shot token is burned; the credential hash is persisted.
    with get_sessionmaker()() as db:
        server = inventory.get_server(db, tenant_id, server_id)
        assert server.enroll_token_hash is None
        assert server.server_cred_hash == crypto.hash_token(credential)
        assert server.agent_id == AGENT_ID


def test_commands_are_bound_to_the_authenticated_agent(app_env):
    """Every command AMS emits carries target_agent_id == the session's agent
    (recipient binding). SessionSetup and the deliver both."""
    signer = signing.Signer.from_env_or_generate()
    tenant_id, account_id, server_id = _seed_tenant_account_server("bind@example.com")
    token = _issue_enroll(tenant_id, server_id)
    assignment_id = _create_assignment(tenant_id, account_id, server_id)

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            stub = pb_grpc.AmxControlPlaneStub(channel)
            call = stub.Session()
            await call.write(
                pb.AmaMessage(register=pb.Register(agent_id=AGENT_ID, enroll_token=token))
            )
            setup = await _read(call)
            assert setup.WhichOneof("cmd") == "session_setup"
            assert setup.target_agent_id == AGENT_ID

            await asyncio.to_thread(_rest_deliver, tenant_id, assignment_id)
            cmd = await _read(call)
            assert cmd.WhichOneof("cmd") == "deliver"
            assert cmd.target_agent_id == AGENT_ID
            await call.done_writing()

    asyncio.run(scenario())


def test_configure_port_refuses_insecure_without_optin(monkeypatch):
    """configure_port fails closed: no TLS and no explicit opt-in -> refuse."""
    from app.grpc.server import configure_port

    monkeypatch.delenv("AMX_GRPC_TLS_CERT", raising=False)
    monkeypatch.delenv("AMX_GRPC_TLS_KEY", raising=False)
    monkeypatch.delenv("AMX_GRPC_ALLOW_INSECURE", raising=False)

    class _StubServer:
        def add_insecure_port(self, _addr):  # pragma: no cover - must not be called
            raise AssertionError("bound insecurely without opt-in")

        def add_secure_port(self, _addr, _creds):  # pragma: no cover
            raise AssertionError("bound TLS without cert/key")

    with pytest.raises(RuntimeError):
        configure_port(_StubServer(), 50051)


def test_configure_port_insecure_optin_binds_plaintext(monkeypatch):
    from app.grpc.server import configure_port

    monkeypatch.delenv("AMX_GRPC_TLS_CERT", raising=False)
    monkeypatch.delenv("AMX_GRPC_TLS_KEY", raising=False)
    monkeypatch.setenv("AMX_GRPC_ALLOW_INSECURE", "1")

    bound: list[str] = []

    class _StubServer:
        def add_insecure_port(self, addr):
            bound.append(addr)

    assert configure_port(_StubServer(), 50051) == "insecure"
    assert bound == ["[::]:50051"]


def test_deliver_roundtrip_converges_assignment_to_active(app_env):
    signer = signing.Signer.from_env_or_generate()
    tenant_id, account_id, server_id = _seed_tenant_account_server("deliver@example.com")
    token = _issue_enroll(tenant_id, server_id)
    assignment_id = _create_assignment(tenant_id, account_id, server_id)

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            stub = pb_grpc.AmxControlPlaneStub(channel)
            call = stub.Session()
            await call.write(
                pb.AmaMessage(register=pb.Register(agent_id=AGENT_ID, enroll_token=token))
            )
            setup = await _read(call)
            kek = setup.session_setup.keys[0].wrapped_key

            # REST deliver: enqueue + pending->delivering.
            await asyncio.to_thread(_rest_deliver, tenant_id, assignment_id)

            cmd = await _read(call)
            assert cmd.WhichOneof("cmd") == "deliver"
            assert command_signature_valid(signer.public_key(), cmd)
            deliver = cmd.deliver
            assert deliver.desired_status == pb.ALLOCATION_STATUS_ACTIVE

            # Open the credential envelope with the delivered KEK + local AAD.
            enc = deliver.encrypted_credential
            plaintext = signing.open_credential(
                kek,
                enc.ciphertext,
                enc.nonce,
                ams_account_id=str(account_id),
                agent_id=AGENT_ID,
            )
            payload = json.loads(plaintext)
            assert payload["claudeAiOauth"]["emailAddress"] == "deliver@example.com"
            # aad_* fields are comparison copies only.
            assert enc.aad_ams_account_id == str(account_id)
            assert enc.aad_agent_id == AGENT_ID

            # Agent converges and acks.
            await call.write(
                pb.AmaMessage(
                    ack=pb.CommandAck(
                        command_id=cmd.command_id,
                        agent_id=AGENT_ID,
                        convergence=pb.CommandAck.CONVERGENCE_CONVERGED,
                    )
                )
            )
            state = await asyncio.to_thread(
                _wait_state, tenant_id, assignment_id, "active"
            )
            await call.done_writing()
            return state

    assert asyncio.run(scenario()) == "active"
    with get_sessionmaker()() as db:
        cmd_row = next(iter(commands.fetch_queued(db, server_id)), None)
        assert cmd_row is None  # the deliver row is no longer queued (acked)


def test_wrong_tenant_ack_does_not_move_another_tenants_command(app_env):
    # Tenant B has a queued deliver; an ack processed under tenant A's session
    # binding must not touch it (proto CommandAck: cross-tenant -> REJECTED).
    tenant_a, _, _ = _seed_tenant_account_server("a@ex.com")
    tenant_b, account_b, server_b = _seed_tenant_account_server("b@ex.com")
    assignment_b = _create_assignment(tenant_b, account_b, server_b)
    _rest_deliver(tenant_b, assignment_b)  # B: pending->delivering, command queued

    with get_sessionmaker()() as db:
        queued = commands.fetch_queued(db, server_b)
        assert len(queued) == 1
        command_id = queued[0].command_id
        # Apply an ack under tenant A's binding for B's command.
        reconcile.apply_ack(
            db,
            tenant_id=tenant_a,
            command_id=command_id,
            convergence=reconcile.CONVERGED,
        )

    # B's command is untouched and B's assignment is still delivering.
    with get_sessionmaker()() as db:
        still_queued = commands.fetch_queued(db, server_b)
        assert len(still_queued) == 1
        assert inventory.get_assignment(db, tenant_b, assignment_b).state == "delivering"


def test_tampered_command_fails_signature_verification(app_env):
    signer = signing.Signer.from_env_or_generate()
    from app.grpc.server import sign_command

    cmd = pb.AmsCommand(command_id="cmd_x")
    cmd.recall.assignment_id = "assign-1"
    sign_command(signer, cmd)
    assert command_signature_valid(signer.public_key(), cmd)

    # Any post-signature mutation is rejected — the agent runs nothing that
    # fails verification (proto §6.2); the effect at the agent is REJECTED.
    cmd.recall.assignment_id = "assign-2"
    assert not command_signature_valid(signer.public_key(), cmd)


def test_cold_start_empty_register_does_not_delete_accounts(app_env):
    signer = signing.Signer.from_env_or_generate()
    tenant_id, account_id, server_id = _seed_tenant_account_server("cold@example.com")
    token = _issue_enroll(tenant_id, server_id)
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _rest_deliver(tenant_id, assignment_id)
    # Grab the queued command id so we can also assert rule 3 suppression is safe.
    with get_sessionmaker()() as db:
        applied_id = commands.fetch_queued(db, server_id)[0].command_id

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            stub = pb_grpc.AmxControlPlaneStub(channel)
            call = stub.Session()
            # Cold start: no KEK yet, so no accounts reported; but the agent
            # does claim to have applied the deliver command id.
            await call.write(
                pb.AmaMessage(
                    register=pb.Register(
                        agent_id=AGENT_ID,
                        enroll_token=token,
                        applied_command_ids=[applied_id],
                    )
                )
            )
            setup = await _read(call)
            assert setup.WhichOneof("cmd") == "session_setup"
            await call.done_writing()

    asyncio.run(scenario())

    # Nothing deleted; and because the empty report names no account, rule 3 did
    # NOT suppress the deliver — it is still pending redelivery (queued).
    with get_sessionmaker()() as db:
        assert inventory.get_account(db, tenant_id, account_id) is not None
        assert inventory.get_assignment(db, tenant_id, assignment_id).state == "delivering"
        assert len(commands.fetch_queued(db, server_id)) == 1


# -- helpers that touch the sync service layer --------------------------------
def _rest_deliver(tenant_id: uuid.UUID, assignment_id: uuid.UUID) -> None:
    with get_sessionmaker()() as db:
        commands.request_deliver(db, tenant_id, assignment_id)


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    # get_settings / get_engine / get_sessionmaker are lru_cached and were
    # populated by app_env against the test DB; nothing to reset per-test, but
    # keep the hook so a future fixture ordering change is a one-line edit.
    yield
