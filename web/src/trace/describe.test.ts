import { describe, expect, it } from 'vitest';
import { parseTrace } from '../api/trace';
import { BLOCKED_RAW_TRACE, RAW_TRACE, REWRITE_RAW_TRACE, sample } from '../testing/sampleTrace';
import { NODE_IDS, STEP_INFO, branchContribution, nodeStates, rankMoves, summarize, waterfall } from './describe';

describe('parseTrace', () => {
  it('maps every step kind and rejects junk', () => {
    const t = sample();
    expect(t.steps.map((s) => s.kind)).toEqual(['guardrail', 'telemetry_redaction', 'plan', 'semantic_cache', 'retrieval', 'grade', 'llm', 'output_guard']);
    expect(t.totalMs).toBe(2700);
    for (const bad of [null, 'x', {}, { steps: [] }, { steps: [{ step: 'unknown' }] }]) expect(parseTrace(bad)).toBeNull();
  });
  it('coerces malformed numbers instead of rendering NaN', () => {
    const t = parseTrace({ steps: [{ step: 'llm', tokens_in: 'lots', ms: -5, model: 3 }], total_ms: 'x' });
    expect(t?.steps[0]).toMatchObject({ kind: 'llm', tokensIn: 0, ms: 0, model: null });
    expect(t?.totalMs).toBe(0);
  });
});

describe('every step has plain-English copy', () => {
  it('has a title and a "why this matters" for each kind', () => {
    for (const info of Object.values(STEP_INFO)) {
      expect(info.title.length).toBeGreaterThan(3);
      expect(info.why.length).toBeGreaterThan(40);
    }
    expect(Object.keys(STEP_INFO)).toHaveLength(10);
  });
});

describe('nodeStates (which diagram nodes light up)', () => {
  it('lights the whole happy path', () => {
    const s = nodeStates(sample());
    expect(s).toMatchObject({ guardrail: 'lit', plan: 'lit', cache: 'lit', retrieve: 'lit', rerank: 'lit', grade: 'lit', llm: 'lit', cite: 'lit', rewrite: 'idle' });
  });
  it('stops at the guardrail for a blocked request and lights nothing after it', () => {
    const s = nodeStates(sample(BLOCKED_RAW_TRACE));
    expect(s.guardrail).toBe('stop');
    for (const id of NODE_IDS.filter((n) => n !== 'guardrail')) expect(s[id]).toBe('idle');
  });
  it('shows the rewrite loop and treats a poor grade followed by a good one as a recovery', () => {
    const s = nodeStates(sample(REWRITE_RAW_TRACE));
    expect(s.rewrite).toBe('lit');
    expect(s.grade).toBe('lit');
  });
  it('marks the grader as the stopping point when the system declines', () => {
    const s = nodeStates(parseTrace({ steps: [{ step: 'guardrail', blocked: false }, { step: 'grade', verdict: 'poor' }, { step: 'decline', reason: 'poor retrieval' }] }));
    expect(s.grade).toBe('stop');
    expect(s.llm).toBe('idle');
  });
  it('is all idle with no trace', () => {
    expect(Object.values(nodeStates(null)).every((x) => x === 'idle')).toBe(true);
  });
});

describe('waterfall', () => {
  it('starts the guardrail and the planner together, then runs the rest in sequence', () => {
    const { rows, total } = waterfall(sample());
    const by = Object.fromEntries(rows.map((r) => [r.label, r]));
    expect(by.Guardrail).toMatchObject({ start: 0, ms: 310, parallel: true });
    expect(by['Plan + rewrite']).toMatchObject({ start: 0, ms: 420, parallel: true });
    expect(by['Semantic cache']?.start).toBe(420);
    expect(by['Retrieval #1']?.start).toBe(460);
    expect(by['Answer model']?.start).toBe(630);
    expect(by['Output check']?.start).toBe(1930);
    expect(total).toBe(2700);
    expect(rows.at(-1)?.label).toMatch(/^Other/); // 2700 - 2210 of unaccounted time is shown, not hidden
  });
  it('never divides by zero', () => {
    const { total } = waterfall(sample(BLOCKED_RAW_TRACE));
    expect(total).toBeGreaterThan(0);
  });
});

describe('summarize', () => {
  it('says "zero AI calls" for a pattern-screen block', () => {
    expect(summarize(sample(BLOCKED_RAW_TRACE))).toBe('Stopped at the guardrail in 1 ms with zero AI calls.');
  });
  it('describes a normal answer', () => {
    expect(summarize(sample())).toBe('Answered from the documents · 1 search pass · 4 AI calls · 2700 ms.');
  });
  it('mentions search passes after a rewrite', () => {
    expect(summarize(sample(REWRITE_RAW_TRACE))).toContain('2 search passes');
  });
});

describe('retrieval helpers', () => {
  it('reports what each search found that the other missed', () => {
    const c = branchContribution([{ source: 'a' }, { source: 'b' }], [{ source: 'b' }, { source: 'c' }]);
    expect(c).toEqual({ denseOnly: ['a'], bm25Only: ['c'], both: ['b'] });
  });
  it('computes how far the reranker moved each row', () => {
    expect(rankMoves([{ fusedRank: 2 }, { fusedRank: 1 }, { fusedRank: null }, { fusedRank: 3 }])).toEqual([1, -1, 0, -1]);
  });
  it('raw fixture really is the BFF shape (snake_case)', () => {
    expect(RAW_TRACE.steps[0]).toHaveProperty('llm_calls');
  });
});
