"""O9 upstream credential re-sync tests (design §5.7, R3 — credential flow,
decryption, monotonicity, concurrency).

The client plays the AMA agent: it opens the SessionSetup KEK, seals a refreshed
credential set under it (AAD = ams_account_id‖agent_id, same envelope as
DeliverAccount), and pushes it upstream as ``AmaMessage.cred_update``. AMS must
open it, re-encrypt under the at-rest Fernet key, and store it — honouring tenant
ownership, observed_at monotonicity, and opaque failure.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from google.protobuf.timestamp_pb2 import Timestamp

from app.core import crypto
from app.db import get_sessionmaker
from app.grpc import signing
from app.grpc.proto import pb, pb_grpc
from app.services import commands, inventory

from tests.test_grpc_channel import (
    AGENT_ID,
    _Harness,
    _create_assignment,
    _issue_enroll,
    _read,
    _rest_deliver,
    _seed_tenant_account_server,
)


def _oauth_secret(email: str, refresh_token: str) -> str:
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "at-" + uuid.uuid4().hex,
                "refreshToken": refresh_token,
                "scopes": ["user:inference"],
                "emailAddress": email,
                "organizationName": "Acme",
            }
        }
    )


def _add_account(tenant_id: uuid.UUID, email: str, refresh_token: str) -> uuid.UUID:
    with get_sessionmaker()() as db:
        account = inventory.create_account(
            db,
            tenant_id,
            email=email,
            credential_type="oauth",
            secret=_oauth_secret(email, refresh_token),
        )
        return account.id


def _ts(dt: datetime) -> Timestamp:
    ts = Timestamp()
    ts.FromDatetime(dt)
    return ts


def _seal(kek: bytes, key_id: str, account_id: uuid.UUID, agent_id: str, secret_json: str):
    ciphertext, nonce = signing.seal_credential(
        kek, secret_json.encode(), ams_account_id=str(account_id), agent_id=agent_id
    )
    return pb.EncryptedCredential(
        algorithm=pb.ENCRYPTION_ALGORITHM_AES_256_GCM,
        ciphertext=ciphertext,
        nonce=nonce,
        key_id=key_id,
        aad_ams_account_id=str(account_id),
        aad_agent_id=agent_id,
    )


def _cred_update(account_id, encrypted, server_credential, observed_at) -> pb.AmaMessage:
    return pb.AmaMessage(
        cred_update=pb.CredentialUpdate(
            account=pb.AccountRef(ams_account_id=str(account_id)),
            encrypted_credential=encrypted,
            server_credential=server_credential,
            observed_at=_ts(observed_at),
        )
    )


def _stored(tenant_id, account_id):
    with get_sessionmaker()() as db:
        return inventory.get_account(db, tenant_id, account_id)


def _decrypt_rt(tenant_id, account_id) -> str:
    account = _stored(tenant_id, account_id)
    payload = json.loads(crypto.decrypt_secret(account.encrypted_secret))
    return payload["claudeAiOauth"]["refreshToken"]


def _wait_observed(tenant_id, account_id, expected, timeout=8.0):
    """Poll until credential_observed_at reaches ``expected`` (aware datetimes)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed = _stored(tenant_id, account_id).credential_observed_at
        if observed is not None and abs((observed - expected).total_seconds()) < 1e-3:
            return observed
        time.sleep(0.05)
    return _stored(tenant_id, account_id).credential_observed_at


async def _open_session(channel, token, agent_id=AGENT_ID):
    """Register, drain SessionSetup + decision-5 re-assertion, return (call, kek, key_id)."""
    stub = pb_grpc.AmxControlPlaneStub(channel)
    call = stub.Session()
    await call.write(
        pb.AmaMessage(register=pb.Register(agent_id=agent_id, enroll_token=token))
    )
    setup = await _read(call)
    assert setup.WhichOneof("cmd") == "session_setup"
    kek = setup.session_setup.keys[0].wrapped_key
    key_id = setup.session_setup.keys[0].key_id
    assert (await _read(call)).WhichOneof("cmd") == "set_mode"
    assert (await _read(call)).WhichOneof("cmd") == "set_policy"
    return call, kek, key_id


