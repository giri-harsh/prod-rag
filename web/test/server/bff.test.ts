import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { createBff } from '../../server/app.mjs';
import { ConfigError, loadConfig } from '../../server/config.mjs';

const KEY = 'rag_TESTKEY_do_not_leak_0123456789';

interface Seen {
  method?: string;
  url?: string;
  headers: http.IncomingHttpHeaders;
  body: string;
}
type Handler = (req: http.IncomingMessage, res: http.ServerResponse, seen: Seen) => void;

let upstream: http.Server;
let upstreamUrl: string;
let seen: Seen[] = [];
let upstreamHandler: Handler;
let staticDir: string;
const stops: (() => Promise<void>)[] = [];
const logs: Record<string, unknown>[] = [];

beforeAll(async () => {
  upstream = http.createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on('data', (c: Buffer) => chunks.push(c));
    req.on('end', () => {
      const s: Seen = { method: req.method, url: req.url, headers: req.headers, body: Buffer.concat(chunks).toString('utf8') };
      seen.push(s);
      upstreamHandler(req, res, s);
    });
  });
  await new Promise<void>((r) => upstream.listen(0, '127.0.0.1', r));
  upstreamUrl = `http://127.0.0.1:${(upstream.address() as AddressInfo).port}`;

  staticDir = await mkdtemp(path.join(os.tmpdir(), 'bff-static-'));
  await mkdir(path.join(staticDir, 'assets'));
  await writeFile(path.join(staticDir, 'index.html'), '<!doctype html><title>t</title><div id="root"></div>');
  await writeFile(path.join(staticDir, 'assets', 'index-abcdef12.js'), 'console.log(1)');
  await writeFile(path.join(staticDir, '.secret'), 'nope');
  await writeFile(path.join(path.dirname(staticDir), 'outside.txt'), 'outside the static root');
});

afterAll(async () => {
  const closed = new Promise<void>((r) => upstream.close(() => r()));
  upstream.closeAllConnections();
  await closed;
  await rm(staticDir, { recursive: true, force: true });
  await rm(path.join(path.dirname(staticDir), 'outside.txt'), { force: true });
});

beforeEach(() => {
  seen = [];
  logs.length = 0;
  upstreamHandler = (_req, res) => {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ answer: 'ok', session_id: 'echo', sources: ['a.txt'], thought_process: ['p'], status: 'Response generated.', blocked: false, cache_hit: false, iterations: 1 }));
  };
});

afterEach(async () => {
  while (stops.length) await (stops.pop() as () => Promise<void>)();
});

async function start(env: Record<string, string> = {}, now?: () => number) {
  const config = loadConfig({ BFF_RAG_API_KEY: KEY, RAG_API_URL: upstreamUrl, BFF_PORT: '0', BFF_STATIC_DIR: staticDir, ...env });
  const bff = createBff(config, { logger: (e) => logs.push(e), ...(now ? { now } : {}) });
  const { port } = await bff.listen();
  stops.push(() => bff.close());
  return { base: `http://127.0.0.1:${port}`, port };
}

function post(base: string, body: unknown, headers: Record<string, string> = {}) {
  return fetch(`${base}/api/query`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...headers },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  });
}

function cookieOf(res: Response): string {
  const sc = res.headers.get('set-cookie');
  expect(sc).toBeTruthy();
  return (sc as string).split(';')[0] as string;
}

