"""Visual-retrieval eval: text-only vs hybrid (EG2 page-image) recall@k, plus the
image-only vs image+page-text interleaved page-embedding A/B (plan T10).

Usage:  python tests/eval_visual.py [--k 3,5,10] [--project clpe-cdhk]

Ground truth comes from tests/eval_queries.json (see its _README; the user supplies
20-30 real queries). Only sources referenced there are page-indexed, into throwaway
collections — the production kilo-projects-pages collection is never touched.

GATE (exit code 1 on failure): hybrid page-recall@k >= text-only page-recall@k on
visual queries (text-only has no page output, so its page recall is 0 by definition —
the meaningful comparison is against its source-level recall) AND no text-recall
regression on text queries. The verdict is printed as a comparison report.

The interleaved variant embeds {"text": <page md/text>, "image": <png>} per page
(EG2 8K context) into its own eval collection and reports the same page-recall
metrics — it stays an eval variant unless it clearly beats image-only.
"""

import argparse
import base64
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rag_core

QUERIES_PATH = Path(__file__).resolve().parent / "eval_queries.json"
PAGE_TEXT_CHARS = 6000  # page text fed to the interleaved variant (fits EG2 8K context)
DEFAULT_PROJECT = "clpe-cdhk"  # overridden by --project


def load_queries():
    data = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))
    queries = data.get("queries", data if isinstance(data, list) else [])
    queries = [q for q in queries if not str(q.get("query", "")).startswith("EXAMPLE")]
    if len(queries) < 5:
        sys.exit(
            f"Only {len(queries)} real queries in {QUERIES_PATH} (need >=5, aim 20-30).\n"
            "Fill in real ground-truth queries first — see the file's _README key."
        )
    return queries


def page_text(pdf_path, page_number):
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        return pdf[page_number - 1].get_textpage().get_text_range()
    finally:
        pdf.close()


def index_eval_collection(collection, sources, interleaved):
    """Index the given (project_id, filename) PDFs into a throwaway collection.
    interleaved=False → image-only entries; True → {"text": page text, "image": png}."""
    old = rag_core.PAGE_COLLECTION_NAME
    rag_core.PAGE_COLLECTION_NAME = collection
    try:
        if rag_core.qdrant.collection_exists(collection):
            rag_core.qdrant.delete_collection(collection)
        rag_core.ensure_page_collection()
        for project_id, filename in sorted(sources):
            rag_core.index_pages_file(project_id, filename)
        if interleaved:
            # re-embed every page of these sources with text+image interleaved
            for project_id, filename in sorted(sources):
                pdf_path = os.path.join(rag_core.project_dir(rag_core.PDF_INBOX_DIR, project_id), filename)
                stem = os.path.splitext(filename)[0]
                _reembed_interleaved(collection, project_id, filename, pdf_path, stem)
    finally:
        rag_core.PAGE_COLLECTION_NAME = old