# -- Tests --------------------------------------------------------------------
def test_cred_update_reencrypts_and_stores_latest(app_env):
    signer = signing.Signer.from_env_or_generate()
    tenant_id, account_id, server_id = _seed_tenant_account_server("resync@example.com")
    token = _issue_enroll(tenant_id, server_id)
    observed = datetime.now(UTC)
    new_rt = "rt-refreshed-" + uuid.uuid4().hex

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            call, kek, key_id = await _open_session(channel, token)
            enc = _seal(
                kek, key_id, account_id, AGENT_ID,
                _oauth_secret("resync@example.com", new_rt),
            )
            await call.write(_cred_update(account_id, enc, "", observed))
            await asyncio.to_thread(_wait_observed, tenant_id, account_id, observed)
            await call.done_writing()

    asyncio.run(scenario())
    # Re-encrypted under Fernet (opens with AMX_ENCRYPTION_KEY) and carries the
    # refreshed token; observed_at recorded.
    assert _decrypt_rt(tenant_id, account_id) == new_rt
    assert _stored(tenant_id, account_id).credential_observed_at is not None


def test_cred_update_monotonic_ignores_stale(app_env):
    signer = signing.Signer.from_env_or_generate()
    tenant_id, account_id, server_id = _seed_tenant_account_server("mono@example.com")
    # A second account in the same tenant acts as an ordering sentinel: once it
    # updates, the earlier stale push to account_id has already been processed.
    sentinel_id = _add_account(tenant_id, "sentinel@example.com", "rt-sentinel-orig")
    token = _issue_enroll(tenant_id, server_id)

    t1 = datetime.now(UTC)
    t0 = t1 - timedelta(seconds=120)  # strictly older -> must be ignored
    ts = t1 + timedelta(seconds=5)  # sentinel time on the other account
    rt1 = "rt-t1-" + uuid.uuid4().hex
    rt_stale = "rt-stale-" + uuid.uuid4().hex

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            call, kek, key_id = await _open_session(channel, token)
            # Establish the current copy at t1.
            enc1 = _seal(kek, key_id, account_id, AGENT_ID, _oauth_secret("mono@example.com", rt1))
            await call.write(_cred_update(account_id, enc1, "", t1))
            await asyncio.to_thread(_wait_observed, tenant_id, account_id, t1)
            # Stale push (older observed_at) — must not overwrite.
            enc_stale = _seal(
                kek, key_id, account_id, AGENT_ID, _oauth_secret("mono@example.com", rt_stale)
            )
            await call.write(_cred_update(account_id, enc_stale, "", t0))
            # Sentinel on the other account; when it lands, the stale push is done.
            enc_s = _seal(
                kek, key_id, sentinel_id, AGENT_ID,
                _oauth_secret("sentinel@example.com", "rt-sentinel-new"),
            )
            await call.write(_cred_update(sentinel_id, enc_s, "", ts))
            await asyncio.to_thread(_wait_observed, tenant_id, sentinel_id, ts)
            await call.done_writing()

    asyncio.run(scenario())
    # Stale push ignored: the t1 copy stands, observed_at unmoved.
    assert _decrypt_rt(tenant_id, account_id) == rt1
    observed = _stored(tenant_id, account_id).credential_observed_at
    assert abs((observed - t1).total_seconds()) < 1e-3


