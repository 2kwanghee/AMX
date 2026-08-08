import 'server-only';

import { parseCookies } from './http';
import { SESSION_COOKIE, verifySessionToken } from './session';

/** True when the request carries a valid, unexpired session cookie. */
export async function isAuthenticated(req: Request): Promise<boolean> {
  const cookies = parseCookies(req.headers.get('cookie'));
  return verifySessionToken(cookies[SESSION_COOKIE]);
}
