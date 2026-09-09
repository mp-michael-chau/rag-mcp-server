"""rag_core.py — shared core for the RAG document workflow.

Extracted from convert_pdfs.py / ingest_docs.py / rag_mcp_server.py so the CLI
scripts and the FastAPI web UI (api/) run the exact same logic.

All base directories are anchored to this file's location, so behavior no
longer depends on the current working directory (the CLI wrappers used to
require CWD = repo root).

Layout convention (mirrored across the three dirs):
root-level files = the virtual "global" scope; first-level subfolder names are
project_ids (dotfolders skipped, no deeper recursion).
"""

import json
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PointStruct,
    VectorParams,
)
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

import flatten_md_tables

# 1. Base directories (absolute; anchored to this file)
BASE_DIR = Path(__file__).resolve().parent
PDF_INBOX_DIR = str(BASE_DIR / "pdf_inbox")  # Drop PDFs to convert here
MD_REVIEW_DIR = str(BASE_DIR / "md_review")  # Docling output awaiting user review; moving the finished .md into my_shared_docs IS the approval
DOCS_DIR = str(BASE_DIR / "my_shared_docs")  # Read-only check: if the finalized md already lives here, skip reconversion

# 2. System settings
QDRANT_HOST = "localhost"
QDRANT_PORT = 6333
COLLECTION_NAME = "kilo-projects-info"
OLLAMA_HOST = "http://localhost:11434"
EMBED_MODEL = "bge-m3"
CONVERT_BATCH_PAGES = 40  # Pages per conversion batch (limits memory usage; resumable after interruption)
EMBED_BATCH = 32  # Texts per Ollama /api/embed call
UPSERT_BATCH = 100  # Points per Qdrant upsert call (32MB single-request payload limit)

# Sidecar/hidden-file suffixes produced by this pipeline (importers: api/app.py listings/uploads/deletes).
# ".flatten-tmp" is produced by flatten_md_tables.flatten_file's atomic write.
SIDECAR_SUFFIXES = (".progress", ".prev", ".flatten-tmp")

qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)

# 3. Docling PDF converter (lazy singleton: importing docling is slow, and search/inggest must not pay for it)
_converter = None


def _get_converter():
    global _converter
    if _converter is None:
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode

        # ACCURATE mode for table recognition: slower but better table reconstruction
        pdf_pipeline = PdfPipelineOptions()
        pdf_pipeline.table_structure_options.mode = TableFormerMode.ACCURATE
        _converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_pipeline)}
        )
    return _converter


def get_pdf_page_count(filepath):
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(filepath))
    try:
        return len(pdf)
    finally:
        pdf.close()


# 4. Folder helpers

def project_dir(base_dir, project_id):
    """Folder for a project inside base_dir; the virtual 'global' scope maps to the base dir root (never a subfolder)."""
    if project_id == "global":
        return str(base_dir)
    return os.path.join(str(base_dir), project_id)


def scan_doc_sources(directory):
    """Two-level scan: root-level files = global; first-level subfolder names are project_ids
    (skip folders starting with . and don't recurse deeper). Returns [(project_id, filename, filepath)]."""
    sources = []
    if not os.path.isdir(directory):
        return sources
    for entry in sorted(os.listdir(directory)):
        entry_path = os.path.join(directory, entry)
        if os.path.isdir(entry_path):
            if entry.startswith("."):
                continue
            for filename in sorted(os.listdir(entry_path)):
                filepath = os.path.join(entry_path, filename)
                if os.path.isfile(filepath):
                    sources.append((entry, filename, filepath))
        elif os.path.isfile(entry_path):
            sources.append(("global", entry, entry_path))
    return sources


def list_projects():
    """Union of first-level subfolder names across the three base dirs; 'global' (root-level files) always present."""
    projects = {"global"}
    for directory in (PDF_INBOX_DIR, MD_REVIEW_DIR, DOCS_DIR):
        if os.path.isdir(directory):
            for entry in os.listdir(directory):
                if entry.startswith(".") or entry == "global":
                    continue
                if os.path.isdir(os.path.join(directory, entry)):
                    projects.add(entry)
    return sorted(projects)


