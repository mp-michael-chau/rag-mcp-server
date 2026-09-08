# rag_mcp_server.py
import json
import urllib.error
import urllib.request
from fastmcp import FastMCP
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchAny

OLLAMA_HOST = "http://localhost:11434"
EMBED_MODEL = "bge-m3"

mcp = FastMCP("Shared-RAG-Server")
qdrant = QdrantClient(host="localhost", port=6333)

def embed_text(text):
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/embed",
        data=json.dumps({"model": EMBED_MODEL, "input": [text]}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)["embeddings"][0]
    except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError) as e:
        raise RuntimeError(
            f"Cannot connect to Ollama ({OLLAMA_HOST}, model {EMBED_MODEL}): {e}. "
            "Please make sure Ollama is running (e.g. run `ollama serve`) and the model is downloaded, then retry."
        ) from e

@mcp.tool()
def search_shared_knowledge(query: str, project_id: str = None, target_parents: int = 3) -> str:
    """Search the cross-project shared knowledge base (architecture docs, API specs, common library guides, etc.).

    Uses Parent-Child (Small-to-Big) retrieval: matches against small chunks,
    deduplicates by parent_id, and returns complete large sections to keep full context.

    When project_id is given, only that project is searched, automatically including global shared documents;
    when project_id is omitted, everything is searched.
    Currently valid values: clpe-cdhk, global (i.e. folder names under my_shared_docs/).

    Args:
        query: Search question
        project_id: Restrict the search to a specific project
        target_parents: Number of complete large sections to return (default 3)
    """
    try:
        query_vector = embed_text(query)
    except RuntimeError as e:
        return f"[Search failed] {e}"

    search_filter = None
    if project_id:
        search_filter = Filter(must=[
            FieldCondition(key="project_id", match=MatchAny(any=[project_id, "global"]))
        ])

    # Over-fetch: top hits may be concentrated among multiple children of the same parent chunk,
    # so fetch 5x the target count and deduplicate by parent_id to avoid "top-K domination" diluting results
    fetch_limit = target_parents * 5

    results = qdrant.query_points(
        collection_name="shared_knowledge",
        query=query_vector,
        query_filter=search_filter,
        limit=fetch_limit
    ).points

    if not results:
        return "No relevant information found."

    # Deduplicate by parent_id: only keep the highest-scoring hit per parent chunk,
    # returning the full parent content from payload["text"]
    seen_parents = set()
    formatted_results = []
    for res in results:
        p_id = res.payload.get("parent_id") or res.id
        if p_id in seen_parents:
            continue
        seen_parents.add(p_id)

        parts = [f"Project: {res.payload.get('project_id')}", f"Source: {res.payload.get('source')}"]
        if res.payload.get("headers"):
            parts.append(f"Section: {res.payload.get('headers')}")
        formatted_results.append(f"[{' | '.join(parts)}]\n{res.payload.get('text')}")

        if len(formatted_results) >= target_parents:
            break

    return "\n\n---\n\n".join(formatted_results)

if __name__ == "__main__":
    mcp.run(transport="stdio")
