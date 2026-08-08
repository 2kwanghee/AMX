// A minimal stateful fake of ams-server for BFF integration tests. One catch-all
// handler (msw path params choke on the `:verb` action suffix, so we branch by
// hand) that records every inbound request — crucially the Authorization header,
// so tests can prove the BFF attached the admin Bearer server-side.
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';

export const API_BASE = 'http://ams.test/api/v1';

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

  return HttpResponse.json({ title: 'not-mapped', status: 404, path }, { status: 404 });
}

export const server = setupServer(http.all(`${API_BASE}/*`, resolver));
