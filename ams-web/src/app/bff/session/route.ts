// Login / logout. POST verifies the operator password and sets an httpOnly,
// SameSite=Strict session cookie. The response body NEVER contains the admin
// token or the session secret — only a boolean.
import { jsonResponse, serializeCookie } from '@/lib/server/http';
import {
  SESSION_COOKIE,
  issueSessionToken,
  sessionCookieAttributes,
  verifyLoginPassword,
} from '@/lib/server/session';
import { serverEnv } from '@/lib/server/env';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function POST(req: Request): Promise<Response> {
  let password: unknown;
  try {
    const body = (await req.json()) as { password?: unknown };
    password = body?.password;
  } catch {
    return jsonResponse(400, { error: 'invalid_body' });
  }
  if (typeof password !== 'string' || password.length === 0) {
    return jsonResponse(400, { error: 'password_required' });
  }
  if (!verifyLoginPassword(password)) {
    return jsonResponse(401, { error: 'invalid_credentials' });
  }
  const env = serverEnv();
  const token = await issueSessionToken();
  const cookie = serializeCookie(
    SESSION_COOKIE,
    token,
    sessionCookieAttributes(env.sessionTtlSeconds),
  );
  return jsonResponse(200, { ok: true }, { 'set-cookie': cookie });
}

export async function DELETE(): Promise<Response> {
  const cleared = serializeCookie(SESSION_COOKIE, '', {
    httpOnly: true,
    sameSite: 'strict',
    secure: process.env.NODE_ENV === 'production',
    path: '/',
    maxAge: 0,
  });
  return jsonResponse(200, { ok: true }, { 'set-cookie': cleared });
}
