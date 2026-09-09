import os
import argparse
import pypdfium2 as pdfium
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode

# 1. Folder layout (mirrors the my_shared_docs convention: root-level files = global, first-level subfolders = project_id)
PDF_INBOX_DIR = "./pdf_inbox" # Drop PDFs to convert here
MD_REVIEW_DIR = "./md_review" # Docling output awaiting user review; moving the finished .md into my_shared_docs IS the approval
DOCS_DIR = "./my_shared_docs" # Read-only check: if the finalized md already lives here, skip reconversion
CONVERT_BATCH_PAGES = 40 # Pages per conversion batch (limits memory usage; resumable after interruption)

# 2. Docling PDF converter (ACCURATE mode for table recognition: slower but better table reconstruction)
pdf_pipeline = PdfPipelineOptions()
pdf_pipeline.table_structure_options.mode = TableFormerMode.ACCURATE
converter = DocumentConverter(
    format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_pipeline)}
)

def get_pdf_page_count(filepath):
    pdf = pdfium.PdfDocument(filepath)
    try:
        return len(pdf)
    finally:
        pdf.close()

def convert_pdf(pdf_path, review_path):
    """Convert PDF to Markdown with Docling in batches, writing each batch to the review file; rerunning after interruption resumes from the breakpoint"""
    progress_path = review_path + ".progress"

    # Review md exists, conversion finished (no .progress) and not older than the source PDF → keep it (preserves manual edits made during review)
    if (os.path.exists(review_path) and not os.path.exists(progress_path)
            and os.path.getmtime(review_path) >= os.path.getmtime(pdf_path)):
        print(f"  Review md up to date, skip: {review_path}")
        return

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
            start = end + 1

    os.remove(progress_path)
    with open(review_path, "r", encoding="utf-8") as f:
        if not f.read().strip():
            print(f"Warning: conversion produced no text for {pdf_path}; it may be a scan without a text layer")

def convert_inbox():
    if not os.path.isdir(PDF_INBOX_DIR):
        print(f"No {PDF_INBOX_DIR} directory found. Put PDFs into {PDF_INBOX_DIR}/{{project_id}}/ (root-level files = global) first.")
        return

    # Two-level scan: root-level files = global; first-level subfolder names are project_ids (skip folders starting with . and don't recurse deeper)
    pdf_sources = []
    for entry in sorted(os.listdir(PDF_INBOX_DIR)):
        entry_path = os.path.join(PDF_INBOX_DIR, entry)
        if os.path.isdir(entry_path):
            if entry.startswith("."):
                continue
            for filename in sorted(os.listdir(entry_path)):
                filepath = os.path.join(entry_path, filename)
                if os.path.isfile(filepath) and filename.lower().endswith(".pdf"):
                    pdf_sources.append((entry, filename, filepath))
        elif os.path.isfile(entry_path) and entry.lower().endswith(".pdf"):
            pdf_sources.append(("global", entry, entry_path))

    if not pdf_sources:
        print(f"No PDFs found in {PDF_INBOX_DIR}.")
        return

    for project_id, filename, filepath in pdf_sources:
        stem = os.path.splitext(filename)[0]

        # Finalized md already moved into my_shared_docs → never reconvert (protects the reviewed/approved copy);
        # global documents live at the my_shared_docs root (mirroring the two-level scan), so check both locations
        finalized_paths = [os.path.join(DOCS_DIR, project_id, stem + ".md")]
        if project_id == "global":
            finalized_paths.append(os.path.join(DOCS_DIR, stem + ".md"))
        if any(os.path.exists(p) for p in finalized_paths):
            print(f"PDF: {project_id}/{filename} → finalized md already present, skip")
            continue

        print(f"PDF: {project_id}/{filename}")
        review_path = os.path.join(MD_REVIEW_DIR, project_id, stem + ".md")
        os.makedirs(os.path.dirname(review_path), exist_ok=True)
        try:
            convert_pdf(filepath, review_path)
        except Exception as e:
            print(f"Warning: Docling conversion failed for {project_id}/{filename} ({e}); skipped")

    print(f"Done. Review/edit the Markdown in {MD_REVIEW_DIR}/, then move the finalized .md files into {DOCS_DIR}/ and run ingest_docs.py.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert PDFs from pdf_inbox/ into reviewable Markdown in md_review/ (Docling; resumable)")
    parser.parse_args()
    convert_inbox()
