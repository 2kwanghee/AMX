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

from app.core import crypto, kek
from app.db import get_sessionmaker
from app.grpc import server as grpc_server
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
    with get_sessionmaker()() as db:
        account = inventory.get_account(db, tenant_id, account_id)
        payload = json.loads(
            crypto.decrypt_secret(account.encrypted_secret, tenant_id=tenant_id, db=db)
        )
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


def test_cred_update_empty_token_set_rejected(app_env, monkeypatch):
    """A logged-out credential shell must not overwrite the stored copy.

    The set authenticates, decodes, and is well-formed JSON — but every token in
    it is blank, so it is what an agent reads off disk after a local logout. The
    at-rest secret must stand and ``credential_observed_at`` must NOT advance
    (an advanced ratchet would block the recovered credential), the session must
    survive, and a valid set pushed afterwards must still be applied.
    """
    signer = signing.Signer.from_env_or_generate()
    tenant_id, account_id, server_id = _seed_tenant_account_server("empty@ex.com")
    _assign(tenant_id, account_id, server_id)
    original_rt = _decrypt_rt(tenant_id, account_id)
    sentinel_id = _add_account(tenant_id, "empty-sentinel@ex.com", "rt-sentinel-orig")
    _assign(tenant_id, sentinel_id, server_id)
    token = _issue_enroll(tenant_id, server_id)

    rejects: list[str] = []
    real_warning = grpc_server._logger.warning

    def spy_warning(msg, *args, **kwargs):
        rejects.append(msg % args if args else msg)
        return real_warning(msg, *args, **kwargs)

    monkeypatch.setattr(grpc_server._logger, "warning", spy_warning)

    empty_set = json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "",
                "refreshToken": "",
                "expiresAt": 0,
                "scopes": [],
            }
        }
    )
    t = datetime.now(UTC)
    ts = t + timedelta(seconds=5)
    later = ts + timedelta(seconds=5)
    recovered_rt = "rt-recovered-" + uuid.uuid4().hex

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            call, kek_bytes, key_id = await _open_session(channel, token)
            # 1) Token-less set -> rejected, nothing stored, ratchet unmoved.
            enc_empty = _seal(kek_bytes, key_id, account_id, AGENT_ID, empty_set)
            await call.write(_cred_update(account_id, enc_empty, "", t))
            # Sentinel fences the rejected push and proves the session survived.
            enc_s = _seal(
                kek_bytes, key_id, sentinel_id, AGENT_ID,
                _oauth_secret("empty-sentinel@ex.com", "rt-sentinel-new"),
            )
            await call.write(_cred_update(sentinel_id, enc_s, "", ts))
            await asyncio.to_thread(_wait_observed, tenant_id, sentinel_id, ts)
            # The sentinel fences the empty push: by now it has been processed, so
            # the account row must still hold the ORIGINAL secret with no ratchet.
            assert await asyncio.to_thread(_decrypt_rt, tenant_id, account_id) == original_rt
            assert (
                await asyncio.to_thread(_stored, tenant_id, account_id)
            ).credential_observed_at is None
            # 2) The recovered credential still lands (the reject left no ratchet).
            enc_ok = _seal(
                kek_bytes, key_id, account_id, AGENT_ID,
                _oauth_secret("empty@ex.com", recovered_rt),
            )
            await call.write(_cred_update(account_id, enc_ok, "", later))
            await asyncio.to_thread(_wait_observed, tenant_id, account_id, later)
            await call.done_writing()

    asyncio.run(scenario())

    # (a) session survived; (b) the recovered set was applied afterwards.
    assert _decrypt_rt(tenant_id, sentinel_id) == "rt-sentinel-new"
    assert original_rt != recovered_rt
    assert _decrypt_rt(tenant_id, account_id) == recovered_rt
    observed = _stored(tenant_id, account_id).credential_observed_at
    # observed_at is the recovered push's stamp, never the rejected one's.
    assert abs((observed - later).total_seconds()) < 1e-3
    # (c) a rejected line was logged for the empty push.
    assert any(
        "no token material" in m and str(account_id) in m for m in rejects
    )


