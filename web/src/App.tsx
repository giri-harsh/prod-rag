import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from './api/errors';
import { sendQuery } from './api/client';
import { Architecture } from './components/Architecture';
import { ChatView } from './components/ChatView';
import { Hero } from './components/Hero';
import { MetricCards } from './components/MetricCards';
import { QuotaNotice } from './components/QuotaNotice';
import { SessionList } from './components/SessionList';
import { Showcase } from './components/Showcase';
import { TryIt } from './components/TryIt';
import { CHIPS } from './demo/chips';
import type { Chip, ChipResult } from './demo/chips';
import { useChat } from './hooks/useChat';
import { useDemoData, useTraceToggle } from './hooks/useDemo';
import { useHealth } from './hooks/useHealth';
import type { HealthState } from './hooks/useHealth';
import { useReplay } from './hooks/useReplay';
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
import type { Trace } from './api/trace';

const HEALTH_LABEL: Record<HealthState, string> = {
  checking: 'Checking…',
  ready: 'Backend ready',
  not_ready: 'Backend not ready',
  unreachable: 'Backend unreachable',
};

interface Live {
  loading: boolean;
  result: ChipResult | null;
  error: string | null;
  quota: boolean;
}

const IDLE_LIVE: Live = { loading: false, result: null, error: null, quota: false };

export function App() {
  const [sessions, setSessions] = useState<SessionsState>(() => loadState(browserStorage(), newSessionId(), Date.now()));
  const [menuOpen, setMenuOpen] = useState(false);
  const chat = useChat();
  const health = useHealth();
  const [theme, cycleTheme] = useTheme();
  const [showTrace, toggleTrace] = useTraceToggle();
  const demo = useDemoData();
  const composerRef = useRef<HTMLTextAreaElement>(null);

  const [chip, setChip] = useState<Chip | null>(null);
  const [live, setLive] = useState<Live>(IDLE_LIVE);

  useEffect(() => {
    saveState(browserStorage(), sessions);
  }, [sessions]);

  const active = sessions.sessions.find((s) => s.id === sessions.activeId) ?? sessions.sessions[0];
  const focusComposer = useCallback(() => {
    requestAnimationFrame(() => composerRef.current?.focus());
  }, []);

  const recorded: ChipResult | null = chip !== null && chip.mode === 'recorded' ? (demo.chips?.chips[chip.id] ?? null) : null;
  const shown = chip?.mode === 'live' ? live.result : recorded;
  const lastTrace: Trace | null = shown?.cases.at(-1)?.answer.trace ?? null;
  const { states, active: activeNode } = useReplay(lastTrace, live.loading);

  if (!active) return null; // unreachable: loadState always yields at least one session

  const id = active.id;
  const { state } = chat;

  const pick = (c: Chip) => {
    setChip(c);
    setLive(IDLE_LIVE);
    if (c.mode !== 'live') return;
    setLive({ ...IDLE_LIVE, loading: true });
    sendQuery(c.question, newSessionId()).then(
      (answer) => {
        setLive({ loading: false, error: null, quota: false, result: { layout: 'single', cases: [{ label: null, question: c.question, answer }] } });
      },
      (e: unknown) => {
        const quota = e instanceof ApiError && (e.kind === 'rate_limited' || e.kind === 'unavailable' || e.kind === 'network');
        setLive({ loading: false, result: null, quota, error: quota ? null : e instanceof ApiError ? e.message : 'Something went wrong.' });
      },
    );
  };
  const showRecorded = () => {
    const first = CHIPS.find((c) => c.mode === 'recorded');
    if (first) pick(first);
  };

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

  const chatLimited = state.errors[id]?.kind === 'rate_limited' || state.errors[id]?.kind === 'unavailable';

  return (
    <div className="site">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <h1 className="brand">Guardrailed RAG · live demo</h1>
        <p className={`health health-${health}`} role="status" data-testid="health">
          <span className="health-dot" aria-hidden="true" />
          {HEALTH_LABEL[health]}
        </p>
        <button type="button" className="btn btn-ghost btn-small" onClick={cycleTheme} aria-label={`Colour theme: ${theme}. Activate to change.`}>
          Theme: {theme}
        </button>
      </header>

      <main id="main" className="page">
        <Hero />
        <MetricCards metrics={demo.metrics} failed={demo.metricsFailed} />

        <section className="arch-section" aria-labelledby="arch-title">
          <h3 id="arch-title" className="section-title">
            How a question travels
          </h3>
          <Architecture states={states} active={activeNode} />
        </section>

        <TryIt selectedId={chip?.id ?? null} busy={live.loading} onPick={pick} />
        {demo.chipsFailed && (
          <p className="banner banner-warn" role="status">
            The recorded scenarios could not be loaded. Reload the page to try again.
          </p>
        )}
        {live.quota && <QuotaNotice onShowRecorded={showRecorded} />}
        <div className="trace-toggle">
          <label>
            <input type="checkbox" checked={showTrace} onChange={toggleTrace} /> Show &ldquo;Under the hood&rdquo; for every answer
          </label>
        </div>
        <Showcase
          chip={chip}
          result={shown}
          recordedAt={demo.chips?.recordedAt ?? ''}
          loading={live.loading || (chip?.mode === 'recorded' && demo.chips === null && !demo.chipsFailed)}
          error={live.error}
          showTrace={showTrace}
        />

        <section id="ask" className="ask" aria-labelledby="ask-title">
          <h3 id="ask-title" className="section-title">
            Ask your own question
          </h3>
          <p className="muted small">
            Live questions use shared free-tier AI quotas and are rate limited. The same &ldquo;Under the hood&rdquo; panel appears under each answer.
          </p>
          {chatLimited && <QuotaNotice onShowRecorded={showRecorded} />}
          <div className="ask-panel">
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
              <div className="main" role="region" aria-labelledby="chat-title">
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
                  showTrace={showTrace}
                  onToggleSessions={() => {
                    setMenuOpen((o) => !o);
                  }}
                  menuOpen={menuOpen}
                  onDraft={(text) => {
                    chat.setDraft(id, text);
                  }}
                  onSend={send}
                  onRetry={retry}
                  onDismissError={() => {
                    chat.dismissError(id);
                  }}
                />
              </div>
            </div>
          </div>
        </section>
      </main>

      <footer className="site-foot muted small">
        Built by Harsh Giri. Answers come from a small public documentation set (Kubernetes jobs, CronJobs, autoscaling, work queues). Conversations are remembered on the server for a limited time so follow-up
        questions work; don&apos;t type anything private.
      </footer>
    </div>
  );
}
