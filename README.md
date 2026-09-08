## RAG MCP Server

### Start Server
    docker compose up -d

### Web Access
    http://localhost:6333/dashboard

---

## Ingest Documents

### 新增共享文件

1. 將檔案放入 `my_shared_docs/{project-id/}`（支援 `.pdf`、`.txt`、`.md`）
2. 執行攝取腳本（會自動切塊、產生向量並上傳至 Qdrant）：

```bash
python ingest_docs.py
```

### 注意事項

- **PDF 前處理**：PDF 會先經 [Docling](https://github.com/docling-project/docling) 轉成 Markdown（含表格結構重建、閱讀順序整理、內建 OCR）才進行切塊攝取
- **轉換快取**：轉換結果存於 `converted_md/`，來源 PDF 未變更時重跑直接使用快取，不會重新轉換；轉換以 40 頁為一批增量寫入，中斷後重跑會自動從斷點續轉
- **首次執行**：Docling 需下載模型（約數百 MB），大型 PDF（數百頁）轉換可能需要數十分鐘，之後都有快取
- **重複執行安全**：chunk ID 由 `檔名 + 切塊編號` 決定，重跑同一檔案會覆蓋舊資料，不會重複堆疊
- **變更檔案內容**：若改檔內容後重跑，舊檔較多餘的 chunks 可能殘留；乾淨做法是先刪除 collection 再重跑：
```bash
curl -X DELETE http://localhost:6333/collections/shared_knowledge
python ingest_docs.py
```

---

## RAG MCP
MCP server 設定目前只在 rag/kilo.json。其他專案要呼叫此工具，需在其 kilo.json（或全域 ~/.config/kilo/kilo.json）加入同一段設定：
```jsonc
{
  "mcp": {
    "shared-rag": {
      "type": "local",
      "command": ["/Users/bellergy/Documents/Projects/local-mcp/rag/.venv/bin/python",
                  "/Users/bellergy/Documents/Projects/local-mcp/rag/rag_mcp_server.py"],
      "enabled": true
    }
  }
}
```

### 讓每個專案「自動」帶自己的 project_id
在各專案的 AGENTS.md 加一條規則，例如在 clpe-cdhk 專案：
`呼叫 search_shared_knowledge 時一律帶 project_id="clpe-cdhk"。`