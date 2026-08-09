// SERVER ONLY. Importing this module from a Client Component breaks the build
// (the `server-only` marker). This is the single place the break-glass admin
// Bearer token and the session secret are read; they must never be bundled for
// the browser.
import 'server-only';

export interface ServerEnv {
  /** ams-server REST root, including the /api/v1 prefix. */
  apiBase: string;
  /**
   * Break-glass root Bearer token for ams-server. Kept in the server-process
   * env so an operator can recover, but the normal login path no longer uses
   * it — per-admin session tokens (from POST /auth/login) are what the proxy
   * forwards upstream. Never returned to the browser.
   */
  adminToken: string;
  /**
   * Secret from which both the AES-GCM cookie key (session token, encrypted)
   * and the HMAC nav-cookie signing key are derived by HKDF. Never leaves the
   * server process.
   */
  sessionSecret: string;
  /** Upper bound on the session cookie lifetime, seconds. */
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
  const ttlRaw = process.env.AMX_SESSION_TTL_SECONDS;
  const sessionTtlSeconds = ttlRaw ? Math.max(60, parseInt(ttlRaw, 10) || 0) : 60 * 60 * 8;
  cached = { apiBase, adminToken, sessionSecret, sessionTtlSeconds };
  return cached;
}

// Test-only: drop the memoised env so a test can rebind process.env.
export function __resetServerEnvForTests(): void {
  cached = null;
}
