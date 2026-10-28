// @ts-check
// The explain-this-answer trace, as the BROWSER may see it. The API produces the trace for the key's own tenant, but the
// BFF still treats it as untrusted input: only allowlisted steps and fields survive, every value is coerced to the expected
// type and length, and every string is scrubbed of anything that looks like a secret, an internal address, a stack trace or
// a fragment of our own prompts. A new field in the API therefore stays invisible until it is added here on purpose.

const MAX_STEPS = 24;
const MAX_ROWS = 8;

/** Fragments that only occur in OUR prompts / templates: a string containing one is replaced wholesale. */
const PROMPT_MARKERS = [
  '<retrieved_context',
  '</retrieved_context',
  'untrusted DATA',
  '[ASSISTANT DOMAIN:',
  'ORIGINAL QUESTION:',
  'PREVIOUS QUERY (retrieved poorly)',
  'CONVERSATION HISTORY:',
  'LATEST MESSAGE:',
  'You are a query planner',
  'Should the message be blocked',
];

/** [pattern, replacement] applied in order to every string. */
const SCRUBBERS = /** @type {[RegExp, string][]} */ ([
  [/\b(?:gsk_|sk-|rag_|lsv2_|pylf_|ghp_|xox[abp]-)[A-Za-z0-9_-]{8,}/g, '[removed]'],
  [/\bAIza[0-9A-Za-z_-]{20,}/g, '[removed]'],
  [/\bAKIA[0-9A-Z]{16}\b/g, '[removed]'],
  [/\bbearer\s+[A-Za-z0-9._~+/=-]{12,}/gi, '[removed]'],
  [/-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)/g, '[removed]'],
  [/\b(?:postgres(?:ql)?|redis|rediss|amqp):\/\/\S+/gi, '[removed]'],
  [/\bhttps?:\/\/(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|(?:api|web|postgres|pgbouncer|redis|qdrant|caddy|worker)\b)[^\s)"']*/gi, '[removed]'],
  [/\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0|(?:api|web|postgres|pgbouncer|redis|qdrant|caddy|worker)):\d{2,5}\b/gi, '[removed]'],
  [/\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b/g, '[removed]'],
  [/\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:internal|local|cluster\.local|svc)\b/gi, '[removed]'],
  [/Traceback \(most recent call last\):[\s\S]*/g, '[removed]'],
  [/\bFile "[^"]+", line \d+[\s\S]*/g, '[removed]'],
  [/(?:^|\s)(?:\/app|\/opt|\/usr\/local|\/home\/[a-z]+|[A-Z]:\\(?:Users|Work))[\w/\\.-]*/g, ' [removed]'],
]);

/**
 * A string safe to show: type-checked, scrubbed, whitespace-normalised and truncated.
 * @param {unknown} v @param {number} max @returns {string}
 */
export function cleanText(v, max) {
  if (typeof v !== 'string') return '';
  if (PROMPT_MARKERS.some((m) => v.includes(m))) return '[removed]';
  let out = v;
  for (const [rx, rep] of SCRUBBERS) out = out.replace(rx, rep);
  return out.replace(/\s+/g, ' ').trim().slice(0, max);
}

/** @param {unknown} v @param {number} max @returns {string | null} */
function optText(v, max) {
  const s = cleanText(v, max);
  return typeof v === 'string' && s !== '' ? s : null;
}

/** @param {unknown} v @returns {number} a non-negative integer (ms, counts), 0 when not a finite number */
function int(v) {
  return typeof v === 'number' && Number.isFinite(v) ? Math.max(0, Math.min(Math.round(v), 10_000_000)) : 0;
}

/** @param {unknown} v @returns {number | null} */
function num(v) {
  return typeof v === 'number' && Number.isFinite(v) ? Math.round(v * 10_000) / 10_000 : null;
}

/** @param {unknown} v @returns {boolean} */
function bool(v) {
  return v === true;
}

/** @param {unknown} v @returns {Record<string, unknown>} */
function rec(v) {
  return typeof v === 'object' && v !== null && !Array.isArray(v) ? /** @type {Record<string, unknown>} */ (v) : {};
}

/** @template T @param {unknown} v @param {(x: Record<string, unknown>) => T} f @returns {T[]} */
function rows(v, f) {
  return Array.isArray(v) ? v.slice(0, MAX_ROWS).map((x) => f(rec(x))) : [];
}

/** @param {Record<string, unknown>} x */
const hitRow = (x) => ({ source: cleanText(x['source'], 120), score: num(x['score']), snippet: cleanText(x['snippet'], 120) });