def test_cred_update_cross_tenant_account_rejected(app_env):
    signer = signing.Signer.from_env_or_generate()
    tenant_a, account_a, server_a = _seed_tenant_account_server("a@ex.com")
    tenant_b, account_b, _server_b = _seed_tenant_account_server("b@ex.com")
    original_b_rt = _decrypt_rt(tenant_b, account_b)
    token = _issue_enroll(tenant_a, server_a)

    t = datetime.now(UTC)
    ts = t + timedelta(seconds=5)

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            call, kek, key_id = await _open_session(channel, token)
            # A's session pushes an update naming B's account id (cross-tenant).
            enc_b = _seal(
                kek, key_id, account_b, AGENT_ID, _oauth_secret("b@ex.com", "rt-attacker")
            )
            await call.write(_cred_update(account_b, enc_b, "", t))
            # Sentinel on A's own account to fence the cross-tenant push.
            enc_a = _seal(
                kek, key_id, account_a, AGENT_ID, _oauth_secret("a@ex.com", "rt-a-new")
            )
            await call.write(_cred_update(account_a, enc_a, "", ts))
            await asyncio.to_thread(_wait_observed, tenant_a, account_a, ts)
            await call.done_writing()

    asyncio.run(scenario())
    # B untouched (unknown-to-tenant rejection); A's own account updated.
    assert _decrypt_rt(tenant_b, account_b) == original_b_rt
    assert _stored(tenant_b, account_b).credential_observed_at is None
    assert _decrypt_rt(tenant_a, account_a) == "rt-a-new"


def test_cred_update_bad_aad_not_applied(app_env):
    signer = signing.Signer.from_env_or_generate()
    tenant_id, account_id, server_id = _seed_tenant_account_server("aad@example.com")
    original_rt = _decrypt_rt(tenant_id, account_id)
    sentinel_id = _add_account(tenant_id, "aad-sentinel@example.com", "rt-sentinel-orig")
    token = _issue_enroll(tenant_id, server_id)

    t = datetime.now(UTC)
    ts = t + timedelta(seconds=5)

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            call, kek, key_id = await _open_session(channel, token)
            # Seal under a DIFFERENT agent_id -> AAD mismatch -> auth fails at open.
            enc_bad = _seal(
                kek, key_id, account_id, "ama_other",
                _oauth_secret("aad@example.com", "rt-forged"),
            )
            await call.write(_cred_update(account_id, enc_bad, "", t))
            # Sentinel fences the failed push; session must survive the failure.
            enc_s = _seal(
                kek, key_id, sentinel_id, AGENT_ID,
                _oauth_secret("aad-sentinel@example.com", "rt-sentinel-new"),
            )
            await call.write(_cred_update(sentinel_id, enc_s, "", ts))
            await asyncio.to_thread(_wait_observed, tenant_id, sentinel_id, ts)
            await call.done_writing()

    asyncio.run(scenario())
    # Decryption/auth failure -> no update, no observed_at, secret intact.
    assert _decrypt_rt(tenant_id, account_id) == original_rt
    assert _stored(tenant_id, account_id).credential_observed_at is None


def test_deliver_after_resync_sends_latest(app_env):
    signer = signing.Signer.from_env_or_generate()
    tenant_id, account_id, server_id = _seed_tenant_account_server("deliver-latest@example.com")
    token = _issue_enroll(tenant_id, server_id)
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    observed = datetime.now(UTC)
    new_rt = "rt-latest-" + uuid.uuid4().hex

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            call, kek, key_id = await _open_session(channel, token)
            enc = _seal(
                kek, key_id, account_id, AGENT_ID,
                _oauth_secret("deliver-latest@example.com", new_rt),
            )
            await call.write(_cred_update(account_id, enc, "", observed))
            await asyncio.to_thread(_wait_observed, tenant_id, account_id, observed)

            # Now a cross-server style deliver must carry the refreshed copy.
            await asyncio.to_thread(_rest_deliver, tenant_id, assignment_id)
            cmd = await _read(call)
            assert cmd.WhichOneof("cmd") == "deliver"
            enc_out = cmd.deliver.encrypted_credential
            plaintext = signing.open_credential(
                kek, enc_out.ciphertext, enc_out.nonce,
                ams_account_id=str(account_id), agent_id=AGENT_ID,
            )
            await call.done_writing()
            return json.loads(plaintext)["claudeAiOauth"]["refreshToken"]

    assert asyncio.run(scenario()) == new_rt
