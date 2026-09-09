"""CLI wrapper: ingest shared documents into Qdrant (Parent-Child chunking).

All logic lives in rag_core.py (anchored to the repo directory, so this works from any CWD)."""

import argparse
import os

import rag_core


def main():
    parser = argparse.ArgumentParser(description="Ingest shared documents into Qdrant (Parent-Child chunking)")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Delete and recreate the collection (recommended after chunking format changes, to avoid leftover points in the old format)",
    )
    args = parser.parse_args()

    os.makedirs(rag_core.DOCS_DIR, exist_ok=True)
    if args.rebuild and rag_core.qdrant.collection_exists(rag_core.COLLECTION_NAME):
        rag_core.qdrant.delete_collection(rag_core.COLLECTION_NAME)
        print(f"Deleted old collection: {rag_core.COLLECTION_NAME}")
    rag_core.ensure_collection()
    print(f"Put shared documents into {rag_core.DOCS_DIR} (first-level subfolder names are project_ids; root-level files = global), then run this script again.")
    rag_core.ingest_all()


if __name__ == "__main__":
    main()
