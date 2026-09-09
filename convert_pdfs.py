"""CLI wrapper: convert PDFs from pdf_inbox/ into reviewable Markdown in md_review/ (Docling; resumable).

All logic lives in rag_core.py (anchored to the repo directory, so this works from any CWD)."""

import argparse

import rag_core


def main():
    parser = argparse.ArgumentParser(description="Convert PDFs from pdf_inbox/ into reviewable Markdown in md_review/ (Docling; resumable)")
    parser.parse_args()
    rag_core.convert_inbox()


if __name__ == "__main__":
    main()
