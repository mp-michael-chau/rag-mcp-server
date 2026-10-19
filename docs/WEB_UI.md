# Web UI (optional)

A local web app wraps the whole CLI workflow: dashboard with Qdrant/Ollama health badges and project cards, multi-file upload (`.pdf`/`.xlsx`/`.xlsm`/`.csv` → inbox, `.md`/`.txt` → approved docs), Docling conversion with live progress bars plus spreadsheet conversion into per-row `###` blocks ("Convert all pending" handles both in one job), a CodeMirror Markdown review editor with preview, Approve / Approve & Ingest actions, per-file and per-project (re-)ingest with chunk counts, an **Index pages** button (project-wide page-image indexing for visual retrieval), a read-only viewer for approved docs, and a search tab mirroring the MCP retrieval — including a **Page matches** section with clickable page thumbnails when page indexes exist.

## Run via Docker

Everything at once (see [Start Server](../README.md#start-server)):

```bash
docker compose up -d   # web UI on http://localhost:3300
```

Note: the `/api/*` proxy target is baked into the web image at build time (`API_ORIGIN` build arg); after changing it or the API service name, rebuild with `docker compose build web`.

## Run as Two Dev Processes

1. Start the backend (from `rag/`, single process only — the job queue lives in it):

```bash
.venv/bin/python -m uvicorn api.app:app --port 8100
```

2. Start the frontend (from `rag/web/`):

```bash
npm install
npm run dev
```

3. Open http://localhost:3000 — the Next.js dev server proxies `/api/*` to `127.0.0.1:8100`. If port 3000 is taken (e.g. by a Docker port mapping), run `npm run dev -- --port 3100` and open http://localhost:3100 (the API allows both origins).

## Notes

- **Jobs**: conversions and ingestions run in a single background worker thread (FIFO) inside the FastAPI process; the frontend polls job status ~1×/s. The in-memory job registry is lost on restart — that is fine, because PDF conversion resumes from its `.progress` sidecar files and ingestion is idempotent
- **Concurrent edit protection**: while a conversion job is queued or running for a review file, saving that file from the editor returns 409 (protects the flatten/review flow)
- **Project rename is not supported** (uuid5 point IDs embed the project_id); delete and recreate instead
- **`global`** is a virtual scope (root-level files); it always appears on the dashboard and cannot be created or deleted as a project