def test_cred_update_setup_token_shape_accepted(app_env):
    """An accessToken-only set (a ``claude setup token`` account) still applies.

    The guard rejects only a token-less set, not a refresh-token-less one: enroll
    demands a refresh_token because it can answer the operator with a 400, while a
    silently dropped re-sync would strand this account's rotations forever.
    """
    signer = signing.Signer.from_env_or_generate()
    tenant_id, account_id, server_id = _seed_tenant_account_server("setuptok@ex.com")
    _assign(tenant_id, account_id, server_id)
    token = _issue_enroll(tenant_id, server_id)

    observed = datetime.now(UTC)
    access_only = json.dumps(
        {"claudeAiOauth": {"accessToken": "sk-ant-oat-" + uuid.uuid4().hex, "expiresAt": 0}}
    )

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            call, kek_bytes, key_id = await _open_session(channel, token)
            enc = _seal(kek_bytes, key_id, account_id, AGENT_ID, access_only)
            await call.write(_cred_update(account_id, enc, "", observed))
            await asyncio.to_thread(_wait_observed, tenant_id, account_id, observed)
            await call.done_writing()

    asyncio.run(scenario())
    with get_sessionmaker()() as db:
        account = inventory.get_account(db, tenant_id, account_id)
        stored = json.loads(
            crypto.decrypt_secret(account.encrypted_secret, tenant_id=tenant_id, db=db)
        )
    assert stored == json.loads(access_only)
    assert _stored(tenant_id, account_id).credential_observed_at is not None


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


def test_cred_update_encrypt_failure_session_survives(app_env, monkeypatch):
    """A1: a KEK/DEK failure at re-encrypt rejects the push, not the session.

    ``crypto.encrypt_secret`` runs after the credential has authenticated and
    decoded; a missing tenant DEK or a KEK-provider error raises ``KekError``
    there. Left uncaught it unwinds the session read loop and drops the whole
    agent stream. It must instead be an opaque per-update reject: the account
    stays untouched (the write never runs), a rejected line is logged, and a
    sentinel pushed afterwards still lands (the session survived).
    """
    signer = signing.Signer.from_env_or_generate()
    tenant_id, account_id, server_id = _seed_tenant_account_server("dekfail@ex.com")
    _assign(tenant_id, account_id, server_id)
    original_rt = _decrypt_rt(tenant_id, account_id)
    sentinel_id = _add_account(tenant_id, "dekfail-sentinel@ex.com", "rt-sentinel-orig")
    _assign(tenant_id, sentinel_id, server_id)
    token = _issue_enroll(tenant_id, server_id)

    # Inject a KEK-provider failure scoped to the target credential only, so the
    # sentinel still re-encrypts normally and proves the stream is alive.
    real_encrypt = crypto.encrypt_secret

    def flaky_encrypt(plaintext, *, tenant_id, db):
        if "rt-dek-missing" in plaintext:
            raise kek.KekError("tenant DEK unavailable (injected)")
        return real_encrypt(plaintext, tenant_id=tenant_id, db=db)

    monkeypatch.setattr(crypto, "encrypt_secret", flaky_encrypt)

    # Spy the reject log at the call site. The upstream loop runs in a worker
    # thread (``asyncio.to_thread``), so pytest's ``caplog`` does not reliably
    # capture it; wrapping the servicer's own logger.warning is deterministic.
    rejects: list[str] = []
    real_warning = grpc_server._logger.warning

    def spy_warning(msg, *args, **kwargs):
        rejects.append(msg % args if args else msg)
        return real_warning(msg, *args, **kwargs)

    monkeypatch.setattr(grpc_server._logger, "warning", spy_warning)

    t = datetime.now(UTC)
    ts = t + timedelta(seconds=5)

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            call, kek_bytes, key_id = await _open_session(channel, token)
            enc = _seal(
                kek_bytes, key_id, account_id, AGENT_ID,
                _oauth_secret("dekfail@ex.com", "rt-dek-missing"),
            )
            await call.write(_cred_update(account_id, enc, "", t))
            enc_s = _seal(
                kek_bytes, key_id, sentinel_id, AGENT_ID,
                _oauth_secret("dekfail-sentinel@ex.com", "rt-sentinel-new"),
            )
            await call.write(_cred_update(sentinel_id, enc_s, "", ts))
            await asyncio.to_thread(_wait_observed, tenant_id, sentinel_id, ts)
            await call.done_writing()

    asyncio.run(scenario())

    # (b) account row unchanged: original secret stands, no observed_at ratchet.
    assert _decrypt_rt(tenant_id, account_id) == original_rt
    assert _stored(tenant_id, account_id).credential_observed_at is None
    # (a) session survived: the sentinel pushed after the failure was applied.
    assert _decrypt_rt(tenant_id, sentinel_id) == "rt-sentinel-new"
    # (c) a rejected line was logged for the failed account.
    assert any(
        "at-rest encryption unavailable" in m and str(account_id) in m
        for m in rejects
    )


