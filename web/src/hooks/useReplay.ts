import { useEffect, useState } from 'react';
import type { Trace } from '../api/trace';
import { NODE_IDS, nodeStates } from '../trace/describe';
import type { NodeId, NodeState } from '../trace/describe';

const STEP_MS = 260;
const reduced = (): boolean => typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/** Identifies one "run" of the animation, so a new trace (or a new request) restarts it. */
function runKey(trace: Trace | null, pending: boolean): string {
  if (pending) return 'pending';
  if (trace === null) return 'idle';
  return `${trace.steps.map((s) => s.kind).join('>')}@${String(trace.totalMs)}`;
}

/**
 * Drives the architecture diagram.
 * - while a request is in flight (`pending`): stages light up one after another as a progress cue (the real path is not
 *   known until the answer arrives);
 * - when a trace is set: the real path is revealed stage by stage (all at once for reduced motion).
 */
export function useReplay(trace: Trace | null, pending: boolean): { states: Record<NodeId, NodeState>; active: NodeId | null } {
  const key = runKey(trace, pending);
  const [tick, setTick] = useState({ key: 'idle', n: 0 });

  useEffect(() => {
    if (key === 'idle') return;
    const limit = pending ? NODE_IDS.length - 1 : NODE_IDS.length;
    const t = setInterval(() => {
      setTick((s) => {
        const n = s.key === key ? s.n : 0;
        if (n >= limit) return pending ? { key, n: 1 } : s.key === key ? s : { key, n };
        return { key, n: n + 1 };
      });
    }, STEP_MS);
    return () => {
      clearInterval(t);
    };
  }, [key, pending]);

  const final = nodeStates(trace);
  const shown = !pending && reduced() ? NODE_IDS.length : tick.key === key ? tick.n : 0;
  const states = Object.fromEntries(
    NODE_IDS.map((id, i) => [id, key === 'idle' ? 'idle' : pending ? (i < shown ? 'lit' : 'idle') : i < shown ? final[id] : 'idle']),
  ) as Record<NodeId, NodeState>;
  const active = pending ? (NODE_IDS[Math.min(shown, NODE_IDS.length - 1)] ?? null) : null;
  return { states, active };
}
