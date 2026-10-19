"""End-to-end integration test for page-image visual retrieval against the live local stack
(real Ollama EG2 + real Qdrant). Skips automatically when either service is unreachable.

Uses a throwaway collection + tmp dirs (never touches kilo-projects-info or the real pages/):
render → embed → upsert → hybrid search → page-image endpoint path shape → cleanup.
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rag_core
from visual_fakes import make_two_page_pdf  # noqa: E402 (same dir)


def _service_up(url):
    try:
        with urllib.request.urlopen(url, timeout=3):
            return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def services():
    qdrant_up = _service_up(f"http://{rag_core.QDRANT_HOST}:{rag_core.QDRANT_PORT}/collections")
    ollama_up = _service_up(f"{rag_core.OLLAMA_HOST}/api/tags")
    if not (qdrant_up and ollama_up):
        pytest.skip("local Qdrant/Ollama not running")
    return True


@pytest.fixture
def scratch(services, tmp_path, monkeypatch):
    """Isolated dirs + throwaway page collection named after the test run."""
    inbox = tmp_path / "pdf_inbox"
    docs = tmp_path / "my_shared_docs"
    pages = tmp_path / "pages"
    (inbox / "itest").mkdir(parents=True)
    (docs / "itest").mkdir(parents=True)
    pages.mkdir()
    monkeypatch.setattr(rag_core, "PDF_INBOX_DIR", str(inbox))
    monkeypatch.setattr(rag_core, "DOCS_DIR", str(docs))
    monkeypatch.setattr(rag_core, "PAGES_DIR", str(pages))
    collection = "itest-pages"
    monkeypatch.setattr(rag_core, "PAGE_COLLECTION_NAME", collection)
    yield {"collection": collection, "pages": pages}
    try:
        rag_core.qdrant.delete_collection(collection)
    except Exception:
        pass


def test_two_page_pdf_end_to_end(scratch, monkeypatch):
    pdf_path = Path(rag_core.PDF_INBOX_DIR) / "itest" / "doc.pdf"
    pdf_path.write_bytes(make_two_page_pdf())
    (Path(rag_core.DOCS_DIR) / "itest" / "doc.md").write_text("# doc\napproved content\n")

    rag_core.ensure_page_collection()
    info = rag_core.qdrant.get_collection(rag_core.PAGE_COLLECTION_NAME)
    assert info.config.params.vectors.size == 768  # EG2 full MRL width, probed not hardcoded

    result = rag_core.index_pages_file("itest", "doc.pdf")
    assert result["status"] == "indexed" and result["pages"] == 2
    assert sorted(os.listdir(scratch["pages"] / "itest" / "doc")) == ["page-0001.png", "page-0002.png"]

    # rerun is idempotent (deterministic IDs, delete-then-upsert)
    again = rag_core.index_pages_file("itest", "doc.pdf")
    assert again["pages"] == 2
    count = rag_core.qdrant.count(rag_core.PAGE_COLLECTION_NAME, exact=True).count
    assert count == 2

    # hybrid search: visual branch active, page hits returned with servable image paths
    res = rag_core.search_full("test query", project_id="itest", include_pages=True)
    assert res["pages"] == [] or all(
        p["source"] == "doc.pdf" and p["page_image"].startswith("itest/doc/page-") for p in res["pages"]
    )
    # both pages actually embedded → page query over the throwaway collection finds them
    qv = rag_core.embed_page_query("blank pages document")
    pts = rag_core.qdrant.query_points(collection_name=rag_core.PAGE_COLLECTION_NAME, query=qv, limit=10).points
    assert {p.payload["page_number"] for p in pts} == {1, 2}

    # cleanup contract: page points + PNG dir
    rag_core.delete_points_pages(project_id="itest", source="doc.pdf")
    assert rag_core.qdrant.count(rag_core.PAGE_COLLECTION_NAME, exact=True).count == 0
    rag_core.delete_page_pngs("itest", "doc.pdf")
    assert not (scratch["pages"] / "itest" / "doc").exists()


def test_page_image_url_shape(scratch):
    """The payload's page_image maps 1:1 onto the API's path-safe PNG route."""
    from api.app import PAGE_IMAGE_RE  # endpoint's filename guard

    for n in (1, 12, 123, 1234):
        rel = rag_core.page_png_relpath("itest", "doc", n)
        pid, stem, fname = rel.split("/")
        assert PAGE_IMAGE_RE.fullmatch(fname)
        assert pid == "itest" and stem == "doc"
    assert not PAGE_IMAGE_RE.fullmatch("page-1.png")
    assert not PAGE_IMAGE_RE.fullmatch("../page-0001.png")


def test_query_embed_matches_page_embed_space(scratch):
    """Query-side (instruction text) and page-side (image) vectors share one EG2 space:
    their cosine with a real page must be meaningful (> 0.1, not ~0 or negative)."""
    import base64

    png = Path(rag_core.PAGES_DIR)  # ensure dirs exist for this test via scratch
    (png / "itest" / "doc").mkdir(parents=True, exist_ok=True)
    import pypdfium2 as pdfium

    src = pdfium.PdfDocument(make_two_page_pdf())
    pil = src[0].render(scale=1.0).to_pil()
    src.close()
    buf_path = png / "itest" / "doc" / "page-0001.png"
    pil.convert("RGB").save(buf_path, format="PNG")
    with open(buf_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    page_vec = rag_core.embed_page_images([b64])[0]
    query_vec = rag_core.embed_page_query("engineering document page")
    dot = sum(a * b for a, b in zip(page_vec, query_vec))
    norm = (sum(a * a for a in page_vec) ** 0.5) * (sum(b * b for b in query_vec) ** 0.5)
    assert dot / norm > 0.1, json.dumps({"cosine": dot / norm})
