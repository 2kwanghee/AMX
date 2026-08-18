// REST DTOs for the AMS management surface. Mirror of contracts/openapi.yaml
// (the SSOT). The generated @amx/contracts package covers the gRPC control
// plane, not this REST surface, so these are authored from openapi.yaml; the
// usage payload additionally follows contracts/schemas/usage-report.schema.json.

export type TenantStatus = 'active' | 'suspended';
export type AccountStatus = 'available' | 'assigned' | 'disabled' | 'quarantined';
export type CredentialType = 'oauth' | 'api_key';
export type Provider = 'claude' | 'codex';
export type ServerStatus = 'online' | 'offline' | 'degraded';
export type SwitchMode = 'auto' | 'manual';
export type SwitchStrategy = 'best' | 'next_available';
export type AssignmentState =
  | 'pending'
  | 'delivering'
  | 'active'
  | 'inactive'
  | 'quarantined'
  | 'recalling'
  | 'detached';

export interface PageInfo {
  nextPageToken?: string;
  totalSize?: number;
}

export interface Tenant {
  id: string;
  name: string;
  status: TenantStatus;
  createdAt: string;
  updatedAt?: string;
}
export interface TenantCreate {
  name: string;
}
export interface TenantUpdate {
  name?: string;
  status?: TenantStatus;
}

export interface Account {
  id: string;
  tenantId: string;
  provider: Provider;
  email: string;
  // Free-text label (a person, a team) for the console and for audit; not a
  // reference to an admin principal.
  owner?: string | null;
  // opt-in. true면 새 배정에서 제외된 계정 — 신규 배정만 막고 기존 배정은 그대로 둔다.
  assignmentExcluded?: boolean;
  credentialType: CredentialType;
  status: AccountStatus;
  secretMasked: string;
  // 월 구독료. 서버가 Decimal을 JSON 문자열로 내려보내므로 문자열이며(부동소수
  // 오차 방지), 미설정이면 null. 비용 배분의 held 근거가 된다.
  monthlyPrice?: string | null;
  currency?: string;
  accountUuid?: string;
  organizationName?: string;
  scopes?: string[];
  credentialExpiresAt?: string;
  lastSwitchedAt?: string;
  createdAt?: string;
}
export interface AccountCreate {
  email: string;
  provider?: Provider;
  credentialType: CredentialType;
  // For codex this is the complete ~/.codex/auth.json; for claude oauth the
  // credential set, for api_key the raw key. Write-only, never echoed back.
  secret: string;
  owner?: string;
  // 미제공 시 서버 기본값 false(배정 가능).
  assignmentExcluded?: boolean;
  // 금액은 문자열 그대로 전송(숫자 변환 금지 — 정밀도). currency 미제공 시 서버 기본 USD.
  monthlyPrice?: string;
  currency?: string;
}
export interface AccountUpdate {
  email?: string;
  status?: AccountStatus;
  secret?: string;
  owner?: string;
  // 미제공 = 유지. monthlyPrice와 달리 true/false 자체가 값이라 null 센티널이 없다.
  assignmentExcluded?: boolean;
  // 명시 null = 지우기(clear), 미제공 = 유지(서버 model_fields_set 기준).
  monthlyPrice?: string | null;
  currency?: string;
}

export interface OauthStartRequest {
  label?: string;
  provider?: Provider;
}
export interface OauthStartResponse {
  flowId: string;
  authorizeUrl: string;
  expiresAt: string;
}
export interface OauthCompleteRequest {
  flowId: string;
  code: string;
  email?: string;
  assignmentExcluded?: boolean;
}

export interface Server {
  id: string;
  tenantId: string;
  name: string;
  hostname?: string;
  switchMode: SwitchMode;
  // O4-B/O4-C central policy (design §O4). null = no central override; the
  // agent falls back to its tsamx-local default.
  thresholdPct?: number | null;
  defaultStrategy?: SwitchStrategy | null;
  cooldownSeconds?: number | null;
  hysteresisPct?: number | null;
  status: ServerStatus;
  agentId?: string;
  agentVersion?: string;
  tsamxVersion?: string;
  enrolled?: boolean;
  lastSeenAt?: string;
  assignedAccountCount?: number;
  // Host telemetry (metrics track, parallel dev). Absent until the agent
  // reports; the topology canvas renders a "미보고" fallback when undefined.
  cpuPct?: number;
  memPct?: number;
  diskPct?: number;
  metricsReportedAt?: string;
  createdAt?: string;
}
export interface ServerCreate {
  name: string;
  hostname?: string;
  switchMode?: SwitchMode;
}
export interface ServerUpdate {
  name?: string;
  hostname?: string;
  status?: ServerStatus;
  // O4 central policy. A field present with null clears the central override
  // back to the tsamx-local default; an omitted field is left untouched.
  thresholdPct?: number | null;
  defaultStrategy?: SwitchStrategy | null;
  cooldownSeconds?: number | null;
  hysteresisPct?: number | null;
}
export interface SwitchModeRequest {
  mode: SwitchMode;
}
export interface EnrollTokenResponse {
  token: string;
  expiresAt: string;
  amsEndpoint?: string | null;
  amsPubkey?: string | null;
}

