# Plan: Section-ID Search Reinforcement for the Shared RAG MCP Server

Target repo (all changes here): `/Users/bellergy/Documents/Projects/local-mcp/rag`
Files in scope: `rag_core.py`, `rag_mcp_server.py`, `api/app.py`, `README.md`, new `ref_ids.py`, new tests.
Corpus: `my_shared_docs/clpe-cdhk/` (10 files, ~716 KB). **Never modify files under `my_shared_docs/`** — all transforms are in-memory at ingest time.

## 1. Problem (evidence summary)

Compliance-table workflows query by section ID ("PS 10.3.6", "Section 23", "Schedule 3"). The current `rag_core.search()` is pure dense retrieval (bge-m3 cosine, top-15 children → dedup by parent → 3 parents) with no lexical layer. Verified failure modes:

1. **Number-only queries carry no semantic signal** — meta-documents that *discuss* PS clauses (Tender Clarifications, TQ/PTQ, 2400 comments) outrank the clause itself.
2. **Sub-clauses are not structural** — `## 10.3` (H2) is the only header; `- 10.3.6` is a plain list line, so a 2000-char parent mixes 10.3.4(i)–(n) + 10.3.5–10.3.9 into one grab-bag; the 300-char child holding 10.3.6 is one thin sentence.
3. **Ranking theft by richer neighbors** — 10.3.7/9/10/11 discuss controller/web-server/data-logging in longer, denser text and beat the 10.3.6 child for any topical query.
4. **PDF-conversion noise** — "webbased" (no space/hyphen), double spaces; "web-based" queries tokenize differently.
5. **Chicken-and-egg** — without knowing a clause's content, query vocabulary guesses miss.

Control experiment: querying with the clause's own vocabulary returns the correct parent at rank 1 → the chunk IS indexed; failure is query-side + ranking, not ingestion.

11 references failed in the §9 compliance work (acceptance set): PS 10.3.6 / 10.3.7 / 10.3.9 / 10.3.21 / 10.3.22 / 10.3.23 / 10.4.8 / Section 23 / Section 25 / Schedule 3 / Sch.1 guidelines (i)–(viii).

## 2. User-confirmed decisions

- **Depth: all three layers** (L1 query-time exact match, L2 ingest-time structural fix, L3 reverse citation lookup).
- **MCP surface: enhance the existing `search_shared_knowledge` tool only** — auto-detect refs in `query`, plus one optional `ref` parameter for explicit override. No new tool, no Kilo client config changes.

## 3. Design

### 3.1 Canonical ref-ID model (`ref_ids.py`, new pure-function module)

Canonical forms stored lowercase in Qdrant payload arrays:

| Kind | Examples (query surface) | Canonical |
|---|---|---|
| Dotted numeric, ≥2 segments | `PS 10.3.6`, `Clause 10.3.11(d)`, `10.4.8` | `10.3.6`; letter suffix stripped → parent `10.3.11` (+ keep `sub: d`) |
| Top-level section | `Section 23`, `Section 25` | `23`, `25` |
| Named schedule | `Schedule No. 3`, `Schedule 3`, `Sch.1`, `SC No. 1` | `schedule-3`, `schedule-1` |
| Alphanumeric row/item IDs | `TC2-T3`, `TQ-T8-1`, `2400-01` | verbatim lowercase |

Normalization: strip prefixes `{ps, clause, section, schedule no., schedule nr., schedule, sch., sc no., sor schedule nr.}`, trailing periods, spaces. Letter sub-items `(d)`/`(ii)` never address a parent of their own — they resolve to the containing clause.

### 3.2 L2 — ingest-time clause-header promotion (in `rag_core.ingest_file`)

Before `split_parents_with_headers(content)`, transform in memory:

- Line regex `^\s*-\s*(\d+(?:\.\d+)+)\s+(.*)` (dotted, ≥2 segments) → **insert** a line `### <id>` immediately above the original line; keep the original line unchanged as body text.
  - Insert-above (not replace) is required because `MarkdownHeaderTextSplitter(strip_headers=True)` drops the header line from `page_content`; keeping the original line prevents empty single-line clauses like 10.3.6.
  - ≥2 dotted segments excludes PDF-conversion artifacts like `2. (a) Carbon Dioxide Sensor;` and prose decimals (`1.5 mm²`, `IEC 60502-1:2004`).
- Result: each clause becomes its own parent; breadcrumb `… > 10.3 Energy Monitoring System ('EMS') > 10.3.6` enters `embed_src` (rag_core.py L507), which also fixes vector recall for ID-ish queries.
- Alphanumeric IDs (`TC2-T3`, `2400-01`, TQ items) are already `###`/`##` headers in the corpus — no promotion needed.

### 3.3 Payload schema changes (per child point, additive)

- `parent_ref_ids: [str]` — own IDs of the parent: clause ID from the promoted/builtin header **plus** every section ID on the breadcrumb path (e.g. children of clause 10.3.6 get `["10.3", "10.3.6"]`; children under `## (Optional) Schedule No. 3 …` get `["schedule-3"]`; under `## 23 (Optional) Integration…` get `["23"]`).
- `mentioned_refs: [str]` — reference-shaped tokens found in the parent text (`clause 10.3.3`, `PS 22.5`, `Schedule No. 1(B)(a)`, `2400-01`, `TQ-T8-1`). Over-extraction here is tolerable (used only for the Referenced-by block).
- Create Qdrant keyword payload indexes for both fields in `ensure_collection()` (idempotent, like the existing `project_id` index).

