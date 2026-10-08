import { useCallback, useEffect, useReducer, useRef } from 'react';
import { MAX_QUERY_CHARS, sendQueryWithBusyRetry } from '../api/client';
import { describeError } from '../api/errors';
import { chatReducer, initialChat } from '../state/chat';
import type { ChatMessage } from '../state/chat';
import { newSessionId } from '../state/sessions';

/**
 * Owns the in-memory transcripts and the request lifecycle. Conversations are independent: one may be waiting for an
 * answer while the user reads or writes in another, and an answer always lands in the conversation that asked.
 */
export function useChat() {
  const [state, dispatch] = useReducer(chatReducer, initialChat);
  const inflight = useRef(new Map<string, AbortController>());

  useEffect(() => {
    const controllers = inflight.current;
    return () => {
      controllers.forEach((c) => {
        c.abort();
      });
    };
  }, []);

  const run = useCallback(async (sessionId: string, messageId: string, text: string, retry: boolean): Promise<void> => {
    if (inflight.current.has(sessionId)) return; // one request per conversation (the API would answer 409 anyway)
    const ac = new AbortController();
    inflight.current.set(sessionId, ac);
    dispatch({ type: 'send_start', sessionId, messageId, text, now: Date.now(), retry });
    try {
      const answer = await sendQueryWithBusyRetry(text, sessionId, {
        signal: ac.signal,
        onBusyRetry: () => {
          dispatch({ type: 'busy_retry', sessionId });
        },
      });
      if (!ac.signal.aborted) dispatch({ type: 'send_ok', sessionId, assistantId: newSessionId(), answer, now: Date.now() });
    } catch (e) {
      if (!ac.signal.aborted) dispatch({ type: 'send_failed', sessionId, messageId, error: describeError(e, MAX_QUERY_CHARS), now: Date.now() });
    } finally {
      inflight.current.delete(sessionId);
    }
  }, []);

  const send = useCallback(
    (sessionId: string, text: string): Promise<void> => run(sessionId, newSessionId(), text.trim(), false),
    [run],
  );
  const retry = useCallback((sessionId: string, message: ChatMessage): Promise<void> => run(sessionId, message.id, message.text, true), [run]);

  const discard = useCallback((sessionId: string) => {
    inflight.current.get(sessionId)?.abort();
    inflight.current.delete(sessionId);
    dispatch({ type: 'remove_session', sessionId });
  }, []);

  const setDraft = useCallback((sessionId: string, text: string) => {
    dispatch({ type: 'draft', sessionId, text });
  }, []);

  const dismissError = useCallback((sessionId: string) => {
    dispatch({ type: 'dismiss_error', sessionId });
  }, []);

  return { state, send, retry, discard, setDraft, dismissError };
}