describe('POST /api/query', () => {
  it('adds the service key server-side, derives the session id and rewrites it in the answer', async () => {
    const { base } = await start();
    const res = await post(base, { q: 'hello', session_id: 'sess1' });
    expect(res.status).toBe(200);
    const cookie = cookieOf(res);
    const browserId = cookie.split('=')[1] as string;

    expect(seen).toHaveLength(1);
    const s = seen[0] as Seen;
    expect(s.url).toBe('/query');
    expect(s.headers['x-api-key']).toBe(KEY);
    expect(JSON.parse(s.body)).toEqual({ q: 'hello', session_id: `${browserId}-sess1` });
    expect(s.headers['x-request-id']).toMatch(/^web-/);

    const body = (await res.json()) as Record<string, unknown>;
    expect(body['session_id']).toBe('sess1'); // never the derived id: it embeds the HttpOnly browser id
    expect(body['answer']).toBe('ok');
    expect(JSON.stringify(body)).not.toContain(browserId);
    expect(JSON.stringify(body)).not.toContain(KEY);
  });

  it('never forwards client-supplied identity: headers and body keys are dropped', async () => {
    const { base } = await start();
    const res = await post(
      base,
      { q: 'hi', session_id: 's', thread_id: 'evil-thread', tenant_id: 'other-tenant', include_contexts: true, user_id: 'root' },
      { 'x-api-key': 'rag_attacker', authorization: 'Bearer attacker', 'x-tenant-id': 'other', 'x-forwarded-for': '6.6.6.6', 'x-request-id': 'spoofed' },
    );
    expect(res.status).toBe(200);
    const s = seen[0] as Seen;
    expect(s.headers['x-api-key']).toBe(KEY);
    expect(s.headers['authorization']).toBeUndefined();
    expect(s.headers['x-tenant-id']).toBeUndefined();
    expect(s.headers['x-forwarded-for']).toBeUndefined();
    expect(s.headers['x-request-id']).not.toBe('spoofed');
    expect(s.headers['cookie']).toBeUndefined();
    expect(Object.keys(JSON.parse(s.body) as object).sort()).toEqual(['q', 'session_id']);
    expect(s.body).not.toContain('evil-thread');
    expect(s.body).not.toContain('other-tenant');
    expect(logs.find((l) => l['msg'] === 'request')?.['stripped']).toEqual(['thread_id', 'tenant_id', 'include_contexts', 'user_id']);
  });

  it('keeps conversations of two browsers apart and a browser stable across requests', async () => {
    const { base } = await start();
    const r1 = await post(base, { q: 'a', session_id: 'same' });
    const c1 = cookieOf(r1);
    await r1.arrayBuffer();
    const r2 = await post(base, { q: 'a', session_id: 'same' });
    const c2 = cookieOf(r2);
    await r2.arrayBuffer();
    const r3 = await post(base, { q: 'b', session_id: 'same' }, { cookie: c1 });
    expect(r3.headers.get('set-cookie')).toBeNull(); // existing valid cookie: not re-issued
    await r3.arrayBuffer();
    const ids = seen.map((s) => (JSON.parse(s.body) as { session_id: string }).session_id);
    expect(c1).not.toBe(c2);
    expect(ids[0]).not.toBe(ids[1]);
    expect(ids[2]).toBe(ids[0]);
  });

  it('replaces a malformed cookie with a fresh one', async () => {
    const { base } = await start();
    const res = await post(base, { q: 'a', session_id: 's' }, { cookie: 'rag_bid=short' });
    expect(cookieOf(res)).toMatch(/^rag_bid=[A-Za-z0-9_-]{22}$/);
    await res.arrayBuffer();
  });

  it('issues an HttpOnly SameSite=Strict cookie (Secure + __Host- when configured)', async () => {
    const plain = await start();
    const r1 = await post(plain.base, { q: 'a', session_id: 's' });
    const sc1 = r1.headers.get('set-cookie') as string;
    expect(sc1).toMatch(/^rag_bid=/);
    expect(sc1).toContain('HttpOnly');
    expect(sc1).toContain('SameSite=Strict');
    expect(sc1).not.toContain('Secure');
    await r1.arrayBuffer();
    const secure = await start({ BFF_COOKIE_SECURE: '1' });
    const r2 = await post(secure.base, { q: 'a', session_id: 's' });
    const sc2 = r2.headers.get('set-cookie') as string;
    expect(sc2).toMatch(/^__Host-rag_bid=/);
    expect(sc2).toContain('Secure');
    expect(r2.headers.get('strict-transport-security')).toBeTruthy();
    await r2.arrayBuffer();
  });

  it.each([
    [401, { detail: 'invalid API key' }, undefined, 401, 'upstream_unauthorized'],
    [409, { error: 'busy', request_id: 'r' }, '1', 409, 'session_busy'],
    [413, { error: 'payload too large' }, undefined, 413, 'payload_too_large'],
    [429, { error: 'rate limit exceeded (minute)' }, '17', 429, 'rate_limited'],
    [503, { error: 'unavailable' }, '9', 503, 'unavailable'],
    [500, { error: 'internal server error' }, undefined, 502, 'upstream_error'],
  ])('propagates upstream %i faithfully (status %i, code %s) with Retry-After', async (status, upstreamBody, retryAfter, expectStatus, code) => {
    upstreamHandler = (_req, res) => {
      res.writeHead(status as number, { 'content-type': 'application/json', ...(retryAfter ? { 'retry-after': retryAfter as string } : {}) });
      res.end(JSON.stringify(upstreamBody));
    };
    const { base } = await start();
    const res = await post(base, { q: 'x', session_id: 's' });
    expect(res.status).toBe(expectStatus);
    const body = (await res.json()) as { code: string; error: string; retry_after?: number; request_id: string };
    expect(body.code).toBe(code);
    expect(body.request_id).toBeTruthy();
    if (retryAfter) {
      expect(res.headers.get('retry-after')).toBe(retryAfter);
      expect(body.retry_after).toBe(Number(retryAfter));
    }
    expect(JSON.stringify(body)).not.toContain(KEY);
  });

  it('answers 503 + Retry-After when the API is unreachable, and 504 on timeout', async () => {
    const dead = http.createServer();
    await new Promise<void>((r) => dead.listen(0, '127.0.0.1', r));
    const deadUrl = `http://127.0.0.1:${(dead.address() as AddressInfo).port}`;
    await new Promise<void>((r) => dead.close(() => r()));
    const down = await start({ RAG_API_URL: deadUrl });
    const r1 = await post(down.base, { q: 'x', session_id: 's' });
    expect(r1.status).toBe(503);
    expect(r1.headers.get('retry-after')).toBe('5');
    expect(((await r1.json()) as { code: string }).code).toBe('upstream_unreachable');

    upstreamHandler = () => {
      /* never answers */
    };
    const slow = await start({ BFF_UPSTREAM_TIMEOUT_MS: '1000' });
    const r2 = await post(slow.base, { q: 'x', session_id: 's' });
    expect(r2.status).toBe(504);
    expect(((await r2.json()) as { code: string }).code).toBe('upstream_timeout');
  }, 15_000);

  it('turns a malformed upstream success body into 502', async () => {
    upstreamHandler = (_req, res) => {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end('<html>not json</html>');
    };
    const { base } = await start();
    const res = await post(base, { q: 'x', session_id: 's' });
    expect(res.status).toBe(502);
    expect(((await res.json()) as { code: string }).code).toBe('bad_gateway');
  });

  it('does not follow upstream redirects (the key must not follow them)', async () => {
    const evilSeen: string[] = [];
    const evil = http.createServer((req, res) => {
      evilSeen.push(String(req.headers['x-api-key']));
      res.end('{}');
    });
    await new Promise<void>((r) => evil.listen(0, '127.0.0.1', r));
    upstreamHandler = (_req, res) => {
      res.writeHead(307, { location: `http://127.0.0.1:${(evil.address() as AddressInfo).port}/query` });
      res.end();
    };
    const { base } = await start();
    const res = await post(base, { q: 'x', session_id: 's' });
    await new Promise<void>((r) => evil.close(() => r()));
    expect(res.status).toBeGreaterThanOrEqual(500);
    expect(evilSeen).toEqual([]);
  });

  it('rejects bad input before contacting the API', async () => {
    const { base } = await start({ BFF_MAX_QUERY_CHARS: '20' });
    expect((await post(base, '{not json')).status).toBe(400);
    expect((await post(base, { q: '', session_id: 's' })).status).toBe(422);
    expect((await post(base, { q: 'x', session_id: 'bad id!' })).status).toBe(422);
    expect((await post(base, { q: 'x'.repeat(21), session_id: 's' })).status).toBe(413);
    const wrongType = await fetch(`${base}/api/query`, { method: 'POST', headers: { 'content-type': 'text/plain' }, body: '{}' });
    expect(wrongType.status).toBe(415);
    expect(seen).toHaveLength(0);
  });

  it('enforces the body size limit (declared and streamed) with a JSON 413', async () => {
    const { base } = await start({ BFF_MAX_BODY_BYTES: '1024' });
    const big = await post(base, { q: 'x', session_id: 's', pad: 'y'.repeat(5000) });
    expect(big.status).toBe(413);
    expect(((await big.json()) as { code: string }).code).toBe('payload_too_large');
    expect(seen).toHaveLength(0);
  });

  it('refuses cross-origin POSTs but accepts same-origin ones', async () => {
    const { base, port } = await start();
    const evil = await post(base, { q: 'x', session_id: 's' }, { origin: 'https://evil.example' });
    expect(evil.status).toBe(403);
    expect(((await evil.json()) as { code: string }).code).toBe('forbidden_origin');
    const same = await post(base, { q: 'x', session_id: 's' }, { origin: `http://127.0.0.1:${port}` });
    expect(same.status).toBe(200);
    await same.arrayBuffer();
    const allowed = await start({ BFF_ALLOWED_ORIGINS: 'https://chat.example.com' });
    const ok = await post(allowed.base, { q: 'x', session_id: 's' }, { origin: 'https://chat.example.com' });
    expect(ok.status).toBe(200);
    await ok.arrayBuffer();
  });

  it('applies a per-IP rate limit with Retry-After, independent of the API limits', async () => {
    let t = 1_000_000;
    const { base } = await start({ BFF_RATE_LIMIT_PER_MIN: '2' }, () => t);
    const codes: number[] = [];
    for (let i = 0; i < 3; i++) {
      const r = await post(base, { q: 'x', session_id: 's' });
      codes.push(r.status);
      if (r.status === 429) {
        expect(Number(r.headers.get('retry-after'))).toBeGreaterThanOrEqual(1);
        expect(((await r.json()) as { code: string }).code).toBe('bff_rate_limited');
      } else await r.arrayBuffer();
    }
    expect(codes).toEqual([200, 200, 429]);
    expect(seen).toHaveLength(2);
    t += 31_000; // one token = 60s / 2
    const again = await post(base, { q: 'x', session_id: 's' });
    expect(again.status).toBe(200);
    await again.arrayBuffer();
  });

  it('only honours X-Forwarded-For from trusted proxies when rate limiting', async () => {
    const { base } = await start({ BFF_RATE_LIMIT_PER_MIN: '1' });
    const a = await post(base, { q: 'x', session_id: 's' }, { 'x-forwarded-for': '1.1.1.1' });
    await a.arrayBuffer();
    const b = await post(base, { q: 'x', session_id: 's' }, { 'x-forwarded-for': '2.2.2.2' });
    expect(b.status).toBe(429); // untrusted peer: header ignored, same bucket
    const trusted = await start({ BFF_RATE_LIMIT_PER_MIN: '1', BFF_TRUSTED_PROXIES: '127.0.0.1' });
    const c = await post(trusted.base, { q: 'x', session_id: 's' }, { 'x-forwarded-for': '1.1.1.1' });
    await c.arrayBuffer();
    const d = await post(trusted.base, { q: 'x', session_id: 's' }, { 'x-forwarded-for': '2.2.2.2' });
    expect(d.status).toBe(200);
    await d.arrayBuffer();
  });

  it('answers 405/404 as JSON for other API routes', async () => {
    const { base } = await start();
    const get = await fetch(`${base}/api/query`);
    expect(get.status).toBe(405);
    expect(get.headers.get('allow')).toBe('POST');
    expect((await fetch(`${base}/api/nope`)).status).toBe(404);
    expect((await fetch(`${base}/api/health`, { method: 'POST' })).status).toBe(405);
  });
});

