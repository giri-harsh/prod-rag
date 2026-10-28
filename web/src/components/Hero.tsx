export const GITHUB_URL = 'https://github.com/giri-harsh/prod-rag';
/** The walkthrough video's URL; empty until it is published, and the page then says so instead of linking to nothing. */
export function demoVideoUrl(): string {
  return '';
}

export function Hero() {
  const video = demoVideoUrl();
  return (
    <section className="hero" aria-labelledby="hero-title">
      <p className="eyebrow">Open-source portfolio project</p>
      <h2 id="hero-title" className="hero-title">
        A guardrailed, multi-tenant, agentic RAG system for technical documentation
      </h2>
      <p className="hero-lede">
        Ask a question about Kubernetes and networking documents. It answers <strong>with citations</strong>, or <strong>says honestly that it doesn&apos;t know</strong>, and
        it shows you every step it took, so you can see the engineering instead of taking it on trust.
      </p>
      <ul className="hero-links">
        <li>
          Built by <strong>Harsh Giri</strong>
        </li>
        <li>
          <a href={GITHUB_URL} rel="noopener noreferrer" target="_blank">
            Source code on GitHub
          </a>
        </li>
        <li>
          {video === '' ? (
            <span className="muted" data-testid="video-placeholder">
              Demo video: coming soon
            </span>
          ) : (
            <a href={video} rel="noopener noreferrer" target="_blank">
              Watch the 3-minute demo
            </a>
          )}
        </li>
      </ul>
      <ol className="steps60" aria-label="How to read this page in under a minute">
        <li>
          <strong>Pick a scenario</strong> below. Recorded ones answer instantly and cost nothing.
        </li>
        <li>
          <strong>Watch the diagram</strong> light up: that is the path the question took.
        </li>
        <li>
          <strong>Open &ldquo;Under the hood&rdquo;</strong> to see the guardrail verdict, both searches, the grader and the timings.
        </li>
      </ol>
    </section>
  );
}
