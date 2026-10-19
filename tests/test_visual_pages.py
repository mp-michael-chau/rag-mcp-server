"""Offline tests for the page-image visual retrieval pipeline — no Ollama needed and no real
Qdrant (the client is stubbed; rag_core import is safe: QdrantClient(...) performs no network
call). A minimal 2-page PDF is synthesized in-memory so rendering runs through real pypdfium2."""

import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rag_core

sys.path.insert(0, str(Path(__file__).resolve().parent))
from visual_fakes import FakeQdrant, make_two_page_pdf  # noqa: E402


def _setup_dirs(tmp_path, monkeypatch):
    """Point rag_core's data dirs at a tmp tree; returns (inbox, docs, pages) paths."""
    inbox = tmp_path / "pdf_inbox"
    docs = tmp_path / "my_shared_docs"
    pages = tmp_path / "pages"
    proj = inbox / "test-proj"
    proj.mkdir(parents=True)
    docs.mkdir()
    pages.mkdir()
    monkeypatch.setattr(rag_core, "PDF_INBOX_DIR", str(inbox))
    monkeypatch.setattr(rag_core, "DOCS_DIR", str(docs))
    monkeypatch.setattr(rag_core, "PAGES_DIR", str(pages))
    return proj, docs, pages


def _fake_embedder(monkeypatch, dim=4):
    calls = {"n": 0}

    def embed(images_b64):
        calls["n"] += len(images_b64)
        return [[0.05] * dim for _ in images_b64]

    monkeypatch.setattr(rag_core, "embed_page_images", embed)
    return calls


# --- index_pages_file ---

def test_index_pages_gates_and_happy_path(tmp_path, monkeypatch):
    proj, docs, pages = _setup_dirs(tmp_path, monkeypatch)
    fake = FakeQdrant(exists={rag_core.PAGE_COLLECTION_NAME})
    monkeypatch.setattr(rag_core, "qdrant", fake)
    _fake_embedder(monkeypatch)

    pdf = proj / "spec.pdf"
    pdf.write_bytes(make_two_page_pdf())

    # no approved md yet → human-approval gate skips
    r = rag_core.index_pages_file("test-proj", "spec.pdf")
    assert r["status"] == "skipped" and r["pages"] == 0
    assert fake.calls == [] and not (pages / "test-proj").exists()

    # non-PDF never renders
    (docs / "test-proj").mkdir(parents=True)
    r = rag_core.index_pages_file("test-proj", "notes.md")
    assert r["status"] == "skipped" and fake.calls == []

    # missing source PDF (md approved, inbox PDF gone) → skip
    (docs / "test-proj" / "gone.md").write_text("# gone\n")
    r = rag_core.index_pages_file("test-proj", "gone.pdf")
    assert r["status"] == "skipped" and "source PDF not found" in r["message"]

    # happy path: approved md present → render, embed, delete-then-upsert
    (docs / "test-proj" / "spec.md").write_text("# spec\ncontent")
    progress = []
    r = rag_core.index_pages_file("test-proj", "spec.pdf", progress_cb=lambda d, t: progress.append((d, t)))
    assert r["status"] == "indexed" and r["pages"] == 2
    assert progress == [(2, 2)] or progress == [(4, 2)]  # one batch may overshoot total like ingest's

    # deterministic point IDs, stable payload scheme
    upserts = [p for (coll, ps) in fake.upserts for p in ps]
    assert {p.id for p in upserts} == {
        str(uuid.uuid5(uuid.NAMESPACE_URL, "test-proj:spec.pdf:page1")),
        str(uuid.uuid5(uuid.NAMESPACE_URL, "test-proj:spec.pdf:page2")),
    }
    assert {p.payload["page_number"] for p in upserts} == {1, 2}
    assert {p.payload["page_image"] for p in upserts} == {
        "test-proj/spec/page-0001.png", "test-proj/spec/page-0002.png",
    }
    assert all(p.payload["project_id"] == "test-proj" and p.payload["source"] == "spec.pdf" for p in upserts)

    # safety order: this file's page points deleted only right before the upsert
    del_pages = [c for c in fake.calls if c[0] == "delete_pages"]
    assert len(del_pages) == 1
    assert fake.calls.index(del_pages[0]) < next(i for i, c in enumerate(fake.calls) if c[0] == "upsert")
    assert del_pages[0][2] == "test-proj" and del_pages[0][3] == "spec.pdf"

    # PNGs on disk in page order
    pngs = sorted(os.listdir(pages / "test-proj" / "spec"))
    assert pngs == ["page-0001.png", "page-0002.png"]

    # rerun → identical IDs (idempotent), old points deleted again
    r2 = rag_core.index_pages_file("test-proj", "spec.pdf")
    assert r2["status"] == "indexed" and r2["pages"] == 2
    upserts2 = [p.id for (coll, ps) in fake.upserts for p in ps]
    assert upserts2[:2] == upserts2[2:4]


