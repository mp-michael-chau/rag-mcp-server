"""表格扁平化：把 Docling 輸出的 Markdown pipe 表格重寫為「每列一個 ### 區塊」的 Q&A 形式，改善 RAG 切塊品質。

用法：
    python flatten_md_tables.py <path...>

path 可為檔案或目錄（目錄 → 遞迴處理 *.md，略過 *.progress、*.prev 與隱藏目錄）。
就地原子寫回（同目錄暫存檔 + os.replace）；內容無變更則不寫入。
逐檔印出「扁平化 N 表格、合併 M 續行、去重 K 標題」。

解析與轉換規則：
- 表格區塊：連續以 | 開頭的行；cell 以未轉義的 | 切分後 trim、\\| 解逸出
- 分隔列偵測：所有 cell 匹配 ^:?-{3,}:?$
- 表頭判定：分隔列上一行且所有非空 cell 皆為短標籤（≤ 40 字元）→ 表頭；
  若上方判定不出表頭，改看分隔列下一行（Docling 把合併儲存格標題放在分隔線上、
  真正欄名放在分隔線下的 artifact，如週報表的 Mon/Tue/... 列）
- 表頭沿用：後續碎片的表頭與文件中上一個表頭完全相同（跨頁重複）→ 丟棄；
  欄數不同但標籤逐詞相同（欄位融合，如 Item|Question|Reply 融成 Item Question|Reply）
  → 亦丟棄並沿用原表頭；無表頭但欄數相同 → 沿用上一個表頭
- 無表頭 fallback：不發明任何標籤——區塊標題用首欄前 ~60 字元（截斷加 "…"），
  完整首欄文字以純文字行保留在內文（避免截斷遺失內容），
  其餘各非空 cell 依欄序輸出為無前綴的純文字行
- 合併儲存格 artifact：整列所有 cell 完全相同（Docling 把合併標題重複填入每欄）→ 收縮為單一 cell
- 續行合併：首 cell 為空且其他 cell 有內容的資料列 → 各 cell 以一個空格接上前一列同欄
  （跨表格碎片亦生效，處理跨頁中斷的句子；TC2-T6 逐欄續 Question 與 Reply）
- (Cont'd) 合併：首 cell 形如「TC1-C2 (Cont'd)」→ 去掉後綴、逐欄併入同 base ID 的前一列
- 項目碼切分：資料列欄數少於表頭欄數、且首 cell 開頭是項目碼
  （字母數字-字母數字、至少含一位數字）→ 拆成 ID＋其餘文字再對位（Docling 欄位融合的補救）
- 輸出格式（每列一區塊，區塊間空行）：
  ### {項目ID 或 首欄前 ~60 字元}

  [Question]: ...

  [Reply]: ...
- 頁首去重：文件含表格時，刪除稍早已出現過的完全相同標題行（#～###），保留第一個
- HTML entity 解逸出：全檔 html.unescape（本批文件無 code block，安全）
- 非表格內容原樣保留（<!-- image -->、-End- 等）

已知限制：Docling 層級的欄位融合／字序錯亂（如 EMS TC1 第 3 碎片回覆開頭
"terms after the" 被移到最前）無法自動還原，md_review 人工審查是最後防線。
"""

import argparse
import html
import os
import re
import sys

SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")
HEADING_RE = re.compile(r"^#{1,3}\s+\S.*$")
ITEM_CODE_RE = re.compile(r"^([A-Za-z]+\d*-[A-Za-z]+\d*)\s+(\S.*)$")
CONTD_RE = re.compile(r"^(.*?)\s*\(Cont['’]d\)\s*$", re.IGNORECASE)
TITLE_MAX = 60  # 區塊標題最長字元數，超過則截斷加 "…"
LABEL_MAX = 40  # 表頭 cell「短標籤」判定上限


def split_row(line):
    """表格行 → cell 清單：以未轉義的 | 切分、trim、\\| 解逸出（行首尾的 | 捨棄）"""
    parts = re.split(r"(?<!\\)\|", line.strip())
    if parts and not parts[0].strip():
        parts.pop(0)
    if parts and not parts[-1].strip():
        parts.pop()
    return [p.strip().replace("\\|", "|") for p in parts]


