# Web UI Excel/CSV Convert Support

## Goal

Let the web UI handle Excel/CSV files end-to-end: upload `.xlsx`/`.xlsm`/`.csv` → inbox, convert (per-file and "Convert all pending"), review, approve, ingest — same pipeline as PDFs. This fixes the reported error `Invalid job target '….xlsx': expected .pdf`.

The conversion core already exists and is web-UI-ready: `rag_core.convert_project_sheet(pid, name, progress_cb)` (mtime-skip, finalized-md skip, atomic write, `.prev` backup) and `rag_core.convert_sheets_inbox()`. `openpyxl` is already in `requirements.txt`. **No changes to `rag_core.py`, `sheet_to_md.py`, or `api/jobs.py`** (job blocking matches by stem, which already covers xlsx→md).

Confirmed decision: "Convert all pending" (per-project and library-wide) converts **both PDFs and sheets** in one job.

## Changes

### 1. `api/app.py`

- Add near the top: `SHEET_EXTS = rag_core.SHEET_EXTS` and `CONVERTIBLE_EXTS = (".pdf", *SHEET_EXTS)` (single source of truth from rag_core).
- `_route_upload`: route `.xlsx/.xlsm/.csv` → `inbox`; add an explicit 400 for `.xls`/`.ods` ("not supported — save as .xlsx first"); update the generic unsupported-type message to list `.pdf, .xlsx, .xlsm, .csv, .md, .txt`.
- `upload` endpoint (line ~235): the `saved` stage detection currently checks `.pdf` only — change to `ext in CONVERTIBLE_EXTS → "inbox"` so it stays consistent with `_route_upload`.
- `project_documents`:
  - kind map: map `xlsx/xlsm/csv` → new kind `"sheet"`.
  - `already_approved`: extend from `kind == "pdf"` to `kind in ("pdf", "sheet")` (`finalized_md_path` already works by stem).
  - `convert_progress` block needs no change (sheets never create `.progress` sidecars).
- `create_job`: change the convert validation to `_check_job_name(pid, body.name, "inbox", CONVERTIBLE_EXTS)` — this is the exact line that produced the reported error.
- `_submit_convert`:
  - **Per-file**: generalize the not-found message ("Inbox file not found: …"); branch on extension → `rag_core.convert_project_sheet` for sheet exts, else `convert_project_pdf`. For sheet files pass `progress_cb=None` (avoids a misleading "1/1 pages" flash; the result message is logged via `ctx.log`).
  - **Project-wide**: list inbox files matching any ext in `CONVERTIBLE_EXTS` (currently `.pdf` only) and convert each via the matching function; keep the per-file loop logging and error propagation.
  - **Library-wide**: run `rag_core.convert_inbox(...)` then `rag_core.convert_sheets_inbox(...)` in the same job; update log text ("Converting all pending PDFs and spreadsheets").

### 2. `web/lib/api.ts`

- `DocumentItem.kind` union: add `"sheet"`.

### 3. `web/app/projects/[id]/page.tsx`

- Upload input `accept`: `.pdf,.md,.txt,.xlsx,.xlsm,.csv`.
- "Convert all pending" button visibility: `inbox.some((d) => d.kind === "pdf" || d.kind === "sheet")`.
- Inbox section heading "Inbox PDFs" → e.g. "Inbox files"; update the empty-state copy to mention PDFs and Excel/CSV.
- `convertOne`'s already-approved message needs no logic change (it derives the `.md` name by extension).

### 4. `README.md` (root, Web UI section ~line 142)

- Update the upload description: `.xlsx/.xlsm/.csv` also route to the inbox; conversion covers spreadsheets (per-row `###` blocks via the sheet converter) alongside Docling PDFs; "Convert all pending" handles both.

## Edge cases (already handled — verify, don't change)

- Same-stem `.pdf` + spreadsheet in one project map to the same review md (existing documented CLI limitation).
- `.prev` / `.flatten-tmp` sidecars are already hidden via `rag_core.SIDECAR_SUFFIXES`.
- Review-edit blocking during sheet conversion works via stem matching in `blocks_review_edit`.
- CSV encodings (utf-8-sig → utf-16 → big5) and delimiter sniffing live in `sheet_to_md.py`.

## Validation

1. Restart `uvicorn api.app:app --port 8100` and the web dev server.
2. Upload the reported file `2400 - EMS Functional Development Specification_R0_Comments.xlsx` to a project → appears in inbox (kind sheet), then Convert → job completes, review md appears with per-row `###` blocks.
3. Edit → Approve → auto-ingest → chunk count > 0; search tab finds a row's content.
4. Mixed inbox (pdf + xlsx + csv) → "Convert all pending" converts both types in one job.
5. Upload a `.xls` → clear 400 "save as .xlsx" message.
6. Sheet with an approved md → Convert shows the already-approved info alert instead of enqueueing.
7. `cd web && npm run lint` (or `npx tsc --noEmit`) passes.