def test_cred_update_invalid_observed_at_session_survives(app_env, monkeypatch):
    """A1: an out-of-range observed_at Timestamp rejects the push, not the session.

    protobuf does not range-check Timestamp seconds on the wire, so a stamp like
    seconds=2**62 makes ``ToDatetime`` raise. Left uncaught it unwinds the session
    read loop and drops the whole agent stream. It must be an opaque per-update
    reject: the account is untouched (the parse fails before any DB access), a
    rejected line is logged, and a sentinel pushed afterwards still lands.
    """
    signer = signing.Signer.from_env_or_generate()
    tenant_id, account_id, server_id = _seed_tenant_account_server("badts@ex.com")
    _assign(tenant_id, account_id, server_id)
    original_rt = _decrypt_rt(tenant_id, account_id)
    sentinel_id = _add_account(tenant_id, "badts-sentinel@ex.com", "rt-sentinel-orig")
    _assign(tenant_id, sentinel_id, server_id)
    token = _issue_enroll(tenant_id, server_id)

    rejects: list[str] = []
    real_warning = grpc_server._logger.warning

    def spy_warning(msg, *args, **kwargs):
        rejects.append(msg % args if args else msg)
        return real_warning(msg, *args, **kwargs)

    monkeypatch.setattr(grpc_server._logger, "warning", spy_warning)

    ts_sentinel = datetime.now(UTC) + timedelta(seconds=5)

    def _bad_ts_cred_update(acc_id, encrypted) -> pb.AmaMessage:
        msg = pb.AmaMessage(
            cred_update=pb.CredentialUpdate(
                account=pb.AccountRef(ams_account_id=str(acc_id)),
                encrypted_credential=encrypted,
                server_credential="",
            )
        )
        # Assigning the scalar marks observed_at present; 2**62 overflows datetime
        # so ToDatetime raises (protobuf never range-checks it on the wire).
        msg.cred_update.observed_at.seconds = 2**62
        return msg

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            call, kek_bytes, key_id = await _open_session(channel, token)
            enc = _seal(
                kek_bytes, key_id, account_id, AGENT_ID,
                _oauth_secret("badts@ex.com", "rt-bad-ts"),
            )
            await call.write(_bad_ts_cred_update(account_id, enc))
            enc_s = _seal(
                kek_bytes, key_id, sentinel_id, AGENT_ID,
                _oauth_secret("badts-sentinel@ex.com", "rt-sentinel-new"),
            )
            await call.write(_cred_update(sentinel_id, enc_s, "", ts_sentinel))
            await asyncio.to_thread(_wait_observed, tenant_id, sentinel_id, ts_sentinel)
            await call.done_writing()

    asyncio.run(scenario())

    # (b) account row unchanged: original secret stands, no observed_at ratchet.
    assert _decrypt_rt(tenant_id, account_id) == original_rt
    assert _stored(tenant_id, account_id).credential_observed_at is None
    # (a) session survived: the sentinel pushed after the bad stamp was applied.
    assert _decrypt_rt(tenant_id, sentinel_id) == "rt-sentinel-new"
    # (c) a rejected line was logged for the failed account.
    assert any(
        "invalid observed_at" in m and str(account_id) in m for m in rejects
    )


