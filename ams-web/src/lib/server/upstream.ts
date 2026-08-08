// The one place ams-server is called. The admin Bearer is attached here,
// server-side, and only here. Client-supplied Authorization/Cookie headers are
// never forwarded upstream, and the upstream response is re-emitted with a
// minimal, safe header set so nothing leaks back to the browser.
import 'server-only';

import { serverEnv } from './env';

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
  new RegExp(`^tenants/${ID}/servers/${ID}/(enroll-token|usage|events)$`),
  new RegExp(`^tenants/${ID}/servers/${ID}:(refresh-usage|switch-mode)$`),
  new RegExp(`^tenants/${ID}/assignments$`),
  new RegExp(`^tenants/${ID}/assignments/${ID}$`),
  new RegExp(
    `^tenants/${ID}/assignments/${ID}:(deliver|recall|activate|deactivate|recover|switch-now)$`,
  ),
  new RegExp(`^tenants/${ID}/alerts$`),
  new RegExp(`^tenants/${ID}/alerts/${ID}:ack$`),
];

const ALLOWED_METHODS = new Set(['GET', 'POST', 'PATCH', 'DELETE']);

export interface ProxyResult {
  status: number;
  body: string;
  contentType: string;
}

export function isAllowedPath(path: string): boolean {
  // path is the segment portion after /bff/api/, query already stripped.
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
  return ALLOWLIST.some((re) => re.test(path));
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
    // The token lives here and nowhere the browser can reach.
    authorization: `Bearer ${env.adminToken}`,
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
