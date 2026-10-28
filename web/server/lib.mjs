// @ts-check
// Pure helpers for the BFF (no I/O, no globals except an injectable clock) so every security-relevant rule is unit-testable.
import { randomBytes } from 'node:crypto';
import net from 'node:net';
import { sanitizeTrace } from './trace.mjs';

/** The API's own session-id rule (app/api.py SESSION_RE). The DERIVED id must always satisfy it. */
export const SESSION_RE = /^[A-Za-z0-9_-]{1,64}$/;
/** The part chosen by the browser. 22 (browser id) + 1 ("-") + 40 = 63 <= 64. */
export const CLIENT_SESSION_RE = /^[A-Za-z0-9_-]{1,40}$/;
/** randomBytes(16) as base64url is always exactly 22 characters. */
export const BROWSER_ID_RE = /^[A-Za-z0-9_-]{22}$/;
/** What the upstream API accepts as X-Request-ID. */
export const REQUEST_ID_RE = /^[A-Za-z0-9._-]{1,64}$/;

/** Body keys the browser may send. Everything else (thread_id, tenant_id, include_contexts, ...) is dropped. */
export const ALLOWED_BODY_KEYS = new Set(['q', 'session_id']);
/** Names worth reporting in logs when a client tried to send them (names only, never values). */
const SENSITIVE_BODY_KEYS = new Set(['thread_id', 'tenant_id', 'include_contexts', 'api_key', 'x-api-key', 'user_id']);

export const SECURITY_HEADERS = Object.freeze({
  'content-security-policy':
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; " +
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
  'x-content-type-options': 'nosniff',
  'referrer-policy': 'no-referrer',
  'x-frame-options': 'DENY',
  'cross-origin-opener-policy': 'same-origin',
  'cross-origin-resource-policy': 'same-origin',
  'permissions-policy': 'camera=(), microphone=(), geolocation=(), payment=(), usb=()',
  'x-permitted-cross-domain-policies': 'none',
});
export const HSTS_HEADER = 'max-age=15552000';

// ---------------------------------------------------------------- identifiers ----------------------------------------

/** Opaque, unguessable browser id (128 bits). It is the only thing separating end users' conversations, so it never leaves the cookie. */
export function newBrowserId() {
  return randomBytes(16).toString('base64url');
}

/** Upstream-safe request id: lets one id be followed BFF log -> API log -> response header. */
export function newRequestId() {
  return `web-${randomBytes(9).toString('base64url')}`;
}

/**
 * The session id sent to the API: `<browserid>-<clientSessionId>`. Returns null unless both parts are well formed and the
 * result satisfies the API's rule. The tenant/user part of the thread key is added by the API from the key, never by us.
 * @param {unknown} browserId
 * @param {unknown} clientSessionId
 * @returns {string | null}
 */
export function deriveSessionId(browserId, clientSessionId) {
  if (typeof browserId !== 'string' || !BROWSER_ID_RE.test(browserId)) return null;
  if (typeof clientSessionId !== 'string' || !CLIENT_SESSION_RE.test(clientSessionId)) return null;
  const derived = `${browserId}-${clientSessionId}`;
  return SESSION_RE.test(derived) ? derived : null;
}

// ---------------------------------------------------------------- cookies --------------------------------------------

/**
 * @param {string | undefined} header
 * @returns {Record<string, string>}
 */
export function parseCookies(header) {
  /** @type {Record<string, string>} */
  const out = Object.create(null);
  if (!header) return out;
  for (const part of header.split(';')) {
    const i = part.indexOf('=');
    if (i < 0) continue;
    const name = part.slice(0, i).trim();
    if (name === '') continue;
    let value = part.slice(i + 1).trim();
    if (value.length >= 2 && value.startsWith('"') && value.endsWith('"')) value = value.slice(1, -1);
    if (!(name in out)) out[name] = value; // first occurrence wins (most specific path first per RFC 6265)
  }
  return out;
}

/**
 * Cookie that carries the browser id: HttpOnly (page scripts cannot read it), SameSite=Strict (never sent cross-site).
 * `__Host-` prefix when Secure: browsers then refuse a cookie set from a subdomain or with a Domain attribute.
 * @param {{ secure: boolean }} opts
 */
export function cookieName(opts) {
  return opts.secure ? '__Host-rag_bid' : 'rag_bid';
}

/**
 * @param {string} name
 * @param {string} browserId
 * @param {{ secure: boolean, maxAgeSec?: number }} opts
 */
export function serializeBrowserCookie(name, browserId, opts) {
  if (!BROWSER_ID_RE.test(browserId)) throw new Error('refusing to serialise a malformed browser id');
  const maxAge = opts.maxAgeSec ?? 365 * 24 * 3600;
  return `${name}=${browserId}; Path=/; Max-Age=${maxAge}; HttpOnly; SameSite=Strict${opts.secure ? '; Secure' : ''}`;
}

