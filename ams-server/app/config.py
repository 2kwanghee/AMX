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
    # Assignment-history retention (console-test gap G54): the sibling sweep
    # (…0A) batch-deletes `detached` assignment rows whose `updated_at` is older
    # than this many days. detached rows are pure audit history — a recalled
    # account no longer installed anywhere — so ageing them out keeps the table
    # from growing without bound. 0 or negative disables the sweep (keep every
    # detached row); the DELETE endpoint remains the manual path either way.
    assignment_retention_days: int = 90
    # Audit-log retention (console-test gap G53). The sibling sweep (…0B)
    # batch-deletes admin_audit_logs rows older than this many days. Unlike the
    # snapshot/assignment sweeps this defaults to 0 = **keep forever**: the audit
    # trail is a compliance record whose value is precisely its longevity, so a
    # bounded window is opt-in. Set > 0 only to satisfy an explicit retention cap.
    audit_retention_days: int = 0
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
    # P5 경보 웹훅(BACKLOG G41). AMS의 모든 경보 open/resolve 전이를 범용 웹훅으로
    # 내보낸다. URL과 시크릿이 **둘 다** 설정돼야 활성(webhook_enabled); 하나라도
    # 없으면 아웃박스 스테이징 자체를 건너뛰어 완전 무부작용이다. 시크릿은 발송
    # 서명(HMAC)에만 쓰이고 로그에 남기지 않는다.
    alert_webhook_url: str | None = None
    alert_webhook_secret: str | None = None
    # 웹훅 드레인은 offline 스위퍼와 분리된 전용 태스크로 돈다 — 불량 수신자가 오프라인
    # 탐지·명령 복구를 지연시키지 못하게. 자체 주기(기본 30초, 최소 5로 클램프)와, 발송
    # POST에만 쓰는 짧은 타임아웃(http_timeout과 분리, 기본 5초)을 둔다.
    alert_webhook_drain_seconds: int = 30
    alert_webhook_timeout_seconds: float = 5.0
    # P5 Langfuse 실측 임계값 경보(langfuse_alerts 스윕, langfuse 활성 게이트 공유).
    # usage_spike: 당일 총 토큰이 전일 대비 이 배수를 초과하면 open. 전일이 0이면
    # 배수가 무의미하므로 절대 하한(spike_min_tokens)을 초과할 때만 open.
    alert_spike_factor: float = 3.0
    alert_spike_min_tokens: int = 1_000_000
    # stale: 롤업 max(updated_at)이 이 분(minute)을 넘겨 정체되면 open.
    alert_stale_minutes: int = 60
    # latency: Metrics API latency p95(최근 1시간)가 이 밀리초를 초과하면 open.
    alert_latency_p95_ms: float = 60_000.0
    # P5 위험명령 통보 수신(danger_hook.py → POST /api/v1/ingest/danger-command).
    # 정적 토큰만으로 인증하는 무인 에이전트 발 호출이다(TenantScope 아님). 경보는
    # 실 테넌트에 귀속시켜 콘솔 목록·ack 동선에 정상 노출한다(langfuse 경보 관례와
    # 정렬): 귀속 테넌트는 `danger_tenant_id`, 없으면 `langfuse_tenant_id`로 폴백한다.
    # **토큰이 없거나 귀속 테넌트가 없으면 엔드포인트 자체가 비활성**(404)이라, 설정하지
    # 않은 AMS는 이 경로가 없는 것처럼 행동한다. 레이트 제한은 전역 고정창(분당 상한).
    danger_ingest_token: str | None = None
    danger_tenant_id: str | None = None
    danger_rate_limit_per_min: int = 120
    # 세션 비용구조 수신(session_usage_hook.py → POST /api/v1/ingest/session-usage).
    # danger 수신과 같은 무인 경로 규약(정적 토큰, TenantScope 아님, 미설정 시 404)이나
    # **토큰은 공유하지 않는다**: 위험명령 통보는 critical 경보를 여는 쓰기이고 이쪽은
    # 진단 집계 upsert라, 한 호스트에 세션 훅만 무장시킬 수 있어야 한다. 귀속 테넌트는
    # `session_tenant_id`, 없으면 `langfuse_tenant_id`로 폴백한다(danger와 같은 폴백
    # 모양이지만 danger 설정을 경유하지 않는다 — 두 경로는 서로 독립이다).
    session_ingest_token: str | None = None
    session_tenant_id: str | None = None
    session_rate_limit_per_min: int = 60
    # session_usage 보존 창(일). 이 테이블은 진단 입력이라 어떤 적분도 이 위에서
    # 돌지 않는다 — usage_snapshots의 정산 경계 가드가 필요 없는 단순 age purge다.
    # 하위 소비자가 없으므로 audit처럼 opt-in(0=영구)으로 두지 않고 기본 90일로
    # 켜 둔다. 0 이하면 purge 비활성.
    session_usage_retention_days: int = 90
    # 계정 풀 P1. 창 하나라도 이 % 에 닿으면 `account_window_high` 경고를 연다.
    # 서버 범위 `all_exhausted`(전원 pct>=95)가 이미 막힌 뒤에야 울리는 것과 달리,
    # 이건 아직 교체를 준비할 시간이 남아 있을 때 울리라고 기본값을 낮게 둔다.
    pool_window_high_pct: float = 80.0
    # 마지막 관측이 이 분 수보다 오래됐으면 "관측 없음"으로 본다. 충전소 복귀는
    # 시각(cooling_until)과 관측(pct<=readyReturnPct)을 함께 요구하는데, 에이전트가
    # 죽어 관측이 영영 안 오면 계정이 충전소에 갇힌다 — 그 유예 창이다.
    pool_observation_grace_minutes: int = 15
    # 체인 한 단계(deliver/switch/recall)가 이 분 수 안에 수렴하지 않으면 실패로
    # 접는다. 에이전트가 오프라인이면 명령은 큐에 남아 언젠가 전달되는데, 그 사이
    # 체인이 서버를 계속 점유하면 다음 교체가 영영 시작되지 않는다.
    # 창 관측이 이 분 수보다 오래됐으면 그 pct 는 미상으로 본다. 낡은 관측으로
    # 교체를 트리거하면 이미 리셋된 계정을 거두거나, 반대로 소진된 계정을 계속
    # 물린 채로 둔다. 모른다고 말하는 쪽이 틀린 값을 믿는 쪽보다 싸다.
    pool_window_stale_minutes: int = 30
    # pool_events 보존 창(일). 자동 변경 감사라 정산 경계 가드가 필요 없고, 나이만
    # 보고 지운다. 0 이하면 purge 비활성(영구 보존).
    pool_event_retention_days: int = 90
    pool_chain_step_timeout_minutes: int = 10
    # 한 테넌트에서 컨트롤러가 동시에 돌릴 수 있는 자동 체인 수. 관측이 한꺼번에
    # 틀렸을 때 피해 범위를 묶는 상한이다(운영자가 여는 체인은 세지 않는다).
    pool_max_concurrent_chains: int = 3

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

    @property
    def alert_webhook_enabled(self) -> bool:
        return bool(self.alert_webhook_url and self.alert_webhook_secret)

    @property
    def danger_tenant(self) -> str | None:
        """위험명령 경보를 귀속시킬 테넌트. 전용 설정이 우선, 없으면 langfuse 테넌트."""
        return self.danger_tenant_id or self.langfuse_tenant_id

    @property
    def danger_ingest_enabled(self) -> bool:
        # 토큰과 귀속 테넌트가 **둘 다** 있어야 활성. 어느 하나라도 없으면 경보를 어디에
        # 매달지 알 수 없으므로 엔드포인트를 404로 비활성한다.
        return bool(self.danger_ingest_token and self.danger_tenant)

    @property
    def session_tenant(self) -> str | None:
        """세션 사용량을 귀속시킬 테넌트. 전용 설정이 우선, 없으면 langfuse 테넌트."""
        return self.session_tenant_id or self.langfuse_tenant_id

    @property
    def session_ingest_enabled(self) -> bool:
        # 토큰과 귀속 테넌트가 **둘 다** 있어야 활성. 어느 하나라도 없으면 행을 어느
        # 테넌트에 넣을지 알 수 없으므로 엔드포인트를 404로 비활성한다.
        return bool(self.session_ingest_token and self.session_tenant)


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
        assignment_retention_days=int(
            os.environ.get("AMX_ASSIGNMENT_RETENTION_DAYS", "90")
        ),
        audit_retention_days=int(os.environ.get("AMX_AUDIT_RETENTION_DAYS", "0")),
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
        alert_webhook_url=os.environ.get("AMX_ALERT_WEBHOOK_URL", "").strip() or None,
        alert_webhook_secret=os.environ.get("AMX_ALERT_WEBHOOK_SECRET", "").strip() or None,
        alert_webhook_drain_seconds=int(
            os.environ.get("AMX_ALERT_WEBHOOK_DRAIN_SECONDS", "30")
        ),
        alert_webhook_timeout_seconds=float(
            os.environ.get("AMX_ALERT_WEBHOOK_TIMEOUT_SECONDS", "5")
        ),
        alert_spike_factor=float(os.environ.get("AMX_ALERT_SPIKE_FACTOR", "3.0")),
        alert_spike_min_tokens=int(
            os.environ.get("AMX_ALERT_SPIKE_MIN_TOKENS", "1000000")
        ),
        alert_stale_minutes=int(os.environ.get("AMX_ALERT_STALE_MINUTES", "60")),
        alert_latency_p95_ms=float(os.environ.get("AMX_ALERT_LATENCY_P95_MS", "60000")),
        danger_ingest_token=os.environ.get("AMX_DANGER_INGEST_TOKEN", "").strip() or None,
        danger_tenant_id=os.environ.get("AMX_DANGER_TENANT_ID", "").strip() or None,
        danger_rate_limit_per_min=int(
            os.environ.get("AMX_DANGER_RATE_LIMIT_PER_MIN", "120")
        ),
        session_ingest_token=os.environ.get("AMX_SESSION_INGEST_TOKEN", "").strip() or None,
        session_tenant_id=os.environ.get("AMX_SESSION_TENANT_ID", "").strip() or None,
        session_rate_limit_per_min=int(
            os.environ.get("AMX_SESSION_RATE_LIMIT_PER_MIN", "60")
        ),
        session_usage_retention_days=int(
            os.environ.get("AMX_SESSION_USAGE_RETENTION_DAYS", "90")
        ),
        pool_window_high_pct=float(os.environ.get("AMX_POOL_WINDOW_HIGH_PCT", "80")),
        pool_observation_grace_minutes=int(
            os.environ.get("AMX_POOL_OBSERVATION_GRACE_MINUTES", "15")
        ),
        pool_window_stale_minutes=int(
            os.environ.get("AMX_POOL_WINDOW_STALE_MINUTES", "30")
        ),
        pool_event_retention_days=int(
            os.environ.get("AMX_POOL_EVENT_RETENTION_DAYS", "90")
        ),
        pool_chain_step_timeout_minutes=int(
            os.environ.get("AMX_POOL_CHAIN_STEP_TIMEOUT_MINUTES", "10")
        ),
        pool_max_concurrent_chains=int(
            os.environ.get("AMX_POOL_MAX_CONCURRENT_CHAINS", "3")
        ),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
