"""At-rest credential encryption (§7).

This module is the single chokepoint for at-rest credential crypto: every one
of the five access points (create/update account, O9 re-sync, deliver, ops
verify) goes through `encrypt_secret` / `decrypt_secret`, so no code path
produces credential ciphertext independently. That invariant is what keeps
tenant DEK envelope encryption (F2) and the double-encryption boundary intact.

Two on-disk formats coexist during the migration:

* legacy: a Fernet token under `AMX_ENCRYPTION_KEY` (no prefix).
* v2: `v2:{dek_version}:{b64(nonce)}:{b64(ct)}` — AES-256-GCM under the tenant's
  data-encryption key (DEK), with the tenant_id bound as AAD.

Reads auto-detect by the `v2:` prefix, so legacy ciphertext keeps opening
forever. Writes emit v2 only when `AMX_ENVELOPE_WRITE=1` (the rollback boundary);
until then they keep writing legacy Fernet. Nothing here logs, formats, or
returns plaintext except through `decrypt_secret`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import uuid
from typing import Any

import bcrypt
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core import kek

_V2_PREFIX = "v2:"


class CredentialDecryptionError(RuntimeError):
    """The stored ciphertext could not be opened with the configured key."""


def _fernet() -> Fernet:
    return Fernet(get_settings().encryption_key.encode())


def _aad(tenant_id: uuid.UUID | str) -> bytes:
    return str(tenant_id).encode("utf-8")


def encrypt_secret(plaintext: str, *, tenant_id: uuid.UUID | str, db: Session) -> str:
    """Encrypt a credential for storage.

    v2 (tenant DEK, AES-256-GCM, AAD=tenant_id) when `AMX_ENVELOPE_WRITE=1`;
    otherwise legacy Fernet, so the flip is the clean rollback boundary. Both
    forms are readable by `decrypt_secret` regardless of the flag.
    """
    if not kek.envelope_write_enabled():
        return _fernet().encrypt(plaintext.encode()).decode()
    dek, version = kek.unwrap_active_dek(db, _as_uuid(tenant_id))
    nonce = secrets.token_bytes(12)
    ct = AESGCM(dek).encrypt(nonce, plaintext.encode(), _aad(tenant_id))
    del dek
    return (
        f"{_V2_PREFIX}{version}:"
        f"{base64.b64encode(nonce).decode()}:{base64.b64encode(ct).decode()}"
    )


def decrypt_secret(ciphertext: str, *, tenant_id: uuid.UUID | str, db: Session) -> str:
    """Open stored credential ciphertext.

    `v2:` prefix -> tenant DEK path (version + tenant_id select the wrapped DEK);
    no prefix -> legacy Fernet. The two keys coexist so a flag flip never
    strands already-written rows.
    """
    if ciphertext.startswith(_V2_PREFIX):
        return _decrypt_v2(ciphertext, tenant_id=tenant_id, db=db)
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise CredentialDecryptionError(
            "stored credential could not be decrypted with AMX_ENCRYPTION_KEY"
        ) from exc


def _decrypt_v2(ciphertext: str, *, tenant_id: uuid.UUID | str, db: Session) -> str:
    try:
        _, version_s, nonce_b64, ct_b64 = ciphertext.split(":", 3)
        version = int(version_s)
        nonce = base64.b64decode(nonce_b64)
        ct = base64.b64decode(ct_b64)
    except (ValueError, TypeError) as exc:
        raise CredentialDecryptionError("malformed v2 credential ciphertext") from exc
    try:
        dek = kek.unwrap_dek_version(db, _as_uuid(tenant_id), version)
    except kek.KekError as exc:
        raise CredentialDecryptionError("tenant DEK unavailable for ciphertext") from exc
    try:
        plaintext = AESGCM(dek).decrypt(nonce, ct, _aad(tenant_id))
    except Exception as exc:  # noqa: BLE001 - opaque, no crypto detail (§7)
        raise CredentialDecryptionError(
            "stored credential could not be decrypted with the tenant DEK"
        ) from exc
    finally:
        del dek
    return plaintext.decode()


def _as_uuid(tenant_id: uuid.UUID | str) -> uuid.UUID:
    return tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))


def mask_secret(credential_type: str, plaintext: str) -> str:
    """A display hint that cannot be walked back to the credential.

    Four characters of a plain SHA-256 digest, not of the secret itself — a
    suffix of the real token would leak material every time the console renders
    a list.

    The digest is unsalted, so equal masks are evidence that two accounts hold
    the same credential and a mask is a (weak, 16-bit) correlation handle across
    tenants. That is accepted for P1: the mask discloses no credential material
    and 4 hex characters collide often enough to make the signal noisy. Salting
    it properly needs a masking key kept separate from `AMX_ENCRYPTION_KEY`
    (§7 keeps key purposes apart), which belongs with the SaaS-stage envelope
    encryption work, not here.
    """
    digest = hashlib.sha256(plaintext.encode()).hexdigest()[:4].upper()
    return f"{credential_type}:…{digest}"


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()


def _password_prehash(password: str) -> bytes:
    """A fixed-length, NUL-free input for bcrypt.

    bcrypt silently truncates at 72 bytes and stops at the first NUL byte, so a
    long or binary-heavy password would have its tail ignored. Pre-hashing with
    SHA-256 caps the length at 32 bytes; base64-encoding removes NUL bytes and
    keeps the digest inside bcrypt's 72-byte window (44 chars).
    """
    return base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())


def hash_password(password: str) -> str:
    """bcrypt hash of the pre-hashed password. Store only this, never the raw."""
    return bcrypt.hashpw(_password_prehash(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time bcrypt verification of a candidate password.

    Returns False (never raises) on a malformed stored hash, so a corrupt row
    is an authentication failure rather than a 500.
    """
    try:
        return bcrypt.checkpw(_password_prehash(password), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


def dumps_credential(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)
