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

// 계약 밖 추가분(account-remaining-usage-plan.md 1단계). 창 하나의 마지막
// 관측값 — stale이어도 값은 그대로 온다(값을 숨기지 않고 부모 stale로만 표시한다).
export interface AccountUsageWindowSummary {
  pct: number | null;
  resetsAt?: string | null;
}
// 계약 밖 추가분. window_minutes 300(5h)/10080(7d)로만 매칭한 창 요약.
// stale 판정 SSOT는 서버 app/services/pool.py의 _fresh_pct와 같은 규칙
// (pool_window_stale_minutes 이내 관측만 신선)이며, 여기서는 서버가 이미
// 판단해 내려준 값을 그대로 쓴다.
export interface AccountUsageSummary {
  fiveHour: AccountUsageWindowSummary | null;
  sevenDay: AccountUsageWindowSummary | null;
  fetchedAt?: string | null;
  stale: boolean;
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
  // 계약 밖 추가분. 서버가 항상 채워 보낸다(관측이 없어도 null 슬롯을 가진 객체).
  usage?: AccountUsageSummary | null;
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
  // Free-text label (a person, a team); see api-client Account.owner. Blank
  // means "org-wide" under rotation_scope=owner (server accepts any account).
  owner?: string | null;
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
  // 계정 풀 정책(설계: docs/design-notes/account-pool-automation-plan.md §2.2).
  // 서버가 풀 정책을 아직 안 실은 응답이면 undefined이고, 이때 화면은 기본값으로
  // 채운다. null은 서버가 정책 없음을 명시한 경우다.
  poolPolicy?: PoolPolicy | null;
  createdAt?: string;
}
export interface ServerCreate {
  name: string;
  hostname?: string;
  owner?: string;
  switchMode?: SwitchMode;
}
export interface ServerUpdate {
  name?: string;
  hostname?: string;
  // 미제공 = 유지. 명시적 ""는 조직 공용으로 되돌린다(AccountUpdate.owner와 같은 관례).
  owner?: string;
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

// GET …/servers/{sid}/usage 의 payload 는 amx.v1.UsageReport proto 를 서버가
// MessageToDict(preserving_proto_field_name=True) 로 저장한 것이라 키가 전부
// snake_case 다(ams-server/app/grpc/server.py:841). 스냅샷 껍데기(serverId 등)는
// Wire alias_generator 로 camelCase 지만 payload dict 내부는 변환되지 않는다.
// enum 은 이름 문자열로 온다: allocation_status="ALLOCATION_STATUS_ACTIVE",
// trigger="TRIGGER_SCHEDULE". MessageToDict 는 기본값(0·false·빈)을 생략하므로
// pct 0·is_current false 등은 키 자체가 없을 수 있다. 근거 proto:
// AccountUsage(:170)·AccountRef(:85)·PoolSummary(:195)·QuotaWindow(:147).
// drift 는 UsageSnapshot 응답 스키마(schemas.py:457 payload dict)에 실리지 않아
// 여기서 뺐다(별도 DB 컬럼이며 REST 로 노출되지 않음).
export interface UsageAccountRef {
  ams_account_id?: string;
  email?: string;
  account_uuid?: string;
  provider?: string;
}

export interface UsageQuotaWindow {
  id?: string;
  pct?: number;
  resets_at?: string;
  window_minutes?: number;
  model?: string;
}

// 위치형 legacy 창(five_hour/seven_day). P2b 이중 기록 이전 보고 호환용.
export interface UsagePositionalWindow {
  pct?: number;
  resets_at?: string;
}

export interface UsageSpend {
  used?: number;
  limit?: number;
  pct?: number;
  currency?: string;
  resets_at?: string;
}

export interface UsageAccount {
  account?: UsageAccountRef;
  allocation_status?: string;
  is_current?: boolean;
  five_hour?: UsagePositionalWindow;
  seven_day?: UsagePositionalWindow;
  usage_fetched_at?: string;
  windows?: UsageQuotaWindow[];
  spend?: UsageSpend;
  scoped_windows?: UsageQuotaWindow[];
}

export interface UsagePayload {
  schema_version?: number;
  agent_id?: string;
  generated_at?: string;
  trigger?: string;
  active_account?: UsageAccountRef;
  pool_summary?: {
    total?: number;
    active?: number;
    eligible?: number;
    quarantined?: number;
    all_exhausted?: boolean;
    max_utilization_pct?: number;
  };
  accounts?: UsageAccount[];
  in_response_to_command_id?: string;
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
  | 'credential_unusable'
  | 'account_window_high'
  | 'pool_chain_failed'
  | 'pool_usage_stale';
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

// -- 대시보드 집계 통계 (dashboard-redesign-plan.md 부록 A) -------------------
// GET /tenants/{id}/stats/*. 공통 쿼리는 range(기본 7d)뿐이고, 모든 응답이
// range·asOf를 함께 돌려준다. 서버 축(occupancy)은 seconds, 모델·계정 축은
// tokens — ams-server/app/services/stats.py 모듈 docstring과 같은 구분이다.
export type StatsRange = '24h' | '7d' | '30d';
export type StatsTimeseriesBy = 'model' | 'server' | 'account';

export interface StatsValuePrev {
  value: number;
  prev: number;
}

export interface StatsCostValue {
  // 당월 합계(usage/cost)이고 range와 무관하다. prev는 바로 전 달 합계.
  value: string;
  currency: string;
  prev: string;
}

export interface StatsSparkline {
  tokens: number[];
  sessions: number[];
}

export interface StatsSummary {
  range: StatsRange;
  asOf: string;
  tokens: StatsValuePrev;
  cost: StatsCostValue;
  sessions: StatsValuePrev;
  // 그 구간에 생성된 경보 수, 상태(open/acked/resolved) 무관.
  alertsOpened: StatsValuePrev;
  // 시간창과 무관하게 지금 open인 경보 총수. alertsOpened와 다른 질문에 답하는
  // 값이라 prev가 없다.
  alertsOpenNow: number;
  // 지금 시점 상태값이라 prev가 없다.
  serversOnline: number;
  accountsActive: number;
  sparkline: StatsSparkline;
}

export interface StatsSeries {
  key: string;
  label: string;
  values: number[];
}

export interface StatsTimeseries {
  range: StatsRange;
  asOf: string;
  unit: 'tokens' | 'seconds';
  buckets: string[];
  // 합계 상위 8개 + 나머지를 합친 "other"(있을 때만).
  series: StatsSeries[];
}

export interface StatsFlowNode {
  id: string;
  kind: 'server' | 'account';
  label: string;
}

export interface StatsFlowLink {
  source: string;
  target: string;
  value: number;
}

export interface StatsFlows {
  range: StatsRange;
  asOf: string;
  unit: 'seconds';
  nodes: StatsFlowNode[];
  links: StatsFlowLink[];
}

export interface StatsAccountRow {
  accountId: string;
  email?: string | null;
  provider?: string | null;
  tokens: number;
  sessions: number;
  messages: number;
  topModel?: string | null;
  topServerId?: string | null;
  // topServerId가 지금 서버 목록에 없으면(지워진 서버) "(삭제된 서버)".
  topServerName?: string | null;
  topProject?: string | null;
  heldSeconds: number;
  // account_usage_windows의 최신값(신선도 필터 없음) — 없으면 null.
  remaining5HPct?: number | null;
  remaining7DPct?: number | null;
}

export interface StatsAccounts {
  range: StatsRange;
  asOf: string;
  // 토큰 내림차순, 상위 50.
  rows: StatsAccountRow[];
}

export interface StatsServerCost {
  amount: string;
  currency: string;
}

// servers 테이블에 지금 없는 id(지워진 서버)와 server_id가 NULL인(미귀속) 세션의
// 합산 행이 status="deleted"를 공유한다 — 둘 다 "살아 있는 서버가 아니다"라는
// 점은 같고, 스키마에 별도 "unassigned" 상태가 없기 때문(services/stats.py 참고).
export type StatsServerStatus = 'online' | 'offline' | 'degraded' | 'deleted';

export interface StatsServerRow {
  // null이면 "(미귀속)" 행 — server_id가 NULL인 세션들을 합친 것.
  serverId: string | null;
  name: string;
  status: StatsServerStatus;
  heldSeconds: number;
  tokens: number;
  sessions: number;
  messages: number;
  topModel?: string | null;
  topAccountId?: string | null;
  topAccountEmail?: string | null;
  cost: StatsServerCost;
}

export interface StatsServers {
  range: StatsRange;
  asOf: string;
  // heldSeconds 내림차순.
  rows: StatsServerRow[];
}

export interface StatsHeatmap {
  range: StatsRange;
  asOf: string;
  // cells[요일][시간] = 세션 수. 요일 인덱스는 월요일=0(ISO), 시간은 UTC 0~23시.
  cells: number[][];
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

// -- 세션 실측 비용구조 (Stop 훅 수집) ----------------------------------------
// GET /tenants/{id}/usage/sessions?days=N. Langfuse 집계(위)가 합쳐서 보여주는
// 캐시 쓰기를 세션·모델 단위로 쪼갠 값이다. 1시간 캐시와 5분 캐시는 가격이 다른데
// Langfuse의 usageByType은 둘을 합쳐 보고하므로, 이 경로만 구분을 보존한다.
// 필드 이름의 1H/5M 대문자는 서버 스키마의 camelCase 별칭 생성 결과 그대로다.
export interface SessionUsageRow {
  sessionId: string;
  model: string;
  accountEmail?: string | null;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheCreate1HTokens: number;
  cacheCreate5MTokens: number;
  // 출력 토큰의 부분집합(더하는 값이 아니다).
  thinkingTokens: number;
  webSearchRequests: number;
  webFetchRequests: number;
  messageCount: number;
  // {요금 티어: 메시지 수}, {stop_reason: 메시지 수}. 값이 늘어도 타입은 그대로다.
  serviceTierCounts: Record<string, number>;
  stopReasonCounts: Record<string, number>;
  // 훅이 읽기 상한(줄 수·바이트·iterations)에 걸려 일부를 버린 세션이면 true.
  // 부분 집계이므로 합계를 그대로 신뢰하면 안 된다.
  truncated: boolean;
  startedAt?: string | null;
  endedAt?: string | null;
}

export interface SessionUsage {
  rows: SessionUsageRow[];
  // 훅이 마지막으로 보고한 시각. null이면 아직 수집이 없다(오류가 아니다).
  lastReportedAt?: string | null;
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

// -- 계정 풀 자동 배분 (design-notes/account-pool-automation-plan.md §2·§3·§4) --
// 계정만 "배급처(READY) → 대여중(LEASED) → 충전소(COOLING) → 배급처" 순환을 돌게
// 하는 풀 컨트롤러의 관측·조작 표면. 값·필드는 pool-api-contract.md 기준이며,
// 열거값이 늘어도 라벨 매핑(lib/pool.ts)이 원문 폴백을 하므로 화면은 죽지 않는다.
export type PoolState = 'ready' | 'leased' | 'recalling' | 'cooling' | 'pinned' | 'held';
export type PoolMode = 'manual' | 'auto';
export type RecommendationKind = 'prefetch' | 'swap' | 'recall_idle' | 'lease';
export type ChainStep = 'deliver' | 'switch' | 'recall' | 'done' | 'failed';
export type PoolEventKind =
  | 'state_changed'
  | 'recommendation_created'
  | 'recommendation_dropped'
  | 'chain_started'
  | 'chain_step'
  | 'chain_done'
  | 'chain_failed'
  | 'policy_changed'
  | 'automation_paused'
  | 'automation_resumed';

export interface PoolPolicy {
  mode: PoolMode;
  // 서버가 유지할 대여 계정 수(1~5). swap/prefetch는 소진 사용률 임계.
  targetLeases: number;
  swapAtPct: number;
  prefetchAtPct: number;
  minLeaseMinutes: number;
  // 충전소에서 배급처로 되돌릴 관측 사용률 상한.
  readyReturnPct: number;
}

// 한 사용량 창의 마지막 관측치. windowId는 five_hour·seven_day 등 프로바이더 로컬
// 식별자이며, resetsAt은 소진분이 풀리는 시각(없으면 null).
// 부적격 사유(서버 services.pool.ineligible_reason와 같은 값). 적격이면 null.
export type IneligibleReason =
  | 'api_key'
  | 'excluded'
  | 'unusable'
  | 'pinned'
  | 'held'
  | 'no_observation'
  | 'stale_observation';

export interface WindowState {
  windowId: string;
  // 관측을 못 읽은 창은 null이다(0으로 보내면 화면이 "여유 100%"로 오독한다).
  // null이면 카드는 "미상"으로 적고 막대를 그리지 않는다.
  pct: number | null;
  resetsAt?: string | null;
  usageFetchedAt?: string | null;
  reportedAt: string;
  serverId: string;
}

export interface PoolAccount {
  accountId: string;
  email: string;
  provider: Provider;
  poolState: PoolState;
  // COOLING일 때만 의미. 소진된 창들의 resetsAt 최댓값과 그 창.
  coolingUntil?: string | null;
  coolingWindowId?: string | null;
  leasedServerId?: string | null;
  leaseStartedAt?: string | null;
  lastLeaseEndedAt?: string | null;
  windows: WindowState[];
  poolStateChangedAt?: string | null;
  // 이 계정을 컨트롤러가 다룰 수 있는가와, 못 다룬다면 그 이유(지속적 사유만).
  // 대여·충전 같은 순환의 정상 국면은 부적격이 아니라 상태 열이 이미 보여준다.
  autoEligible: boolean;
  ineligibleReason?: IneligibleReason | null;
}

export interface PoolServer {
  serverId: string;
  name: string;
  status: ServerStatus;
  poolPolicy: PoolPolicy;
  leasedAccountIds: string[];
  activeAccountId?: string | null;
  // 전달·회수 명령이 아직 수렴 중이면 true. true인 서버엔 새 체인을 걸지 않는다.
  inFlight: boolean;
  maxPct?: number | null;
}

export interface Recommendation {
  id: string;
  serverId: string;
  kind: RecommendationKind;
  fromAccountId?: string | null;
  toAccountId?: string | null;
  reason: string;
  createdAt: string;
  triggerPct?: number | null;
}

export interface Chain {
  id: string;
  serverId: string;
  recommendationId?: string | null;
  fromAccountId?: string | null;
  toAccountId?: string | null;
  // 체인 종류. from·to만으로는 prefetch와 swap을 구분할 수 없어 서버가 함께 싣는다.
  kind: RecommendationKind;
  step: ChainStep;
  error?: string | null;
  startedAt: string;
  // 지금 단계가 시작된 시각. updatedAt은 같은 단계의 재발행에도 움직이므로
  // 단계 경과 시간은 이 값으로 잰다. 서버가 안 실으면 null.
  stepStartedAt?: string | null;
  updatedAt: string;
  // 실패한 체인을 운영자가 확인(:ack)한 시각. 확인 전이면 null.
  ackedAt?: string | null;
  // 'pool-controller' 또는 실행한 관리자 이메일.
  actor: string;
}

export interface PoolEvent {
  id: string;
  kind: PoolEventKind;
  accountId?: string | null;
  serverId?: string | null;
  detail: Record<string, unknown>;
  createdAt: string;
  actor: string;
}

export interface PoolOverview {
  automationPaused: boolean;
  accounts: PoolAccount[];
  servers: PoolServer[];
  recommendations: Recommendation[];
}

// pool:pause · pool:resume 응답.
export interface PoolPauseState {
  automationPaused: boolean;
}
