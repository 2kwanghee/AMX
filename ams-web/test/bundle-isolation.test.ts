// Post-build guarantee: the browser-served bundle (.next/static) never contains
// the server secret env-var names. Meaningful after `next build`; skipped when
// no build output is present so `npm test` alone still passes.
import { describe, expect, it } from 'vitest';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

const STATIC = new URL('../.next/static', import.meta.url).pathname;

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else out.push(p);
  }
  return out;
}

describe('client bundle contains no server secrets', () => {
  it('no .next/static asset references AMX_ADMIN_TOKEN / AMX_SESSION_SECRET', () => {
    if (!existsSync(STATIC)) {
      // eslint-disable-next-line no-console
      console.warn('[bundle-isolation] .next/static not found — run `next build` for the strongest check.');
      return;
    }
    const needles = ['AMX_ADMIN_TOKEN', 'AMX_SESSION_SECRET', 'AMX_CONSOLE_PASSWORD'];
    for (const f of walk(STATIC)) {
      if (!/\.(js|mjs|css|map|json|txt|html)$/.test(f)) continue;
      const body = readFileSync(f, 'utf8');
      for (const n of needles) {
        expect(body.includes(n), `${f} leaks ${n}`).toBe(false);
      }
    }
  });
});