describe('logging', () => {
  it('never logs the key, cookies, headers, question text or the derived session id', async () => {
    const { base } = await start();
    const secretQuestion = 'my-super-secret-question-text';
    const res = await post(base, { q: secretQuestion, session_id: 'logsess' }, { 'x-api-key': 'rag_attacker_key', authorization: 'Bearer zzz' });
    const browserId = cookieOf(res).split('=')[1] as string;
    await res.arrayBuffer();
    upstreamHandler = (_req, r) => {
      r.writeHead(401, { 'content-type': 'application/json' });
      r.end('{"detail":"invalid API key"}');
    };
    const bad = await post(base, { q: secretQuestion, session_id: 'logsess' }, { cookie: `rag_bid=${browserId}` });
    await bad.arrayBuffer();
    const dump = JSON.stringify(logs);
    for (const needle of [KEY, 'rag_attacker_key', 'Bearer zzz', secretQuestion, browserId, 'logsess']) expect(dump).not.toContain(needle);
    const failed = logs.filter((l) => l['msg'] === 'request' && l['status'] === 401);
    expect(failed).toHaveLength(1);
    expect(failed[0]?.['note']).toMatch(/BFF_RAG_API_KEY/);
    expect(failed[0]?.['upstream_status']).toBe(401);
  });
});

