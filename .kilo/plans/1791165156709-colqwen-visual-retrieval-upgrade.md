# ColQwen2.5 Page-as-Image Visual Retrieval Upgrade Plan

## Goal

Add page-image semantic retrieval (ColQwen late-interaction) alongside the existing text RAG, fused by RRF, 100% local on the Mac host. Existing text pipeline (bge-m3, Parent-Child, ref-ID exact matching, human review gate) is untouched — this is a pure addition.

## Non-goals

- Replacing the bge-m3 text index or ref-ID / compliance features
- Any external API (GLM / Cohere) — all-local by decision
- VLM answer-generation from page images (future work)
- Custom Col training on a 7B base (Phase 6, gated on eval; see Blind spot #1)

## Current state (verified facts)

- Pipeline: `pdf_inbox` → Docling → `md_review` (human approve) → `my_shared_docs` → bge-m3 Parent-Child → Qdrant `kilo-projects-info` (`rag_core.py`)
- VL captions: Ollama `qwen3.8:27b-mlx`, prompt `"Describe this image:"` (`describe_image()`, `replace_image_placeholders()`), replaced by `[Image]: ...` text before chunking
- Corpus: ~10 PDFs (largest 536 pages), est. ~1.5–2.5k pages total
- Docker: `qdrant/qdrant:latest` (unpinned), `api` (builds repo), `web` (port 3300); Ollama on host via `host.docker.internal`
- `api/jobs.py`: in-process FIFO single worker, job types `convert` | `ingest`; `blocks_document_delete()` guards deletes
- Deletes: `api/app.py` `delete_document` / `delete_project` call `rag_core.delete_points()`

## Blind spots identified & how each is handled

1. **"ColQwen2.5-7B" has no official Col weights** — official `vidore/colqwen2.5-v1.0` is Qwen2.5-VL-**3B** based. A 7B base requires custom Col training (out of scope). Plan deploys 3B official first; 7B/community checkpoints only in Phase 6 after eval proves value. **Needs user sign-off.**
2. **macOS Docker has no GPU/Metal passthrough** — the embedder service runs on the host venv (same pattern as Ollama), reached from the `api` container via `host.docker.internal` + new `COLQWEN_HOST` env.
3. **colpali-engine defaults to flash-attention-2 (NVIDIA-only)** — on MPS must set `attn_implementation="sdpa"` (fallback `eager`); pin `torch` in a separate requirements file.
4. **`qdrant:latest` unpinned; multivector `MAX_SIM` + prefetch/rerank Query API need Qdrant ≥ 1.10 and a matching `qdrant-client`** — pin both; add a runtime capability assert in `ensure_page_collection()`.
5. **Page PNGs in a new `./pages/` dir** — must be bind-mounted into the `api` container (compose volume) and served through a path-safe endpoint (reuse `safe_file_path()` pattern). Add `pages/` to `.gitignore`; do NOT add to `.dockerignore`.
6. **Delete lifecycle** — `delete_document` / `delete_project` must also purge page-collection points and the page PNG dir; `blocks_document_delete()` must also cover the new `index_pages` job type.
7. **Query-side latency** — ColQwen query encoding ~200–500 ms on MPS per search; acceptable, no caching. Visual branch degrades gracefully to text-only if the embedder service is down (note appended to `notes`).
8. **Page hits carry no ref IDs / parents** — they do not participate in exact-ref matching or `referenced_by`. Returned as a separate `pages` list in API responses; text `hits` semantics unchanged.
9. **Re-index consistency** — point ID `uuid5("{project_id}:{filename}:page{n}")`, delete-then-upsert per file (mirrors `ingest_file`); re-index re-embeds the whole file, idempotent.
10. **MPS batch memory** — embed batches of 1–4 pages, images resized ~768 px longest side; indexing 2–5 s/page → full corpus ~1–3 h, run as background job.
11. **Eval ground truth** — 20–30 drawing/table queries with target page must come from the user; full backfill is gated on eval pass (Phase 5).
12. **Storage** — ~768 patches × 128 dim × int8 ≈ ~100 KB/page → a few hundred MB worst case; non-issue.

## Tasks (ordered)

### Phase 0 — Preflight & quick wins
- [ ] T1 Pin Qdrant image (≥1.15) in `docker-compose.yml`; pin `qdrant-client` in `requirements.txt`; runtime assert multivector/Query-API support.
- [ ] T2 Upgrade `VL_PROMPT` to a structured caption (chart/diagram type, axes, labels, readable numbers) — one-line local change, immediate caption quality gain.
- [ ] T3 Host preflight: confirm ≥12 GB free unified memory; download `vidore/colqwen2.5-v1.0` (~7 GB HF); smoke-test a forward pass on MPS with `sdpa`.

### Phase 1 — Embedder service (host, not Docker)
- [ ] T4 New `colqwen_service/` (host-only): FastAPI with `/health`, `POST /embed_pages` (PNG bytes → per-page `{"late": [n_patches,128], "pooled": [128]}`), `POST /embed_query` (text → `[~32,128]`). Model loaded once at startup; `sdpa`; batch ≤ 4; port 8200.
- [ ] T5 `requirements-colqwen.txt` (torch, colpali-engine, qwen-vl-utils, fastapi, uvicorn, pillow) + launch script + README section ("run on host like Ollama").

### Phase 2 — Page indexing (`rag_core` + `api`)
- [ ] T6 `rag_core`: `PAGES_DIR = ./pages/` (`pages/{project_id}/{stem}/page-{nnnn}.png`, pypdfium2 render scale 2.0); `PAGE_COLLECTION_NAME = "kilo-projects-pages"`; `ensure_page_collection()` — named vectors: `late` (128-dim, `MAX_SIM`, `MultiVectorConfig`), `pooled` (128-dim cosine); scalar int8 quantization on `late`; `hnsw_config m=0` initially; payload indexes `project_id`/`source`.
- [ ] T7 `index_pages_file(project_id, filename, progress_cb)`: gate on `finalized_md_path()` (human-approval gate preserved); render → embed via `COLQWEN_HOST` → upsert with payload `{project_id, source, page_number, page_image}`; deterministic uuid5 IDs; delete-then-upsert per file; batch progress like convert's page counter.
- [ ] T8 `delete_points_pages(project_id, source)`; wire into `api/app.py` deletes (also remove the page PNG dir); extend `jobs.py` `blocks_document_delete()` to `index_pages` jobs.
- [ ] T9 `api`: job type `index_pages` (file / project / all scopes, same patterns as ingest); CLI wrapper `index_pages.py`; README update.

### Phase 3 — Hybrid retrieval
- [ ] T10 `search_full`: optional visual branch — query embed via `/embed_query` → prefetch `pooled` top-50 → rerank `late` → scope filter (`MatchAny([project_id,"global"])`) → RRF fuse (k=60) with text ranking. Response gains `"pages": [...]` (source, page_number, score, page_image); text `hits` list unchanged; embedder failure → text-only + note. Env flag `ENABLE_VISUAL=1` default on when service healthy.
- [ ] T11 `api /api/search` returns `pages`; new `GET /api/projects/{pid}/pages/{stem}/{page}.png` (path-safe `FileResponse`); compose: bind-mount `./pages:/app/pages` + `COLQWEN_HOST` env on `api`. MCP server: optional `include_pages` flag, default off (keep MCP contract stable).

### Phase 4 — Web UI (minimal)
- [ ] T12 Search results: "Page matches" section — thumbnail + source + page number + score, click to open full image; existing parent-text cards untouched; shadcn default style.

### Phase 5 — Eval & backfill
- [ ] T13 Eval harness `tests/eval_visual.py` + `tests/eval_queries.json` (user-supplied 20–30 drawing/table queries with target source+page): recall@k text-only vs hybrid. **Gate: hybrid ≥ text on visual queries and no regression on text queries.**
- [ ] T14 Full backfill as an `index_pages` job (all projects); record throughput; int8 quantization already active via collection config.

### Phase 6 — Optional, gated on Phase 5 results (separate plan if pursued)
- [ ] T15 Evaluate ColQwen2 (2B) vs 2.5 (3B) vs 7B-base community checkpoints; image-query (search-by-diagram) support.

## Validation

- Unit: deterministic page IDs; delete cleanup (points + PNG dir); RRF fusion order (exact ref hits never displaced); collection creation config.
- Integration: 2-page synthetic PDF end-to-end (render → embed → search → thumbnail fetch); embedder-down graceful degradation test.
- Regression: existing 40 chunking tests green; text search byte-identical when `ENABLE_VISUAL=0`.
- Eval: Phase 5 recall@k A/B is the go/no-go for full backfill and any 7B work.

## Open questions (recommended defaults baked in)

1. 3B official first, 7B only after eval — **needs explicit user sign-off** (contradicts the original "7B" premise; no official 7B Col weights exist).
2. Eval ground truth: user supplies ~20–30 queries (drawings/tables focus) before Phase 5.
3. UI thumbnails included now (recommended: yes, minimal) — drop T12 for API-only first if preferred.
