"""Process configuration.

Every secret comes from the environment. A missing one is a startup failure,
never a default: an AMS that silently boots with a guessable admin token or an
ad-hoc encryption key would accept commands and write credential ciphertext
nobody can decrypt later.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


class ConfigError(RuntimeError):
    """Raised when required configuration is absent or unusable."""


@dataclass(frozen=True)
class Settings:
    database_url: str
    admin_token: str
    encryption_key: str
    # F2 envelope encryption (§7). `kek_provider` selects how tenant DEKs are
    # wrapped at rest; `kek` is the local provider's 32-byte KEK material
    # (urlsafe-base64), and when absent the local provider falls back to
    # `encryption_key` during the transition. The v2-write rollback boundary
    # (AMX_ENVELOPE_WRITE) is read live at call time in app.core.crypto, not
    # cached here, so it can be flipped without a restart.
    kek_provider: str = "local"
    kek: str | None = None
    oauth_flow_ttl_seconds: int = 600
    http_timeout_seconds: float = 15.0


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. AMS refuses to start without it "
            f"(docs/AMX-DESIGN.md §7)."
        )
    return value


def load_settings() -> Settings:
    encryption_key = _require("AMX_ENCRYPTION_KEY")
    # Fail here rather than on the first account write, when the caller's
    # plaintext credential is already in flight.
    from cryptography.fernet import Fernet

    try:
        Fernet(encryption_key.encode())
    except Exception as exc:  # noqa: BLE001 - surfaced as configuration error
        raise ConfigError(
            "AMX_ENCRYPTION_KEY is not a valid Fernet key "
            "(generate with `python -c \"from cryptography.fernet import Fernet; "
            'print(Fernet.generate_key().decode())"`)'
        ) from exc

    admin_token = _require("AMX_ADMIN_TOKEN")
    if len(admin_token) < 16:
        raise ConfigError("AMX_ADMIN_TOKEN must be at least 16 characters.")

    kek_provider = os.environ.get("AMX_KEK_PROVIDER", "local").strip() or "local"
    if kek_provider not in ("local", "aws-kms", "vault"):
        raise ConfigError(
            f"AMX_KEK_PROVIDER={kek_provider!r} is not one of local|aws-kms|vault."
        )
    kek = os.environ.get("AMX_KEK", "").strip() or None

    return Settings(
        database_url=_require("AMX_DATABASE_URL"),
        admin_token=admin_token,
        encryption_key=encryption_key,
        kek_provider=kek_provider,
        kek=kek,
        oauth_flow_ttl_seconds=int(os.environ.get("AMX_OAUTH_FLOW_TTL_SECONDS", "600")),
        http_timeout_seconds=float(os.environ.get("AMX_HTTP_TIMEOUT_SECONDS", "15")),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
