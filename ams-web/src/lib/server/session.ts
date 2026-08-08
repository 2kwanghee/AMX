// Session cookie: a signed marker, NOT a token vault. It carries only a subject
// and expiry, HMAC-signed with AMX_SESSION_SECRET. The admin Bearer token is
// never placed inside it. Uses Web Crypto (globalThis.crypto.subtle) so the same
// code runs in the Node route handlers and the Edge middleware.
import 'server-only';

import { serverEnv } from './env';

export const SESSION_COOKIE = 'amx_session';

interface SessionPayload {
  sub: string;
  iat: number;
  exp: number;
}

function b64urlEncode(bytes: Uint8Array): string {
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function b64urlEncodeStr(s: string): string {
  return b64urlEncode(new TextEncoder().encode(s));
}

function b64urlDecodeStr(s: string): string {
  const pad = s.length % 4 === 0 ? '' : '='.repeat(4 - (s.length % 4));
  const bin = atob(s.replace(/-/g, '+').replace(/_/g, '/') + pad);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new TextDecoder().decode(bytes);
}

async function hmac(data: string, secret: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    'raw',
    new TextEncoder().encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  );
  const sig = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(data));
  return b64urlEncode(new Uint8Array(sig));
}

// Length-independent constant-time string compare.
function timingSafeEqual(a: string, b: string): boolean {
  const enc = new TextEncoder();
  const ab = enc.encode(a);
  const bb = enc.encode(b);
  let diff = ab.length ^ bb.length;
  const len = Math.max(ab.length, bb.length);
  for (let i = 0; i < len; i++) {
    diff |= (ab[i] ?? 0) ^ (bb[i] ?? 0);
  }
  return diff === 0;
}

export async function issueSessionToken(now = Date.now()): Promise<string> {
  const env = serverEnv();
  const payload: SessionPayload = {
    sub: 'admin',
    iat: Math.floor(now / 1000),
    exp: Math.floor(now / 1000) + env.sessionTtlSeconds,
  };
  const data = b64urlEncodeStr(JSON.stringify(payload));
  const sig = await hmac(data, env.sessionSecret);
  return `${data}.${sig}`;
}

export async function verifySessionToken(
  token: string | undefined | null,
  now = Date.now(),
): Promise<boolean> {
  if (!token) return false;
  const dot = token.indexOf('.');
  if (dot <= 0) return false;
  const data = token.slice(0, dot);
  const sig = token.slice(dot + 1);
  const env = serverEnv();
  const expected = await hmac(data, env.sessionSecret);
  if (!timingSafeEqual(sig, expected)) return false;
  try {
    const payload = JSON.parse(b64urlDecodeStr(data)) as SessionPayload;
    if (typeof payload.exp !== 'number') return false;
    if (Math.floor(now / 1000) >= payload.exp) return false;
    return payload.sub === 'admin';
  } catch {
    return false;
  }
}

/** Verify the operator's login secret against the configured password. */
export function verifyLoginPassword(password: string): boolean {
  const env = serverEnv();
  return timingSafeEqual(password, env.consolePassword);
}

export function sessionCookieAttributes(maxAgeSeconds: number): {
  httpOnly: true;
  sameSite: 'strict';
  secure: boolean;
  path: string;
  maxAge: number;
} {
  return {
    httpOnly: true,
    sameSite: 'strict',
    secure: process.env.NODE_ENV === 'production',
    path: '/',
    maxAge: maxAgeSeconds,
  };
}
