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

import base64
import json
import os
import re
import shutil
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
import ref_ids
import sheet_to_md

# 1. Base directories (absolute; anchored to this file)
BASE_DIR = Path(__file__).resolve().parent
PDF_INBOX_DIR = str(BASE_DIR / "pdf_inbox")  # Drop PDFs to convert here
MD_REVIEW_DIR = str(BASE_DIR / "md_review")  # Docling output awaiting user review; moving the finished .md into my_shared_docs IS the approval
DOCS_DIR = str(BASE_DIR / "my_shared_docs")  # Read-only check: if the finalized md already lives here, skip reconversion

# 2. System settings (env-overridable so the Docker stack can point at service names
# instead of localhost; defaults keep the bare-metal CLI/dev workflow unchanged)
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
COLLECTION_NAME = "kilo-projects-info"
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")
CONVERT_BATCH_PAGES = 40  # Pages per conversion batch (limits memory usage; resumable after interruption)
EMBED_BATCH = 32  # Texts per Ollama /api/embed call
UPSERT_BATCH = 100  # Points per Qdrant upsert call (32MB single-request payload limit)
VL_MODEL = os.getenv("VL_MODEL", "qwen3.8:27b-mlx")  # Ollama vision-language model describing images found in PDFs
# Structured caption prompt: extracts chart/diagram type, axes, labels and readable numbers so
# figures become findable via text. English prompt → English descriptions (matches the mostly-English corpus).
VL_PROMPT = (
    "Describe this image from an engineering/tender document for retrieval purposes, in one compact paragraph: "
    "(1) what kind of visual it is (drawing, chart, graph, table, diagram, flowchart, photo, map...); "
    "(2) its title or subject; "
    "(3) for charts/graphs/tables: the axes, columns, series, units, and any readable numbers, values or ranges; "
    "(4) for drawings/diagrams: the main components, symbols and how they connect; "
    "(5) any labels, legends, keys or annotations visible. "
    "State only what is actually visible; do not guess or interpret beyond the image."
)
VL_TIMEOUT = 300  # Seconds per VL request; a 27B model can take tens of seconds per image

# 2b. Page-image visual retrieval (EmbeddingGemma 2 multimodal, "EG2", 768d MRL)
# Ollama /api/embed accepts images only in object input form: each entry carries its own
# {"image": <b64>} (T0 preflight: a top-level "images" field is silently ignored by the embed
# endpoints — vectors come back text-only — and an empty-string input is rejected).
PAGES_DIR = str(BASE_DIR / "pages")  # Rendered page PNGs: pages/{project_id}/{stem}/page-{nnnn}.png
PAGE_COLLECTION_NAME = "kilo-projects-pages"
PAGE_EMBED_MODEL = os.getenv("PAGE_EMBED_MODEL", "embeddinggemma-2")
PAGE_EMBED_BATCH = 4  # Images per Ollama /api/embed call (each page PNG is a multi-hundred-KB base64 JSON entry)
PAGE_EMBED_TIMEOUT = 300  # Seconds per page-embed call (vision encoding of a batch is slower than text)
PAGE_RENDER_SCALE = 2.0  # pypdfium2 render scale (matches Docling images_scale; keeps small text readable for the vision encoder)
PAGE_QUERY_INSTRUCTION = "task: given a web page query, retrieve the relevant page\nquery: "  # EG2 query-side task prefix
PAGE_RESULTS = 6  # Page matches returned per search (fused order; scored by raw page cosine)
ENABLE_VISUAL = os.getenv("ENABLE_VISUAL", "1") != "0"  # default on; search degrades to text-only when off

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
        pdf_pipeline.generate_picture_images = True  # populate picture.image (PIL) so the VL model can describe it
        pdf_pipeline.images_scale = 2.0  # small text/thin lines on engineering drawings stay readable
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


def embed_page_images(images_b64):
    """Embed base64 page PNGs with the multimodal model (object input form, one image per
    entry — see the T0 note at PAGE_EMBED_MODEL). Raises RuntimeError when Ollama is
    unreachable, mirroring embed_texts."""
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/embed",
        data=json.dumps({"model": PAGE_EMBED_MODEL, "input": [{"image": b} for b in images_b64]}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=PAGE_EMBED_TIMEOUT) as resp:
            return json.load(resp)["embeddings"]
    except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError) as e:
        raise RuntimeError(
            f"Cannot embed page images with Ollama ({OLLAMA_HOST}, model {PAGE_EMBED_MODEL}): {e}. "
            f"Please make sure Ollama is running (e.g. run `ollama serve`) and the model is downloaded "
            f"(`ollama pull {PAGE_EMBED_MODEL}`), then retry."
        ) from e


