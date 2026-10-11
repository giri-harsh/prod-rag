import process from 'node:process';

import { defineConfig, devices } from '@playwright/test';

// The smoke test runs against the REAL stack (built SPA -> real BFF -> real RAG API with APP_ENV=loadtest
// fakes for the model calls only). Postgres, Redis and Qdrant must be up; see e2e/stack.mjs.
const BFF_PORT = Number(process.env['E2E_BFF_PORT'] ?? 8788);
const baseURL = `http://127.0.0.1:${String(BFF_PORT)}`;

export default defineConfig({
  testDir: './e2e',
  testMatch: /.*\.spec\.ts$/,
  // One shared stack with one set of datastores, and the API serialises per conversation (409 session_busy),
  // so parallel workers would fight each other rather than find bugs.
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env['CI']),
  retries: 0,
  reporter: [['list']],
  timeout: 90_000,
  // The fake LLM sleeps ~800 ms and a cold first request pays NeMo/Qdrant warm-up.
  expect: { timeout: 20_000 },
  use: { baseURL, trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    // The BFF serves dist/, so the SPA has to be built before the stack starts.
    command: 'npm run build && node e2e/stack.mjs',
    url: `${baseURL}/api/health`,
    reuseExistingServer: !process.env['CI'],
    timeout: 300_000,
    stdout: 'pipe',
    stderr: 'pipe',
  },
});
