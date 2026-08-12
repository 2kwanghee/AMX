// REST DTOs for the AMS management surface. Mirror of contracts/openapi.yaml
// (the SSOT). The generated @amx/contracts package covers the gRPC control
// plane, not this REST surface, so these are authored from openapi.yaml; the
// usage payload additionally follows contracts/schemas/usage-report.schema.json.

export type TenantStatus = 'active' | 'suspended';
export type AccountStatus = 'available' | 'assigned' | 'disabled' | 'quarantined';
export type CredentialType = 'oauth' | 'api_key';
export type Provider = 'claude';
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
  credentialType: CredentialType;
  status: AccountStatus;
  secretMasked: string;
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
  secret: string;
}
export interface AccountUpdate {
  email?: string;
  status?: AccountStatus;
  secret?: string;
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
  amsEndpoint?: string;
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
    usage?: { fiveHour?: { pct: number }; sevenDay?: { pct: number } };
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
export type AlertKind =
  | 'all_exhausted'
  | 'drift'
  | 'server_offline'
  | 'quarantine'
  | 'recall_failed'
  | 'command_send_failed'
  | 'self_update_failed';
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

export interface ApiError {
  type?: string;
  title?: string;
  status: number;
  detail?: string;
  code?: string;
}