// Latest self_update command projection (GET …/servers/{id}/self-update-status).
// All fields null when the server has never been asked to self-update.
export type SelfUpdateCommandStatus = 'queued' | 'sent' | 'acked' | 'failed';
export interface SelfUpdateStatus {
  status: SelfUpdateCommandStatus | null;
  detail?: string | null;
  createdAt?: string | null;
  sentAt?: string | null;
  ackedAt?: string | null;
}

export interface Assignment {
  id: string;
  tenantId: string;
  accountId: string;
  serverId: string;
  state: AssignmentState;
  pinned?: boolean;
  deliveredAt?: string;
  ackedAt?: string;
  lastError?: string;
  pendingCommandId?: string;
}
export interface AssignmentCreate {
  accountId: string;
  serverId: string;
  pinned?: boolean;
  deliverImmediately?: boolean;
}
export interface AssignmentUpdate {
  pinned?: boolean;
}
export interface SwitchNowRequest {
  strategy?: 'best' | 'next_available';
}

export interface CommandAccepted {
  commandId: string;
  acceptedAt: string;
  serverId?: string;
}

export interface UsageSnapshot {
  id?: string;
  serverId: string;
  accountId?: string;
  reportType: 'usage' | 'switch_event';
  reportedAt: string;
  payload: UsagePayload;
}

// Mirrors contracts/schemas/usage-report.schema.json (§6.5).
export interface UsagePayload {
  schemaVersion?: number;
  reportType?: string;
  agentId?: string;
  generatedAt?: string;
  trigger?: 'schedule' | 'ams_query' | 'switch';
  activeAccount?: { amsAccountId: string; email: string };
  poolSummary?: {
    total?: number;
    allExhausted?: boolean;
    maxUtilizationPct?: number;
  };
  accounts?: Array<{
    amsAccountId: string;
    email: string;
    allocationStatus: string;
    isCurrent: boolean;
    accountUuid?: string;
    usage?: {
      fiveHour?: { pct: number };
      sevenDay?: { pct: number };
      windows?: Array<{ id: string; pct: number; windowMinutes?: number; resetsAt?: string }>;
    };
  }>;
  // Reconcile drift, surfaced by ams-server (design §2, §5.4).
  drift?: Array<{ email?: string; amsAccountId?: string; detail?: string }>;
}

// -- Switch/quarantine/all_exhausted events (E2 timeline). ------------------
// GET …/servers/{sid}/events returns UsageSnapshot rows with
// reportType "switch_event". Unlike the usage payload, this payload is the raw
// AccountEvent proto rendered with proto field names, so its keys are
// snake_case (contracts/proto/amx.proto AccountEvent). kind/trigger arrive as
// their proto enum names, e.g. "KIND_SWITCH" / "TRIGGER_AT_LIMIT".
export interface EventAccountRef {
  ams_account_id?: string;
  email?: string;
  account_uuid?: string;
}
export interface EventPayload {
  kind?: string;
  trigger?: string;
  from?: EventAccountRef;
  to?: EventAccountRef;
  detail?: string;
  occurred_at?: string;
  pool_summary?: { total?: number; all_exhausted?: boolean; max_utilization_pct?: number };
}
export interface ServerEvent {
  id?: string;
  serverId: string;
  accountId?: string;
  reportType: 'switch_event';
  reportedAt: string;
  payload: EventPayload;
}

// -- Alerts (design §5.6 / p4-architecture §4). Not yet in openapi; Track A. ---
// ams-server models.ALERT_KINDS와 1:1로 맞춘다(14종). 여기서 빠진 kind가 응답에
// 실려도 krLabel이 string을 받아 화면은 죽지 않지만, 그만큼 타입이 계약 역할을
// 못 하므로 서버 쪽 kind를 추가할 때 이 목록도 같이 늘린다.
export type AlertKind =
  | 'all_exhausted'
  | 'drift'
  | 'server_offline'
  | 'quarantine'
  | 'recall_failed'
  | 'command_send_failed'
  | 'self_update_failed'
  | 'billing_watermark_future'
  | 'langfuse_usage_spike'
  | 'langfuse_stale'
  | 'langfuse_latency'
  | 'alert_webhook_dropped'
  | 'dangerous_command'
  | 'credential_unusable';
