import { afterAll, afterEach, beforeAll } from 'vitest';
import { API_BASE, server } from './fake-ams';

// Break-glass root token — still in server env, but the login path must NOT use
// it. A distinctive sentinel so response/bundle scans can grep for a leak.
export const ADMIN_TOKEN = 'AMX_ADMIN_TOKEN_SENTINEL_do-not-leak-9f8e7d6c';
export const SESSION_SECRET = 'session-secret-abcdefghijklmnop';

// Admin fixtures (email/password/session_token) live in fake-ams so the fake
// server can authenticate against them; re-exported here for test convenience.
export { GLOBAL_ADMIN, TENANT_ADMIN, ALL_SESSION_TOKENS } from './fake-ams';
export type { AdminFixture } from './fake-ams';

process.env.AMX_API_BASE = API_BASE;
process.env.AMX_ADMIN_TOKEN = ADMIN_TOKEN;
process.env.AMX_SESSION_SECRET = SESSION_SECRET;
process.env.AMX_SESSION_TTL_SECONDS = '3600';

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