def is_separator_row(cells):
    return bool(cells) and all(SEPARATOR_CELL_RE.match(c) for c in cells)


def is_short_label_row(cells):
    """所有非空 cell 皆 ≤ LABEL_MAX 字元且至少兩個非空 → 表頭候選"""
    non_empty = [c for c in cells if c]
    return len(non_empty) >= 2 and all(len(c) <= LABEL_MAX for c in non_empty)


def header_words(labels):
    """表頭標籤逐詞展開（比對欄位融合變體：['Item','Question','Reply'] vs ['Item Question','Reply']）"""
    return tuple(" ".join(labels).split())


def collapse_merged_row(cells):
    """Docling 合併儲存格 artifact：整列所有 cell 完全相同 → 收縮為單一 cell（保留在首欄）"""
    if len(cells) > 1 and cells[0] and len(set(cells)) == 1:
        return [cells[0]]
    return cells


def parse_table_block(block_lines, last_header):
    """解析單一表格區塊 → (labels, data_rows, new_last_header)；labels=None 表示無表頭表格"""
    rows = [split_row(line) for line in block_lines]
    sep_idx = next((i for i, r in enumerate(rows) if is_separator_row(r)), None)
    above, below = [], rows
    if sep_idx is not None:
        above, below = rows[:sep_idx], rows[sep_idx + 1:]

    header = None
    if above and is_short_label_row(above[-1]):
        header = above[-1]
        above = above[:-1]
    elif below and is_short_label_row(below[0]):
        header = below[0]
        below = below[1:]
    data = above + below

    labels = None
    if header is not None:
        if last_header is not None and header == last_header:
            labels = last_header  # 跨頁重複表頭 → 丟棄、沿用
        elif (last_header is not None and len(header) != len(last_header)
              and header_words(header) == header_words(last_header)):
            labels = last_header  # 欄位融合變體 → 丟棄、沿用原表頭（含欄數）
        else:
            labels = header
            last_header = header
    elif last_header is not None and data and len(data[0]) == len(last_header):
        labels = last_header  # 無表頭碎片但欄數相同 → 沿用文件中上一個表頭
    return labels, data, last_header


def merge_cells(target, cells):
    """逐欄把 cells 的非空內容以一個空格接到 target 同欄之後（跳過首欄：ID／空欄不接）"""
    for i in range(1, min(len(cells), len(target["cells"]))):
        if cells[i]:
            target["cells"][i] = (target["cells"][i] + " " + cells[i]).strip()


def append_data_rows(data, labels, logical_rows, stats):
    """把資料列轉為邏輯列（就地處理續行 / (Cont'd) / 項目碼切分 / 補欄），附加到 logical_rows"""
    for raw in data:
        if not any(raw):
            continue
        cells = collapse_merged_row(raw)

        # (Cont'd) → 去後綴、逐欄併入同 base ID 的前一列
        m = CONTD_RE.match(cells[0])
        if m and m.group(1).strip():
            base = m.group(1).strip()
            target = next((r for r in reversed(logical_rows) if r["cells"][0] == base), None)
            if target is not None:
                merge_cells(target, cells)
                stats["merges"] += 1
                continue
            cells = [base] + cells[1:]

        # 續行：首 cell 空、其他欄有內容 → 逐欄接上前一列（跨表格碎片亦生效）
        if not cells[0] and any(cells[1:]):
            if logical_rows and len(logical_rows[-1]["cells"]) == len(cells):
                merge_cells(logical_rows[-1], cells)
                stats["merges"] += 1
                continue

        # 項目碼切分：欄數少於表頭、首 cell 開頭是項目碼 → 拆成 ID＋其餘文字再對位
        if labels and len(cells) < len(labels):
            m = ITEM_CODE_RE.match(cells[0])
            if m and any(ch.isdigit() for ch in m.group(1)):
                expanded = [m.group(1), m.group(2).strip()] + cells[1:]
                if len(expanded) == len(labels):
                    cells = expanded

        if labels and len(cells) < len(labels):
            cells = cells + [""] * (len(labels) - len(cells))
        logical_rows.append({"cells": cells, "labels": labels})


