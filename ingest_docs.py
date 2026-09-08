import os
import json
import uuid
import urllib.request
import pypdfium2 as pdfium
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance, PointStruct
from langchain_text_splitters import RecursiveCharacterTextSplitter, MarkdownHeaderTextSplitter

# 1. 系統設定
QDRANT_HOST = "localhost"
QDRANT_PORT = 6333
COLLECTION_NAME = "shared_knowledge"
DOCS_DIR = "./my_shared_docs" # 放入你想跨專案共享的檔案
CONVERTED_DIR = "./converted_md" # Docling 轉出的 Markdown 快取（來源 PDF 未變更時直接重用）
CONVERT_BATCH_PAGES = 40 # 每批轉換的頁數（限制記憶體用量，中斷後可從斷點續轉）
OLLAMA_HOST = "http://localhost:11434"
EMBED_MODEL = "bge-m3"

# 2. 初始化資料庫連線
qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)

# 3. Docling PDF 轉換器（表格辨識用 ACCURATE 模式，較慢但表格重建品質較佳）
pdf_pipeline = PdfPipelineOptions()
pdf_pipeline.table_structure_options.mode = TableFormerMode.ACCURATE
converter = DocumentConverter(
    format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_pipeline)}
)

def embed_texts(texts):
    """批次呼叫 Ollama embedding API"""
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/embed",
        data=json.dumps({"model": EMBED_MODEL, "input": texts}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)["embeddings"]

# 4. 確保 Collection 存在（維度由 Ollama 模型實際輸出決定）
if not qdrant.collection_exists(COLLECTION_NAME):
    qdrant.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(
            size=len(embed_texts(["dimension probe"])[0]),
            distance=Distance.COSINE
        )
    )

# project_id payload index（冪等：已存在時 qdrant 回 409，直接忽略）
try:
    qdrant.create_payload_index(COLLECTION_NAME, field_name="project_id", field_schema="keyword")
except Exception:
    pass

# 5. 文本切塊設定：先依 Markdown 標題層級（H1-H3）切分並組父標題麵包屑，再對各節做長度切塊
# 依照 Markdown 標題層級切割，並自動將父標題注入 Payload/Text
headers_to_split_on = [
    ("#", "Header 1"),
    ("##", "Header 2"),
    ("###", "Header 3"),
]

header_splitter = MarkdownHeaderTextSplitter(
    headers_to_split_on=headers_to_split_on,
    strip_headers=True,
)

# 各節內仍需控制長度 (避免單个文本超過 LLM 或 Embedding 模型上限)
text_splitter = RecursiveCharacterTextSplitter(
    chunk_size=800,
    chunk_overlap=200,
    separators=["\n\n", "\n", " ", ""]
)

def split_with_headers(content):
    """回傳 (父標題麵包屑, 切塊文字) 清單；無標題的內容麵包屑為空字串"""
    results = []
    for section in header_splitter.split_text(content):
        breadcrumb = " > ".join(
            section.metadata[key] for _, key in headers_to_split_on
            if section.metadata.get(key)
        )
        for chunk in text_splitter.split_text(section.page_content):
            results.append((breadcrumb, chunk))
    return results

def get_pdf_page_count(filepath):
    pdf = pdfium.PdfDocument(filepath)
    try:
        return len(pdf)
    finally:
        pdf.close()

def convert_pdf_to_markdown(filepath, project_id):
    """以 Docling 分批將 PDF 轉為 Markdown、逐批寫入快取；中斷後重跑會自動從斷點續轉"""
    os.makedirs(CONVERTED_DIR, exist_ok=True)
    # 快取檔名加 project_id 前綴，防跨專案同名 PDF 快取互蓋
    cache_path = os.path.join(CONVERTED_DIR, f"{project_id}__{os.path.basename(filepath)}.md")
    progress_path = cache_path + ".progress"

    # 完整快取且來源 PDF 未變更 → 直接使用
    if (os.path.exists(cache_path) and not os.path.exists(progress_path)
            and os.path.getmtime(cache_path) > os.path.getmtime(filepath)):
        print(f"  使用現有轉換結果：{cache_path}")
        with open(cache_path, "r", encoding="utf-8") as f:
            return f.read()

    # 來源 PDF 已變更 → 清除舊進度重轉
    if os.path.exists(cache_path) and os.path.getmtime(cache_path) < os.path.getmtime(filepath):
        os.remove(cache_path)
        if os.path.exists(progress_path):
            os.remove(progress_path)

    total_pages = get_pdf_page_count(filepath)
    done_pages, done_bytes = 0, 0
    if os.path.exists(progress_path):
        with open(progress_path, "r", encoding="utf-8") as pf:
            done_pages, done_bytes = (int(x) for x in pf.read().strip().split(","))
    print(f"  Docling 轉換中（已完成 {done_pages}/{total_pages} 頁，每批 {CONVERT_BATCH_PAGES} 頁）")

    with open(cache_path, "ab" if done_pages else "wb") as out:
        if done_pages:
            out.truncate(done_bytes)
        start = done_pages + 1
        while start <= total_pages:
            end = min(start + CONVERT_BATCH_PAGES - 1, total_pages)
            result = converter.convert(filepath, page_range=(start, end))
            out.write(result.document.export_to_markdown().encode("utf-8"))
            out.write(b"\n\n")
            out.flush()
            with open(progress_path, "w", encoding="utf-8") as pf:
                pf.write(f"{end},{out.tell()}")
            print(f"  已轉換 {end}/{total_pages} 頁")
            start = end + 1

    os.remove(progress_path)
    with open(cache_path, "r", encoding="utf-8") as f:
        return f.read()

