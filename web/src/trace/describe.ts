import type { Trace, TraceStep } from '../api/trace';

export type StepKind = TraceStep['kind'];

/** Plain-English copy for each stage: what it is, and why a reader should care. */
export const STEP_INFO: Record<StepKind, { title: string; why: string }> = {
  guardrail: {
    title: 'Guardrail',
    why: 'Checks the question before anything expensive happens. Known attack phrases are stopped by pattern matching with no AI call at all; everything else goes past a small classifier that also enforces the topic.',
  },
  telemetry_redaction: {
    title: 'Privacy scrub for logs',
    why: 'Emails, phone numbers, card numbers and keys are masked before the question is written to logs or monitoring. The answer path itself is not altered.',
  },
  plan: {
    title: 'Intent and query rewrite',
    why: 'Decides whether the message needs a document search, and rewrites follow-ups ("what about nodes?") into a standalone search query using the conversation so far.',
  },
  semantic_cache: {
    title: 'Semantic cache',
    why: 'If a near-identical question was already answered, the stored answer is returned instantly. No search and no AI call, so it costs nothing and cannot drift.',
  },
  retrieval: {
    title: 'Hybrid retrieval and rerank',
    why: 'Two searches run side by side: by meaning (dense vectors) and by exact words (BM25). Their rankings are merged (RRF), then a reranker re-reads the best candidates against the question. Exact terms like config keys need the word search.',
  },
  grade: {
    title: 'Grader',
    why: 'Judges whether the retrieved text is relevant enough to answer from. If not, the system rewrites the query and tries again instead of guessing.',
  },
  rewrite: {
    title: 'Self-correcting rewrite',
    why: 'The first search was weak, so a small model rewrites the query with more precise keywords and the loop retrieves again.',
  },
  decline: {
    title: 'Honest decline',
    why: 'Nothing relevant enough was found, so the system says so instead of inventing an answer. No answer model is called.',
  },
  llm: {
    title: 'Answer model',
    why: 'Writes the answer using only the retrieved passages. If the first model or key is rate-limited it automatically fails over to a fallback.',
  },
  output_guard: {
    title: 'Output check',
    why: 'Screens the finished answer for leaked secrets, prompt leakage and policy problems before it reaches the user.',
  },
};

/** The nodes of the architecture diagram, in flow order. */
export const NODE_IDS = ['guardrail', 'plan', 'cache', 'retrieve', 'rerank', 'grade', 'rewrite', 'llm', 'cite'] as const;
export type NodeId = (typeof NODE_IDS)[number];
export type NodeState = 'idle' | 'lit' | 'stop';

/** Which diagram nodes this trace actually passed through, and where it stopped. */
export function nodeStates(trace: Trace | null): Record<NodeId, NodeState> {
  const s = Object.fromEntries(NODE_IDS.map((n) => [n, 'idle'])) as Record<NodeId, NodeState>;
  if (!trace) return s;
  for (const step of trace.steps) {
    switch (step.kind) {
      case 'guardrail':
        s.guardrail = step.blocked ? 'stop' : 'lit';
        break;
      case 'plan':
        s.plan = 'lit';
        break;
      case 'semantic_cache':
        s.cache = step.hit ? 'lit' : s.cache === 'idle' ? 'lit' : s.cache;
        break;
      case 'retrieval':
        s.retrieve = 'lit';
        s.rerank = 'lit';
        break;
      case 'grade':
        s.grade = step.verdict === 'good' ? 'lit' : 'stop';
        break;
      case 'rewrite':
        s.rewrite = 'lit';
        break;
      case 'decline':
        s.grade = 'stop';
        break;
      case 'llm':
        s.llm = 'lit';
        break;
      case 'output_guard':
        s.cite = step.blocked ? 'stop' : 'lit';
        break;
    }
  }
  // a poor grade that was followed by a good one is a recovery, not a stop
  const grades = trace.steps.filter((x) => x.kind === 'grade');
  if (grades.at(-1)?.verdict === 'good') s.grade = 'lit';
  return s;
}

export interface WaterfallRow {
  id: string;
  label: string;
  start: number;
  ms: number;
  parallel: boolean;
}

