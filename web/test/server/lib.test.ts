import { describe, expect, it } from 'vitest';
import {
  BROWSER_ID_RE,
  SESSION_RE,
  buildUpstreamHeaders,
  clientIp,
  cookieName,
  createRateLimiter,
  deriveSessionId,
  isAllowedOrigin,
  mapFetchError,
  mapUpstreamFailure,
  newBrowserId,
  normalizeQueryResponse,
  parseCookies,
  parseRetryAfter,
  parseTrustedProxies,
  sanitizeQueryBody,
  serializeBrowserCookie,
} from '../../server/lib.mjs';

const BID = 'AAAAAAAAAAAAAAAAAAAAAA'; // 22 chars

describe('browser id and session id derivation', () => {
  it('generates 22-char url-safe ids that are unique', () => {
    const a = newBrowserId();
    expect(a).toMatch(BROWSER_ID_RE);
    expect(new Set(Array.from({ length: 200 }, newBrowserId)).size).toBe(200);
  });

  it('derives <browserid>-<clientSessionId> and always satisfies the API session rule', () => {
    expect(deriveSessionId(BID, 'abc123')).toBe(`${BID}-abc123`);
    const longest = deriveSessionId(BID, 'x'.repeat(40));
    expect(longest).toMatch(SESSION_RE);
    expect(longest).toHaveLength(63);
  });

  it.each([
    ['too long client part', BID, 'x'.repeat(41)],
    ['empty client part', BID, ''],
    ['space', BID, 'a b'],
    ['slash', BID, 'a/b'],
    ['dot', BID, 'a.b'],
    ['newline', BID, 'a\nb'],
    ['non-ascii', BID, 'café'],
    ['short browser id', 'abc', 'ok'],
    ['browser id with bad char', 'AAAAAAAAAAAAAAAAAAAAA!', 'ok'],
  ])('rejects %s', (_name, bid, sid) => {
    expect(deriveSessionId(bid, sid)).toBeNull();
  });

  it('rejects non-string input', () => {
    expect(deriveSessionId(undefined, 'x')).toBeNull();
    expect(deriveSessionId(BID, 5)).toBeNull();
    expect(deriveSessionId(null, null)).toBeNull();
  });

  it('keeps two browsers apart even when they choose the same client session id', () => {
    expect(deriveSessionId(newBrowserId(), 'same')).not.toBe(deriveSessionId(newBrowserId(), 'same'));
  });
});

describe('cookies', () => {
  it('parses a header and ignores malformed parts; first occurrence wins', () => {
    const c = parseCookies('a=1; b="two"; =x; junk; a=2');
    expect(c.a).toBe('1');
    expect(c.b).toBe('two');
    expect(Object.keys(c).sort()).toEqual(['a', 'b']);
    expect(parseCookies(undefined)).toEqual({});
  });

  it('does not let __proto__ style names pollute anything', () => {
    const c = parseCookies('__proto__=x; constructor=y');
    expect(Object.getPrototypeOf(c)).toBeNull();
    expect(c.__proto__).toBe('x');
  });

  it('serialises HttpOnly + SameSite=Strict, and Secure only when asked', () => {
    const plain = serializeBrowserCookie('rag_bid', BID, { secure: false });
    expect(plain).toContain('HttpOnly');
    expect(plain).toContain('SameSite=Strict');
    expect(plain).toContain('Path=/');
    expect(plain).not.toContain('Secure');
    expect(plain).not.toContain('Domain');
    const secure = serializeBrowserCookie(cookieName({ secure: true }), BID, { secure: true });
    expect(secure.startsWith('__Host-rag_bid=')).toBe(true);
    expect(secure).toContain('; Secure');
  });

  it('refuses to serialise anything that is not a well-formed browser id (header injection)', () => {
    expect(() => serializeBrowserCookie('rag_bid', 'x; Domain=evil', { secure: false })).toThrow();
    expect(() => serializeBrowserCookie('rag_bid', `${BID}\r\nSet-Cookie: a=b`, { secure: false })).toThrow();
  });
});

