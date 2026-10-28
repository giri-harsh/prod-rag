// @ts-check
// Headline numbers for the landing page. The file is produced by scripts/build_benchmarks.py from the repository's own result
// files (it is never edited by hand and the UI has no numbers of its own); this module only validates and serves it.
import { readFileSync } from 'node:fs';
import { cleanText } from './trace.mjs';

const DEFAULT_FILE = new URL('./benchmarks.json', import.meta.url);

/**
 * @param {string | URL} [file]
 * @returns {{ generated_at: string, metrics: { id: string, label: string, value: string, sub: string, env: string, source: string }[] }}
 */
export function loadBenchmarks(file = DEFAULT_FILE) {
  /** @type {unknown} */
  let raw;
  try {
    raw = JSON.parse(readFileSync(file, 'utf8'));
  } catch {
    return { generated_at: '', metrics: [] };
  }
  const o = typeof raw === 'object' && raw !== null ? /** @type {Record<string, unknown>} */ (raw) : {};
  const list = Array.isArray(o['metrics']) ? o['metrics'] : [];
  const metrics = list.slice(0, 8).flatMap((m) => {
    const r = typeof m === 'object' && m !== null ? /** @type {Record<string, unknown>} */ (m) : {};
    const id = cleanText(r['id'], 40);
    const value = cleanText(r['value'], 40);
    if (id === '' || value === '') return [];
    return [{ id, label: cleanText(r['label'], 80), value, sub: cleanText(r['sub'], 160), env: cleanText(r['env'], 160), source: cleanText(r['source'], 120) }];
  });
  return { generated_at: cleanText(o['generated_at'], 40), metrics };
}
