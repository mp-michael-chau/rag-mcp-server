import os
import json
import uuid
import argparse
import urllib.request
from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance, PointStruct
from langchain_text_splitters import RecursiveCharacterTextSplitter, MarkdownHeaderTextSplitter

# 1. System settings
QDRANT_HOST = "localhost"
QDRANT_PORT = 6333
COLLECTION_NAME = "kilo-projects-info"
DOCS_DIR = "./my_shared_docs" # Put the files you want to share across projects here (md/txt only; PDFs go through convert_pdfs.py first)
OLLAMA_HOST = "http://localhost:11434"
EMBED_MODEL = "bge-m3"

# 2. Initialize database connection
qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)

def embed_texts(texts):
    """Call the Ollama embedding API in batches"""
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/embed",
        data=json.dumps({"model": EMBED_MODEL, "input": texts}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)["embeddings"]

# 3. Ensure the collection exists (dimension determined by the actual Ollama model output)
def ensure_collection():
    if not qdrant.collection_exists(COLLECTION_NAME):
        qdrant.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(
                size=len(embed_texts(["dimension probe"])[0]),
                distance=Distance.COSINE
            )
        )

    # project_id payload index (idempotent: qdrant returns 409 if it already exists, which we ignore)
    try:
        qdrant.create_payload_index(COLLECTION_NAME, field_name="project_id", field_schema="keyword")
    except Exception:
        pass

# 4. Text chunking settings: Parent-Child (Small-to-Big) architecture
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

def ingest_directory():
    points = []

    # Two-level scan: root-level files = global; first-level subfolder names are project_ids (skip folders starting with . and don't recurse deeper)
    doc_sources = []
    for entry in sorted(os.listdir(DOCS_DIR)):
        entry_path = os.path.join(DOCS_DIR, entry)
        if os.path.isdir(entry_path):
            if entry.startswith("."):
                continue
            for filename in sorted(os.listdir(entry_path)):
                filepath = os.path.join(entry_path, filename)
                if os.path.isfile(filepath):
                    doc_sources.append((entry, filename, filepath))
        elif os.path.isfile(entry_path):
            doc_sources.append(("global", entry, entry_path))

    for project_id, filename, filepath in doc_sources:
        if filename.lower().endswith('.pdf'):
            print(f"Warning: PDFs are not ingested directly; put them in pdf_inbox/, run convert_pdfs.py, review in md_review/, then move the md here ({project_id}/{filename} skipped)")
            continue
        elif filename.endswith(('.txt', '.md')):
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
        else:
            continue

        if not content.strip():
            print(f"Warning: no text found in {project_id}/{filename}; skipped")
            continue

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
        EMBED_BATCH = 32
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

    if points:
        print(f"Upserting {len(points)} chunks into Qdrant...")
        # Upload in batches to avoid exceeding Qdrant's single-request payload limit (32MB)
        BATCH_SIZE = 100
        for start in range(0, len(points), BATCH_SIZE):
            batch = points[start:start + BATCH_SIZE]
            qdrant.upsert(
                collection_name=COLLECTION_NAME,
                points=batch
            )
            print(f"  Uploaded {min(start + BATCH_SIZE, len(points))}/{len(points)} chunks")
        print("Done!")
    else:
        print("No ingestible documents found.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest shared documents into Qdrant (Parent-Child chunking)")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Delete and recreate the collection (recommended after chunking format changes, to avoid leftover points in the old format)",
    )
    args = parser.parse_args()

    os.makedirs(DOCS_DIR, exist_ok=True)
    if args.rebuild and qdrant.collection_exists(COLLECTION_NAME):
        qdrant.delete_collection(COLLECTION_NAME)
        print(f"Deleted old collection: {COLLECTION_NAME}")
    ensure_collection()
    print(f"Put shared documents into {DOCS_DIR} (first-level subfolder names are project_ids; root-level files = global), then run this script again.")
    ingest_directory()
