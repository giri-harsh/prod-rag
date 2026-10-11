# 17 - Production web client (`./web`)

What this covers: the React + TypeScript SPA in [`web/`](../web), the Node BFF that serves it, and the
security contract between them. This is the primary UI; the Streamlit app remains as a demo only.

Getting it running, the script list and the test layout are in [`web/README.md`](../web/README.md). This
document is about *why* the BFF exists and what it guarantees.

## 1. The problem: the API key cannot be in the browser

The RAG API authenticates with a static `X-API-Key` that carries a tenant, a user and rate limits. Anything
a browser holds is readable by the person using it, and a key handed to the browser is a key handed to the
visitor - who can then spend the tenant's quota directly against the API, bypassing the UI entirely. There is
no "public" variant of the key to fall back on.

So the browser never receives one. A small Node server (the **BFF**, backend-for-frontend) holds the key, and
the SPA talks only to that server, same-origin:

```
browser  ──POST /api/query──▶  BFF (node, holds BFF_RAG_API_KEY)  ──POST /query + X-API-Key──▶  RAG API
         ◀──answer JSON─────                                       ◀──answer JSON──────────────
```

The SPA has no notion of a key, no field to enter one and no code path that sends one. The property is
asserted in a real browser by the Playwright smoke test, against every request header, every response header,
the rendered DOM, `localStorage`, `sessionStorage` and cookies - not inferred from the architecture.

## 2. What the BFF does beyond proxying

It is deliberately not a transparent proxy. `server/app.mjs` exposes exactly two API routes plus the static
SPA, and everything else is refused.

| route | behaviour |
|---|---|
| `POST /api/query` | the only way to reach the model. Body is parsed, size-capped and **key-filtered** |
| `GET /api/health` | cached, rate-limited readiness summary (`{ok, bff, upstream}`) |
| everything else | static files from `dist/`, or 404/405 |

**The request body is rebuilt, not forwarded.** `sanitizeQueryBody` accepts only `q` and `session_id`
(`ALLOWED_BODY_KEYS`); any other field a crafted request sends is dropped. This matters because the API's
`QueryRequest` has `extra="allow"` and understands fields the UI must not be able to set - `tenant_id` (a
cross-tenant assertion) and `include_contexts` (raw chunk text, gated on the `eval` scope). Forwarding the
body verbatim would let a browser request both.

**The session id is derived server-side.** The thread key sent upstream is `<browserId>-<clientSessionId>`,
where `browserId` comes from an `HttpOnly` cookie the BFF issues and the client half must match
`[A-Za-z0-9_-]{1,40}`. A browser therefore cannot read or forge another visitor's conversation id: picking a
`session_id` only ever addresses threads under its own cookie. The tenant and user parts of the thread key
are added by the API from the key itself.

**Failures are translated, not leaked.** `mapUpstreamFailure` maps each upstream status onto a stable
`{error, code, request_id?, retry_after?}` shape; upstream error text is passed through only where it is safe
and useful, and the API's `request_id` is preserved so a user-visible reference matches the server logs:

| upstream | BFF | code | retry hint |
|---|---|---|---|
| 400 / 422 | same | `bad_request` / `invalid_request` | - |
| 401 | 401 | `upstream_unauthorized` | none - the *service* key was rejected, so retrying cannot help |
| 403 | 403 | `forbidden` | - |
| 409 | 409 | `session_busy` | `Retry-After` or 1 s |
| 413 | 413 | `payload_too_large` | - |
| 429 | 429 | `rate_limited` | `Retry-After` or 30 s |
| 503 | 503 | `unavailable` | `Retry-After` or 10 s |
| anything else | **502** | `upstream_error` | - |
| transport failure / timeout | 503 / 504 | - | yes |

Note the default: an unexpected upstream status becomes a 502 with a generic message rather than being
forwarded. The browser never sees an upstream status the UI has not been designed to handle.

**Its own limits.** A per-IP token-bucket limiter (`BFF_RATE_LIMIT_PER_MIN`, default 30) sits in front of
`/api/query`, so one visitor cannot drain the tenant's API quota through the UI. `X-Forwarded-For` is only
trusted from `BFF_TRUSTED_PROXIES`; otherwise the peer address is used, because an untrusted header would let
a client choose its own limiter bucket.

