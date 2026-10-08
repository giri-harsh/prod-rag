// @ts-check
// The BFF ("backend for frontend"): serves the built SPA and is the ONLY thing that talks to the RAG API.
// The browser never sees the API key; the key is attached here from BFF_RAG_API_KEY.
import http from 'node:http';
import {
  BROWSER_ID_RE,
  HSTS_HEADER,
  SECURITY_HEADERS,
  buildUpstreamHeaders,
  clientIp,
  cookieName,
  createRateLimiter,
  deriveSessionId,
  isAllowedOrigin,
  mapFetchError,
  mapUpstreamFailure,
  newBrowserId,
  newRequestId,
  normalizeQueryResponse,
  parseCookies,
  sanitizeQueryBody,
  serializeBrowserCookie,
} from './lib.mjs';
import { createStaticHandler } from './static.mjs';

/**
 * @typedef {import('./config.mjs').BffConfig} BffConfig
 * @typedef {(entry: Record<string, unknown>) => void} Logger
 * @typedef {{ fetch?: typeof fetch, logger?: Logger, now?: () => number }} Deps
 * @typedef {import('node:http').IncomingMessage} Req
 * @typedef {import('node:http').ServerResponse} Res
 * @typedef {import('./lib.mjs').Failure} Failure
 */

const MAX_UPSTREAM_BYTES = 1024 * 1024;
const HEALTH_CACHE_MS = 3000;
const HEALTH_TIMEOUT_MS = 3000;

/** One JSON line per event on stdout. Callers only ever pass non-secret fields (no headers, no bodies, no query text). */
/** @type {Logger} */
export const defaultLogger = (entry) => {
  process.stdout.write(`${JSON.stringify({ ts: new Date().toISOString(), ...entry })}\n`);
};

class HttpError extends Error {
  /** @param {number} status @param {string} code @param {string} message */
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

/**
 * Read the request body, refusing anything over `limit` bytes (declared or streamed).
 * @param {Req} req
 * @param {number} limit
 */
async function readBody(req, limit) {
  const declared = req.headers['content-length'];
  if (declared !== undefined) {
    const n = Number(declared);
    if (!Number.isInteger(n) || n < 0) throw new HttpError(400, 'invalid_request', 'Invalid Content-Length.');
    if (n > limit) throw new HttpError(413, 'payload_too_large', 'The request body is too large.');
  }
  /** @type {Buffer[]} */
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    const buf = /** @type {Buffer} */ (chunk);
    size += buf.length;
    if (size > limit) throw new HttpError(413, 'payload_too_large', 'The request body is too large.');
    chunks.push(buf);
  }
  return Buffer.concat(chunks).toString('utf8');
}

/**
 * Read an upstream response as text, giving up past `limit` bytes.
 * @param {Response} response
 * @param {number} limit
 */
async function readLimitedText(response, limit) {
  const reader = response.body?.getReader();
  if (!reader) return '';
  const decoder = new TextDecoder();
  let size = 0;
  let out = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > limit) {
      await reader.cancel();
      throw new Error('upstream response too large');
    }
    out += decoder.decode(value, { stream: true });
  }
  return out + decoder.decode();
}

/**
 * @param {BffConfig} config
 * @param {Deps} [deps]
 */
