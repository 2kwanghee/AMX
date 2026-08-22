// The one place ams-server is called. The upstream Bearer is attached here,
// server-side, and only here — it is the per-admin session token from the
// caller's encrypted cookie (NOT the shared root token). Client-supplied
// Authorization/Cookie headers are never forwarded upstream, and the upstream
// response is re-emitted with a minimal, safe header set so nothing leaks back
// to the browser.
import 'server-only';

import { serverEnv } from './env';
import type { Role } from './session';

// Structural allowlist of the ams-server REST surface (openapi.yaml) plus the
// P4 alerts/events endpoints (design §5.6, Track A). Anything else is refused
// before a request is ever made — this bounds the proxy to a known surface and
// blocks SSRF/path-traversal attempts. `[^/:]+` id segments so resource paths
// do not swallow a trailing `:verb`.
const ID = '[^/:]+';
const ALLOWLIST: RegExp[] = [
  new RegExp('^tenants$'),
  new RegExp(`^tenants/${ID}$`),
  new RegExp(`^tenants/${ID}/accounts$`),
  new RegExp(`^tenants/${ID}/accounts:oauth-(start|complete)$`),
  new RegExp(`^tenants/${ID}/accounts/${ID}$`),
  new RegExp(`^tenants/${ID}/servers$`),
  new RegExp(`^tenants/${ID}/servers/${ID}$`),
  new RegExp(`^tenants/${ID}/servers/${ID}/(enroll-token|usage|events|self-update-status)$`),
  new RegExp(`^tenants/${ID}/servers/${ID}:(refresh-usage|switch-mode|self-update)$`),
  new RegExp(`^tenants/${ID}/assignments$`),
  new RegExp(`^tenants/${ID}/assignments/${ID}$`),
  new RegExp(
    `^tenants/${ID}/assignments/${ID}:(deliver|recall|activate|deactivate|recover|switch-now)$`,
  ),
  new RegExp(`^tenants/${ID}/usage/cost$`),
  new RegExp(`^tenants/${ID}/usage/langfuse$`),
  new RegExp(`^tenants/${ID}/usage/sessions$`),
  // 대시보드 집계 통계(design-notes/dashboard-redesign-plan.md 부록 A).
  new RegExp(`^tenants/${ID}/stats/(summary|timeseries|flows|accounts|servers|heatmap)$`),
  new RegExp(`^tenants/${ID}/alerts$`),
  new RegExp(`^tenants/${ID}/alerts/${ID}:ack$`),
  new RegExp(`^tenants/${ID}/audit-logs$`),
  // 계정 풀(design-notes/account-pool-automation-plan.md, pool-api-contract.md).
  new RegExp(`^tenants/${ID}/pool$`),
  new RegExp(`^tenants/${ID}/pool:(pause|resume)$`),
  new RegExp(`^tenants/${ID}/pool/(recommendations|chains|events)$`),
  new RegExp(`^tenants/${ID}/pool/recommendations/${ID}:apply$`),
  new RegExp(`^tenants/${ID}/pool/chains/${ID}:ack$`),
  new RegExp(`^tenants/${ID}/servers/${ID}/pool-policy$`),
  new RegExp(`^tenants/${ID}/accounts/${ID}/pool:(pin|unpin|hold|release)$`),
];

const ALLOWED_METHODS = new Set(['GET', 'POST', 'PATCH', 'DELETE']);

export interface ProxyResult {
  status: number;
  body: string;
  contentType: string;
}

export function isAllowedPath(path: string): boolean {
  // path is the segment portion after /bff/api/, query already stripped.
  // Reject encoded slash/backslash outright: uvicorn decodes them upstream, so
  // a `%2f`/`%5c` smuggled inside an id segment would slip a raw path past the
  // structural allowlist (id segments are `[^/:]+`) while the *decoded* path
  // uvicorn actually routes reaches a deeper route the allowlist never vetted.
  if (/%2f|%5c/i.test(path)) {
    return false;
  }
  let decoded: string;
  try {
    decoded = decodeURIComponent(path);
  } catch {
    return false;
  }
  if (decoded.includes('..') || decoded.includes('\\') || decoded.includes('//')) {
    return false;
  }
  // No control characters.
  for (let i = 0; i < decoded.length; i++) {
    if (decoded.charCodeAt(i) < 0x20) return false;
  }
  // Match the allowlist against the DECODED path so the vetted surface equals
  // the path uvicorn will route after it decodes percent-escapes.
  return ALLOWLIST.some((re) => re.test(decoded));
}