def embed_page_query(query):
    """Embed a text query for page-image retrieval (EG2 retrieval task prefix on the query side;
    the page side embeds images alone). Raises RuntimeError when Ollama is unreachable."""
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/embed",
        data=json.dumps({"model": PAGE_EMBED_MODEL, "input": [PAGE_QUERY_INSTRUCTION + query]}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)["embeddings"][0]
    except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError) as e:
        raise RuntimeError(
            f"Cannot embed the page query with Ollama ({OLLAMA_HOST}, model {PAGE_EMBED_MODEL}): {e}. "
            f"Please make sure Ollama is running (e.g. run `ollama serve`) and the model is downloaded "
            f"(`ollama pull {PAGE_EMBED_MODEL}`), then retry."
        ) from e


def ensure_collection():
    """Create the collection (dimension probed from the actual Ollama model output)
    + keyword payload indexes (idempotent): project_id, and the ref-ID arrays
    parent_ref_ids / mentioned_refs used by section-ID exact matching."""
    if not qdrant.collection_exists(COLLECTION_NAME):
        qdrant.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(
                size=len(embed_texts(["dimension probe"])[0]),
                distance=Distance.COSINE
            )
        )
    for field in ("project_id", "parent_ref_ids", "mentioned_refs"):
        try:
            qdrant.create_payload_index(COLLECTION_NAME, field_name=field, field_schema="keyword")
        except Exception:
            pass


def embed_eg2_texts(texts):
    """Embed plain texts with the page model's text tower (eval tooling: EG2-vs-bge-m3
    offline comparison; the production text pipeline stays on bge-m3)."""
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/embed",
        data=json.dumps({"model": PAGE_EMBED_MODEL, "input": texts}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)["embeddings"]
    except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError) as e:
        raise RuntimeError(
            f"Cannot embed texts with Ollama ({OLLAMA_HOST}, model {PAGE_EMBED_MODEL}): {e}. "
            f"Please make sure Ollama is running (e.g. run `ollama serve`) and the model is downloaded "
            f"(`ollama pull {PAGE_EMBED_MODEL}`), then retry."
        ) from e


def ensure_page_collection():
    """Create the page collection (dimension probed from the actual model output — EG2 emits
    768, but a server-side `dimensions` option could truncate it, so it is never hardcoded)
    + project_id/source keyword indexes (idempotent), mirroring ensure_collection."""
    if not qdrant.collection_exists(PAGE_COLLECTION_NAME):
        qdrant.create_collection(
            collection_name=PAGE_COLLECTION_NAME,
            vectors_config=VectorParams(
                size=len(embed_page_query("dimension probe")),
                distance=Distance.COSINE
            )
        )
    for field in ("project_id", "source"):
        try:
            qdrant.create_payload_index(PAGE_COLLECTION_NAME, field_name=field, field_schema="keyword")
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


def delete_points_pages(project_id=None, source=None):
    """Delete page-collection points by payload filter (mirrors delete_points). No-op when the
    page collection doesn't exist yet — nothing has been indexed, so there is nothing to delete
    (and delete_points-style hard failures here would break document deletes on fresh stacks)."""
    if not qdrant.collection_exists(PAGE_COLLECTION_NAME):
        return
    must = []
    if project_id is not None:
        must.append(FieldCondition(key="project_id", match=MatchValue(value=project_id)))
    if source is not None:
        must.append(FieldCondition(key="source", match=MatchValue(value=source)))
    qdrant.delete(collection_name=PAGE_COLLECTION_NAME, points_selector=Filter(must=must))


# 6. PDF → Markdown conversion (Docling, resumable)

