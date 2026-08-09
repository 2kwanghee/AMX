import 'server-only';

import { parseCookies } from './http';
import { SESSION_COOKIE, decryptSession, type SessionData } from './session';

/**
 * Decrypt and validate the request's session cookie. Returns the session record
 * (including the upstream token) for server-side use, or null when the cookie is
 * missing, forged, tampered, or expired. The token in the result must never be
 * echoed to the browser.
 */
export async function getSession(req: Request): Promise<SessionData | null> {
  const cookies = parseCookies(req.headers.get('cookie'));
  return decryptSession(cookies[SESSION_COOKIE]);
}

/** True when the request carries a valid, unexpired session cookie. */
export async function isAuthenticated(req: Request): Promise<boolean> {
  return (await getSession(req)) !== null;
}
