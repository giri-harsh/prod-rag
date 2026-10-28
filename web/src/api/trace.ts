/** The explain-this-answer trace, as the BFF delivers it (already allowlisted server-side; validated again here). */

export interface HitRow {
  source: string;
  score: number | null;
  snippet: string;
}
export interface RerankRow extends HitRow {
  fusedRank: number | null;
}
export interface Chunk {
  source: string;
  score: number | null;
  text: string;
  page: number | null;
  heading: string;
}

export type TraceStep =
  | { kind: 'guardrail'; blocked: boolean; rail: string | null; layer: string; ms: number; llmCalls: number }
  | { kind: 'telemetry_redaction'; applied: boolean; labels: Record<string, number>; loggedAs: string }
  | {
      kind: 'plan';
      intent: 'search' | 'conversational';
      original: string;
      rewritten: string | null;
      standalone: boolean;
      hasHistory: boolean;
      cacheHit: boolean;
      degraded: boolean;
      ms: number;
      llmCalls: number;
    }
  | { kind: 'semantic_cache'; hit: boolean; skipped: boolean; ms: number }
  | {
      kind: 'retrieval';
      iteration: number;
      query: string;
      embedMs: number;
      searchMs: number;
      rerankMs: number;
      ms: number;
      kept: number;
      dropped: number;
      neutralized: number;
      dense: HitRow[];
      bm25: HitRow[];
      fused: HitRow[];
      reranked: RerankRow[];
      chunks: Chunk[];
    }
  | { kind: 'grade'; iteration: number; score: number | null; threshold: number | null; verdict: 'good' | 'poor'; next: string }
  | { kind: 'rewrite'; previous: string; next: string; model: string | null; ms: number; llmCalls: number }
  | { kind: 'decline'; reason: string }
  | {
      kind: 'llm';
      model: string | null;
      fallback: boolean;
      cacheHit: boolean;
      tokensIn: number;
      tokensOut: number;
      ms: number;
      llmCalls: number;
      grounded: boolean;
      contextChunks: number;
    }
  | { kind: 'output_guard'; blocked: boolean; rail: string | null; ms: number; storedInCache: boolean; llmCalls: number };

export interface Trace {
  steps: TraceStep[];
  totalMs: number;
  llmCalls: number;
}

type Rec = Record<string, unknown>;
const isRec = (v: unknown): v is Rec => typeof v === 'object' && v !== null && !Array.isArray(v);
const str = (v: unknown): string => (typeof v === 'string' ? v : '');
const optStr = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : null);
const int = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? Math.max(0, Math.round(v)) : 0);
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const list = (v: unknown): Rec[] => (Array.isArray(v) ? v.filter(isRec) : []);
const hit = (r: Rec): HitRow => ({ source: str(r.source), score: num(r.score), snippet: str(r.snippet) });

function parseStep(r: Rec): TraceStep | null {
  switch (r.step) {
    case 'guardrail':
      return { kind: 'guardrail', blocked: r.blocked === true, rail: optStr(r.rail), layer: str(r.layer), ms: int(r.ms), llmCalls: int(r.llm_calls) };
    case 'telemetry_redaction': {
      const labels: Record<string, number> = {};
      if (isRec(r.labels)) for (const [k, v] of Object.entries(r.labels)) labels[k] = int(v);
      return { kind: 'telemetry_redaction', applied: r.applied === true, labels, loggedAs: str(r.logged_as) };
    }
    case 'plan':
      return {
        kind: 'plan',
        intent: r.intent === 'conversational' ? 'conversational' : 'search',
        original: str(r.original),
        rewritten: optStr(r.rewritten),
        standalone: r.standalone === true,
        hasHistory: r.has_history === true,
        cacheHit: r.cache_hit === true,
        degraded: r.degraded === true,
        ms: int(r.ms),
        llmCalls: int(r.llm_calls),
      };
    case 'semantic_cache':
      return { kind: 'semantic_cache', hit: r.hit === true, skipped: r.skipped === true, ms: int(r.ms) };
    case 'retrieval':
      return {
        kind: 'retrieval',
        iteration: int(r.iteration),
        query: str(r.query),
        embedMs: int(r.embed_ms),
        searchMs: int(r.search_ms),
        rerankMs: int(r.rerank_ms),
        ms: int(r.ms),
        kept: int(r.kept),
        dropped: int(r.dropped),
        neutralized: int(r.neutralized),
        dense: list(r.dense).map(hit),
        bm25: list(r.bm25).map(hit),
        fused: list(r.fused).map(hit),
        reranked: list(r.reranked).map((x) => ({ ...hit(x), fusedRank: num(x.fused_rank) })),
        chunks: list(r.chunks).map((x) => ({ source: str(x.source), score: num(x.score), text: str(x.text), page: num(x.page), heading: str(x.heading) })),
      };
    case 'grade':
      return { kind: 'grade', iteration: int(r.iteration), score: num(r.score), threshold: num(r.threshold), verdict: r.verdict === 'good' ? 'good' : 'poor', next: str(r.next) };
    case 'rewrite':
      return { kind: 'rewrite', previous: str(r.previous), next: str(r.new), model: optStr(r.model), ms: int(r.ms), llmCalls: int(r.llm_calls) };
    case 'decline':
      return { kind: 'decline', reason: str(r.reason) };
    case 'llm':
      return {
        kind: 'llm',
        model: optStr(r.model),
        fallback: r.fallback === true,
        cacheHit: r.cache_hit === true,
        tokensIn: int(r.tokens_in),
        tokensOut: int(r.tokens_out),
        ms: int(r.ms),
        llmCalls: int(r.llm_calls),
        grounded: r.grounded === true,
        contextChunks: int(r.context_chunks),
      };
    case 'output_guard':
      return { kind: 'output_guard', blocked: r.blocked === true, rail: optStr(r.rail), ms: int(r.ms), storedInCache: r.stored_in_cache === true, llmCalls: int(r.llm_calls) };
    default:
      return null;
  }
}

/** null when `v` has no usable steps (an older API, or nothing to show). */
export function parseTrace(v: unknown): Trace | null {
  if (!isRec(v) || !Array.isArray(v.steps)) return null;
  const steps = list(v.steps).flatMap((s) => {
    const p = parseStep(s);
    return p ? [p] : [];
  });
  return steps.length === 0 ? null : { steps, totalMs: int(v.total_ms), llmCalls: int(v.llm_calls) };
}
