"""CLI wrapper: index approved PDFs' pages as images into the Qdrant page collection
(EmbeddingGemma 2 via Ollama) for visual retrieval.

All logic lives in rag_core.py (anchored to the repo directory, so this works from any CWD).
A PDF is page-indexed only after its conversion md has been approved into my_shared_docs
(the same human-approval gate as text ingestion)."""

import argparse
import os

import rag_core


def main():
    parser = argparse.ArgumentParser(
        description="Index approved PDFs' pages as images into Qdrant (EmbeddingGemma 2 visual retrieval)")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Delete and recreate the page collection (recommended after changing PAGE_EMBED_MODEL, "
             "to avoid mixing vectors from different embedding spaces)",
    )
    args = parser.parse_args()

    os.makedirs(rag_core.PAGES_DIR, exist_ok=True)
    if args.rebuild and rag_core.qdrant.collection_exists(rag_core.PAGE_COLLECTION_NAME):
        rag_core.qdrant.delete_collection(rag_core.PAGE_COLLECTION_NAME)
        print(f"Deleted old page collection: {rag_core.PAGE_COLLECTION_NAME}")
    rag_core.ensure_page_collection()
    rag_core.index_pages_all()


if __name__ == "__main__":
    main()
