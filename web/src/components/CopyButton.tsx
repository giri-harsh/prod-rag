import { useEffect, useRef, useState } from 'react';

/** Clipboard write with a fallback for non-secure contexts (plain http on a LAN address has no navigator.clipboard). */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (typeof navigator.clipboard.writeText === 'function') {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through to the legacy path */
  }
  try {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.className = 'sr-only';
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    return ok;
  } catch {
    return false;
  }
}

export function CopyButton({ text, label }: { text: string; label: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const timer = useRef<number | undefined>(undefined);

  useEffect(
    () => () => {
      window.clearTimeout(timer.current);
    },
    [],
  );

  const onClick = () => {
    void copyText(text).then((ok) => {
      setState(ok ? 'copied' : 'failed');
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => {
        setState('idle');
      }, 2000);
    });
  };

  return (
    <>
      <button type="button" className="btn btn-ghost btn-small" onClick={onClick} aria-label={label}>
        {state === 'copied' ? 'Copied' : state === 'failed' ? 'Copy failed' : 'Copy'}
      </button>
      <span className="sr-only" role="status">
        {state === 'copied' ? 'Answer copied to the clipboard' : state === 'failed' ? 'Could not copy the answer' : ''}
      </span>
    </>
  );
}
