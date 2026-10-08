import type { KeyboardEvent, RefObject } from 'react';
import { MAX_QUERY_CHARS } from '../api/client';

interface Props {
  value: string;
  pending: boolean;
  /** Seconds of rate-limit pause left (0 = not paused). */
  pausedFor: number;
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  onChange: (text: string) => void;
  onSubmit: () => void;
}

export function Composer({ value, pending, pausedFor, textareaRef, onChange, onSubmit }: Props) {
  const length = Array.from(value).length;
  const over = length > MAX_QUERY_CHARS;
  const empty = value.trim() === '';
  const paused = pausedFor > 0;
  const canSend = !empty && !over && !pending && !paused;

  const hint = paused
    ? `Rate limited. You can send again in ${String(pausedFor)}s.`
    : pending
      ? 'Waiting for the assistant to answer…'
      : over
        ? `Too long: ${String(length)} of ${String(MAX_QUERY_CHARS)} characters.`
        : 'Enter to send, Shift+Enter for a new line.';

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (canSend) onSubmit();
    }
  };

  return (
    <form
      className="composer"
      onSubmit={(e) => {
        e.preventDefault();
        if (canSend) onSubmit();
      }}
    >
      <label htmlFor="composer" className="sr-only">
        Your message
      </label>
      <textarea
        id="composer"
        ref={textareaRef}
        className="composer-input"
        rows={3}
        value={value}
        placeholder="Ask a question about your documentation…"
        aria-describedby="composer-hint"
        aria-invalid={over}
        onChange={(e) => {
          onChange(e.target.value);
        }}
        onKeyDown={onKeyDown}
      />
      <div className="composer-bar">
        <p id="composer-hint" className={`composer-hint${over || paused ? ' composer-hint-warn' : ''}`} data-testid="composer-hint">
          {hint}
        </p>
        {length > MAX_QUERY_CHARS * 0.8 && (
          <p className={`composer-count${over ? ' composer-hint-warn' : ''}`} aria-hidden="true">
            {length}/{MAX_QUERY_CHARS}
          </p>
        )}
        <button type="submit" className="btn btn-primary" disabled={!canSend}>
          {pending ? 'Sending…' : paused ? `Wait ${String(pausedFor)}s` : 'Send'}
        </button>
      </div>
    </form>
  );
}
