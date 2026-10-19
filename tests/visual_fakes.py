"""Shared fakes for the visual-retrieval tests: a Qdrant client stub and a minimal
2-page PDF builder (valid xref, empty pages — pypdfium2 renders them as blanks)."""

from types import SimpleNamespace

import rag_core


class FakeQdrant:
    """Records calls; serves canned query/scroll results per collection."""

    def __init__(self, exists=(), text_results=None, page_results=None, scroll_results=None):
        self.exists = set(exists)
        self.text_results = text_results or []
        self.page_results = page_results or []
        self.scroll_results = scroll_results or []
        self.calls = []  # ordered (op, ...) tuples
        self.upserts = []  # [(collection_name, [points])]
        self.deletes = []  # [(collection_name, selector)]
        self.page_queries = []

    def _record(self, *entry):
        self.calls.append(entry)

    def collection_exists(self, name):
        return name in self.exists

    def create_collection(self, collection_name, vectors_config):
        self._record("create_collection", collection_name, vectors_config)

    def create_payload_index(self, collection_name, field_name, field_schema):
        self._record("create_index", collection_name, field_name)

    def query_points(self, collection_name, query, query_filter=None, limit=None):
        if collection_name == rag_core.PAGE_COLLECTION_NAME:
            self.page_queries.append(limit)
            src = self.page_results
        else:
            self._record("query_text", collection_name, limit)
            src = self.text_results
        points = [
            SimpleNamespace(id=-(i + 1), score=item["score"], payload=dict(item["payload"]))
            for i, item in enumerate(src[: (limit or len(src))])
        ]
        return SimpleNamespace(points=points)

    def scroll(self, collection_name, scroll_filter=None, limit=10, offset=None, with_payload=True):
        self._record("scroll", collection_name)
        return self.scroll_results, None

    def count(self, collection_name, exact=True, count_filter=None):
        return SimpleNamespace(count=1)

    def upsert(self, collection_name, points):
        self._record("upsert", collection_name)
        self.upserts.append((collection_name, list(points)))

    def delete(self, collection_name, points_selector=None):
        if collection_name == rag_core.PAGE_COLLECTION_NAME:
            self._record("delete_pages", collection_name,
                         self._match_value(points_selector, "project_id"),
                         self._match_value(points_selector, "source"))
        else:
            self._record("delete_text", collection_name)
        self.deletes.append((collection_name, points_selector))

    @staticmethod
    def _match_value(points_selector, key):
        if points_selector is None:
            return None
        must = getattr(points_selector, "must", None) or []
        for cond in must:
            if getattr(cond, "key", None) == key:
                return cond.match.value
        return None


def make_two_page_pdf():
    """Build a minimal valid 2-page PDF (proper xref) as bytes; pages have no content."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)
