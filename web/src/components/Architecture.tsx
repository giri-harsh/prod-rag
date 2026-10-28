import type { NodeId, NodeState } from '../trace/describe';

interface Node {
  id: NodeId;
  label: [string, string?];
  x: number;
  y: number;
  w: number;
  h: number;
}

const LABELS: Record<NodeId, [string, string?]> = {
  guardrail: ['Guardrail', 'pattern + model'],
  plan: ['Intent router', '+ query rewrite'],
  cache: ['Semantic', 'cache'],
  retrieve: ['Hybrid search', 'dense + BM25 → RRF'],
  rerank: ['Rerank', 'cross-encoder'],
  grade: ['Grade', 'good enough?'],
  rewrite: ['Rewrite', 'and retry'],
  llm: ['LLM', 'with fallback'],
  cite: ['Output check', '+ citations'],
};

const MAIN: NodeId[] = ['guardrail', 'plan', 'cache', 'retrieve', 'rerank', 'grade', 'llm', 'cite'];

function wide(): { nodes: Node[]; edges: string[]; w: number; h: number } {
  const nodes: Node[] = MAIN.map((id, i) => ({ id, label: LABELS[id], x: 14 + i * 122, y: 40, w: 108, h: 56 }));
  const grade = nodes.find((n) => n.id === 'grade');
  const retrieve = nodes.find((n) => n.id === 'retrieve');
  if (!grade || !retrieve) throw new Error('layout');
  const rewrite: Node = { id: 'rewrite', label: LABELS.rewrite, x: grade.x, y: 150, w: 108, h: 50 };
  nodes.push(rewrite);
  const edges = nodes
    .slice(0, MAIN.length - 1)
    .map((n, i) => `M${String(n.x + n.w)} ${String(n.y + n.h / 2)} L${String(nodes[i + 1]?.x ?? 0)} ${String(n.y + n.h / 2)}`);
  edges.push(`M${String(grade.x + grade.w / 2)} ${String(grade.y + grade.h)} L${String(rewrite.x + rewrite.w / 2)} ${String(rewrite.y)}`);
  edges.push(`M${String(rewrite.x)} ${String(rewrite.y + rewrite.h / 2)} Q${String(retrieve.x + retrieve.w / 2)} ${String(rewrite.y + rewrite.h / 2)} ${String(retrieve.x + retrieve.w / 2)} ${String(retrieve.y + retrieve.h)}`);
  return { nodes, edges, w: 14 + MAIN.length * 122, h: 215 };
}

function narrow(): { nodes: Node[]; edges: string[]; w: number; h: number } {
  const nodes: Node[] = MAIN.map((id, i) => ({ id, label: LABELS[id], x: 18, y: 12 + i * 66, w: 170, h: 46 }));
  const grade = nodes.find((n) => n.id === 'grade');
  const retrieve = nodes.find((n) => n.id === 'retrieve');
  if (!grade || !retrieve) throw new Error('layout');
  const rewrite: Node = { id: 'rewrite', label: LABELS.rewrite, x: 214, y: grade.y, w: 100, h: 46 };
  nodes.push(rewrite);
  const edges = nodes
    .slice(0, MAIN.length - 1)
    .map((n, i) => `M${String(n.x + n.w / 2)} ${String(n.y + n.h)} L${String(n.x + n.w / 2)} ${String(nodes[i + 1]?.y ?? 0)}`);
  edges.push(`M${String(grade.x + grade.w)} ${String(grade.y + grade.h / 2)} L${String(rewrite.x)} ${String(rewrite.y + rewrite.h / 2)}`);
  edges.push(`M${String(rewrite.x + rewrite.w / 2)} ${String(rewrite.y)} Q${String(rewrite.x + rewrite.w / 2)} ${String(retrieve.y + retrieve.h / 2)} ${String(retrieve.x + retrieve.w)} ${String(retrieve.y + retrieve.h / 2)}`);
  return { nodes, edges, w: 328, h: 12 + MAIN.length * 66 };
}

function Diagram({ variant, states, active }: { variant: 'wide' | 'narrow'; states: Record<NodeId, NodeState>; active: NodeId | null }) {
  const { nodes, edges, w, h } = variant === 'wide' ? wide() : narrow();
  return (
    <svg className={`arch arch-${variant}`} viewBox={`0 0 ${String(w)} ${String(h)}`} role="img" aria-labelledby={`arch-title-${variant}`} focusable="false">
      <title id={`arch-title-${variant}`}>
        Request flow: guardrail, intent router with query rewrite, semantic cache, hybrid dense plus BM25 search fused by RRF, rerank, grade with a rewrite loop, LLM with fallback, output check and citations.
      </title>
      <defs>
        <marker id={`arrow-${variant}`} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0 0 L8 4 L0 8 z" className="arch-arrowhead" />
        </marker>
      </defs>
      {edges.map((d) => (
        <path key={d} d={d} className="arch-edge" markerEnd={`url(#arrow-${variant})`} />
      ))}
      {nodes.map((n) => (
        <g key={n.id} className={`arch-node is-${states[n.id]}${active === n.id ? ' is-active' : ''}`} data-node={n.id}>
          <rect x={n.x} y={n.y} width={n.w} height={n.h} rx="9" />
          <text x={n.x + n.w / 2} y={n.y + (n.label[1] ? n.h / 2 - 3 : n.h / 2 + 5)} textAnchor="middle" className="arch-label">
            {n.label[0]}
          </text>
          {n.label[1] && (
            <text x={n.x + n.w / 2} y={n.y + n.h / 2 + 13} textAnchor="middle" className="arch-sub">
              {n.label[1]}
            </text>
          )}
        </g>
      ))}
    </svg>
  );
}

/** The request flow. Nodes light up as a request passes through them; a red node is where it stopped. */
export function Architecture({ states, active }: { states: Record<NodeId, NodeState>; active: NodeId | null }) {
  return (
    <figure className="arch-fig" data-testid="architecture">
      <Diagram variant="wide" states={states} active={active} />
      <Diagram variant="narrow" states={states} active={active} />
      <figcaption className="muted small">
        Every question takes this path. Green stages ran for the last answer; a red stage is where the system stopped, on purpose.
      </figcaption>
    </figure>
  );
}