def ingest_directory():
    points = []

    # 雙層掃描：根目錄檔案 = global；一級子資料夾名即 project_id（跳過 . 開頭資料夾，不遞迴更深層）
    doc_sources = []
    for entry in sorted(os.listdir(DOCS_DIR)):
        entry_path = os.path.join(DOCS_DIR, entry)
        if os.path.isdir(entry_path):
            if entry.startswith("."):
                continue
            for filename in sorted(os.listdir(entry_path)):
                filepath = os.path.join(entry_path, filename)
                if os.path.isfile(filepath):
                    doc_sources.append((entry, filename, filepath))
        elif os.path.isfile(entry_path):
            doc_sources.append(("global", entry, entry_path))

    for project_id, filename, filepath in doc_sources:
        if filename.endswith('.pdf'):
            # 先以 Docling 轉成 Markdown，再進行後續切塊
            try:
                content = convert_pdf_to_markdown(filepath, project_id)
            except Exception as e:
                print(f"警告：{project_id}/{filename} Docling 轉換失敗（{e}），已跳過")
                continue
        elif filename.endswith(('.txt', '.md')):
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
        else:
            continue

        if not content.strip():
            print(f"警告：{project_id}/{filename} 沒有提取到任何文字，可能是掃描式 PDF（需要 OCR）")
            continue

        print(f"處理中：{project_id}/{filename}")

        # 進行文本切塊（父標題注入 text 與 payload）
        chunks = split_with_headers(content)

        # 批次產生向量（Ollama /api/embed 支援多筆輸入；embedding 文字含父標題麵包屑）
        EMBED_BATCH = 32
        for start in range(0, len(chunks), EMBED_BATCH):
            batch_chunks = chunks[start:start + EMBED_BATCH]
            vectors = embed_texts(
                [f"{breadcrumb}\n\n{chunk}" if breadcrumb else chunk
                 for breadcrumb, chunk in batch_chunks]
            )

            for j, ((breadcrumb, chunk), vector) in enumerate(zip(batch_chunks, vectors)):
                i = start + j
                # 建立 Qdrant 資料點（ID 由 專案+檔名+切塊編號 決定，重複執行時覆蓋而非重複插入）
                point = PointStruct(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{project_id}:{filename}:{i}")),
                    vector=vector,
                    payload={
                        "project_id": project_id,
                        "source": filename,
                        "headers": breadcrumb,
                        "chunk_index": i,
                        "text": f"{breadcrumb}\n\n{chunk}" if breadcrumb else chunk
                    }
                )
                points.append(point)

    if points:
        print(f"Upserting {len(points)} chunks into Qdrant...")
        # 分批上傳，避免單次請求超過 Qdrant 的 payload 上限 (32MB)
        BATCH_SIZE = 100
        for start in range(0, len(points), BATCH_SIZE):
            batch = points[start:start + BATCH_SIZE]
            qdrant.upsert(
                collection_name=COLLECTION_NAME,
                points=batch
            )
            print(f"  已上傳 {min(start + BATCH_SIZE, len(points))}/{len(points)} chunks")
        print("Done!")
    else:
        print("沒有找到可攝取的文件。")

if __name__ == "__main__":
    os.makedirs(DOCS_DIR, exist_ok=True)
    print(f"請將共享文件放入 {DOCS_DIR}（一級子資料夾名即 project_id，根目錄檔案 = global）後，再次執行此腳本。")
    ingest_directory()
