# ams-web — AMX management console

Next.js (App Router) console for AMX. It is a **BFF**: the browser only ever
talks to same-origin `/bff/*` route handlers, which attach the administrator
Bearer token server-side and proxy to `ams-server`. The admin token and session
secret live in the Next server process env only and are structurally prevented
from reaching the browser (design `docs/design-notes/p4-architecture.md`,
decisions 1/5/7; `docs/AMX-DESIGN.md` §5.6, §7).

## Run

```bash
cp .env.example .env.local   # fill AMX_API_BASE, AMX_ADMIN_TOKEN, AMX_SESSION_SECRET
npm install
npm run build && npm start   # http://localhost:3000  -> /login
```

> `npm run dev` does not work: the production CSP forbids the `unsafe-eval` that
> Next's dev server needs, so the console renders blank. Always run the
> production build (`build` + `start`), as `deploy/fullstack-run.sh` does — see
> OVERVIEW §5.

## Verify (single command gate)

```bash
npm install && npm run build && npm test
```

`npm test` runs Vitest:

- **`bff-lifecycle.test.ts`** — R3 completion gate. Drives the BFF handlers
  through the full account lifecycle (tenant → OAuth enroll → server → assign →
  deliver → deactivate/activate/switch-now → recall → alert list + ack) against
  a fake ams-server, asserting each upstream call carried `Bearer <adminToken>`
  and that the token never appears in any response body/header/cookie.
- **`proxy-guard.test.ts`** — the proxy allowlist accepts the documented REST
  surface and rejects traversal / SSRF / unknown paths before any upstream call.
- **`source-isolation.test.ts`** — the secret env vars are referenced only under
  `src/lib/server/`, those modules are `server-only`, and no `"use client"`
  component imports that graph.
- **`bundle-isolation.test.ts`** — after `next build`, no `.next/static` asset
  references the secret env-var names. (Skips with a warning if run without a
  prior build.)

## Token isolation — how it is guaranteed

1. `src/lib/server/env.ts` is `import 'server-only'`; reading `AMX_ADMIN_TOKEN`
   from a client component fails the build.
2. `src/lib/server/upstream.ts` is the single place the Bearer is attached, and
   it never forwards client `Authorization`/`Cookie` upstream nor reflects the
   upstream auth header back.
3. The browser holds only an httpOnly, `SameSite=Strict` signed session cookie
   (`src/lib/server/session.ts`) that carries `{sub, exp}` — never the token.
4. Middleware redirects unauthenticated page loads to `/login`; `/bff/*`
   handlers self-enforce auth and 401 otherwise.

## Contracts

REST DTOs in `src/lib/api-client/types.ts` mirror `contracts/openapi.yaml` (the
SSOT), and the usage payload additionally follows
`contracts/schemas/usage-report.schema.json`. The generated `@amx/contracts`
package is the ts-proto output of the **gRPC** control plane (AMS↔AMA), not this
REST surface, so it is not imported here: its shapes are proto codecs unsuited
to the REST DTOs, and it declares no `@bufbuild/protobuf` runtime dependency, so
importing it — even type-only — fails the typecheck (and `contracts/` is not
editable from this track). The alerts endpoints (`GET /alerts`,
`POST /alerts/{id}:ack`) follow design §5.6.
