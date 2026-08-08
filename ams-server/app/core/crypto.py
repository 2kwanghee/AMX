"""At-rest credential encryption (§7).

Fernet under `AMX_ENCRYPTION_KEY`, kept deliberately separate from the
authentication secret so the two rotate independently. Nothing in this module
logs, formats, or returns plaintext except through `decrypt_secret`, whose only
non-test caller is the deliver path (P2) and `scripts/verify_credential.py`.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any

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

    Four characters of a salted digest, not of the secret itself — a suffix of
    the real token would leak material every time the console renders a list.
    """
    digest = hashlib.sha256(plaintext.encode()).hexdigest()[:4].upper()
    return f"{credential_type}:…{digest}"


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()


def dumps_credential(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)
