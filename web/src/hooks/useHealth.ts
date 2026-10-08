import { useEffect, useState } from 'react';
import { fetchHealth } from '../api/client';
import type { Health } from '../api/client';

export type HealthState = Health | 'checking';

/** Polls the BFF's /api/health every 30 s while the tab is visible. */
export function useHealth(): HealthState {
  const [health, setHealth] = useState<HealthState>('checking');
  useEffect(() => {
    const ac = new AbortController();
    const poll = async () => {
      const result = await fetchHealth({ signal: ac.signal });
      if (!ac.signal.aborted) setHealth(result);
    };
    void poll();
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void poll();
    }, 30_000);
    return () => {
      ac.abort();
      window.clearInterval(timer);
    };
  }, []);
  return health;
}
