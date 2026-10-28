import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { parseAnswer } from '../api/client';
import { CHIPS } from '../demo/chips';
import type { Chip } from '../demo/chips';
import { RAW_TRACE, BLOCKED_RAW_TRACE, sample } from '../testing/sampleTrace';
import { nodeStates } from '../trace/describe';
import { Architecture } from './Architecture';
import { Hero } from './Hero';
import { MetricCards } from './MetricCards';
import { QuotaNotice } from './QuotaNotice';
import { Showcase } from './Showcase';
import { TracePanel } from './TracePanel';
import { TryIt } from './TryIt';

const html = (el: React.ReactElement): string => renderToStaticMarkup(el);

describe('Hero', () => {
  const out = html(<Hero />);
  it('says what it is, who built it, links the source and is honest that there is no video yet', () => {
    expect(out).toContain('guardrailed, multi-tenant, agentic RAG');
    expect(out).toContain('Harsh Giri');
    expect(out).toContain('href="https://github.com/giri-harsh/prod-rag"');
    expect(out).toContain('rel="noopener noreferrer"');
    expect(out).toContain('Demo video: coming soon');
  });
  it('has a heading and a 3-step reading guide', () => {
    expect(out).toContain('<h2 id="hero-title"');
    expect(out).toMatch(/aria-label="How to read this page in under a minute"/);
  });
});

describe('MetricCards', () => {
  it('renders exactly the metrics it is given, each with its measurement source', () => {
    const out = html(<MetricCards failed={false} metrics={[{ id: 'a', label: 'Hit@5', value: '100%', sub: 'n=60', env: 'real embeddings', source: 'f.json' }]} />);
    expect(out).toContain('100%');
    expect(out).toContain('real embeddings');
    expect(out).toContain('f.json');
    expect(out.match(/data-testid="metric-card"/g)).toHaveLength(1);
  });
  it('invents no number when the figures are unavailable', () => {
    expect(html(<MetricCards failed={true} metrics={null} />).replace(/<[^>]+>/g, '')).not.toMatch(/\d/); // visible text only
    expect(html(<MetricCards failed={false} metrics={null} />).replace(/<[^>]+>/g, '')).not.toMatch(/\d/); // skeletons carry no figures
    expect(html(<MetricCards failed={false} metrics={[]} />)).toBe('');
  });
});

describe('Architecture', () => {
  it('draws both layouts, labelled, and lights only the nodes that ran', () => {
    const out = html(<Architecture states={nodeStates(sample())} active={null} />);
    expect(out.match(/<svg/g)).toHaveLength(2);
    expect(out).toContain('role="img"');
    expect(out).toContain('hybrid dense plus BM25 search fused by RRF');
    expect(out).toContain('class="arch-node is-lit"');
    expect(out).toContain('class="arch-node is-idle"'); // the rewrite loop did not run
    expect(out).not.toContain(' style=');
  });
  it('shows a blocked request as stopped at the guardrail', () => {
    const out = html(<Architecture states={nodeStates(sample(BLOCKED_RAW_TRACE))} active={null} />);
    expect(out).toContain('arch-node is-stop');
    expect(out.match(/arch-node is-lit/g)).toBeNull();
  });
});

