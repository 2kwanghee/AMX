// A minimal stateful fake of ams-server for BFF integration tests. One catch-all
// handler (msw path params choke on the `:verb` action suffix, so we branch by
// hand) that records every inbound request — crucially the Authorization header,
// so tests can prove the BFF attached the admin Bearer server-side.
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';

export const API_BASE = 'http://ams.test/api/v1';

// --- Admin fixtures ------------------------------------------------------
// The session_token is the SECRET the BFF forwards upstream; sentinels must
// never surface in any browser-visible output.
export interface AdminFixture {
  email: string;
  password: string;
  sessionToken: string;
  role: 'global-admin' | 'tenant-admin';
  tenantIds: string[];
}

export const GLOBAL_ADMIN: AdminFixture = {
  email: 'root@amx.test',
  password: 'global-pw-abcdef',
  sessionToken: 'SESSION_TOKEN_GLOBAL_SENTINEL_do-not-leak-1a2b3c4d',
  role: 'global-admin',
  tenantIds: [],
};

export const TENANT_ADMIN: AdminFixture = {
  email: 't1@amx.test',
  password: 'tenant-pw-abcdef',
  sessionToken: 'SESSION_TOKEN_TENANT_SENTINEL_do-not-leak-5e6f7a8b',
  role: 'tenant-admin',
  tenantIds: ['ten-1'],
};

const ADMINS = [GLOBAL_ADMIN, TENANT_ADMIN];
export const ALL_SESSION_TOKENS = ADMINS.map((a) => a.sessionToken);

export interface CapturedRequest {
  method: string;
  path: string; // portion after /api/v1/
  authorization: string | null;
  cookie: string | null;
  body: string;
}

export const captured: CapturedRequest[] = [];

export function resetCaptured() {
  captured.length = 0;
}

const iso = () => new Date().toISOString();

