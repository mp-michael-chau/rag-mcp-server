# Excel/CSV → Markdown 轉換器（convert_sheets.py）實作計畫

## Goal

新增一個與 `convert_pdfs.py` 對位的 CLI 轉換器：把 `pdf_inbox/` 裡的 Excel/CSV 轉成**結構適合 RAG embed 與查詢**的 Markdown，輸出到 `md_review/` 走既有的 review → approve → ingest 流程。

## 已確認決策

1. **範圍：僅 CLI**。不動 `api/` 與 `web/`（Web UI 整合明確延後，但程式介面預留相容性）。
2. **收件夾：沿用 `pdf_inbox/`**（兩層結構不變：root = global、一級子資料夾 = project_id）；以副檔名區分由哪個腳本處理。
3. **輸出結構：每列一個 `###` 區塊**，與 `flatten_md_tables.py` 的輸出慣例一致（每列自成一個檢索單元，切塊行為與扁平化後的 PDF 表格相同）：

   ```markdown
   # price-list.xlsx — Sheet: Summary

   2026 年度設備價格表（表頭列之上的標題/備註列，保留為純文字）

   ### TC1-C2

   [Description]: Temporary power supply

   [Qty]: 2

   ### TC1-C3

   ...
   ```

## 採用的預設值（未另行詢問，可在 review 時調整）

- **支援副檔名**：`.xlsx`、`.xlsm`、`.csv`。`.xls`（舊二進位格式）openpyxl 不支援 → 回清楚錯誤訊息請先另存 `.xlsx`；`.ods` 不支援。
- **相依套件**：`openpyxl`（venv 已有 3.1.5，為 docling 的傳依賴；加入 `requirements.txt` 明確宣告為直接依賴）。不用 pandas。
- **CSV 編碼**：依序嘗試 `utf-8-sig` → `utf-16` → `big5`；分隔符號用 `csv.Sniffer`（限制 `,`、`\t`、`;`），失敗 fallback `,`。
- **Merged cells**：展開填值（每列帶完整 context，適合 RAG）。
- **公式**：`data_only=True` 讀快取值；从未由 Excel 計算儲存的檔案公式值為空（README 註明 caveat）。
- **隱藏/空 sheet**：隱藏 sheet 照轉，H1 標題加註 ` (hidden)`；全空 sheet 跳過（console 註記）。
- **無表頭 sheet**：不加任何自訂標籤（沿用專案既有決策 `headerless_table_no_labels`）——區塊標題取首個非空 cell（≤60 字元，超長截斷加 `…` 並在內文保留全文），其餘 cell 輸出為無前綴純文字行。

## 檔案變更

### 1. 新增 `sheet_to_md.py` — 純轉換模組（不含路徑/流程耦合）

定位比照 `flatten_md_tables.py`：獨立模組、可單檔使用、被 `rag_core` 引用。

- `convert_file(path) -> str`：讀取檔案 → 完整 Markdown 字串。
  - **xlsx/xlsm**：`openpyxl.load_workbook(path, data_only=True)`（非 read_only 模式，否則 `merged_cells.ranges` 不可用；文件型檔案記憶體可接受）。先掃 `merged_cells.ranges` 建展開映射，再逐列取值展開。
  - **csv**：編碼鏈 + Sniffer 如上；視為單一 sheet。
- 每個 sheet 輸出：
  - `# {filename} — Sheet: {sheet_name}`（CSV 為 `# {filename}`；隱藏加 ` (hidden)`）
  - **表頭判定**：在前 10 個非空列中，找第一列「≥2 個非空 cell 且全部 ≤40 字元短標籤」者為表頭（門檻同 flatten 的 `LABEL_MAX`）；其上的非空列保留為純文字 preamble（sheet 說明，進 H1 區塊內容）；找不到 → headerless。
  - **每資料列一個 `###` 區塊**：標題 = 首欄值（空則取首個非空 cell；>60 字元截斷，全文以 `[首欄標籤]: 全文`／純文字行保留於內文，鏡射 `flatten_md_tables.render_row`）；其餘非空 cell 輸出 `[欄位標籤]: 值`（headerless 則無前綴）；欄數不足補空、超出表頭的 cell 以無前綴行輸出。