# 5. Embedding + collection bootstrap

def embed_texts(texts):
    """Call the Ollama embedding API; raises RuntimeError with a friendly hint when Ollama is unreachable."""
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/embed",
        data=json.dumps({"model": EMBED_MODEL, "input": texts}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)["embeddings"]
    except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError) as e:
        raise RuntimeError(
            f"Cannot connect to Ollama ({OLLAMA_HOST}, model {EMBED_MODEL}): {e}. "
            "Please make sure Ollama is running (e.g. run `ollama serve`) and the model is downloaded, then retry."
        ) from e


def ensure_collection():
    """Create the collection (dimension probed from the actual Ollama model output) + the project_id payload index (idempotent)."""
    if not qdrant.collection_exists(COLLECTION_NAME):
        qdrant.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(
                size=len(embed_texts(["dimension probe"])[0]),
                distance=Distance.COSINE
            )
        )
    try:
        qdrant.create_payload_index(COLLECTION_NAME, field_name="project_id", field_schema="keyword")
    except Exception:
        pass


def delete_points(project_id=None, source=None):
    """Delete Qdrant points by payload filter (project_id and/or source filename)."""
    must = []
    if project_id is not None:
        must.append(FieldCondition(key="project_id", match=MatchValue(value=project_id)))
    if source is not None:
        must.append(FieldCondition(key="source", match=MatchValue(value=source)))
    qdrant.delete(collection_name=COLLECTION_NAME, points_selector=Filter(must=must))


# 6. PDF → Markdown conversion (Docling, resumable)

def convert_pdf(pdf_path, review_path, progress_cb=None):
    """Convert PDF to Markdown with Docling in batches, writing each batch to the review file; rerunning after interruption resumes from the breakpoint.
    progress_cb(done_pages, total_pages) is invoked after each batch. Returns True if a conversion actually ran to
    completion, False on the up-to-date skip path."""
    pdf_path, review_path = str(pdf_path), str(review_path)
    progress_path = review_path + ".progress"

    # Review md exists, conversion finished (no .progress) and not older than the source PDF → keep it (preserves manual edits made during review)
    if (os.path.exists(review_path) and not os.path.exists(progress_path)
            and os.path.getmtime(review_path) >= os.path.getmtime(pdf_path)):
        print(f"  Review md up to date, skip: {review_path}")
        return False

    # Source PDF changed → back up the old review md (only one .prev is kept) and reconvert
    if os.path.exists(review_path) and os.path.getmtime(review_path) < os.path.getmtime(pdf_path):
        os.replace(review_path, review_path + ".prev")
        if os.path.exists(progress_path):
            os.remove(progress_path)

    # Orphaned .progress without a review md → stale sidecar; start over from page 1
    # (otherwise "ab" + truncate() would pad the recreated file with NUL bytes)
    if os.path.exists(progress_path) and not os.path.exists(review_path):
        os.remove(progress_path)

    total_pages = get_pdf_page_count(pdf_path)
    done_pages, done_bytes = 0, 0
    if os.path.exists(progress_path):
        with open(progress_path, "r", encoding="utf-8") as pf:
            done_pages, done_bytes = (int(x) for x in pf.read().strip().split(","))
    print(f"  Converting with Docling ({done_pages}/{total_pages} pages done, {CONVERT_BATCH_PAGES} pages per batch)")

    converter = _get_converter()
    with open(review_path, "ab" if done_pages else "wb") as out:
        if done_pages:
            out.truncate(done_bytes)
        start = done_pages + 1
        while start <= total_pages:
            end = min(start + CONVERT_BATCH_PAGES - 1, total_pages)
            result = converter.convert(pdf_path, page_range=(start, end))
            out.write(result.document.export_to_markdown().encode("utf-8"))
            out.write(b"\n\n")
            out.flush()
            with open(progress_path, "w", encoding="utf-8") as pf:
                pf.write(f"{end},{out.tell()}")
            print(f"  Converted {end}/{total_pages} pages")
            if progress_cb:
                progress_cb(end, total_pages)
            start = end + 1

    os.remove(progress_path)
    with open(review_path, "r", encoding="utf-8") as f:
        if not f.read().strip():
            print(f"Warning: conversion produced no text for {pdf_path}; it may be a scan without a text layer")
    return True


