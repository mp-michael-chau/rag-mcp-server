這是一個非常務實且可控的決定，將「資料清理」留在本地自研腳本，將「檢索與重排序」交給 Dify，能最大化系統的精準度與穩定性。

# 建構 RAG 工作流的完整實作計畫

## 階段一：Data Preprocessing

此階段確保輸入向量庫的資料品質，利用你已開發的工具鏈進行嚴格的格式化。

* **PDF 解析與轉譯：** 將標單或技術文件放入 `pdf_inbox/{project_id}/`，執行 `convert_pdfs.py`。依賴 Docling 進行版面分析，並透過 Ollama 視覺模型提取圖片描述。

* **表格扁平化：** 緊接著執行 `flatten_md_tables.py`，將複雜的跨頁表格或問答對話，重構為帶有明確標題（如 `### {Item ID}`）的線性 Markdown 文本，消除跨 Chunk 語意斷裂的問題。

* **人工審核閘道 (Review Gate)：** 在 `md_review/` 資料夾中進行最後的 Markdown 檢視與修正。確認無誤後，將 `.md` 檔案移至 `my_shared_docs/{project_id}/`，作為核准發布的最終來源。

## 階段二：Dify and Qdrant Infrastructure Setup

設定 Dify 以接收並最佳化你精心準備的 Markdown 檔案。

1. **啟用 Qdrant 引擎：** 編輯 Dify 的 `docker-compose.yaml` 與 `.env`，確認 `VECTOR_STORE=qdrant` 已啟用並正確連線。

2. **建立隔離的知識庫 (Datasets)：** 在 Dify Web UI 中，為每一個專案建立獨立的知識庫（例如 `dataset_project_a`），並建立一個 `dataset_global` 存放跨專案共用規範。

3. **自訂切片策略 (Custom Chunking)：** 這是最關鍵的一步。在 Dify 知識庫的設定中，**切勿使用預設的自動切片**。
* 選擇 **自訂分段 (Custom)**。
* 分段識別符號 (Delimiter) 設為你腳本產出的標題格式：`\n### ` 或 `\n## `。
* 將 Chunk 限制拉高（例如 1500 tokens），確保每一個扁平化後的表格問答或 Parent 段落能完整存入單一 Vector Point。

## 階段三：Automated Ingestion)

取代手動在 Dify 網頁上傳檔案，開發一支橋接腳本來自動同步 `my_shared_docs/` 的內容。

| 腳本名稱 | 核心功能與 API 端點 | 執行邏輯 |
| --- | --- | --- |
| `sync_to_dify.py` | `POST /v1/datasets/{id}/document/create_by_text` | 掃描 `my_shared_docs/`，對應資料夾名稱與 Dify Dataset ID。 |
| (更新機制) | `POST /v1/datasets/{id}/documents/{doc_id}/update_by_text` | 若檔案已存在且有修改，呼叫更新 API 覆蓋舊向量，實現冪等性 (Idempotent)。 |

## 階段四：Kilo Code connect MCP

撰寫一個輕量級的 FastMCP 伺服器，讓 Kilo Code 能直接向 Dify 索要上下文。

* **MCP Server (`dify_mcp_server.py`)：** 定義工具 `search_shared_knowledge(query, project_id)`。
* **雙路徑檢索邏輯：** 當接收到查詢時，MCP 同時向 Dify 的 `dataset_global` 與對應的 `dataset_{project_id}` 發起 `POST /v1/datasets/{id}/retrieve` 請求。
* **合併與回傳：** 將兩邊回傳的檢索片段（Chunks）合併，格式化為包含檔案名稱與內容的純文字，回傳給 Kilo Code。
* **Kilo 設定：** 在 `kilo.json` 中掛載此 Python 腳本，並在專案的 `AGENTS.md` 提示模型在撰寫程式碼前主動呼叫此工具查閱規格。