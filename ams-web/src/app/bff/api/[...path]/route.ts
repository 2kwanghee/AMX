// Authenticated reverse proxy to ams-server. Every browser data request goes
// through here. Flow: verify session cookie -> validate path against the REST
// allowlist -> forward with the server-side admin Bearer -> return upstream
// status/body. No admin token, no session secret, no upstream auth header is
// ever reflected to the browser.
import { isAuthenticated } from '@/lib/server/auth';
import { proxyToUpstream } from '@/lib/server/upstream';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const PREFIX = '/bff/api/';

async function handle(req: Request): Promise<Response> {
  if (!(await isAuthenticated(req))) {
    return new Response(
      JSON.stringify({ type: 'about:blank', title: 'Unauthorized', status: 401, code: 'bff.unauthenticated' }),
      { status: 401, headers: { 'content-type': 'application/problem+json' } },
    );
  }

  const url = new URL(req.url);
  const idx = url.pathname.indexOf(PREFIX);
  if (idx < 0) {
    return new Response('{"title":"Bad Request","status":400}', {
      status: 400,
      headers: { 'content-type': 'application/problem+json' },
    });
  }
  const path = url.pathname.slice(idx + PREFIX.length);

  let body: string | undefined;
  if (req.method !== 'GET' && req.method !== 'DELETE') {
    body = await req.text();
  }

  const result = await proxyToUpstream(
    req.method,
    path,
    url.search,
    body,
    req.headers.get('content-type'),
  );

  return new Response(result.status === 204 ? null : result.body, {
    status: result.status,
    headers: { 'content-type': result.contentType },
  });
}

export const GET = handle;
export const POST = handle;
export const PATCH = handle;
export const DELETE = handle;
