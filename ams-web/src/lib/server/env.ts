// SERVER ONLY. Importing this module from a Client Component breaks the build
// (the `server-only` marker). This is the single place the admin Bearer token
// and session secret are read; they must never be bundled for the browser.
import 'server-only';

export interface ServerEnv {
  /** ams-server REST root, including the /api/v1 prefix. */
  apiBase: string;
  /** Administrator Bearer token for ams-server. Server-process env only. */
  adminToken: string;
  /**
   * Password the console operator types on the login page. Falls back to the
   * admin token when unset, so a single-secret deployment still works. Either
   * way the secret itself is never returned to the browser.
   */
  consolePassword: string;
  /** HMAC secret used to sign the session cookie. */
  sessionSecret: string;
  /** Session cookie lifetime, seconds. */
  sessionTtlSeconds: number;
}

function req(name: string): string {
  const v = process.env[name];
  if (!v || v.length === 0) {
    throw new Error(`Missing required environment variable ${name}`);
  }
  return v;
}

let cached: ServerEnv | null = null;

export function serverEnv(): ServerEnv {
  if (cached) return cached;
  const adminToken = req('AMX_ADMIN_TOKEN');
  const sessionSecret = req('AMX_SESSION_SECRET');
  if (sessionSecret.length < 16) {
    throw new Error('AMX_SESSION_SECRET must be at least 16 characters');
  }
  const apiBase = req('AMX_API_BASE').replace(/\/+$/, '');
  const consolePassword = process.env.AMX_CONSOLE_PASSWORD || adminToken;
  const ttlRaw = process.env.AMX_SESSION_TTL_SECONDS;
  const sessionTtlSeconds = ttlRaw ? Math.max(60, parseInt(ttlRaw, 10) || 0) : 60 * 60 * 8;
  cached = { apiBase, adminToken, consolePassword, sessionSecret, sessionTtlSeconds };
  return cached;
}

// Test-only: drop the memoised env so a test can rebind process.env.
export function __resetServerEnvForTests(): void {
  cached = null;
}
