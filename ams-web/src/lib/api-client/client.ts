'use client';

// Browser-side client. Talks ONLY to same-origin /bff/api/*; it never sees the
// ams-server URL or the admin token. The BFF attaches those server-side.
import type {
  Account,
  AccountCreate,
  AccountPage,
  Alert,
  AlertPage,
  Assignment,
  AssignmentCreate,
  AssignmentPage,
  AssignmentState,
  CommandAccepted,
  EnrollTokenResponse,
  EventPage,
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

  // Alerts (Track A backend; UI ready)
  listAlerts: (t: string, status?: string) =>
    bff<AlertPage>('GET', `tenants/${t}/alerts${status ? `?status=${status}` : ''}`),
  ackAlert: (t: string, id: string) =>
    bff<Alert>('POST', `tenants/${t}/alerts/${id}:ack`),
};

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