- **值 → 文字**：`None` → 空（跳過）、`datetime/date/time` → ISO 格式、`bool` → `TRUE`/`FALSE`、整數值的 float 去小數（`2.0` → `2`）、其餘 `str()`；cell 內換行換成空格、trim。
- 全空白行跳過；列尾空欄 trim。
- 全部 sheet 皆空 → `raise ValueError("no data found")`（呼叫端轉為 per-file error）。
- 附小 CLI：`python sheet_to_md.py <file...>` 將 Markdown 印到 stdout（方便測試單檔）。

### 2. `rag_core.py` — 流程編排（沿用既有規則）

- 常數 `SHEET_EXTS = (".xlsx", ".xlsm", ".csv")`。
- `convert_project_sheet(project_id, filename, progress_cb=None) -> {"status": converted|skipped|error, "message": str}`：
  - `finalized_md_path()` 已存在 → skip（stem 對應，xlsx/csv 自動適用；`my_shared_docs/` 的核准受保護）。
  - review md 存在且 mtime ≥ 來源檔 → skip（保留人工編輯）。**無 `.progress` sidecar**——轉換快且原子（暫存檔 + `os.replace`）。
  - 來源較新 → 舊 review md 改名 `.prev` 後重轉。
  - 例外（損毀、密碼保護、`.xls`、無資料）→ 回 `status=error`，不中斷其他檔案。
  - **不跑 flatten**（區塊結構直接生成，無 pipe 表格）。
  - 保留 `progress_cb` 參數簽名與 PDF 版對位（之後 Web UI 整合零改動）。
- `convert_sheets_inbox(progress_cb=None)`：兩層掃描 `PDF_INBOX_DIR` 過濾 `SHEET_EXTS`，逐一轉換，結尾印 review 流程提示（同 `convert_inbox` 收尾文案）。
- 不改 `convert_inbox()`（維持只掃 PDF；兩個 CLI 各自明確）。

### 3. 新增 `convert_sheets.py` — thin CLI wrapper

比照 `convert_pdfs.py`：docstring 說明、argparse 無額外參數、呼叫 `rag_core.convert_sheets_inbox()`。

### 4. `requirements.txt`

加一行 `openpyxl`。

### 5. `README.md`

- Components 表與 Directory Overview 加 `convert_sheets.py`／`sheet_to_md.py`。
- Data Flow 與 Ingest Documents 段落註明 `pdf_inbox/` 亦收 `.xlsx/.xlsm/.csv`，執行 `python convert_sheets.py`。
- Notes：表頭判定規則、merged cell 展開、公式快取值 caveat、`.xls` 不支援、headerless 不加標籤、已知限制（見下）。

## 邊界情況（實作須處理）

- 同一 project 內同 stem 的 `.pdf` 與 `.xlsx`（如 `spec.pdf` + `spec.xlsx`）→ 兩者對應同一個 `md_review/{pid}/spec.md`，mtime 規則下後轉者觸發 `.prev` 重轉。**既有 stem 對應機制的天生限制，README 註明即可**。
- 密碼保護／損毀檔 → per-file error，續跑其他檔。
- 公式無快取值 → 空 cell（README caveat，review 時可補）。
- 多表格上下同 sheet：只用第一個偵測到的表頭；第二個表的表頭列會成為資料列。限制註明，review 修正。
- 標題 >60 字元 → 截斷標題 + 內文保留全文；首 cell 空 → 取首個非空 cell 當標題（均鏡射 flatten 慣例）。

## 驗證計畫

1. 建測試檔於 `pdf_inbox/_sheets_test/`（或臨時 project）：
   - xlsx：多 sheet、preamble+表頭、merged cells、headerless sheet、空 sheet、隱藏 sheet、datetime／數字／bool 混合
   - csv：`utf-8-sig` 與 `big5` 各一
2. `python convert_sheets.py` → 檢查 `md_review/{pid}/*.md`：H1 標題、preamble、每列區塊、`[欄位]:` 標籤、截斷標題、merged 展開、headerless 無標籤。
3. 跳過規則：重跑 → 全 skipped；`touch` 來源 → 產生 `.prev` 重轉；把 md 搬進 `my_shared_docs/` → skip。
4. 單檔測試：`python sheet_to_md.py fixture.xlsx` 檢查 stdout。
5. （服務在跑時，選做）ingest 測試檔後用 `rag_core.search()` 查一筆列內容，確認回傳該列區塊。
6. 收尾：刪除測試專案資料夾與產出的 points。

## 明確 Out of Scope

- Web UI / API 整合（延後；函式簽名已預留）
- `.xls`／`.ods` 支援
- 數字格式重建（貨幣/百分比顯示原始數值；review 修正）
- 單 sheet 多表格的自動偵測
