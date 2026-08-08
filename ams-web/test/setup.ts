import { afterAll, afterEach, beforeAll } from 'vitest';
import { API_BASE, server } from './fake-ams';

// Distinctive sentinel so response/bundle scans can grep for a leaked token.
export const ADMIN_TOKEN = 'AMX_ADMIN_TOKEN_SENTINEL_do-not-leak-9f8e7d6c';
export const CONSOLE_PASSWORD = 'console-pw-1234';
export const SESSION_SECRET = 'session-secret-abcdefghijklmnop';

process.env.AMX_API_BASE = API_BASE;
process.env.AMX_ADMIN_TOKEN = ADMIN_TOKEN;
process.env.AMX_CONSOLE_PASSWORD = CONSOLE_PASSWORD;
process.env.AMX_SESSION_SECRET = SESSION_SECRET;
process.env.AMX_SESSION_TTL_SECONDS = '3600';

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