// ---------------------------------------------------------------- request validation ---------------------------------

/**
 * @typedef {{ ok: true, value: { q: string, clientSessionId: string }, stripped: string[] }} BodyOk
 * @typedef {{ ok: false, status: number, code: string, message: string }} BodyErr
 */

/**
 * Validate the browser's JSON body and reduce it to the two fields the API gets from us. Anything else is dropped; the
 * names of well-known dangerous keys (thread_id, tenant_id, ...) are reported so the caller can log that it happened.
 * @param {unknown} parsed
 * @param {{ maxQueryChars: number }} opts
 * @returns {BodyOk | BodyErr}
 */
export function sanitizeQueryBody(parsed, opts) {
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { ok: false, status: 400, code: 'invalid_request', message: 'The request body must be a JSON object.' };
  }
  const body = /** @type {Record<string, unknown>} */ (parsed);
  const stripped = Object.keys(body).filter((k) => !ALLOWED_BODY_KEYS.has(k) && SENSITIVE_BODY_KEYS.has(k.toLowerCase()));
  const q = body['q'];
  if (typeof q !== 'string' || q.trim() === '') {
    return { ok: false, status: 422, code: 'invalid_request', message: 'Please enter a question.' };
  }
  const text = q.trim();
  if (text.length > opts.maxQueryChars * 2 || Array.from(text).length > opts.maxQueryChars) {
    return {
      ok: false,
      status: 413,
      code: 'payload_too_large',
      message: `That message is too long. Please keep it under ${opts.maxQueryChars} characters.`,
    };
  }
  const sid = body['session_id'];
  if (typeof sid !== 'string' || !CLIENT_SESSION_RE.test(sid)) {
    return { ok: false, status: 422, code: 'invalid_session', message: 'session_id must match [A-Za-z0-9_-]{1,40}.' };
  }
  return { ok: true, value: { q: text, clientSessionId: sid }, stripped };
}

/**
 * Headers for the upstream call. Built from scratch: NOTHING the browser sent is forwarded (not X-API-Key, Authorization,
 * Cookie, X-Tenant-Id, X-Forwarded-*, ...), so a client cannot smuggle identity upstream.
 * @param {{ apiKey: string, requestId: string }} p
 * @returns {Record<string, string>}
 */
export function buildUpstreamHeaders(p) {
  return {
    'content-type': 'application/json',
    accept: 'application/json',
    'user-agent': 'rag-web-bff/0.1',
    'x-api-key': p.apiKey,
    'x-request-id': p.requestId,
  };
}

/**
 * Same-origin guard for state-changing requests. A missing Origin is allowed (non-browser clients cannot ride our cookie);
 * `Origin: null` and any other site are refused.
 * @param {{ origin?: string, host?: string, secFetchSite?: string }} h
 * @param {Set<string>} allowedOrigins exact origins such as "https://chat.example.com"
 */
