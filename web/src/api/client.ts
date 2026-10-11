import { ApiError, kindForStatus, parseRetryAfter } from './errors';

/** Mirrors MAX_QUERY_CHARS of the API (default 4000); the API stays the authority. */
export const MAX_QUERY_CHARS = 4000;
/** A little above the BFF's upstream timeout (65 s) so the BFF's 504 arrives first. */
export const CLIENT_TIMEOUT_MS = 75_000;
/** The longest we are willing to wait before the single automatic retry of a 409. */
export const MAX_BUSY_WAIT_S = 5;

export interface QueryAnswer {
  answer: string;
  sessionId: string;
  sources: string[];
  thoughtProcess: string[];
  status: string | null;
  blocked: boolean;
  cacheHit: boolean;
  iterations: number;
  requestId: string | null;
}

export interface SendOptions {
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
  now?: () => number;
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function strings(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [];
}

/** Validate the BFF's success body. Anything unexpected is an error, never a half-rendered answer. */
export function parseAnswer(data: unknown): QueryAnswer {
  if (!isRecord(data) || typeof data.answer !== 'string') {
    throw new ApiError('unknown', 'The server returned an answer in an unexpected format.');
  }
  const iterations = data.iterations;
  return {
    answer: data.answer,
    sessionId: typeof data.session_id === 'string' ? data.session_id : '',
    sources: strings(data.sources),
    thoughtProcess: strings(data.thought_process),
    status: typeof data.status === 'string' ? data.status : null,
    blocked: data.blocked === true,
    cacheHit: data.cache_hit === true,
    iterations: typeof iterations === 'number' && Number.isFinite(iterations) ? iterations : 0,
    requestId: typeof data.request_id === 'string' ? data.request_id : null,
  };
}

/** Turn a non-2xx BFF response into an ApiError; the Retry-After header wins over the body's retry_after. */
export async function errorFromResponse(res: Response, nowMs: number): Promise<ApiError> {
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    /* not JSON (proxy error page, etc.) */
  }
  const rec = isRecord(body) ? body : {};
  const bodyRetry = typeof rec.retry_after === 'number' && Number.isFinite(rec.retry_after) ? Math.max(1, Math.ceil(rec.retry_after)) : null;
  const retryAfter = parseRetryAfter(res.headers.get('retry-after'), nowMs) ?? bodyRetry;
  const message = typeof rec.error === 'string' && rec.error !== '' ? rec.error : `The server answered with HTTP ${String(res.status)}.`;
  return new ApiError(kindForStatus(res.status), message, {
    status: res.status,
    retryAfter,
    requestId: typeof rec.request_id === 'string' ? rec.request_id : (res.headers.get('x-request-id') ?? null),
    code: typeof rec.code === 'string' ? rec.code : null,
  });
}

/** POST /api/query once. Throws ApiError for every failure (including network failures and client-side timeouts). */
export async function sendQuery(q: string, sessionId: string, opts: SendOptions = {}): Promise<QueryAnswer> {
  const doFetch = opts.fetchImpl ?? fetch;
  const now = opts.now ?? Date.now;
  const timeout = new AbortController();
  const timer = setTimeout(() => {
    timeout.abort();
  }, opts.timeoutMs ?? CLIENT_TIMEOUT_MS);
  const onAbort = () => {
    timeout.abort();
  };
  if (opts.signal?.aborted) timeout.abort();
  else opts.signal?.addEventListener('abort', onAbort, { once: true });

  try {
    let res: Response;
    try {
      res = await doFetch('/api/query', {
        method: 'POST',
        headers: { 'content-type': 'application/json', accept: 'application/json' },
        body: JSON.stringify({ q, session_id: sessionId }),
        credentials: 'same-origin',
        signal: timeout.signal,
      });
    } catch (e) {
      if (opts.signal?.aborted) throw e; // the caller cancelled: let AbortError through untouched
      // Read the deadline off our own controller rather than a `timedOut` flag set inside the timer callback.
      // Reaching this line means the caller did NOT abort, so if our controller is aborted the timer is the
      // only thing that could have done it. The flag was correct at runtime but TypeScript does not track
      // assignments made inside a callback, so it narrowed it to `false` forever and both branches below were
      // typed as dead code - the sort of unsound narrowing that silently survives a refactor.
      const timedOut = timeout.signal.aborted;
      throw new ApiError('network', timedOut ? 'The request timed out.' : 'The server could not be reached.', { code: timedOut ? 'client_timeout' : 'network' });
    }
    if (!res.ok) throw await errorFromResponse(res, now());
    let data: unknown;
    try {
      data = await res.json();
    } catch {
      throw new ApiError('unknown', 'The server returned an answer that is not valid JSON.', { status: res.status });
    }
    return parseAnswer(data);
  } finally {
    clearTimeout(timer);
    opts.signal?.removeEventListener('abort', onAbort);
  }
}

function delay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('Aborted', 'AbortError'));
      return;
    }
    const t = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(t);
      reject(new DOMException('Aborted', 'AbortError'));
    };
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

export interface BusyRetryOptions extends SendOptions {
  /** Called once, right before waiting for the automatic retry. */
  onBusyRetry?: (waitSeconds: number) => void;
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>;
}

/**
 * Send, and if the API says the conversation is busy (409) wait Retry-After (default 1 s, at most MAX_BUSY_WAIT_S) and try
 * exactly once more. A second 409 is returned to the caller as a normal error.
 */
export async function sendQueryWithBusyRetry(q: string, sessionId: string, opts: BusyRetryOptions = {}): Promise<QueryAnswer> {
  try {
    return await sendQuery(q, sessionId, opts);
  } catch (e) {
    if (!(e instanceof ApiError) || e.kind !== 'session_busy') throw e;
    const wait = Math.min(MAX_BUSY_WAIT_S, e.retryAfter ?? 1);
    opts.onBusyRetry?.(wait);
    await (opts.sleep ?? delay)(wait * 1000, opts.signal);
    return sendQuery(q, sessionId, opts);
  }
}

export type Health = 'ready' | 'not_ready' | 'unreachable';

/** GET /api/health -> what the BFF could learn about the API. Never throws. */
export async function fetchHealth(opts: { signal?: AbortSignal; fetchImpl?: typeof fetch } = {}): Promise<Health> {
  try {
    const res = await (opts.fetchImpl ?? fetch)('/api/health', { credentials: 'same-origin', ...(opts.signal ? { signal: opts.signal } : {}) });
    const body: unknown = await res.json().catch(() => null);
    const upstream = isRecord(body) ? body.upstream : undefined;
    if (upstream === 'ready') return 'ready';
    if (upstream === 'not_ready') return 'not_ready';
    return res.ok ? 'ready' : 'unreachable';
  } catch {
    return 'unreachable';
  }
}
