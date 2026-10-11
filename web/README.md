# rag-web

Production web client for the RAG API: a React + TypeScript SPA served by a small Node BFF that keeps the API
key server-side. Replaces Streamlit as the primary UI; the Streamlit app stays as a demo.

Design notes and the BFF's security contract: [../DOCS/17_WEB_CLIENT.md](../DOCS/17_WEB_CLIENT.md).

## Quick start

```bash
npm install
npm run build                 # the BFF serves dist/

# the API key is read from the environment and never sent to the browser
BFF_RAG_API_KEY=rag_xxx RAG_API_URL=http://127.0.0.1:8080 npm start
# -> http://127.0.0.1:8787
```

For UI work, `npm run dev` runs Vite on :5173 and proxies `/api` to the BFF, so the browser still never holds
a key. Start the BFF (`npm start`) alongside it.

## Scripts

| script | what it does |
|---|---|
| `npm run dev` | Vite dev server, `/api` proxied to the BFF |
| `npm run build` | type-aware production build into `dist/` |
| `npm run start` | run the BFF (serves `dist/` + `/api/*`) |
| `npm run lint` | ESLint, type-aware, `--max-warnings 0` |
| `npm run typecheck` | `tsc --noEmit` over all four TS projects (app, node/server, test, e2e) |
| `npm run test` | vitest: 88 unit tests, mostly the BFF's request handling |
| `npm run e2e` | Playwright smoke test against the real stack (see below) |
| `npm run e2e:install` | one-off: download the Chromium Playwright uses |

## Tests

**Unit (`npm run test`, 88 tests).** Almost all of the BFF: auth injection, session-cookie derivation, body
limits, origin checks, per-IP rate limiting, upstream status mapping, header and error redaction. No browser
and no network.

**Smoke (`npm run e2e`, 6 tests).** The real thing: built SPA -> real BFF -> real RAG API with
`APP_ENV=loadtest`, so the model calls are fakes and **no provider is billed**, but Postgres, Redis and Qdrant
are real and must be running (`_infra/up_live.sh`). `e2e/stack.mjs` starts the API, mints a fresh tenant key
through `app.auth.cli`, hands it to the BFF in-process and holds everything open; Playwright's `webServer`
waits on `/api/health`. Nothing is read from a developer's `.env`, and the key never leaves the process tree.

What the smoke test covers that the unit tests cannot:

* a question typed into the UI comes back as a rendered answer, end to end;
* **the API key never reaches the browser** - asserted against every request header, every response header,
  the rendered DOM, `localStorage`, `sessionStorage` and cookies. This is the one property the BFF exists to
  provide, so it is checked in a real browser rather than inferred;
* 401 / 429 / 503 each render the right recovery affordance (and 401/429 deliberately offer *no* retry
  button). These are injected with `page.route` - the BFF's own status mapping is already covered by the unit
  tests, so what is under test here is the UI's reaction;
* a new conversation starts empty and does not inherit the previous answer.

Ports default to 8097 (API) and 8788 (BFF); override with `E2E_API_PORT` / `E2E_BFF_PORT`. The launcher
refuses to start if either is already in use rather than measuring somebody else's process.

## Configuration

`BFF_RAG_API_KEY` is the only required variable. Everything else has a default; see
[../DOCS/17_WEB_CLIENT.md](../DOCS/17_WEB_CLIENT.md) for the full table and
`server/config.mjs` for the validation (it rejects a bad value at startup and never prints the key).

## TypeScript project layout

Four projects, not one: `tsconfig.json` (the browser app, DOM libs), `tsconfig.node.json` (the BFF and the
root config files), `test/tsconfig.json` and `e2e/tsconfig.json` (Node libs, plus DOM for `page.evaluate`
callbacks). The per-directory configs are not cosmetic: typescript-eslint resolves a file by walking **up** to
the nearest `tsconfig.json`, and without them every file outside `src/` was skipped as "not found by the
project service" - the type-aware rules looked like they passed while never running.
