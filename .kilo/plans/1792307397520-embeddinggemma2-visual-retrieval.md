# EmbeddingGemma 2 Page-as-Image Visual Retrieval Plan

Supersedes `.kilo/plans/1791165156709-colqwen-visual-retrieval-upgrade.md` (kept as fallback reference; ColQwen is demoted to the Phase 6 upgrade path).

## Goal

Add page-image semantic retrieval (EmbeddingGemma 2 / "EG2", single-vector 768d) alongside the existing text RAG, fused by RRF, 100% local on the Mac host. Existing text pipeline (bge-m3, Parent-Child, ref-ID exact matching, human review gate) is untouched — pure addition. Additionally run an offline EG2-vs-bge-m3 text-embedder eval (report only; no swap without explicit user approval).

## Why EG2 over ColQwen (decided 2026-10-18)

- Corpus is small (~10 PDFs, ~2k pages); a strong text pipeline already does RRF-fusion heavy lifting → EG2's quality-per-cost wins over ColQwen's higher ceiling.
- Infra: EG2 runs on Ollama (`embeddinggemma-2`, ~440M text+vision), reusing the existing `/api/embed` pattern; no colpali-engine, no host FastAPI service, no multivector Qdrant config, no MPS/flash-attn workarounds.
- Apache 2.0; 8K context leaves headroom for an image+text composite eval variant.

## Decisions (user-confirmed)

1. Visual engine: **EG2 replaces ColQwen**; ColQwen only if Phase 4 eval fails.
2. Text embedder: **keep bge-m3 in production**; EG2 text gets an offline recall@k eval only.
3. Granularity: **page-level only** for v1 (figure-level deferred to Phase 6).

## Non-goals

- Replacing the bge-m3 text index or ref-ID / compliance features (eval is report-only)
- Any external API — all-local by decision
- Figure-level indexing, image-query search, fine-tuning (Phase 6 candidates)
- VLM answer-generation from page images (future work)

## Current state (verified)

- Pipeline: `pdf_inbox` → Docling (ACCURATE tables, `images_scale=2.0`, VL captions via `qwen3.8:27b-mlx`) → `md_review` (human gate) → `my_shared_docs` → bge-m3 Parent-Child → Qdrant `kilo-projects-info` (`rag_core.py`)
- Visual weakness today: images survive only as one-line `[Image]: <description>` text (rag_core.py:56, `VL_PROMPT`); page layout/figures unindexed
- `api/jobs.py`: in-process FIFO single worker, job types `convert` | `ingest`; `blocks_document_delete()` guards deletes; `api/app.py` deletes call `rag_core.delete_points()`
- Docker: `qdrant/qdrant:latest` (unpinned), `api`, `web` (port 3300); Ollama on host via `host.docker.internal`

## Preflight risks & handling

