// Drives the REAL ams-web BFF against a live ams-server, so the P4 console E2E
// exercises the same code the browser reaches: the session Route Handler (login
// -> httpOnly cookie) and the `[...path]` proxy Route Handler (allowlist +
// server-side admin Bearer + header hygiene). Nothing here re-implements BFF
// logic; it imports the handlers and calls them as `(Request) => Response`.
//
// Protocol: read one JSON job from stdin, run it, print one JSON result to
// stdout. Job: { steps: [ { method, path, body?, noauth? }, ... ] }. `path` is
// the segment after /bff/api/ and may carry a query string. The result reports,
// per step, the upstream status, parsed JSON, and whether the admin-token
// sentinel leaked into any browser-visible surface (body, header, or set-cookie).
import { register } from 'node:module';
import { pathToFileURL } from 'node:url';

register('./bff_loader.mjs', pathToFileURL(`${process.cwd()}/`));

const SRC = process.env.AMS_WEB_SRC;
const SENTINEL = process.env.AMX_ADMIN_TOKEN || '';

const session = await import(`${SRC}/app/bff/session/route.ts`);
const proxy = await import(`${SRC}/app/bff/api/[...path]/route.ts`);
const PROXY = { GET: proxy.GET, POST: proxy.POST, PATCH: proxy.PATCH, DELETE: proxy.DELETE };

const ORIGIN = 'http://console.local';

function leaks(...parts) {
  if (!SENTINEL) return false;
  return parts.some((p) => typeof p === 'string' && p.includes(SENTINEL));
}

function headerValues(res) {
  const out = [];
  for (const [, v] of res.headers) out.push(v);
  return out;
}

async function readStdin() {
  const chunks = [];
  for await (const c of process.stdin) chunks.push(c);
  return Buffer.concat(chunks).toString('utf8');
}

async function login() {
  const res = await session.POST(
    new Request(`${ORIGIN}/bff/session`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ password: process.env.AMX_CONSOLE_PASSWORD }),
    }),
  );
  const setCookie = res.headers.get('set-cookie');
  const body = await res.text();
  const cookie = setCookie ? setCookie.split(';')[0] : null;
  return {
    status: res.status,
    cookie,
    setCookiePresent: Boolean(setCookie),
    leaked: leaks(body, setCookie ?? '', ...headerValues(res)),
  };
}

async function step(cookie, { method, path, body, noauth }) {
  const headers = {};
  if (cookie && !noauth) headers.cookie = cookie;
  if (body !== undefined) headers['content-type'] = 'application/json';
  const req = new Request(`${ORIGIN}/bff/api/${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const handler = PROXY[method];
  if (!handler) throw new Error(`unsupported method ${method}`);
  const res = await handler(req);
  const text = await res.text();
  let json;
  try {
    json = text ? JSON.parse(text) : undefined;
  } catch {
    json = undefined;
  }
  return {
    method,
    path,
    status: res.status,
    json,
    bodyText: json === undefined ? text : undefined,
    leaked: leaks(text, res.headers.get('set-cookie') ?? '', ...headerValues(res)),
  };
}

async function main() {
  const job = JSON.parse(await readStdin());
  const auth = await login();
  const results = [];
  for (const s of job.steps ?? []) {
    results.push(await step(auth.cookie, s));
  }
  process.stdout.write(JSON.stringify({ login: auth, results }));
}

main().catch((e) => {
  process.stderr.write(String(e && e.stack ? e.stack : e));
  process.exit(1);
});
