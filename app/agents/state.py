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
