import { useEffect, useRef } from 'react';
import type { RefObject } from 'react';
import { useCountdown } from '../hooks/useNow';
import type { ChatMessage, StoredError } from '../state/chat';
import type { SessionMeta } from '../state/sessions';
import { Composer } from './Composer';
import { ErrorBanner } from './ErrorBanner';
import { MessageItem } from './MessageItem';

interface Props {
  session: SessionMeta;
  messages: ChatMessage[];
  pending: boolean;
  busyRetry: boolean;
  error: StoredError | undefined;
  draft: string;
  rateLimitedUntil: number | null;
  failedMessage: ChatMessage | undefined;
  composerRef: RefObject<HTMLTextAreaElement | null>;
  onDraft: (text: string) => void;
  onSend: (text: string) => void;
  onRetry: (message: ChatMessage) => void;
  onDismissError: () => void;
}

export function ChatView(props: Props) {
  const { session, messages, pending, busyRetry, error, draft, rateLimitedUntil, failedMessage, composerRef } = props;
  const pausedFor = useCountdown(rateLimitedUntil);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' });
  }, [messages.length, pending, error]);

  return (
    <div className="chat">
      <div className="chat-head">
        <h2 id="chat-title" className="chat-title" title={session.title}>
          {session.title}
        </h2>
      </div>

      <div className="log" role="log" aria-live="polite" aria-relevant="additions" aria-labelledby="chat-title" tabIndex={0}>
        {messages.length === 0 && (
          <div className="empty" data-testid="empty-state">
            {session.started ? (
              <p>
                <strong>Earlier messages are not shown.</strong> The assistant still remembers this conversation, but the server has no history
                endpoint, so this app can only display messages from the current page view.
              </p>
            ) : (
              <p>Ask a question about your documentation. Answers come with the sources they were based on.</p>
            )}
          </div>
        )}
        {messages.map((m) => (
          <MessageItem key={m.id} message={m} />
        ))}
        {pending && (
          <div className="pending" role="status" data-testid="pending">
            <span className="dots" aria-hidden="true" />
            {busyRetry ? 'Another request in this conversation is still running. Retrying automatically…' : 'The assistant is thinking…'}
          </div>
        )}
        <div ref={endRef} />
      </div>

      {error && (
        <ErrorBanner
          key={`${error.kind}-${String(error.retryUntil)}`}
          error={error}
          onRetry={failedMessage ? () => { props.onRetry(failedMessage); } : undefined}
          onDismiss={props.onDismissError}
        />
      )}

      <Composer
        value={draft}
        pending={pending}
        pausedFor={pausedFor}
        textareaRef={composerRef}
        onChange={props.onDraft}
        onSubmit={() => {
          props.onSend(draft);
        }}
      />
    </div>
  );
}