/** @type {Record<string, (s: Record<string, unknown>) => Record<string, unknown>>} */
const STEPS = {
  guardrail: (s) => ({
    blocked: bool(s['blocked']),
    rail: optText(s['rail'], 60),
    layer: cleanText(s['layer'], 80),
    ms: int(s['ms']),
    llm_calls: int(s['llm_calls']),
  }),
  telemetry_redaction: (s) => {
    /** @type {Record<string, number>} */
    const labels = {};
    for (const [k, v] of Object.entries(rec(s['labels'])).slice(0, 12)) {
      if (/^[A-Z_]{1,12}$/.test(k)) labels[k] = int(v);
    }
    return { applied: bool(s['applied']), labels, logged_as: cleanText(s['logged_as'], 200) };
  },
  plan: (s) => ({
    intent: s['intent'] === 'conversational' ? 'conversational' : 'search',
    original: cleanText(s['original'], 300),
    rewritten: optText(s['rewritten'], 300),
    standalone: bool(s['standalone']),
    has_history: bool(s['has_history']),
    cache_hit: bool(s['cache_hit']),
    degraded: bool(s['degraded']),
    ms: int(s['ms']),
    llm_calls: int(s['llm_calls']),
  }),
  semantic_cache: (s) => ({ hit: bool(s['hit']), skipped: bool(s['skipped']), ms: int(s['ms']) }),
  retrieval: (s) => ({
    iteration: int(s['iteration']),
    query: cleanText(s['query'], 300),
    embed_ms: int(s['embed_ms']),
    search_ms: int(s['search_ms']),
    rerank_ms: int(s['rerank_ms']),
    ms: int(s['ms']),
    kept: int(s['kept']),
    dropped: int(s['dropped']),
    neutralized: int(s['neutralized']),
    dense: rows(s['dense'], hitRow),
    bm25: rows(s['bm25'], hitRow),
    fused: rows(s['fused'], hitRow),
    reranked: rows(s['reranked'], (x) => ({ ...hitRow(x), fused_rank: x['fused_rank'] === null ? null : int(x['fused_rank']) || null })),
    chunks: rows(s['chunks'], (x) => ({
      source: cleanText(x['source'], 120),
      score: num(x['score']),
      text: cleanText(x['text'], 900),
      page: x['page'] === null || x['page'] === undefined ? null : int(x['page']),
      heading: cleanText(x['heading'], 200),
    })),
  }),
  grade: (s) => ({
    iteration: int(s['iteration']),
    score: num(s['score']),
    threshold: num(s['threshold']),
    verdict: s['verdict'] === 'good' ? 'good' : 'poor',
    next: cleanText(s['next'], 80),
  }),
  rewrite: (s) => ({
    previous: cleanText(s['previous'], 300),
    new: cleanText(s['new'], 300),
    model: optText(s['model'], 80),
    ms: int(s['ms']),
    llm_calls: int(s['llm_calls']),
  }),
  decline: (s) => ({ reason: cleanText(s['reason'], 120) }),
  llm: (s) => ({
    model: optText(s['model'], 80),
    fallback: bool(s['fallback']),
    cache_hit: bool(s['cache_hit']),
    tokens_in: int(s['tokens_in']),
    tokens_out: int(s['tokens_out']),
    ms: int(s['ms']),
    llm_calls: int(s['llm_calls']),
    grounded: bool(s['grounded']),
    context_chunks: int(s['context_chunks']),
  }),
  output_guard: (s) => ({
    blocked: bool(s['blocked']),
    rail: optText(s['rail'], 60),
    ms: int(s['ms']),
    stored_in_cache: bool(s['stored_in_cache']),
    llm_calls: int(s['llm_calls']),
  }),
};

/**
 * @param {unknown} raw the API's `trace` object
 * @returns {{ steps: Record<string, unknown>[], total_ms: number, llm_calls: number } | null} null when there is nothing usable
 */
export function sanitizeTrace(raw) {
  const t = rec(raw);
  if (!Array.isArray(t['steps'])) return null;
  /** @type {Record<string, unknown>[]} */
  const steps = [];
  for (const item of t['steps'].slice(0, MAX_STEPS)) {
    const s = rec(item);
    const name = typeof s['step'] === 'string' ? s['step'] : '';
    const pick = Object.hasOwn(STEPS, name) ? STEPS[name] : undefined;
    if (pick) steps.push({ step: name, ...pick(s) });
  }
  if (steps.length === 0) return null;
  return { steps, total_ms: int(t['total_ms']), llm_calls: int(t['llm_calls']) };
}