describe('sanitizeQueryBody', () => {
  const opts = { maxQueryChars: 50 };

  it('keeps only q and session_id and reports dangerous keys by name', () => {
    const r = sanitizeQueryBody(
      { q: '  hello  ', session_id: 's1', thread_id: 'evil', tenant_id: 'other', include_contexts: true, 'X-API-Key': 'k', extra: 1 },
      opts,
    );
    expect(r).toEqual({ ok: true, value: { q: 'hello', clientSessionId: 's1' }, stripped: ['thread_id', 'tenant_id', 'include_contexts', 'X-API-Key'] });
    // everything outside the allowlist is dropped ("extra" too); only well-known dangerous names are reported
    if (r.ok) expect(Object.keys(r.value)).toEqual(['q', 'clientSessionId']);
  });

  it('reports api key style keys without ever echoing their values', () => {
    const r = sanitizeQueryBody({ q: 'x', session_id: 's', api_key: 'rag_secret_value' }, opts);
    expect(r.ok && r.stripped).toEqual(['api_key']);
    expect(JSON.stringify(r)).not.toContain('rag_secret_value');
  });

  it.each([[null], ['str'], [5], [[]], [[{ q: 'x' }]]])('rejects a non-object body %j with 400', (body) => {
    expect(sanitizeQueryBody(body, opts)).toMatchObject({ ok: false, status: 400 });
  });

  it.each([{}, { q: 5, session_id: 's' }, { q: '   ', session_id: 's' }])('rejects a missing/blank q %j with 422', (body) => {
    expect(sanitizeQueryBody(body, opts)).toMatchObject({ ok: false, status: 422, code: 'invalid_request' });
  });

  it('rejects an over-long q with 413 and counts code points like the API', () => {
    expect(sanitizeQueryBody({ q: 'a'.repeat(51), session_id: 's' }, opts)).toMatchObject({ ok: false, status: 413, code: 'payload_too_large' });
    expect(sanitizeQueryBody({ q: 'a'.repeat(50), session_id: 's' }, opts).ok).toBe(true);
    // 50 astral characters are 100 UTF-16 units but 50 code points: allowed
    expect(sanitizeQueryBody({ q: '\u{1F600}'.repeat(50), session_id: 's' }, opts).ok).toBe(true);
    expect(sanitizeQueryBody({ q: '\u{1F600}'.repeat(51), session_id: 's' }, opts).ok).toBe(false);
  });

  it.each([undefined, '', 'a b', 'x'.repeat(41), 7])('rejects invalid session_id %j with 422', (sid) => {
    expect(sanitizeQueryBody({ q: 'hi', session_id: sid }, opts)).toMatchObject({ ok: false, status: 422, code: 'invalid_session' });
  });
});

describe('upstream request headers', () => {
  it('contains exactly the service key and fixed metadata - nothing from the browser', () => {
    const h = buildUpstreamHeaders({ apiKey: 'rag_key', requestId: 'web-abc' });
    expect(Object.keys(h).sort()).toEqual(['accept', 'content-type', 'user-agent', 'x-api-key', 'x-request-id']);
    expect(h['x-api-key']).toBe('rag_key');
    expect(h['x-request-id']).toBe('web-abc');
  });
});

describe('isAllowedOrigin', () => {
  const allowed = new Set(['https://chat.example.com']);
  it('allows same origin, missing origin and configured origins', () => {
    expect(isAllowedOrigin({ origin: 'http://localhost:8787', host: 'localhost:8787' }, allowed)).toBe(true);
    expect(isAllowedOrigin({ host: 'localhost:8787' }, allowed)).toBe(true);
    expect(isAllowedOrigin({ origin: 'https://chat.example.com', host: 'internal:8787' }, allowed)).toBe(true);
  });
  it('rejects other sites, "null" and cross-site fetches', () => {
    expect(isAllowedOrigin({ origin: 'https://evil.example', host: 'localhost:8787' }, allowed)).toBe(false);
    expect(isAllowedOrigin({ origin: 'null', host: 'localhost:8787' }, allowed)).toBe(false);
    expect(isAllowedOrigin({ origin: 'not a url', host: 'localhost:8787' }, allowed)).toBe(false);
    expect(isAllowedOrigin({ origin: 'http://localhost:8787', host: 'localhost:8787', secFetchSite: 'cross-site' }, allowed)).toBe(false);
    expect(isAllowedOrigin({ origin: 'http://localhost:8788', host: 'localhost:8787' }, allowed)).toBe(false);
  });
});

describe('parseRetryAfter', () => {
  const now = Date.parse('2026-01-01T00:00:00Z');
  it('parses delta-seconds and clamps to [1, 3600]', () => {
    expect(parseRetryAfter('7')).toBe(7);
    expect(parseRetryAfter('0')).toBe(1);
    expect(parseRetryAfter('999999')).toBe(3600);
  });
  it('parses HTTP dates', () => {
    expect(parseRetryAfter('Thu, 01 Jan 2026 00:00:30 GMT', now)).toBe(30);
    expect(parseRetryAfter('Wed, 31 Dec 2025 23:00:00 GMT', now)).toBe(1);
  });
  it('returns undefined for junk', () => {
    for (const v of [undefined, null, '', ' ', 'soon', '-5', '1.5', '12abc']) expect(parseRetryAfter(v, now)).toBeUndefined();
  });
});

