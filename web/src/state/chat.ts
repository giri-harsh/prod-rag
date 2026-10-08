import type { QueryAnswer } from '../api/client';
import type { UiError } from '../api/errors';

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  createdAt: number;
  /** Present on assistant messages: sources, thought process and badges come from here. */
  answer?: QueryAnswer;
  /** User messages whose request failed; they stay visible and can be retried. */
  failed?: boolean;
}

/** A UiError plus the moment (epoch ms) at which the server said a retry makes sense. */
export type StoredError = UiError & { retryUntil: number | null };

export interface ChatState {
  /** In memory only: the API has no history endpoint, so a page reload starts each conversation view empty. */
  messages: Record<string, ChatMessage[]>;
  pending: Record<string, boolean>;
  /** True while the one automatic retry of a 409 is waiting. */
  busyRetry: Record<string, boolean>;
  errors: Record<string, StoredError | undefined>;
  drafts: Record<string, string>;
  /** Epoch ms until which sending is blocked (429). Global: the limit belongs to the API key / network, not the conversation. */
  rateLimitedUntil: number | null;
}

export const initialChat: ChatState = { messages: {}, pending: {}, busyRetry: {}, errors: {}, drafts: {}, rateLimitedUntil: null };

export type ChatAction =
  | { type: 'draft'; sessionId: string; text: string }
  | { type: 'send_start'; sessionId: string; messageId: string; text: string; now: number; retry: boolean }
  | { type: 'busy_retry'; sessionId: string }
  | { type: 'send_ok'; sessionId: string; assistantId: string; answer: QueryAnswer; now: number }
  | { type: 'send_failed'; sessionId: string; messageId: string; error: UiError; now: number }
  | { type: 'dismiss_error'; sessionId: string }
  | { type: 'remove_session'; sessionId: string }
  | { type: 'rate_limit_over' };

function replace<T>(rec: Record<string, T>, key: string, value: T): Record<string, T> {
  return { ...rec, [key]: value };
}

function without<T>(rec: Record<string, T>, key: string): Record<string, T> {
  return Object.fromEntries(Object.entries(rec).filter(([k]) => k !== key));
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case 'draft':
      return { ...state, drafts: replace(state.drafts, action.sessionId, action.text) };

    case 'send_start': {
      const list = state.messages[action.sessionId] ?? [];
      const next = action.retry
        ? list.map((m) => (m.id === action.messageId ? { ...m, failed: false } : m))
        : [...list, { id: action.messageId, role: 'user' as const, text: action.text, createdAt: action.now }];
      return {
        ...state,
        messages: replace(state.messages, action.sessionId, next),
        pending: replace(state.pending, action.sessionId, true),
        busyRetry: replace(state.busyRetry, action.sessionId, false),
        errors: replace(state.errors, action.sessionId, undefined),
        drafts: action.retry ? state.drafts : replace(state.drafts, action.sessionId, ''),
      };
    }

    case 'busy_retry':
      return { ...state, busyRetry: replace(state.busyRetry, action.sessionId, true) };

    case 'send_ok': {
      const list = state.messages[action.sessionId] ?? [];
      const reply: ChatMessage = { id: action.assistantId, role: 'assistant', text: action.answer.answer, createdAt: action.now, answer: action.answer };
      return {
        ...state,
        messages: replace(state.messages, action.sessionId, [...list, reply]),
        pending: replace(state.pending, action.sessionId, false),
        busyRetry: replace(state.busyRetry, action.sessionId, false),
        errors: replace(state.errors, action.sessionId, undefined),
      };
    }

    case 'send_failed': {
      const list = state.messages[action.sessionId] ?? [];
      const failedMsg = list.find((m) => m.id === action.messageId);
      // Too long / malformed: the text goes back into the composer and the message leaves the transcript.
      const restore = action.error.kind === 'too_large' || action.error.kind === 'invalid';
      const messages = restore
        ? list.filter((m) => m.id !== action.messageId)
        : list.map((m) => (m.id === action.messageId ? { ...m, failed: true } : m));
      const rateLimitedUntil =
        action.error.kind === 'rate_limited' ? action.now + (action.error.retryAfter ?? 30) * 1000 : state.rateLimitedUntil;
      return {
        ...state,
        messages: replace(state.messages, action.sessionId, messages),
        pending: replace(state.pending, action.sessionId, false),
        busyRetry: replace(state.busyRetry, action.sessionId, false),
        errors: replace(state.errors, action.sessionId, {
          ...action.error,
          retryUntil: action.error.retryAfter === null ? null : action.now + action.error.retryAfter * 1000,
        }),
        drafts: restore && failedMsg ? replace(state.drafts, action.sessionId, failedMsg.text) : state.drafts,
        rateLimitedUntil,
      };
    }

    case 'dismiss_error':
      return { ...state, errors: replace(state.errors, action.sessionId, undefined) };

    case 'remove_session':
      return {
        ...state,
        messages: without(state.messages, action.sessionId),
        pending: without(state.pending, action.sessionId),
        busyRetry: without(state.busyRetry, action.sessionId),
        errors: without(state.errors, action.sessionId),
        drafts: without(state.drafts, action.sessionId),
      };

    case 'rate_limit_over':
      return { ...state, rateLimitedUntil: null };
  }
}

/** The last user message that failed in this session (what "Try again" re-sends), if any. */
export function lastFailedMessage(state: ChatState, sessionId: string): ChatMessage | undefined {
  return [...(state.messages[sessionId] ?? [])].reverse().find((m) => m.role === 'user' && m.failed === true);
}
