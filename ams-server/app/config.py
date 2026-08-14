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
    # F5 billing: a UTC day D is only "closed" (eligible for a billing_events
    # row) once now >= D+1 00:00 + this grace, so a late-arriving usage report
    # for D still lands before the day is aggregated.
    billing_close_grace_seconds: int = 3600
    # Usage-cost integration: the step value of a usage snapshot represents time
    # only up to this many seconds before the next snapshot is required. A longer
    # real gap (agent offline / report loss) is clamped to this ceiling so a stale
    # observation cannot dominate the time-weighted utilization integral.
    usage_max_gap_seconds: int = 600
    # Console install-command support. `advertise_host` is the host (or IP) the
    # agent should dial; combined with `grpc_port` it forms the endpoint shown in
    # the enroll-token modal. Absent host → no endpoint (the console renders a
    # placeholder). `ams_pubkey` is the standard-base64 Ed25519 public key the
    # agent pins; derived from AMX_SIGNING_KEY when set, or taken verbatim from
    # AMX_AMS_PUBKEY, so the REST process advertises the same key the gRPC signer
    # holds.
    advertise_host: str | None = None
    grpc_port: int = 50051
    ams_pubkey: str | None = None

    @property
    def ams_endpoint(self) -> str | None:
        if not self.advertise_host:
            return None
        return f"{self.advertise_host}:{self.grpc_port}"


def _derive_ams_pubkey() -> str | None:
    """Standard-base64 Ed25519 public key for the enroll-token response.

    Prefers an explicit AMX_AMS_PUBKEY; otherwise derives it from the same
    AMX_SIGNING_KEY seed the gRPC signer loads (deploy/fullstack-run.sh keeps
    both in sync). Returns None when neither is configured.
    """
    explicit = os.environ.get("AMX_AMS_PUBKEY", "").strip()
    if explicit:
        return explicit
    seed_b64 = os.environ.get("AMX_SIGNING_KEY", "").strip()
    if not seed_b64:
        return None
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    padded = seed_b64 + "=" * (-len(seed_b64) % 4)
    seed = base64.urlsafe_b64decode(padded.encode())
    if len(seed) != 32:
        raise ConfigError("AMX_SIGNING_KEY must be a url-safe base64 32-byte Ed25519 seed.")
    pub = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return base64.b64encode(pub).decode()


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
        billing_close_grace_seconds=int(
            os.environ.get("AMX_BILLING_CLOSE_GRACE_SECONDS", "3600")
        ),
        usage_max_gap_seconds=int(
            os.environ.get("AMX_USAGE_MAX_GAP_SECONDS", "600")
        ),
        advertise_host=os.environ.get("AMX_ADVERTISE_HOST", "").strip() or None,
        grpc_port=int(os.environ.get("AMX_GRPC_PORT", "50051")),
        ams_pubkey=_derive_ams_pubkey(),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
