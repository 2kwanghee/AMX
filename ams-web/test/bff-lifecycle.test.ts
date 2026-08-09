// R3 completion gate: drive the BFF Route Handlers programmatically through the
// full account lifecycle (enroll -> assign -> deliver -> state transitions ->
// recall + alert ack) against a fake ams-server, asserting at every step that
// (a) the upstream call carried the caller's PER-ADMIN session token (not the
// shared break-glass root token) and (b) neither the root token nor the secret
// session token ever appears in any response body, header, or cookie handed to
// the browser. The session token now lives only inside the AES-GCM cookie.
import { beforeEach, describe, expect, it } from 'vitest';
import { ADMIN_TOKEN, GLOBAL_ADMIN, TENANT_ADMIN, ALL_SESSION_TOKENS, type AdminFixture } from './setup';
import { captured, resetCaptured } from './fake-ams';
import { __resetServerEnvForTests } from '@/lib/server/env';
import { DELETE as sessionDelete, POST as sessionPost } from '@/app/bff/session/route';
import { DELETE as proxyDelete, GET as proxyGet, PATCH as proxyPatch, POST as proxyPost } from '@/app/bff/api/[...path]/route';

const ORIGIN = 'http://localhost:3000';
const SECRETS = [ADMIN_TOKEN, ...ALL_SESSION_TOKENS];

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

// Assert no secret (root token OR any session token) leaks to the browser side.
async function assertNoLeak(res: Response) {
  const cloned = res.clone();
  const text = await cloned.text();
  for (const s of SECRETS) expect(text, 'body leak').not.toContain(s);
  for (const [, v] of res.headers) {
    for (const s of SECRETS) expect(v, 'header leak').not.toContain(s);
  }
  for (const c of res.headers.getSetCookie()) {
    for (const s of SECRETS) expect(c, 'cookie leak').not.toContain(s);
  }
}

// Log in as `admin`; returns the cookie header (both cookies) for proxy calls.
async function login(admin: AdminFixture): Promise<string> {
  const res = await sessionPost(
    new Request(`${ORIGIN}/bff/session`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ email: admin.email, password: admin.password }),
    }),
  );
  expect(res.status).toBe(200);
  const setCookies = res.headers.getSetCookie();
  expect(setCookies.length).toBe(2);
  await assertNoLeak(res);
  return setCookies.map((c) => c.split(';')[0]).join('; ');
}

function navCookieOf(setCookies: string[]): string {
  const nav = setCookies.find((c) => c.startsWith('amx_nav='));
  if (!nav) throw new Error('no amx_nav cookie');
  return nav;
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
        body: JSON.stringify({ email: GLOBAL_ADMIN.email, password: 'nope' }),
      }),
    );
    expect(res.status).toBe(401);
    expect(res.headers.getSetCookie().length).toBe(0);
  });

  it('requires both email and password', async () => {
    const res = await sessionPost(
      new Request(`${ORIGIN}/bff/session`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ password: 'x' }),
      }),
    );
    expect(res.status).toBe(400);
  });

  it('issues httpOnly+Strict encrypted session and readable Strict nav cookie; neither holds the session token', async () => {
    const res = await sessionPost(
      new Request(`${ORIGIN}/bff/session`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ email: GLOBAL_ADMIN.email, password: GLOBAL_ADMIN.password }),
      }),
    );
    const cookies = res.headers.getSetCookie();
    const session = cookies.find((c) => c.startsWith('amx_session='))!;
    const nav = cookies.find((c) => c.startsWith('amx_nav='))!;
    // Encrypted session cookie: httpOnly, Strict, and NOT the session token.
    expect(session).toContain('HttpOnly');
    expect(session).toContain('SameSite=Strict');
    expect(session).not.toContain(GLOBAL_ADMIN.sessionToken);
    // Nav cookie: readable (no HttpOnly), Strict, carries role but no secret.
    expect(nav).not.toContain('HttpOnly');
    expect(nav).toContain('SameSite=Strict');
    expect(nav).not.toContain(GLOBAL_ADMIN.sessionToken);
    expect(nav).not.toContain(ADMIN_TOKEN);
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