# -- Judgement-table unit tests ------------------------------------------------
# These mirror, case-for-case and in the same order, the Go tables in
# ama-agent/internal/provider/claude/claude_test.go::TestHasCredentialMaterial and
# .../codex/codex_test.go::TestHasCredentialMaterial. The two implementations gate
# the SAME push from opposite ends, so a row where they disagree is a live bug: a
# Go True + an AMS False lets AMA advance its baseline on a push AMS refuses,
# stranding AMS on the stale copy with no retry. Keep the two tables in lockstep.
CLAUDE_MATERIAL_CASES = [
    ("both tokens", r'{"claudeAiOauth":{"accessToken":"a1","refreshToken":"r1"}}', True),
    ("access token only", r'{"claudeAiOauth":{"accessToken":"a1","refreshToken":""}}', True),
    ("refresh token only", r'{"claudeAiOauth":{"accessToken":"","refreshToken":"r1"}}', True),
    ("setup-token shape (no refreshToken key)", r'{"claudeAiOauth":{"accessToken":"sk-ant-oat-x"}}', True),
    ("empty token set", r'{"claudeAiOauth":{"accessToken":"","refreshToken":"","expiresAt":0}}', False),
    ("whitespace-only tokens", r'{"claudeAiOauth":{"accessToken":"  ","refreshToken":"\t\n"}}', False),
    # U+001C is blank to str.strip() but not to Go's unicode.IsSpace; both sides
    # use (space OR Cc), so this row reads False on both.
    ("control-char-only tokens", r'{"claudeAiOauth":{"accessToken":"\u001c","refreshToken":"\u0000"}}', False),
    ("null tokens", r'{"claudeAiOauth":{"accessToken":null,"refreshToken":null}}', False),
    ("one token key present and blank", r'{"claudeAiOauth":{"accessToken":""}}', False),
    # The three rows below were False before the 2026-08-17 review: an unknown
    # schema INSIDE claudeAiOauth is as unjudgeable as one outside it.
    ("unknown keys inside the block", r'{"claudeAiOauth":{"token":"abc","expiresAt":1}}', True),
    ("no token keys at all", r'{"claudeAiOauth":{"expiresAt":0}}', True),
    ("empty claudeAiOauth object", r'{"claudeAiOauth":{}}', True),
    ("no claudeAiOauth key", r'{"apiKey":"sk-xyz"}', True),
    ("empty object", r"{}", True),
    ("claudeAiOauth not an object", r'{"claudeAiOauth":"opaque"}', True),
    ("claudeAiOauth null", r'{"claudeAiOauth":null}', True),
    ("non-string token", r'{"claudeAiOauth":{"accessToken":123,"refreshToken":""}}', True),
    ("not JSON (opaque api key)", "sk-ant-api03-opaque", True),
    ("JSON array", "[1,2,3]", True),
    ("JSON string", '"just-a-string"', True),
    # A blank body cannot be any credential, opaque api_key included, so unlike the
    # non-JSON rows above it is refused rather than waved through.
    ("empty input", "", False),
    ("whitespace-only body", "   \n\t", False),
    ("control-char-only body", "\x00\x1f", False),
]

CODEX_MATERIAL_CASES = [
    ("full token set", r'{"auth_mode":"chatgpt","tokens":{"id_token":"i1","access_token":"a1","refresh_token":"r1","account_id":"acc1"}}', True),
    ("access token only", r'{"tokens":{"refresh_token":"","access_token":"a1"}}', True),
    ("refresh token only", r'{"tokens":{"refresh_token":"r1","access_token":""}}', True),
    ("empty token set", r'{"auth_mode":"chatgpt","tokens":{"refresh_token":"","access_token":"","id_token":""}}', False),
    ("whitespace-only tokens", r'{"tokens":{"refresh_token":" ","access_token":"\t"}}', False),
    ("control-char-only tokens", r'{"tokens":{"refresh_token":"\u001f","access_token":"\u0000"}}', False),
    ("null tokens values", r'{"tokens":{"refresh_token":null,"access_token":null}}', False),
    ("one token key present and blank", r'{"tokens":{"refresh_token":""}}', False),
    ("unknown keys inside tokens", r'{"tokens":{"token":"abc","account_id":"acc1"}}', True),
    ("no token keys at all", r'{"tokens":{"account_id":"acc1"}}', True),
    ("empty tokens object", r'{"tokens":{}}', True),
    ("empty tokens but api key", r'{"OPENAI_API_KEY":"sk-x","tokens":{"refresh_token":"","access_token":""}}', True),
    ("empty tokens and blank api key", r'{"OPENAI_API_KEY":"  ","tokens":{"refresh_token":"","access_token":""}}', False),
    ("api-key form (tokens null)", r'{"auth_mode":"apikey","OPENAI_API_KEY":"sk-x","tokens":null}', True),
    ("no tokens key", r'{"OPENAI_API_KEY":"sk-x"}', True),
    ("empty object", r"{}", True),
    ("non-string token", r'{"tokens":{"refresh_token":123,"access_token":""}}', True),
    ("not JSON", "not-json-at-all", True),
    ("JSON array", "[1,2,3]", True),
    ("empty input", "", False),
    ("whitespace-only body", "   \n\t", False),
    ("control-char-only body", "\x00\x1f", False),
]


