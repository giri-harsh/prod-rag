import type { Chip, ChipResult, DemoCase } from '../demo/chips';
import { TracePanel } from './TracePanel';

function CaseView({ c, showTrace }: { c: DemoCase; showTrace: boolean }) {
  const a = c.answer;
  return (
    <article className="case" data-testid="case">
      {c.label !== null && <h4 className="case-label">{c.label}</h4>}
      <p className="case-q">
        <span className="sr-only">Question: </span>
        {c.question}
      </p>
      <ul className="badges" aria-label="Answer details">
        {a.blocked && <li className="badge badge-warn">Blocked by guardrail</li>}
        {a.cacheHit && <li className="badge badge-info">Cached</li>}
        {a.iterations > 1 && <li className="badge">{a.iterations} search passes (self-corrected)</li>}
      </ul>
      <p className="case-a" data-testid="case-answer">
        {a.answer}
      </p>
      {a.sources.length > 0 && (
        <ul className="source-list" aria-label="Sources">
          {a.sources.map((s) => (
            <li key={s} className="source-chip">
              {s}
            </li>
          ))}
        </ul>
      )}
      {showTrace && a.trace !== null && <TracePanel trace={a.trace} />}
      {showTrace && a.trace === null && <p className="muted small">No trace was recorded for this answer.</p>}
    </article>
  );
}

interface Props {
  chip: Chip | null;
  result: ChipResult | null;
  recordedAt: string;
  loading: boolean;
  error: string | null;
  showTrace: boolean;
}

/** The result area under the chips: instant for recorded scenarios, a spinner for the live one. */
export function Showcase({ chip, result, recordedAt, loading, error, showTrace }: Props) {
  if (chip === null) {
    return (
      <section className="showcase showcase-empty" aria-label="Scenario result" aria-live="polite">
        <p className="muted">Pick a scenario above to see the answer and how the system produced it.</p>
      </section>
    );
  }
  const live = chip.mode === 'live';
  return (
    <section className="showcase" aria-label="Scenario result" aria-live="polite" aria-busy={loading} data-testid="showcase">
      <header className="showcase-head">
        <h3 className="showcase-title">{chip.title}</h3>
        <span className={`badge ${live ? 'badge-info' : ''}`}>
          {live ? 'Ran live just now' : recordedAt !== '' ? `Recorded from the real pipeline on ${recordedAt}` : 'Recorded from the real pipeline'}
        </span>
      </header>
      {loading && (
        <p className="pending" role="status">
          <span className="dots" aria-hidden="true" />
          Running the question through the pipeline…
        </p>
      )}
      {error !== null && (
        <p className="banner banner-warn" role="alert">
          {error}
        </p>
      )}
      {result !== null && (
        <div className={`cases cases-${result.layout}`}>
          {result.cases.map((c) => (
            <CaseView key={`${c.label ?? ''}-${c.question}`} c={c} showTrace={showTrace} />
          ))}
        </div>
      )}
    </section>
  );
}
