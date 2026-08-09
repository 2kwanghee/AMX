// Two cookies, two purposes:
//
//  * amx_session (SECRET): AES-256-GCM encrypted. Carries the per-admin upstream
//    session token so the BFF can forward it to ams-server. The token is never
//    exposed to the browser in readable form — the cookie value is ciphertext,
//    httpOnly, SameSite=Strict, Secure. This is the R3 promotion: the previous
//    signed-but-readable cookie could not safely hold a secret.
//
//  * amx_nav (NON-SECRET): HMAC-SHA256 signed, readable by the browser. Carries
//    role + tenant_ids purely so the client can filter navigation. Enforcement
//    is entirely ams-server's job; this is a UI convenience only.
//
// Both keys are HKDF-derived from AMX_SESSION_SECRET with distinct info labels,
// so a single deployment secret yields independent encryption and signing keys.
// Uses Web Crypto (globalThis.crypto.subtle) so the same code runs in the Node
// route handlers and the Edge middleware.
import 'server-only';

import { serverEnv } from './env';

export const SESSION_COOKIE = 'amx_session';
export const NAV_COOKIE = 'amx_nav';

export type Role = 'global-admin' | 'tenant-admin';

/** The secret record kept only inside the AES-GCM cookie. */
export interface SessionData {
  /** Upstream ams-server session token. SECRET — never leaves the server. */
  st: string;
  /** Subject (admin email), for logging/debugging on the server side. */
  sub: string;
  role: Role;
  tenantIds: string[];
  iat: number;
  exp: number;
}

/** The readable mirror kept in the signed (non-secret) nav cookie. */
export interface NavData {
  role: Role;
  tenantIds: string[];
  exp: number;
}

// Copy into an ArrayBuffer-backed view so the result satisfies BufferSource
// (TextEncoder returns Uint8Array<ArrayBufferLike>, which the Web Crypto DOM
// typings reject because ArrayBufferLike admits SharedArrayBuffer).
function enc(s: string): Uint8Array<ArrayBuffer> {
  return new Uint8Array(new TextEncoder().encode(s));
}