@pytest.mark.parametrize(("name", "secret", "want"), CLAUDE_MATERIAL_CASES)
def test_credential_has_material_claude_table(name, secret, want):
    assert grpc_server._credential_has_material(secret, "claude") is want, name


@pytest.mark.parametrize(("name", "secret", "want"), CODEX_MATERIAL_CASES)
def test_credential_has_material_codex_table(name, secret, want):
    assert grpc_server._credential_has_material(secret, "codex") is want, name


def test_credential_has_material_unknown_provider_passes():
    """No schema to judge against -> conservative pass. Python-only: the Go side
    has one driver per provider and so has no such fallback."""
    empty_claude_set = r'{"claudeAiOauth":{"accessToken":"","refreshToken":""}}'
    assert grpc_server._credential_has_material(empty_claude_set, "claude") is False
    assert grpc_server._credential_has_material(empty_claude_set, "gemini") is True
    # ...but a blank body is refused for every provider: that check precedes the
    # provider switch.
    assert grpc_server._credential_has_material("  ", "gemini") is False


def test_credential_has_material_survives_pathological_json():
    """A parse blow-up degrades to "cannot judge" instead of escaping into the
    session read loop (deep nesting raises RecursionError inside json.loads)."""
    assert grpc_server._credential_has_material("[" * 60000 + "]" * 60000, "claude") is True
    assert grpc_server._credential_has_material('{"a":' * 5000 + "1" + "}" * 5000, "codex") is True


def test_is_blank_control_and_space_definition():
    """The blank definition is (whitespace OR Cc) — the parity contract with
    ama-agent's isBlankCredential. U+001C-U+001F are exactly the characters
    str.strip() and Go's unicode.IsSpace disagree about, so they are pinned here.
    """
    assert grpc_server._is_blank("") is True
    assert grpc_server._is_blank(" \t\r\n\v\f") is True
    assert grpc_server._is_blank("\x1c\x1d\x1e\x1f") is True
    assert grpc_server._is_blank("\x00\x08\x7f\x85") is True
    assert grpc_server._is_blank(" 　") is True  # Unicode spaces
    assert grpc_server._is_blank("a") is False
    assert grpc_server._is_blank(" \x1c x") is False
    assert grpc_server._is_blank("\u200b") is False  # ZWSP is Cf, neither space nor Cc


def test_token_material_present_and_material_split():
    """``present`` separates "unknown schema" (no token key at all -> pass) from
    "logged out" (a token key present but blank -> reject)."""
    assert grpc_server._token_material({}, "a", "b") == (False, False)
    assert grpc_server._token_material({"c": "x"}, "a", "b") == (False, False)
    assert grpc_server._token_material({"a": ""}, "a", "b") == (True, False)
    assert grpc_server._token_material({"a": None}, "a", "b") == (True, False)
    assert grpc_server._token_material({"a": "", "b": "x"}, "a", "b") == (True, True)
    # A non-string value is unjudgeable, so it counts as material.
    assert grpc_server._token_material({"a": 123}, "a", "b") == (True, True)
    assert grpc_server._token_material({"a": ["x"]}, "a", "b") == (True, True)


# -- Metadata refresh (§5.7): the row's expiry/scopes must track the rotation ---
def _oauth_secret_meta(email: str, refresh_token: str, **oauth: object) -> str:
    """`_oauth_secret` plus explicit claudeAiOauth metadata fields."""
    payload = json.loads(_oauth_secret(email, refresh_token))
    payload["claudeAiOauth"].update(oauth)
    return json.dumps(payload)


