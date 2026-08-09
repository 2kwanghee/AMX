// Client-safe reader for the NON-SECRET nav cookie (amx_nav). It exposes only
// role + tenant_ids so the browser can filter navigation. It never touches the
// encrypted session cookie or any secret — enforcement is entirely ams-server's
// job; this is a UI convenience. The signature is not verified here (a tampered
// hint at most changes what buttons show; the server rejects unauthorized
// calls regardless).
export type Role = 'global-admin' | 'tenant-admin';

export interface NavSession {
  role: Role;
  tenantIds: string[];
}

const NAV_COOKIE = 'amx_nav';

function readCookie(name: string): string | null {
  if (typeof document === 'undefined') return null;
  for (const part of document.cookie.split(';')) {
    const eq = part.indexOf('=');
    if (eq <= 0) continue;
    if (part.slice(0, eq).trim() === name) {
      return decodeURIComponent(part.slice(eq + 1).trim());
    }
  }
  return null;
}

function b64urlDecodeStr(s: string): string {
  const pad = s.length % 4 === 0 ? '' : '='.repeat(4 - (s.length % 4));
  return atob(s.replace(/-/g, '+').replace(/_/g, '/') + pad);
}

/** Parse the nav cookie, or null when absent/malformed. */
export function readNavSession(): NavSession | null {
  const raw = readCookie(NAV_COOKIE);
  if (!raw) return null;
  const dot = raw.indexOf('.');
  const dataPart = dot > 0 ? raw.slice(0, dot) : raw;
  try {
    const nav = JSON.parse(b64urlDecodeStr(dataPart)) as {
      role?: unknown;
      tenantIds?: unknown;
    };
    if (nav.role !== 'global-admin' && nav.role !== 'tenant-admin') return null;
    const tenantIds = Array.isArray(nav.tenantIds)
      ? nav.tenantIds.filter((t): t is string => typeof t === 'string')
      : [];
    return { role: nav.role, tenantIds };
  } catch {
    return null;
  }
}
