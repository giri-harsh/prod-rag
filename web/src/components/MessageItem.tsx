import type { ChatMessage } from '../state/chat';
import { CopyButton } from './CopyButton';
import { SourcesPanel } from './SourcesPanel';
import { TracePanel } from './TracePanel';

/** Answers are rendered as plain text (pre-wrap): nothing from the model is ever interpreted as HTML. */
export function MessageItem({ message, showTrace = true }: { message: ChatMessage; showTrace?: boolean }) {
  if (message.role === 'user') {
    return (
      <article className="msg msg-user" data-testid="message-user">
        <h3 className="sr-only">You</h3>
        <p className="msg-text">{message.text}</p>
        {message.failed === true && <p className="msg-failed">Not delivered</p>}
      </article>
    );
  }
  const a = message.answer;
  return (
    <article className="msg msg-assistant" data-testid="message-assistant">
      <h3 className="sr-only">Assistant</h3>
      {a && (
        <ul className="badges" aria-label="Answer details">
          {a.blocked && (
            <li className="badge badge-warn" title="The input or output guardrail refused this request">
              Blocked
            </li>
          )}
          {a.cacheHit && (
            <li className="badge badge-info" title="Served from the semantic answer cache">
              Cached
            </li>
          )}
          {a.iterations > 0 && (
            <li className="badge" title="Retrieval passes used (the graph may rewrite the query and retrieve again)">
              {a.iterations} {a.iterations === 1 ? 'iteration' : 'iterations'}
            </li>
          )}
        </ul>
      )}
      <p className="msg-text">{message.text}</p>
      <div className="msg-actions">
        <CopyButton text={message.text} label="Copy answer" />
      </div>
      {a && <SourcesPanel answer={a} />}
      {a?.trace && showTrace && <TracePanel trace={a.trace} />}
    </article>
  );
}
