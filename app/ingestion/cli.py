"""python -m app.ingestion.cli DATA/true_data --tenant acme [--source-type true] [--rebuild]

Exit code is non-zero if any file failed.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from app.config import load_settings
from app.ingestion.pipeline import ingest
from app.resilience import Breakers
from app.services.embedding import GeminiEmbedder
from app.services.vectorstore import VectorStore, make_client


async def _run(a) -> int:
    s = load_settings()
    b = Breakers(s.breaker_failures, s.breaker_reset_s)
    client = make_client(s)
    try:
        vs = VectorStore(client, s, b)
        rep = await ingest(vs, GeminiEmbedder(s, b), s, a.tenant, a.path, a.source_type, a.rebuild)
    finally:
        await client.close()
    print(f"collection={rep.collection} files_ok={rep.files_ok} chunks={rep.chunks_total} "
          f"embedded={rep.chunks_embedded} swapped={rep.swapped} failed={len(rep.failed)}")
    for name, err in rep.failed:
        print(f"  FAILED {name}: {err}", file=sys.stderr)
    return 0 if rep.ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="app.ingestion.cli")
    ap.add_argument("path")
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--source-type")
    ap.add_argument("--rebuild", action="store_true", help="blue/green rebuild + atomic alias swap")
    return asyncio.run(_run(ap.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
