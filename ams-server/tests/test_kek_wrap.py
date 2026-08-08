"""C2 per-agent KEK wrapping — AMS sealing side (proto §6.2, design §7).

The AMA agent sends a fresh ephemeral X25519 public key (raw 32 bytes) in
Register. AMS seals the per-session KEK to it with a NaCl sealed box, so the KEK
is never cleartext even where TLS terminates ahead of AMS. These tests play the
agent with a real X25519 key pair and assert:

* a sealed KEK opens only with the matching private key, and the recovered KEK is
  the real session KEK (it opens a delivered credential envelope);
* a keyless session is refused unless AMX_ALLOW_RAW_KEK=1 (dev), which then
  returns the raw KEK;
* a malformed public key is refused.

Sealing agreement with AMA (Go): the wire value is the raw 32-byte X25519 public
key (not PEM/DER); AMA seals/opens with golang.org/x/crypto/nacl/box
SealAnonymous / OpenAnonymous, the wire-compatible sibling of NaCl SealedBox.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import grpc
import pytest
from nacl.public import PrivateKey, SealedBox

from app.db import get_sessionmaker
from app.grpc import signing
from app.grpc.proto import pb, pb_grpc
from app.grpc.server import create_server
from app.services import commands, inventory

AGENT_ID = "ama_kek"


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


def _seed(email: str):
    with get_sessionmaker()() as db:
        tenant = inventory.create_tenant(db, "t-" + uuid.uuid4().hex[:8])
        account = inventory.create_account(
            db, tenant.id, email=email, credential_type="oauth", secret=_oauth_secret(email)
        )
        server = inventory.create_server(
            db, tenant.id, name="s-" + uuid.uuid4().hex[:8], hostname="h", switch_mode="auto"
        )
        return tenant.id, account.id, server.id


def _issue_enroll(tenant_id, server_id) -> str:
    with get_sessionmaker()() as db:
        token, _ = inventory.issue_enroll_token(db, tenant_id, server_id, ttl_seconds=3600)
        return token


def _assign(tenant_id, account_id, server_id):
    with get_sessionmaker()() as db:
        return inventory.create_assignment(
            db, tenant_id, account_id=account_id, server_id=server_id, pinned=False
        ).id


def _rest_deliver(tenant_id, assignment_id) -> None:
    with get_sessionmaker()() as db:
        commands.request_deliver(db, tenant_id, assignment_id)


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


# -- Sealed-box happy path ----------------------------------------------------
def test_agent_public_key_is_sealed_and_opens_only_with_private_key(app_env, monkeypatch):
    # Even with the dev raw fallback enabled, a capable agent is never downgraded.
    monkeypatch.setenv("AMX_ALLOW_RAW_KEK", "1")
    signer = signing.Signer.from_env_or_generate()
    tenant_id, account_id, server_id = _seed("sealed@example.com")
    token = _issue_enroll(tenant_id, server_id)
    assignment_id = _assign(tenant_id, account_id, server_id)

    agent_sk = PrivateKey.generate()
    agent_pk = bytes(agent_sk.public_key)
    assert len(agent_pk) == 32

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            stub = pb_grpc.AmxControlPlaneStub(channel)
            call = stub.Session()
            await call.write(
                pb.AmaMessage(
                    register=pb.Register(
                        agent_id=AGENT_ID, enroll_token=token, agent_public_key=agent_pk
                    )
                )
            )
            setup = await _read(call)
            assert setup.WhichOneof("cmd") == "session_setup"
            wrapped = setup.session_setup.keys[0].wrapped_key

            # It is a sealed box, not the raw KEK: a 32-byte KEK sealed to X25519
            # is longer (ephemeral pubkey + Poly1305 tag), and the ciphertext must
            # not equal any plausible raw key.
            assert len(wrapped) > 32
            kek = SealedBox(agent_sk).decrypt(wrapped)
            assert len(kek) == 32
            assert kek != wrapped

            # Only the matching private key opens it.
            with pytest.raises(Exception):
                SealedBox(PrivateKey.generate()).decrypt(wrapped)

            # The unwrapped KEK is the real session KEK: it opens a delivered
            # credential envelope built server-side under that KEK.
            assert (await _read(call)).WhichOneof("cmd") == "set_mode"
            assert (await _read(call)).WhichOneof("cmd") == "set_policy"
            await asyncio.to_thread(_rest_deliver, tenant_id, assignment_id)
            cmd = await _read(call)
            assert cmd.WhichOneof("cmd") == "deliver"
            enc = cmd.deliver.encrypted_credential
            plaintext = signing.open_credential(
                kek, enc.ciphertext, enc.nonce,
                ams_account_id=str(account_id), agent_id=AGENT_ID,
            )
            assert json.loads(plaintext)["claudeAiOauth"]["emailAddress"] == "sealed@example.com"
            await call.done_writing()

    asyncio.run(scenario())


# -- Keyless refusal ----------------------------------------------------------
def test_keyless_session_is_refused_without_raw_optin(app_env, monkeypatch):
    monkeypatch.delenv("AMX_ALLOW_RAW_KEK", raising=False)
    signer = signing.Signer.from_env_or_generate()
    tenant_id, _account_id, server_id = _seed("refuse@example.com")
    token = _issue_enroll(tenant_id, server_id)

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            stub = pb_grpc.AmxControlPlaneStub(channel)
            call = stub.Session()
            await call.write(
                pb.AmaMessage(register=pb.Register(agent_id=AGENT_ID, enroll_token=token))
            )
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await _read(call)
            assert exc.value.code() == grpc.StatusCode.FAILED_PRECONDITION

    asyncio.run(scenario())


# -- Dev raw fallback ---------------------------------------------------------
def test_keyless_session_falls_back_to_raw_kek_with_optin(app_env, monkeypatch):
    monkeypatch.setenv("AMX_ALLOW_RAW_KEK", "1")
    signer = signing.Signer.from_env_or_generate()
    tenant_id, account_id, server_id = _seed("rawdev@example.com")
    token = _issue_enroll(tenant_id, server_id)
    assignment_id = _assign(tenant_id, account_id, server_id)

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            stub = pb_grpc.AmxControlPlaneStub(channel)
            call = stub.Session()
            await call.write(
                pb.AmaMessage(register=pb.Register(agent_id=AGENT_ID, enroll_token=token))
            )
            setup = await _read(call)
            assert setup.WhichOneof("cmd") == "session_setup"
            kek = setup.session_setup.keys[0].wrapped_key
            # Raw fallback: the wrapped_key IS the 32-byte KEK, and it opens a
            # delivered envelope directly.
            assert len(kek) == 32
            assert (await _read(call)).WhichOneof("cmd") == "set_mode"
            assert (await _read(call)).WhichOneof("cmd") == "set_policy"
            await asyncio.to_thread(_rest_deliver, tenant_id, assignment_id)
            cmd = await _read(call)
            assert cmd.WhichOneof("cmd") == "deliver"
            enc = cmd.deliver.encrypted_credential
            plaintext = signing.open_credential(
                kek, enc.ciphertext, enc.nonce,
                ams_account_id=str(account_id), agent_id=AGENT_ID,
            )
            assert json.loads(plaintext)["claudeAiOauth"]["emailAddress"] == "rawdev@example.com"
            await call.done_writing()

    asyncio.run(scenario())


# -- Malformed public key -----------------------------------------------------
def test_malformed_public_key_is_refused(app_env, monkeypatch):
    # Even with the raw fallback available, a bad key is refused (not downgraded).
    monkeypatch.setenv("AMX_ALLOW_RAW_KEK", "1")
    signer = signing.Signer.from_env_or_generate()
    tenant_id, _account_id, server_id = _seed("badkey@example.com")
    token = _issue_enroll(tenant_id, server_id)

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            stub = pb_grpc.AmxControlPlaneStub(channel)
            call = stub.Session()
            await call.write(
                pb.AmaMessage(
                    register=pb.Register(
                        agent_id=AGENT_ID,
                        enroll_token=token,
                        agent_public_key=b"\x01" * 31,  # wrong length
                    )
                )
            )
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await _read(call)
            assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT

    asyncio.run(scenario())


# -- Unit-level: wrap_kek never downgrades a capable agent --------------------
def test_wrap_kek_unit_behaviour():
    kek = signing.new_kek()
    agent_sk = PrivateKey.generate()
    agent_pk = bytes(agent_sk.public_key)

    # Present key -> always sealed, even when allow_raw is True.
    wrapped = signing.wrap_kek(kek, agent_pk, allow_raw=True)
    assert wrapped != kek
    assert SealedBox(agent_sk).decrypt(wrapped) == kek

    # No key + allow_raw -> raw KEK.
    assert signing.wrap_kek(kek, b"", allow_raw=True) == kek

    # No key + not allowed -> refuse.
    with pytest.raises(signing.RawKekNotAllowed):
        signing.wrap_kek(kek, b"", allow_raw=False)

    # Malformed key -> refuse, regardless of allow_raw.
    with pytest.raises(signing.InvalidAgentPublicKey):
        signing.wrap_kek(kek, b"\x02" * 33, allow_raw=True)
    with pytest.raises(signing.InvalidAgentPublicKey):
        signing.wrap_kek(kek, b"\x02" * 31, allow_raw=False)
