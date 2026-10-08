import { useCallback, useEffect, useState } from 'react';

export type ThemeChoice = 'system' | 'light' | 'dark';
export const THEME_KEY = 'rag.theme';
const ORDER: ThemeChoice[] = ['system', 'light', 'dark'];

function readTheme(): ThemeChoice {
  try {
    const v = window.localStorage.getItem(THEME_KEY);
    return v === 'light' || v === 'dark' ? v : 'system';
  } catch {
    return 'system';
  }
}

/** system -> light -> dark. The same choice is applied before first paint by public/theme-init.js (no flash). */
export function useTheme(): [ThemeChoice, () => void] {
  const [theme, setTheme] = useState<ThemeChoice>(readTheme);

  useEffect(() => {
    const root = document.documentElement;
    if (theme === 'system') root.removeAttribute('data-theme');
    else root.setAttribute('data-theme', theme);
    try {
      if (theme === 'system') window.localStorage.removeItem(THEME_KEY);
      else window.localStorage.setItem(THEME_KEY, theme);
    } catch {
      /* storage blocked: the choice just lasts for this page view */
    }
  }, [theme]);

  const cycle = useCallback(() => {
    setTheme((t) => ORDER[(ORDER.indexOf(t) + 1) % ORDER.length] ?? 'system');
  }, []);
  return [theme, cycle];
}
