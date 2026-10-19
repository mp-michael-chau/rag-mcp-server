# Local RAG Knowledge Base + MCP Server

A locally hosted RAG knowledge base and MCP Server that lets AI coding agents such as Kilo retrieve shared cross-project documents (e.g. specifications, technical docs) directly via the MCP tool `search_shared_knowledge`, without pasting document contents into the conversation. All components (Qdrant, Ollama) run locally, so document contents are never sent to any external API. Documents are isolated per project by `project_id`; querying a specific project automatically includes the root-level `global` shared documents.

## Prerequisites

- Docker (to run the Qdrant container)
- Ollama running with the embedding model pulled: `ollama pull bge-m3`
- Optional models: `ollama pull embeddinggemma-2` (visual retrieval), `ollama pull qwen3.8:27b-mlx` (image descriptions in converted PDFs)
- Python venv with dependencies installed: `pip install -r requirements.txt`
- Node.js (only for the optional web UI)

## Start Server

```bash
docker compose up -d
```

Starts the whole stack: Qdrant (pinned `v1.19.1`), the FastAPI backend (`api/`, Docling included), and the web UI. Ollama is expected on the host (models: `bge-m3`, `qwen3.8:27b-mlx`, `embeddinggemma-2` for visual retrieval).

- Web UI: http://localhost:3300 (change the `web` port mapping in `docker-compose.yml` if needed)
- Qdrant dashboard: http://localhost:6333/dashboard

## Add & Ingest Documents

Markdown/text files (`.md`, `.txt`): put them directly into `my_shared_docs/{project-id}/`.

PDFs and Excel/CSV spreadsheets (`.xlsx`/`.xlsm`/`.csv`) go through conversion + review first:

1. Drop the files into `pdf_inbox/{project-id}/` (root-level files = `global`)
2. Convert them to reviewable Markdown:

```bash
python convert_pdfs.py     # PDFs (tables flattened automatically right after conversion)
python convert_sheets.py   # Excel/CSV
```

3. **Review / edit the generated Markdown in `md_review/{project-id}/`** — this is the quality gate before anything is indexed
4. Move the finalized `.md` files into `my_shared_docs/{project-id}/` — the move itself is the approval signal
5. Run the ingestion script (chunks files, generates vectors, and uploads to Qdrant):

```bash
python ingest_docs.py
```

6. *(Optional)* Index the approved PDFs' pages as images for visual retrieval:

```bash
python index_pages.py
```

Detailed notes and known limitations: [docs/INGEST.md](docs/INGEST.md)

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

Add a rule to each project's AGENTS.md, e.g. for a project whose documents live in `my_shared_docs/my-project/`:
`Always call search_shared_knowledge with project_id="my-project".`

Section-ID search reference: [docs/MCP.md](docs/MCP.md)

## Web UI (optional)

Included in the docker stack above (http://localhost:3300). For development as two dev processes (FastAPI + Next.js), see [docs/WEB_UI.md](docs/WEB_UI.md).

## Further Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — components, ingest/query data flow, key features, directory overview
- [docs/INGEST.md](docs/INGEST.md) — detailed ingestion notes (review gate, table flattening, resume, spreadsheets, reruns)
- [docs/MCP.md](docs/MCP.md) — Section-ID search reference
- [docs/WEB_UI.md](docs/WEB_UI.md) — web UI features, dev setup, notes
- [docs/VISUAL_RETRIEVAL.md](docs/VISUAL_RETRIEVAL.md) — page-as-image retrieval (indexing, searching, eval)
