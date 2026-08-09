// E1 (server policy edit) + E2 (event timeline) console gaps.
//   E1: the policy PATCH must traverse the BFF proxy to ams-server carrying the
//       caller's session token and the policy body — proving the allowlist
//       admits PATCH tenants/{t}/servers/{id} (it already did; this locks it in).
//   E2: formatEventRow (the timeline render logic) maps a raw AccountEvent
//       payload to the fields the table renders, and the events endpoint is
//       reachable through the proxy.
import { beforeEach, describe, expect, it } from 'vitest';
import { ADMIN_TOKEN, GLOBAL_ADMIN, ALL_SESSION_TOKENS } from './setup';
import { captured, resetCaptured } from './fake-ams';
import { __resetServerEnvForTests } from '@/lib/server/env';
import { POST as sessionPost } from '@/app/bff/session/route';
import { GET as proxyGet, PATCH as proxyPatch } from '@/app/bff/api/[...path]/route';
import { formatEventRow } from '@/lib/event-format';
import type { ServerEvent } from '@/lib/api-client/types';

const ORIGIN = 'http://localhost:3000';
const SECRETS = [ADMIN_TOKEN, ...ALL_SESSION_TOKENS];

async function login(): Promise<string> {
  const res = await sessionPost(
    new Request(`${ORIGIN}/bff/session`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ email: GLOBAL_ADMIN.email, password: GLOBAL_ADMIN.password }),
    }),
  );
  expect(res.status).toBe(200);
  return res.headers.getSetCookie().map((c) => c.split(';')[0]).join('; ');
}

async function assertNoLeak(res: Response) {
  const text = await res.clone().text();
  for (const s of SECRETS) expect(text, 'body leak').not.toContain(s);
}

describe('E1 — server policy PATCH through the BFF', () => {
  beforeEach(() => {
    __resetServerEnvForTests();
    resetCaptured();
  });

  it('forwards a policy PATCH to ams-server with the session token and body', async () => {
    const cookie = await login();
    const body = {
      thresholdPct: 90,
      defaultStrategy: 'best',
      cooldownSeconds: 300,
      hysteresisPct: 5,
    };
    const res = await proxyPatch(
      new Request(`${ORIGIN}/bff/api/tenants/ten-1/servers/srv-1`, {
        method: 'PATCH',
        headers: { cookie, 'content-type': 'application/json' },
        body: JSON.stringify(body),
      }),
    );
    expect(res.status).toBe(200);
    await assertNoLeak(res);
    const echoed = await res.json();
    expect(echoed.thresholdPct).toBe(90);
    expect(echoed.defaultStrategy).toBe('best');

    const patch = captured.find((c) => c.path === 'tenants/ten-1/servers/srv-1' && c.method === 'PATCH');
    expect(patch, 'PATCH reached upstream').toBeTruthy();
    expect(patch!.authorization).toBe(`Bearer ${GLOBAL_ADMIN.sessionToken}`);
    expect(patch!.body).toContain('thresholdPct');
    expect(patch!.body).toContain('cooldownSeconds');
  });
});

describe('E2 — event timeline', () => {
  beforeEach(() => {
    __resetServerEnvForTests();
    resetCaptured();
  });

  it('formatEventRow maps a switch payload to display fields', () => {
    const ev: ServerEvent = {
      id: 'evt-1',
      serverId: 'srv-1',
      reportType: 'switch_event',
      reportedAt: '2026-08-09T00:00:00Z',
      payload: {
        kind: 'KIND_SWITCH',
        trigger: 'TRIGGER_AT_LIMIT',
        from: { email: 'a@acme.io' },
        to: { email: 'b@acme.io' },
        detail: 'utilization crossed threshold',
      },
    };
    const row = formatEventRow(ev);
    expect(row.kind).toBe('switch');
    expect(row.trigger).toBe('at limit');
    expect(row.transition).toBe('a@acme.io → b@acme.io');
    expect(row.detail).toBe('utilization crossed threshold');
    expect(row.reportedAt).toBe('2026-08-09T00:00:00Z');
  });

  it('formatEventRow tolerates a quarantine payload with no "to" side', () => {
    const row = formatEventRow({
      serverId: 'srv-1',
      reportType: 'switch_event',
      reportedAt: '2026-08-09T00:00:00Z',
      payload: { kind: 'KIND_QUARANTINE', from: { email: 'a@acme.io' } },
    });
    expect(row.kind).toBe('quarantine');
    expect(row.trigger).toBe('');
    expect(row.transition).toBe('a@acme.io → —');
  });

  it('the events endpoint is reachable through the proxy', async () => {
    const cookie = await login();
    const res = await proxyGet(
      new Request(`${ORIGIN}/bff/api/tenants/ten-1/servers/srv-1/events`, {
        method: 'GET',
        headers: { cookie },
      }),
    );
    expect(res.status).toBe(200);
    await assertNoLeak(res);
    const page = await res.json();
    expect(page.items.length).toBe(2);
    expect(formatEventRow(page.items[0]).kind).toBe('switch');
  });
});
