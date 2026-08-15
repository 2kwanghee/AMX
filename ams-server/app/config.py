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
    # G27 watermark-future guard: the usage-rollup watermark is only flagged as
    # parked in the future once it sits more than this many seconds ahead of the
    # wall clock. The tolerance absorbs benign clock skew/jitter between the DB
    # and the AMS host so a few seconds of drift never raises a spurious alert.
    billing_watermark_skew_seconds: int = 300
    # Usage-snapshot retention: the raw JSONB usage_snapshots ledger grows ~5min
    # per server forever, so the retention sweep purges snapshots older than this
    # many days — but only once they are past the settlement watermark, never an
    # unsettled row. 0 or negative disables the purge (keep every snapshot).
    usage_snapshot_retention_days: int = 90
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
    # Artifact distribution (packaged install PR2). `artifacts_dir` is the
    # filesystem directory holding what `deploy/build-artifacts.sh` produced
    # (the ama binaries, the tsamx wheel and manifest.json). Absent or empty
    # means distribution is off and every /download route answers 404 — an AMS
    # that has no build output must not look like one that lost a file.
    # `install_scripts_dir` holds install.sh / install.ps1; it defaults to
    # `artifacts_dir` so a production host that copies the scripts next to the
    # binaries needs one setting, while dev points it at the repo's deploy/.
    artifacts_dir: str | None = None
    install_scripts_dir: str | None = None
    # P4 Langfuse console monitoring. The periodic metrics sweep and its REST read
    # are active only when all four of base_url / public_key / secret_key /
    # tenant_id are set; any missing → the sweep is a no-op and the endpoint
    # returns empty. `langfuse_tenant_id` is the AMS tenant whose account emails
    # the sweep loops over (as the Metrics API `userId` filter) and the only
    # tenant whose roll-up rows the REST returns. `langfuse_ui_url` is the console
    # deep-link base shown to operators; it falls back to `langfuse_base_url` at
    # the read layer, and to null when neither is set. `langfuse_metrics_window_days`
    # is the sliding re-aggregation window (idempotent upsert, so re-rolling recent
    # days each tick is safe).
    langfuse_base_url: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_tenant_id: str | None = None
    langfuse_ui_url: str | None = None
    # Re-aggregation window. Clamped to a floor of 2 at the sweep: a window of 1
    # rolls only today (still partial), so a UTC day's finalised total would never
    # be re-fetched after it closed and its last live value would be stored
    # forever — covering today+yesterday guarantees each day is re-rolled once
    # closed. Default 3 leaves extra slack for late-settling Langfuse data.
    langfuse_metrics_window_days: int = 3
    # The metrics sweep runs on its own cadence, independent of the 30s offline
    # sweeper tick that drives it: a tick sooner than this many seconds since the
    # last run returns immediately (process-local state; the advisory lock still
    # coordinates across instances). Clamped to a floor of 60 at the sweep.
    langfuse_poll_seconds: int = 300
    # The user axis issues one Metrics API call per account email; a tenant with
    # thousands of accounts would make the sweep unbounded, so it is capped — past
    # this many accounts the sweep warns and rolls only the first N (sorted).
    langfuse_max_accounts: int = 100

    @property
    def ams_endpoint(self) -> str | None:
        if not self.advertise_host:
            return None
        return f"{self.advertise_host}:{self.grpc_port}"

    @property
    def langfuse_enabled(self) -> bool:
        return bool(
            self.langfuse_base_url
            and self.langfuse_public_key
            and self.langfuse_secret_key
            and self.langfuse_tenant_id
        )


def _pubkey_from_seed(seed_b64: str) -> str:
    """Standard-base64 Ed25519 public key derived from a url-safe base64 seed."""
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


def _derive_ams_pubkey() -> str | None:
    """Standard-base64 Ed25519 public key for the enroll-token response.

    The value must equal the key the gRPC signer actually holds, so:

    * both AMX_AMS_PUBKEY and AMX_SIGNING_KEY set → they must agree, else the
      console would advertise a key the signer never signs with;
    * AMX_AMS_PUBKEY alone → refused: with no seed the signer generates a random
      key at startup that is guaranteed to diverge from the advertised one;
    * AMX_SIGNING_KEY alone → derive from it;
    * neither → None (dev without a fixed key; the console shows a placeholder).
    """
    explicit = os.environ.get("AMX_AMS_PUBKEY", "").strip() or None
    seed_b64 = os.environ.get("AMX_SIGNING_KEY", "").strip() or None
    derived = _pubkey_from_seed(seed_b64) if seed_b64 else None
    if explicit and seed_b64:
        if explicit != derived:
            raise ConfigError(
                "AMX_AMS_PUBKEY does not match the key derived from AMX_SIGNING_KEY; "
                "the console would advertise a public key the gRPC signer does not hold."
            )
        return explicit
    if explicit and not seed_b64:
        raise ConfigError(
            "AMX_AMS_PUBKEY is set without AMX_SIGNING_KEY. The gRPC signer would "
            "generate a random key that diverges from the advertised one; set "
            "AMX_SIGNING_KEY (the 32-byte seed) so both sides share one key."
        )
    return derived


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
    artifacts_dir = os.environ.get("AMX_ARTIFACTS_DIR", "").strip() or None

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
        billing_watermark_skew_seconds=int(
            os.environ.get("AMX_BILLING_WATERMARK_SKEW_SECONDS", "300")
        ),
        usage_snapshot_retention_days=int(
            os.environ.get("AMX_USAGE_SNAPSHOT_RETENTION_DAYS", "90")
        ),
        advertise_host=os.environ.get("AMX_ADVERTISE_HOST", "").strip() or None,
        grpc_port=int(os.environ.get("AMX_GRPC_PORT", "50051")),
        ams_pubkey=_derive_ams_pubkey(),
        artifacts_dir=artifacts_dir,
        install_scripts_dir=(
            os.environ.get("AMX_INSTALL_SCRIPTS_DIR", "").strip() or artifacts_dir
        ),
        langfuse_base_url=(
            os.environ.get("AMX_LANGFUSE_BASE_URL", "").strip().rstrip("/") or None
        ),
        langfuse_public_key=os.environ.get("AMX_LANGFUSE_PUBLIC_KEY", "").strip() or None,
        langfuse_secret_key=os.environ.get("AMX_LANGFUSE_SECRET_KEY", "").strip() or None,
        langfuse_tenant_id=os.environ.get("AMX_LANGFUSE_TENANT_ID", "").strip() or None,
        langfuse_ui_url=(
            os.environ.get("AMX_LANGFUSE_UI_URL", "").strip().rstrip("/") or None
        ),
        langfuse_metrics_window_days=int(
            os.environ.get("AMX_LANGFUSE_METRICS_WINDOW_DAYS", "3")
        ),
        langfuse_poll_seconds=int(os.environ.get("AMX_LANGFUSE_POLL_SECONDS", "300")),
        langfuse_max_accounts=int(os.environ.get("AMX_LANGFUSE_MAX_ACCOUNTS", "100")),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
