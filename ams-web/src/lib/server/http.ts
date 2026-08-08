// Small std-Web helpers so Route Handlers stay pure (Request) => Response and
// are directly unit-testable without a running Next server.
import 'server-only';

export function parseCookies(header: string | null): Record<string, string> {
  const out: Record<string, string> = {};
  if (!header) return out;
  for (const part of header.split(';')) {
    const eq = part.indexOf('=');
    if (eq <= 0) continue;
    const k = part.slice(0, eq).trim();
    const v = part.slice(eq + 1).trim();
    if (k) out[k] = decodeURIComponent(v);
  }
  return out;
}

export interface CookieAttrs {
  httpOnly: boolean;
  sameSite: 'strict' | 'lax' | 'none';
  secure: boolean;
  path: string;
  maxAge: number;
}

export function serializeCookie(name: string, value: string, attrs: CookieAttrs): string {
  const parts = [`${name}=${encodeURIComponent(value)}`];
  parts.push(`Path=${attrs.path}`);
  parts.push(`Max-Age=${attrs.maxAge}`);
  parts.push(`SameSite=${attrs.sameSite.charAt(0).toUpperCase()}${attrs.sameSite.slice(1)}`);
  if (attrs.httpOnly) parts.push('HttpOnly');
  if (attrs.secure) parts.push('Secure');
  return parts.join('; ');
}

export function jsonResponse(
  status: number,
  body: unknown,
  extraHeaders: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', ...extraHeaders },
  });
}
