// R3 completion gate: drive the BFF Route Handlers programmatically through the
// full account lifecycle (enroll -> assign -> deliver -> state transitions ->
// recall + alert ack) against a fake ams-server, asserting at every step that
// (a) the upstream call carried the admin Bearer and (b) the admin token never
// appears in any response body, header, or cookie handed to the browser.
import { beforeEach, describe, expect, it } from 'vitest';
import { ADMIN_TOKEN, CONSOLE_PASSWORD } from './setup';
import { captured, resetCaptured } from './fake-ams';
import { __resetServerEnvForTests } from '@/lib/server/env';
import { DELETE as sessionDelete, POST as sessionPost } from '@/app/bff/session/route';
import { DELETE as proxyDelete, GET as proxyGet, PATCH as proxyPatch, POST as proxyPost } from '@/app/bff/api/[...path]/route';

const ORIGIN = 'http://localhost:3000';

function proxyReq(method: string, path: string, cookie?: string, body?: unknown): Request {
  return new Request(`${ORIGIN}/bff/api/${path}`, {
    method,
    headers: {
      ...(cookie ? { cookie } : {}),
      ...(body !== undefined ? { 'content-type': 'application/json' } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
}

const proxy = { GET: proxyGet, POST: proxyPost, PATCH: proxyPatch, DELETE: proxyDelete } as const;

async function callProxy(method: keyof typeof proxy, path: string, cookie?: string, body?: unknown) {
  return proxy[method](proxyReq(method, path, cookie, body));
}

// Assert nothing sensitive leaks to the browser side of a BFF response.
async function assertNoLeak(res: Response) {
  const cloned = res.clone();
  const text = await cloned.text();
  expect(text).not.toContain(ADMIN_TOKEN);
  for (const [, v] of res.headers) {
    expect(v).not.toContain(ADMIN_TOKEN);
  }
  const setCookie = res.headers.get('set-cookie');
  if (setCookie) expect(setCookie).not.toContain(ADMIN_TOKEN);
}

async function login(): Promise<string> {
  const res = await sessionPost(
    new Request(`${ORIGIN}/bff/session`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ password: CONSOLE_PASSWORD }),
    }),
  );
  expect(res.status).toBe(200);
  const setCookie = res.headers.get('set-cookie');
  expect(setCookie).toBeTruthy();
  await assertNoLeak(res);
  // Extract the cookie name=value pair.
  const pair = setCookie!.split(';')[0];
  return pair;
}

describe('BFF authentication', () => {
  beforeEach(() => {
    __resetServerEnvForTests();
    resetCaptured();
  });

  it('rejects a wrong password without setting a cookie', async () => {
    const res = await sessionPost(
      new Request(`${ORIGIN}/bff/session`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ password: 'nope' }),
      }),
    );
    expect(res.status).toBe(401);
    expect(res.headers.get('set-cookie')).toBeNull();
  });

  it('issues an httpOnly, SameSite=Strict session cookie that is not the admin token', async () => {
    const res = await sessionPost(
      new Request(`${ORIGIN}/bff/session`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ password: CONSOLE_PASSWORD }),
      }),
    );
    const setCookie = res.headers.get('set-cookie')!;
    expect(setCookie).toContain('HttpOnly');
    expect(setCookie).toContain('SameSite=Strict');
    expect(setCookie).not.toContain(ADMIN_TOKEN);
  });

  it('refuses proxy requests without a valid session', async () => {
    const res = await callProxy('GET', 'tenants');
    expect(res.status).toBe(401);
    expect(captured.length).toBe(0); // never reached upstream
  });

  it('refuses a forged/garbage session cookie', async () => {
    const res = await callProxy('GET', 'tenants', 'amx_session=forged.deadbeef');
    expect(res.status).toBe(401);
    expect(captured.length).toBe(0);
  });
});

describe('BFF full lifecycle with token isolation', () => {
  beforeEach(() => {
    __resetServerEnvForTests();
    resetCaptured();
  });

  it('drives enroll -> assign -> deliver -> transitions -> recall + alert ack', async () => {
    const cookie = await login();

    const steps: Array<[keyof typeof proxy, string, unknown?]> = [
      ['POST', 'tenants', { name: 'Acme' }],
      ['POST', 'tenants/ten-1/accounts:oauth-start', { label: 'primary' }],
      ['POST', 'tenants/ten-1/accounts:oauth-complete', { flowId: 'flow-1', code: 'the-auth-code' }],
      ['POST', 'tenants/ten-1/servers', { name: 'ama-1' }],
      ['POST', 'tenants/ten-1/assignments', { accountId: 'acc-1', serverId: 'srv-1' }],
      ['POST', 'tenants/ten-1/assignments/asg-1:deliver'],
      ['POST', 'tenants/ten-1/assignments/asg-1:deactivate'],
      ['POST', 'tenants/ten-1/assignments/asg-1:activate'],
      ['POST', 'tenants/ten-1/assignments/asg-1:switch-now', {}],
      ['POST', 'tenants/ten-1/assignments/asg-1:recall'],
      ['GET', 'tenants/ten-1/alerts'],
      ['POST', 'tenants/ten-1/alerts/alr-1:ack'],
    ];

    for (const [method, path, body] of steps) {
      const res = await callProxy(method, path, cookie, body);
      expect([200, 201, 202], `${method} ${path} -> ${res.status}`).toContain(res.status);
      await assertNoLeak(res);
    }

    // Every upstream request carried the admin Bearer, server-side.
    expect(captured.length).toBe(steps.length);
    for (const c of captured) {
      expect(c.authorization, `auth on ${c.path}`).toBe(`Bearer ${ADMIN_TOKEN}`);
      // The browser session cookie is never forwarded to ams-server.
      expect(c.cookie ?? '').not.toContain('amx_session');
    }

    // Spot-check the OAuth code reached upstream exactly once, via the BFF.
    const oauthCompletes = captured.filter((c) => c.path.endsWith('accounts:oauth-complete'));
    expect(oauthCompletes.length).toBe(1);
    expect(oauthCompletes[0]!.body).toContain('the-auth-code');
  });

  it('logout clears the session cookie', async () => {
    const res = await sessionDelete();
    expect(res.status).toBe(200);
    const setCookie = res.headers.get('set-cookie')!;
    expect(setCookie).toContain('amx_session=');
    expect(setCookie).toContain('Max-Age=0');
  });
});