describe('per-admin token forwarding + nav cookie', () => {
  beforeEach(() => {
    __resetServerEnvForTests();
    resetCaptured();
  });

  it('forwards the caller-specific session token upstream, not a shared token', async () => {
    // Two different admins log in and each drive one request.
    const gCookie = await login(GLOBAL_ADMIN);
    await callProxy('GET', 'tenants', gCookie);
    const tCookie = await login(TENANT_ADMIN);
    await callProxy('GET', 'tenants', tCookie);

    const auths = captured.filter((c) => c.path === 'tenants').map((c) => c.authorization);
    expect(auths).toContain(`Bearer ${GLOBAL_ADMIN.sessionToken}`);
    expect(auths).toContain(`Bearer ${TENANT_ADMIN.sessionToken}`);
    // The break-glass root token is never used by the proxy.
    for (const c of captured) {
      expect(c.authorization).not.toBe(`Bearer ${ADMIN_TOKEN}`);
    }
  });

  it('tenant-admin nav cookie exposes role + own tenant only', async () => {
    const res = await sessionPost(
      new Request(`${ORIGIN}/bff/session`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ email: TENANT_ADMIN.email, password: TENANT_ADMIN.password }),
      }),
    );
    const nav = navCookieOf(res.headers.getSetCookie());
    const raw = decodeURIComponent(nav.split(';')[0].slice('amx_nav='.length));
    const json = JSON.parse(Buffer.from(raw.split('.')[0].replace(/-/g, '+').replace(/_/g, '/'), 'base64').toString('utf8'));
    expect(json.role).toBe('tenant-admin');
    expect(json.tenantIds).toEqual(['ten-1']);
  });
});

describe('BFF full lifecycle with token isolation', () => {
  beforeEach(() => {
    __resetServerEnvForTests();
    resetCaptured();
  });

  it('drives enroll -> assign -> deliver -> transitions -> recall + alert ack', async () => {
    const cookie = await login(GLOBAL_ADMIN);

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

    // Every upstream request carried the caller's session token, server-side.
    const dataCalls = captured.filter((c) => !c.path.startsWith('auth/'));
    expect(dataCalls.length).toBe(steps.length);
    for (const c of dataCalls) {
      expect(c.authorization, `auth on ${c.path}`).toBe(`Bearer ${GLOBAL_ADMIN.sessionToken}`);
      // The browser session cookie is never forwarded to ams-server.
      expect(c.cookie ?? '').not.toContain('amx_session');
    }

    // Spot-check the OAuth code reached upstream exactly once, via the BFF.
    const oauthCompletes = captured.filter((c) => c.path.endsWith('accounts:oauth-complete'));
    expect(oauthCompletes.length).toBe(1);
    expect(oauthCompletes[0]!.body).toContain('the-auth-code');
  });

  it('logout revokes upstream and clears both cookies', async () => {
    const cookie = await login(GLOBAL_ADMIN);
    resetCaptured();
    const res = await sessionDelete(
      new Request(`${ORIGIN}/bff/session`, { method: 'DELETE', headers: { cookie } }),
    );
    expect(res.status).toBe(200);
    // Upstream /auth/logout called with the caller's session token.
    const logout = captured.find((c) => c.path === 'auth/logout');
    expect(logout, 'logout reached upstream').toBeTruthy();
    expect(logout!.authorization).toBe(`Bearer ${GLOBAL_ADMIN.sessionToken}`);
    // Both cookies cleared.
    const cleared = res.headers.getSetCookie();
    expect(cleared.some((c) => c.startsWith('amx_session=') && c.includes('Max-Age=0'))).toBe(true);
    expect(cleared.some((c) => c.startsWith('amx_nav=') && c.includes('Max-Age=0'))).toBe(true);
  });
});
