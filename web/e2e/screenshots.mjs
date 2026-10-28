// Takes the README screenshots: node e2e/screenshots.mjs <base-url> <out-dir>
// Uses the Edge/Chrome already installed (no browser download). Recorded scenarios only, so it spends no model quota.
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { chromium } from '@playwright/test';

const [base = 'http://127.0.0.1:8787', out = '../docs/screenshots'] = process.argv.slice(2);
mkdirSync(out, { recursive: true });

const browser = await chromium.launch({ channel: process.env.PW_CHANNEL ?? 'msedge' });
/** @param {import('@playwright/test').Page} page @param {string} name */
const shot = (page, name) => page.screenshot({ path: path.join(out, name) });

/** @param {string} label @param {{ width: number, height: number }} viewport @param {boolean} isMobile */
async function run(label, viewport, isMobile) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: isMobile ? 2 : 1, isMobile, hasTouch: isMobile, colorScheme: 'light' });
  const page = await ctx.newPage();
  await page.goto(base, { waitUntil: 'networkidle' });
  await page.getByTestId('metric-card').first().waitFor();
  await shot(page, `${label}-landing.png`);

  await page.getByTestId('chip-exact').click();
  await page.getByTestId('trace-panel').waitFor();
  await page.waitForTimeout(2600); // let the diagram finish lighting up
  await page.locator('.arch-section').screenshot({ path: path.join(out, `${label}-diagram-lit.png`) });
  await page.getByTestId('showcase').scrollIntoViewIfNeeded();
  await page.getByTestId('showcase').screenshot({ path: path.join(out, `${label}-trace.png`) });

  await page.getByTestId('chip-tenants').click();
  await page.getByTestId('showcase').waitFor();
  await page.getByTestId('showcase').screenshot({ path: path.join(out, `${label}-tenants.png`) });
  // the one live scenario: stopped by the pattern screen, so it spends no model quota
  await page.getByTestId('chip-jailbreak').click();
  await page.getByTestId('trace-summary').waitFor();
  await page.getByText('with zero AI calls').waitFor();
  await page.getByTestId('showcase').screenshot({ path: path.join(out, `${label}-live-jailbreak.png`) });
  await ctx.close();
}

await run('desktop', { width: 1280, height: 900 }, false);
await run('mobile', { width: 390, height: 844 }, true);
await browser.close();
console.log('screenshots written to', out);
