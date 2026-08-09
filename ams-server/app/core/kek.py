"""KEK providers and tenant DEK management (F2 envelope encryption, §7).

Envelope encryption splits the at-rest key hierarchy: a per-tenant
*data-encryption key* (DEK) encrypts credential ciphertext, and a
*key-encryption key* (KEK) — held by a provider — wraps the DEK. Only wrapped
DEKs are stored (``tenant_deks``); the plaintext DEK exists in memory only
between an unwrap and its use, cached briefly to avoid a KEK round-trip per
operation.

The KEK provider is pluggable (``AMX_KEK_PROVIDER``). The local provider (MVP)
wraps with AES-256-GCM under a single 32-byte env KEK, binding ``tenant_id`` as
AAD so a wrapped DEK is useless under any other tenant. KMS adapters are stubbed
until a vendor is chosen; the DEK plumbing (provider_id, key_id) is complete so
adopting one is a provider swap, not a schema change.

Nothing here logs DEK, KEK, or plaintext (§7).
"""

from __future__ import annotations

import base64
import os
import secrets
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections import OrderedDict
from functools import lru_cache
from typing import Protocol, runtime_checkable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import TenantDek

DEK_BYTES = 32
_KEK_BYTES = 32
ALGORITHM = "AES-256-GCM"


class KekError(RuntimeError):
    """A KEK provider could not wrap or unwrap a DEK."""


def _tenant_aad(tenant_id: uuid.UUID | str) -> bytes:
    """Canonical AAD for a tenant. Both wrap/unwrap and DEK-GCM use this form."""
    return str(tenant_id).encode("utf-8")


# -- Provider interface -------------------------------------------------------
@runtime_checkable
class KekProvider(Protocol):
    provider_id: str

    def wrap_dek(self, dek: bytes, *, tenant_id: uuid.UUID | str) -> tuple[bytes, str]:
        """Wrap a plaintext DEK, returning (wrapped_dek, key_id)."""
        ...

    def unwrap_dek(
        self, wrapped: bytes, *, tenant_id: uuid.UUID | str, key_id: str
    ) -> bytes:
        """Recover the plaintext DEK from a wrapped DEK."""
        ...


class LocalKekProvider:
    """AES-256-GCM DEK wrapping under a single 32-byte env KEK (MVP).

    ``tenant_id`` is bound as AAD, so a DEK wrapped for tenant A fails
    authentication if unwrap is attempted under tenant B — the tenant-isolation
    property the design rests on. ``key_id`` is a constant here; a real KMS would
    return an ARN/version. The wire format of ``wrapped`` is ``nonce(12) || ct``.
    """

    provider_id = "local"
    _KEY_ID = "local-kek-v1"

    def __init__(self, kek: bytes) -> None:
        if len(kek) != _KEK_BYTES:
            raise KekError("local KEK must be exactly 32 bytes")
        self._aead = AESGCM(kek)

    def wrap_dek(self, dek: bytes, *, tenant_id: uuid.UUID | str) -> tuple[bytes, str]:
        nonce = secrets.token_bytes(12)
        ct = self._aead.encrypt(nonce, dek, _tenant_aad(tenant_id))
        return nonce + ct, self._KEY_ID

    def unwrap_dek(
        self, wrapped: bytes, *, tenant_id: uuid.UUID | str, key_id: str
    ) -> bytes:
        if len(wrapped) < 12:
            raise KekError("wrapped DEK is truncated")
        nonce, ct = wrapped[:12], wrapped[12:]
        try:
            return self._aead.decrypt(nonce, ct, _tenant_aad(tenant_id))
        except Exception as exc:  # noqa: BLE001 - opaque, no crypto detail (§7)
            raise KekError("DEK unwrap failed (wrong tenant or KEK)") from exc


