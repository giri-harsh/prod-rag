import { CHIPS } from '../demo/chips';
import type { Chip } from '../demo/chips';

interface Props {
  selectedId: string | null;
  busy: boolean;
  onPick: (chip: Chip) => void;
}

/** Guided scenarios, grouped by the capability each one proves. Each has a one-line, plain-English caption. */
export function TryIt({ selectedId, busy, onPick }: Props) {
  const groups = [...new Set(CHIPS.map((c) => c.group))];
  return (
    <section className="tryit" aria-labelledby="tryit-title">
      <h3 id="tryit-title" className="section-title">
        Try it: one click per capability
      </h3>
      <div className="chip-groups">
        {groups.map((g) => (
          <div key={g} className="chip-group" role="group" aria-label={g}>
            <h4 className="chip-group-title">{g}</h4>
            <ul>
              {CHIPS.filter((c) => c.group === g).map((c) => (
                <li key={c.id}>
                  <button
                    type="button"
                    className={`chip${selectedId === c.id ? ' chip-selected' : ''}`}
                    aria-pressed={selectedId === c.id}
                    aria-describedby={`chip-cap-${c.id}`}
                    disabled={busy && c.mode === 'live'}
                    onClick={() => {
                      onPick(c);
                    }}
                    data-testid={`chip-${c.id}`}
                  >
                    <span className="chip-title">{c.title}</span>
                    <span className="chip-mode">{c.mode === 'live' ? 'live' : 'recorded'}</span>
                  </button>
                  <p id={`chip-cap-${c.id}`} className="chip-caption">
                    {c.caption}
                  </p>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </section>
  );
}
