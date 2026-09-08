# rag_mcp_server.py
import json
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
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)["embeddings"][0]

@mcp.tool()
def search_shared_knowledge(query: str, project_id: str = None, top_k: int = 5) -> str:
    """搜尋跨專案共享知識庫（包含架構文件、API 規格、通用函式庫說明等）。

    指定 project_id 時僅搜尋該專案，並自動涵蓋 global 共用文件；
    不指定 project_id 則搜尋全部。
    目前有效值：clpe-cdhk、global（即 my_shared_docs/ 下的資料夾名）。
    """
    query_vector = embed_text(query)

    search_filter = None
    if project_id:
        search_filter = Filter(must=[
            FieldCondition(key="project_id", match=MatchAny(any=[project_id, "global"]))
        ])

    results = qdrant.query_points(
        collection_name="shared_knowledge",
        query=query_vector,
        query_filter=search_filter,
        limit=top_k
    ).points

    if not results:
        return "沒有找到相關資料。"

    formatted_results = []
    for res in results:
        formatted_results.append(f"[專案: {res.payload.get('project_id')} | 來源: {res.payload.get('source')}]\n{res.payload.get('text')}")

    return "\n\n---\n\n".join(formatted_results)

if __name__ == "__main__":
    mcp.run(transport="stdio")
