// The proxy allowlist bounds the BFF to the known REST surface and blocks
// path traversal / SSRF attempts before any upstream call is made.
import { beforeEach, describe, expect, it } from 'vitest';
import { isAllowedPath } from '@/lib/server/upstream';
import { __resetServerEnvForTests } from '@/lib/server/env';
import { captured, resetCaptured } from './fake-ams';
import { GLOBAL_ADMIN } from './setup';
import { POST as sessionPost } from '@/app/bff/session/route';
import { GET as proxyGet } from '@/app/bff/api/[...path]/route';

describe('isAllowedPath', () => {
  it('accepts the documented REST surface', () => {
    for (const p of [
      'tenants',
      'tenants/ten-1',
      'tenants/ten-1/accounts',
      'tenants/ten-1/accounts:oauth-start',
      'tenants/ten-1/accounts:oauth-complete',
      'tenants/ten-1/accounts/acc-1',
      'tenants/ten-1/servers/srv-1/enroll-token',
      'tenants/ten-1/servers/srv-1/usage',
      'tenants/ten-1/servers/srv-1/events',
      'tenants/ten-1/servers/srv-1:refresh-usage',
      'tenants/ten-1/servers/srv-1:switch-mode',
      'tenants/ten-1/assignments/asg-1:deliver',
      'tenants/ten-1/alerts',
      'tenants/ten-1/alerts/alr-1:ack',
      'tenants/ten-1/pool',
      'tenants/ten-1/pool:pause',
      'tenants/ten-1/pool:resume',
      'tenants/ten-1/pool/recommendations',
      'tenants/ten-1/pool/chains',
      'tenants/ten-1/pool/events',
      'tenants/ten-1/pool/recommendations/rec-1:apply',
      'tenants/ten-1/pool/chains/chn-1:ack',
      'tenants/ten-1/servers/srv-1/pool-policy',
      'tenants/ten-1/accounts/acc-1/pool:pin',
      'tenants/ten-1/accounts/acc-1/pool:unpin',
      'tenants/ten-1/accounts/acc-1/pool:hold',
      'tenants/ten-1/accounts/acc-1/pool:release',
    ]) {
      expect(isAllowedPath(p), p).toBe(true);
    }
  });

  it('rejects traversal, absolute and unknown paths', () => {
    for (const p of [
      '../../etc/passwd',
      'tenants/../../secret',
      'tenants/ten-1/../ten-2/servers',
      'admin',
      'tenants/ten-1/accounts/acc-1/secret',
      'tenants/ten-1/assignments/asg-1:drop-table',
      'tenants/ten-1/accounts/acc-1/pool:drop',
      'tenants/ten-1/pool:halt',
      'tenants/ten-1/pool/secrets',
      'tenants/ten-1/pool/chains/chn-1:cancel',
      'http://evil.test/',
      'tenants//servers',
    ]) {
      expect(isAllowedPath(p), p).toBe(false);
    }
  });

  it('rejects encoded-slash/backslash smuggled inside an id segment', () => {
    // `%2f`/`%5c` pass the raw structural regex (id is `[^/:]+`) but uvicorn
    // decodes them to `/` `\` and reaches a deeper, unvetted route.
    for (const p of [
      'tenants/a/servers/b%2fsecret',
      'tenants/a/servers/b%2Fsecret',
      'tenants/a/servers/b%5csecret',
      'tenants/a/servers/b%5Csecret',
      'tenants/a%2f..%2fb/servers/c',
    ]) {
      expect(isAllowedPath(p), p).toBe(false);
    }
    // A normal tenant-scoped path is unaffected.
    expect(isAllowedPath('tenants/ten-1/servers/srv-1/usage')).toBe(true);
  });
});

describe('proxy rejects disallowed paths after auth', () => {
  beforeEach(() => {
    __resetServerEnvForTests();
    resetCaptured();
  });

  it('returns 404 and never calls upstream for an unknown path', async () => {
    const login = await sessionPost(
      new Request('http://localhost/bff/session', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ email: GLOBAL_ADMIN.email, password: GLOBAL_ADMIN.password }),
      }),
    );
    const cookie = login.headers
      .getSetCookie()
      .map((c) => c.split(';')[0])
      .join('; ');
    resetCaptured(); // drop the /auth/login call; assert the proxy never forwards
    const res = await proxyGet(
      new Request('http://localhost/bff/api/tenants/ten-1/../../etc', { headers: { cookie } }),
    );
    expect(res.status).toBe(404);
    expect(captured.length).toBe(0);
  });
});