def _reembed_interleaved(collection, project_id, filename, pdf_path, stem):
    import pypdfium2 as pdfium
    from qdrant_client.models import PointStruct

    png_dir = rag_core.pages_png_dir(project_id, stem)
    pages = sorted(p for p in os.listdir(png_dir) if p.endswith(".png"))
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        for start in range(0, len(pages), rag_core.PAGE_EMBED_BATCH):
            batch = pages[start:start + rag_core.PAGE_EMBED_BATCH]
            inputs = []
            for png in batch:
                n = int(png[len("page-"):-len(".png")])
                with open(os.path.join(png_dir, png), "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("ascii")
                text = page_text(pdf_path, n)[:PAGE_TEXT_CHARS]
                inputs.append({"text": text, "image": b64})
            vectors = rag_core.embed_page_images(inputs)
            points = []
            for png, vector in zip(batch, vectors):
                n = int(png[len("page-"):-len(".png")])
                points.append(PointStruct(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{project_id}:{filename}:page{n}")),
                    vector=vector,
                    payload={
                        "project_id": project_id, "source": filename, "page_number": n,
                        "page_image": rag_core.page_png_relpath(project_id, stem, n),
                    },
                ))
            rag_core.qdrant.upsert(collection_name=collection, points=points)
            print(f"  interleaved {project_id}/{filename}: {min(start + len(batch), len(pages))}/{len(pages)}")
    finally:
        pdf.close()


def recall_at_k(runs, k):
    """runs: list of ranked "hit descriptors"; returns fraction of runs whose top-k contains
    a target match. Descriptors are ('page', source, page) or ('source', source)."""
    hits = 0
    for ranked, target in runs:
        topk = ranked[:k]
        if target[0] == "page":
            found = any(t[0] == "page" and t[1] == target[1] and t[2] == target[2] for t in topk)
        else:
            found = any(t[1] == target[1] for t in topk)
        hits += found
    return hits / len(runs) if runs else float("nan")


def evaluate_mode(queries, include_pages, page_collection):
    """One eval pass. Returns runs grouped for the metrics."""
    old = rag_core.PAGE_COLLECTION_NAME
    if page_collection:
        rag_core.PAGE_COLLECTION_NAME = page_collection
    try:
        visual_page_runs, text_runs_visual_on, text_runs_visual_off = [], [], []
        for q in queries:
            project = q.get("project_id") or DEFAULT_PROJECT
            if q["kind"] == "visual":
                res = rag_core.search_full(q["query"], project_id=project, include_pages=include_pages)
                ranked = [("page", p["source"], p["page_number"]) for p in res["pages"]]
                visual_page_runs.append((ranked, ("page", q["source"], q.get("page"))))
            else:
                off = rag_core.search_full(q["query"], project_id=project, include_pages=False)
                on = rag_core.search_full(q["query"], project_id=project, include_pages=include_pages)
                target = ("source", q["source"])
                text_runs_visual_off.append(([("source", h["source"]) for h in off["hits"]], target))
                text_runs_visual_on.append(([("source", h["source"]) for h in on["hits"]], target))
        return {
            "visual_page": visual_page_runs,
            "text_off": text_runs_visual_off,
            "text_on": text_runs_visual_on,
        }
    finally:
        rag_core.PAGE_COLLECTION_NAME = old


def report(name, k, metrics):
    print(f"\n=== {name} ===")
    if metrics["visual_page"]:
        print(f"  visual queries page-recall@{k}:  {recall_at_k(metrics['visual_page'], k):.2f}")
    if metrics["text_off"]:
        print(f"  text queries source-recall@{k} (visual off): {recall_at_k(metrics['text_off'], k):.2f}")
    if metrics["text_on"]:
        print(f"  text queries source-recall@{k} (visual on):  {recall_at_k(metrics['text_on'], k):.2f}")


def main():
    global DEFAULT_PROJECT
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--k", default="3,5,10", help="comma-separated k values (default 3,5,10)")
    parser.add_argument("--project", default=DEFAULT_PROJECT, help="default project_id for queries")
    args = parser.parse_args()
    ks = [int(x) for x in args.k.split(",")]
    DEFAULT_PROJECT = args.project

    queries = load_queries()
    sources = {(q.get("project_id") or DEFAULT_PROJECT, q["source"]) for q in queries
               if q["source"].lower().endswith(".pdf")}
    print(f"{len(queries)} queries, {len(sources)} distinct PDF sources")

    print("Indexing eval page collections (image-only, then interleaved)…")
    index_eval_collection("eval-pages-img", sources, interleaved=False)
    index_eval_collection("eval-pages-itx", sources, interleaved=True)

    text_only = evaluate_mode([q for q in queries if q["kind"] == "text"], include_pages=False, page_collection=None)
    hybrid_img = evaluate_mode(queries, include_pages=True, page_collection="eval-pages-img")
    hybrid_itx = evaluate_mode(queries, include_pages=True, page_collection="eval-pages-itx")

    gate_ok = True
    for k in ks:
        print(f"\n########## k={k} ##########")
        report("text-only (baseline)", k, text_only)
        report("hybrid: page-image EG2 (image-only embedding)", k, hybrid_img)
        report("hybrid A/B: image+page-text interleaved", k, hybrid_itx)

        if hybrid_img["visual_page"] and recall_at_k(hybrid_img["visual_page"], k) <= 0:
            print("  GATE FAIL: hybrid returns no correct pages on visual queries")
            gate_ok = False
        if text_only["text_off"] and hybrid_img["text_on"]:
            if recall_at_k(hybrid_img["text_on"], k) < recall_at_k(text_only["text_off"], k):
                print("  GATE FAIL: text recall regression with visual fusion on")
                gate_ok = False

    print("\nVerdict:", "PASS" if gate_ok else "FAIL",
          "(gate: hybrid must return correct pages on visual queries with no text-recall regression)")
    sys.exit(0 if gate_ok else 1)


if __name__ == "__main__":
    main()
