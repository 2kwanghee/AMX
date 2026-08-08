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


def _add_server(tenant_id: uuid.UUID, name: str) -> uuid.UUID:
    with get_sessionmaker()() as db:
        server = inventory.create_server(
            db, tenant_id, name=name, hostname="h", switch_mode="auto"
        )
        return server.id


def _assign(tenant_id, account_id, server_id, state: str = "active"):
    """Create an assignment and force it into ``state`` (default ``active``).

    Ownership for re-sync requires a state where the credential is resident
    locally (active/inactive/quarantined); the bare create leaves it ``pending``.
    """
    assignment_id = _create_assignment(tenant_id, account_id, server_id)
    _set_assignment_state(tenant_id, assignment_id, state)
    return assignment_id


def _set_assignment_state(tenant_id, assignment_id, state: str) -> None:
    with get_sessionmaker()() as db:
        inventory.get_assignment(db, tenant_id, assignment_id).state = state
        db.commit()


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
    # The account must be installed on THIS server for a re-sync to be accepted
    # (ownership, §5.7): the agent only re-seals a credential it legitimately holds.
    _assign(tenant_id, account_id, server_id)
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
    # Both accounts are installed on this server so the ownership gate lets the
    # re-syncs through; the test isolates monotonicity, not ownership.
    _assign(tenant_id, account_id, server_id)
    _assign(tenant_id, sentinel_id, server_id)
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
    # A owns account_a on server_a; account_b is never assigned to server_a, so the
    # cross-tenant push fails the ownership gate as well as the tenant gate.
    _assign(tenant_a, account_a, server_a)
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
    _assign(tenant_id, account_id, server_id)
    _assign(tenant_id, sentinel_id, server_id)
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
    # Active while owned so the re-sync push is accepted (ownership gate).
    assignment_id = _assign(tenant_id, account_id, server_id)
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

            # A fresh (re)deliver requires a 'pending' assignment; move it back so the
            # deliver command re-reads and carries the just-re-synced copy.
            await asyncio.to_thread(
                _set_assignment_state, tenant_id, assignment_id, "pending"
            )
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


def test_cred_update_foreign_server_account_rejected(app_env):
    """Fix 1: a session may only re-seal a credential its OWN server holds.

    Same tenant, but the target account is installed on a DIFFERENT server. The
    session (enrolled on server_own) pushes a set sealed under its own KEK for that
    account; without the ownership gate the AAD re-derives from the session agent_id
    and the write would succeed, overwriting a sibling server's account. It must be
    refused, and the at-rest secret must stand.
    """
    signer = signing.Signer.from_env_or_generate()
    tenant_id, account_id, server_own = _seed_tenant_account_server("owner@ex.com")
    server_other = _add_server(tenant_id, "s-other-" + uuid.uuid4().hex[:8])
    # The account lives on the OTHER server, never on this session's server.
    _assign(tenant_id, account_id, server_other)
    original_rt = _decrypt_rt(tenant_id, account_id)
    # A sentinel account owned by THIS server fences the rejected push.
    sentinel_id = _add_account(tenant_id, "owner-sentinel@ex.com", "rt-sentinel-orig")
    _assign(tenant_id, sentinel_id, server_own)
    token = _issue_enroll(tenant_id, server_own)

    t = datetime.now(UTC)
    ts = t + timedelta(seconds=5)

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            call, kek, key_id = await _open_session(channel, token)
            enc = _seal(
                kek, key_id, account_id, AGENT_ID,
                _oauth_secret("owner@ex.com", "rt-attacker"),
            )
            await call.write(_cred_update(account_id, enc, "", t))
            enc_s = _seal(
                kek, key_id, sentinel_id, AGENT_ID,
                _oauth_secret("owner-sentinel@ex.com", "rt-sentinel-new"),
            )
            await call.write(_cred_update(sentinel_id, enc_s, "", ts))
            await asyncio.to_thread(_wait_observed, tenant_id, sentinel_id, ts)
            await call.done_writing()

    asyncio.run(scenario())
    # Foreign-server account untouched; the session's own account was updatable.
    assert _decrypt_rt(tenant_id, account_id) == original_rt
    assert _stored(tenant_id, account_id).credential_observed_at is None
    assert _decrypt_rt(tenant_id, sentinel_id) == "rt-sentinel-new"


def test_cred_update_future_observed_at_rejected_no_lockin(app_env):
    """Fix 2: a far-future observed_at is refused, so it cannot pin the ratchet.

    A push stamped +10 years is rejected by the skew clamp; a normal push then a
    later (still-valid) push both apply, proving the future stamp never locked the
    account and honest rotation still flows.
    """
    signer = signing.Signer.from_env_or_generate()
    tenant_id, account_id, server_id = _seed_tenant_account_server("future@ex.com")
    _assign(tenant_id, account_id, server_id)
    original_rt = _decrypt_rt(tenant_id, account_id)
    token = _issue_enroll(tenant_id, server_id)

    far_future = datetime.now(UTC) + timedelta(days=3650)
    normal = datetime.now(UTC)
    later = normal + timedelta(seconds=30)  # newer, still within the skew bound

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            call, kek, key_id = await _open_session(channel, token)
            # 1) Far-future stamp -> rejected by the clamp (no store, no ratchet).
            enc_future = _seal(
                kek, key_id, account_id, AGENT_ID,
                _oauth_secret("future@ex.com", "rt-future"),
            )
            await call.write(_cred_update(account_id, enc_future, "", far_future))
            # 2) Normal stamp -> applied.
            enc_normal = _seal(
                kek, key_id, account_id, AGENT_ID,
                _oauth_secret("future@ex.com", "rt-normal"),
            )
            await call.write(_cred_update(account_id, enc_normal, "", normal))
            await asyncio.to_thread(_wait_observed, tenant_id, account_id, normal)
            # 3) A later honest rotation still applies (future stamp did not stick).
            enc_later = _seal(
                kek, key_id, account_id, AGENT_ID,
                _oauth_secret("future@ex.com", "rt-later"),
            )
            await call.write(_cred_update(account_id, enc_later, "", later))
            await asyncio.to_thread(_wait_observed, tenant_id, account_id, later)
            await call.done_writing()

    asyncio.run(scenario())
    assert original_rt != "rt-later"
    # The later rotation won; observed_at is the later stamp, not +10 years.
    assert _decrypt_rt(tenant_id, account_id) == "rt-later"
    observed = _stored(tenant_id, account_id).credential_observed_at
    assert abs((observed - later).total_seconds()) < 1e-3


