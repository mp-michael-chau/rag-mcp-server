"""Offline tests for rag_core parent-child chunking — no Qdrant/Ollama needed
(rag_core import is safe: QdrantClient(...) construction performs no network call)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rag_core
import ref_ids


def build_clause_doc(items="abcdefghijklmnop", item_chars=150):
    """Synthetic clause: promoted '- 10.3.4' line + lettered items, ~item_chars each."""
    filler = "x" * item_chars
    lines = [
        "## 10.3 Energy Monitoring System ('EMS')",
        "",
        "- 10.3.4 The EMS dashboard shall fulfil the following functional requirements:",
        "",
    ]
    for letter in items:
        lines.append(f"- ({letter}) {filler}")
        lines.append("")
    return "\n".join(lines)


def test_long_clause_is_single_parent():
    # ~2.4k chars: exceeds the old 2000 cap, must stay ONE parent under PARENT_CHUNK_SIZE=12000
    doc = build_clause_doc()
    content = ref_ids.promote_clause_headers(doc)
    parents = rag_core.split_parents_with_headers(content)
    clause_parents = [p for p in parents if p[0].endswith("10.3.4")]
    assert len(clause_parents) == 1
    text = clause_parents[0][1]
    assert "(a) " in text
    assert "(p) " in text


def test_oversize_section_still_splits():
    # A section larger than PARENT_CHUNK_SIZE falls back to length splitting (≥2 parents)
    doc = build_clause_doc(items="abcdefghijklmnopqrstuvwx", item_chars=600)  # ~14.4k chars
    content = ref_ids.promote_clause_headers(doc)
    parents = rag_core.split_parents_with_headers(content)
    clause_parents = [p for p in parents if p[0].endswith("10.3.4")]
    assert len(clause_parents) >= 2