**Security headers** are set on every response (`SECURITY_HEADERS`): a CSP of `default-src 'self'` with
`object-src 'none'`, `base-uri 'none'` and `frame-ancestors 'none'`, plus `nosniff`, `no-referrer`,
`X-Frame-Options: DENY`, COOP/CORP `same-origin` and a `Permissions-Policy` that denies camera, microphone,
geolocation, payment and USB. HSTS is added only when `BFF_COOKIE_SECURE` is on (i.e. when actually served
over HTTPS). The CSP forbids inline script, which is why the pre-paint theme applier is a served file
(`public/theme-init.js`) rather than an inline snippet.

## 3. Configuration

`BFF_RAG_API_KEY` is the only required variable; startup fails with a message that names the variable and
never prints its value. Validation lives in `server/config.mjs`.

| variable | default | notes |
|---|---|---|
| `BFF_RAG_API_KEY` | **required** | the `X-API-Key` sent upstream. Rejected if it contains characters invalid in an HTTP header |
| `RAG_API_URL` | `http://127.0.0.1:8080` | must be http/https, with no credentials, query string or fragment |
| `BFF_HOST` / `BFF_PORT` | `127.0.0.1` / `8787` | `PORT` is honoured too (Cloud Run / Heroku convention) |
| `BFF_STATIC_DIR` | `../dist` | the built SPA |
| `BFF_MAX_BODY_BYTES` | 32768 | request body cap |
| `BFF_MAX_QUERY_CHARS` | 4000 | matches the API's `max_query_chars` |
| `BFF_RATE_LIMIT_PER_MIN` | 30 | per client IP, for `/api/query` |
| `BFF_HEALTH_RATE_LIMIT_PER_MIN` | 120 | per client IP, for `/api/health` |
| `BFF_UPSTREAM_TIMEOUT_MS` | 65000 | above the API's own request budget, so the API's 503 wins over a BFF timeout |
| `BFF_COOKIE_SECURE` | off | set to 1 when served over HTTPS; also enables HSTS |
| `BFF_ALLOWED_ORIGINS` | empty | extra origins allowed to post; same-origin always is |
| `BFF_TRUSTED_PROXIES` | empty | CIDRs whose `X-Forwarded-For` is believed |

Startup warns (and keeps running) when the combination is risky: a plain-http non-loopback `RAG_API_URL`
(the key would cross the network unencrypted), `NODE_ENV=production` without `BFF_COOKIE_SECURE`, or binding
a non-loopback address with no trusted-proxy configuration.

## 4. The SPA

Vite + React 19 + TypeScript, ~244 kB of JS (76 kB gzipped), no UI framework and no state library - the state
is two reducers (`src/state/chat.ts`, `src/state/sessions.ts`).

* **Chat** with the user message, the answer, and badges for `blocked` / `cache_hit` / iteration count.
* **Sources panel** showing retrieved sources and the `thought_process` steps the API returns.
* **Session list** in `localStorage`; the server only ever sees the derived thread id. Tampered or
  unreadable stored state degrades to a single fresh conversation rather than throwing.
* **Answers are rendered as plain text** (`pre-wrap`). Nothing from the model is ever interpreted as HTML, so
  a prompt-injected `<img onerror=...>` in retrieved content cannot execute. Combined with the CSP, that is
  two independent barriers.
* **Error states** for 401, 403, 409, 413, 429, 503, malformed responses and transport failures, each with
  its own wording and the right affordance: 429 is a hard stop with a countdown, 503 offers an immediate
  retry, and 401 offers **no** retry because the rejected credential is the server's, not the user's.
* Light/dark/system theme, applied before first paint to avoid a flash.
* Accessibility: the message list is a `log` region, errors are an `alert`, the retry countdown is announced
  every 10 s rather than every second, and every control is reachable and labelled.

## 5. Honesty note

What is verified: `npm run lint` (type-aware, zero warnings), `npm run typecheck` across all four TS
projects, 88 vitest unit tests, a production build, and 6 Playwright tests against the real stack with fake
model calls.

What is **not**: no visual regression tests, no cross-browser run (Chromium only), no load test of the BFF
itself, and no deployment of this client anywhere - there is no Dockerfile for `web/` yet, so shipping it
means `npm run build` plus `node server/index.mjs` behind TLS. The BFF is single-process with an in-memory
rate limiter, so running several replicas gives each its own bucket; that is fine for the default 30/min
per IP but is not a shared quota.