def describe_image(image):
    """Send one image (DoclingImage / PIL) to the local Ollama VL model; returns the description
    text, or None on any failure (timeout, model not found, Ollama down...) — never raises, so a
    VL problem can never abort a conversion; the caller keeps the placeholder instead."""
    import base64
    import io

    try:
        pil = getattr(image, "pil_image", image)
        if pil is None:
            return None
        buf = io.BytesIO()
        pil.convert("RGB").save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        req = urllib.request.Request(
            f"{OLLAMA_HOST}/api/chat",
            data=json.dumps({
                "model": VL_MODEL,
                "stream": False,
                "options": {"temperature": 0},
                "messages": [{"role": "user", "content": VL_PROMPT, "images": [b64]}],
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=VL_TIMEOUT) as resp:
            return json.load(resp)["message"]["content"].strip()
    except Exception as e:
        print(f"Warning: image description failed ({e}). Please make sure Ollama is running (e.g. run "
              f"`ollama serve`) and the model is downloaded (`ollama pull {VL_MODEL}`); placeholder kept")
        return None


def replace_image_placeholders(md_text, pictures):
    """Replace each <!-- image --> placeholder in md_text with an [Image]: <description> paragraph,
    in document order. If the placeholder count doesn't match len(pictures), keeps ALL placeholders
    and warns (never guess the alignment — a missing description beats a wrong one). A failed or
    image-less picture keeps its placeholder."""
    pictures = list(pictures)
    parts = md_text.split("<!-- image -->")
    count = len(parts) - 1
    if count != len(pictures):
        if count or pictures:
            print(f"Warning: {count} image placeholders vs {len(pictures)} pictures in this batch; keeping all placeholders")
        return md_text
    out = parts[0]
    for i, (picture, rest) in enumerate(zip(pictures, parts[1:]), 1):
        print(f"  Describing image {i}/{len(pictures)}...")
        desc = describe_image(picture.image) if picture.image is not None else None
        out += (f"[Image]: {desc}" if desc else "<!-- image -->") + rest
    return out


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
            md = result.document.export_to_markdown()
            md = replace_image_placeholders(md, result.document.pictures)
            out.write(md.encode("utf-8"))
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


def finalized_md_path(project_id, filename):
    """Path of the approved .md for an inbox source file, or None if it has not been approved yet.
    global documents live at the my_shared_docs root (mirroring the two-level scan), but the legacy
    my_shared_docs/global/ subfolder location is also checked so old-layout approvals still count."""
    stem = os.path.splitext(filename)[0]
    candidates = [os.path.join(project_dir(DOCS_DIR, project_id), stem + ".md")]
    if project_id == "global":
        candidates.append(os.path.join(DOCS_DIR, "global", stem + ".md"))
    return next((p for p in candidates if os.path.exists(p)), None)


def convert_project_pdf(project_id, filename, progress_cb=None):
    """Convert a single pdf_inbox PDF through the full convert rules (finalized-md skip, resume, flatten after
    fresh conversion only). Returns {"status": converted|skipped|error, "message": str}."""
    filepath = os.path.join(project_dir(PDF_INBOX_DIR, project_id), filename)
    stem = os.path.splitext(filename)[0]

    # Finalized md already moved into my_shared_docs → never reconvert (protects the reviewed/approved copy)
    if finalized_md_path(project_id, filename):
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


# 6b. Excel/CSV → Markdown conversion (block structure generated directly; no Docling, no flatten)

SHEET_EXTS = (".xlsx", ".xlsm", ".csv")


def convert_project_sheet(project_id, filename, progress_cb=None):
    """Convert a single pdf_inbox Excel/CSV through the convert rules: finalized-md skip, mtime-based
    reconvert (old review md backed up as .prev), atomic write. No flatten (the ### block structure is
    generated directly) and no .progress sidecar (conversion is in-memory and atomic).
    progress_cb(done, total) matches convert_project_pdf's signature for the future web UI integration.
    Returns {"status": converted|skipped|error, "message": str}."""
    filepath = os.path.join(project_dir(PDF_INBOX_DIR, project_id), filename)
    stem = os.path.splitext(filename)[0]

    # Finalized md already moved into my_shared_docs → never reconvert (protects the reviewed/approved copy)
    if finalized_md_path(project_id, filename):
        print(f"Sheet: {project_id}/{filename} → finalized md already present, skip")
        return {"status": "skipped", "message": f"{project_id}/{filename}: finalized md already present, skipped"}

    # Review md up to date → keep it (preserves manual edits made during review)
    review_path = os.path.join(project_dir(MD_REVIEW_DIR, project_id), stem + ".md")
    if os.path.exists(review_path) and os.path.getmtime(review_path) >= os.path.getmtime(filepath):
        print(f"Sheet: {project_id}/{filename} → review md up to date, skip")
        return {"status": "skipped", "message": f"{project_id}/{filename}: review md up to date, skipped"}

    print(f"Sheet: {project_id}/{filename}")
    os.makedirs(os.path.dirname(review_path), exist_ok=True)
    try:
        md = sheet_to_md.convert_file(filepath)
        # Convert fully in memory first: on failure the old review md stays untouched; on success back
        # it up (only one .prev kept) then swap in the new content atomically (temp file + os.replace).
        # ".flatten-tmp" is reused so the web API already hides the temp file as a sidecar.
        if os.path.exists(review_path):
            os.replace(review_path, review_path + ".prev")
        tmp_path = review_path + ".flatten-tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(md)
        os.replace(tmp_path, review_path)
    except Exception as e:
        print(f"Warning: sheet conversion failed for {project_id}/{filename} ({e}); skipped")
        return {"status": "error", "message": f"sheet conversion failed for {project_id}/{filename}: {e}"}
    if progress_cb:
        progress_cb(1, 1)
    return {"status": "converted", "message": f"{project_id}/{filename}: converted → {review_path}"}


def convert_sheets_inbox(progress_cb=None):
    """CLI entry: convert every pending Excel/CSV (.xlsx/.xlsm/.csv) found in pdf_inbox/ (two-level scan)."""
    if not os.path.isdir(PDF_INBOX_DIR):
        print(f"No {PDF_INBOX_DIR} directory found. Put spreadsheets into {PDF_INBOX_DIR}/{{project_id}}/ (root-level files = global) first.")
        return

    sources = scan_doc_sources(PDF_INBOX_DIR)
    for project_id, filename, _ in sources:
        if filename.lower().endswith((".xls", ".ods")):
            print(f"Warning: {project_id}/{filename}: .xls/.ods is not supported; save it as .xlsx first, then rerun")

    sheet_sources = [(p, f, fp) for (p, f, fp) in sources if f.lower().endswith(SHEET_EXTS)]
    if not sheet_sources:
        print(f"No spreadsheets ({', '.join(SHEET_EXTS)}) found in {PDF_INBOX_DIR}.")
        return

    for project_id, filename, filepath in sheet_sources:
        convert_project_sheet(project_id, filename, progress_cb=progress_cb)

    print(f"Done. Review/edit the Markdown in {MD_REVIEW_DIR}/, then move the finalized .md files into {DOCS_DIR}/ and run ingest_docs.py.")


# 6c. Page rendering (visual-retrieval input; pypdfium2, not Docling — imports stay cheap and
#     search/ingest never pay for the converter)

def pages_png_dir(project_id, stem):
    """Directory holding one document's rendered page PNGs: pages/{project_id}/{stem}/."""
    return os.path.join(PAGES_DIR, project_id, stem)


def page_png_relpath(project_id, stem, page_number):
    """Page PNG path relative to PAGES_DIR — the value stored in Qdrant payloads and served
    by the API's page-image endpoint."""
    return f"{project_id}/{stem}/page-{page_number:04d}.png"


def delete_page_pngs(project_id, filename=None):
    """Remove rendered page PNGs — one document's dir when filename is given, the whole
    project's pages/ subdir otherwise. Never raises (absent targets are fine)."""
    target = pages_png_dir(project_id, os.path.splitext(filename)[0]) if filename else os.path.join(PAGES_DIR, project_id)
    shutil.rmtree(target, ignore_errors=True)


def render_pdf_pages(pdf_path, project_id, filename):
    """Render every page of pdf_path to pages/{project_id}/{stem}/page-{nnnn}.png at
    PAGE_RENDER_SCALE. The output dir is recreated from scratch, so pages of a shrunk or
    replaced source never survive a rerun. Returns the PNG file paths in page order."""
    stem = os.path.splitext(filename)[0]
    out_dir = pages_png_dir(project_id, stem)
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)

    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        paths = []
        for i in range(len(pdf)):
            pil = pdf[i].render(scale=PAGE_RENDER_SCALE).to_pil()
            png_path = os.path.join(out_dir, f"page-{i + 1:04d}.png")
            pil.convert("RGB").save(png_path, format="PNG")
            paths.append(png_path)
        return paths
    finally:
        pdf.close()


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

# Parent: covers a complete section, no overlap needed.
# Parents are never embedded (only Children are), so this cap does not affect matching
# precision — it only bounds payload duplication (each child copies the full parent text)
# and guards header-less flat files. Keep it large so "Parent = complete section" holds
# for real contract clauses.
PARENT_CHUNK_SIZE = 12000

parent_splitter = RecursiveCharacterTextSplitter(
    chunk_size=PARENT_CHUNK_SIZE,
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
        parent_texts = parent_splitter.split_text(section.page_content)
        if len(parent_texts) > 1:
            print(f"Warning: section '{breadcrumb}' ({len(section.page_content)} chars) exceeds "
                  f"PARENT_CHUNK_SIZE ({PARENT_CHUNK_SIZE}); split into {len(parent_texts)} parts — "
                  f"retrieval may truncate")
        for parent_text in parent_texts:
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

    # L2 structural fix (in-memory only; the file on disk is never modified):
    # promote dotted clause list lines ("- 10.3.6 ...") to "### <id>" headers so
    # each clause becomes its own parent, and nest letter subsection headers
    # ("## (A) General") under their parent section.
    content = ref_ids.promote_clause_headers(content)

    # Parent-Child chunking: split Parents first (with breadcrumb prefix), then split each Parent into Children;
    # only Children are vectorized; the full Parent text is stored in each Child's payload["text"]
    entries = []
    for p_idx, (breadcrumb, parent_text) in enumerate(split_parents_with_headers(content)):
        parent_full = f"{breadcrumb}\n\n{parent_text}" if breadcrumb else parent_text
        parent_id = f"{project_id}/{filename}#p{p_idx}"
        parent_ref_ids = ref_ids.extract_header_ref_ids(breadcrumb)
        mentioned_refs = ref_ids.extract_mentioned_refs(parent_text)
        for c_idx, child_text in enumerate(child_splitter.split_text(parent_text)):
            entries.append({
                "breadcrumb": breadcrumb,
                "parent_id": parent_id,
                "p_idx": p_idx,
                "c_idx": c_idx,
                "parent_full": parent_full,
                "embed_src": f"{breadcrumb}\n\n{child_text}" if breadcrumb else child_text,
                "parent_ref_ids": parent_ref_ids,
                "mentioned_refs": mentioned_refs,
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
                    "parent_ref_ids": entry["parent_ref_ids"],  # own section/clause IDs on the breadcrumb path (exact ref matching)
                    "mentioned_refs": entry["mentioned_refs"],  # ref IDs mentioned in the parent body (Referenced-by lookup)
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


# 8b. Page indexing (visual retrieval: EG2 page-as-image embeddings)

def index_pages_file(project_id, filename, progress_cb=None):
    """Index one approved PDF's pages as images: render → EG2 embed → upsert into the page
    collection. Mirrors ingest_file's safety order: the human-approval gate (finalized md in
    my_shared_docs) comes first, embedding runs before any deletion, and this file's previous
    page points are deleted only right before upsert — an embedder failure never destroys
    existing index state. Point IDs are deterministic (uuid5 "{project_id}:{filename}:page{n}"),
    matching the text-point scheme, so reruns are idempotent.
    progress_cb(done_pages, total_pages) fires per embed batch.
    Returns {"status": indexed|skipped, "pages": int, "message": str}."""
    if not filename.lower().endswith(".pdf"):
        return {"status": "skipped", "pages": 0, "message": f"{project_id}/{filename}: not a PDF; no page images to index"}

    md_path = finalized_md_path(project_id, filename)  # approval gate: only reviewed documents get page-indexed
    if md_path is None:
        return {"status": "skipped", "pages": 0,
                "message": f"{project_id}/{filename}: approved md not found in {DOCS_DIR}; approve the conversion first"}

    pdf_path = os.path.join(project_dir(PDF_INBOX_DIR, project_id), filename)
    if not os.path.isfile(pdf_path):
        return {"status": "skipped", "pages": 0,
                "message": f"{project_id}/{filename}: source PDF not found in pdf_inbox; cannot render pages"}

    print(f"Indexing pages: {project_id}/{filename}")
    png_paths = render_pdf_pages(pdf_path, project_id, filename)
    stem = os.path.splitext(filename)[0]

    points = []
    for start in range(0, len(png_paths), PAGE_EMBED_BATCH):
        batch = png_paths[start:start + PAGE_EMBED_BATCH]
        images_b64 = []
        for png_path in batch:
            with open(png_path, "rb") as f:
                images_b64.append(base64.b64encode(f.read()).decode("ascii"))
        vectors = embed_page_images(images_b64)
        for png_path, vector in zip(batch, vectors):
            page_number = int(os.path.splitext(os.path.basename(png_path))[0].rsplit("-", 1)[1])
            points.append(PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{project_id}:{filename}:page{page_number}")),
                vector=vector,
                payload={
                    "project_id": project_id,
                    "source": filename,
                    "page_number": page_number,
                    "page_image": page_png_relpath(project_id, stem, page_number),
                },
            ))
        if progress_cb:
            progress_cb(min(start + PAGE_EMBED_BATCH, len(png_paths)), len(png_paths))

    # Guard against vanish-during-embedding (same rationale as ingest_file: never write points
    # whose source or approval was removed while the embedder ran).
    if not (os.path.isfile(pdf_path) and os.path.exists(md_path)):
        msg = f"Warning: {project_id}/{filename} (or its approved md) disappeared during page indexing; aborted before upsert"
        print(msg)
        return {"status": "skipped", "pages": 0, "message": msg}

    # Delete this file's previous page points only now, after embedding succeeded.
    delete_points_pages(project_id=project_id, source=filename)

    print(f"Upserting {len(points)} page vectors into {PAGE_COLLECTION_NAME}...")
    for start in range(0, len(points), UPSERT_BATCH):
        qdrant.upsert(
            collection_name=PAGE_COLLECTION_NAME,
            points=points[start:start + UPSERT_BATCH]
        )
        print(f"  Uploaded {min(start + UPSERT_BATCH, len(points))}/{len(points)} pages")
    print("Done!")
    return {"status": "indexed", "pages": len(points),
            "message": f"Indexed {project_id}/{filename} ({len(points)} pages)"}


def index_pages_all(progress_cb=None):
    """CLI entry: index page images for every approved PDF (an inbox PDF whose finalized md
    already lives in my_shared_docs)."""
    indexed = 0
    for project_id, filename, filepath in scan_doc_sources(PDF_INBOX_DIR):
        if not filename.lower().endswith(".pdf"):
            continue
        if finalized_md_path(project_id, filename) is None:
            continue
        result = index_pages_file(project_id, filename, progress_cb=progress_cb)
        if result["status"] == "indexed":
            indexed += 1
    if not indexed:
        print("No approved PDFs to index (a PDF counts once its md is approved into my_shared_docs).")


# 9. Search (Parent-Child Small-to-Big retrieval + section-ID exact matching)

def _sub_in_text(text, sub):
    """True when the letter/number sub-item "(d)"/"(ii)" appears verbatim in the text."""
    return re.search(r"\(\s*" + re.escape(sub) + r"\s*\)", text or "", re.IGNORECASE) is not None


def _hit_from_payload(payload, score):
    return {
        "score": score,
        "project_id": payload.get("project_id"),
        "source": payload.get("source"),
        "headers": payload.get("headers"),
        "text": payload.get("text"),
    }


# Reciprocal Rank Fusion constant (standard k=60) for the text × page-image hybrid ranking.
RRF_K = 60


def _visual_page_hits(query, scope, vector_pool, limit, enabled):
    """Page-image half of hybrid retrieval. Returns (pages, source_page_rank, note):
    pages  = fused-order page hits [{project_id, source, page_number, score, page_image}] —
             score is the raw page cosine; the ORDER is RRF(page rank, best text rank of the
             page's source within vector_pool), so pages from documents that also match text
             rank above equally-similar pages of text-unrelated documents.
    source_page_rank = best page rank per source, feeding the text-fill fusion in search_full.
    note  = user-facing degradation note, or None.
    Never raises: missing page collection = nothing indexed yet (silent), embedder/Qdrant
    failure = text-only fallback with a note."""
    if not enabled:
        return [], {}, None
    note = None
    try:
        if not qdrant.collection_exists(PAGE_COLLECTION_NAME):
            return [], {}, None  # no pages indexed yet — not an error
        query_vector = embed_page_query(query)
        page_results = qdrant.query_points(
            collection_name=PAGE_COLLECTION_NAME, query=query_vector,
            query_filter=Filter(must=[scope]), limit=max(limit * 4, 24),
        ).points
    except Exception as e:
        return [], {}, f"[visual search unavailable ({e}); showing text-only results]"

    source_text_rank = {}
    for rank, hit in enumerate(vector_pool, 1):
        src = hit.get("source")
        if src is not None and src not in source_text_rank:
            source_text_rank[src] = rank

    fused, source_page_rank = [], {}
    for rank, res in enumerate(page_results, 1):
        payload = res.payload
        src = payload.get("source")
        if src is not None and src not in source_page_rank:
            source_page_rank[src] = rank
        rrf = 1.0 / (RRF_K + rank)
        if src in source_text_rank:
            rrf += 1.0 / (RRF_K + source_text_rank[src])
        fused.append((rrf, {
            "project_id": payload.get("project_id"),
            "source": src,
            "page_number": payload.get("page_number"),
            "score": res.score,
            "page_image": payload.get("page_image"),
        }))
    fused.sort(key=lambda pair: -pair[0])  # stable: ties keep page-cosine order
    return [entry for _, entry in fused[:limit]], source_page_rank, note


def _scroll_all(scroll_filter, limit_per_page=256, max_points=2048):
    """Scroll every point matching the filter (paginated); local corpus scale keeps this cheap."""
    points, offset = qdrant.scroll(
        collection_name=COLLECTION_NAME, scroll_filter=scroll_filter,
        limit=limit_per_page, with_payload=True,
    )
    while offset is not None and len(points) < max_points:
        page, offset = qdrant.scroll(
            collection_name=COLLECTION_NAME, scroll_filter=scroll_filter,
            limit=limit_per_page, offset=offset, with_payload=True,
        )
        points.extend(page)
    return points


def search_full(query, project_id=None, target_parents=3, ref=None, include_pages=None):
    """Search with section-ID reinforcement. Returns
    {"hits": [...], "notes": [str], "referenced_by": {ref_id: {source: count}},
    "pages": [{project_id, source, page_number, score, page_image}]}.

    hits is the same list of hit dicts as search() (one per deduplicated parent),
    where exact ref matches additionally carry "matched_refs": [ref_id, ...].
    notes carries e.g. "[ref 'X' not found exactly; showing semantic results]".
    referenced_by (only when exact hits exist) lists, per matched ref id, the
    source files whose content mentions it and their distinct-parent counts —
    the reverse-citation lookup for compliance-matrix work.

    Flow: detect refs in the query (ref_ids.normalize_query_refs; an explicit
    `ref` overrides and is trusted) → existence-check bare candidates against
    parent_ref_ids → exact match (depth >= 3 clauses and alphanumeric IDs
    return their own parent; shallower section/schedule refs vector-rank
    children within the ref filter) → blend with plain vector results up to
    target_parents → referenced_by. Zero exact hits → pure vector behavior.

    Visual branch (include_pages=None → ENABLE_VISUAL env, default on): the
    query is also embedded by the page model and matched against page images;
    the vector-fill order of hits and the new pages list are then fused by RRF
    per source. Exact ref hits always stay first and keep their payload/score
    (fusion touches only vector-ranked fill); with the branch off, hits are
    byte-identical to the text-only search and pages is [].

    When project_id is given, only that project is searched, automatically
    including global shared documents; when project_id is omitted, only global
    shared documents are searched. Raises RuntimeError when Ollama is unreachable."""
    query_vector = embed_texts([query])[0]

    scope = FieldCondition(
        key="project_id",
        match=MatchAny(any=[project_id, "global"]) if project_id else MatchValue(value="global"),
    )

    # 1. Detect refs (explicit `ref` overrides auto-detection and is trusted)
    if ref is not None and str(ref).strip():
        tokens = ref_ids.normalize_query_refs(str(ref))
        if not tokens:
            notes_hint = str(ref).strip()
            tokens, notes = [], [f"[ref '{notes_hint}' not recognized as a section/clause/schedule/item ID; showing semantic results]"]
        else:
            notes = []
            for t in tokens:
                t["bare"] = False
    else:
        tokens = ref_ids.normalize_query_refs(query)
        notes = []
    tokens = tokens[:4]  # keep latency bounded on pathological queries

    # 2. Existence-check bare candidates against parent_ref_ids (kills false
    # positives like "IEC 60502-1" that happen to look like an ID)
    confirmed = []
    for t in tokens:
        if not t["bare"]:
            confirmed.append(t)
            continue
        try:
            exists = qdrant.count(
                COLLECTION_NAME, exact=True,
                count_filter=Filter(must=[scope, FieldCondition(key="parent_ref_ids", match=MatchValue(value=t["id"]))]),
            ).count
        except Exception:
            exists = 0
        if exists:
            confirmed.append(t)

    # 3. Exact match per confirmed ref
    exact_hits = []
    exact_parent_ids = set()
    for t in confirmed:
        ref_filter = Filter(must=[scope, FieldCondition(key="parent_ref_ids", match=MatchValue(value=t["id"]))])
        deep = t["kind"] == "alnum" or (t["kind"] == "clause" and t["id"].count(".") >= 2)
        if deep:
            # depth >= 3 dotted clause or alphanumeric item: its own parent, first block
            points, _ = qdrant.scroll(COLLECTION_NAME, scroll_filter=ref_filter, limit=64, with_payload=True)
            if not points:
                continue
            best = min(points, key=lambda p: (p.payload.get("parent_index") or 0, p.payload.get("child_index") or 0))
            p_id = best.payload.get("parent_id") or str(best.id)
            hit = _hit_from_payload(best.payload, 1.0)
            hit["matched_refs"] = [t["id"]]
            if p_id in exact_parent_ids:
                for h in exact_hits:
                    if h.get("parent_id") == p_id and t["id"] not in h["matched_refs"]:
                        h["matched_refs"].append(t["id"])
                exact_parent_ids.add(p_id)
                continue
            hit["parent_id"] = p_id
            exact_parent_ids.add(p_id)
            if t.get("sub") and not _sub_in_text(hit["text"], t["sub"]):
                notes.append(f"[sub-item ({t['sub']}) not found verbatim in {t['id']}; returning the whole clause]")
            exact_hits.append(hit)
        else:
            # section / 2-segment clause / schedule: many parents — vector-rank
            # children within the ref filter, return the top target_parents clauses
            ranked = qdrant.query_points(
                collection_name=COLLECTION_NAME, query=query_vector,
                query_filter=ref_filter, limit=max(target_parents * 5, 10),
            ).points
            added, sub_found = 0, t.get("sub") is None
            for res in ranked:
                if added >= target_parents:
                    break
                p_id = res.payload.get("parent_id") or str(res.id)
                if _sub_in_text(res.payload.get("text"), t.get("sub") or ""):
                    sub_found = True
                if p_id in exact_parent_ids:
                    continue
                hit = _hit_from_payload(res.payload, res.score)
                hit["matched_refs"] = [t["id"]]
                hit["parent_id"] = p_id
                exact_parent_ids.add(p_id)
                exact_hits.append(hit)
                added += 1
            if t.get("sub") and not sub_found:
                notes.append(f"[sub-item ({t['sub']}) not found verbatim in {t['id']}; returning the section]")

    # 4. Blend: exact hits first, then fill up to target_parents with plain vector results
    # deduplicated against the exact hits. Candidates are collected into vector_pool (pure
    # text-vector order) so the visual branch can fuse the fill order below; with the visual
    # branch off the pool order IS the final fill order (byte-identical to the text-only search).
    hits = exact_hits
    vector_pool = []
    if len(hits) < target_parents:
        fetch_limit = target_parents * 5
        results = qdrant.query_points(
            collection_name=COLLECTION_NAME, query=query_vector,
            query_filter=Filter(must=[scope]), limit=fetch_limit,
        ).points
        for res in results:
            p_id = res.payload.get("parent_id") or str(res.id)
            if p_id in exact_parent_ids:
                continue
            hit = _hit_from_payload(res.payload, res.score)
            hit["parent_id"] = p_id
            exact_parent_ids.add(p_id)
            vector_pool.append(hit)

    # 4b. Visual branch: page-image retrieval, RRF-fused with the text ranking per source.
    # Failures degrade to text-only (pages=[] + note); exact hits are never touched.
    pages, source_page_rank, visual_note = _visual_page_hits(
        query, scope, vector_pool, PAGE_RESULTS,
        enabled=ENABLE_VISUAL if include_pages is None else bool(include_pages),
    )
    if visual_note:
        notes.append(visual_note)

    if len(hits) < target_parents:
        def fill_rrf(text_rank, source):
            score = 1.0 / (RRF_K + text_rank)
            page_rank = source_page_rank.get(source)
            if page_rank is not None:
                score += 1.0 / (RRF_K + page_rank)
            return score

        ordered = sorted(
            enumerate(vector_pool),
            key=lambda item: -fill_rrf(item[0] + 1, item[1].get("source")),
        )  # stable sort: equal fused scores keep text order; no page signal ⇒ pure text order
        hits = hits + [hit for _, hit in ordered[:target_parents - len(hits)]]

    matched = {rid for h in exact_hits for rid in h.get("matched_refs", [])}
    for t in confirmed:
        if t["id"] not in matched:
            notes.append(f"[ref '{t['id']}' not found exactly; showing semantic results]")

    # 5. Reverse citation lookup: which sources mention the matched refs?
    referenced_by = {}
    for t in confirmed:
        if t["id"] not in matched:
            continue
        points = _scroll_all(Filter(must=[
            scope,
            FieldCondition(key="mentioned_refs", match=MatchValue(value=t["id"])),
        ]))
        counts, seen_parents = {}, set()
        for p in points:
            p_id = p.payload.get("parent_id") or str(p.id)
            if p_id in seen_parents:
                continue
            seen_parents.add(p_id)
            source = p.payload.get("source")
            counts[source] = counts.get(source, 0) + 1
        if counts:
            referenced_by[t["id"]] = dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    return {"hits": hits, "notes": notes, "referenced_by": referenced_by, "pages": pages}


def search(query, project_id=None, target_parents=3, ref=None):
    """Search the shared knowledge base. Returns a list of hit dicts
    [{"score", "project_id", "source", "headers", "text", ...}] — one per
    deduplicated parent — or an empty list when nothing matches; exact ref
    matches additionally carry "matched_refs". See search_full for the
    section-ID behavior and metadata. Raises RuntimeError when Ollama is
    unreachable."""
    return search_full(query, project_id=project_id, target_parents=target_parents, ref=ref)["hits"]
