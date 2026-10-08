import { useCountdown } from '../hooks/useNow';
import type { StoredError } from '../state/chat';

interface Props {
  error: StoredError;
  /** Resend the message that failed (absent when nothing can be resent, e.g. the text went back into the box). */
  onRetry: (() => void) | undefined;
  onDismiss: () => void;
}

/** Screen readers hear the countdown only every 10 s (an assertive region that changes every second would be unusable). */
function announcement(remaining: number): string {
  if (remaining <= 0) return 'You can try again now.';
  if (remaining <= 10) return 'You can try again in a few seconds.';
  return `You can try again in about ${String(Math.ceil(remaining / 10) * 10)} seconds.`;
}

export function ErrorBanner({ error, onRetry, onDismiss }: Props) {
  const remaining = useCountdown(error.retryUntil);
  const waiting = remaining > 0;
  const limited = error.kind === 'rate_limited';
  // Rate limits are a hard stop until the countdown ends; other errors allow an immediate manual retry.
  const retryLabel = limited ? 'Send again' : error.kind === 'unavailable' ? 'Retry now' : 'Try again';
  const showRetry = onRetry !== undefined && (limited ? !waiting : error.canRetry);

  return (
    <>
      <div className={`banner banner-${limited ? 'warn' : 'error'}`} role="alert" data-testid="error-banner" data-kind={error.kind}>
        <div className="banner-body">
          <strong className="banner-title">{error.title}</strong>
          <p>{error.detail}</p>
          {error.retryUntil !== null && (
            <p className="countdown" aria-hidden="true" data-testid="countdown">
              {waiting ? (limited ? `You can send again in ${String(remaining)}s` : `Suggested wait: ${String(remaining)}s`) : 'You can try again now.'}
            </p>
          )}
          {error.requestId !== null && <p className="muted small">Reference: {error.requestId}</p>}
        </div>
        <div className="banner-actions">
          {showRetry && (
            <button type="button" className="btn btn-primary btn-small" onClick={onRetry}>
              {retryLabel}
            </button>
          )}
          <button type="button" className="btn btn-ghost btn-small" onClick={onDismiss}>
            Dismiss
          </button>
        </div>
      </div>
      {error.retryUntil !== null && (
        <p className="sr-only" role="status">
          {announcement(remaining)}
        </p>
      )}
    </>
  );
}
