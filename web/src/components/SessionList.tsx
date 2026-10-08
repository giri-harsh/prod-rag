import { useRef, useState } from 'react';
import type { SessionMeta } from '../state/sessions';

interface Props {
  sessions: SessionMeta[];
  activeId: string;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
}

function Row({
  session,
  active,
  props,
}: {
  session: SessionMeta;
  active: boolean;
  props: Props;
}) {
  const [mode, setMode] = useState<'view' | 'rename' | 'confirm'>('view');
  const [name, setName] = useState(session.title);
  const renameBtn = useRef<HTMLButtonElement>(null);

  if (mode === 'rename') {
    const finish = (save: boolean) => {
      if (save) props.onRename(session.id, name);
      setMode('view');
      requestAnimationFrame(() => renameBtn.current?.focus());
    };
    return (
      <li className="session session-editing">
        <label className="sr-only" htmlFor={`rename-${session.id}`}>
          Conversation name
        </label>
        <input
          id={`rename-${session.id}`}
          className="session-input"
          value={name}
          maxLength={60}
          autoFocus
          onChange={(e) => {
            setName(e.target.value);
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') finish(true);
            if (e.key === 'Escape') finish(false);
          }}
          onBlur={() => {
            finish(true);
          }}
        />
      </li>
    );
  }

  if (mode === 'confirm') {
    return (
      <li className="session session-confirm">
        <p className="small" id={`confirm-${session.id}`}>
          Remove &ldquo;{session.title}&rdquo; from this browser? The server keeps the conversation memory until it expires.
        </p>
        <div className="session-actions-row">
          <button
            type="button"
            className="btn btn-danger btn-small"
            aria-describedby={`confirm-${session.id}`}
            autoFocus
            onClick={() => {
              props.onDelete(session.id);
            }}
          >
            Remove
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-small"
            onClick={() => {
              setMode('view');
            }}
          >
            Cancel
          </button>
        </div>
      </li>
    );
  }

  return (
    <li className={`session${active ? ' session-active' : ''}`}>
      <button
        type="button"
        className="session-main"
        aria-current={active ? 'true' : undefined}
        onClick={() => {
          props.onSelect(session.id);
        }}
      >
        <span className="session-title">{session.title}</span>
      </button>
      <button
        ref={renameBtn}
        type="button"
        className="icon-btn"
        aria-label={`Rename ${session.title}`}
        title="Rename"
        onClick={() => {
          setName(session.title);
          setMode('rename');
        }}
      >
        <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" focusable="false">
          <path d="M4 20h4L19 9l-4-4L4 16v4zM14 6l4 4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" />
        </svg>
      </button>
      <button
        type="button"
        className="icon-btn"
        aria-label={`Remove ${session.title}`}
        title="Remove from this browser"
        onClick={() => {
          setMode('confirm');
        }}
      >
        <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" focusable="false">
          <path d="M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" />
        </svg>
      </button>
    </li>
  );
}

export function SessionList(props: Props) {
  return (
    <nav className="sessions" aria-label="Conversations">
      <button type="button" className="btn btn-primary btn-block" onClick={props.onCreate} data-testid="new-chat">
        New chat
      </button>
      <ul className="session-list">
        {props.sessions.map((s) => (
          <Row key={s.id} session={s} active={s.id === props.activeId} props={props} />
        ))}
      </ul>
      <p className="muted small sessions-note">
        This list lives only in this browser. Removing a conversation here does not delete it on the server.
      </p>
    </nav>
  );
}
