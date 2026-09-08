# 多專案 RAG：單一 Collection + project_id Metadata Filtering

## 結論

提案（單一 collection + payload 過濾）方向可行，為 Qdrant 官方建議的多租戶模式。但提案程式碼需修正 4 點後才能落地：

1. `uuid.uuid4()` 破壞重複執行冪等性 → 沿用 uuid5，且 key 加入 project_id 防跨專案同名檔案碰撞
2. `qdrant.search()` 已棄用（本 repo 已升級到 `query_points()`，提案是倒退）→ 改用 `query_points(query_filter=...)`
3. 缺 `project_id` payload index → 建立 keyword index
4. `global` 語意未定義 → 已決策：指定專案搜尋時自動包含 global

## 已確認的設計決策

| 決策 | 結果 |
|---|---|
| 指定 project_id 搜尋 | 自動包含 global（`MatchAny([project_id, "global"])`） |
| 現有 02-Particular-Specification.md 歸屬 | `project_id="clpe-cdhk"` |
| Ingest 輸入介面 | 子資料夾即專案：`my_shared_docs/<project_id>/`；根目錄檔案 = `global` |
| 殘留 PDF（536 頁重轉陷阱） | 移出 `my_shared_docs/` 至 `./archive/` |
| Collection 命名 | 沿用 `shared_knowledge` |

## 實作任務

### 1. 檔案整理（shell）

```bash
mkdir -p my_shared_docs/clpe-cdhk archive
mv my_shared_docs/02-Particular-Specification.md my_shared_docs/clpe-cdhk/
mv "my_shared_docs/02.Particular Specification.pdf" archive/
```

### 2. 修改 `ingest_docs.py`

- **掃描邏輯改雙層**：根目錄的 `.pdf/.txt/.md` → `project_id="global"`；每個一級子資料夾名即 `project_id`（跳過 `.` 開頭資料夾，不遞迴更深層）。
- **payload 加入** `"project_id": project_id`。
- **Point ID 公式改為** `uuid.uuid5(uuid.NAMESPACE_URL, f"{project_id}:{filename}:{i}")`。
- **Collection 建立後加 payload index**（冪等，以 try/except 包住 409）：
  ```python
  qdrant.create_payload_index(COLLECTION_NAME, field_name="project_id", field_schema="keyword")
  ```
- **converted_md 快取檔名改為** `{project_id}__{basename}.md`，防跨專案同名 PDF 快取互蓋（既有快取不受影響，PDF 已移出）。
- 其餘（Docling 轉換、續轉進度、Ollama 批次 embedding、每批 100 upsert）保持不變。

### 3. 修改 `rag_mcp_server.py`

- 工具簽名：`search_shared_knowledge(query: str, project_id: str = None, top_k: int = 5)`。
- Docstring 說明：指定 `project_id` 時自動涵蓋 global 文件；列出目前有效值（`clpe-cdhk`、`global`，即 `my_shared_docs/` 下資料夾名），避免 LLM 猜測不存在的 ID。
- 過濾邏輯：
  ```python
  from qdrant_client.models import Filter, FieldCondition, MatchAny

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
  ```
- 輸出格式改為 `[專案: {project_id} | 來源: {source}]\n{text}`。
- 無結果時回傳「沒有找到相關資料。」。

### 4. 資料遷移（ID 公式改變 → 清空重建）

舊 674 points 的 ID 無 project 前綴且缺 `project_id` payload，新公式不會覆蓋它們，會成孤兒。處理方式：

```bash
# 在 rag/.venv 中執行；先確認 Ollama 已在跑（bge-m3）
python -c "from qdrant_client import QdrantClient; QdrantClient(host='localhost', port=6333).delete_collection('shared_knowledge')"
python ingest_docs.py   # 自動重建 collection + payload index + 新結構攝取
```

### 5. 驗證

- `qdrant.count("shared_knowledge")` → 應為 674（僅 clpe-cdhk，尚無 global 文件）。
- MCP 搜尋測試（重啟 Kilo session 讓 stdio MCP server 重新 spawn，kilo.json 無需改動）：
  - `project_id="clpe-cdhk"` → 命中且格式顯示 `[專案: clpe-cdhk | ...]`
  - 不帶 `project_id` → 搜全庫
  - `project_id="nonexistent"` → 回「沒有找到相關資料。」
  - 之後在根目錄加入任一 global 文件、重跑 ingest，驗證 `clpe-cdhk` 搜尋會自動混入 global 結果（MatchAny 行為）。

## 風險與邊界

- 刪除 collection 重建期間短暫無法搜尋（本地單人使用，可接受）。
- 子資料夾僅掃一層、不遞迴；`global` 也可作為實體資料夾名，語意一致。
- 過濾後命中數不足 `top_k` 屬正常行為。
- embedding 來源不變（Ollama bge-m3、1024 維），向量相容，無需其他遷移。
