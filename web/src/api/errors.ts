/** Everything the UI needs to tell failures apart. The BFF maps upstream failures onto these statuses (see DOCS/17_WEB_CLIENT.md). */
export type ApiErrorKind =
  | 'unauthorized' // 401: the BFF's own service key was rejected by the API (operator problem, not the user's)
  | 'rate_limited' // 429: API key limit or BFF per-IP limit
  | 'session_busy' // 409: the previous request on this conversation is still running
  | 'too_large' // 413: message over the size limit
  | 'unavailable' // 502/503/504: API (or a dependency of it) is down or slow
  | 'network' // the browser could not reach the BFF at all (offline, BFF down, DNS) or the client timeout fired
  | 'invalid' // 400/422: the request was rejected as malformed
  | 'forbidden' // 403
  | 'unknown';

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  /** HTTP status, or 0 when no response was received. */
  readonly status: number;
  /** Seconds to wait before retrying, when the server said so. */
  readonly retryAfter: number | null;
  readonly requestId: string | null;
  /** Machine-readable code from the BFF body (e.g. "bff_rate_limited"), if any. */
  readonly code: string | null;

  constructor(
    kind: ApiErrorKind,
    message: string,
    extra: { status?: number; retryAfter?: number | null; requestId?: string | null; code?: string | null } = {},
  ) {
    super(message);
    this.name = 'ApiError';
    this.kind = kind;
    this.status = extra.status ?? 0;
    this.retryAfter = extra.retryAfter ?? null;
    this.requestId = extra.requestId ?? null;
    this.code = extra.code ?? null;
  }
}

export function kindForStatus(status: number): ApiErrorKind {
  switch (status) {
    case 400:
    case 422:
      return 'invalid';
    case 401:
      return 'unauthorized';
    case 403:
      return 'forbidden';
    case 409:
      return 'session_busy';
    case 413:
      return 'too_large';
    case 429:
      return 'rate_limited';
    case 502:
    case 503:
    case 504:
      return 'unavailable';
    default:
      return 'unknown';
  }
}

/**
 * Retry-After as whole seconds (1..3600) from delta-seconds or an HTTP-date; null when absent or unusable.
 */
export function parseRetryAfter(value: string | null | undefined, nowMs: number): number | null {
  if (value === null || value === undefined) return null;
  const v = value.trim();
  if (v === '') return null;
  let secs: number;
  if (/^\d{1,9}$/.test(v)) secs = Number(v);
  else {
    if (!/^[A-Za-z]{3}, \d{2} [A-Za-z]{3} \d{4} \d{2}:\d{2}:\d{2} GMT$/.test(v)) return null;
    const t = Date.parse(v);
    if (Number.isNaN(t)) return null;
    secs = Math.ceil((t - nowMs) / 1000);
  }
  return Math.min(3600, Math.max(1, secs));
}

/** What the user sees. Pure so it can be unit-tested without rendering. */
export interface UiError {
  kind: ApiErrorKind;
  title: string;
  detail: string;
  /** Seconds to wait; the view turns it into a live countdown. */
  retryAfter: number | null;
  /** Whether a "Try again" button makes sense. */
  canRetry: boolean;
  requestId: string | null;
}

export function describeError(err: unknown, maxChars: number): UiError {
  if (!(err instanceof ApiError)) {
    return { kind: 'unknown', title: 'Something went wrong', detail: 'An unexpected error occurred in the web app.', retryAfter: null, canRetry: true, requestId: null };
  }
  const base = { kind: err.kind, retryAfter: err.retryAfter, requestId: err.requestId };
  switch (err.kind) {
    case 'unauthorized':
      return {
        ...base,
        title: 'The assistant refused this web app’s credentials',
        detail:
          'The service key configured on the web server was rejected by the API (HTTP 401). This is a server configuration problem and retrying will not help until an administrator fixes it (BFF_RAG_API_KEY).',
        canRetry: true,
      };
    case 'rate_limited':
      return {
        ...base,
        title: 'Rate limit reached',
        detail: 'Too many requests were sent in a short time. Sending is paused until the countdown ends.',
        canRetry: false,
      };
    case 'session_busy':
      return {
        ...base,
        title: 'This conversation is still busy',
        detail: 'The assistant is still working on an earlier message in this conversation. Try again in a moment.',
        canRetry: true,
      };
    case 'too_large':
      return {
        ...base,
        title: 'Message too long',
        detail: `Messages can be up to ${String(maxChars)} characters. Your text has been put back in the box so you can shorten it.`,
        canRetry: false,
      };
    case 'unavailable':
      return {
        ...base,
        title: 'The assistant is temporarily unavailable',
        detail:
          err.status === 504
            ? 'The assistant took too long to answer. It may be under heavy load.'
            : 'The backend (or one of its dependencies) is not accepting requests right now.',
        canRetry: true,
      };
    case 'network':
      return {
        ...base,
        title: err.code === 'client_timeout' ? 'The request timed out' : 'Cannot reach the server',
        detail:
          err.code === 'client_timeout'
            ? 'No answer arrived in time. The message may or may not have been processed.'
            : 'Check your internet connection. If you are online, the web server itself may be down.',
        canRetry: true,
      };
    case 'invalid':
      return { ...base, title: 'The request was rejected', detail: err.message, canRetry: false };
    case 'forbidden':
      return { ...base, title: 'Request refused', detail: err.message, canRetry: false };
    default:
      return { ...base, title: 'Unexpected error', detail: err.message || 'The server returned an unexpected response.', canRetry: true };
  }
}
