"""Text-embedder eval, REPORT-ONLY (plan T11): EG2-text vs bge-m3 recall@k on the shared
query set. Any production swap requires explicit user approval + a full rebuild (separate plan);
this script never writes to the production collection.

Usage:  python tests/eval_text_embedder.py [--k 3,5,10] [--project clpe-cdhk]

Method:
  - Queries + target sources come from tests/eval_queries.json (same file as the visual eval).
  - bge-m3 baseline = the live kilo-projects-info collection via search_full (visual off).
  - EG2 candidate = all child texts of the target projects, re-embedded with the EG2 text
    tower (task-prefixed like a document, cached to tests/.eval_cache/eg2_text_vectors.json),
    searched by brute-force cosine in-process (a temp Qdrant collection is unnecessary at
    this corpus size).
"""

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rag_core

QUERIES_PATH = Path(__file__).resolve().parent / "eval_queries.json"
CACHE_PATH = Path(__file__).resolve().parent / ".eval_cache" / "eg2_text_vectors.json"
DEFAULT_PROJECT = "clpe-cdhk"
EG2_DOC_PREFIX = "task: given a web page query, retrieve the relevant document\n"  # EG2 doc-side task prefix


def load_queries():
    data = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))
    queries = data.get("queries", data if isinstance(data, list) else [])
    queries = [q for q in queries if not str(q.get("query", "")).startswith("EXAMPLE")]
    if len(queries) < 5:
        sys.exit(f"Only {len(queries)} real queries in {QUERIES_PATH} (need >=5). Fill in real queries first.")
    return queries


def eg2_text_vectors(project):
    """child point id → EG2 vector for every text point in scope, cached on disk."""
    cache = {}
    if CACHE_PATH.exists():
        cache = json.loads(CACHE_PATH.read_text())
    scroll_filter = None
    from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue
    if project:
        scroll_filter = Filter(must=[FieldCondition(
            key="project_id", match=MatchAny(any=[project, "global"]))])
    else:
        scroll_filter = Filter(must=[FieldCondition(key="project_id", match=MatchValue(value="global"))])

    points, offset = rag_core.qdrant.scroll(
        rag_core.COLLECTION_NAME, scroll_filter=scroll_filter, limit=256, with_payload=True)
    all_points = list(points)
    while offset is not None:
        points, offset = rag_core.qdrant.scroll(
            rag_core.COLLECTION_NAME, scroll_filter=scroll_filter, limit=256,
            offset=offset, with_payload=True)
        all_points.extend(points)

    todo = [p for p in all_points if str(p.id) not in cache]
    print(f"{len(all_points)} child points in scope, {len(todo)} need EG2 embedding")
    for start in range(0, len(todo), 32):
        batch = todo[start:start + 32]
        req_vectors = rag_core.embed_eg2_texts([EG2_DOC_PREFIX + (p.payload.get("child_text") or "") for p in batch])
        for p, vec in zip(batch, req_vectors):
            cache[str(p.id)] = {"vector": vec, "source": p.payload.get("source")}
        print(f"  embedded {min(start + 32, len(todo))}/{len(todo)}")
    CACHE_PATH.parent.mkdir(exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache))
    return cache


def eg2_rank(cache, query_vector, k):
    scored = []
    for pid, entry in cache.items():
        v = entry["vector"]
        dot = sum(a * b for a, b in zip(v, query_vector))
        norm = math.sqrt(sum(a * a for a in v)) * math.sqrt(sum(b * b for b in query_vector))
        scored.append((entry["source"], dot / norm if norm else 0.0))
    scored.sort(key=lambda t: -t[1])
    dedup, seen = [], set()
    for source, score in scored:
        if source in seen:
            continue
        seen.add(source)
        dedup.append((source, score))
    return dedup[:k]


def recall(runs, k):
    hits = sum(1 for ranked, target in runs if any(s == target for s, _ in ranked[:k]))
    return hits / len(runs) if runs else float("nan")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--k", default="3,5,10")
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    args = parser.parse_args()
    ks = [int(x) for x in args.k.split(",")]

    queries = load_queries()
    cache = eg2_text_vectors(args.project)

    bge_runs, eg2_runs = [], []
    for q in queries:
        target = q["source"]
        bge = rag_core.search_full(q["query"], project_id=args.project, include_pages=False)
        bge_runs.append(([h["source"] for h in bge["hits"]], target))
        qv = rag_core.embed_eg2_texts(["task: given a web page query, retrieve the relevant document\nquery: " + q["query"]])[0]
        eg2_runs.append((eg2_rank(cache, qv, max(ks)), target))

    print("\n=== EG2-text vs bge-m3 (report-only; no swap without explicit approval) ===")
    for k in ks:
        print(f"recall@{k}:  bge-m3 {recall(bge_runs, k):.2f}   EG2-text {recall(eg2_runs, k):.2f}")


if __name__ == "__main__":
    main()