def convert_project_pdf(project_id, filename, progress_cb=None):
    """Convert a single pdf_inbox PDF through the full convert rules (finalized-md skip, resume, flatten after
    fresh conversion only). Returns {"status": converted|skipped|error, "message": str}."""
    filepath = os.path.join(project_dir(PDF_INBOX_DIR, project_id), filename)
    stem = os.path.splitext(filename)[0]

    # Finalized md already moved into my_shared_docs → never reconvert (protects the reviewed/approved copy);
    # global documents live at the my_shared_docs root (mirroring the two-level scan), but the legacy
    # my_shared_docs/global/ subfolder location is also checked so old-layout approvals still count
    finalized_paths = [os.path.join(project_dir(DOCS_DIR, project_id), stem + ".md")]
    if project_id == "global":
        finalized_paths.append(os.path.join(DOCS_DIR, "global", stem + ".md"))
    if any(os.path.exists(p) for p in finalized_paths):
        print(f"PDF: {project_id}/{filename} → finalized md already present, skip")
        return {"status": "skipped", "message": f"{project_id}/{filename}: finalized md already present, skipped"}

    print(f"PDF: {project_id}/{filename}")
    review_path = os.path.join(project_dir(MD_REVIEW_DIR, project_id), stem + ".md")
    os.makedirs(os.path.dirname(review_path), exist_ok=True)
    try:
        converted = convert_pdf(filepath, review_path, progress_cb=progress_cb)
    except Exception as e:
        print(f"Warning: Docling conversion failed for {project_id}/{filename} ({e}); skipped")
        return {"status": "error", "message": f"Docling conversion failed for {project_id}/{filename}: {e}"}
    else:
        # Flatten tables only after a fresh conversion (the skip path is never flattened, protecting manual review edits);
        # a flatten failure keeps the raw Docling output for review as usual
        if converted:
            try:
                flatten_md_tables.flatten_file(review_path)
            except Exception as e:
                print(f"Warning: table flattening failed for {review_path} ({e}); keeping raw Docling output")

    if converted:
        return {"status": "converted", "message": f"{project_id}/{filename}: converted → {review_path}"}
    return {"status": "skipped", "message": f"{project_id}/{filename}: review md up to date, skipped"}


def convert_inbox(progress_cb=None):
    """CLI entry: convert every pending PDF found in pdf_inbox/ (two-level scan)."""
    if not os.path.isdir(PDF_INBOX_DIR):
        print(f"No {PDF_INBOX_DIR} directory found. Put PDFs into {PDF_INBOX_DIR}/{{project_id}}/ (root-level files = global) first.")
        return

    pdf_sources = [(p, f, fp) for (p, f, fp) in scan_doc_sources(PDF_INBOX_DIR) if f.lower().endswith(".pdf")]
    if not pdf_sources:
        print(f"No PDFs found in {PDF_INBOX_DIR}.")
        return

    for project_id, filename, filepath in pdf_sources:
        convert_project_pdf(project_id, filename, progress_cb=progress_cb)

    print(f"Done. Review/edit the Markdown in {MD_REVIEW_DIR}/, then move the finalized .md files into {DOCS_DIR}/ and run ingest_docs.py.")