def test_index_pages_vanish_guard(tmp_path, monkeypatch):
    """Source removed while the embedder ran → no delete, no upsert (index state untouched)."""
    proj, docs, pages = _setup_dirs(tmp_path, monkeypatch)
    fake = FakeQdrant(exists={rag_core.PAGE_COLLECTION_NAME})
    monkeypatch.setattr(rag_core, "qdrant", fake)
    pdf = proj / "spec.pdf"
    pdf.write_bytes(make_two_page_pdf())
    (docs / "test-proj").mkdir(parents=True)
    (docs / "test-proj" / "spec.md").write_text("# spec\n")

    def embed_then_remove(images_b64):
        os.remove(pdf)  # vanish mid-embedding
        return [[0.05] * 4 for _ in images_b64]

    monkeypatch.setattr(rag_core, "embed_page_images", embed_then_remove)
    r = rag_core.index_pages_file("test-proj", "spec.pdf")
    assert r["status"] == "skipped" and "disappeared" in r["message"]
    assert fake.upserts == []
    assert all(c[0] != "delete_pages" for c in fake.calls)


def test_render_recreates_png_dir_from_scratch(tmp_path, monkeypatch):
    proj, docs, pages = _setup_dirs(tmp_path, monkeypatch)
    pdf = proj / "spec.pdf"
    pdf.write_bytes(make_two_page_pdf())
    stale = pages / "test-proj" / "spec" / "page-0009.png"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"stale")
    paths = rag_core.render_pdf_pages(str(pdf), "test-proj", "spec.pdf")
    assert [os.path.basename(p) for p in paths] == ["page-0001.png", "page-0002.png"]
    assert not stale.exists()  # a shrunk source must not leave stale pages behind


def test_delete_page_pngs(tmp_path, monkeypatch):
    proj, docs, pages = _setup_dirs(tmp_path, monkeypatch)
    d = pages / "test-proj" / "spec"
    d.mkdir(parents=True)
    (d / "page-0001.png").write_bytes(b"x")
    (pages / "other").mkdir()
    rag_core.delete_page_pngs("test-proj", "spec.pdf")
    assert not d.exists() and (pages / "other").exists()  # filename scope only
    rag_core.delete_page_pngs("test-proj")  # whole project, absent dirs fine
    assert not (pages / "test-proj").exists()


def test_delete_points_pages_noop_without_collection(tmp_path, monkeypatch):
    fake = FakeQdrant(exists=set())
    monkeypatch.setattr(rag_core, "qdrant", fake)
    rag_core.delete_points_pages(project_id="p", source="s.pdf")  # must not raise / call
    assert fake.deletes == []


# --- search fusion ---

def _text_point(source, parent_id, score, text="parent text"):
    return {"score": score, "payload": {
        "project_id": "p1", "source": source, "headers": "h", "text": text, "parent_id": parent_id,
    }}


def _page_point(source, page_number, score):
    stem = source[:-4] if source.endswith(".pdf") else source
    return {"score": score, "payload": {
        "project_id": "p1", "source": source, "page_number": page_number,
        "page_image": f"p1/{stem}/page-{page_number:04d}.png",
    }}


def _setup_search(monkeypatch, fake, dim=4):
    monkeypatch.setattr(rag_core, "qdrant", fake)
    monkeypatch.setattr(rag_core, "embed_texts", lambda texts: [[0.01] * dim for _ in texts])


def test_search_visual_off_is_text_order(tmp_path, monkeypatch):
    fake = FakeQdrant(exists={rag_core.PAGE_COLLECTION_NAME},
                      text_results=[_text_point("a.md", "A", 0.9), _text_point("b.pdf", "B", 0.8)],
                      page_results=[_page_point("b.pdf", 5, 0.7)])
    _setup_search(monkeypatch, fake)
    monkeypatch.setattr(rag_core, "embed_page_query", lambda q: [0.02] * 4)
    monkeypatch.setattr(rag_core, "ENABLE_VISUAL", False)

    res = rag_core.search_full("storage requirements")
    assert [h["parent_id"] for h in res["hits"]] == ["A", "B"]  # pure text order
    assert res["pages"] == [] and fake.page_queries == []  # branch fully skipped


