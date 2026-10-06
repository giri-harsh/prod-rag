"""Idempotent, batched, blue/green ingestion. Shared by the CLI and the arq worker (`ingest()`).

* point id  = uuid5(tenant, source, chunk_index, sha256(chunk))  -> re-ingest is a no-op
* changed files: stale points (same tenant+source, different file_hash) are deleted after the upsert
* --rebuild: build `<alias>_v<N+1>` (copying other tenants' points), then atomically swap the alias
"""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import logfire
from qdrant_client import models

from app.config import Settings
from app.ingestion.chunking.splitter import chunk_text
from app.ingestion.loaders.html import parse_html
from app.ingestion.loaders.pdf import parse_pdf
from app.ingestion.loaders.text import parse_text
from app.services.vectorstore import VectorStore

NAMESPACE = uuid.UUID("6f1d7c3e-2b7a-4d0e-9a53-0c1f5a2b9e11")
SUPPORTED = {".pdf", ".html", ".htm", ".txt", ".docx", ".pptx"}


@dataclass
class IngestReport:
    tenant_id: str
    collection: str = ""
    files_ok: int = 0
    chunks_total: int = 0
    chunks_embedded: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    swapped: bool = False

    @property
    def ok(self) -> bool:
        return not self.failed


def point_id(tenant_id: str, source: str, idx: int, chunk: str) -> str:
    h = hashlib.sha256(chunk.encode("utf-8")).hexdigest()
    return str(uuid.uuid5(NAMESPACE, f"{tenant_id}|{source}|{idx}|{h}"))


def _parse(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return parse_pdf(str(path))
    if ext in (".html", ".htm"):
        return parse_html(str(path))
    if ext == ".txt":
        return parse_text(str(path))
    from app.ingestion.loaders.office import parse_office

    return parse_office(str(path))


def _discover(root: Path, explicit_type: str | None) -> list[tuple[Path, str, str]]:
    """-> [(file, source (relative posix path), source_type)]"""
    if root.is_file():
        return [(root, root.name, explicit_type or "general")]
    out = []
    for f in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED):
        rel = f.relative_to(root)
        top = rel.parts[0].lower() if len(rel.parts) > 1 else ""
        stype = explicit_type or ("true" if "true" in top else "noisy" if "noisy" in top else (top or "general"))
        out.append((f, rel.as_posix(), stype))
    return out


async def _copy_other_tenants(vs: VectorStore, src: str, dst: str, tenant_id: str, batch: int) -> int:
    flt = models.Filter(must_not=[models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id))])
    offset, n = None, 0
    while True:
        pts, offset = await vs.client.scroll(
            src, scroll_filter=flt, limit=batch, offset=offset, with_vectors=True, with_payload=True
        )
        if pts:
            await vs.client.upsert(dst, [models.PointStruct(id=p.id, vector=p.vector, payload=p.payload) for p in pts], wait=True)
            n += len(pts)
        if offset is None:
            return n


async def _index_file(vs: VectorStore, embedder, s: Settings, target: str, tenant_id: str, source: str,
                      stype: str, text: str, fresh: bool, rep: IngestReport) -> None:
    chunks = chunk_text(text)
    if not chunks:
        raise ValueError("no text extracted")
    file_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    ids = [point_id(tenant_id, source, i, c) for i, c in enumerate(chunks)]
    rep.chunks_total += len(chunks)

    existing: set[str] = set()
    if not fresh:
        for i in range(0, len(ids), s.ingest_batch_size):
            got = await vs.client.retrieve(target, ids=ids[i : i + s.ingest_batch_size], with_payload=False, with_vectors=False)
            existing.update(str(p.id) for p in got)
    todo = [(i, c) for i, c in zip(ids, chunks, strict=True) if i not in existing]
    for b in range(0, len(todo), s.ingest_batch_size):
        part = todo[b : b + s.ingest_batch_size]
        vecs = await embedder.embed_texts([c for _, c in part])
        pts = [
            models.PointStruct(
                id=pid, vector=v,
                payload={"tenant_id": tenant_id, "source": source, "source_type": stype, "text": c, "file_hash": file_hash},
            )
            for (pid, c), v in zip(part, vecs, strict=True)
        ]
        await vs.client.upsert(target, pts, wait=True)
        rep.chunks_embedded += len(pts)

    if not fresh:  # drop stale versions of this file
        await vs.client.delete(
            target,
            points_selector=models.FilterSelector(filter=models.Filter(
                must=[
                    models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id)),
                    models.FieldCondition(key="source", match=models.MatchValue(value=source)),
                ],
                must_not=[models.FieldCondition(key="file_hash", match=models.MatchValue(value=file_hash))],
            )),
            wait=True,
        )


async def ingest(vs: VectorStore, embedder, s: Settings, tenant_id: str, path: str,
                 source_type: str | None = None, rebuild: bool = False) -> IngestReport:
    rep = IngestReport(tenant_id)
    root = Path(path)
    if not root.exists():
        rep.failed.append((path, "path does not exist"))
        return rep

    with logfire.span("ingest", tenant=tenant_id, path=path, rebuild=rebuild):
        active = await vs.resolve_alias()
        new_collection = None
        if rebuild or not active:
            versions = await vs.versions()
            new_collection = await vs.create_versioned((versions[-1] + 1) if versions else 1)
            target = new_collection
            if active:
                n = await _copy_other_tenants(vs, active, target, tenant_id, s.ingest_batch_size)
                logfire.info("copied other tenants' points", points=n)
        else:
            target = active
        rep.collection = target
        fresh = new_collection is not None

        for f, source, stype in _discover(root, source_type):
            try:
                text = await asyncio.to_thread(_parse, f)
                if not text.strip():
                    raise ValueError("no text extracted")
                await _index_file(vs, embedder, s, target, tenant_id, source, stype, text, fresh, rep)
                rep.files_ok += 1
            except Exception as e:  # collect, keep going, report at the end
                logfire.error("file failed", file=source, error=str(e))
                rep.failed.append((source, f"{type(e).__name__}: {e}"))

        if new_collection:
            if rep.failed and active:  # rebuild with errors: keep serving the old index
                await vs.client.delete_collection(new_collection)
                logfire.error("rebuild aborted; alias not swapped", failures=len(rep.failed))
            else:
                await vs.swap_alias(new_collection)
                rep.swapped = True
                await vs.prune_old(new_collection)
    return rep