export function createBff(config, deps = {}) {
  const doFetch = deps.fetch ?? globalThis.fetch;
  const log = deps.logger ?? defaultLogger;
  const now = deps.now ?? Date.now;
  const queryLimiter = createRateLimiter({ limit: config.ratePerMin, windowMs: 60_000, now });
  const healthLimiter = createRateLimiter({ limit: config.healthRatePerMin, windowMs: 60_000, now });
  const serveStatic = createStaticHandler(config.staticDir);
  const bidCookie = cookieName({ secure: config.cookieSecure });
  /** @type {{ at: number, status: number, body: Record<string, unknown> } | null} */
  let healthCache = null;

  // ------------------------------------------------------------ small helpers
  /**
   * @param {Res} res
   * @param {number} status
   * @param {unknown} body
   * @param {Record<string, string>} [extra]
   */
  function sendJson(res, status, body, extra = {}) {
    if (res.headersSent || res.destroyed) return;
    const payload = JSON.stringify(body);
    res.writeHead(status, {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'no-store',
      'content-length': Buffer.byteLength(payload),
      ...extra,
    });
    res.end(res.req.method === 'HEAD' ? undefined : payload);
  }

  /** @param {Res} res @param {Failure} f @param {string} rid */
  function sendFailure(res, f, rid) {
    const extra = { 'x-request-id': rid, ...(f.retryAfter ? { 'retry-after': String(f.retryAfter) } : {}) };
    sendJson(res, f.status, { ...f.body, request_id: f.body.request_id ?? rid }, extra);
  }

  /** The opaque browser id lives in an HttpOnly cookie. Issued when absent (or refreshed on page loads). */
  function ensureBrowserId(/** @type {Req} */ req, /** @type {Res} */ res, refresh = false) {
    const existing = parseCookies(req.headers.cookie)[bidCookie];
    const valid = existing !== undefined && BROWSER_ID_RE.test(existing);
    if (valid && !refresh) return existing;
    const id = valid ? existing : newBrowserId();
    res.setHeader('set-cookie', serializeBrowserCookie(bidCookie, id, { secure: config.cookieSecure }));
    return id;
  }

  // ------------------------------------------------------------ /api/query
  /** @param {Req} req @param {Res} res @param {string} rid @param {string} ip @param {Record<string, unknown>} info */
  async function handleQuery(req, res, rid, ip, info) {
    const h = req.headers;
    if (!isAllowedOrigin({ origin: h.origin, host: h.host, secFetchSite: /** @type {string | undefined} */ (h['sec-fetch-site']) }, config.allowedOrigins)) {
      return sendFailure(res, { status: 403, body: { error: 'Cross-origin requests are not allowed.', code: 'forbidden_origin' } }, rid);
    }
    if (!(h['content-type'] ?? '').toLowerCase().startsWith('application/json')) {
      return sendFailure(res, { status: 415, body: { error: 'Content-Type must be application/json.', code: 'unsupported_media_type' } }, rid);
    }
    const rl = queryLimiter.check(ip);
    if (!rl.allowed) {
      info['rate_limited_ip'] = ip;
      return sendFailure(
        res,
        { status: 429, body: { error: 'Too many requests from your network. Please wait a moment.', code: 'bff_rate_limited', retry_after: rl.retryAfterSec }, retryAfter: rl.retryAfterSec },
        rid,
      );
    }
    const browserId = ensureBrowserId(req, res);

    /** @type {unknown} */
    let parsed;
    try {
      parsed = JSON.parse(await readBody(req, config.maxBodyBytes));
    } catch (e) {
      if (e instanceof HttpError) {
        res.setHeader('connection', 'close');
        return sendFailure(res, { status: e.status, body: { error: e.message, code: e.code } }, rid);
      }
      return sendFailure(res, { status: 400, body: { error: 'The request body is not valid JSON.', code: 'invalid_json' } }, rid);
    }
    const checked = sanitizeQueryBody(parsed, { maxQueryChars: config.maxQueryChars });
    if (!checked.ok) return sendFailure(res, { status: checked.status, body: { error: checked.message, code: checked.code } }, rid);
    if (checked.stripped.length > 0) info['stripped'] = checked.stripped;
    const sessionId = deriveSessionId(browserId, checked.value.clientSessionId);
    if (sessionId === null) {
      return sendFailure(res, { status: 422, body: { error: 'Invalid session id.', code: 'invalid_session' } }, rid);
    }

    const ac = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      ac.abort();
    }, config.upstreamTimeoutMs);
    const onClose = () => {
      if (!res.writableEnded) ac.abort(); // the browser went away: stop waiting for the API
    };
    res.on('close', onClose);

    /** @type {Failure | null} */
    let failure = null;
    let answer = null;
    try {
      const upstream = await doFetch(`${config.upstreamBase}/query`, {
        method: 'POST',
        headers: buildUpstreamHeaders({ apiKey: config.apiKey, requestId: rid }),
        body: JSON.stringify({ q: checked.value.q, session_id: sessionId }),
        signal: ac.signal,
        redirect: 'error', // never replay the API key to a redirect target
      });
      info['upstream_status'] = upstream.status;
      const raw = await readLimitedText(upstream, MAX_UPSTREAM_BYTES);
      if (upstream.ok) {
        try {
          answer = normalizeQueryResponse(JSON.parse(raw), checked.value.clientSessionId, rid);
        } catch {
          answer = null;
        }
        if (answer === null) {
          failure = { status: 502, body: { error: 'The API returned a malformed answer.', code: 'bad_gateway' } };
        }
      } else {
        failure = mapUpstreamFailure(upstream.status, raw, upstream.headers.get('retry-after'), now());
        if (upstream.status === 401) info['note'] = 'upstream rejected the service key: check BFF_RAG_API_KEY';
      }
    } catch (err) {
      info['err'] = err instanceof Error ? err.name : 'error'; // name only: messages from fetch can embed URLs
      failure = mapFetchError(err, timedOut);
    } finally {
      clearTimeout(timer);
      res.off('close', onClose);
    }
    if (failure) return sendFailure(res, failure, rid);
    return sendJson(res, 200, answer, { 'x-request-id': rid });
  }

  // ------------------------------------------------------------ /api/health
  /** @param {Res} res @param {string} rid @param {string} ip */
  async function handleHealth(res, rid, ip) {
    const rl = healthLimiter.check(ip);
    if (!rl.allowed) {
      return sendFailure(res, { status: 429, body: { error: 'Too many requests.', code: 'bff_rate_limited', retry_after: rl.retryAfterSec }, retryAfter: rl.retryAfterSec }, rid);
    }
    if (!healthCache || now() - healthCache.at > HEALTH_CACHE_MS) {
      /** @type {'ready' | 'not_ready' | 'unreachable'} */
      let upstream = 'unreachable';
      const ac = new AbortController();
      const timer = setTimeout(() => ac.abort(), HEALTH_TIMEOUT_MS);
      try {
        // /readyz is unauthenticated on the API; the service key is deliberately NOT sent
        const r = await doFetch(`${config.upstreamBase}/readyz`, { signal: ac.signal, redirect: 'error', headers: { accept: 'application/json' } });
        await r.body?.cancel();
        upstream = r.ok ? 'ready' : 'not_ready';
      } catch {
        upstream = 'unreachable';
      } finally {
        clearTimeout(timer);
      }
      const ok = upstream === 'ready';
      healthCache = { at: now(), status: ok ? 200 : 503, body: { ok, bff: 'ok', upstream } };
    }
    sendJson(res, healthCache.status, healthCache.body, healthCache.status === 200 ? {} : { 'retry-after': '5' });
  }

  // ------------------------------------------------------------ dispatcher
  /** @param {Req} req @param {Res} res */
  async function handle(req, res) {
    const started = performance.now();
    const rid = newRequestId();
    /** @type {Record<string, unknown>} */
    const info = {};
    for (const [k, v] of Object.entries(SECURITY_HEADERS)) res.setHeader(k, v);
    if (config.cookieSecure) res.setHeader('strict-transport-security', HSTS_HEADER);

    const rawUrl = req.url ?? '/';
    const qs = rawUrl.indexOf('?');
    const pathname = qs === -1 ? rawUrl : rawUrl.slice(0, qs);
    const isStatic = !pathname.startsWith('/api/') && pathname !== '/healthz';
    res.once('close', () => {
      if (isStatic && res.statusCode < 400) return; // static hits are noise
      const status = res.statusCode;
      log({
        level: status >= 500 ? 'error' : status >= 400 ? 'warn' : 'info',
        msg: 'request',
        method: req.method,
        path: pathname.slice(0, 200),
        status,
        ms: Math.round(performance.now() - started),
        rid,
        aborted: !res.writableFinished || undefined,
        ...info,
      });
    });

    if (!pathname.startsWith('/')) {
      return sendFailure(res, { status: 400, body: { error: 'Bad request target.', code: 'invalid_request' } }, rid);
    }
    const method = req.method ?? 'GET';
    const ip = clientIp(req.socket.remoteAddress, /** @type {string | undefined} */ (req.headers['x-forwarded-for']), config.trustedProxies);

    if (pathname === '/healthz') {
      if (method !== 'GET' && method !== 'HEAD') return sendFailure(res, { status: 405, body: { error: 'Method not allowed.', code: 'method_not_allowed' } }, rid);
      return sendJson(res, 200, { status: 'ok' });
    }
    if (pathname === '/api/health') {
      if (method !== 'GET') return sendFailure(res, { status: 405, body: { error: 'Method not allowed.', code: 'method_not_allowed' } }, rid);
      return handleHealth(res, rid, ip);
    }
    if (pathname === '/api/query') {
      if (method !== 'POST') {
        res.setHeader('allow', 'POST');
        return sendFailure(res, { status: 405, body: { error: 'Method not allowed.', code: 'method_not_allowed' } }, rid);
      }
      return handleQuery(req, res, rid, ip, info);
    }
    if (pathname.startsWith('/api/')) {
      return sendFailure(res, { status: 404, body: { error: 'Not found.', code: 'not_found' } }, rid);
    }
    if (method !== 'GET' && method !== 'HEAD') {
      res.setHeader('allow', 'GET, HEAD');
      return sendFailure(res, { status: 405, body: { error: 'Method not allowed.', code: 'method_not_allowed' } }, rid);
    }
    if (!/\.\w+$/.test(pathname) || pathname === '/index.html') ensureBrowserId(req, res, true); // page load: issue/refresh the cookie
    return serveStatic(req, res, pathname);
  }

  const server = http.createServer((req, res) => {
    handle(req, res).catch((err) => {
      log({ level: 'error', msg: 'unhandled', err: err instanceof Error ? err.name : 'error' });
      if (!res.headersSent) sendFailure(res, { status: 500, body: { error: 'Internal error.', code: 'internal_error' } }, 'n/a');
      else res.destroy();
    });
  });
  server.requestTimeout = 30_000;
  server.headersTimeout = 15_000;
  server.keepAliveTimeout = 5_000;

  return {
    server,
    config,
    /** @returns {Promise<{ port: number, host: string }>} */
    listen() {
      return new Promise((resolve, reject) => {
        server.once('error', reject);
        server.listen(config.port, config.host, () => {
          server.off('error', reject);
          const addr = server.address();
          resolve({ port: typeof addr === 'object' && addr ? addr.port : config.port, host: config.host });
        });
      });
    },
    /** @returns {Promise<void>} */
    close() {
      return new Promise((resolve) => {
        server.close(() => resolve());
        server.closeIdleConnections();
        setTimeout(() => server.closeAllConnections(), 5000).unref();
      });
    },
  };
}