export function isAllowedOrigin(h, allowedOrigins) {
  if (h.secFetchSite === 'cross-site') return false;
  if (h.origin === undefined || h.origin === '') return true;
  if (allowedOrigins.has(h.origin)) return true;
  try {
    const u = new URL(h.origin);
    return (u.protocol === 'http:' || u.protocol === 'https:') && h.host !== undefined && u.host.toLowerCase() === h.host.toLowerCase();
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------- upstream mapping -----------------------------------

/**
 * Retry-After as whole seconds clamped to [1, 3600]; accepts delta-seconds or an HTTP-date. undefined when unusable.
 * @param {string | null | undefined} value
 * @param {number} [now] epoch ms
 * @returns {number | undefined}
 */
export function parseRetryAfter(value, now = Date.now()) {
  if (value === null || value === undefined) return undefined;
  const v = value.trim();
  if (v === '') return undefined;
  let secs;
  if (/^\d{1,9}$/.test(v)) secs = Number(v);
  else {
    // only the IMF-fixdate form ("Thu, 01 Jan 2026 00:00:30 GMT"); Date.parse alone would also accept junk such as "-5"
    if (!/^[A-Za-z]{3}, \d{2} [A-Za-z]{3} \d{4} \d{2}:\d{2}:\d{2} GMT$/.test(v)) return undefined;
    const t = Date.parse(v);
    if (Number.isNaN(t)) return undefined;
    secs = Math.ceil((t - now) / 1000);
  }
  return Math.min(3600, Math.max(1, secs));
}

/**
 * @typedef {{ status: number, body: { error: string, code: string, request_id?: string, retry_after?: number }, retryAfter?: number }} Failure
 */

/**
 * A short, printable message taken from an upstream error body ({"error": "..."} or FastAPI's {"detail": "..."}).
 * @param {string} raw
 * @returns {{ message?: string, requestId?: string }}
 */
export function extractUpstreamError(raw) {
  let obj;
  try {
    obj = JSON.parse(raw);
  } catch {
    return {};
  }
  if (obj === null || typeof obj !== 'object') return {};
  const rec = /** @type {Record<string, unknown>} */ (obj);
  const m = typeof rec['error'] === 'string' ? rec['error'] : typeof rec['detail'] === 'string' ? rec['detail'] : undefined;
  // eslint-disable-next-line no-control-regex
  const message = m?.replace(/[\u0000-\u001f\u007f]/g, ' ').trim().slice(0, 200) || undefined;
  const rid = rec['request_id'];
  return { message, requestId: typeof rid === 'string' && REQUEST_ID_RE.test(rid) ? rid : undefined };
}

/**
 * Translate a non-2xx upstream answer into what the browser gets. 401/409/413/429/503 and Retry-After are propagated;
 * the 401 text is fixed (it is an operator problem, and the upstream wording is not for end users); other statuses become 502.
 * @param {number} status
 * @param {string} rawBody
 * @param {string | null | undefined} retryAfterHeader
 * @param {number} [now]
 * @returns {Failure}
 */
export function mapUpstreamFailure(status, rawBody, retryAfterHeader, now = Date.now()) {
  const { message, requestId } = extractUpstreamError(rawBody);
  const ra = parseRetryAfter(retryAfterHeader, now);
  /** @param {number} s @param {string} code @param {string} text @param {number} [retryAfter] @returns {Failure} */
  const f = (s, code, text, retryAfter) => ({
    status: s,
    body: {
      error: text,
      code,
      ...(requestId ? { request_id: requestId } : {}),
      ...(retryAfter ? { retry_after: retryAfter } : {}),
    },
    ...(retryAfter ? { retryAfter } : {}),
  });
  switch (status) {
    case 400:
      return f(400, 'bad_request', message ?? 'The API rejected the request.');
    case 401:
      return f(401, 'upstream_unauthorized', 'The API rejected the service key this web client uses. This is a server configuration problem.');
    case 403:
      return f(403, 'forbidden', message ?? 'The API refused the request.');
    case 409:
      return f(409, 'session_busy', 'Another request for this conversation is still running.', ra ?? 1);
    case 413:
      return f(413, 'payload_too_large', message ?? 'The message is too large.');
    case 422:
      return f(422, 'invalid_request', message ?? 'The API could not process the request.');
    case 429:
      return f(429, 'rate_limited', message ?? 'Rate limit exceeded.', ra ?? 30);
    case 503:
      return f(503, 'unavailable', 'The assistant is temporarily unavailable.', ra ?? 10);
    default:
      return f(502, 'upstream_error', 'The API returned an unexpected error.');
  }
}

/**
 * Transport-level failure (connection refused, DNS, timeout) -> 503/504 with a retry hint.
 * @param {unknown} err
 * @param {boolean} timedOut true when our own timeout fired
 * @returns {Failure}
 */
export function mapFetchError(err, timedOut) {
  const name = err instanceof Error ? err.name : '';
  if (timedOut || name === 'TimeoutError') {
    return { status: 504, body: { error: 'The assistant took too long to answer.', code: 'upstream_timeout', retry_after: 5 }, retryAfter: 5 };
  }
  return { status: 503, body: { error: 'The assistant is unreachable right now.', code: 'upstream_unreachable', retry_after: 5 }, retryAfter: 5 };
}

/**
 * Reduce the API's success body to the documented fields, with `session_id` replaced by the id the browser used (the derived
 * id embeds the browser id from the HttpOnly cookie and must not be echoed to page scripts). `contexts` is never forwarded.
 * @param {unknown} data
 * @param {string} clientSessionId
 * @param {string} requestId
 * @returns {Record<string, unknown> | null} null when the body is not a valid answer
 */
export function normalizeQueryResponse(data, clientSessionId, requestId) {
  if (data === null || typeof data !== 'object' || Array.isArray(data)) return null;
  const d = /** @type {Record<string, unknown>} */ (data);
  if (typeof d['answer'] !== 'string') return null;
  /** @param {unknown} v */
  const strings = (v) => (Array.isArray(v) ? v.filter((x) => typeof x === 'string').slice(0, 100) : []);
  const iterations = Number(d['iterations']);
  const trace = sanitizeTrace(d['trace']);
  return {
    ...(trace ? { trace } : {}),
    answer: d['answer'],
    session_id: clientSessionId,
    sources: strings(d['sources']),
    thought_process: strings(d['thought_process']),
    status: typeof d['status'] === 'string' ? d['status'] : null,
    blocked: d['blocked'] === true,
    cache_hit: d['cache_hit'] === true,
    iterations: Number.isFinite(iterations) ? iterations : 0,
    request_id: requestId,
  };
}

// ---------------------------------------------------------------- rate limiting --------------------------------------

/**
 * Per-key token bucket (capacity = `limit`, refilled evenly over `windowMs`). Bounded memory: when `maxKeys` is reached,
 * idle (full) buckets are dropped first, then the oldest entry. An attacker rotating source addresses can therefore evict
 * other keys' state, which only resets their counters - it never lets the attacker exceed the per-address limit.
 * @param {{ limit: number, windowMs: number, maxKeys?: number, now?: () => number }} cfg
 */
export function createRateLimiter(cfg) {
  const maxKeys = cfg.maxKeys ?? 10_000;
  const now = cfg.now ?? Date.now;
  const ratePerMs = cfg.limit / cfg.windowMs;
  /** @type {Map<string, { tokens: number, t: number }>} */
  const buckets = new Map();

  /** @param {string} key */
  function check(key) {
    const t = now();
    let b = buckets.get(key);
    if (b === undefined) {
      if (buckets.size >= maxKeys) prune(t);
      b = { tokens: cfg.limit, t };
    } else {
      b.tokens = Math.min(cfg.limit, b.tokens + (t - b.t) * ratePerMs);
      b.t = t;
      buckets.delete(key); // re-insert: keeps Map order = least recently used first
    }
    buckets.set(key, b);
    if (b.tokens >= 1) {
      b.tokens -= 1;
      return { allowed: true, retryAfterSec: 0, remaining: Math.floor(b.tokens) };
    }
    return { allowed: false, retryAfterSec: Math.max(1, Math.ceil((1 - b.tokens) / ratePerMs / 1000)), remaining: 0 };
  }

  /** @param {number} t */
  function prune(t) {
    for (const [k, b] of buckets) {
      if (Math.min(cfg.limit, b.tokens + (t - b.t) * ratePerMs) >= cfg.limit) buckets.delete(k);
    }
    while (buckets.size >= maxKeys) {
      const oldest = buckets.keys().next();
      if (oldest.done) break;
      buckets.delete(oldest.value);
    }
  }

  return { check, size: () => buckets.size };
}

// ---------------------------------------------------------------- client address -------------------------------------

/**
 * @param {string} spec comma-separated IPs or CIDRs, e.g. "10.0.0.0/8,127.0.0.1"
 * @returns {net.BlockList | null}
 */
export function parseTrustedProxies(spec) {
  const items = spec.split(',').map((s) => s.trim()).filter(Boolean);
  if (items.length === 0) return null;
  const list = new net.BlockList();
  for (const item of items) {
    const [addr = '', prefix] = item.split('/');
    const fam = net.isIP(addr);
    if (fam === 0) throw new Error(`BFF_TRUSTED_PROXIES: "${item}" is not an IP address or CIDR`);
    const type = fam === 4 ? 'ipv4' : 'ipv6';
    if (prefix === undefined) list.addAddress(addr, type);
    else {
      const bits = Number(prefix);
      if (!Number.isInteger(bits) || bits < 0 || bits > (fam === 4 ? 32 : 128)) throw new Error(`BFF_TRUSTED_PROXIES: bad prefix in "${item}"`);
      list.addSubnet(addr, bits, type);
    }
  }
  return list;
}

/** @param {string} ip */
export function normalizeIp(ip) {
  return ip.startsWith('::ffff:') && net.isIPv4(ip.slice(7)) ? ip.slice(7) : ip;
}

/**
 * @param {net.BlockList} list
 * @param {string} ip
 */
function inList(list, ip) {
  const fam = net.isIP(ip);
  return fam !== 0 && list.check(ip, fam === 4 ? 'ipv4' : 'ipv6');
}

/**
 * Address used for per-IP limits. X-Forwarded-For is honoured ONLY when the direct peer is a trusted proxy; the chain is
 * then walked from the right and the first untrusted hop wins (left-most entries are attacker-controlled).
 * @param {string | undefined} peer socket remote address
 * @param {string | undefined} xff X-Forwarded-For header
 * @param {net.BlockList | null} trusted
 */
export function clientIp(peer, xff, trusted) {
  const p = normalizeIp(peer ?? 'unknown');
  if (!trusted || !xff || !inList(trusted, p)) return p;
  const hops = xff.split(',').map((h) => normalizeIp(h.trim())).filter(Boolean);
  for (let i = hops.length - 1; i >= 0; i--) {
    const hop = /** @type {string} */ (hops[i]);
    if (net.isIP(hop) === 0) return p; // garbage in the chain: do not trust any of it
    if (!inList(trusted, hop)) return hop;
  }
  return p;
}