async function resolver({ request }: { request: Request }) {
  const url = new URL(request.url);
  const path = url.pathname.replace('/api/v1/', '');
  const body = request.method === 'GET' || request.method === 'DELETE' ? '' : await request.text();
  captured.push({
    method: request.method,
    path,
    authorization: request.headers.get('authorization'),
    cookie: request.headers.get('cookie'),
    body,
  });

  const m = request.method;

  // --- Auth endpoints ---
  if (path === 'auth/login' && m === 'POST') {
    let creds: { email?: string; password?: string } = {};
    try {
      creds = JSON.parse(body || '{}');
    } catch {
      /* fallthrough to 422 */
    }
    const admin = ADMINS.find((a) => a.email === creds.email && a.password === creds.password);
    if (!admin) {
      return HttpResponse.json({ title: 'invalid_credentials', status: 401 }, { status: 401 });
    }
    return HttpResponse.json({
      sessionToken: admin.sessionToken,
      role: admin.role,
      tenantIds: admin.tenantIds,
      expiresAt: new Date(Date.now() + 3600_000).toISOString(),
    });
  }
  if (path === 'auth/logout' && m === 'POST') {
    return new HttpResponse(null, { status: 204 });
  }

  if (path === 'tenants' && m === 'POST') {
    return HttpResponse.json({ id: 'ten-1', name: 'Acme', status: 'active', createdAt: iso() }, { status: 201 });
  }
  if (path === 'tenants' && m === 'GET') {
    return HttpResponse.json({ items: [{ id: 'ten-1', name: 'Acme', status: 'active', createdAt: iso() }] });
  }
  if (path === 'tenants/ten-1/accounts:oauth-start' && m === 'POST') {
    return HttpResponse.json(
      { flowId: 'flow-1', authorizeUrl: 'https://claude.ai/oauth?x=1', expiresAt: iso() },
      { status: 201 },
    );
  }
  if (path === 'tenants/ten-1/accounts:oauth-complete' && m === 'POST') {
    return HttpResponse.json(
      {
        id: 'acc-1', tenantId: 'ten-1', email: 'a@acme.io', credentialType: 'oauth',
        status: 'available', secretMasked: 'oauth:…AB3F',
      },
      { status: 201 },
    );
  }
  if (path === 'tenants/ten-1/servers' && m === 'POST') {
    return HttpResponse.json(
      { id: 'srv-1', tenantId: 'ten-1', name: 'ama-1', switchMode: 'manual', status: 'online' },
      { status: 201 },
    );
  }
  if (path === 'tenants/ten-1/servers/srv-1' && m === 'PATCH') {
    // Echo the policy fields back so the caller sees the applied values (the BFF
    // test asserts the PATCH body reached upstream).
    let patch: Record<string, unknown> = {};
    try {
      patch = JSON.parse(body || '{}');
    } catch {
      /* leave empty */
    }
    return HttpResponse.json({
      id: 'srv-1', tenantId: 'ten-1', name: 'ama-1', switchMode: 'manual', status: 'online',
      thresholdPct: patch.thresholdPct ?? null,
      defaultStrategy: patch.defaultStrategy ?? null,
      cooldownSeconds: patch.cooldownSeconds ?? null,
      hysteresisPct: patch.hysteresisPct ?? null,
    });
  }
  if (path === 'tenants/ten-1/servers/srv-1/events' && m === 'GET') {
    return HttpResponse.json({
      items: [
        {
          id: 'evt-1', serverId: 'srv-1', reportType: 'switch_event', reportedAt: iso(),
          payload: {
            kind: 'KIND_SWITCH', trigger: 'TRIGGER_AT_LIMIT',
            from: { email: 'a@acme.io' }, to: { email: 'b@acme.io' },
            detail: 'utilization crossed threshold',
          },
        },
        {
          id: 'evt-2', serverId: 'srv-1', reportType: 'switch_event', reportedAt: iso(),
          payload: { kind: 'KIND_QUARANTINE', from: { email: 'a@acme.io' }, detail: 'isolated' },
        },
      ],
      pageInfo: { totalSize: 2 },
    });
  }
  if (path === 'tenants/ten-1/assignments' && m === 'POST') {
    return HttpResponse.json(
      { id: 'asg-1', tenantId: 'ten-1', accountId: 'acc-1', serverId: 'srv-1', state: 'pending' },
      { status: 201 },
    );
  }
  if (path.startsWith('tenants/ten-1/assignments/asg-1:') && m === 'POST') {
    const verb = path.split(':')[1];
    const stateByVerb: Record<string, string> = {
      deliver: 'delivering', recall: 'recalling', activate: 'active',
      deactivate: 'inactive', recover: 'active',
    };
    if (verb === 'switch-now') {
      return HttpResponse.json({ commandId: 'cmd-1', acceptedAt: iso() }, { status: 202 });
    }
    return HttpResponse.json(
      { id: 'asg-1', tenantId: 'ten-1', accountId: 'acc-1', serverId: 'srv-1', state: stateByVerb[verb] ?? 'pending', pendingCommandId: 'cmd-1' },
      { status: 202 },
    );
  }
  if (path === 'tenants/ten-1/alerts' && m === 'GET') {
    return HttpResponse.json({
      items: [{ id: 'alr-1', tenantId: 'ten-1', serverId: 'srv-1', kind: 'all_exhausted', severity: 'critical', status: 'open', createdAt: iso() }],
    });
  }
  if (path === 'tenants/ten-1/alerts/alr-1:ack' && m === 'POST') {
    return HttpResponse.json({ id: 'alr-1', tenantId: 'ten-1', kind: 'all_exhausted', severity: 'critical', status: 'acked', createdAt: iso(), ackedAt: iso() });
  }

  // --- 계정 풀 ---
  if (path === 'tenants/ten-1/pool' && m === 'GET') {
    return HttpResponse.json({
      automationPaused: false,
      accounts: [
        {
          accountId: 'acc-1', email: 'a@acme.io', provider: 'claude', poolState: 'cooling',
          coolingUntil: new Date(Date.now() + 3600_000).toISOString(), coolingWindowId: 'five_hour',
          leasedServerId: null, windows: [
            { windowId: 'five_hour', pct: 92, resetsAt: iso(), reportedAt: iso(), serverId: 'srv-1' },
            { windowId: 'seven_day', pct: 40, reportedAt: iso(), serverId: 'srv-1' },
          ],
        },
      ],
      servers: [
        {
          serverId: 'srv-1', name: 'ama-1', status: 'online',
          poolPolicy: { mode: 'manual', targetLeases: 1, swapAtPct: 85, prefetchAtPct: 70, minLeaseMinutes: 30, readyReturnPct: 20 },
          leasedAccountIds: [], activeAccountId: null, inFlight: false, maxPct: 92,
        },
      ],
      recommendations: [
        { id: 'rec-1', serverId: 'srv-1', kind: 'swap', reason: 'five_hour 창 92%', createdAt: iso(), triggerPct: 92 },
      ],
    });
  }
  if (path === 'tenants/ten-1/pool/recommendations' && m === 'GET') {
    return HttpResponse.json([
      { id: 'rec-1', serverId: 'srv-1', kind: 'swap', reason: 'five_hour 창 92%', createdAt: iso(), triggerPct: 92 },
    ]);
  }
  if (path === 'tenants/ten-1/pool/recommendations/rec-1:apply' && m === 'POST') {
    return HttpResponse.json(
      { id: 'chn-1', serverId: 'srv-1', recommendationId: 'rec-1', step: 'deliver', startedAt: iso(), updatedAt: iso(), actor: 'root@amx.test' },
      { status: 202 },
    );
  }
  if (path === 'tenants/ten-1/pool/chains' && m === 'GET') {
    return HttpResponse.json([
      { id: 'chn-1', serverId: 'srv-1', step: 'switch', startedAt: iso(), updatedAt: iso(), actor: 'pool-controller' },
    ]);
  }
  if (path === 'tenants/ten-1/pool/events' && m === 'GET') {
    return HttpResponse.json([
      { id: 'pe-1', kind: 'state_changed', accountId: 'acc-1', serverId: 'srv-1', detail: {}, createdAt: iso(), actor: 'pool-controller' },
    ]);
  }
  if ((path === 'tenants/ten-1/pool:pause' || path === 'tenants/ten-1/pool:resume') && m === 'POST') {
    return HttpResponse.json({ automationPaused: path.endsWith('pause') });
  }
  if (path === 'tenants/ten-1/servers/srv-1/pool-policy' && m === 'PATCH') {
    let patch: Record<string, unknown> = {};
    try { patch = JSON.parse(body || '{}'); } catch { /* leave empty */ }
    return HttpResponse.json({
      id: 'srv-1', tenantId: 'ten-1', name: 'ama-1', switchMode: 'manual', status: 'online',
      poolPolicy: { mode: 'manual', targetLeases: 1, swapAtPct: 85, prefetchAtPct: 70, minLeaseMinutes: 30, readyReturnPct: 20, ...patch },
    });
  }
  if (path.startsWith('tenants/ten-1/accounts/acc-1/pool:') && m === 'POST') {
    const verb = path.split(':')[1];
    const stateByVerb: Record<string, string> = { pin: 'pinned', unpin: 'available', hold: 'quarantined', release: 'available' };
    return HttpResponse.json({
      id: 'acc-1', tenantId: 'ten-1', provider: 'claude', email: 'a@acme.io', credentialType: 'oauth',
      status: stateByVerb[verb] ?? 'available', secretMasked: 'oauth:…AB3F',
    });
  }

  return HttpResponse.json({ title: 'not-mapped', status: 404, path }, { status: 404 });
}

export const server = setupServer(http.all(`${API_BASE}/*`, resolver));
