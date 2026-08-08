// Module resolve hook so plain Node can import the real ams-web BFF TypeScript
// modules (Node 22 strips the types natively). Three rewrites:
//   * `server-only`  -> an empty module (the marker only guards the RSC build;
//     here we import the server modules directly, exactly as vitest does).
//   * `@/…`          -> <ams-web>/src/… (the tsconfig path alias).
//   * `./x`, `../x`  -> `./x.ts` (extensionless relative imports the TS uses).
// AMS_WEB_SRC (…/ams-web/src) is supplied by the caller's environment.
import { pathToFileURL } from 'node:url';

const SRC = process.env.AMS_WEB_SRC;
const HAS_EXT = /\.[mc]?[jt]s$/;

export async function resolve(spec, ctx, next) {
  if (spec === 'server-only') {
    return { url: 'data:text/javascript,export{}', shortCircuit: true };
  }
  if (spec.startsWith('@/')) {
    if (!SRC) throw new Error('AMS_WEB_SRC env is required to resolve @/ imports');
    const base = pathToFileURL(`${SRC}/${spec.slice(2)}`).href;
    const url = HAS_EXT.test(base) ? base : `${base}.ts`;
    return next(url, ctx);
  }
  if ((spec.startsWith('./') || spec.startsWith('../')) && !HAS_EXT.test(spec)) {
    try {
      return await next(`${spec}.ts`, ctx);
    } catch {
      /* fall through to the default resolver */
    }
  }
  return next(spec, ctx);
}
