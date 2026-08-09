"""F2 envelope encryption — tenant DEKs, KEK providers, chokepoint (design §5.1, §7).

Covers the S3a/S3b/S3c surface:
* local KEK provider round-trip and tenant AAD isolation;
* a fake KMS provider swapped in through the same interface (proves the seam);
* per-tenant DEK provisioning (create_tenant) and backfill (ensure_tenant_dek);
* the encrypt/decrypt chokepoint under both AMX_ENVELOPE_WRITE states, v2 format,
  legacy Fernet coexistence, and lazy promotion on the next write;
* the five at-rest access points routing through the DEK when the flag is on,
  including the O9 re-sync (monotonic WHERE preserved) and deliver.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import uuid
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import text

from app.core import crypto, kek
from app.core.kek import KekProvider, LocalKekProvider
from app.db import get_sessionmaker
from app.grpc.proto import pb, pb_grpc
from app.models import Account, Tenant, TenantDek
from app.services import inventory

rewrap = importlib.import_module("scripts.rewrap_secrets")


# -- Fixtures / helpers -------------------------------------------------------
@pytest.fixture()
def write_on(monkeypatch):
    monkeypatch.setenv("AMX_ENVELOPE_WRITE", "1")
    yield


@pytest.fixture()
def write_off(monkeypatch):
    monkeypatch.delenv("AMX_ENVELOPE_WRITE", raising=False)
    yield


def _new_tenant() -> uuid.UUID:
    with get_sessionmaker()() as db:
        return inventory.create_tenant(db, "t-" + uuid.uuid4().hex[:8]).id


def _oauth(email: str, rt: str) -> str:
    return json.dumps(
        {"claudeAiOauth": {"accessToken": "at", "refreshToken": rt, "emailAddress": email}}
    )


# -- Local KEK provider -------------------------------------------------------
def test_local_provider_round_trip_and_tenant_isolation():
    provider = LocalKekProvider(os.urandom(32))
    assert isinstance(provider, KekProvider)  # runtime_checkable Protocol
    dek = kek.generate_dek()
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()

    wrapped, key_id = provider.wrap_dek(dek, tenant_id=tenant_a)
    assert wrapped != dek and len(wrapped) > 32  # nonce+ct+tag, opaque
    assert provider.unwrap_dek(wrapped, tenant_id=tenant_a, key_id=key_id) == dek

    # A wrapped-DEK for tenant A cannot be unwrapped under tenant B (AAD bind).
    with pytest.raises(kek.KekError):
        provider.unwrap_dek(wrapped, tenant_id=tenant_b, key_id=key_id)


def test_local_provider_rejects_wrong_kek_length():
    with pytest.raises(kek.KekError):
        LocalKekProvider(b"\x00" * 16)


# -- Fake KMS provider swapped through the interface --------------------------
class _FakeKms:
    """A non-local provider that satisfies KekProvider without NotImplemented.

    Wraps under a private AES key with an EncryptionContext-style AAD, mirroring
    what a real KMS adapter does — enough to prove the DEK plumbing is provider
    agnostic (key_id round-trips, tenant AAD enforced).
    """

    provider_id = "fake-kms"

    def __init__(self) -> None:
        self._aead = AESGCM(os.urandom(32))

    def wrap_dek(self, dek, *, tenant_id):
        nonce = os.urandom(12)
        ct = self._aead.encrypt(nonce, dek, str(tenant_id).encode())
        return nonce + ct, "arn:fake:key/v1"

    def unwrap_dek(self, wrapped, *, tenant_id, key_id):
        try:
            return self._aead.decrypt(wrapped[:12], wrapped[12:], str(tenant_id).encode())
        except Exception as exc:  # noqa: BLE001
            raise kek.KekError("fake-kms unwrap failed") from exc


def test_fake_kms_provider_swaps_through_chokepoint(monkeypatch, write_on):
    fake = _FakeKms()
    monkeypatch.setattr(kek, "get_kek_provider", lambda: fake)
    kek.invalidate_dek_cache()

    tenant_id = _new_tenant()  # provisions v1 DEK wrapped by the fake KMS
    with get_sessionmaker()() as db:
        row = kek.active_dek(db, tenant_id)
        assert row.kek_provider == "fake-kms"
        assert row.kek_key_id == "arn:fake:key/v1"
        ct = crypto.encrypt_secret("hello", tenant_id=tenant_id, db=db)
        assert ct.startswith("v2:")
        assert crypto.decrypt_secret(ct, tenant_id=tenant_id, db=db) == "hello"


def test_kms_provider_names_have_no_adapter_yet(monkeypatch):
    # Config accepts aws-kms/vault, but building one is refused until a vendor
    # is chosen — a clean startup failure, not a mid-write surprise.
    from app.config import Settings

    base = kek.get_settings()
    for name in ("aws-kms", "vault"):
        stub = Settings(
            database_url=base.database_url,
            admin_token=base.admin_token,
            encryption_key=base.encryption_key,
            kek_provider=name,
            kek=None,
        )
        monkeypatch.setattr(kek, "get_settings", lambda s=stub: s)
        with pytest.raises(kek.KekError):
            kek.build_kek_provider()


# -- DEK provisioning + backfill ----------------------------------------------
def test_create_tenant_provisions_v1_dek():
    tenant_id = _new_tenant()
    with get_sessionmaker()() as db:
        row = kek.active_dek(db, tenant_id)
        assert row.version == 1
        assert row.kek_provider == "local"
        assert row.retired_at is None


def test_ensure_tenant_dek_backfills_only_when_missing():
    # A tenant inserted WITHOUT the create_tenant provisioning path (as the 0008
    # backfill encounters pre-existing tenants).
    with get_sessionmaker()() as db:
        tenant = Tenant(name="backfill-" + uuid.uuid4().hex[:8], status="active")
        db.add(tenant)
        db.commit()
        db.refresh(tenant)
        tid = tenant.id
        with pytest.raises(kek.KekError):
            kek.active_dek(db, tid)  # none yet
        first = kek.ensure_tenant_dek(db, tid)
        db.commit()
        # Idempotent: a second call returns the same active row, no v2.
        again = kek.ensure_tenant_dek(db, tid)
        assert again.id == first.id
        count = db.query(TenantDek).filter(TenantDek.tenant_id == tid).count()
        assert count == 1


# -- Chokepoint: flag off (legacy) / on (v2) ----------------------------------
def test_write_flag_off_writes_legacy_fernet(write_off):
    tenant_id = _new_tenant()
    with get_sessionmaker()() as db:
        ct = crypto.encrypt_secret("secret-value", tenant_id=tenant_id, db=db)
        assert not ct.startswith("v2:")
        assert crypto.decrypt_secret(ct, tenant_id=tenant_id, db=db) == "secret-value"


def test_write_flag_on_writes_v2(write_on):
    tenant_id = _new_tenant()
    with get_sessionmaker()() as db:
        ct = crypto.encrypt_secret("secret-value", tenant_id=tenant_id, db=db)
        assert ct.startswith("v2:1:")  # version 1, DEK path
        assert crypto.decrypt_secret(ct, tenant_id=tenant_id, db=db) == "secret-value"


def test_legacy_fernet_still_readable_after_flag_flips_on(monkeypatch):
    tenant_id = _new_tenant()
    monkeypatch.delenv("AMX_ENVELOPE_WRITE", raising=False)
    with get_sessionmaker()() as db:
        legacy = crypto.encrypt_secret("old-secret", tenant_id=tenant_id, db=db)
    assert not legacy.startswith("v2:")
    # Flip the write flag on; the previously-written Fernet row must still open.
    monkeypatch.setenv("AMX_ENVELOPE_WRITE", "1")
    with get_sessionmaker()() as db:
        assert crypto.decrypt_secret(legacy, tenant_id=tenant_id, db=db) == "old-secret"


def test_v2_ciphertext_of_one_tenant_does_not_open_as_another(write_on):
    tenant_a = _new_tenant()
    tenant_b = _new_tenant()
    with get_sessionmaker()() as db:
        ct_a = crypto.encrypt_secret("a-secret", tenant_id=tenant_a, db=db)
        # Same version number exists for B, but B's DEK + AAD differ -> opaque fail.
        with pytest.raises(crypto.CredentialDecryptionError):
            crypto.decrypt_secret(ct_a, tenant_id=tenant_b, db=db)


def test_lazy_promotion_on_next_write(monkeypatch):
    # Enroll with the flag off (legacy), then update with the flag on: the row is
    # promoted to v2 by the write path, no batch needed.
    tenant_id = _new_tenant()
    monkeypatch.delenv("AMX_ENVELOPE_WRITE", raising=False)
    with get_sessionmaker()() as db:
        account = inventory.create_account(
            db, tenant_id, email="lazy@example.com", credential_type="oauth",
            secret=_oauth("lazy@example.com", "rt-old"),
        )
        account_id = account.id
        assert not (account.encrypted_secret or "").startswith("v2:")

    monkeypatch.setenv("AMX_ENVELOPE_WRITE", "1")
    with get_sessionmaker()() as db:
        inventory.update_account(
            db, tenant_id, account_id, email=None, status=None,
            secret=_oauth("lazy@example.com", "rt-new"),
        )
    with get_sessionmaker()() as db:
        row = db.get(Account, account_id)
        assert row.encrypted_secret.startswith("v2:")
        payload = json.loads(
            crypto.decrypt_secret(row.encrypted_secret, tenant_id=tenant_id, db=db)
        )
        assert payload["claudeAiOauth"]["refreshToken"] == "rt-new"


# -- Access point round-trips through the DEK (flag on) -----------------------
def test_create_and_verify_script_path_use_dek(write_on):
    # Access points #1 (create_account) and #5 (verify_credential.py read).
    tenant_id = _new_tenant()
    with get_sessionmaker()() as db:
        account = inventory.create_account(
            db, tenant_id, email="verify@example.com", credential_type="oauth",
            secret=_oauth("verify@example.com", "rt-verify"),
        )
        assert account.encrypted_secret.startswith("v2:")
        # The verify script's read is exactly decrypt_secret(tenant_id, db).
        payload = json.loads(
            crypto.decrypt_secret(account.encrypted_secret, tenant_id=tenant_id, db=db)
        )
        assert payload["claudeAiOauth"]["refreshToken"] == "rt-verify"


def test_o9_resync_routes_through_dek(write_on):
    # Access point #3: O9 _apply_cred_update (write). The account is 'active'
    # (owned by this server) so the re-sync is accepted; the stored copy must be
    # v2 (DEK) and monotonicity (the conditional WHERE) must still hold.
    from app.grpc import signing

    from tests.test_credential_resync import (
        _assign,
        _cred_update,
        _oauth_secret,
        _open_session,
        _seal,
        _stored,
        _wait_observed,
    )
    from tests.test_grpc_channel import (
        AGENT_ID,
        _Harness,
        _issue_enroll,
        _seed_tenant_account_server,
    )

    signer = signing.Signer.from_env_or_generate()
    tenant_id, account_id, server_id = _seed_tenant_account_server("env-o9@example.com")
    # create_account ran under the flag -> the seeded secret is already v2.
    assert (_stored(tenant_id, account_id).encrypted_secret or "").startswith("v2:")
    _assign(tenant_id, account_id, server_id)  # forces 'active' (ownership)
    token = _issue_enroll(tenant_id, server_id)
    observed = datetime.now(UTC)
    new_rt = "rt-env-" + uuid.uuid4().hex

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            call, session_kek, key_id = await _open_session(channel, token)
            enc = _seal(
                session_kek, key_id, account_id, AGENT_ID,
                _oauth_secret("env-o9@example.com", new_rt),
            )
            await call.write(_cred_update(account_id, enc, "", observed))
            await asyncio.to_thread(_wait_observed, tenant_id, account_id, observed)
            await call.done_writing()

    asyncio.run(scenario())
    stored = _stored(tenant_id, account_id)
    assert stored.encrypted_secret.startswith("v2:")  # DEK path, not Fernet
    assert stored.credential_observed_at is not None
    # And it decodes to the pushed token through the DEK.
    with get_sessionmaker()() as db:
        payload = json.loads(
            crypto.decrypt_secret(stored.encrypted_secret, tenant_id=tenant_id, db=db)
        )
    assert payload["claudeAiOauth"]["refreshToken"] == new_rt


def test_deliver_routes_through_dek(write_on):
    # Access point #4: deliver (read). A v2 (DEK) at-rest secret is opened by AMS
    # and re-sealed under the session KEK; the agent recovers the exact plaintext.
    from app.grpc import signing

    from tests.test_credential_resync import _stored
    from tests.test_grpc_channel import (
        AGENT_ID,
        _Harness,
        _create_assignment,
        _issue_enroll,
        _read,
        _rest_deliver,
        _seed_tenant_account_server,
    )

    signer = signing.Signer.from_env_or_generate()
    tenant_id, account_id, server_id = _seed_tenant_account_server("env-deliver@example.com")
    stored = _stored(tenant_id, account_id)
    assert (stored.encrypted_secret or "").startswith("v2:")
    assignment_id = _create_assignment(tenant_id, account_id, server_id)  # 'pending'
    token = _issue_enroll(tenant_id, server_id)

    async def scenario():
        async with _Harness(signer) as h, h.channel() as channel:
            stub = pb_grpc.AmxControlPlaneStub(channel)
            call = stub.Session()
            await call.write(
                pb.AmaMessage(register=pb.Register(agent_id=AGENT_ID, enroll_token=token))
            )
            setup = await _read(call)
            session_kek = setup.session_setup.keys[0].wrapped_key
            assert (await _read(call)).WhichOneof("cmd") == "set_mode"
            assert (await _read(call)).WhichOneof("cmd") == "set_policy"
            await asyncio.to_thread(_rest_deliver, tenant_id, assignment_id)
            cmd = await _read(call)
            assert cmd.WhichOneof("cmd") == "deliver"
            enc_out = cmd.deliver.encrypted_credential
            plaintext = signing.open_credential(
                session_kek, enc_out.ciphertext, enc_out.nonce,
                ams_account_id=str(account_id), agent_id=AGENT_ID,
            )
            assert json.loads(plaintext)["claudeAiOauth"]["emailAddress"] == (
                "env-deliver@example.com"
            )
            await call.done_writing()

    asyncio.run(scenario())


# -- R3 fix 3: unwrap dispatches on the row's provider, not the active one ----
def test_provider_for_dispatches_by_id(monkeypatch):
    kek.invalidate_dek_cache()
    assert isinstance(kek._provider_for("local"), LocalKekProvider)
    with pytest.raises(kek.KekError):
        kek._provider_for("aws-kms")
    with pytest.raises(kek.KekError):
        kek._provider_for("vault")
    # With a fake provider active, its id resolves to that instance, yet 'local'
    # still builds a real local provider — dispatch follows the row's id.
    fake = _FakeKms()
    monkeypatch.setattr(kek, "get_kek_provider", lambda: fake)
    assert kek._provider_for("fake-kms") is fake
    assert isinstance(kek._provider_for("local"), LocalKekProvider)


def test_unwrap_uses_row_provider_not_active(write_on):
    # The DEK is really wrapped by the active local provider, so unwrapping under
    # 'local' would succeed. Re-tag the row as an unimplemented provider: unwrap
    # must now fail *via that provider*, proving it dispatches on row.kek_provider
    # rather than blindly using the active local one.
    tenant_id = _new_tenant()
    with get_sessionmaker()() as db:
        row = kek.active_dek(db, tenant_id)
        row.kek_provider = "vault"
        db.commit()
    kek.invalidate_dek_cache(tenant_id)
    with get_sessionmaker()() as db:
        with pytest.raises(kek.KekError):
            kek.unwrap_dek_version(db, tenant_id, 1)


# -- R3 fix 1: rewrap forward is CAS lost-update safe -------------------------
def _mk_legacy_account(tenant_id, email, rt) -> uuid.UUID:
    with get_sessionmaker()() as db:
        account = inventory.create_account(
            db, tenant_id, email=email, credential_type="oauth", secret=_oauth(email, rt)
        )
        assert not (account.encrypted_secret or "").startswith("v2:")
        return account.id


def test_rewrap_forward_promotes_legacy_to_v2(monkeypatch, write_off):
    tenant_id = _new_tenant()
    account_id = _mk_legacy_account(tenant_id, "fwd@example.com", "rt-fwd")
    monkeypatch.setenv("AMX_ENVELOPE_WRITE", "1")
    monkeypatch.setattr("sys.argv", ["rewrap", "--tenant", str(tenant_id)])
    assert rewrap.main() == 0
    with get_sessionmaker()() as db:
        row = db.get(Account, account_id)
        assert row.encrypted_secret.startswith("v2:")
        payload = json.loads(
            crypto.decrypt_secret(row.encrypted_secret, tenant_id=tenant_id, db=db)
        )
        assert payload["claudeAiOauth"]["refreshToken"] == "rt-fwd"


def test_rewrap_forward_cas_skips_concurrent_change(monkeypatch, write_off):
    # A newer credential committed (O9-style) between the rewrap's read and its
    # write must NOT be overwritten by the stale plaintext the rewrap holds.
    tenant_id = _new_tenant()
    account_id = _mk_legacy_account(tenant_id, "cas@example.com", "rt-stale")
    monkeypatch.setenv("AMX_ENVELOPE_WRITE", "1")

    real_encrypt = crypto.encrypt_secret
    state = {"injected": False}

    def racing_encrypt(plaintext, *, tenant_id, db):
        # Fire the concurrent write exactly once, after the rewrap has read the
        # old value but before its CAS UPDATE lands.
        if not state["injected"]:
            state["injected"] = True
            with get_sessionmaker()() as other:
                a = other.get(Account, account_id)
                a.encrypted_secret = real_encrypt(
                    _oauth("cas@example.com", "rt-o9-newer"),
                    tenant_id=a.tenant_id, db=other,
                )
                other.commit()
        return real_encrypt(plaintext, tenant_id=tenant_id, db=db)

    monkeypatch.setattr(rewrap.crypto, "encrypt_secret", racing_encrypt)
    monkeypatch.setattr("sys.argv", ["rewrap", "--tenant", str(tenant_id)])
    assert rewrap.main() == 0
    # The O9 credential survives; the stale rewrap did not resurrect rt-stale.
    with get_sessionmaker()() as db:
        row = db.get(Account, account_id)
        payload = json.loads(
            crypto.decrypt_secret(row.encrypted_secret, tenant_id=tenant_id, db=db)
        )
        assert payload["claudeAiOauth"]["refreshToken"] == "rt-o9-newer"
    assert state["injected"] is True


# -- R3 fix 2b: reverse rewrap folds v2 -> Fernet -----------------------------
def test_rewrap_reverse_folds_v2_to_fernet(monkeypatch):
    tenant_id = _new_tenant()
    monkeypatch.setenv("AMX_ENVELOPE_WRITE", "1")
    with get_sessionmaker()() as db:
        account = inventory.create_account(
            db, tenant_id, email="rev@example.com", credential_type="oauth",
            secret=_oauth("rev@example.com", "rt-rev"),
        )
        account_id = account.id
        assert account.encrypted_secret.startswith("v2:")
    # Reverse is a rollback tool: independent of the write flag. Unset it to prove.
    monkeypatch.delenv("AMX_ENVELOPE_WRITE", raising=False)
    monkeypatch.setattr("sys.argv", ["rewrap", "--reverse", "--tenant", str(tenant_id)])
    assert rewrap.main() == 0
    with get_sessionmaker()() as db:
        row = db.get(Account, account_id)
        assert not row.encrypted_secret.startswith("v2:")  # back to legacy Fernet
        payload = json.loads(
            crypto.decrypt_secret(row.encrypted_secret, tenant_id=tenant_id, db=db)
        )
        assert payload["claudeAiOauth"]["refreshToken"] == "rt-rev"


# -- R3 fix 2a: 0008 downgrade refuses while any v2 credential exists ----------
def _load_migration_0008():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "alembic", "versions", "0008_tenant_deks.py")
    spec = importlib.util.spec_from_file_location("_mig_0008", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


def test_downgrade_refuses_while_v2_exists(monkeypatch):
    mig = _load_migration_0008()
    from alembic import op as alembic_op

    monkeypatch.setattr(
        alembic_op, "get_bind", lambda: type(
            "B", (), {"execute": lambda self, *a: _FakeResult((1,))}
        )()
    )
    with pytest.raises(RuntimeError, match="downgrade refused"):
        mig.downgrade()


def test_downgrade_allowed_when_no_v2(monkeypatch):
    mig = _load_migration_0008()
    from alembic import op as alembic_op

    dropped = {"called": False}
    monkeypatch.setattr(
        alembic_op, "get_bind", lambda: type(
            "B", (), {"execute": lambda self, *a: _FakeResult(None)}
        )()
    )
    monkeypatch.setattr(
        alembic_op, "drop_table", lambda name: dropped.__setitem__("called", True)
    )
    mig.downgrade()
    assert dropped["called"] is True


def test_reverse_rewrap_clears_downgrade_guard(monkeypatch):
    # The migration's guard predicate is `... LIKE 'v2:%'`. A v2 row trips it;
    # after --reverse the predicate is empty, so downgrade would proceed.
    tenant_id = _new_tenant()
    monkeypatch.setenv("AMX_ENVELOPE_WRITE", "1")
    with get_sessionmaker()() as db:
        inventory.create_account(
            db, tenant_id, email="guard@example.com", credential_type="oauth",
            secret=_oauth("guard@example.com", "rt-guard"),
        )
    guard = text("SELECT 1 FROM accounts WHERE encrypted_secret LIKE 'v2:%' LIMIT 1")
    with get_sessionmaker()() as db:
        assert db.execute(guard).first() is not None
    monkeypatch.delenv("AMX_ENVELOPE_WRITE", raising=False)
    monkeypatch.setattr("sys.argv", ["rewrap", "--reverse", "--tenant", str(tenant_id)])
    assert rewrap.main() == 0
    with get_sessionmaker()() as db:
        assert db.execute(guard).first() is None
