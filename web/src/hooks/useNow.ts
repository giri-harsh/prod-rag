import { useSyncExternalStore } from 'react';

/**
 * A shared clock that ticks twice a second, but only while something is subscribed. Components that need "now" (countdowns)
 * read it through useSyncExternalStore, which keeps render pure and avoids setState-in-effect.
 */
const listeners = new Set<() => void>();
let snapshot = Date.now();
let timer: number | undefined;

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (listeners.size === 1) {
    snapshot = Date.now();
    timer = window.setInterval(() => {
      snapshot = Date.now();
      listeners.forEach((l) => {
        l();
      });
    }, 500);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) window.clearInterval(timer);
  };
}

function idleSubscribe(): () => void {
  return () => undefined;
}

function getSnapshot(): number {
  return snapshot;
}

/** Epoch ms, refreshed every 500 ms while `active`; frozen otherwise. */
export function useNow(active: boolean): number {
  return useSyncExternalStore(active ? subscribe : idleSubscribe, getSnapshot, getSnapshot);
}

/** Whole seconds left until `untilMs` (0 when null or already past). */
export function secondsLeft(untilMs: number | null, nowMs: number): number {
  return untilMs === null ? 0 : Math.max(0, Math.ceil((untilMs - nowMs) / 1000));
}

export function useCountdown(untilMs: number | null): number {
  const now = useNow(untilMs !== null);
  return secondsLeft(untilMs, now);
}