describe('TracePanel', () => {
  const out = html(<TracePanel trace={sample()} />);
  it('renders one timeline entry per stage with its verdict', () => {
    expect(out.match(/class="tl-item/g)).toHaveLength(8);
    expect(out).toContain('Guardrail');
    expect(out).toContain('passed');
    expect(out).toContain('kubernetes nodes autoscaling'); // the rewritten standalone query
    expect(out).toContain('and the nodes?'); // the original
    expect(out).toContain('openai/gpt-oss-120b');
    expect(out).toContain('1800 tokens in / 220 out');
    expect(out).toContain('primary model');
  });
  it('shows dense, BM25, fused and reranked lists and what moved', () => {
    for (const t of ['Meaning search', 'Word search', 'Merged (RRF)', 'After rerank']) expect(out).toContain(t);
    expect(out).toContain('cronjobs.docx');
    expect(out).toContain('that meaning search missed'); // cronjobs.docx came only from BM25
    expect(out).toContain('▲1');
  });
  it('shows the masked text the logs record, never a raw email', () => {
    expect(out).toContain('[REDACTED:EMAIL]');
  });
  it('gives every stage a keyboard-operable "why this matters" control', () => {
    expect(out.match(/aria-label="Why this matters"/g)).toHaveLength(8);
    expect(out.match(/aria-expanded="false"/g)?.length).toBeGreaterThanOrEqual(8);
    expect(out).toContain('hidden=""'); // collapsed by default
  });
  it('has a latency waterfall with accessible bars and expandable citations', () => {
    expect(out).toContain('Where the 2700 ms went');
    expect(out).toContain('aria-label="Guardrail: 310 milliseconds"');
    expect(out).toContain('<details>');
    expect(out).toContain('concurrencyPolicy: Forbid skips a run.');
    expect(out).not.toContain(' style=');
  });
  it('summarises a guardrail block with zero AI calls', () => {
    expect(html(<TracePanel trace={sample(BLOCKED_RAW_TRACE)} />)).toContain('with zero AI calls');
  });
  it('is HTML-safe: text from the trace is escaped', () => {
    const t = sample({ steps: [{ step: 'decline', reason: '<img src=x onerror=alert(1)>' }], total_ms: 1 });
    const o = html(<TracePanel trace={t} />);
    expect(o).not.toContain('<img');
    expect(o).toContain('&lt;img');
  });
});

describe('TryIt', () => {
  const out = html(<TryIt selectedId="normal" busy={false} onPick={() => undefined} />);
  it('offers every scenario with a plain-English caption, grouped by capability', () => {
    expect(CHIPS).toHaveLength(8);
    for (const c of CHIPS) {
      expect(out).toContain(`data-testid="chip-${c.id}"`);
      expect(out).toContain(c.caption);
    }
    for (const g of ['Grounded answers', 'Conversation', 'Honesty', 'Safety', 'Isolation']) expect(out).toContain(g);
  });
  it('marks the selected chip and labels live vs recorded', () => {
    expect(out).toContain('aria-pressed="true"');
    expect(out).toContain('>live<');
    expect(out).toContain('>recorded<');
  });
  it('only the jailbreak chip runs live (it is stopped before any model is called)', () => {
    expect(CHIPS.filter((c) => c.mode === 'live').map((c) => c.id)).toEqual(['jailbreak']);
  });
});

const demoChip: Chip = { id: 'x', group: 'g', title: 'Normal question', caption: 'c', mode: 'recorded', layout: 'single', question: 'q' };
const answer = parseAnswer({ answer: 'The HPA watches CPU.', sources: ['a.txt'], blocked: false, iterations: 2, trace: RAW_TRACE });

describe('Showcase', () => {
  it('prompts before anything is picked', () => {
    expect(html(<Showcase chip={null} result={null} recordedAt="" loading={false} error={null} showTrace={true} />)).toContain('Pick a scenario');
  });
  it('labels recorded answers honestly and shows the trace when it is on', () => {
    const r = { layout: 'single' as const, cases: [{ label: null, question: 'How?', answer }] };
    const on = html(<Showcase chip={demoChip} result={r} recordedAt="2026-10-28" loading={false} error={null} showTrace={true} />);
    expect(on).toContain('Recorded from the real pipeline on 2026-10-28');
    expect(on).toContain('data-testid="trace-panel"');
    expect(on).toContain('2 search passes (self-corrected)');
    const off = html(<Showcase chip={demoChip} result={r} recordedAt="" loading={false} error={null} showTrace={false} />);
    expect(off).not.toContain('data-testid="trace-panel"');
    expect(off).toContain('The HPA watches CPU.');
  });
  it('compares tenants side by side with their labels', () => {
    const r = { layout: 'compare' as const, cases: [{ label: 'Tenant A', question: 'Q', answer }, { label: 'Tenant B', question: 'Q', answer }] };
    const o = html(<Showcase chip={demoChip} result={r} recordedAt="" loading={false} error={null} showTrace={false} />);
    expect(o).toContain('cases-compare');
    expect(o).toContain('Tenant A');
    expect(o).toContain('Tenant B');
  });
  it('announces progress and errors to assistive tech', () => {
    const o = html(<Showcase chip={{ ...demoChip, mode: 'live' }} result={null} recordedAt="" loading={true} error="nope" showTrace={true} />);
    expect(o).toContain('aria-busy="true"');
    expect(o).toContain('role="status"');
    expect(o).toContain('role="alert"');
    expect(o).toContain('Ran live just now');
  });
});

describe('QuotaNotice', () => {
  it('explains the quota state and points to what still works', () => {
    const o = html(<QuotaNotice onShowRecorded={() => undefined} />);
    expect(o).toContain('data-testid="quota-notice"');
    expect(o).toContain('recorded');
    expect(o).toContain('without spending any quota');
    expect(o).toContain('Show a recorded scenario');
  });
});
