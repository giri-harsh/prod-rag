/**
 * The conversation list. Only ids and titles live in the browser (localStorage); the conversation itself is the server's
 * checkpointed thread, addressed by the session id. Everything here is pure except load/save, which take a storage object
 * so they can be tested and so a blocked/failing storage never breaks the app.
 */

export interface SessionMeta {
  id: string;
  title: string;
  createdAt: number;
  /** True once a message was sent from this browser: the server then holds history this client cannot re-display after a reload. */
  started: boolean;
}

export interface SessionsState {
  sessions: SessionMeta[];
  activeId: string;
}

export const STORAGE_KEY = 'rag.sessions.v1';
export const DEFAULT_TITLE = 'New chat';
export const MAX_TITLE_CHARS = 60;
export const MAX_SESSIONS = 100;
/** Same character set as the BFF's client-session rule (see web/server/lib.mjs CLIENT_SESSION_RE). */
export const SESSION_ID_RE = /^[A-Za-z0-9_-]{1,40}$/;

type ReadableStorage = Pick<Storage, 'getItem'>;
type WritableStorage = Pick<Storage, 'setItem'>;

/** 20 url-safe characters from 15 random bytes (120 bits). */
export function newSessionId(randomBytes: (n: number) => Uint8Array = (n) => crypto.getRandomValues(new Uint8Array(n))): string {
  const bytes = randomBytes(15);
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function cleanTitle(raw: string): string {
  const t = raw.replace(/\s+/g, ' ').trim().slice(0, MAX_TITLE_CHARS);
  return t === '' ? DEFAULT_TITLE : t;
}

export function initialState(id: string, now: number): SessionsState {
  return { sessions: [{ id, title: DEFAULT_TITLE, createdAt: now, started: false }], activeId: id };
}

export function createSession(state: SessionsState, id: string, now: number): SessionsState {
  const sessions = [{ id, title: DEFAULT_TITLE, createdAt: now, started: false }, ...state.sessions].slice(0, MAX_SESSIONS);
  return { sessions, activeId: id };
}

export function selectSession(state: SessionsState, id: string): SessionsState {
  return state.sessions.some((s) => s.id === id) ? { ...state, activeId: id } : state;
}

export function renameSession(state: SessionsState, id: string, title: string): SessionsState {
  return { ...state, sessions: state.sessions.map((s) => (s.id === id ? { ...s, title: cleanTitle(title) } : s)) };
}

/** Mark the session as used and, unless the user already named it, title it with the first words of the first message. */
export function markStarted(state: SessionsState, id: string, text: string): SessionsState {
  return {
    ...state,
    sessions: state.sessions.map((s) => {
      if (s.id !== id) return s;
      const title = s.title === DEFAULT_TITLE ? cleanTitle(text.slice(0, 48)) : s.title;
      return { ...s, title, started: true };
    }),
  };
}

/**
 * Removes the session from THIS BROWSER only (the server keeps the thread until its TTL). If it was the last one, or the
 * active one, a neighbour (or a fresh session built from `freshId`) becomes active.
 */
export function deleteSession(state: SessionsState, id: string, freshId: string, now: number): SessionsState {
  const idx = state.sessions.findIndex((s) => s.id === id);
  if (idx < 0) return state;
  const sessions = state.sessions.filter((s) => s.id !== id);
  if (sessions.length === 0) return initialState(freshId, now);
  if (state.activeId !== id) return { sessions, activeId: state.activeId };
  // `sessions` is non-empty here (the length-0 case returned above), so this index is always in range.
  // noUncheckedIndexedAccess cannot know that, and the honest way to say so is a safe fallback rather than a
  // non-null assertion: if it somehow were empty, a fresh session is the correct degradation, not a crash.
  const next = sessions[Math.min(idx, sessions.length - 1)] ?? sessions[0];
  if (!next) return initialState(freshId, now);
  return { sessions, activeId: next.id };
}

function isMeta(v: unknown): v is SessionMeta {
  if (typeof v !== 'object' || v === null) return false;
  const r = v as Record<string, unknown>;
  return typeof r.id === 'string' && SESSION_ID_RE.test(r.id) && typeof r.title === 'string' && typeof r.createdAt === 'number' && typeof r.started === 'boolean';
}

/** Never throws: unreadable, tampered or missing data yields a single fresh session. */
export function loadState(storage: ReadableStorage | null, freshId: string, now: number): SessionsState {
  try {
    const raw = storage?.getItem(STORAGE_KEY);
    if (raw) {
      const parsed: unknown = JSON.parse(raw);
      if (typeof parsed === 'object' && parsed !== null) {
        const rec = parsed as Record<string, unknown>;
        const seen = new Set<string>();
        const sessions = (Array.isArray(rec.sessions) ? (rec.sessions as unknown[]) : [])
          .filter(isMeta)
          .filter((s) => (seen.has(s.id) ? false : (seen.add(s.id), true)))
          .slice(0, MAX_SESSIONS)
          .map((s) => ({ id: s.id, title: cleanTitle(s.title), createdAt: s.createdAt, started: s.started }));
        const first = sessions[0];
        if (first) {
          const active = typeof rec.activeId === 'string' && seen.has(rec.activeId) ? rec.activeId : first.id;
          return { sessions, activeId: active };
        }
      }
    }
  } catch {
    /* fall through */
  }
  return initialState(freshId, now);
}

export function saveState(storage: WritableStorage | null, state: SessionsState): void {
  try {
    storage?.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    /* storage full or blocked (private mode): the list simply will not survive a reload */
  }
}

/** window.localStorage, or null when even touching it throws. */
export function browserStorage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}
