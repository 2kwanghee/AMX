// Page-level guard. Unauthenticated visitors to any app page are redirected to
// /login. The /bff/* handlers self-guard (excluded here). Session decryption
// uses Web Crypto (AES-GCM + HKDF), which runs in the Edge runtime.
import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

import { SESSION_COOKIE, decryptSession } from '@/lib/server/session';

export async function middleware(req: NextRequest) {
  const cookie = req.cookies.get(SESSION_COOKIE)?.value;
  const session = await decryptSession(cookie);
  if (!session) {
    const url = req.nextUrl.clone();
    url.pathname = '/login';
    url.search = '';
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

export const config = {
  // Everything except Next internals, the login page and the BFF handlers
  // (which enforce their own auth) and static assets.
  matcher: ['/((?!_next/static|_next/image|favicon.ico|bff/|login).*)'],
};