1. **Ollama `/api/embed` image support for `embeddinggemma-2` is unverified** — Preflight T0 must confirm images can be embedded via Ollama; fallback = lightweight host microservice (sentence-transformers `google/embeddinggemma-2`, FastAPI, port 8200) reached via `host.docker.internal`. Decide before Phase 1.
2. **Dimension**: EG2 = 768 (MRL-truncatable). Keep full 768; ~2k pages ≈ ~6 MB vectors — storage non-issue. Separate collection avoids any clash with bge-m3's 1024d.
3. **`qdrant:latest` unpinned** — pin image ≥1.15 and `qdrant-client` (carried over from ColQwen plan T1). No multivector/Query-API features needed beyond standard `query_points`.
4. **Page PNGs** in `./pages/` — bind-mount into `api` container; serve via path-safe endpoint (reuse `safe_file_path()` pattern); add `pages/` to `.gitignore`, NOT to `.dockerignore`.
5. **Delete lifecycle** — page collection points + PNG dir must be purged by `delete_document` / `delete_project`; `blocks_document_delete()` extended to `index_pages` jobs.
6. **Page hits carry no ref IDs / parents** — separate `pages` list in responses; text `hits` semantics unchanged; exact-ref flow untouched.
7. **Embedder down** — visual branch degrades gracefully to text-only + note in `notes` (same as ColQwen plan #7).

## Tasks (ordered)

### Phase 0 — Preflight & quick win
- [x] T0 Pull `embeddinggemma-2` in Ollama; smoke-test: text embed (768d probe) AND image embed (page PNG bytes). If Ollama can't embed images → build host microservice fallback (T0b) before Phase 1. Record decision in README.
  - DONE 2026-10-18: Ollama 0.40.5 **supports** image embedding via `/api/embed` object input form `{"input": [{"image": <b64>}]}` (768d, ~0.21 s/page, batch 4 OK). A top-level `"images"` field is silently ignored (vectors come back text-only) — T0b microservice NOT needed. Decision recorded in README.
- [x] T1 Pin Qdrant image (≥1.15) in `docker-compose.yml`; pin `qdrant-client` in `requirements.txt`.
  - DONE: `qdrant/qdrant:1.19.1` (was running as unpinned latest) + `qdrant-client>=1.15`.
- [x] T2 Upgrade `VL_PROMPT` to structured caption (chart/diagram type, axes, labels, readable numbers) — one-line change, immediate caption gain regardless of EG2.
  - DONE: structured caption prompt (visual kind, title/subject, axes/series/units/numbers, components/connections, labels/annotations; no guessing).

### Phase 1 — Page rendering + EG2 page collection
- [x] T3 `rag_core`: `PAGES_DIR = ./pages/` (`pages/{project_id}/{stem}/page-{nnnn}.png`, pypdfium2 render scale 2.0); `PAGE_COLLECTION_NAME = "kilo-projects-pages"`; `PAGE_EMBED_MODEL = os.getenv("PAGE_EMBED_MODEL", "embeddinggemma-2")`; `ensure_page_collection()` (768d cosine, dimension probed from actual model output, mirrors `ensure_collection`).
- [x] T4 `index_pages_file(project_id, filename, progress_cb)`: gate on `finalized_md_path()` (human-approval gate preserved); render → embed (batch ≤4) → upsert payload `{project_id, source, page_number, page_image}`; ID = `uuid5("{project_id}:{filename}:page{n}")`; delete-then-upsert per file (mirrors `ingest_file`).
- [x] T5 `delete_points_pages(project_id, source)`; wire into `api/app.py` deletes (also remove PNG dir); extend `jobs.py` `blocks_document_delete()` to `index_pages`.
  - Delete guard extended to inbox stage too (index_pages renders the inbox PDF).
- [x] T6 `api` job type `index_pages` (file/project/all scopes); CLI wrapper `index_pages.py`; README section.

### Phase 2 — Hybrid retrieval
- [x] T7 `search_full`: optional visual branch — query embed via EG2 → page collection `query_points` with scope filter (`MatchAny([project_id,"global"])` / `MatchValue("global")`) → RRF fuse (k=60) with text ranking. Response gains `"pages": [{source, page_number, score, page_image}]`; text `hits` unchanged; embedder failure → text-only + note. Env `ENABLE_VISUAL=1` default on when service healthy.
  - Fusion is per-source: vector-fill parents reorder by RRF(text rank, source's best page rank); pages list orders by RRF(page rank, source's best text rank); exact ref hits never displaced (unit-tested).
- [x] T8 `api /api/search` returns `pages`; new `GET /api/projects/{pid}/pages/{stem}/{page}.png` (path-safe `FileResponse`); compose: bind-mount `./pages:/app/pages`. MCP server: optional `include_pages` flag, default off (MCP contract stable).

### Phase 3 — Web UI (minimal)
- [x] T9 Search results: "Page matches" section — thumbnail + source + page number + score, click to open full image; existing parent-text cards untouched; shadcn default style.
  - Plus a project-level "Index pages" button (appears when an approved PDF exists) so the job type is reachable from the UI.

### Phase 4 — Eval (two tracks, user-supplied ground truth)
- [ ] T10 **Visual eval** `tests/eval_visual.py` + `tests/eval_queries.json` (user supplies 20–30 drawing/table queries with target source+page): recall@k text-only vs hybrid. **Gate: hybrid ≥ text on visual queries AND no regression on text queries.** Also A/B page-embedding variant: image-only (default) vs image+page-MD-text interleaved (EG2 8K context).
  - Harness BUILT and refuses to run on placeholder data; **blocked on user-supplied ground-truth queries** (≥5 required, aim 20–30; format in eval_queries.json `_README`). Smoke-verified: 1 real query against TQ-T3-1 returns the correct page (p.2) as top page hit.
- [ ] T11 **Text-embedder eval (report-only)** `tests/eval_text_embedder.py`: embed all existing child texts with EG2 text (cache vectors to disk), compare recall@k vs live bge-m3 Qdrant on the same query set. Output a comparison report; any swap requires explicit user approval + full rebuild (separate plan).
  - Script BUILT (disk-cached EG2 text vectors, brute-force cosine); same ground-truth prerequisite as T10.

### Phase 5 — Backfill (gated on T10 pass)
- [ ] T12 Full-corpus `index_pages` job (all projects); record throughput.
  - Ready: `python index_pages.py` / `POST /api/jobs {"type":"index_pages"}` (all-scope). TQ-T3-1.pdf already indexed as the smoke test (23 pages, idempotent — full backfill will simply overwrite).

### Phase 6 — Optional, gated on Phase 4 results (separate plan if pursued)
- [ ] T13 ColQwen2.5 late-interaction path if EG2 fails the visual gate (reuse superseded plan); figure-level indexing; image-query search.

## Validation

- Unit: deterministic page IDs; delete cleanup (points + PNG dir); RRF fusion order (exact ref hits never displaced); collection creation config.
- Integration: 2-page synthetic PDF end-to-end (render → embed → search → thumbnail fetch); embedder-down graceful degradation.
- Regression: existing 40 chunking tests green; text search byte-identical when `ENABLE_VISUAL=0`.
- Eval: T10 recall@k A/B is the go/no-go for full backfill and any ColQwen escalation.

## Open questions / prerequisites

1. Ollama image-embed capability (T0) — decides embed path; fallback design ready.
2. User must supply 20–30 eval queries (drawings/tables focus) before Phase 4.
3. Interleaved image+text page embedding stays an eval variant unless it clearly beats image-only.
