# FastAPI backend (api/app.py) wrapping rag_core: Docling PDF conversion, ingest, search.
# Docling pulls torch + friends, so this image is multi-GB; models download lazily at
# first conversion into /root/.cache (mount a named volume to keep them across rebuilds).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY rag_core.py flatten_md_tables.py ref_ids.py sheet_to_md.py convert_pdfs.py convert_sheets.py ingest_docs.py rag_mcp_server.py ./
COPY api ./api

EXPOSE 8100
CMD ["python", "-m", "uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8100"]
