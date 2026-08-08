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

    return Settings(
        database_url=_require("AMX_DATABASE_URL"),
        admin_token=admin_token,
        encryption_key=encryption_key,
        oauth_flow_ttl_seconds=int(os.environ.get("AMX_OAUTH_FLOW_TTL_SECONDS", "600")),
        http_timeout_seconds=float(os.environ.get("AMX_HTTP_TIMEOUT_SECONDS", "15")),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
