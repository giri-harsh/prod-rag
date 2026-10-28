import { useId, useState } from 'react';
import type { HitRow, RerankRow, Trace, TraceStep } from '../api/trace';
import { STEP_INFO, branchContribution, rankMoves, summarize, waterfall } from '../trace/describe';

const fmtScore = (s: number | null): string => (s === null ? 'n/a' : s.toFixed(3));

/** A step title with a "why this matters" toggle that works with keyboard, touch and screen readers. */
function Why({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <>
      <button
        type="button"
        className="why-btn"
        aria-expanded={open}
        aria-controls={id}
        aria-label="Why this matters"
        title={text}
        onClick={() => {
          setOpen((o) => !o);
        }}
      >
        ?
      </button>
      <p id={id} className="why-text" hidden={!open}>
        {text}
      </p>
    </>
  );
}

function Status({ tone, label }: { tone: 'ok' | 'stop' | 'info' | 'skip'; label: string }) {
  return <span className={`pill pill-${tone}`}>{label}</span>;
}

function HitList({ title, rows, hint }: { title: string; rows: HitRow[]; hint: string }) {
  return (
    <div className="hitcol">
      <h5>{title}</h5>
      <p className="muted small">{hint}</p>
      {rows.length === 0 ? (
        <p className="muted small">not captured</p>
      ) : (
        <ol>
          {rows.map((r, i) => (
            <li key={`${String(i)}-${r.source}-${r.snippet}`} title={r.snippet}>
              <span className="hit-src">{r.source}</span> <span className="hit-score">{fmtScore(r.score)}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function RerankList({ rows }: { rows: RerankRow[] }) {
  const moves = rankMoves(rows);
  return (
    <div className="hitcol">
      <h5>After rerank</h5>
      <p className="muted small">A model re-reads each passage against the question; arrows show how far it moved.</p>
      <ol>
        {rows.map((r, i) => {
          const m = moves[i] ?? 0;
          return (
            <li key={`${String(i)}-${r.source}-${r.snippet}`} title={r.snippet}>
              <span className="hit-src">{r.source}</span> <span className="hit-score">{fmtScore(r.score)}</span>
              {m !== 0 && (
                <span className={m > 0 ? 'mv mv-up' : 'mv mv-down'} aria-label={m > 0 ? `moved up ${String(m)}` : `moved down ${String(-m)}`}>
                  {m > 0 ? `▲${String(m)}` : `▼${String(-m)}`}
                </span>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function Retrieval({ step }: { step: Extract<TraceStep, { kind: 'retrieval' }> }) {
  const c = branchContribution(step.dense, step.bm25);
  const haveBranches = step.dense.length > 0 || step.bm25.length > 0;
  return (
    <div className="retrieval">
      <p className="muted small">
        Query searched: <q>{step.query}</q>
      </p>
      {haveBranches && (
        <p className="small contrib">
          {c.bm25Only.length > 0 && (
            <span>
              <strong>Word search (BM25) found</strong> {c.bm25Only.join(', ')} <strong>that meaning search missed.</strong>{' '}
            </span>
          )}
          {c.denseOnly.length > 0 && (
            <span>
              <strong>Meaning search found</strong> {c.denseOnly.join(', ')} <strong>that word search missed.</strong>{' '}
            </span>
          )}
          {c.bm25Only.length === 0 && c.denseOnly.length === 0 && <span>Both searches agreed on the same documents.</span>}
        </p>
      )}
      <div className="hitgrid">
        <HitList title="Meaning search" rows={step.dense} hint="dense vectors: finds paraphrases" />
        <HitList title="Word search" rows={step.bm25} hint="BM25: finds exact terms like config keys" />
        <HitList title="Merged (RRF)" rows={step.fused} hint="reciprocal rank fusion of the two lists" />
        <RerankList rows={step.reranked} />
      </div>
      <p className="muted small">
        {step.kept} passage{step.kept === 1 ? '' : 's'} sent to the model · {step.dropped} dropped · {step.neutralized} neutralised by the injection screen · embed {step.embedMs} ms · search {step.searchMs} ms · rerank {step.rerankMs} ms
      </p>
    </div>
  );
}

function Citations({ trace }: { trace: Trace }) {
  const last = [...trace.steps].reverse().find((s): s is Extract<TraceStep, { kind: 'retrieval' }> => s.kind === 'retrieval');
  if (!last || last.chunks.length === 0) return null;
  return (
    <section className="citations" aria-label="Citations">
      <h5>Citations: the exact passages the answer was written from</h5>
      <ul>
        {last.chunks.map((c, i) => (
          <li key={`${String(i)}-${c.source}`}>
            <details>
              <summary>
                <span className="hit-src">[{i + 1}] {c.source}</span>
                {c.page !== null && <span className="muted small"> · p. {c.page}</span>}
                {c.heading !== '' && <span className="muted small"> · {c.heading}</span>}
                <span className="hit-score"> {fmtScore(c.score)}</span>
              </summary>
              <blockquote>{c.text}</blockquote>
            </details>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Waterfall({ trace }: { trace: Trace }) {
  const { rows, total } = waterfall(trace);
  return (
    <section className="waterfall" aria-label="Latency by stage">
      <h5>Where the {total} ms went</h5>
      <ul>
        {rows.map((r) => (
          <li key={r.id}>
            <span className="wf-label">{r.label}</span>
            <svg className={`wf-track${r.parallel ? ' wf-parallel' : ''}`} viewBox="0 0 100 6" preserveAspectRatio="none" role="img" aria-label={`${r.label}: ${String(r.ms)} milliseconds`}>
              <rect className="wf-bg" x="0" y="0" width="100" height="6" />
              <rect className="wf-bar" x={(r.start / total) * 100} y="0.5" width={Math.max((r.ms / total) * 100, 0.8)} height="5" rx="1" />
            </svg>
            <span className="wf-ms">{r.ms} ms</span>
          </li>
        ))}
      </ul>
      <p className="muted small">The guardrail and the planner run at the same time, so their bars start together. Streaming is not used: the answer arrives whole, so time-to-first-token equals total time.</p>
    </section>
  );
}

function StepBody({ step }: { step: TraceStep }) {
  switch (step.kind) {
    case 'guardrail':
      return (
        <>
          <Status tone={step.blocked ? 'stop' : 'ok'} label={step.blocked ? 'blocked' : 'passed'} />{' '}
          <span className="muted small">
            {step.layer} · {step.ms} ms · {step.llmCalls} AI call{step.llmCalls === 1 ? '' : 's'}
            {step.rail !== null && (
              <>
                {' '}
                · rail: <code>{step.rail}</code>
              </>
            )}
          </span>
        </>
      );
    case 'telemetry_redaction':
      return step.applied ? (
        <>
          <Status tone="info" label="masked in logs" />{' '}
          <span className="muted small">
            {Object.entries(step.labels)
              .map(([k, v]) => `${k} ×${String(v)}`)
              .join(', ')}{' '}
            · what the logs record: <code>{step.loggedAs}</code>
          </span>
        </>
      ) : (
        <Status tone="skip" label="nothing sensitive found" />
      );
    case 'plan':
      return (
        <>
          <Status tone="info" label={step.intent} />{' '}
          <span className="muted small">
            {step.cacheHit ? 'planner cache · ' : ''}
            {step.ms} ms
          </span>
          {step.rewritten !== null && (
            <p className="rewrite-box">
              <span className="muted small">You asked</span> <q>{step.original}</q>
              <br />
              <span className="muted small">Searched as</span> <q>{step.rewritten}</q>
              {step.hasHistory && <span className="muted small"> (uses the earlier turns of this conversation)</span>}
            </p>
          )}
        </>
      );
    case 'semantic_cache':
      return (
        <>
          <Status tone={step.hit ? 'ok' : 'skip'} label={step.hit ? 'hit' : step.skipped ? 'skipped' : 'miss'} /> <span className="muted small">{step.ms} ms</span>
        </>
      );
    case 'retrieval':
      return <Retrieval step={step} />;
    case 'grade':
      return (
        <>
          <Status tone={step.verdict === 'good' ? 'ok' : 'stop'} label={step.verdict} />{' '}
          <span className="muted small">
            relevance {fmtScore(step.score)} vs. threshold {fmtScore(step.threshold)} → {step.next}
          </span>
        </>
      );
    case 'rewrite':
      return (
        <p className="rewrite-box">
          <span className="muted small">Weak result for</span> <q>{step.previous}</q>
          <br />
          <span className="muted small">Retrying with</span> <q>{step.next}</q> <span className="muted small">· {step.ms} ms</span>
        </p>
      );
    case 'decline':
      return (
        <>
          <Status tone="stop" label="declined" /> <span className="muted small">{step.reason}</span>
        </>
      );
    case 'llm':
      return (
        <>
          <Status tone={step.fallback ? 'info' : 'ok'} label={step.fallback ? 'fallback used' : 'primary model'} />{' '}
          <span className="muted small">
            <code>{step.model ?? 'model hidden by gateway'}</code> · {step.tokensIn} tokens in / {step.tokensOut} out · {step.ms} ms
            {step.cacheHit ? ' · cache hit' : ''}
            {step.fallback ? ' · automatically failed over' : ''}
          </span>
        </>
      );
    case 'output_guard':
      return (
        <>
          <Status tone={step.blocked ? 'stop' : 'ok'} label={step.blocked ? 'blocked' : 'passed'} />{' '}
          <span className="muted small">
            {step.ms} ms
            {step.storedInCache ? ' · stored in the semantic cache' : ''}
          </span>
        </>
      );
  }
}

/** "Under the hood": one entry per stage the request passed through, then the latency waterfall and the citations. */
export function TracePanel({ trace }: { trace: Trace }) {
  return (
    <section className="trace" aria-label="Under the hood" data-testid="trace-panel">
      <h4 className="trace-title">Under the hood</h4>
      <p className="trace-summary" data-testid="trace-summary">
        {summarize(trace)}
      </p>
      <ol className="timeline">
        {trace.steps.map((step, i) => {
          const info = STEP_INFO[step.kind];
          return (
            <li key={`${step.kind}-${String(i)}`} className={`tl-item tl-${step.kind}`}>
              <h5 className="tl-title">
                {info.title}
                <Why text={info.why} />
              </h5>
              <div className="tl-body">
                <StepBody step={step} />
              </div>
            </li>
          );
        })}
      </ol>
      <Waterfall trace={trace} />
      <Citations trace={trace} />
    </section>
  );
}
