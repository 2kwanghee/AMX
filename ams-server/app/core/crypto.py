"""At-rest credential encryption (§7).

Fernet under `AMX_ENCRYPTION_KEY`, kept deliberately separate from the
authentication secret so the two rotate independently. Nothing in this module
logs, formats, or returns plaintext except through `decrypt_secret`, whose only
non-test caller is the deliver path (P2) and `scripts/verify_credential.py`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from typing import Any

import bcrypt
from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class CredentialDecryptionError(RuntimeError):
    """The stored ciphertext could not be opened with the configured key."""


def _fernet() -> Fernet:
    return Fernet(get_settings().encryption_key.encode())


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise CredentialDecryptionError(
            "stored credential could not be decrypted with AMX_ENCRYPTION_KEY"
        ) from exc


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