def test_search_rrf_promotes_page_matched_source(tmp_path, monkeypatch):
    # text ranking prefers a.md; the page ranking prefers b.pdf → fused fill must flip to b.pdf
    fake = FakeQdrant(exists={rag_core.PAGE_COLLECTION_NAME},
                      text_results=[_text_point("a.md", "A", 0.9), _text_point("b.pdf", "B", 0.8)],
                      page_results=[_page_point("b.pdf", 5, 0.7)])
    _setup_search(monkeypatch, fake)
    monkeypatch.setattr(rag_core, "embed_page_query", lambda q: [0.02] * 4)

    res = rag_core.search_full("storage requirements")
    assert [h["parent_id"] for h in res["hits"]] == ["B", "A"]  # RRF flip within the fill
    assert res["pages"][0]["source"] == "b.pdf" and res["pages"][0]["page_number"] == 5
    assert res["pages"][0]["page_image"] == "p1/b/page-0005.png"
    assert res["pages"][0]["project_id"] == "p1"
    # hit payloads keep their original text scores; only the order changed
    assert res["hits"][0]["score"] == 0.8 and res["hits"][1]["score"] == 0.9


def test_search_visual_embedder_down_degrades(tmp_path, monkeypatch):
    fake = FakeQdrant(exists={rag_core.PAGE_COLLECTION_NAME},
                      text_results=[_text_point("a.md", "A", 0.9)])
    _setup_search(monkeypatch, fake)

    def boom(q):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(rag_core, "embed_page_query", boom)
    res = rag_core.search_full("storage requirements")
    assert [h["parent_id"] for h in res["hits"]] == ["A"]
    assert res["pages"] == []
    assert any("visual search unavailable" in n for n in res["notes"])


def test_search_without_page_collection_is_silent(tmp_path, monkeypatch):
    fake = FakeQdrant(exists=set(), text_results=[_text_point("a.md", "A", 0.9)])
    _setup_search(monkeypatch, fake)
    monkeypatch.setattr(rag_core, "embed_page_query", lambda q: [0.02] * 4)
    res = rag_core.search_full("storage requirements")
    assert res["pages"] == [] and res["notes"] == []


def test_search_exact_ref_never_displaced_by_fusion(tmp_path, monkeypatch):
    """An exact clause hit stays hits[0] even when the visual ranking favors other sources."""
    exact_payload = {
        "project_id": "p1", "source": "spec.md", "headers": "10.3.6", "text": "clause body",
        "parent_id": "EX", "parent_index": 0, "child_index": 0, "parent_ref_ids": ["10.3.6"],
    }
    fake = FakeQdrant(
        exists={rag_core.PAGE_COLLECTION_NAME},
        text_results=[_text_point("a.md", "A", 0.9), _text_point("b.pdf", "B", 0.8)],
        page_results=[_page_point("b.pdf", 7, 0.95)],
        scroll_results=[SimpleNamespace(id="sx", payload=exact_payload)],
    )
    _setup_search(monkeypatch, fake)
    monkeypatch.setattr(rag_core, "embed_page_query", lambda q: [0.02] * 4)

    res = rag_core.search_full("requirements", ref="10.3.6")
    assert res["hits"][0]["parent_id"] == "EX"
    assert res["hits"][0]["matched_refs"] == ["10.3.6"]
    assert res["hits"][0]["score"] == 1.0
    assert [h["parent_id"] for h in res["hits"][1:]] == ["B", "A"]  # fusion only touches the fill
    assert res["pages"][0]["source"] == "b.pdf"


def test_include_pages_false_skips_visual(tmp_path, monkeypatch):
    fake = FakeQdrant(exists={rag_core.PAGE_COLLECTION_NAME},
                      text_results=[_text_point("a.md", "A", 0.9)],
                      page_results=[_page_point("a.pdf", 1, 0.9)])
    _setup_search(monkeypatch, fake)
    monkeypatch.setattr(rag_core, "embed_page_query", lambda q: [0.02] * 4)
    res = rag_core.search_full("storage requirements", include_pages=False)
    assert res["pages"] == [] and fake.page_queries == []
