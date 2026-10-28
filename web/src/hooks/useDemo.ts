import { useEffect, useState } from 'react';
import { loadBenchmarks, loadChips } from '../demo/chips';
import type { ChipsFile, Metric } from '../demo/chips';

export interface DemoData {
  chips: ChipsFile | null;
  chipsFailed: boolean;
  metrics: Metric[] | null;
  metricsFailed: boolean;
}

/** Loads the recorded scenarios and the benchmark figures once, in the background (neither blocks the first paint). */
export function useDemoData(): DemoData {
  const [state, setState] = useState<DemoData>({ chips: null, chipsFailed: false, metrics: null, metricsFailed: false });
  useEffect(() => {
    const ac = new AbortController();
    loadChips(fetch, ac.signal).then(
      (chips) => {
        setState((s) => ({ ...s, chips }));
      },
      () => {
        if (!ac.signal.aborted) setState((s) => ({ ...s, chipsFailed: true }));
      },
    );
    loadBenchmarks(fetch, ac.signal).then(
      (metrics) => {
        setState((s) => ({ ...s, metrics }));
      },
      () => {
        if (!ac.signal.aborted) setState((s) => ({ ...s, metricsFailed: true }));
      },
    );
    return () => {
      ac.abort();
    };
  }, []);
  return state;
}

const KEY = 'rag.showTrace';

/** "Under the hood" is ON by default; the choice is remembered per browser when storage is available. */
export function useTraceToggle(): [boolean, () => void] {
  const [on, setOn] = useState<boolean>(() => {
    try {
      return localStorage.getItem(KEY) !== '0';
    } catch {
      return true;
    }
  });
  const toggle = () => {
    setOn((v) => {
      try {
        localStorage.setItem(KEY, v ? '0' : '1');
      } catch {
        /* storage blocked: the choice just does not persist */
      }
      return !v;
    });
  };
  return [on, toggle];
}