class KmsKekProvider(ABC):
    """Abstract KMS-backed KEK provider — the seam for aws-kms / vault.

    A real adapter calls the vendor's Encrypt/Decrypt with an EncryptionContext
    of ``{"tenant": tenant_id}`` (the KMS equivalent of the local AAD binding)
    and returns the key ARN/version as ``key_id``. Left NotImplemented until a
    vendor is chosen; ``build_kek_provider`` refuses to construct one so a
    misconfigured ``AMX_KEK_PROVIDER`` fails loudly at startup, not mid-write.
    """

    provider_id: str = "kms"

    @abstractmethod
    def wrap_dek(self, dek: bytes, *, tenant_id: uuid.UUID | str) -> tuple[bytes, str]:
        raise NotImplementedError

    @abstractmethod
    def unwrap_dek(
        self, wrapped: bytes, *, tenant_id: uuid.UUID | str, key_id: str
    ) -> bytes:
        raise NotImplementedError


def _load_local_kek() -> bytes:
    """32-byte KEK for the local provider.

    ``AMX_KEK`` (urlsafe-base64 of 32 bytes) if set; otherwise the transitional
    reuse of ``AMX_ENCRYPTION_KEY`` (itself a 32-byte urlsafe-base64 Fernet key).
    Either way the material is exactly 32 bytes.
    """
    settings = get_settings()
    raw = settings.kek or settings.encryption_key
    try:
        key = base64.urlsafe_b64decode(raw)
    except Exception as exc:  # noqa: BLE001
        raise KekError(
            "AMX_KEK is not valid urlsafe-base64 (expected 32 bytes encoded)"
        ) from exc
    if len(key) != _KEK_BYTES:
        raise KekError(
            f"AMX_KEK must decode to 32 bytes (got {len(key)}); reuse of "
            "AMX_ENCRYPTION_KEY requires a standard Fernet key"
        )
    return key


def build_provider_by_id(provider_id: str) -> KekProvider:
    """Construct the provider named by a stored DEK's ``kek_provider``.

    Independent of the currently-configured provider, so a DEK wrapped by one
    provider still unwraps after the config is switched to another (mixed
    local/KMS). Only ``local`` has an adapter today; ``aws-kms``/``vault`` raise
    until a vendor is chosen, which is why the mismatch surfaces loudly rather
    than being silently mis-unwrapped by whatever is active.
    """
    if provider_id == "local":
        return LocalKekProvider(_load_local_kek())
    raise KekError(
        f"KEK provider {provider_id!r} has no implementation yet "
        "(KMS vendor undecided; F2 ships the local provider)"
    )


def build_kek_provider() -> KekProvider:
    return build_provider_by_id(get_settings().kek_provider)


@lru_cache(maxsize=1)
def get_kek_provider() -> KekProvider:
    return build_kek_provider()


def _provider_for(provider_id: str) -> KekProvider:
    """Resolve the provider a stored DEK was wrapped with.

    When the DEK's provider is the one currently active, reuse that instance
    (honours a test-injected or cached provider); otherwise build it by id.
    Dispatch is on the row's ``kek_provider``, never blindly on the active one.
    """
    active = get_kek_provider()
    if provider_id == active.provider_id:
        return active
    return build_provider_by_id(provider_id)


# -- Unwrapped-DEK cache ------------------------------------------------------
class _DekCache:
    """Small TTL + max-size cache of unwrapped DEKs, keyed by (tenant, version).

    Holds plaintext DEKs in process memory — the same trust level as the
    existing AMX_ENCRYPTION_KEY, and the design's accepted MVP posture (§5).
    Thread-safe: the gRPC server unwraps from worker threads.
    """

    def __init__(self, *, max_size: int = 512, ttl_seconds: float = 300.0) -> None:
        self._max = max_size
        self._ttl = ttl_seconds
        self._store: OrderedDict[tuple[str, int], tuple[float, bytes]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple[str, int]) -> bytes | None:
        with self._lock:
            item = self._store.get(key)
            if item is None:
                return None
            expiry, dek = item
            if expiry < time.monotonic():
                del self._store[key]
                return None
            self._store.move_to_end(key)
            return dek

    def put(self, key: tuple[str, int], dek: bytes) -> None:
        with self._lock:
            self._store[key] = (time.monotonic() + self._ttl, dek)
            self._store.move_to_end(key)
            while len(self._store) > self._max:
                self._store.popitem(last=False)

    def invalidate(self, tenant_id: uuid.UUID | str, version: int | None = None) -> None:
        tid = str(tenant_id)
        with self._lock:
            if version is not None:
                self._store.pop((tid, version), None)
                return
            for k in [k for k in self._store if k[0] == tid]:
                del self._store[k]

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