describe('GET /api/health and /healthz', () => {
  it('reports upstream readiness without sending the service key, with a short cache', async () => {
    upstreamHandler = (req, res) => {
      res.writeHead(req.url === '/readyz' ? 200 : 404, { 'content-type': 'application/json' });
      res.end('{"ready":true}');
    };
    let t = 5_000_000;
    const { base } = await start({}, () => t);
    const h = await fetch(`${base}/api/health`);
    expect(h.status).toBe(200);
    expect(await h.json()).toEqual({ ok: true, bff: 'ok', upstream: 'ready' });
    expect((seen[0] as Seen).url).toBe('/readyz');
    expect((seen[0] as Seen).headers['x-api-key']).toBeUndefined();
    await (await fetch(`${base}/api/health`)).arrayBuffer();
    expect(seen).toHaveLength(1); // cached
    t += 4000;
    upstreamHandler = (_req, res) => {
      res.writeHead(503, { 'content-type': 'application/json' });
      res.end('{"ready":false}');
    };
    const down = await fetch(`${base}/api/health`);
    expect(down.status).toBe(503);
    expect(down.headers.get('retry-after')).toBe('5');
    expect(await down.json()).toEqual({ ok: false, bff: 'ok', upstream: 'not_ready' });
  });

  it('has a dependency-free /healthz', async () => {
    const { base } = await start({ RAG_API_URL: 'http://127.0.0.1:1' });
    const r = await fetch(`${base}/healthz`);
    expect(r.status).toBe(200);
    expect(await r.json()).toEqual({ status: 'ok' });
    expect(seen).toHaveLength(0);
  });
});