def make_title(text):
    return text if len(text) <= TITLE_MAX else text[:TITLE_MAX].rstrip() + "…"


def render_row(row):
    """邏輯列 → '### 標題' 區塊（有表頭 → [標籤]: 行；無表頭 → 無前綴純文字行）"""
    cells, labels = row["cells"], row["labels"]
    first = cells[0] if cells and cells[0] else next((c for c in cells[1:] if c), "")
    body = []
    if len(first) > TITLE_MAX:
        # 標題有截斷 → 首 cell 全文以內文行保留，避免內容遺失
        body.append(f"[{labels[0]}]: {first}" if labels and labels[0] else first)
    for i in range(1, len(cells)):
        if not cells[i]:
            continue
        body.append(f"[{labels[i]}]: {cells[i]}" if labels and i < len(labels) and labels[i] else cells[i])
    return "\n\n".join([f"### {make_title(first)}"] + body)


def flatten_text(text):
    """整檔扁平化 → (新內容, 統計)；無 pipe 表格時僅做 entity 解逸出（表格相關處理 no-op）"""
    stats = {"tables": 0, "merges": 0, "headings": 0}
    text = html.unescape(text)
    lines = text.split("\n")
    in_table = [line.lstrip().startswith("|") for line in lines]
    if not any(in_table):
        return text, stats

    out = []  # (kind, value)：raw=原始行群、block=邏輯列
    raw_buf = []
    seen_headings = set()
    last_header = None
    logical_rows = []

    def flush_raw():
        if raw_buf:
            out.append(("raw", raw_buf.copy()))
            raw_buf.clear()

    i = 0
    while i < len(lines):
        if not in_table[i]:
            stripped = lines[i].strip()
            if HEADING_RE.match(stripped):
                if stripped in seen_headings:
                    stats["headings"] += 1  # Docling 分頁頁首 artifact：完全相同的重複標題 → 去重
                    i += 1
                    continue
                seen_headings.add(stripped)
            raw_buf.append(lines[i])
            i += 1
            continue
        flush_raw()
        j = i
        while j < len(lines) and in_table[j]:
            j += 1
        labels, data, last_header = parse_table_block(lines[i:j], last_header)
        stats["tables"] += 1
        new_rows = len(logical_rows)
        append_data_rows(data, labels, logical_rows, stats)
        for row in logical_rows[new_rows:]:
            out.append(("block", row))
        i = j
    flush_raw()

    # 组装：raw 群組內部保持原始換行；區塊與各段之間以空行分隔，並收斂 3 行以上連續空行
    parts = ["\n".join(val) if kind == "raw" else render_row(val) for kind, val in out]
    new_text = re.sub(r"\n{3,}", "\n\n", "\n\n".join(parts)).strip("\n") + "\n"
    return new_text, stats


def flatten_file(path):
    """單檔就地扁平化（原子寫回；無變更則不寫入），印出統計"""
    with open(path, "r", encoding="utf-8") as f:
        original = f.read()
    new_text, stats = flatten_text(original)
    if new_text != original:
        tmp_path = path + ".flatten-tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(new_text)
        os.replace(tmp_path, path)
    print(f"{path}: 扁平化 {stats['tables']} 表格、合併 {stats['merges']} 續行、去重 {stats['headings']} 標題")
    return stats


def iter_md_files(path):
    if os.path.isfile(path):
        yield path
    elif os.path.isdir(path):
        for root, dirs, files in os.walk(path):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for filename in sorted(files):
                if filename.endswith(".md"):  # 自然排除 *.progress、*.prev
                    yield os.path.join(root, filename)


def main():
    parser = argparse.ArgumentParser(
        description="就地扁平化 Markdown pipe 表格為 ### Q&A 區塊（Docling 輸出後處理，改善 RAG 切塊）"
    )
    parser.add_argument("paths", nargs="+", help="檔案或目錄（目錄 → 遞迴處理 *.md）")
    args = parser.parse_args()
    for path in args.paths:
        if not os.path.exists(path):
            print(f"找不到路徑：{path}", file=sys.stderr)
            continue
        for md_path in iter_md_files(path):
            flatten_file(md_path)


if __name__ == "__main__":
    main()