describe('mapUpstreamFailure', () => {
  const body = (o: unknown) => JSON.stringify(o);

  it('propagates 429 with Retry-After in header value and body', () => {
    const f = mapUpstreamFailure(429, body({ error: 'rate limit exceeded (minute)', request_id: 'r1' }), '42');
    expect(f.status).toBe(429);
    expect(f.retryAfter).toBe(42);
    expect(f.body).toEqual({ error: 'rate limit exceeded (minute)', code: 'rate_limited', request_id: 'r1', retry_after: 42 });
  });

  it('propagates 503 and 409 with retry hints, defaulting when the header is missing', () => {
    expect(mapUpstreamFailure(503, body({ error: 'x' }), '7')).toMatchObject({ status: 503, retryAfter: 7, body: { code: 'unavailable', retry_after: 7 } });
    expect(mapUpstreamFailure(503, '', null)).toMatchObject({ status: 503, retryAfter: 10 });
    expect(mapUpstreamFailure(409, body({ error: 'busy' }), '1')).toMatchObject({ status: 409, body: { code: 'session_busy', retry_after: 1 } });
    expect(mapUpstreamFailure(409, '', undefined)).toMatchObject({ status: 409, retryAfter: 1 });
    expect(mapUpstreamFailure(429, 'not json', undefined)).toMatchObject({ status: 429, retryAfter: 30 });
  });

  it('maps 401 to a fixed operator-facing message and does not leak the upstream wording', () => {
    const f = mapUpstreamFailure(401, body({ detail: 'invalid API key' }), null);
    expect(f.status).toBe(401);
    expect(f.body.code).toBe('upstream_unauthorized');
    expect(f.body.error).not.toContain('invalid API key');
    expect(f.retryAfter).toBeUndefined();
  });

  it('propagates 413 and understands FastAPI {"detail"} bodies', () => {
    expect(mapUpstreamFailure(413, body({ error: 'payload too large' }), null)).toMatchObject({ status: 413, body: { code: 'payload_too_large', error: 'payload too large' } });
    expect(mapUpstreamFailure(413, body({ detail: 'q exceeds 4000 characters' }), null).body.error).toBe('q exceeds 4000 characters');
  });

  it('turns other upstream statuses into a generic 502 without echoing the body', () => {
    for (const s of [404, 500, 502, 504, 418]) {
      const f = mapUpstreamFailure(s, body({ error: 'Traceback (most recent call last) secret' }), null);
      expect(f.status).toBe(502);
      expect(f.body.code).toBe('upstream_error');
      expect(JSON.stringify(f.body)).not.toContain('Traceback');
    }
  });

  it('ignores a request_id that is not a safe token, and strips control characters from messages', () => {
    const f = mapUpstreamFailure(400, body({ error: 'bad\r\nthing', request_id: 'x y<script>' }), null);
    expect(f.body.request_id).toBeUndefined();
    expect(f.body.error).toBe('bad  thing');
  });

  it('survives non-string detail (422 validation arrays) and truncates long messages', () => {
    expect(mapUpstreamFailure(422, body({ detail: [{ msg: 'x' }] }), null).body.error).toBe('The API could not process the request.');
    expect(mapUpstreamFailure(400, body({ error: 'e'.repeat(1000) }), null).body.error).toHaveLength(200);
  });
});

describe('mapFetchError', () => {
  it('maps timeouts to 504 and everything else to 503, both with Retry-After', () => {
    expect(mapFetchError(new Error('x'), true)).toMatchObject({ status: 504, retryAfter: 5, body: { code: 'upstream_timeout' } });
    const te = new Error('t');
    te.name = 'TimeoutError';
    expect(mapFetchError(te, false).status).toBe(504);
    expect(mapFetchError(new TypeError('fetch failed'), false)).toMatchObject({ status: 503, retryAfter: 5, body: { code: 'upstream_unreachable' } });
    expect(mapFetchError('weird', false).status).toBe(503);
  });
});