describe('static files and security headers', () => {
  it('serves index.html with security headers and issues the browser cookie', async () => {
    const { base } = await start();
    const res = await fetch(`${base}/`);
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toContain('text/html');
    expect(res.headers.get('cache-control')).toBe('no-cache');
    expect(res.headers.get('set-cookie')).toMatch(/HttpOnly/);
    const csp = res.headers.get('content-security-policy') as string;
    expect(csp).toContain("default-src 'self'");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).not.toContain('unsafe-inline');
    expect(csp).not.toContain('unsafe-eval');
    expect(res.headers.get('x-content-type-options')).toBe('nosniff');
    expect(res.headers.get('referrer-policy')).toBe('no-referrer');
    expect(res.headers.get('x-frame-options')).toBe('DENY');
    await res.arrayBuffer();
  });

  it('puts the security headers on API responses too, and marks them uncacheable', async () => {
    const { base } = await start();
    const res = await post(base, { q: 'x', session_id: 's' });
    expect(res.headers.get('content-security-policy')).toContain("default-src 'self'");
    expect(res.headers.get('cache-control')).toBe('no-store');
    expect(res.headers.get('x-content-type-options')).toBe('nosniff');
    await res.arrayBuffer();
  });

  it('caches hashed assets immutably and does not SPA-fall-back for missing assets', async () => {
    const { base } = await start();
    const asset = await fetch(`${base}/assets/index-abcdef12.js`);
    expect(asset.headers.get('cache-control')).toContain('immutable');
    expect(asset.headers.get('content-type')).toContain('javascript');
    await asset.arrayBuffer();
    expect((await fetch(`${base}/assets/missing-abcdef12.js`)).status).toBe(404);
    const spa = await fetch(`${base}/some/client/route`);
    expect(spa.status).toBe(200);
    expect(await spa.text()).toContain('id="root"');
  });

  it('blocks path traversal, dotfiles and non-GET methods', async () => {
    const { base, port } = await start();
    for (const p of ['/../outside.txt', '/%2e%2e/outside.txt', '/assets/%2e%2e/%2e%2e/outside.txt', '/.secret', '/%2e%2esecret', '/..%5coutside.txt', '/%00']) {
      // raw socket request so the client does not normalise the path before sending
      const status = await rawStatus(port, `GET ${p} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n`);
      expect([400, 404, 200]).toContain(status);
      if (status === 200) {
        // only acceptable when it was the SPA fallback, never the outside file
        const text = await rawBody(port, `GET ${p} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n`);
        expect(text).not.toContain('outside the static root');
        expect(text).not.toContain('nope');
      }
    }
    const put = await fetch(`${base}/`, { method: 'PUT', body: 'x' });
    expect(put.status).toBe(405);
  });
});