function b64urlEncode(bytes: Uint8Array): string {
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function b64urlDecode(s: string): Uint8Array<ArrayBuffer> {
  const pad = s.length % 4 === 0 ? '' : '='.repeat(4 - (s.length % 4));
  const bin = atob(s.replace(/-/g, '+').replace(/_/g, '/') + pad);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

function b64urlEncodeStr(s: string): string {
  return b64urlEncode(enc(s));
}

function b64urlDecodeStr(s: string): string {
  return new TextDecoder().decode(b64urlDecode(s));
}

// Length-independent constant-time compare of two base64url strings.
function timingSafeEqual(a: string, b: string): boolean {
  const ab = enc(a);
  const bb = enc(b);
  let diff = ab.length ^ bb.length;
  const len = Math.max(ab.length, bb.length);
  for (let i = 0; i < len; i++) {
    diff |= (ab[i] ?? 0) ^ (bb[i] ?? 0);
  }
  return diff === 0;
}

// --- HKDF key derivation (memoised per secret) ---------------------------

interface DerivedKeys {
  aes: CryptoKey;
  nav: CryptoKey;
}

const keyCache = new Map<string, Promise<DerivedKeys>>();

async function deriveKeys(secret: string): Promise<DerivedKeys> {
  const base = await crypto.subtle.importKey('raw', enc(secret), 'HKDF', false, ['deriveKey']);
  const salt = enc('amx-session-hkdf-v1');
  const aes = await crypto.subtle.deriveKey(
    { name: 'HKDF', hash: 'SHA-256', salt, info: enc('aes-gcm') },
    base,
    { name: 'AES-GCM', length: 256 },
    false,
    ['encrypt', 'decrypt'],
  );
  const nav = await crypto.subtle.deriveKey(
    { name: 'HKDF', hash: 'SHA-256', salt, info: enc('nav-hmac') },
    base,
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign', 'verify'],
  );
  return { aes, nav };
}

function keys(): Promise<DerivedKeys> {
  const secret = serverEnv().sessionSecret;
  let p = keyCache.get(secret);
  if (!p) {
    p = deriveKeys(secret);
    keyCache.set(secret, p);
  }
  return p;
}

// --- Encrypted session cookie -------------------------------------------

/** AES-256-GCM encrypt the session record. Format: b64url(iv).b64url(ct+tag). */
export async function encryptSession(data: SessionData): Promise<string> {
  const { aes } = await keys();
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = new Uint8Array(
    await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, aes, enc(JSON.stringify(data))),
  );
  return `${b64urlEncode(iv)}.${b64urlEncode(ct)}`;
}

/**
 * Decrypt + validate the session cookie. Returns null on any failure: bad
 * format, tampering (GCM tag mismatch), or expiry. The upstream token is only
 * ever available to server code that calls this.
 */
export async function decryptSession(
  cookie: string | undefined | null,
  now = Date.now(),
): Promise<SessionData | null> {
  if (!cookie) return null;
  const dot = cookie.indexOf('.');
  if (dot <= 0) return null;
  let iv: Uint8Array<ArrayBuffer>;
  let ct: Uint8Array<ArrayBuffer>;
  try {
    iv = b64urlDecode(cookie.slice(0, dot));
    ct = b64urlDecode(cookie.slice(dot + 1));
  } catch {
    return null;
  }
  if (iv.length !== 12 || ct.length === 0) return null;
  try {
    const { aes } = await keys();
    const pt = await crypto.subtle.decrypt({ name: 'AES-GCM', iv }, aes, ct);
    const data = JSON.parse(new TextDecoder().decode(pt)) as SessionData;
    if (typeof data.st !== 'string' || data.st.length === 0) return null;
    if (typeof data.exp !== 'number' || Math.floor(now / 1000) >= data.exp) return null;
    return data;
  } catch {
    return null;
  }
}

// --- Signed (readable) nav cookie ---------------------------------------

/** HMAC-sign the readable nav record. Format: b64url(json).b64url(sig). */
export async function signNav(nav: NavData): Promise<string> {
  const { nav: key } = await keys();
  const data = b64urlEncodeStr(JSON.stringify(nav));
  const sig = new Uint8Array(await crypto.subtle.sign('HMAC', key, enc(data)));
  return `${data}.${b64urlEncode(sig)}`;
}

/** Verify + parse the nav cookie. Returns null on bad signature or expiry. */
export async function verifyNav(
  cookie: string | undefined | null,
  now = Date.now(),
): Promise<NavData | null> {
  if (!cookie) return null;
  const dot = cookie.indexOf('.');
  if (dot <= 0) return null;
  const data = cookie.slice(0, dot);
  const sig = cookie.slice(dot + 1);
  const { nav: key } = await keys();
  const expected = new Uint8Array(await crypto.subtle.sign('HMAC', key, enc(data)));
  if (!timingSafeEqual(sig, b64urlEncode(expected))) return null;
  try {
    const nav = JSON.parse(b64urlDecodeStr(data)) as NavData;
    if (typeof nav.exp !== 'number' || Math.floor(now / 1000) >= nav.exp) return null;
    return nav;
  } catch {
    return null;
  }
}

// --- Cookie attributes ---------------------------------------------------

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

// The nav cookie is readable by client JS (role/tenant_ids drive UI filtering),
// so it is NOT httpOnly. It carries no secret; the signature only stops the
// browser from forging role hints the server would trust — but even that trust
// is only cosmetic, since ams-server is the sole enforcement point.
export function navCookieAttributes(maxAgeSeconds: number): {
  httpOnly: false;
  sameSite: 'strict';
  secure: boolean;
  path: string;
  maxAge: number;
} {
  return {
    httpOnly: false,
    sameSite: 'strict',
    secure: process.env.NODE_ENV === 'production',
    path: '/',
    maxAge: maxAgeSeconds,
  };
}

/** Clamp a desired cookie lifetime to [60, sessionTtlSeconds]. */
export function clampMaxAge(desiredSeconds: number): number {
  const cap = serverEnv().sessionTtlSeconds;
  if (!Number.isFinite(desiredSeconds)) return cap;
  return Math.max(60, Math.min(cap, Math.floor(desiredSeconds)));
}
