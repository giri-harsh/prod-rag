"""Recursive, heading-aware chunker with overlap.

Hierarchy: headings -> paragraphs (blank lines) -> lines -> sentences -> words.
Sizes are in *tokens*, estimated as chars/4 (no tokenizer dependency; accurate enough for sizing).
Each chunk carries `heading_path` and `page` metadata.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

CHARS_PER_TOKEN = 4
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_NUM_HEADING = re.compile(r"^(\d+(?:\.\d+){0,4})[.)]?\s+([A-Z][^\n]{2,80})$")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


@dataclass
class Chunk:
    text: str
    page: int | None
    heading_path: str
    index: int = 0


def est_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def _heading(block: str) -> tuple[int, str] | None:
    """Return (level, title) if the whole block is a single heading line."""
    if "\n" in block.strip() or len(block) > 120:
        return None
    line = block.strip()
    if m := _MD_HEADING.match(line):
        return len(m.group(1)), m.group(2)
    if m := _NUM_HEADING.match(line):
        return m.group(1).count(".") + 1, line
    return None


def _hard_split(text: str, max_chars: int) -> list[str]:
    """Split an oversized block: lines -> sentences -> words -> characters."""
    for sep in ("\n", None):
        parts = text.split("\n") if sep == "\n" else _SENTENCE.split(text)
        if len(parts) > 1:
            out, cur = [], ""
            for p in parts:
                if len(p) > max_chars:
                    if cur:
                        out.append(cur)
                        cur = ""
                    out.extend(_hard_split(p, max_chars))
                    continue
                joiner = "\n" if sep == "\n" else " "
                if cur and len(cur) + len(joiner) + len(p) > max_chars:
                    out.append(cur)
                    cur = p
                else:
                    cur = f"{cur}{joiner}{p}" if cur else p
            if cur:
                out.append(cur)
            return out
    words, out, cur = text.split(), [], ""  # single long line: split on words, then raw characters
    for w in words:
        if len(w) > max_chars:
            if cur:
                out.append(cur)
                cur = ""
            out.extend(w[i : i + max_chars] for i in range(0, len(w), max_chars))
        elif cur and len(cur) + 1 + len(w) > max_chars:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        out.append(cur)
    return out


def _overlap_tail(text: str, overlap_chars: int) -> str:
    if overlap_chars <= 0 or len(text) <= overlap_chars:
        return ""
    tail = text[-overlap_chars:]
    cut = re.search(r"[\s]", tail)  # start on a word boundary
    return tail[cut.end() :] if cut else tail


def split_pages(
    pages: list[tuple[int | None, str]],
    target_tokens: int = 900,
    overlap_ratio: float = 0.15,
    min_tokens: int = 200,
) -> list[Chunk]:
    max_chars = target_tokens * CHARS_PER_TOKEN
    overlap_chars = int(max_chars * overlap_ratio)
    min_chars = min_tokens * CHARS_PER_TOKEN
    chunks: list[Chunk] = []
    stack: list[tuple[int, str]] = []  # (level, title)

    cur, cur_page, cur_path, carry = "", None, "", ""

    def path() -> str:
        return " > ".join(t for _, t in stack)

    def flush():
        nonlocal cur, carry
        if cur.strip():
            chunks.append(Chunk(cur.strip(), cur_page, cur_path))
            carry = _overlap_tail(cur.strip(), overlap_chars)
        cur = ""

    for page, text in pages:
        for block in re.split(r"\n\s*\n", text):
            block = block.strip()
            if not block:
                continue
            if h := _heading(block):
                level, title = h
                if len(cur) >= min_chars:  # headings start a new chunk once the current one is big enough
                    flush()
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, title))
                if not cur:
                    cur_page, cur_path = page, path()
                cur = f"{cur}\n\n{block}" if cur else block
                continue
            pieces = _hard_split(block, max_chars) if len(block) > max_chars else [block]
            for piece in pieces:
                if cur and len(cur) + 2 + len(piece) > max_chars:
                    flush()
                if not cur:
                    cur_page, cur_path = page, path()
                    cur = f"{carry}\n\n{piece}" if carry and chunks else piece
                else:
                    cur = f"{cur}\n\n{piece}"
    flush()
    for i, c in enumerate(chunks):
        c.index = i
    return chunks


def chunk_text(text: str, chunk_size: int = 1500) -> list[str]:  # legacy helper kept for the old API/notebooks
    return [c.text for c in split_pages([(None, text)], target_tokens=max(1, chunk_size // CHARS_PER_TOKEN), overlap_ratio=0.0)]
