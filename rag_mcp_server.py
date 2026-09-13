# rag_mcp_server.py
import rag_core
from fastmcp import FastMCP

mcp = FastMCP("Shared-RAG-Server")


@mcp.tool()
def search_shared_knowledge(query: str, project_id: str = None, target_parents: int = 3, ref: str = None) -> str:
    """Search the cross-project shared knowledge base (architecture docs, API specs, common library guides, etc.).

    Uses Parent-Child (Small-to-Big) retrieval: matches against small chunks,
    deduplicates by parent_id, and returns complete large sections to keep full context.

    Section-ID lookup: references like "PS 10.3.6", "Clause 10.3.11(d)", "Section 23",
    "Schedule 3", "Sch.1", "TC2-T3", "2400-01" in the query (or the explicit `ref`
    parameter) are matched exactly against the indexed clause/section IDs — the
    referenced clause/section is returned as the first hit (tagged
    "[Exact ref match: <id>]"), followed by semantic results. When exact matches
    exist, a trailing "Referenced by:" block lists the source files that mention
    the matched reference (useful for compliance-matrix work).

    When project_id is given, only that project is searched, automatically including global shared documents;
    when project_id is omitted, only global shared documents are searched.
    Currently valid values: clpe-cdhk, global (i.e. folder names under my_shared_docs/).

    Args:
        query: Search question
        project_id: Restrict the search to a specific project (default: global only)
        target_parents: Number of complete large sections to return (default 3)
        ref: Optional explicit section/clause/schedule/item ID ("10.3.6", "Section 23",
             "Schedule 3", "TC2-T3") overriding auto-detection in the query
    """
    try:
        result = rag_core.search_full(query, project_id=project_id, target_parents=target_parents, ref=ref)
    except RuntimeError as e:
        return f"[Search failed] {e}"

    hits = result["hits"]
    if not hits:
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

    if result["referenced_by"]:
        lines = ["Referenced by:"]
        for rid, sources in result["referenced_by"].items():
            lines.append(f"- {rid}: " + ", ".join(f"{src} ({cnt})" for src, cnt in sources.items()))
        formatted_results.append("\n".join(lines))

    return "\n\n---\n\n".join(formatted_results)


if __name__ == "__main__":
    mcp.run(transport="stdio")
