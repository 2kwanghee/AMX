// Login / logout.
//
// POST exchanges email+password for a per-admin session via ams-server, then
// sets two cookies: an AES-GCM-encrypted `amx_session` holding the upstream
// session token (SECRET, httpOnly) and a signed, readable `amx_nav` holding
// role + tenant_ids for client nav filtering. The response body NEVER contains
// the session token or the session secret.
//
// DELETE revokes the upstream session and clears both cookies.
import { parseCookies, serializeCookie } from '@/lib/server/http';
import {
  NAV_COOKIE,
  SESSION_COOKIE,
  clampMaxAge,
  decryptSession,
  encryptSession,
  navCookieAttributes,
  sessionCookieAttributes,
  signNav,
} from '@/lib/server/session';
import { serverEnv } from '@/lib/server/env';
import { upstreamLogin, upstreamLogout } from '@/lib/server/upstream';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

function jsonWithCookies(
  status: number,
  body: unknown,
  cookies: string[],
): Response {
  const headers = new Headers({ 'content-type': 'application/json' });
  for (const c of cookies) headers.append('set-cookie', c);
  return new Response(JSON.stringify(body), { status, headers });
}

export async function POST(req: Request): Promise<Response> {
  let email: unknown;
  let password: unknown;
  try {
    const body = (await req.json()) as { email?: unknown; password?: unknown };
    email = body?.email;
    password = body?.password;
  } catch {
    return jsonWithCookies(400, { error: 'invalid_body' }, []);
  }
  if (typeof email !== 'string' || email.length === 0) {
    return jsonWithCookies(400, { error: 'email_required' }, []);
  }
  if (typeof password !== 'string' || password.length === 0) {
    return jsonWithCookies(400, { error: 'password_required' }, []);
  }

  const outcome = await upstreamLogin(email, password);
  if (!outcome.ok) {
    const status = outcome.status === 401 ? 401 : 502;
    const err = status === 401 ? 'invalid_credentials' : 'upstream_error';
    return jsonWithCookies(status, { error: err }, []);
  }

  const { login } = outcome;
  const env = serverEnv();
  const nowSec = Math.floor(Date.now() / 1000);
  // Cookie lifetime follows the upstream session expiry, clamped to the BFF cap.
  const desired = login.expiresAtSeconds ? login.expiresAtSeconds - nowSec : env.sessionTtlSeconds;
  const maxAge = clampMaxAge(desired);
  const exp = nowSec + maxAge;

  const sessionValue = await encryptSession({
    st: login.sessionToken,
    sub: email,
    role: login.role,
    tenantIds: login.tenantIds,
    iat: nowSec,
    exp,
  });
  const navValue = await signNav({ role: login.role, tenantIds: login.tenantIds, exp });

  const cookies = [
    serializeCookie(SESSION_COOKIE, sessionValue, sessionCookieAttributes(maxAge)),
    serializeCookie(NAV_COOKIE, navValue, navCookieAttributes(maxAge)),
  ];
  // Body carries only non-secret hints so the client can render immediately.
  return jsonWithCookies(200, { ok: true, role: login.role, tenantIds: login.tenantIds }, cookies);
}

export async function DELETE(req: Request): Promise<Response> {
  // Revoke upstream if we can still read the token from the encrypted cookie.
  const cookies = parseCookies(req.headers.get('cookie'));
  const session = await decryptSession(cookies[SESSION_COOKIE]);
  if (session) await upstreamLogout(session.st);

  const clearSession = serializeCookie(SESSION_COOKIE, '', {
    ...sessionCookieAttributes(0),
  });
  const clearNav = serializeCookie(NAV_COOKIE, '', {
    ...navCookieAttributes(0),
  });
  return jsonWithCookies(200, { ok: true }, [clearSession, clearNav]);
}