# 7. Text chunking: Parent-Child (Small-to-Big) architecture
#    Parent = complete large section (stored in each Child's payload["text"] and returned directly at retrieval time, preserving full context)
#    Child  = small chunk (only Children are vectorized, for precise semantic matching)
#    First split into Parents by Markdown header levels (H1-H3) with parent header breadcrumbs, then split overly long sections by length
headers_to_split_on = [
    ("#", "Header 1"),
    ("##", "Header 2"),
    ("###", "Header 3"),
]

header_splitter = MarkdownHeaderTextSplitter(
    headers_to_split_on=headers_to_split_on,
    strip_headers=True,
)

# Parent: covers a complete section, no overlap needed
parent_splitter = RecursiveCharacterTextSplitter(
    chunk_size=2000,
    chunk_overlap=0,
    separators=["\n\n", "\n", " ", ""]
)

# Child: sized for the embedding model, focused on precise semantic matching
child_splitter = RecursiveCharacterTextSplitter(
    chunk_size=300,
    chunk_overlap=50,
    separators=["\n\n", "\n", " ", ""]
)


def split_parents_with_headers(content):
    """Return a list of (parent header breadcrumb, parent chunk text); content without headers gets an empty breadcrumb"""
    results = []
    for section in header_splitter.split_text(content):
        breadcrumb = " > ".join(
            section.metadata[key] for _, key in headers_to_split_on
            if section.metadata.get(key)
        )
        for parent_text in parent_splitter.split_text(section.page_content):
            results.append((breadcrumb, parent_text))
    return results


# 8. Ingestion

def docs_path(project_id, filename):
    return os.path.join(project_dir(DOCS_DIR, project_id), filename)


def ingest_file(project_id, filename, progress_cb=None):
    """Ingest one approved document: chunk → embed → upsert.
    Before upserting, existing points for this project_id + source are deleted, so re-ingesting an
    edited file leaves no stale chunks behind. progress_cb(done_entries, total_entries) fires per embed batch.
    Returns {"status": ingested|skipped, "chunks": int, "message": str}."""
    filepath = docs_path(project_id, filename)
    if filename.lower().endswith('.pdf'):
        msg = (f"Warning: PDFs are not ingested directly; put them in pdf_inbox/, run convert_pdfs.py, "
               f"review in md_review/, then move the md here ({project_id}/{filename} skipped)")
        print(msg)
        return {"status": "skipped", "chunks": 0, "message": msg}
    if not filename.endswith(('.txt', '.md')):
        return {"status": "skipped", "chunks": 0, "message": f"{project_id}/{filename}: unsupported file type, skipped"}

    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    if not content.strip():
        msg = f"Warning: no text found in {project_id}/{filename}; skipped"
        print(msg)
        return {"status": "skipped", "chunks": 0, "message": msg}

    print(f"Processing: {project_id}/{filename}")

    # Parent-Child chunking: split Parents first (with breadcrumb prefix), then split each Parent into Children;
    # only Children are vectorized; the full Parent text is stored in each Child's payload["text"]
    entries = []
    for p_idx, (breadcrumb, parent_text) in enumerate(split_parents_with_headers(content)):
        parent_full = f"{breadcrumb}\n\n{parent_text}" if breadcrumb else parent_text
        parent_id = f"{project_id}/{filename}#p{p_idx}"
        for c_idx, child_text in enumerate(child_splitter.split_text(parent_text)):
            entries.append({
                "breadcrumb": breadcrumb,
                "parent_id": parent_id,
                "p_idx": p_idx,
                "c_idx": c_idx,
                "parent_full": parent_full,
                "embed_src": f"{breadcrumb}\n\n{child_text}" if breadcrumb else child_text,
            })

    # Generate vectors in batches (Ollama /api/embed supports multiple inputs; embedding text = breadcrumb + Child)
    points = []
    for start in range(0, len(entries), EMBED_BATCH):
        batch = entries[start:start + EMBED_BATCH]
        vectors = embed_texts([entry["embed_src"] for entry in batch])

        for entry, vector in zip(batch, vectors):
            # Create the Qdrant point (ID derived from project + filename + parent/child index; reruns overwrite instead of duplicating)
            point = PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{project_id}:{filename}:{entry['p_idx']}:{entry['c_idx']}")),
                vector=vector,
                payload={
                    "project_id": project_id,
                    "source": filename,
                    "headers": entry["breadcrumb"],
                    "parent_id": entry["parent_id"],
                    "parent_index": entry["p_idx"],
                    "child_index": entry["c_idx"],
                    "child_text": entry["embed_src"],  # for Dashboard debugging
                    "text": entry["parent_full"],  # full Parent content, returned directly at retrieval time
                }
            )
            points.append(point)
        if progress_cb:
            progress_cb(min(start + EMBED_BATCH, len(entries)), len(entries))

    # Guard against the file being deleted while embedding ran: never write points for a vanished source
    # (would leave orphans the UI cannot remove).
    if not os.path.exists(filepath):
        msg = f"Warning: {project_id}/{filename} disappeared during ingestion; aborted before upsert"
        print(msg)
        return {"status": "skipped", "chunks": 0, "message": msg}

    # Remove this file's previous points before upserting, so edited/shrunk files leave no stale chunks.
    # Done after embedding so an Ollama failure never destroys the existing index state.
    delete_points(project_id=project_id, source=filename)

    print(f"Upserting {len(points)} chunks into Qdrant...")
    # Upload in batches to avoid exceeding Qdrant's single-request payload limit (32MB)
    for start in range(0, len(points), UPSERT_BATCH):
        batch = points[start:start + UPSERT_BATCH]
        qdrant.upsert(
            collection_name=COLLECTION_NAME,
            points=batch
        )
        print(f"  Uploaded {min(start + UPSERT_BATCH, len(points))}/{len(points)} chunks")
    print("Done!")
    return {"status": "ingested", "chunks": len(points),
            "message": f"Ingested {project_id}/{filename} ({len(points)} chunks)"}


