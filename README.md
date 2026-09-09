## Introduction

This project is a locally hosted RAG knowledge base and MCP Server that lets AI coding agents such as Kilo retrieve shared cross-project documents (e.g. specifications, technical docs) directly via the MCP tool `search_shared_knowledge`, without pasting document contents into the conversation. All components (Qdrant, Ollama) run locally, so document contents are never sent to any external API. Documents are isolated per project by `project_id`; querying a specific project automatically includes the root-level `global` shared documents.

## Overview

### Components

| Component | Description |
| --- | --- |
| Qdrant (`docker-compose.yml`) | Vector database; HTTP/Dashboard port `6333`, gRPC port `6334`, data stored in `qdrant_storage/` |
| Ollama + `bge-m3` | Embedding model (1024 dims), served at `localhost:11434` |
| `convert_pdfs.py` | PDF→Markdown conversion script (Docling); converts `pdf_inbox/` into reviewable Markdown in `md_review/` |
| `ingest_docs.py` | Ingestion script: md/txt sources → Parent-Child chunking, vectorization, upsert into Qdrant |
| `rag_mcp_server.py` | FastMCP stdio server exposing `search_shared_knowledge(query, project_id, target_parents=3)` |

### Data Flow (Ingest)

1. Drop PDFs into `pdf_inbox/{project_id}/` (root-level files = `global`, mirroring `my_shared_docs`)
2. Run `python convert_pdfs.py` → Docling converts them to reviewable Markdown in `md_review/{project_id}/` (table reconstruction in ACCURATE mode, built-in OCR; written incrementally in 40-page batches and resumable after interruption)
3. **Review / edit the Markdown in `md_review/`** — this is the quality gate before anything is indexed
4. Move the finalized `.md` into `my_shared_docs/{project_id}/` — the move itself is the approval signal
5. Run `python ingest_docs.py` (md/txt only): `MarkdownHeaderTextSplitter` splits by H1–H3, then Parents (2000/0) and Children (300/50); embedding text = header breadcrumb + Child content, vectorized by bge-m3; idempotent uuid5 point IDs are upserted into the `kilo-projects-info` collection, with a `project_id` keyword index created

### Query Flow

1. The query is vectorized by bge-m3
2. Filtered by `project_id` (automatically includes `global`)
3. Over-fetches `target_parents * 5` child hits
4. Deduplicated by `parent_id`
5. Returns complete Parent sections (default 3); if Ollama is not running, returns a `[Search failed]` message and the MCP Server does not crash

### Key Features

- **Parent-Child (Small-to-Big) retrieval**: small chunks (Children) are used for semantic matching, and complete large sections (Parents) are returned
- **Header-aware chunking**: sections split by Markdown headers; embedding text includes the `H1 > H2 > H3` breadcrumb
- **Docling preprocessing**: table structure reconstruction and OCR, with resumable batch conversion and a review gate before ingestion
- **Idempotent reruns**: point IDs are derived from filename and chunk indices; rerunning overwrites old data instead of stacking duplicates
- **Ollama failure tolerance**: friendly error messages on query failure; the MCP Server keeps running

### Directory Overview

- `pdf_inbox/` — Drop PDFs to convert here (root = `global`, first-level subfolders = `project_id`)
- `md_review/` — Docling conversion output awaiting review; edit here, then move the finalized `.md` into `my_shared_docs/`
- `my_shared_docs/` — Approved document source (root = `global`, first-level subfolders = `project_id`)
- `converted_md/` — Legacy conversion cache from the old single-script workflow (safe to delete; keep `02-Particular-Specification-points-list.md` if still needed)
- `qdrant_storage/` — Qdrant data directory (Docker bind mount)
- `convert_pdfs.py` — PDF→Markdown conversion script (review gate)
- `ingest_docs.py` — Ingestion script (documents → chunks → vectors → Qdrant)
- `rag_mcp_server.py` — MCP Server (provides the retrieval tool)

## Prerequisites

- Docker (to run the Qdrant container)
- Ollama running with the embedding model pulled: `ollama pull bge-m3`
- Python venv with dependencies installed: `pip install -r requirements.txt`

---

## Start Server

    ./docker-compose.yml

    docker compose up -d

### Web Access
    http://localhost:6333/dashboard

---

## Ingest Documents

### Adding Shared Documents

For Markdown/text files, put them directly into `my_shared_docs/{project-id}/` (supports `.md`, `.txt`).

For PDFs, go through the conversion + review workflow first:

1. Put the PDFs into `pdf_inbox/{project-id}/`
2. Convert them to reviewable Markdown:

```bash
python convert_pdfs.py
```

3. Review / edit the generated Markdown in `md_review/{project-id}/`
4. Move the finalized `.md` files into `my_shared_docs/{project-id}/`
5. Run the ingestion script (chunks files, generates vectors, and uploads to Qdrant):

```bash
python ingest_docs.py
```

### Notes

- **Review gate**: nothing from a PDF is indexed until you move its reviewed `.md` into `my_shared_docs/`; the move is the approval signal
- **Conversion resume**: conversion is written incrementally in 40-page batches and automatically resumes from where it left off after interruption; a finished review md is reused when the source PDF is unchanged (manual edits preserved). If the PDF changes, the old review md is backed up as `{name}.md.prev` and the PDF is reconverted. If the finalized md already exists in `my_shared_docs/`, the PDF is skipped entirely
- **First run**: Docling needs to download models (several hundred MB); converting large PDFs (hundreds of pages) may take tens of minutes
- **PDFs in `my_shared_docs/`**: `ingest_docs.py` does not ingest PDFs directly; it prints a warning and skips them
- **Safe to rerun**: chunk IDs are derived from `filename + chunk index`; rerunning the same file overwrites old data instead of stacking duplicates
- **Changed file content**: if you edit a file and rerun, leftover chunks from the older file may remain; the clean approach is to delete the collection and rerun:
```bash
curl -X DELETE http://localhost:6333/collections/kilo-projects-info
python ingest_docs.py
```

---

## RAG MCP Setup
The MCP server config currently lives only in rag/kilo.json. Other projects that want to call this tool need to add the same config to their kilo.json (or the global ~/.config/kilo/kilo.json):
```jsonc
{
  "mcp": {
    "shared-rag": {
      "type": "local",
      "command": ["{this_project_directory}/.venv/bin/python",
                  "{this_project_directory}/rag_mcp_server.py"],
      "enabled": true
    }
  }
}
```

### Making Each Project Automatically Pass Its Own project_id
Add a rule to each project's AGENTS.md, e.g. in the clpe-cdhk project:
`Always call search_shared_knowledge with project_id="clpe-cdhk".`