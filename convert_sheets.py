"""CLI wrapper: convert Excel/CSV files (.xlsx/.xlsm/.csv) from pdf_inbox/ into reviewable
Markdown in md_review/ (one ### block per row, mirroring flatten_md_tables.py output).

All logic lives in rag_core.py (anchored to the repo directory, so this works from any CWD)."""

import argparse

import rag_core


def main():
    parser = argparse.ArgumentParser(description="Convert Excel/CSV (.xlsx/.xlsm/.csv) from pdf_inbox/ into reviewable Markdown in md_review/ (one ### block per row)")
    parser.parse_args()
    rag_core.convert_sheets_inbox()


if __name__ == "__main__":
    main()