def test_cred_update_non_utf8_rejected_session_survives(app_env):
    """Fix 3: a self-sealed non-UTF-8 credential is rejected opaquely, not fatally.

    The bytes authenticate (correct KEK + AAD) but are not decodable text; the
    decode must be caught and turned into an opaque reject rather than an
    exception that unwinds the session read loop. A sentinel pushed afterwards
    must still be processed (the session survived).
    """
    signer = signing.Signer.from_env_or_generate()
    tenant_id, account_id, server_id = _seed_tenant_account_server("utf8@ex.com")
    _assign(tenant_id, account_id, server_id)
    original_rt = _decrypt_rt(tenant_id, account_id)
    sentinel_id = _add_account(tenant_id, "utf8-sentinel@ex.com", "rt-sentinel-orig")
    _assign(tenant_id, sentinel_id, server_id)
    token = _issue_enroll(tenant_id, server_id)

    t = datetime.now(UTC)
    ts = t + timedelta(seconds=5)

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            call, kek, key_id = await _open_session(channel, token)
            # Seal raw invalid-UTF-8 bytes under the correct KEK and AAD: opens and
            # authenticates, but .decode() would raise.
            bad = b"\xff\xfe\x00\x80not utf-8"
            ciphertext, nonce = signing.seal_credential(
                kek, bad, ams_account_id=str(account_id), agent_id=AGENT_ID
            )
            enc_bad = pb.EncryptedCredential(
                algorithm=pb.ENCRYPTION_ALGORITHM_AES_256_GCM,
                ciphertext=ciphertext,
                nonce=nonce,
                key_id=key_id,
                aad_ams_account_id=str(account_id),
                aad_agent_id=AGENT_ID,
            )
            await call.write(_cred_update(account_id, enc_bad, "", t))
            # Sentinel proves the session read loop survived the bad decode.
            enc_s = _seal(
                kek, key_id, sentinel_id, AGENT_ID,
                _oauth_secret("utf8-sentinel@ex.com", "rt-sentinel-new"),
            )
            await call.write(_cred_update(sentinel_id, enc_s, "", ts))
            await asyncio.to_thread(_wait_observed, tenant_id, sentinel_id, ts)
            await call.done_writing()

    asyncio.run(scenario())
    # Bad push rejected, account intact; sentinel processed => session alive.
    assert _decrypt_rt(tenant_id, account_id) == original_rt
    assert _stored(tenant_id, account_id).credential_observed_at is None
    assert _decrypt_rt(tenant_id, sentinel_id) == "rt-sentinel-new"


def test_cred_update_pending_assignment_rejected(app_env):
    """Fix 1 (narrowed): a still-``pending`` assignment does not confer ownership.

    A pending assignment means the account is targeted at this server but the
    credential has not been delivered yet — the server holds no local copy, so a
    re-sync for it is not legitimate and is refused. A live (active) sentinel on
    the same server fences the push and proves the gate is state-, not merely
    server-, scoped.
    """
    signer = signing.Signer.from_env_or_generate()
    tenant_id, account_id, server_id = _seed_tenant_account_server("pending@ex.com")
    # Bare create leaves the assignment in 'pending' (pre-deliver, no local copy).
    _create_assignment(tenant_id, account_id, server_id)
    original_rt = _decrypt_rt(tenant_id, account_id)
    sentinel_id = _add_account(tenant_id, "pending-sentinel@ex.com", "rt-sentinel-orig")
    _assign(tenant_id, sentinel_id, server_id)  # active -> owned
    token = _issue_enroll(tenant_id, server_id)

    t = datetime.now(UTC)
    ts = t + timedelta(seconds=5)

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            call, kek, key_id = await _open_session(channel, token)
            enc = _seal(
                kek, key_id, account_id, AGENT_ID,
                _oauth_secret("pending@ex.com", "rt-premature"),
            )
            await call.write(_cred_update(account_id, enc, "", t))
            enc_s = _seal(
                kek, key_id, sentinel_id, AGENT_ID,
                _oauth_secret("pending-sentinel@ex.com", "rt-sentinel-new"),
            )
            await call.write(_cred_update(sentinel_id, enc_s, "", ts))
            await asyncio.to_thread(_wait_observed, tenant_id, sentinel_id, ts)
            await call.done_writing()

    asyncio.run(scenario())
    # Pending account not updatable; the active sentinel was.
    assert _decrypt_rt(tenant_id, account_id) == original_rt
    assert _stored(tenant_id, account_id).credential_observed_at is None
    assert _decrypt_rt(tenant_id, sentinel_id) == "rt-sentinel-new"
