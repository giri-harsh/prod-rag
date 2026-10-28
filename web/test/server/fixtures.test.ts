import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { sanitizeTrace } from '../../server/trace.mjs';

interface Case {
  label: string | null;
  question: string;
  response: { answer: string; sources: string[]; blocked: boolean; iterations?: number; trace?: { steps: Record<string, unknown>[] } };
}
interface File {
  recorded_at: string;
  chips: Record<string, { layout: string; cases: Case[] }>;
}

const raw = readFileSync(new URL('../../public/demo/chips.json', import.meta.url), 'utf8');
const file = JSON.parse(raw) as File;

describe('the recorded scenarios (public/demo/chips.json)', () => {
  it('covers every recorded scenario on the page, with a trace on every case', () => {
    expect(Object.keys(file.chips).sort()).toEqual(['exact', 'followup', 'normal', 'offtopic', 'pii', 'tenants', 'unknown']);
    for (const [id, chip] of Object.entries(file.chips)) {
      expect(chip.cases.length, id).toBeGreaterThan(0);
      for (const c of chip.cases) expect(c.response.trace?.steps.length, `${id}: trace`).toBeGreaterThan(0);
    }
    expect(file.recorded_at).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  it('is already sanitised: running the BFF allowlist over it changes nothing', () => {
    for (const [id, chip] of Object.entries(file.chips)) {
      for (const c of chip.cases) {
        const again = sanitizeTrace(c.response.trace);
        expect(JSON.parse(JSON.stringify(again)), id).toEqual(c.response.trace);
      }
    }
  });

  it('contains no secrets, internal addresses, prompts or stack traces', () => {
    for (const bad of [/\brag_[A-Za-z0-9_-]{12,}/, /\bgsk_/, /\bAIza/, /postgresql:\/\//, /\b(?:qdrant|pgbouncer|postgres|redis):\d{2,5}/, /127\.0\.0\.1/, /Traceback/, /untrusted DATA/, /X-API-Key/i, /tenant[-_ ]?id/i]) {
      expect(raw, String(bad)).not.toMatch(bad);
    }
  });

  it('each scenario demonstrates what it claims to', () => {
    const steps = (id: string, i = 0) => (file.chips[id]?.cases[i]?.response.trace?.steps ?? []).map((s) => s.step);
    const step = (id: string, name: string, i = 0) => file.chips[id]?.cases[i]?.response.trace?.steps.find((s) => s.step === name);

    expect(steps('normal')).toEqual(expect.arrayContaining(['guardrail', 'plan', 'retrieval', 'grade', 'llm', 'output_guard']));
    expect(file.chips.normal?.cases[0]?.response.sources.length).toBeGreaterThan(0);

    // exact-term: the keyword search surfaced a document the meaning search did not
    const ret = step('exact', 'retrieval') as { dense: { source: string }[]; bm25: { source: string }[] };
    const dense = new Set(ret.dense.map((x) => x.source));
    expect(ret.bm25.some((x) => !dense.has(x.source))).toBe(true);

    // follow-up: turn 2 was rewritten into a standalone query using the history
    const plan = step('followup', 'plan', 1) as { has_history: boolean; rewritten: string; original: string };
    expect(plan.has_history).toBe(true);
    expect(plan.rewritten.toLowerCase()).toContain('cronjob');
    expect(plan.original).not.toBe(plan.rewritten);

    // not in the docs: retried, then declined without calling the answer model
    expect(steps('unknown')).toContain('rewrite');
    expect(steps('unknown')).toContain('decline');
    expect(steps('unknown')).not.toContain('llm');
    expect(file.chips.unknown?.cases[0]?.response.iterations).toBeGreaterThan(1);

    // off-topic: blocked by the guard, nothing retrieved
    expect(file.chips.offtopic?.cases[0]?.response.blocked).toBe(true);
    expect(steps('offtopic')).not.toContain('retrieval');

    // PII: the logs record a masked version
    const pii = step('pii', 'telemetry_redaction') as { applied: boolean; logged_as: string };
    expect(pii.applied).toBe(true);
    expect(pii.logged_as).toContain('[REDACTED:EMAIL]');
    expect(pii.logged_as).not.toContain('jane.doe@example.com');

    // tenant isolation: the same question, different documents
    const [a, b] = file.chips.tenants?.cases ?? [];
    expect(a?.question).toBe(b?.question);
    expect(a?.response.sources).toEqual([]);
    expect(b?.response.sources.length).toBeGreaterThan(0);
  });
});
