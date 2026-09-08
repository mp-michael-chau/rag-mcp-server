## Introduction

This project is a locally hosted RAG knowledge base and MCP Server that lets AI coding agents such as Kilo retrieve shared cross-project documents (e.g. specifications, technical docs) directly via the MCP tool `search_shared_knowledge`, without pasting document contents into the conversation. All components (Qdrant, Ollama) run locally, so document contents are never sent to any external API. Documents are isolated per project by `project_id`; querying a specific project automatically includes the root-level `global` shared documents.

## Overview

### Components

| Component | Description |
| --- | --- |
| Qdrant (`docker-compose.yml`) | Vector database; HTTP/Dashboard port `6333`, gRPC port `6334`, data stored in `qdrant_storage/` |
| Ollama + `bge-m3` | Embedding model (1024 dims), served at `localhost:11434` |
| `ingest_docs.py` | Ingestion script: Docling PDF→Markdown, Parent-Child chunking, vectorization, upsert into Qdrant |
| `rag_mcp_server.py` | FastMCP stdio server exposing `search_shared_knowledge(query, project_id, target_parents=3)` |

### Data Flow (Ingest)

1. Scan `my_shared_docs/`: root-level files belong to `global`; first-level subfolder names are the `project_id`
2. PDFs are first converted to Markdown by Docling (table reconstruction in ACCURATE mode, built-in OCR); results are cached in `converted_md/`, written incrementally in 40-page batches and resumable after interruption
3. `MarkdownHeaderTextSplitter` splits by H1–H3 sections, then splits Parents (2000/0) and Children (300/50)
4. Embedding text = header breadcrumb + Child content, vectorized by bge-m3
5. Idempotent point IDs generated with uuid5, upserted into the `shared_knowledge` collection, with a `project_id` keyword index created

### Query Flow

1. The query is vectorized by bge-m3
2. Filtered by `project_id` (automatically includes `global`)
3. Over-fetches `target_parents * 5` child hits
4. Deduplicated by `parent_id`
5. Returns complete Parent sections (default 3); if Ollama is not running, returns a `[Search failed]` message and the MCP Server does not crash

### Key Features

- **Parent-Child (Small-to-Big) retrieval**: small chunks (Children) are used for semantic matching, and complete large sections (Parents) are returned
- **Header-aware chunking**: sections split by Markdown headers; embedding text includes the `H1 > H2 > H3` breadcrumb
- **Docling preprocessing**: table structure reconstruction and OCR, with conversion caching and resumable conversion
- **Idempotent reruns**: point IDs are derived from filename and chunk indices; rerunning overwrites old data instead of stacking duplicates
- **Ollama failure tolerance**: friendly error messages on query failure; the MCP Server keeps running

### Directory Overview

- `my_shared_docs/` — Shared document source (root = `global`, first-level subfolders = `project_id`)
- `converted_md/` — Markdown cache of Docling conversions
- `qdrant_storage/` — Qdrant data directory (Docker bind mount)
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

1. Put files into `my_shared_docs/{project-id/}` (supports `.pdf`, `.txt`, `.md`)
2. Run the ingestion script (chunks files, generates vectors, and uploads to Qdrant):

```bash
python ingest_docs.py
```

### Notes

- **PDF preprocessing**: PDFs are first converted to Markdown by [Docling](https://github.com/docling-project/docling) (including table structure reconstruction, reading-order normalization, and built-in OCR) before chunking and ingestion
- **Conversion cache**: Conversion results are stored in `converted_md/`; if the source PDF is unchanged, reruns reuse the cache without reconverting. Conversion is written incrementally in 40-page batches and automatically resumes from where it left off after interruption
- **First run**: Docling needs to download models (several hundred MB); converting large PDFs (hundreds of pages) may take tens of minutes. All later runs use the cache
- **Safe to rerun**: chunk IDs are derived from `filename + chunk index`; rerunning the same file overwrites old data instead of stacking duplicates
- **Changed file content**: if you edit a file and rerun, leftover chunks from the older file may remain; the clean approach is to delete the collection and rerun:
```bash
curl -X DELETE http://localhost:6333/collections/shared_knowledge
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