def _push(kek, key_id, account_id, secret_json, observed):
    return _cred_update(
        account_id, _seal(kek, key_id, account_id, AGENT_ID, secret_json), "", observed
    )


def test_cred_update_refreshes_expiry_and_scopes(app_env):
    """The point of this change: a rotation carries a new expiry, and the row
    must adopt it. Before, only encrypted_secret/secret_masked/observed_at moved,
    so credential_expires_at kept the enrolment-time value and the console
    eventually showed a live credential as expired."""
    signer = signing.Signer.from_env_or_generate()
    tenant_id, account_id, server_id = _seed_tenant_account_server("meta@example.com")
    _assign(tenant_id, account_id, server_id)
    token = _issue_enroll(tenant_id, server_id)

    before = _stored(tenant_id, account_id)
    assert before.credential_expires_at is None  # enrolment set carries no expiresAt
    assert before.scopes == ["user:inference"]

    observed = datetime.now(UTC)
    expires = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=8)
    secret = _oauth_secret_meta(
        "meta@example.com",
        "rt-meta-" + uuid.uuid4().hex,
        expiresAt=int(expires.timestamp() * 1000),
        scopes=["user:inference", "user:profile"],
        accountUuid="acc-uuid-1234",
        organizationName="Rotated Org",
    )

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            call, kek, key_id = await _open_session(channel, token)
            await call.write(_push(kek, key_id, account_id, secret, observed))
            await asyncio.to_thread(_wait_observed, tenant_id, account_id, observed)
            await call.done_writing()

    asyncio.run(scenario())
    row = _stored(tenant_id, account_id)
    assert abs((row.credential_expires_at - expires).total_seconds()) < 1e-3
    assert row.scopes == ["user:inference", "user:profile"]
    assert row.account_uuid == "acc-uuid-1234"
    assert row.organization_name == "Rotated Org"


def test_cred_update_unparsable_secret_preserves_metadata(app_env):
    """Extraction is best-effort: an opaque api_key body has no metadata to lift,
    so the columns keep what the last parsable set put there rather than being
    blanked."""
    signer = signing.Signer.from_env_or_generate()
    tenant_id, account_id, server_id = _seed_tenant_account_server("opaque@example.com")
    _assign(tenant_id, account_id, server_id)
    token = _issue_enroll(tenant_id, server_id)

    t1 = datetime.now(UTC)
    t2 = t1 + timedelta(seconds=5)
    expires = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=3)
    rich = _oauth_secret_meta(
        "opaque@example.com",
        "rt-rich-" + uuid.uuid4().hex,
        expiresAt=int(expires.timestamp() * 1000),
        scopes=["user:inference", "user:profile"],
        accountUuid="acc-uuid-keepme",
        organizationName="Keep Org",
    )
    opaque = "sk-ant-api03-" + uuid.uuid4().hex  # not JSON: nothing to lift

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            call, kek, key_id = await _open_session(channel, token)
            await call.write(_push(kek, key_id, account_id, rich, t1))
            await asyncio.to_thread(_wait_observed, tenant_id, account_id, t1)
            await call.write(_push(kek, key_id, account_id, opaque, t2))
            await asyncio.to_thread(_wait_observed, tenant_id, account_id, t2)
            await call.done_writing()

    asyncio.run(scenario())
    row = _stored(tenant_id, account_id)
    # The opaque push WAS applied (observed_at advanced)...
    assert abs((row.credential_observed_at - t2).total_seconds()) < 1e-3
    # ...but it left every metadata column exactly as the rich set had it.
    assert abs((row.credential_expires_at - expires).total_seconds()) < 1e-3
    assert row.scopes == ["user:inference", "user:profile"]
    assert row.account_uuid == "acc-uuid-keepme"
    assert row.organization_name == "Keep Org"


