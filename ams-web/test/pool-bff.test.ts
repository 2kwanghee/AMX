// 계정 풀 표면이 BFF 프록시를 통과하는지 잠근다. 허용목록이 GET·PATCH·POST와
// pool:pin / pool:pause 같은 콜론 동사, pool-policy 하위 경로를 모두 admit하고,
// 세션 토큰이 상류로 붙되 브라우저 응답으로는 새지 않음을 확인한다.
import { beforeEach, describe, expect, it } from 'vitest';
import { ADMIN_TOKEN, GLOBAL_ADMIN, ALL_SESSION_TOKENS } from './setup';
import { captured, resetCaptured } from './fake-ams';
import { __resetServerEnvForTests } from '@/lib/server/env';
import { POST as sessionPost } from '@/app/bff/session/route';
import { GET as proxyGet, PATCH as proxyPatch, POST as proxyPost } from '@/app/bff/api/[...path]/route';

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

describe('계정 풀 프록시', () => {
  beforeEach(() => {
    __resetServerEnvForTests();
    resetCaptured();
  });

  it('개요 GET을 세션 토큰과 함께 상류로 넘긴다', async () => {
    const cookie = await login();
    const res = await proxyGet(
      new Request(`${ORIGIN}/bff/api/tenants/ten-1/pool`, { headers: { cookie } }),
    );
    expect(res.status).toBe(200);
    await assertNoLeak(res);
    const body = await res.json();
    expect(body.automationPaused).toBe(false);
    expect(body.accounts[0].poolState).toBe('cooling');
    const hit = captured.find((c) => c.path === 'tenants/ten-1/pool' && c.method === 'GET');
    expect(hit?.authorization).toBe(`Bearer ${GLOBAL_ADMIN.sessionToken}`);
  });

  it('정책 PATCH 본문을 상류로 전달한다', async () => {
    const cookie = await login();
    const res = await proxyPatch(
      new Request(`${ORIGIN}/bff/api/tenants/ten-1/servers/srv-1/pool-policy`, {
        method: 'PATCH',
        headers: { cookie, 'content-type': 'application/json' },
        body: JSON.stringify({ mode: 'auto', targetLeases: 3 }),
      }),
    );
    expect(res.status).toBe(200);
    await assertNoLeak(res);
    const echoed = await res.json();
    expect(echoed.poolPolicy.mode).toBe('auto');
    expect(echoed.poolPolicy.targetLeases).toBe(3);
    const patch = captured.find((c) => c.path === 'tenants/ten-1/servers/srv-1/pool-policy');
    expect(patch?.method).toBe('PATCH');
    expect(patch?.body).toContain('targetLeases');
  });

  it('계정 콜론 동사와 일시정지 POST를 admit한다', async () => {
    const cookie = await login();
    const pin = await proxyPost(
      new Request(`${ORIGIN}/bff/api/tenants/ten-1/accounts/acc-1/pool:pin`, {
        method: 'POST',
        headers: { cookie },
      }),
    );
    expect(pin.status).toBe(200);
    await assertNoLeak(pin);
    expect((await pin.json()).status).toBe('pinned');

    const pause = await proxyPost(
      new Request(`${ORIGIN}/bff/api/tenants/ten-1/pool:pause`, { method: 'POST', headers: { cookie } }),
    );
    expect(pause.status).toBe(200);
    expect((await pause.json()).automationPaused).toBe(true);
  });

  it('권고 적용·체인·이벤트 경로가 열려 있다', async () => {
    const cookie = await login();
    const apply = await proxyPost(
      new Request(`${ORIGIN}/bff/api/tenants/ten-1/pool/recommendations/rec-1:apply`, {
        method: 'POST',
        headers: { cookie },
      }),
    );
    expect(apply.status).toBe(202);
    expect((await apply.json()).step).toBe('deliver');

    const chains = await proxyGet(
      new Request(`${ORIGIN}/bff/api/tenants/ten-1/pool/chains?status=active`, { headers: { cookie } }),
    );
    expect(chains.status).toBe(200);
    expect(Array.isArray(await chains.json())).toBe(true);

    const events = await proxyGet(
      new Request(`${ORIGIN}/bff/api/tenants/ten-1/pool/events?limit=100`, { headers: { cookie } }),
    );
    expect(events.status).toBe(200);
    expect((await events.json())[0].kind).toBe('state_changed');
  });

  it('실패 체인 확인(:ack) 경로가 열려 있다', async () => {
    const cookie = await login();
    const ack = await proxyPost(
      new Request(`${ORIGIN}/bff/api/tenants/ten-1/pool/chains/chn-2:ack`, {
        method: 'POST',
        headers: { cookie },
      }),
    );
    expect(ack.status).toBe(200);
    await assertNoLeak(ack);
    const body = await ack.json();
    expect(body.step).toBe('failed');
    expect(body.ackedAt).toBeTruthy();
    const hit = captured.find((c) => c.path === 'tenants/ten-1/pool/chains/chn-2:ack');
    expect(hit?.authorization).toBe(`Bearer ${GLOBAL_ADMIN.sessionToken}`);
  });
});
