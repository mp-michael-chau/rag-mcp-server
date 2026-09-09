# rag_mcp_server.py
import rag_core
from fastmcp import FastMCP

mcp = FastMCP("Shared-RAG-Server")


@mcp.tool()
def search_shared_knowledge(query: str, project_id: str = None, target_parents: int = 3) -> str:
    """Search the cross-project shared knowledge base (architecture docs, API specs, common library guides, etc.).

    Uses Parent-Child (Small-to-Big) retrieval: matches against small chunks,
    deduplicates by parent_id, and returns complete large sections to keep full context.

    When project_id is given, only that project is searched, automatically including global shared documents;
    when project_id is omitted, only global shared documents are searched.
    Currently valid values: clpe-cdhk, global (i.e. folder names under my_shared_docs/).

    Args:
        query: Search question
        project_id: Restrict the search to a specific project (default: global only)
        target_parents: Number of complete large sections to return (default 3)
    """
    try:
        hits = rag_core.search(query, project_id=project_id, target_parents=target_parents)
    except RuntimeError as e:
        return f"[Search failed] {e}"

    if not hits:
        return "No relevant information found."

    formatted_results = []
    for hit in hits:
        parts = [f"Project: {hit.get('project_id')}", f"Source: {hit.get('source')}"]
        if hit.get("headers"):
            parts.append(f"Section: {hit.get('headers')}")
        formatted_results.append(f"[{' | '.join(parts)}]\n{hit.get('text')}")

    return "\n\n---\n\n".join(formatted_results)


if __name__ == "__main__":
    mcp.run(transport="stdio")