def test_cred_update_stale_push_does_not_move_metadata(app_env):
    """Regression guard for atomicity: metadata rides in the same conditional
    UPDATE as the secret, so a push the observed_at ratchet rejects must not
    repaint the newer row's expiry/scopes either."""
    signer = signing.Signer.from_env_or_generate()
    tenant_id, account_id, server_id = _seed_tenant_account_server("stalemeta@example.com")
    sentinel_id = _add_account(tenant_id, "stalemeta-sentinel@example.com", "rt-sentinel-orig")
    _assign(tenant_id, account_id, server_id)
    _assign(tenant_id, sentinel_id, server_id)
    token = _issue_enroll(tenant_id, server_id)

    t1 = datetime.now(UTC)
    t0 = t1 - timedelta(seconds=120)  # strictly older -> must be ignored
    ts = t1 + timedelta(seconds=5)
    fresh_exp = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=6)
    stale_exp = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=6)
    fresh = _oauth_secret_meta(
        "stalemeta@example.com",
        "rt-fresh-" + uuid.uuid4().hex,
        expiresAt=int(fresh_exp.timestamp() * 1000),
        scopes=["user:inference", "user:profile"],
        organizationName="Fresh Org",
    )
    stale = _oauth_secret_meta(
        "stalemeta@example.com",
        "rt-stale-" + uuid.uuid4().hex,
        expiresAt=int(stale_exp.timestamp() * 1000),
        scopes=["stale:scope"],
        organizationName="Stale Org",
    )

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            call, kek, key_id = await _open_session(channel, token)
            await call.write(_push(kek, key_id, account_id, fresh, t1))
            await asyncio.to_thread(_wait_observed, tenant_id, account_id, t1)
            await call.write(_push(kek, key_id, account_id, stale, t0))
            # Sentinel on the other account fences the stale push.
            await call.write(
                _push(
                    kek, key_id, sentinel_id,
                    _oauth_secret("stalemeta-sentinel@example.com", "rt-sentinel-new"),
                    ts,
                )
            )
            await asyncio.to_thread(_wait_observed, tenant_id, sentinel_id, ts)
            await call.done_writing()

    asyncio.run(scenario())
    row = _stored(tenant_id, account_id)
    assert abs((row.credential_observed_at - t1).total_seconds()) < 1e-3
    assert abs((row.credential_expires_at - fresh_exp).total_seconds()) < 1e-3
    assert row.scopes == ["user:inference", "user:profile"]
    assert row.organization_name == "Fresh Org"


def test_cred_update_unstorable_metadata_keeps_session_alive(app_env):
    """A metadata field an agent can send but PostgreSQL cannot store must be
    dropped, not written.

    NUL and a lone surrogate are the two shapes that reach here intact: JSON
    spells both with ASCII backslash-u escapes, so the UTF-8 decode and the
    token-material guard upstream see nothing unusual, and the write then fails
    inside the driver. Unhandled that ends the session — and the agent re-kills
    it on every reconnect, so deliver/recall for that server stops — while the
    driver's exception would carry the statement's bound parameters (the at-rest
    ciphertext, the mask) into a gRPC status detail (§7).
    """
    signer = signing.Signer.from_env_or_generate()
    tenant_id, account_id, server_id = _seed_tenant_account_server("unstorable@example.com")
    _assign(tenant_id, account_id, server_id)
    token = _issue_enroll(tenant_id, server_id)

    t1 = datetime.now(UTC)
    t2 = t1 + timedelta(seconds=5)
    t3 = t1 + timedelta(seconds=10)
    after_rt = "rt-after-" + uuid.uuid4().hex
    nul = _oauth_secret_meta(
        "unstorable@example.com", "rt-nul-" + uuid.uuid4().hex, organizationName="a\x00b"
    )
    surrogate = _oauth_secret_meta(
        "unstorable@example.com", "rt-sur-" + uuid.uuid4().hex, organizationName="\ud800"
    )
    assert "\\u0000" in nul and "\\ud800" in surrogate  # pure ASCII on the wire
    original_org = _stored(tenant_id, account_id).organization_name

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            call, kek, key_id = await _open_session(channel, token)
            # Each push must be APPLIED, not merely survived: observed_at
            # advancing is what separates "sanitised, then stored" from "the
            # write blew up and the last-resort handler swallowed it".
            await call.write(_push(kek, key_id, account_id, nul, t1))
            landed = await asyncio.to_thread(_wait_observed, tenant_id, account_id, t1)
            assert abs((landed - t1).total_seconds()) < 1e-3, "NUL push was not stored"
            await call.write(_push(kek, key_id, account_id, surrogate, t2))
            landed = await asyncio.to_thread(_wait_observed, tenant_id, account_id, t2)
            assert abs((landed - t2).total_seconds()) < 1e-3, "surrogate push was not stored"
            # The session is still live and still accepting traffic.
            await call.write(
                _push(
                    kek, key_id, account_id,
                    _oauth_secret("unstorable@example.com", after_rt),
                    t3,
                )
            )
            await asyncio.to_thread(_wait_observed, tenant_id, account_id, t3)
            await call.done_writing()

    asyncio.run(scenario())
    assert _decrypt_rt(tenant_id, account_id) == after_rt
    row = _stored(tenant_id, account_id)
    assert abs((row.credential_observed_at - t3).total_seconds()) < 1e-3
    # Neither unstorable value reached the column.
    assert row.organization_name == original_org
    assert "\x00" not in (row.organization_name or "")


