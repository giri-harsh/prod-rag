import operator
from typing import Annotated, TypedDict


class AgentState(TypedDict, total=False):
    # history is append-only (checkpointed per server-derived thread id)
    messages: Annotated[list[dict], operator.add]
    user_text: str
    search_query: str | None  # None => conversational (no retrieval)
    documents: list[str]
    sources: list[str]
    plan: list[str]
    status: str
    draft_answer: str
    final_answer: str
    blocked: bool
    block_rail: str | None
    no_context: bool
    output_checked: bool
    iteration: int
    top_score: float | None
    standalone: bool
    cache_hit: bool
    cache_query: str | None
    # Per-request explanation of what each stage did (see graph.py). Replaced, not appended, every turn: the checkpoint
    # keeps one thread's state across turns and a trace must describe only the current request.
    trace: list[dict]
    want_trace: bool  # also compute the extra retrieval views (dense-only / sparse-only lists) that cost two more Qdrant calls
