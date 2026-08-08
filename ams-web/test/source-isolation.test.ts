// Static guarantee that the admin token / session secret live only in the
// server-only module graph and can never be pulled into a client bundle.
import { describe, expect, it } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

const SRC = new URL('../src', import.meta.url).pathname;

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else if (/\.(ts|tsx)$/.test(name)) out.push(p);
  }
  return out;
}

const files = walk(SRC);
const SERVER_DIR = join(SRC, 'lib', 'server');

describe('secret containment', () => {
  it('references AMX_ADMIN_TOKEN / AMX_SESSION_SECRET only under src/lib/server', () => {
    for (const f of files) {
      const body = readFileSync(f, 'utf8');
      if (/AMX_ADMIN_TOKEN|AMX_SESSION_SECRET|AMX_CONSOLE_PASSWORD/.test(body)) {
        expect(f.startsWith(SERVER_DIR), `${f} touches a server secret env var`).toBe(true);
      }
    }
  });

  it('every server-secret module is marked server-only', () => {
    for (const f of ['env.ts', 'session.ts', 'upstream.ts', 'auth.ts', 'http.ts']) {
      const body = readFileSync(join(SERVER_DIR, f), 'utf8');
      expect(body, `${f} missing server-only marker`).toMatch(/import ['"]server-only['"]/);
    }
  });

  it('no client component imports the server-only module graph', () => {
    for (const f of files) {
      const body = readFileSync(f, 'utf8');
      const isClient = /^\s*['"]use client['"]/m.test(body);
      if (!isClient) continue;
      expect(/from ['"](@\/lib\/server|\.\.?\/(?:\.\.\/)*lib\/server)/.test(body), `${f} is a client component importing lib/server`).toBe(false);
      expect(/import ['"]server-only['"]/.test(body), `${f} is a client component importing server-only`).toBe(false);
    }
  });
});