export type AlertSeverity = 'critical' | 'warning';
export type AlertStatus = 'open' | 'acked' | 'resolved';

export interface Alert {
  id: string;
  tenantId: string;
  serverId?: string;
  accountId?: string;
  kind: AlertKind;
  severity: AlertSeverity;
  status: AlertStatus;
  detail?: Record<string, unknown>;
  createdAt: string;
  ackedAt?: string;
  ackedBy?: string;
  resolvedAt?: string;
}

export interface Page<T> {
  items: T[];
  pageInfo?: PageInfo;
}
export type TenantPage = Page<Tenant>;
export type AccountPage = Page<Account>;
export type ServerPage = Page<Server>;
export type AssignmentPage = Page<Assignment>;
export type AlertPage = Page<Alert>;
export type EventPage = Page<ServerEvent>;

// -- 사용량·비용 배분 ---------------------------------------------------------
// GET /tenants/{id}/usage/cost. 서버는 Decimal을 JSON 문자열로 내려보내므로
// 금액·비율은 모두 string이다(부동소수 오차를 만들지 않도록 숫자로 되돌리지
// 않고 문자열 그대로 표시한다). 필드는 ams-server/app/schemas.py의
// UsageCost* Wire 모델(camelCase 별칭) 기준.

// 계정 가격이 이 서버에 놓인 근거. no_price·unallocated 줄의 cost는 0이며,
// 미배분 금액은 서버 줄이 아니라 subtotals.unallocatedCost에 잡힌다.
export type UsageCostBasis = 'held' | 'observed' | 'unallocated' | 'no_price';

export interface UsageCostAccountLine {
  accountId: string;
  email?: string | null;
  provider?: string | null;
  monthlyPrice?: string | null;
  currency: string;
  basis: UsageCostBasis | string;
  utilizationPct: string;
  sharePct: string;
  cost: string;
}

export interface UsageCostAmount {
  currency: string;
  amount: string;
}

export interface UsageCostServerLine {
  serverId: string;
  name?: string | null;
  utilizationPct: string;
  // 통화별로 나뉘어 오며 절대 합산되지 않는다(한 서버에 통화가 다른 계정이
  // 함께 놓일 수 있다).
  costs: UsageCostAmount[];
  accounts: UsageCostAccountLine[];
}

export interface UsageCostSubtotal {
  currency: string;
  allocatedCost: string;
  unallocatedCost: string;
}

export interface UsageCostResponse {
  month: string; // YYYY-MM
  asOf: string;
  // 이 UTC 날짜 이전은 확정(불변)이다. 롤업이 아직 안 돌았으면 null.
  watermark?: string | null;
  // 아직 확정되지 않은 구간이 포함돼 값이 움직일 수 있으면 true.
  isPartial: boolean;
  servers: UsageCostServerLine[];
  subtotals: UsageCostSubtotal[];
}

// -- Langfuse 실측 사용량 (P4) ------------------------------------------------
// GET /tenants/{id}/usage/langfuse?from=YYYY-MM-DD&to=YYYY-MM-DD.
// 비용 배분(위)과 달리 Langfuse가 관측한 토큰 실측치다. 토큰 값은 정수(Number
// 안전 범위 내)로 내려오므로 number로 둔다. 설정이 없는 서버에선 두 배열이 비고
// uiUrl은 null이다.
export interface LangfuseModelRow {
  day: string; // YYYY-MM-DD
  model: string;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheCreationTokens: number;
  totalTokens: number;
  observations: number;
}

export interface LangfuseUserRow {
  day: string; // YYYY-MM-DD
  userId: string;
  totalTokens: number;
  observations: number;
}

export interface LangfuseUsage {
  modelRows: LangfuseModelRow[];
  userRows: LangfuseUserRow[];
  uiUrl?: string | null;
}

// -- 감사 로그 (관리 API 감사 추적) ------------------------------------------
// GET /tenants/{id}/audit-logs?from&to&limit&pageToken. 관리자가 콘솔·API로
// 수행한 변경 연산의 추적 기록이다. from/to는 ISO 8601(생략 시 서버 기본 범위).
// 각 행은 한 번의 관리 요청 — method/path는 원 HTTP 요청, action은 서버가 분류한
// 도메인 동작명, targetId는 대상 리소스 id(없으면 null).
export interface AuditLogEntry {
  id: string;
  adminEmail: string;
  method: string;
  path: string;
  action: string;
  targetId?: string | null;
  statusCode: number;
  createdAt: string;
}
export type AuditLogPage = Page<AuditLogEntry>;

export interface ApiError {
  type?: string;
  title?: string;
  status: number;
  detail?: string;
  code?: string;
}
