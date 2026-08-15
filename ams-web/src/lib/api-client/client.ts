'use client';

// Browser-side client. Talks ONLY to same-origin /bff/api/*; it never sees the
// ams-server URL or the admin token. The BFF attaches those server-side.
import type {
  Account,
  AccountCreate,
  AccountPage,
  AccountUpdate,
  Alert,
  AlertPage,
  Assignment,
  AssignmentCreate,
  AssignmentPage,
  AssignmentState,
  CommandAccepted,
  EnrollTokenResponse,
  EventPage,
  LangfuseUsage,
  OauthCompleteRequest,
  OauthStartRequest,
  OauthStartResponse,
  SelfUpdateStatus,
  Server,
  ServerCreate,
  ServerPage,
  ServerUpdate,
  SwitchMode,
  Tenant,
  TenantCreate,
  TenantPage,
  UsageCostResponse,
  UsageSnapshot,
} from './types';

const BASE = '/bff/api';

export class BffError extends Error {
  status: number;
  code?: string;
  constructor(status: number, message: string, code?: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function bff<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}/${path}`, {
    method,
    headers: body !== undefined ? { 'content-type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    credentials: 'same-origin',
  });
  if (res.status === 401) {
    if (typeof window !== 'undefined') window.location.href = '/login';
    throw new BffError(401, 'Not authenticated');
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  let data: unknown = undefined;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!res.ok) {
    const d = data as { title?: string; detail?: string; code?: string } | undefined;
    throw new BffError(res.status, d?.detail || d?.title || `HTTP ${res.status}`, d?.code);
  }
  return data as T;
}

export const api = {
  // Tenants
  listTenants: () => bff<TenantPage>('GET', 'tenants'),
  createTenant: (b: TenantCreate) => bff<Tenant>('POST', 'tenants', b),
  deleteTenant: (id: string) => bff<void>('DELETE', `tenants/${id}`),

  // Accounts
  listAccounts: (t: string) => bff<AccountPage>('GET', `tenants/${t}/accounts`),
  createAccount: (t: string, b: AccountCreate) =>
    bff<Account>('POST', `tenants/${t}/accounts`, b),
  updateAccount: (t: string, id: string, b: AccountUpdate) =>
    bff<Account>('PATCH', `tenants/${t}/accounts/${id}`, b),
  deleteAccount: (t: string, id: string) =>
    bff<void>('DELETE', `tenants/${t}/accounts/${id}`),
  oauthStart: (t: string, b: OauthStartRequest) =>
    bff<OauthStartResponse>('POST', `tenants/${t}/accounts:oauth-start`, b),
  oauthComplete: (t: string, b: OauthCompleteRequest) =>
    bff<Account>('POST', `tenants/${t}/accounts:oauth-complete`, b),

  // Servers
  listServers: (t: string) => bff<ServerPage>('GET', `tenants/${t}/servers`),
  createServer: (t: string, b: ServerCreate) =>
    bff<Server>('POST', `tenants/${t}/servers`, b),
  updateServer: (t: string, id: string, b: ServerUpdate) =>
    bff<Server>('PATCH', `tenants/${t}/servers/${id}`, b),
  listServerEvents: (t: string, id: string, pageToken?: string) =>
    bff<EventPage>(
      'GET',
      `tenants/${t}/servers/${id}/events${pageToken ? `?pageToken=${encodeURIComponent(pageToken)}` : ''}`,
    ),
  deleteServer: (t: string, id: string) =>
    bff<void>('DELETE', `tenants/${t}/servers/${id}`),
  issueEnrollToken: (t: string, id: string) =>
    bff<EnrollTokenResponse>('POST', `tenants/${t}/servers/${id}/enroll-token`, {}),
  getUsage: (t: string, id: string) =>
    bff<UsageSnapshot>('GET', `tenants/${t}/servers/${id}/usage`),
  setSwitchMode: (t: string, id: string, mode: SwitchMode) =>
    bff<CommandAccepted>('POST', `tenants/${t}/servers/${id}:switch-mode`, { mode }),
  refreshUsage: (t: string, id: string) =>
    bff<CommandAccepted>('POST', `tenants/${t}/servers/${id}:refresh-usage`),
  serverSelfUpdate: (t: string, id: string) =>
    bff<CommandAccepted>('POST', `tenants/${t}/servers/${id}:self-update`),
  getSelfUpdateStatus: (t: string, id: string) =>
    bff<SelfUpdateStatus>('GET', `tenants/${t}/servers/${id}/self-update-status`),

  // Assignments
  listAssignments: (t: string) => bff<AssignmentPage>('GET', `tenants/${t}/assignments`),
  createAssignment: (t: string, b: AssignmentCreate) =>
    bff<Assignment>('POST', `tenants/${t}/assignments`, b),
  assignmentAction: (t: string, id: string, verb: AssignmentActionVerb, body?: unknown) =>
    bff<Assignment | CommandAccepted>('POST', `tenants/${t}/assignments/${id}:${verb}`, body),

  // Usage cost — month is YYYY-MM; omitted means the current UTC month
  // (ams-server fills it in). The caller only ever builds valid months.
  getUsageCost: (t: string, month?: string) =>
    bff<UsageCostResponse>(
      'GET',
      `tenants/${t}/usage/cost${month ? `?month=${encodeURIComponent(month)}` : ''}`,
    ),

  // Langfuse 실측 사용량 — from/to는 YYYY-MM-DD. UI가 언제나 유효한 날짜만 만든다.
  getLangfuseUsage: (t: string, from: string, to: string) =>
    bff<LangfuseUsage>(
      'GET',
      `tenants/${t}/usage/langfuse?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`,
    ),

  // Alerts (Track A backend; UI ready)
  listAlerts: (t: string, status?: string) =>
    bff<AlertPage>('GET', `tenants/${t}/alerts${status ? `?status=${status}` : ''}`),
  ackAlert: (t: string, id: string) =>
    bff<Alert>('POST', `tenants/${t}/alerts/${id}:ack`),
};

// -- 오류 코드 한글화 ---------------------------------------------------------
// ams-server가 problem+json의 `code`로 내려보내는 도메인 오류를, 사용자가 다음
// 행동을 알 수 있는 문장으로 바꾼다. 여기 없는 코드는 상류 detail을 그대로 쓴다
// (영문일 수 있으나 정보 손실은 없다).
const KR_API_ERROR: Record<string, string> = {
  'account.provider_unsupported':
    '지원하지 않는 프로바이더입니다. 현재 등록 가능한 것은 Claude와 Codex뿐입니다.',
  'account.codex_credential_invalid':
    'auth.json을 읽을 수 없습니다. 로컬에서 codex login으로 만들어진 ~/.codex/auth.json 전문을 그대로 붙여넣었는지 확인하세요.',
  'account.codex_email_mismatch':
    '붙여넣은 auth.json은 다른 계정의 자격증명입니다. 입력한 이메일과 auth.json이 같은 계정의 것인지 확인하세요.',
  'account.codex_email_requires_credential':
    'Codex 계정의 이메일을 바꾸려면 새 auth.json을 같은 요청에 함께 넣어야 합니다. 자격증명 칸에 바뀐 계정의 auth.json을 붙여넣으세요.',
  'assignment.server_codex_capacity':
    '이 서버에는 이미 Codex 계정이 연결돼 있습니다. Codex는 호스트당 자격증명을 하나만 두므로, 기존 Codex 연결을 회수한 뒤 다시 연결하세요.',
};

// 자격증명 파싱 실패만은 상류 detail을 덧붙인다. 서버가 문제된 KEY 이름만
// 담도록 보장하므로(inventory._codex_invalid) 토큰 값이 새지 않는다.
const KEEP_DETAIL = new Set(['account.codex_credential_invalid']);

export function krApiError(e: unknown): string {
  if (e instanceof BffError && e.code) {
    const kr = KR_API_ERROR[e.code];
    if (kr) return KEEP_DETAIL.has(e.code) ? `${kr} (${e.message})` : kr;
  }
  return e instanceof Error ? e.message : String(e);
}

export type AssignmentActionVerb =
  | 'deliver'
  | 'recall'
  | 'activate'
  | 'deactivate'
  | 'recover'
  | 'switch-now';

// §5.2 transition rules — which action verbs are legal from a given state, so
// the UI can disable illegal buttons.
export function allowedAssignmentActions(state: AssignmentState): AssignmentActionVerb[] {
  switch (state) {
    case 'pending':
      return ['deliver'];
    case 'active':
      return ['deactivate', 'recall', 'switch-now'];
    case 'inactive':
      return ['activate', 'recall'];
    case 'quarantined':
      return ['recover', 'recall'];
    case 'delivering':
    case 'recalling':
    case 'detached':
    default:
      return [];
  }
}
