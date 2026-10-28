import type { Metric } from '../demo/chips';

/** Headline numbers, rendered exactly as /api/benchmarks delivered them, each with where it was measured. */
export function MetricCards({ metrics, failed }: { metrics: Metric[] | null; failed: boolean }) {
  if (metrics === null) {
    // Four placeholders with the real cards' height, so the page does not jump when the figures arrive.
    return (
      <section className="metrics" aria-label="Measured results" aria-busy={!failed}>
        <h3 className="section-title">Measured, not claimed</h3>
        {failed ? (
          <p className="muted small">Benchmark figures are unavailable right now.</p>
        ) : (
          <ul className="metric-grid" aria-hidden="true">
            {[0, 1, 2, 3].map((i) => (
              <li key={i} className="metric-card metric-skeleton" />
            ))}
          </ul>
        )}
      </section>
    );
  }
  if (metrics.length === 0) return null;
  return (
    <section className="metrics" aria-labelledby="metrics-title">
      <h3 id="metrics-title" className="section-title">
        Measured, not claimed
      </h3>
      <ul className="metric-grid">
        {metrics.map((m) => (
          <li key={m.id} className="metric-card" data-testid="metric-card">
            <p className="metric-label">{m.label}</p>
            <p className="metric-value">{m.value}</p>
            <p className="metric-sub">{m.sub}</p>
            <details className="metric-env">
              <summary>How it was measured</summary>
              <p>{m.env}</p>
              <p className="muted small">
                Source: <code>{m.source}</code>
              </p>
            </details>
          </li>
        ))}
      </ul>
    </section>
  );
}
