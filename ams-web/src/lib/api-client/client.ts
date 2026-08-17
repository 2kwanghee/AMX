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
  AuditLogPage,
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
  // detached 연결만 삭제 가능. 그 외 상태는 서버가 409 assignment.not_deletable.
  deleteAssignment: (t: string, id: string) =>
    bff<void>('DELETE', `tenants/${t}/assignments/${id}`),

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

  // 감사 로그 — from/to는 ISO 8601. UI가 언제나 유효한 값만 만든다. pageToken이
  // 있으면 '더 보기'로 다음 페이지를 이어 받는다.
  getAuditLogs: (
    t: string,
    params: { from?: string; to?: string; limit?: number; pageToken?: string } = {},
  ) => {
    const qs = new URLSearchParams();
    if (params.from) qs.set('from', params.from);
    if (params.to) qs.set('to', params.to);
    if (params.limit != null) qs.set('limit', String(params.limit));
    if (params.pageToken) qs.set('pageToken', params.pageToken);
    const q = qs.toString();
    return bff<AuditLogPage>('GET', `tenants/${t}/audit-logs${q ? `?${q}` : ''}`);
  },
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
  'assignment.not_deletable':
    'detached(회수 완료) 상태의 할당만 삭제할 수 있습니다. 먼저 회수한 뒤 다시 시도하세요.',
  'assignment.server_codex_capacity':
    '이 서버에는 이미 Codex 계정이 연결돼 있습니다. Codex는 호스트당 자격증명을 하나만 두므로, 기존 Codex 연결을 회수한 뒤 다시 연결하세요.',
  'assignment.account_excluded':
    '이 계정은 할당 대상에서 제외돼 있습니다. 할당하려면 계정 편집에서 제외를 먼저 해제하세요.',
  'assignment.account_already_assigned':
    '한 계정은 서버 하나에만 할당됩니다. 이미 다른 서버에 할당돼 있다면 그 할당을 회수한 뒤 다시 할당하세요.',

  // 상태 전이 거부 — 문장 구조를 일부러 통일했다. 운영자가 같은 자리에서 반복해
  // 읽는 문구라 일관성이 곧 가독성이다. 현재 상태는 목록의 상태 열에 이미 보여서
  // 영문 detail을 덧붙이지 않는다. 상태 이름은 KR_LABEL(components/common.tsx)과
  // 같은 어휘를 쓴다.
  'assignment.not_deliverable':
    '전달은 대기 상태의 할당에만 가능합니다. 이미 전달된 할당이면 회수한 뒤 다시 시도하세요.',
  'assignment.not_recallable':
    '회수는 자격증명이 설치된 할당에만 가능합니다. 대기나 해제됨 상태에는 회수할 것이 없습니다.',
  'assignment.not_activatable':
    '활성화는 비활성 상태의 할당에만 가능합니다. 목록의 상태를 확인하세요.',
  'assignment.not_deactivatable':
    '비활성화는 활성 상태의 할당에만 가능합니다. 목록의 상태를 확인하세요.',
  'assignment.not_recoverable':
    '복구는 격리됨 상태의 할당에만 가능합니다. 목록의 상태를 확인하세요.',
  'assignment.not_switchable':
    '즉시 전환은 자격증명이 설치된 할당에만 가능합니다. 활성이나 비활성 상태인지 확인하세요.',
  'assignment.recall_retries_exhausted':
    '회수가 재시도 한도까지 실패했습니다. recall_failed 경보가 열려 있으니 경보 화면에서 원인을 확인한 뒤 처리하세요.',
  'assignment.detached':
    '해제됨 상태의 할당은 수정하지 못합니다.',
  'assignment.no_fields':
    '변경할 내용이 없습니다.',
  'assignment.deliver_immediately_unsupported':
    'deliverImmediately는 지원하지 않습니다. 할당을 먼저 만들고 전달을 따로 실행하세요. 새 할당은 대기 상태로 남습니다.',

  'account.assigned':
    '이 계정은 아직 서버에 연결돼 있습니다. 할당을 회수하고 삭제한 뒤 계정을 지우세요.',
  'account.duplicate_email':
    '이 테넌트에 같은 이메일의 계정이 이미 있습니다. 다른 이메일을 쓰거나 기존 계정을 편집하세요.',

  'admin.duplicate_email':
    '같은 이메일로 등록된 관리자가 이미 있습니다. 새로 만들지 말고 기존 관리자를 편집하세요.',
  'admin.last_global_admin':
    '마지막으로 남은 전체 관리자는 비활성화하지 못합니다. 다른 전체 관리자를 먼저 추가하세요.',

  'alert.resolved':
    '이미 해결된 경보는 확인 처리 대상이 아닙니다.',

  'billing.void_not_applicable':
    '무효화는 일일 사용량 청구 이벤트에만 적용됩니다.',
  'billing.void_requires_exported':
    '내보내기가 끝난 청구 이벤트만 무효화합니다. 대기 중인 이벤트는 내보내기 전에 다시 집계해서 바로잡으세요.',

  // OAuth 등록 모달. flow_not_found의 상류 detail("Unknown, already-used or
  // expired enrollment flow")이 그대로 노출돼 온 것이 "모달을 두 번 열면 코드가
  // 엇갈린다"는 재발 패턴을 사용자가 모르는 원인이었다.
  'oauth.flow_not_found':
    '이미 사용했거나 만료된 인증 절차입니다. 인증 코드는 한 번만 쓸 수 있어서 모달을 두 번 열면 앞의 절차가 무효가 됩니다. 모달을 닫고 새로 시작하세요.',
  'oauth.missing_code':
    '인증 코드를 입력하지 않았습니다. 브라우저에서 받은 코드를 붙여넣으세요.',
  'oauth.exchange_rejected':
    '인증 코드가 거부됐습니다. 코드는 한 번만 쓸 수 있고 10분이면 만료됩니다. 모달을 닫고 처음부터 다시 시작하세요.',
  'oauth.exchange_malformed':
    '토큰 교환 응답이 JSON이 아닙니다. 잠시 후 인증을 다시 시작하세요.',
  'oauth.exchange_incomplete':
    '토큰 교환 응답에 access token이 없습니다. 인증을 처음부터 다시 시작하세요.',
  'oauth.email_unknown':
    '자격증명에 이메일이 들어 있지 않습니다. 요청에 이메일을 직접 넣어주세요.',
  'oauth.provider_unsupported':
    'AMS가 OAuth 절차를 대행하지 못하는 프로바이더입니다. 자격증명 파일을 직접 가져오는 방식으로 등록하세요.',

  'server.duplicate_name':
    '이 테넌트에 같은 이름의 서버가 이미 있습니다.',
  'server.has_assignments':
    '이 서버에 할당이 남아 있습니다. 할당을 모두 회수하고 삭제한 뒤 서버를 지우세요.',

  'tenant.duplicate_name':
    '같은 이름의 테넌트가 이미 있습니다.',
  'tenant.not_empty':
    '이 테넌트에 계정이나 서버가 남아 있습니다. 먼저 모두 삭제하세요.',
  'tenant.has_admins':
    '이 테넌트에 관리자가 남아 있습니다. 관리자를 먼저 삭제하세요.',
  'tenant.has_assignments':
    '이 테넌트에 할당이 남아 있습니다. 할당을 모두 회수한 뒤 다시 시도하세요.',
  'tenant.has_pending_billing':
    '이 테넌트에 대기 중인 청구 이벤트가 있습니다. 내보내거나 무효화한 뒤 다시 시도하세요.',
};

// 상류 detail을 한글 문장 뒤에 덧붙일 코드. 기준은 하나다 — detail이 담은
// 런타임 값을 콘솔 다른 곳에서 볼 수 없을 때만 붙인다(전이 거부의 현재 상태는
// 목록에 이미 보이므로 대상이 아니다). 붙이는 detail은 서버가 정수·enum·
// 프로바이더 이름만 담도록 보장하는 것들이라 토큰 값이 새지 않는다.
const KEEP_DETAIL = new Set([
  'account.codex_credential_invalid', // 문제된 KEY 이름(inventory._codex_invalid)
  'assignment.recall_retries_exhausted', // 실패 횟수·상한
  'oauth.exchange_rejected', // 업스트림 HTTP 상태
  'oauth.provider_unsupported', // OAuth 가능한 프로바이더 목록
]);

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