describe('normalizeQueryResponse', () => {
  it('keeps the documented fields, rewrites session_id and drops contexts/unknown fields', () => {
    const out = normalizeQueryResponse(
      { answer: 'A', session_id: `${BID}-s1`, sources: ['a.txt', 3, 'b.txt'], thought_process: ['x'], status: 'Response generated.', blocked: false, cache_hit: true, iterations: 2, contexts: ['secret chunk'], thread_id: 't:u:s' },
      's1',
      'web-1',
    );
    expect(out).toEqual({
      answer: 'A', session_id: 's1', sources: ['a.txt', 'b.txt'], thought_process: ['x'], status: 'Response generated.',
      blocked: false, cache_hit: true, iterations: 2, request_id: 'web-1',
    });
    expect(JSON.stringify(out)).not.toContain(BID);
  });

  it('is tolerant of missing optional fields and rejects bodies without an answer', () => {
    expect(normalizeQueryResponse({ answer: 'A' }, 's', 'r')).toMatchObject({ sources: [], thought_process: [], status: null, blocked: false, cache_hit: false, iterations: 0 });
    for (const bad of [null, 'x', [], {}, { answer: 5 }]) expect(normalizeQueryResponse(bad, 's', 'r')).toBeNull();
  });
});

describe('createRateLimiter (token bucket)', () => {
  it('allows `limit` requests, then blocks with a Retry-After, then refills', () => {
    let t = 1_000_000;
    const rl = createRateLimiter({ limit: 3, windowMs: 60_000, now: () => t });
    expect([rl.check('a').allowed, rl.check('a').allowed, rl.check('a').allowed]).toEqual([true, true, true]);
    const blocked = rl.check('a');
    expect(blocked).toMatchObject({ allowed: false });
    expect(blocked.retryAfterSec).toBe(20); // one token = 60s / 3
    t += 19_000;
    expect(rl.check('a').allowed).toBe(false);
    t += 1_500;
    expect(rl.check('a').allowed).toBe(true);
  });

  it('isolates keys from each other', () => {
    const rl = createRateLimiter({ limit: 1, windowMs: 60_000, now: () => 0 });
    expect(rl.check('a').allowed).toBe(true);
    expect(rl.check('a').allowed).toBe(false);
    expect(rl.check('b').allowed).toBe(true);
  });

  it('never refills beyond capacity', () => {
    let t = 0;
    const rl = createRateLimiter({ limit: 2, windowMs: 1000, now: () => t });
    rl.check('a');
    t += 3_600_000;
    expect(rl.check('a').remaining).toBe(1);
    expect(rl.check('a').allowed).toBe(true);
    expect(rl.check('a').allowed).toBe(false);
  });

  it('bounds memory: never holds more than maxKeys entries', () => {
    const rl = createRateLimiter({ limit: 1, windowMs: 60_000, maxKeys: 50, now: () => 0 });
    for (let i = 0; i < 1000; i++) rl.check(`ip-${i}`);
    expect(rl.size()).toBeLessThanOrEqual(50);
  });

  it('an address that is blocked stays blocked even while other addresses churn the table', () => {
    const rl = createRateLimiter({ limit: 1, windowMs: 60_000, maxKeys: 10, now: () => 0 });
    rl.check('victim');
    expect(rl.check('victim').allowed).toBe(false);
    for (let i = 0; i < 5; i++) rl.check(`other-${i}`); // under capacity: no eviction
    expect(rl.check('victim').allowed).toBe(false);
  });
});

describe('clientIp / trusted proxies', () => {
  const trusted = parseTrustedProxies('10.0.0.0/8, 127.0.0.1');

  it('ignores X-Forwarded-For unless the direct peer is a trusted proxy', () => {
    expect(clientIp('203.0.113.5', '1.2.3.4', trusted)).toBe('203.0.113.5');
    expect(clientIp('203.0.113.5', '1.2.3.4', null)).toBe('203.0.113.5');
  });

  it('walks the chain from the right and returns the first untrusted hop', () => {
    expect(clientIp('10.1.1.1', '6.6.6.6, 198.51.100.7, 10.2.2.2', trusted)).toBe('198.51.100.7');
    expect(clientIp('127.0.0.1', '198.51.100.7', trusted)).toBe('198.51.100.7');
    expect(clientIp('10.1.1.1', '10.3.3.3', trusted)).toBe('10.1.1.1');
  });

  it('normalises IPv4-mapped IPv6 peers and distrusts garbage chains', () => {
    expect(clientIp('::ffff:127.0.0.1', '198.51.100.7', trusted)).toBe('198.51.100.7');
    expect(clientIp('::ffff:203.0.113.5', undefined, trusted)).toBe('203.0.113.5');
    expect(clientIp('10.1.1.1', 'not-an-ip, 10.2.2.2', trusted)).toBe('10.1.1.1');
    expect(clientIp(undefined, undefined, null)).toBe('unknown');
  });

  it('rejects malformed trusted proxy specs', () => {
    expect(() => parseTrustedProxies('not-an-ip')).toThrow(/BFF_TRUSTED_PROXIES/);
    expect(() => parseTrustedProxies('10.0.0.0/99')).toThrow(/prefix/);
    expect(parseTrustedProxies('')).toBeNull();
  });
});
