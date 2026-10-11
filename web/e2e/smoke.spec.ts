import { expect, test } from '@playwright/test';

/**
 * Playwright smoke test: the built SPA, the real BFF and the real RAG API (APP_ENV=loadtest, so the model
 * calls are fakes and no provider is billed - the datastores are real). See e2e/stack.mjs.
 *
 * The BFF's own behaviour is covered exhaustively by the vitest suite in test/server. What can only be
 * checked here is the part that involves a browser: that the SPA actually renders an answer end to end, and
 * that the API key stays server-side - the single security property the BFF exists to provide.
 */

const QUESTION = 'What is a CronJob and how is it scheduled?';

test('loads, reports the backend ready, and answers a question end to end', async ({ page }) => {
  await page.goto('/');

  await expect(page.getByTestId('health')).toHaveText('Backend ready');
  await expect(page.getByTestId('empty-state')).toBeVisible();

  await page.locator('#composer').fill(QUESTION);
  await page.getByRole('button', { name: 'Send' }).click();

  // .msg-text, not the whole article: each message carries a visually-hidden "You"/"Assistant" heading.
  await expect(page.getByTestId('message-user').locator('.msg-text')).toHaveText(QUESTION);
  const answer = page.getByTestId('message-assistant');
  await expect(answer).toBeVisible();
  await expect(answer).not.toBeEmpty();
  // The pending indicator must clear - otherwise the UI would sit on "Sending…" forever on a slow answer.
  await expect(page.getByTestId('pending')).toBeHidden();
  await expect(page.getByTestId('error-banner')).toHaveCount(0);
});

test('the API key never reaches the browser', async ({ page }) => {
  // Everything the page receives, plus every header it sends. The BFF adds X-API-Key upstream; if any of it
  // leaked into a response body, a response header, or back out of the browser, this fails.
  const leaks: string[] = [];
  page.on('request', (req) => {
    const headers = req.headers();
    for (const name of Object.keys(headers)) {
      if (name.toLowerCase() === 'x-api-key') leaks.push(`request ${req.url()} sent an x-api-key header`);
    }
  });
  page.on('response', (res) => {
    for (const [name, value] of Object.entries(res.headers())) {
      if (name.toLowerCase() === 'x-api-key' || /\brag_[A-Za-z0-9_-]{8,}/.test(value)) {
        leaks.push(`response ${res.url()} header ${name} looks like the service key`);
      }
    }
  });

  await page.goto('/');
  await page.locator('#composer').fill(QUESTION);
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('message-assistant')).toBeVisible();

  // The key format is rag_<token> (app/auth/cli.py). Check the rendered DOM and every bit of browser-side
  // storage the app can write, not just the network.
  const exposure = await page.evaluate(() => {
    const pattern = /\brag_[A-Za-z0-9_-]{8,}/;
    const hits: string[] = [];
    if (pattern.test(document.documentElement.innerHTML)) hits.push('document');
    for (const store of [localStorage, sessionStorage]) {
      for (let i = 0; i < store.length; i++) {
        const k = store.key(i);
        if (k !== null && pattern.test(`${k}${store.getItem(k) ?? ''}`)) hits.push(`storage:${k}`);
      }
    }
    if (pattern.test(document.cookie)) hits.push('cookie');
    return hits;
  });

  expect(leaks, leaks.join('; ')).toEqual([]);
  expect(exposure, `the service key appeared in: ${exposure.join(', ')}`).toEqual([]);
});

// 401, 429 and 503 are what the spec asks the client to handle. They are injected at the network boundary
// rather than provoked in the API: the point here is that the UI renders the right recovery affordance for
// each, and the BFF's own mapping of upstream statuses is already covered by test/server/bff.test.ts.
interface Failure {
  name: string;
  status: number;
  body: { error: string; code?: string; retry_after?: number; request_id: string };
  kind: string;
  /** Label of the retry button the banner must offer, or null if the user must not be offered one. */
  retry: string | null;
}

const FAILURES: Failure[] = [
  {
    name: '401',
    status: 401,
    body: { error: 'The service is not configured correctly.', code: 'bff_unauthorized', request_id: 'rid-401' },
    kind: 'unauthorized',
    // An operator problem, not the user's: offering a retry would just fail again.
    retry: null,
  },
  { name: '429', status: 429, body: { error: 'Too many requests.', code: 'bff_rate_limited', retry_after: 30, request_id: 'rid-429' }, kind: 'rate_limited', retry: null },
  { name: '503', status: 503, body: { error: 'Service temporarily unavailable', retry_after: 5, request_id: 'rid-503' }, kind: 'unavailable', retry: 'Retry now' },
];

for (const f of FAILURES) {
  test(`shows a recoverable error state for ${f.name}`, async ({ page }) => {
    await page.route('**/api/query', (route) =>
      route.fulfill({
        status: f.status,
        headers: f.body.retry_after === undefined ? {} : { 'retry-after': String(f.body.retry_after) },
        contentType: 'application/json',
        body: JSON.stringify(f.body),
      }),
    );

    await page.goto('/');
    await page.locator('#composer').fill(QUESTION);
    await page.getByRole('button', { name: 'Send' }).click();

    const banner = page.getByTestId('error-banner');
    await expect(banner).toBeVisible();
    await expect(banner).toHaveAttribute('data-kind', f.kind);
    await expect(banner).toContainText(f.body.request_id); // the reference an operator needs
    await expect(page.getByTestId('message-assistant')).toHaveCount(0); // no fabricated answer
    await expect(page.getByTestId('pending')).toBeHidden();

    if (f.retry !== null) {
      await expect(banner.getByRole('button', { name: f.retry })).toBeVisible();
    } else {
      // 429 is a hard stop until the countdown expires; 401 cannot be retried by the user at all.
      await expect(banner.getByRole('button', { name: /Send again|Try again|Retry now/ })).toHaveCount(0);
    }
    if (f.body.retry_after !== undefined) await expect(page.getByTestId('countdown')).toBeVisible();

    await banner.getByRole('button', { name: 'Dismiss' }).click();
    await expect(banner).toHaveCount(0);
  });
}

test('a new conversation starts empty and does not inherit the previous answer', async ({ page }) => {
  await page.goto('/');
  await page.locator('#composer').fill(QUESTION);
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('message-assistant')).toBeVisible();

  await page.getByTestId('new-chat').click();

  await expect(page.getByTestId('empty-state')).toBeVisible();
  await expect(page.getByTestId('message-user')).toHaveCount(0);
  await expect(page.getByTestId('message-assistant')).toHaveCount(0);
  // Two conversations now exist, so the earlier one is still reachable rather than replaced.
  await expect(page.getByRole('navigation', { name: 'Conversations' }).getByRole('button')).not.toHaveCount(1);
});
