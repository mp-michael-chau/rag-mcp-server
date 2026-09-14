# RAG Parent Chunk Fix — Complete Sections (Option A)

**Target repo:** `/Users/bellergy/Documents/Projects/local-mcp/rag/`
**Executed by:** RAG Server agent

## Problem

Exact-ref retrieval of deep clauses returns truncated sections. Querying `ref=10.3.4` returns the clause text only through item **(k)**; items **(l) PV panel system operating conditions, (m) PV fail-to-export alarm, (n) lively student UI** are missing, even though they exist in `my_shared_docs/clpe-cdhk/02.Particular Specification.md` (lines 360–362).

**Root cause (two cooperating defects):**

1. **Ingest:** `rag_core.py:443–447` — `parent_splitter = RecursiveCharacterTextSplitter(chunk_size=2000, chunk_overlap=0)`. Clause 10.3.4's body (intro + items (a)–(n), ≈2.1k chars after Docling's double-spacing) exceeds 2000, so it splits into two parents: parent A = intro…(k), parent B = (l)(m)(n). Both share the breadcrumb `… > 10.3.4`. This violates the stated design (rag_core.py:428: *"Parent = complete large section"*).
2. **Retrieval:** `rag_core.py:692` — deep-ref exact match picks `min(points, key=(parent_index, child_index))`, i.e. only the FIRST parent; its `payload["text"]` ends at (k). Parent B exists in the index but is never surfaced (tiny fragment, loses semantic ranking).

Same failure class is the suspected cause of "Sch.1 guidelines list begins directly at (ix)" (FSD §9.5(B)).

## Decision (user-confirmed)

- Raise the parent cap to **12000** (`PARENT_CHUNK_SIZE`), a named constant. Parents are never embedded (only children are, at 300/50), so this does not affect matching precision — only payload size and returned text size. 12000 gives ~6× headroom over the largest known section (~2–3k).
- **Out of scope:** retrieval-side continuation stitching (former "Option B"); FSD §9.1/§9.5 updates in `ioter3-frontend` (separate follow-up after validation).

## Tasks

1. **`rag_core.py` — splitter constant**
   - Above `parent_splitter` (~line 442), add `PARENT_CHUNK_SIZE = 12000` with a comment: parents are not embedded, the cap exists only to bound payload duplication (each child copies the full parent text) and to guard header-less flat files; keep it large so "Parent = complete section" holds for real contract clauses.
   - Set `parent_splitter = RecursiveCharacterTextSplitter(chunk_size=PARENT_CHUNK_SIZE, chunk_overlap=0, separators=["\n\n", "\n", " ", ""])`. Child splitter (300/50) unchanged. Do not touch `child_splitter`.

2. **`rag_core.py` — oversize warning**
   - In `split_parents_with_headers()` (lines 457–467): when one header section yields more than one parent chunk, print a warning with the breadcrumb, section char length, and part count, e.g. `Warning: section '<breadcrumb>' (<n> chars) exceeds PARENT_CHUNK_SIZE; split into <k> parts — retrieval may truncate`. Keeps future oversize sections visible instead of silently truncated.

3. **README.md sync**
   - Line 30: "Parents (2000/0)" → "Parents (12000/0)".
   - Line 44: reword "buried in a 2000-char section grab-bag" to match (e.g. "a section grab-bag").

4. **Regression test** — new `tests/test_rag_core_chunking.py` (offline; must not require Qdrant/Ollama — `rag_core` import is safe, `QdrantClient(...)` construction performs no network call):
   - Build synthetic markdown: `- 10.3.4 <intro>` clause line + lettered items `(a)`–`(p)` with ~150 chars each (total ≈2.4–3k chars). Run `ref_ids.promote_clause_headers()` → `rag_core.split_parents_with_headers()`; assert **exactly 1 parent** and that its text contains the first and last lettered items (this test fails at the old 2000 cap).
   - Second test: a synthetic section larger than 12000 splits into ≥2 parents (fallback unchanged).
   - Run: `.venv/bin/python -m pytest tests/ -q` (existing `tests/test_ref_ids.py` must stay green).

5. **Re-ingest all documents**
   - From the rag repo root: `python ingest_docs.py` (plain `ingest_all()`). This re-chunks every file under the new cap; per-file embed → delete-old-points → upsert keeps the collection continuously available (no `--rebuild` downtime needed; the corpus is 10 md files in `my_shared_docs/clpe-cdhk/`, no global files). Use `--rebuild` only as a fallback if the dashboard shows any inconsistent point counts.

6. **Validation** (via Qdrant dashboard search, the web UI search tab, or an MCP `search_shared_knowledge` call with `project_id="clpe-cdhk"`):
   - `ref="10.3.4"` → the returned 10.3.4 section must contain `(l) PV panel system operating conditions`, `(m) … fail to feed electricity to power company grid`, and `(n) … lively and attractive to Students`.
   - Re-check Sch.1 general dashboard guidelines: query for the guidelines list / `ref="schedule-1"` and look for items **(i)–(viii)** (previously began at (ix)). Record the result either way — it feeds FSD §9.5(B).
   - Spot-check other long clauses for full text: `ref="10.3.11"`, `ref="10.3.20"`, and one `### 2400-xx` block.
   - Confirm no behavioral regression on a plain semantic query (no `ref`).

7. **Commit** (if the rag repo is under git and that is its convention): one commit covering `rag_core.py`, `README.md`, `tests/test_rag_core_chunking.py`.

## Preconditions

- Qdrant container up: `docker compose up -d` (port 6333).
- Ollama running with `bge-m3` pulled (`ollama pull bge-m3`).
- Use the repo's venv interpreter (`.venv/bin/python` — the same one `kilo.json` MCP config uses).

## Risks / Notes

- Payload growth is bounded and local-scale (worst case: 12k-char parent × ~40 children ≈ 0.5 MB per parent; UPSERT_BATCH=100 stays far below Qdrant's 32MB request limit).
- Query-path code is untouched → the running MCP server does **not** need a restart; it reads the re-upserted points directly.
- `p_idx`/`c_idx` layouts shift, but point IDs are uuid5-derived and each file's old points are deleted before upsert, so no stale/duplicate chunks remain.
- Consequence to remember: while a file is mid-reingest its old points are deleted then re-upserted within seconds; searches during that window may briefly miss that one file.

## Out of Scope (follow-ups elsewhere)

- Retrieval-side continuation stitching for any future >12000-char section (former Option B).
- FSD updates in `ioter3-frontend` after validation succeeds: replace the `PS 10.3.4 (i)–(n) | Not found (9.5)` row (functional-specification_r2.md:1377) with real mappings ((i)→4.1.4/4.4.2, (j)→4.3.3/4.3.13, (k)→4.2.2, (l)→PV monitoring, (m)→4.3.3 PV zero-output alarm, (n)→§2/4.2.2), close §9.5(C), and re-assess §9.5(B) with the new Sch.1 retrieval result.