/**
 * Per-stage latency as bars on one time axis. The guardrail and the planner run at the same time, so they share a start; every
 * later stage starts when the previous one ends. Time the trace does not account for (checkpointing, queueing) is shown as "other".
 */
export function waterfall(trace: Trace): { rows: WaterfallRow[]; total: number } {
  const rows: WaterfallRow[] = [];
  const guard = trace.steps.find((x) => x.kind === 'guardrail');
  const plan = trace.steps.find((x) => x.kind === 'plan');
  if (guard) rows.push({ id: 'guardrail', label: 'Guardrail', start: 0, ms: guard.ms, parallel: plan !== undefined });
  if (plan) rows.push({ id: 'plan', label: 'Plan + rewrite', start: 0, ms: plan.ms, parallel: guard !== undefined });
  let cursor = Math.max(guard?.ms ?? 0, plan?.ms ?? 0);
  for (const [i, step] of trace.steps.entries()) {
    let label: string | null = null;
    let ms = 0;
    if (step.kind === 'semantic_cache') ({ label, ms } = { label: 'Semantic cache', ms: step.ms });
    else if (step.kind === 'retrieval') ({ label, ms } = { label: `Retrieval #${String(step.iteration)}`, ms: step.ms });
    else if (step.kind === 'rewrite') ({ label, ms } = { label: 'Query rewrite', ms: step.ms });
    else if (step.kind === 'llm') ({ label, ms } = { label: 'Answer model', ms: step.ms });
    else if (step.kind === 'output_guard') ({ label, ms } = { label: 'Output check', ms: step.ms });
    if (label === null) continue;
    rows.push({ id: `${step.kind}-${String(i)}`, label, start: cursor, ms, parallel: false });
    cursor += ms;
  }
  const total = Math.max(trace.totalMs, cursor, 1);
  const other = total - cursor;
  if (other >= Math.max(5, total * 0.03)) rows.push({ id: 'other', label: 'Other (state save, queueing)', start: cursor, ms: other, parallel: false });
  return { rows, total };
}

/** One honest sentence for the top of the panel. */
export function summarize(trace: Trace): string {
  const guard = trace.steps.find((x) => x.kind === 'guardrail');
  if (guard?.blocked === true) {
    return trace.llmCalls === 0
      ? `Stopped at the guardrail in ${String(guard.ms)} ms with zero AI calls.`
      : `Stopped at the guardrail in ${String(guard.ms)} ms (${String(trace.llmCalls)} small-model call${trace.llmCalls === 1 ? '' : 's'}).`;
  }
  const retrievals = trace.steps.filter((x) => x.kind === 'retrieval').length;
  const declined = trace.steps.some((x) => x.kind === 'decline');
  const cached = trace.steps.some((x) => x.kind === 'semantic_cache' && x.hit);
  const parts = [
    cached ? 'Served from the semantic cache' : declined ? 'Declined: nothing relevant in the documents' : 'Answered from the documents',
    retrievals > 0 ? `${String(retrievals)} search pass${retrievals === 1 ? '' : 'es'}` : null,
    `${String(trace.llmCalls)} AI call${trace.llmCalls === 1 ? '' : 's'}`,
    `${String(trace.totalMs)} ms`,
  ].filter((x): x is string => x !== null);
  return `${parts.join(' · ')}.`;
}

/** Document titles from the dense list that the keyword (BM25) search did not return, and vice versa: what each side contributed. */
export function branchContribution(dense: { source: string }[], bm25: { source: string }[]): { denseOnly: string[]; bm25Only: string[]; both: string[] } {
  const d = new Set(dense.map((x) => x.source));
  const b = new Set(bm25.map((x) => x.source));
  return {
    denseOnly: [...d].filter((x) => !b.has(x)),
    bm25Only: [...b].filter((x) => !d.has(x)),
    both: [...d].filter((x) => b.has(x)),
  };
}

/** Rows whose rank moved when the reranker re-read them: positive = moved up. */
export function rankMoves(reranked: { fusedRank: number | null }[]): number[] {
  return reranked.map((r, i) => (r.fusedRank === null ? 0 : r.fusedRank - (i + 1)));
}
