// @ts-check
// Brings up the whole stack for the Playwright smoke test and holds it open:
//
//   RAG API (APP_ENV=loadtest, fake LLM/embedder/guard)  ->  BFF (real server/app.mjs)  ->  built SPA (dist/)
//
// Playwright starts this via `webServer` in playwright.config.ts and waits on /api/health.
//
// Why a launcher rather than two `webServer` entries: the BFF needs an API key that does not exist until the
// API is up, so the key is minted here (app.auth.cli) and handed over in-process. Nothing is read from a
// developer's .env, and the key never leaves this process tree - which is the property the BFF exists to have.
//
// Postgres, Redis and Qdrant must already be running (_infra/up_live.sh). APP_ENV=loadtest fakes only the
// model calls, so no provider is billed by this test; the datastores are real.
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

import { createBff, defaultLogger } from '../server/app.mjs';
import { loadConfig } from '../server/config.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB = path.join(HERE, '..');
const ROOT = path.join(WEB, '..');
const PYTHON = path.join(ROOT, '.venv', 'Scripts', process.platform === 'win32' ? 'python.exe' : 'python');
const LOG_DIR = path.join(HERE, '.logs');

const API_PORT = Number(process.env['E2E_API_PORT'] ?? 8097);
const BFF_PORT = Number(process.env['E2E_BFF_PORT'] ?? 8788);
const COLLECTION = process.env['E2E_COLLECTION'] ?? 'loadtest_rag';

/** @param {number} port */
function portOpen(port) {
  return new Promise((resolve) => {
    const s = net.connect({ host: '127.0.0.1', port });
    const done = (/** @type {boolean} */ v) => {
      s.destroy();
      resolve(v);
    };
    s.setTimeout(1000);
    s.once('connect', () => done(true));
    s.once('timeout', () => done(false));
    s.once('error', () => done(false));
  });
}

/** @param {number} port @param {number} timeoutMs */
async function waitPort(port, timeoutMs) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    if (await portOpen(port)) return true;
    await new Promise((r) => setTimeout(r, 300));
  }
  return false;
}

/** Kill a process AND its children. `child.kill()` on Windows hits only the .venv launcher, which re-execs
 *  the real interpreter as a child - so the API would survive and keep the port. Targets a pid, never an
 *  image name. */
function killTree(/** @type {import('node:child_process').ChildProcess} */ child) {
  if (child.pid === undefined || child.exitCode !== null) return;
  if (process.platform === 'win32') spawn('taskkill', ['/F', '/T', '/PID', String(child.pid)], { stdio: 'ignore' });
  else child.kill('SIGTERM');
}

/** @param {Record<string, string>} env */
async function mintKey(env) {
  const args = ['-m', 'app.auth.cli', 'create', '--tenant', `e2e${Date.now().toString(36)}`, '--user', 'playwright'];
  const p = spawn(PYTHON, args, { cwd: ROOT, env, stdio: ['ignore', 'pipe', 'pipe'] });
  let out = '';
  let err = '';
  p.stdout.setEncoding('utf8');
  p.stderr.setEncoding('utf8');
  p.stdout.on('data', (c) => (out += c));
  p.stderr.on('data', (c) => (err += c));
  const [code] = await once(p, 'exit');
  if (code !== 0) throw new Error(`could not mint an API key (exit ${String(code)}): ${err.trim().slice(0, 300)}`);
  const key = out.split(/\s+/).find((w) => w.startsWith('rag_'));
  if (key === undefined) throw new Error(`no API key in the CLI output: ${out.trim().slice(0, 300)}`);
  return key;
}

async function main() {
  fs.mkdirSync(LOG_DIR, { recursive: true });
  if (!fs.existsSync(path.join(WEB, 'dist', 'index.html'))) {
    throw new Error('web/dist is missing - run `npm run build` before the e2e tests (playwright.config.ts does this for you)');
  }
  for (const [name, port] of [['API', API_PORT], ['BFF', BFF_PORT]]) {
    if (await portOpen(Number(port))) throw new Error(`${String(name)} port ${String(port)} is already in use; stop that process or set E2E_${String(name)}_PORT`);
  }

  /** @type {Record<string, string>} */
  const apiEnv = {
    .../** @type {Record<string, string>} */ (process.env),
    APP_ENV: 'loadtest',
    QDRANT_COLLECTION: COLLECTION,
    METRICS_PORT: '0',
    HOST: '127.0.0.1',
    PORT: String(API_PORT),
    UVICORN_LOG_LEVEL: 'warning',
  };

  // `python -m app.main`, not `uvicorn app.main:app`: on Windows the latter builds a ProactorEventLoop that
  // psycopg's pool cannot use, and the app would serve with every Postgres call failing (see DOCS/15).
  const apiLog = fs.openSync(path.join(LOG_DIR, 'api.log'), 'w');
  const api = spawn(PYTHON, ['-m', 'app.main'], { cwd: ROOT, env: apiEnv, stdio: ['ignore', apiLog, apiLog] });
  api.once('exit', (code) => {
    if (code !== 0 && code !== null) defaultLogger({ level: 'fatal', msg: 'the RAG API exited', code, log: path.join(LOG_DIR, 'api.log') });
  });

  /** @type {ReturnType<typeof createBff> | null} */
  let bff = null;
  const shutdown = () => {
    killTree(api);
    if (bff) void bff.close();
  };
  process.on('SIGINT', () => { shutdown(); process.exit(0); });
  process.on('SIGTERM', () => { shutdown(); process.exit(0); });
  process.on('exit', shutdown);

  try {
    if (!(await waitPort(API_PORT, 180_000))) throw new Error(`the RAG API never opened port ${String(API_PORT)}; see ${path.join(LOG_DIR, 'api.log')}`);
    const apiKey = await mintKey(apiEnv);

    const config = loadConfig({
      BFF_RAG_API_KEY: apiKey,
      RAG_API_URL: `http://127.0.0.1:${String(API_PORT)}`,
      BFF_HOST: '127.0.0.1',
      BFF_PORT: String(BFF_PORT),
      BFF_STATIC_DIR: path.join(WEB, 'dist'),
      // Generous, so a 429 in the smoke test means the test asked for one rather than tripping the BFF's own
      // limiter while clicking through the UI.
      BFF_RATE_LIMIT_PER_MIN: '600',
      BFF_HEALTH_RATE_LIMIT_PER_MIN: '600',
    });
    bff = createBff(config);
    const { port, host } = await bff.listen();
    defaultLogger({ level: 'info', msg: 'e2e stack up', bff: `http://${host}:${String(port)}`, api: `http://127.0.0.1:${String(API_PORT)}`, collection: COLLECTION });
  } catch (e) {
    defaultLogger({ level: 'fatal', msg: 'e2e stack failed to start', reason: e instanceof Error ? e.message : String(e) });
    shutdown();
    process.exit(1);
  }
}

await main();
