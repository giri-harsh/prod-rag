import type { QueryAnswer } from '../api/client';

/** What the answer was grounded in, plus the (collapsed by default) planner/graph trace the API returns as `thought_process`. */
export function SourcesPanel({ answer }: { answer: QueryAnswer }) {
  const { sources, thoughtProcess, status } = answer;
  return (
    <section className="sources" aria-label="Sources and reasoning">
      <h4 className="sources-title">Sources</h4>
      {sources.length > 0 ? (
        <ul className="source-list">
          {sources.map((s) => (
            <li key={s} className="source-chip" title={s}>
              {s}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted small">No documents were used for this answer.</p>
      )}
      {thoughtProcess.length > 0 && (
        <details className="thought">
          <summary>Thought process</summary>
          <ol>
            {thoughtProcess.map((step, i) => (
              <li key={`${String(i)}-${step}`}>{step}</li>
            ))}
          </ol>
        </details>
      )}
      {status !== null && <p className="muted small">Status: {status}</p>}
    </section>
  );
}
