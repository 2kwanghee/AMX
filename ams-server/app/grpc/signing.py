"""Command signing and the session KEK (design note §3, §4; proto §6.2).

Two independent pieces of key material live here, both **memory-only** and both
sourced from the environment or generated at startup — never hardcoded, never
logged (§7):

* an **Ed25519 signing key**. Every ``AmsCommand`` is signed over its canonical
  serialization with ``signature`` cleared; the agent runs nothing that fails
  verification. Loaded from ``AMX_SIGNING_KEY`` (url-safe base64 of the 32-byte
  seed) when set, otherwise generated — a generated key is fine for tests and
  for a single-instance deployment, but a fixed public key must be baked into
  the agent build in production, so a real deployment sets the env var.

* a **session KEK** minted per session. It is delivered once in SessionSetup and
  held only in the agent's memory (O1); it opens the AES-256-GCM credential
  envelopes. This module also builds those envelopes for the deliver path.
"""

from __future__ import annotations

import base64
import os
import secrets

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_SIGNING_KEY_ENV = "AMX_SIGNING_KEY"


def _b64url_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode())


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class Signer:
    """Holds the Ed25519 private key and signs commands."""

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private_key = private_key
        self._public_key = private_key.public_key()

    @classmethod
    def from_env_or_generate(cls) -> "Signer":
        raw = os.environ.get(_SIGNING_KEY_ENV, "").strip()
        if raw:
            seed = _b64url_decode(raw)
            if len(seed) != 32:
                raise ValueError(
                    f"{_SIGNING_KEY_ENV} must be a url-safe base64 32-byte Ed25519 seed."
                )
            return cls(Ed25519PrivateKey.from_private_bytes(seed))
        return cls(Ed25519PrivateKey.generate())

    def sign(self, message: bytes) -> bytes:
        return self._private_key.sign(message)

    def public_key(self) -> Ed25519PublicKey:
        return self._public_key


def verify(public_key: Ed25519PublicKey, signature: bytes, message: bytes) -> bool:
    """True iff ``signature`` is a valid Ed25519 signature of ``message``.

    A helper the agent side (and the tests standing in for it) use; never raises
    on a bad signature, so callers branch on the boolean.
    """
    try:
        public_key.verify(signature, message)
        return True
    except InvalidSignature:
        return False


# -- Session KEK & credential envelope ---------------------------------------

def new_kek() -> bytes:
    """A fresh 256-bit key-encryption key for one session."""
    return secrets.token_bytes(32)


def new_key_id() -> str:
    return "kek_" + _b64url_encode(secrets.token_bytes(9))


def build_aad(ams_account_id: str, agent_id: str) -> bytes:
    """Canonical additional-authenticated-data binding (proto §6.2, note §4).

    The recipient re-derives this from values it already holds (its own agent_id
    and the account id of the command it is processing). A NUL separator keeps
    the two components unambiguous. The wire ``aad_*`` fields are comparison-only
    copies and are never fed back in as the AAD input.
    """
    return ams_account_id.encode("utf-8") + b"\x00" + agent_id.encode("utf-8")


def seal_credential(
    kek: bytes, plaintext: bytes, *, ams_account_id: str, agent_id: str
) -> tuple[bytes, bytes]:
    """AES-256-GCM seal of a credential set. Returns (ciphertext_with_tag, nonce).

    Bound to (ams_account_id, agent_id) as AAD so a record copied onto another
    agent fails authentication rather than decrypting.
    """
    nonce = secrets.token_bytes(12)
    aad = build_aad(ams_account_id, agent_id)
    ciphertext = AESGCM(kek).encrypt(nonce, plaintext, aad)
    return ciphertext, nonce


def open_credential(
    kek: bytes,
    ciphertext: bytes,
    nonce: bytes,
    *,
    ams_account_id: str,
    agent_id: str,
) -> bytes:
    """Inverse of :func:`seal_credential`. Raises on authentication failure."""
    aad = build_aad(ams_account_id, agent_id)
    return AESGCM(kek).decrypt(nonce, ciphertext, aad)