def ingest_all(progress_cb=None):
    """CLI entry: scan my_shared_docs/ and ingest every supported file (PDFs skipped with warning)."""
    ingested = 0
    for project_id, filename, filepath in scan_doc_sources(DOCS_DIR):
        result = ingest_file(project_id, filename, progress_cb=progress_cb)
        if result["status"] == "ingested":
            ingested += 1
    if not ingested:
        print("No ingestible documents found.")


# 9. Search (Parent-Child Small-to-Big retrieval)

def search(query, project_id=None, target_parents=3):
    """Search the shared knowledge base. Returns a list of hit dicts
    [{"score", "project_id", "source", "headers", "text"}] — one per deduplicated parent —
    or an empty list when nothing matches. Raises RuntimeError when Ollama is unreachable.

    When project_id is given, only that project is searched, automatically including global
    shared documents; when project_id is omitted, only global shared documents are searched."""
    query_vector = embed_texts([query])[0]

    search_filter = Filter(must=[
        FieldCondition(key="project_id", match=MatchAny(any=[project_id, "global"]) if project_id else MatchValue(value="global"))
    ])

    # Over-fetch: top hits may be concentrated among multiple children of the same parent chunk,
    # so fetch 5x the target count and deduplicate by parent_id to avoid "top-K domination" diluting results
    fetch_limit = target_parents * 5

    results = qdrant.query_points(
        collection_name=COLLECTION_NAME,
        query=query_vector,
        query_filter=search_filter,
        limit=fetch_limit
    ).points

    hits = []
    seen_parents = set()
    for res in results:
        p_id = res.payload.get("parent_id") or res.id
        if p_id in seen_parents:
            continue
        seen_parents.add(p_id)
        hits.append({
            "score": res.score,
            "project_id": res.payload.get("project_id"),
            "source": res.payload.get("source"),
            "headers": res.payload.get("headers"),
            "text": res.payload.get("text"),
        })
        if len(hits) >= target_parents:
            break
    return hits
