# Visual Retrieval (Page-as-Image, EmbeddingGemma 2)

Page-image semantic retrieval runs **alongside** the text RAG: every approved PDF's pages are
rendered to PNGs and embedded with Ollama `embeddinggemma-2` (EG2, 768d multimodal); searches
fuse the text ranking and the page ranking with Reciprocal Rank Fusion (k=60). The text
pipeline (bge-m3, Parent-Child, ref-ID matching, human review gate) is untouched.

**T0 decision (2026-10-18)**: Ollama `/api/embed` *does* embed images for `embeddinggemma-2`,
but only in object input form — each entry carries its own media:
`{"model": "embeddinggemma-2", "input": [{"image": "<b64>"}]}`. A top-level `"images"` field
(like `/api/chat` uses) is silently ignored by the embed endpoints, and an empty-string input
is rejected. No host microservice is needed.

## Indexing pages

A PDF is page-indexed only after its conversion md is approved into `my_shared_docs` (same
human-approval gate as text ingestion; the source PDF must still be in `pdf_inbox`):

```bash
python index_pages.py            # all approved PDFs   (--rebuild recreates the collection)
```

or via the API/web UI: job type `index_pages` (file / project / all scopes) — the project page
shows an **Index pages** button once an approved PDF exists.

- PNGs live in `pages/{project_id}/{stem}/page-{nnnn}.png` (render scale 2.0, pypdfium2).
- Page points go to the separate Qdrant collection `kilo-projects-pages` with deterministic
  IDs `uuid5("{project_id}:{filename}:page{n}")` — reruns are idempotent, and an embedder
  failure never destroys existing index state (delete happens only after successful embedding).
- Deleting an approved document (or a project) purges its page points **and** PNG dir.

## Searching

`search_full` (API `POST /api/search`, MCP `search_shared_knowledge`) gains an optional visual
branch, on by default (`ENABLE_VISUAL=0` disables; per-request `include_pages` overrides —
the MCP flag defaults to **off** to keep that contract stable):

- Responses carry a new `"pages"` list: `{project_id, source, page_number, score, page_image}`
  (score = raw page-image cosine; order = RRF-fused with the text ranking per source).
- The text `hits` keep their exact structure; exact ref hits always stay first — fusion only
  reorders the vector-ranked fill.
- EG2/Ollama down or no pages indexed → text-only results plus a note (never an error).

The web UI shows page matches as a **Page matches** section with clickable thumbnails
(served path-safe from `GET /api/projects/{pid}/pages/{stem}/{page-0001.png}`).

## Eval (ground truth required)

`tests/eval_queries.json` holds user-supplied ground truth (20–30 queries; drawing/table
focus; format documented in the file). With real queries in place:

```bash
python tests/eval_visual.py        # T10: text-only vs hybrid recall@k + image-only vs interleaved A/B (gate)
python tests/eval_text_embedder.py # T11: EG2-text vs bge-m3 recall@k — REPORT-ONLY, no swap without approval
```

Full-corpus backfill (`index_pages` over all projects) is gated on the T10 eval passing.