### 3.4 L1 — query-time detection + exact match + blending (in `rag_core.search`)

1. **Detect**: extract candidate tokens from the query (prefixed forms always; bare dotted/alphanumeric tokens only if an existence check against `parent_ref_ids` confirms them — kills false positives like `IEC 60502-1`).
2. **Exact match**: `query_points` with filter `project_id ∈ {pid, global} AND parent_ref_ids MATCH-ANY {ids}`, dedup by `parent_id`, deterministic order (`p_idx`, `child_index`).
   - Depth ≥ 3 (`10.3.6`) → return that clause parent directly.
   - Depth ≤ 2 (`10.3`, `23`, `schedule-3`) → many parents match; vector-rank children within the filter and return top `target_parents` clauses of that section.
   - Letter sub-item → resolve to containing clause; verify the letter appears in text, else note it.
   - Multiple refs in one query → exact-match each.
3. **Blend**: exact hits first, then fill up to `target_parents` with today's vector results (dedup'd against exact hits). Zero exact hits → current behavior unchanged, plus note `[ref 'X' not found exactly; showing semantic results]`.
4. **L3 reverse lookup**: when exact hits exist, append a `Referenced by:` block listing `source` files (with per-file counts) whose children have the ID in `mentioned_refs`. This directly serves compliance-matrix work with no mode flag.

### 3.5 MCP tool (in `rag_mcp_server.py`)

- Signature: `search_shared_knowledge(query, project_id=None, target_parents=3, ref=None)` — `ref` overrides auto-detection.
- Update docstring: mention section-ID lookup (`"PS 10.3.6"`, `"Section 23"`, `"Schedule 3"`, `"TC2-T3"`) and the Referenced-by block.
- Output format: existing per-hit block + `[Exact ref match: <id>]` tag; hits gain `matched_refs` in the dict (api response stays additive; web UI ignores until updated).

## 4. Task list (ordered)

1. Create `ref_ids.py`: `normalize_query_refs(query)`, `promote_clause_headers(content)`, `extract_header_ref_ids(breadcrumb)`, `extract_mentioned_refs(text)`. Pure functions, stdlib only.
2. Unit tests (`tests/test_ref_ids.py`, pytest): normalization table (all query-surface variants above), promotion (10.3.6 single-line clause keeps body; `2. (a)` artifacts not promoted; `1.5 mm²` not promoted), extraction from breadcrumbs (`23 (Optional) Integration…` → `23`; `Schedule No. 3 …` → `schedule-3`).
3. `rag_core.py` ingest path: apply promotion in `ingest_file` (in-memory only); populate `parent_ref_ids` / `mentioned_refs`; extend `ensure_collection` with the two payload indexes.
4. `rag_core.py` search path: detection → exact match → blend → Referenced-by (3.4).
5. `rag_mcp_server.py`: optional `ref` param + docstring.
6. `api/app.py`: no behavioral change needed (it calls `rag_core.search`); optionally add `matched_refs` passthrough in the search response.
7. Update repo `README.md` (section-ID search behavior, ref syntax).
8. Full re-ingest via existing flow (CLI `ingest_docs.py` or API `POST /ingest/all`). Ingest is delete-then-upsert per file; old points without new fields disappear automatically.
9. Validation (below); deliver a short results table.

## 5. Acceptance criteria

- Each of the 11 acceptance-set references resolves via the MCP tool with the exact clause/section text as hit #1 (clause parents for 10.3.x/10.4.8; within-section clauses for Section 23/25; Schedule No. 3 section for `Schedule 3`; Schedule No. 1 clauses for `Sch.1 guidelines`).
- `PS 10.3.6` query returns the clause verbatim top-1 **plus** a Referenced-by block (expect ≥ TC2-T3, 2400-xx entries).
- Regression (vector quality unchanged): vocabulary query `embedded type webbased programmable controller … data logging function web server I/O` still returns the 10.3.6 clause top-1; `36-month data retention` still returns 10.3.1(e)/10.3.11(a)/10.4.5 clauses.
- `my_shared_docs/` files byte-identical before/after (verify via `git status` in the rag repo).
- MCP tool signature backward compatible (old 3-arg calls work).

## 6. Risks / edge cases

- **Granularity change**: clause-level parents return less sibling context than the old grab-bag parents. Accepted trade-off (compliance work wants precision); breadcrumb keeps section context, `parent_full` unchanged in format.
- **False-positive ref detection** on bare tokens → mitigated by existence check against the index (3.4.1).
- **Header-ID duplication in returned text** (breadcrumb + original line both show `10.3.6`) — accepted, aids readability.
- **Point count growth** (~3–5× for the PS) → re-ingest is minutes on local Ollama; no action.
- `### Operation Hours:`-style pseudo-headers already in the corpus interleave with promoted headers — harmless (splitter treats them the same).

## 7. Out of scope

- General BM25/hybrid ranking for non-ID text queries.
- Web UI ref-search controls (it benefits automatically via `rag_core.search`).
- Per-project configurable ref patterns (built-in defaults; revisit when a second project with different ID conventions lands).