_dek_cache = _DekCache()


def invalidate_dek_cache(
    tenant_id: uuid.UUID | str | None = None, version: int | None = None
) -> None:
    """Drop cached unwrapped DEKs — call after a rotation. Whole-cache if no id."""
    if tenant_id is None:
        _dek_cache.clear()
    else:
        _dek_cache.invalidate(tenant_id, version)


def _unwrap_cached(row: TenantDek) -> bytes:
    key = (str(row.tenant_id), row.version)
    hit = _dek_cache.get(key)
    if hit is not None:
        return hit
    provider = _provider_for(row.kek_provider)
    dek = provider.unwrap_dek(
        bytes(row.wrapped_dek), tenant_id=row.tenant_id, key_id=row.kek_key_id
    )
    _dek_cache.put(key, dek)
    return dek


# -- DEK lifecycle ------------------------------------------------------------
def generate_dek() -> bytes:
    return secrets.token_bytes(DEK_BYTES)


def create_tenant_dek(
    db: Session, tenant_id: uuid.UUID, *, version: int = 1
) -> TenantDek:
    """Provision a fresh wrapped DEK for a tenant. Adds to the session; the
    caller commits. Used by create_tenant and the 0008 backfill."""
    provider = get_kek_provider()
    dek = generate_dek()
    wrapped, key_id = provider.wrap_dek(dek, tenant_id=tenant_id)
    del dek
    row = TenantDek(
        tenant_id=tenant_id,
        version=version,
        wrapped_dek=wrapped,
        kek_provider=provider.provider_id,
        kek_key_id=key_id,
        algorithm=ALGORITHM,
    )
    db.add(row)
    return row


def ensure_tenant_dek(db: Session, tenant_id: uuid.UUID) -> TenantDek:
    """Provision a v1 DEK for a tenant that has none. Idempotent — returns the
    existing active DEK if one is already present. Used by the 0008 backfill."""
    existing = db.scalars(
        select(TenantDek)
        .where(TenantDek.tenant_id == tenant_id, TenantDek.retired_at.is_(None))
        .order_by(TenantDek.version.desc())
        .limit(1)
    ).first()
    if existing is not None:
        return existing
    return create_tenant_dek(db, tenant_id, version=1)


def active_dek(db: Session, tenant_id: uuid.UUID) -> TenantDek:
    """The tenant's active DEK: highest version with retired_at NULL."""
    row = db.scalars(
        select(TenantDek)
        .where(TenantDek.tenant_id == tenant_id, TenantDek.retired_at.is_(None))
        .order_by(TenantDek.version.desc())
        .limit(1)
    ).first()
    if row is None:
        raise KekError(f"tenant {tenant_id} has no active DEK (run migration 0008)")
    return row


def dek_by_version(db: Session, tenant_id: uuid.UUID, version: int) -> TenantDek:
    row = db.scalars(
        select(TenantDek).where(
            TenantDek.tenant_id == tenant_id, TenantDek.version == version
        )
    ).first()
    if row is None:
        raise KekError(f"tenant {tenant_id} has no DEK version {version}")
    return row


def unwrap_active_dek(db: Session, tenant_id: uuid.UUID) -> tuple[bytes, int]:
    """Unwrapped active DEK and its version (for a v2 write)."""
    row = active_dek(db, tenant_id)
    return _unwrap_cached(row), row.version


def unwrap_dek_version(db: Session, tenant_id: uuid.UUID, version: int) -> bytes:
    """Unwrapped DEK for a specific version (for a v2 read)."""
    return _unwrap_cached(dek_by_version(db, tenant_id, version))


def envelope_write_enabled() -> bool:
    """The v2-write rollback boundary, read live so it flips without a restart."""
    return os.environ.get("AMX_ENVELOPE_WRITE", "").strip() == "1"