describe('loadConfig', () => {
  it('requires the key and never echoes it in errors', () => {
    expect(() => loadConfig({})).toThrow(ConfigError);
    expect(() => loadConfig({ BFF_RAG_API_KEY: 'bad key with spaces' })).toThrow(/header/);
    try {
      loadConfig({ BFF_RAG_API_KEY: 'bad key with spaces' });
    } catch (e) {
      expect((e as Error).message).not.toContain('bad key with spaces');
    }
  });

  it('validates RAG_API_URL and rejects embedded credentials', () => {
    const base = { BFF_RAG_API_KEY: 'k' };
    expect(() => loadConfig({ ...base, RAG_API_URL: 'ftp://x' })).toThrow(/http/);
    expect(() => loadConfig({ ...base, RAG_API_URL: 'http://user:pw@x' })).toThrow(/credentials/);
    expect(() => loadConfig({ ...base, RAG_API_URL: 'nonsense' })).toThrow(/valid URL/);
    expect(loadConfig({ ...base, RAG_API_URL: 'https://api.example.com/rag/' }).upstreamBase).toBe('https://api.example.com/rag');
  });

  it('applies defaults and range checks', () => {
    const c = loadConfig({ BFF_RAG_API_KEY: 'k' });
    expect(c).toMatchObject({ host: '127.0.0.1', port: 8787, ratePerMin: 30, maxQueryChars: 4000, cookieSecure: false });
    expect(loadConfig({ BFF_RAG_API_KEY: 'k', PORT: '9000' }).port).toBe(9000);
    expect(loadConfig({ BFF_RAG_API_KEY: 'k', PORT: '9000', BFF_PORT: '9001' }).port).toBe(9001);
    expect(() => loadConfig({ BFF_RAG_API_KEY: 'k', BFF_PORT: 'abc' })).toThrow(/BFF_PORT/);
    expect(() => loadConfig({ BFF_RAG_API_KEY: 'k', BFF_ALLOWED_ORIGINS: 'chat.example.com' })).toThrow(/BFF_ALLOWED_ORIGINS/);
    expect(loadConfig({ BFF_RAG_API_KEY: 'k', BFF_ALLOWED_ORIGINS: 'https://a.example/, https://b.example' }).allowedOrigins).toEqual(new Set(['https://a.example', 'https://b.example']));
  });
});

// ---- raw HTTP helpers (no client-side path normalisation) ---------------------------------------------------------
async function rawRequest(port: number, request: string): Promise<string> {
  const net = await import('node:net');
  return new Promise((resolve, reject) => {
    const sock = net.connect(port, '127.0.0.1', () => sock.write(request));
    let data = '';
    sock.on('data', (c: Buffer) => (data += c.toString('utf8')));
    sock.on('end', () => resolve(data));
    sock.on('error', reject);
    sock.setTimeout(5000, () => {
      sock.destroy();
      reject(new Error('raw request timed out'));
    });
  });
}
async function rawStatus(port: number, request: string): Promise<number> {
  return Number((await rawRequest(port, request)).split(' ')[1]);
}
async function rawBody(port: number, request: string): Promise<string> {
  return (await rawRequest(port, request)).split('\r\n\r\n').slice(1).join('\r\n\r\n');
}