UNSTORABLE_TEXT_CASES = [
    ("NUL byte", "a\x00b", False),
    ("lone surrogate", "\ud800", False),
    ("at the 200-char ceiling", "o" * 200, True),
    ("one char over the ceiling", "o" * 201, False),
    ("multi-megabyte name", "o" * 2_000_000, False),
    ("ordinary name", "Acme", True),
]


@pytest.mark.parametrize(("name", "value", "storable"), UNSTORABLE_TEXT_CASES)
def test_metadata_text_storability_boundaries(name, value, storable):
    """Text columns: the key is present only when the value can be written.
    Rejection drops the field rather than truncating it — a half organisation
    name would read as authentic in the console."""
    secret = _oauth_secret_meta("b@ex.com", "rt", organizationName=value, accountUuid=value)
    meta = inventory.credential_metadata_values("claude", secret)
    assert ("organization_name" in meta) is storable, name
    assert ("account_uuid" in meta) is storable, name
    if storable:
        assert meta["organization_name"] == value


def test_metadata_scopes_item_and_count_limits():
    """JSONB scopes: unstorable ITEMS are filtered out of the list, but breaching
    the 64-item count drops the whole key so the column keeps its old value."""
    at_cap = _oauth_secret_meta("b@ex.com", "rt", scopes=[f"s{i}" for i in range(64)])
    assert len(inventory.credential_metadata_values("claude", at_cap)["scopes"]) == 64
    over_cap = _oauth_secret_meta("b@ex.com", "rt", scopes=[f"s{i}" for i in range(65)])
    assert "scopes" not in inventory.credential_metadata_values("claude", over_cap)
    dirty = _oauth_secret_meta(
        "b@ex.com", "rt", scopes=["user:inference", "a\x00b", "\ud800", "o" * 201, 7]
    )
    assert inventory.credential_metadata_values("claude", dirty)["scopes"] == ["user:inference"]


def test_metadata_codex_lifts_nothing():
    """Codex intentionally yields {}: `account_uuid` is only trustworthy next to
    `_apply_codex_metadata`'s id_token/email cross-check, which a re-sync cannot
    run, so an auth.json naming someone else's account cannot repaint the column.
    """
    hostile = json.dumps(
        {"tokens": {"account_id": "acct_ATTACKER", "refresh_token": "rt", "access_token": "at"}}
    )
    assert inventory.credential_metadata_values("codex", hostile) == {}


def test_metadata_extraction_never_raises_on_hostile_input():
    """Every conversion failure degrades to an omitted key: this runs on the
    session read loop, where an escaping exception drops the whole stream."""
    for secret in (
        "not json at all",
        "[" * 20000 + "]" * 20000,
        '{"claudeAiOauth": []}',
        '{"claudeAiOauth": {"expiresAt": 1e308}}',
        '{"claudeAiOauth": {"expiresAt": "soon", "scopes": "user:inference"}}',
        '{"claudeAiOauth": {"organizationName": "", "accountUuid": 7}}',
    ):
        meta = inventory.credential_metadata_values("claude", secret)
        assert "credential_expires_at" not in meta, secret[:40]
        assert "organization_name" not in meta, secret[:40]
        assert "account_uuid" not in meta, secret[:40]
