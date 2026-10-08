import { useCallback, useEffect, useRef, useState } from 'react';
import { ChatView } from './components/ChatView';
import { SessionList } from './components/SessionList';
import { useChat } from './hooks/useChat';
import { useHealth } from './hooks/useHealth';
import type { HealthState } from './hooks/useHealth';
import { useTheme } from './hooks/useTheme';
import { lastFailedMessage } from './state/chat';
import type { ChatMessage } from './state/chat';
import {
  browserStorage,
  createSession,
  deleteSession,
  loadState,
  markStarted,
  newSessionId,
  renameSession,
  saveState,
  selectSession,
} from './state/sessions';
import type { SessionsState } from './state/sessions';

const HEALTH_LABEL: Record<HealthState, string> = {
  checking: 'Checking…',
  ready: 'Backend ready',
  not_ready: 'Backend not ready',
  unreachable: 'Backend unreachable',
};

export function App() {
  const [sessions, setSessions] = useState<SessionsState>(() => loadState(browserStorage(), newSessionId(), Date.now()));
  const [menuOpen, setMenuOpen] = useState(false);
  const chat = useChat();
  const health = useHealth();
  const [theme, cycleTheme] = useTheme();
  const composerRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    saveState(browserStorage(), sessions);
  }, [sessions]);

  const active = sessions.sessions.find((s) => s.id === sessions.activeId) ?? sessions.sessions[0];
  const focusComposer = useCallback(() => {
    requestAnimationFrame(() => composerRef.current?.focus());
  }, []);

  if (!active) return null; // unreachable: loadState always yields at least one session

  const id = active.id;
  const { state } = chat;

  const send = (text: string) => {
    if (text.trim() === '') return;
    setSessions((s) => markStarted(s, id, text.trim()));
    void chat.send(id, text);
    focusComposer();
  };
  const retry = (m: ChatMessage) => {
    void chat.retry(id, m);
    focusComposer();
  };

  return (
    <div className="shell">
      <a className="skip-link" href="#composer">
        Skip to message box
      </a>
      <header className="topbar">
        <button
          type="button"
          className="btn btn-ghost menu-btn"
          aria-expanded={menuOpen}
          aria-controls="sidebar"
          onClick={() => {
            setMenuOpen((o) => !o);
          }}
        >
          Conversations
        </button>
        <h1 className="brand">Knowledge assistant</h1>
        <p className={`health health-${health}`} role="status" data-testid="health">
          <span className="health-dot" aria-hidden="true" />
          {HEALTH_LABEL[health]}
        </p>
        <button type="button" className="btn btn-ghost" onClick={cycleTheme} aria-label={`Colour theme: ${theme}. Activate to change.`}>
          Theme: {theme}
        </button>
      </header>

      <div className="layout">
        <aside id="sidebar" className={`sidebar${menuOpen ? ' open' : ''}`}>
          <SessionList
            sessions={sessions.sessions}
            activeId={sessions.activeId}
            onSelect={(sid) => {
              setSessions((s) => selectSession(s, sid));
              setMenuOpen(false);
              focusComposer();
            }}
            onCreate={() => {
              setSessions((s) => createSession(s, newSessionId(), Date.now()));
              setMenuOpen(false);
              focusComposer();
            }}
            onRename={(sid, title) => {
              setSessions((s) => renameSession(s, sid, title));
            }}
            onDelete={(sid) => {
              chat.discard(sid);
              setSessions((s) => deleteSession(s, sid, newSessionId(), Date.now()));
              focusComposer();
            }}
          />
        </aside>

        <main className="main" aria-labelledby="chat-title">
          <ChatView
            key={id}
            session={active}
            messages={state.messages[id] ?? []}
            pending={state.pending[id] ?? false}
            busyRetry={state.busyRetry[id] ?? false}
            error={state.errors[id]}
            draft={state.drafts[id] ?? ''}
            rateLimitedUntil={state.rateLimitedUntil}
            failedMessage={lastFailedMessage(state, id)}
            composerRef={composerRef}
            onDraft={(text) => {
              chat.setDraft(id, text);
            }}
            onSend={send}
            onRetry={retry}
            onDismissError={() => {
              chat.dismissError(id);
            }}
          />
        </main>
      </div>
    </div>
  );
}
