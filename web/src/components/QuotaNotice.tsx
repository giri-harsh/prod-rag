import { demoVideoUrl } from './Hero';

/**
 * Shown when a live question cannot be answered because the shared free-tier AI quota is spent (or the backend is busy).
 * It points at what still works: the recorded scenarios, which need no quota at all.
 */
export function QuotaNotice({ onShowRecorded }: { onShowRecorded: () => void }) {
  const video = demoVideoUrl();
  return (
    <aside className="quota" role="status" data-testid="quota-notice">
      <strong>The live demo is resting.</strong>
      <p>
        This project runs on free-tier AI quotas, and they are used up for the moment. Nothing is broken: every <em>recorded</em> scenario above shows the real
        system&apos;s answer and full trace, instantly and without spending any quota.
        {video !== '' && (
          <>
            {' '}
            You can also{' '}
            <a href={video} rel="noopener noreferrer" target="_blank">
              watch the demo video
            </a>
            .
          </>
        )}
      </p>
      <button type="button" className="btn btn-primary btn-small" onClick={onShowRecorded}>
        Show a recorded scenario
      </button>
    </aside>
  );
}