/**
 * Forward a request to ams-server. `path` is the URL-encoded segment portion
 * after /bff/api/ (e.g. `tenants/x/assignments/y:deliver`); `search` is the raw
 * query string including a leading `?` (or empty).
 */
export async function proxyToUpstream(
  method: string,
  path: string,
  search: string,
  body: string | undefined,
  contentType: string | null,
  sessionToken: string,
): Promise<ProxyResult> {
  if (!ALLOWED_METHODS.has(method)) {
    return problem(405, 'method_not_allowed', 'Method not allowed.');
  }
  if (!isAllowedPath(path)) {
    return problem(404, 'unknown_path', 'No such resource on the management API.');
  }
  const env = serverEnv();
  const url = `${env.apiBase}/${path}${search}`;

  const headers: Record<string, string> = {
    // The caller's per-admin session token. ams-server derives the principal
    // (role + tenant scope) from it, so scoping is enforced upstream — the BFF
    // only carries the token. Lives here and nowhere the browser can reach.
    authorization: `Bearer ${sessionToken}`,
    accept: 'application/json',
  };
  const init: RequestInit = { method, headers, redirect: 'manual' };
  if (method !== 'GET' && method !== 'DELETE' && body !== undefined && body !== '') {
    headers['content-type'] = contentType || 'application/json';
    init.body = body;
  }

  let res: Response;
  try {
    res = await fetch(url, init);
  } catch {
    return problem(502, 'upstream_unreachable', 'ams-server is unreachable.');
  }
  const text = await res.text();
  const ct = res.headers.get('content-type') || 'application/json';
  return { status: res.status, body: text, contentType: ct };
}

function problem(status: number, code: string, title: string): ProxyResult {
  return {
    status,
    contentType: 'application/problem+json',
    body: JSON.stringify({ type: 'about:blank', title, status, code: `bff.${code}` }),
  };
}

// --- Auth endpoints (outside the proxy allowlist) ------------------------
// /auth/login is the one unauthenticated ams-server endpoint; /auth/logout
// revokes a session. Both are called directly by the session route handler,
// never proxied through /bff/api, so they are intentionally not in ALLOWLIST.

export interface UpstreamLogin {
  sessionToken: string;
  role: Role;
  tenantIds: string[];
  /** expires_at as epoch seconds, or null if unparseable. */
  expiresAtSeconds: number | null;
}

export type LoginOutcome =
  | { ok: true; login: UpstreamLogin }
  | { ok: false; status: number };

/** Exchange email+password for a per-admin session token via ams-server. */
export async function upstreamLogin(email: string, password: string): Promise<LoginOutcome> {
  const env = serverEnv();
  let res: Response;
  try {
    res = await fetch(`${env.apiBase}/auth/login`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', accept: 'application/json' },
      body: JSON.stringify({ email, password }),
      redirect: 'manual',
    });
  } catch {
    return { ok: false, status: 502 };
  }
  if (!res.ok) {
    // Collapse upstream 401/403/422 to 401 for the browser; anything else 502.
    await res.text().catch(() => '');
    const status = res.status === 401 || res.status === 403 || res.status === 422 ? 401 : 502;
    return { ok: false, status };
  }
  let data: {
    sessionToken?: unknown;
    role?: unknown;
    tenantIds?: unknown;
    expiresAt?: unknown;
  };
  try {
    data = (await res.json()) as typeof data;
  } catch {
    return { ok: false, status: 502 };
  }
  const st = data.sessionToken;
  const role = data.role;
  if (typeof st !== 'string' || st.length === 0) return { ok: false, status: 502 };
  if (role !== 'global-admin' && role !== 'tenant-admin') return { ok: false, status: 502 };
  const tenantIds = Array.isArray(data.tenantIds)
    ? data.tenantIds.filter((t): t is string => typeof t === 'string')
    : [];
  let expiresAtSeconds: number | null = null;
  if (typeof data.expiresAt === 'string') {
    const ms = Date.parse(data.expiresAt);
    if (!Number.isNaN(ms)) expiresAtSeconds = Math.floor(ms / 1000);
  }
  return {
    ok: true,
    login: { sessionToken: st, role, tenantIds, expiresAtSeconds },
  };
}

/** Revoke a session token upstream. Best-effort; failures are swallowed. */
export async function upstreamLogout(sessionToken: string): Promise<void> {
  const env = serverEnv();
  try {
    await fetch(`${env.apiBase}/auth/logout`, {
      method: 'POST',
      headers: { authorization: `Bearer ${sessionToken}`, accept: 'application/json' },
      redirect: 'manual',
    });
  } catch {
    // Logout is best-effort: even if the upstream call fails we still clear the
    // browser cookies, so the session is unusable from this client.
  }
}
