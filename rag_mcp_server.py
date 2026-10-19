# rag_mcp_server.py
import rag_core
from fastmcp import FastMCP

mcp = FastMCP("Shared-RAG-Server")


@mcp.tool()
def search_shared_knowledge(query: str, project_id: str = None, target_parents: int = 3, ref: str = None, include_pages: bool = False) -> str:
    """Search the cross-project shared knowledge base (architecture docs, API specs, common library guides, etc.).

    Uses Parent-Child (Small-to-Big) retrieval: matches against small chunks,
    deduplicates by parent_id, and returns complete large sections to keep full context.

    Section-ID lookup: references like "PS 1.2.3", "Clause 1.2.3(d)", "Section 12",
    "Schedule 2", "Sch.4", "AB1-C2", "1234-56" in the query (or the explicit `ref`
    parameter) are matched exactly against the indexed clause/section IDs — the
    referenced clause/section is returned as the first hit (tagged
    "[Exact ref match: <id>]"), followed by semantic results. When exact matches
    exist, a trailing "Referenced by:" block lists the source files that mention
    the matched reference (useful for compliance-matrix work).

    Page-image matches: with include_pages=true, the query is additionally matched
    against indexed PDF page images (EmbeddingGemma 2 visual retrieval); matching
    pages are appended as a "Page matches:" block (source, page number, similarity).
    Page indexing only covers PDFs whose page indexing job has run — text results
    are returned unchanged either way.

    When project_id is given, only that project is searched, automatically including global shared documents;
    when project_id is omitted, only global shared documents are searched.
    Valid values are "global" plus the first-level folder names under my_shared_docs/ (one folder per project).

    Args:
        query: Search question
        project_id: Restrict the search to a specific project (default: global only)
        target_parents: Number of complete large sections to return (default 3)
        ref: Optional explicit section/clause/schedule/item ID ("1.2.3", "Section 12",
             "Schedule 2", "AB1-C2") overriding auto-detection in the query
        include_pages: Also return page-image matches (default false)
    """
    try:
        result = rag_core.search_full(
            query, project_id=project_id, target_parents=target_parents, ref=ref,
            include_pages=include_pages,
        )
    except RuntimeError as e:
        return f"[Search failed] {e}"

    hits = result["hits"]
    if not hits and not result.get("pages"):
        return "No relevant information found."

    formatted_results = []
    if result["notes"]:
        formatted_results.append("\n".join(result["notes"]))
    for hit in hits:
        parts = [f"Project: {hit.get('project_id')}", f"Source: {hit.get('source')}"]
        if hit.get("matched_refs"):
            parts.append(f"Exact ref match: {', '.join(hit['matched_refs'])}")
        if hit.get("headers"):
            parts.append(f"Section: {hit.get('headers')}")
        formatted_results.append(f"[{' | '.join(parts)}]\n{hit.get('text')}")

    if result.get("pages"):
        lines = ["Page matches (page-image similarity):"]
        for page in result["pages"]:
            lines.append(
                f"- {page.get('source')} p.{page.get('page_number')} "
                f"(similarity {page.get('score'):.3f})"
                if isinstance(page.get("score"), (int, float)) else
                f"- {page.get('source')} p.{page.get('page_number')}"
            )
        formatted_results.append("\n".join(lines))

    if result["referenced_by"]:
        lines = ["Referenced by:"]
        for rid, sources in result["referenced_by"].items():
            lines.append(f"- {rid}: " + ", ".join(f"{src} ({cnt})" for src, cnt in sources.items()))
        formatted_results.append("\n".join(lines))

    return "\n\n---\n\n".join(formatted_results)


if __name__ == "__main__":
    mcp.run(transport="stdio")
