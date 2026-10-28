import { describe, expect, it } from 'vitest';
import { RAW_TRACE } from '../testing/sampleTrace';
import { CHIPS, parseBenchmarks, parseChips } from './chips';

const resp = { answer: 'a', sources: ['s.txt'], blocked: false, trace: RAW_TRACE };

describe('parseChips', () => {
  it('parses recorded chips, keeping the layout and the trace', () => {
    const f = parseChips({ recorded_at: '2026-10-28', chips: { normal: { layout: 'single', cases: [{ label: null, question: 'q', response: resp }] }, tenants: { layout: 'compare', cases: [{ label: 'A', question: 'q', response: resp }, { label: 'B', question: 'q', response: resp }] } } });
    expect(f.recordedAt).toBe('2026-10-28');
    expect(f.chips.normal?.cases[0]?.answer.trace?.steps).toHaveLength(8);
    expect(f.chips.tenants?.layout).toBe('compare');
    expect(f.chips.tenants?.cases.map((c) => c.label)).toEqual(['A', 'B']);
  });
  it('drops a malformed chip without losing the others', () => {
    const f = parseChips({ chips: { bad: { cases: [{ question: 'q', response: { nope: true } }] }, ok: { cases: [{ question: 'q', response: resp }] }, empty: { cases: [] } } });
    expect(Object.keys(f.chips)).toEqual(['ok']);
  });
  it('is empty for junk', () => {
    for (const j of [null, 1, 'x', [], { chips: 3 }]) expect(parseChips(j).chips).toEqual({});
  });
});

describe('parseBenchmarks', () => {
  it('keeps only metrics with an id and a value', () => {
    const m = parseBenchmarks({ metrics: [{ id: 'a', label: 'L', value: '1%', sub: 's', env: 'e', source: 'f' }, { id: '', value: 'x' }, { id: 'b' }, 7] });
    expect(m).toEqual([{ id: 'a', label: 'L', value: '1%', sub: 's', env: 'e', source: 'f' }]);
  });
  it('is empty for junk', () => {
    expect(parseBenchmarks(null)).toEqual([]);
    expect(parseBenchmarks({ metrics: 'x' })).toEqual([]);
  });
});

describe('the scenario list', () => {
  it('has unique ids and every recorded scenario has a question to record', () => {
    expect(new Set(CHIPS.map((c) => c.id)).size).toBe(CHIPS.length);
    for (const c of CHIPS) expect(c.question.length).toBeGreaterThan(10);
  });
});
