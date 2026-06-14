#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
convert_pdf.py — 最小 PDF -> Markdown 转换与结构化抽取系统
================================================================

设计目标(对应作业任务一):
  1. 读取 PDF, 逐页判断是否有文字层(不假设, 实测)。
  2. 文字页: 用 PyMuPDF 抽取文本块(按字号/位置区分 标题/正文/页眉/页脚/脚注),
            用 pdfplumber 抽取表格。
  3. 扫描页(无文字层): 渲染成图片调用 PaddleOCR-VL 做 OCR fallback,
            标记 is_ocr / needs_ocr。关键数值绝不自动采信, 一律进人工复核清单。
  4. 产出:
       outputs/document.md   带 <!-- page: N --> 页码标记的 Markdown
       outputs/blocks.json   逐页文本块 + 来源定位(page + bbox)
       outputs/tables.json   表格专用结构(表标题/表头/行列/页码/数值校验)
       outputs/qa_report.md  解析质量报告 + 自动发现的问题 + 人工复核清单

来源可追溯(source locator):每个 block / table 都带 page 号 + bbox 坐标,
后续 RAG / 检索 / 人工复核都能回溯到原 PDF 的具体位置。

用法:
    python convert_pdf.py <input.pdf> [--outdir outputs] [--dpi 200]
"""

import argparse
import json
import base64
import os
import re
import sys
from datetime import datetime

import fitz  # PyMuPDF
import pdfplumber
import requests

# 从 .env 读取 SILICONFLOW_API_KEY(若装了 python-dotenv)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ----------------------------------------------------------------------------
# 可调阈值(集中放在顶部, 方便针对不同文档复用 / 调参)
# ----------------------------------------------------------------------------
HEADER_MARGIN = 50      # y0 小于该值视为页眉区
FOOTER_MARGIN = 780     # y0 大于该值视为页脚区(A4 高 842)
H1_FONT_SIZE = 16.0     # 字号 >= 该值 -> 一级标题
H2_FONT_SIZE = 11.5     # 字号 >= 该值(且非正文)且像表标题 -> 二级标题
NOTE_FONT_SIZE = 9.5    # 字号 <= 该值 且在正文区 -> 注释/脚注
SCANNED_TEXT_LEN = 30   # 文字层有效字符数 < 该值 且整页有图 -> 判定为扫描页
TOTAL_TOLERANCE = 0.01  # 合计校验浮点容差

# ---- 硅基流动 (SiliconFlow) PaddleOCR-VL 配置 ----
# Key 从环境变量 / .env 读取, 不写进代码, 避免泄露与误提交。
SILICONFLOW_BASE_URL = os.environ.get(
    "SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1/chat/completions")
DEFAULT_OCR_MODEL = "PaddlePaddle/PaddleOCR-VL-1.5"
OCR_PROMPT = (
    "请对这张文档图片做 OCR。完整识别其中所有文字, 保持原阅读顺序, "
    "不要遗漏标题、表标题、注释和页码。\n"
    "若页面含表格, 必须输出标准的 GitHub Markdown 表格, 严格遵守:\n"
    "1) 每一行(包括表头)都用竖线包裹, 形如 `| 列1 | 列2 | 列3 |`;\n"
    "2) 表头下方紧跟一行分隔符 `| --- | --- | --- |`;\n"
    "3) 同一行的各单元格必须在同一行, 不要把单元格拆成多行;\n"
    "4) 表格行之间不要插入空行。\n"
    "只输出识别到的内容本身, 不要添加任何解释或额外说明。"
)

PAGE_NUM_RE = re.compile(r"第\s*(\d+)\s*页")
DATE_COL_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
TOTAL_ROW_RE = re.compile(r"(合计|总计|小计|合\s*计)")
NUM_RE = re.compile(r"^-?[\d,]+(\.\d+)?$")


# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------
def clean(s):
    """规范化空白: 把 \xa0(不间断空格)等替换为普通空格并去首尾。"""
    if s is None:
        return ""
    return re.sub(r"[\u00a0\s]+", " ", s).strip()


def to_number(s):
    """尝试把单元格文本转成 float; 失败返回 None。"""
    if s is None:
        return None
    t = clean(s).replace(",", "")
    if NUM_RE.match(t):
        try:
            return float(t)
        except ValueError:
            return None
    return None


def bbox_center_in(bbox, region):
    """判断 bbox 中心是否落在 region(x0,y0,x1,y1) 内。"""
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    return region[0] <= cx <= region[2] and region[1] <= cy <= region[3]


# ----------------------------------------------------------------------------
# 页面分类: 文字页 vs 扫描页
# ----------------------------------------------------------------------------
def classify_page(page):
    """返回 ('text'|'scanned', text_len, n_images)。不假设, 用实测数据判定。"""
    txt = page.get_text().strip()
    imgs = page.get_images()
    if len(txt) < SCANNED_TEXT_LEN and len(imgs) >= 1:
        return "scanned", len(txt), len(imgs)
    return "text", len(txt), len(imgs)


# ----------------------------------------------------------------------------
# 文字页: 文本块抽取(标题/正文/页眉/页脚/脚注)
# ----------------------------------------------------------------------------
def classify_block(text, size, y0):
    """根据字号和垂直位置给文本块分类。"""
    if y0 < HEADER_MARGIN:
        return "header", None
    if y0 > FOOTER_MARGIN:
        return "footer", None
    if size >= H1_FONT_SIZE:
        return "heading", 1
    # 表标题: 字号略大于正文, 或以"表"开头
    if size >= H2_FONT_SIZE or text.startswith("表"):
        return "heading", 2
    if size <= NOTE_FONT_SIZE and (text.startswith("注") or text.startswith("脚注")
                                   or text.startswith("补充")):
        return "note", None
    return "paragraph", None


def extract_text_blocks(page, page_no, table_regions):
    """
    用 PyMuPDF 抽取文本块。落在表格区域内的文本交给 pdfplumber 处理,
    这里跳过, 避免正文与表格内容重复。
    """
    blocks = []
    d = page.get_text("dict")
    bidx = 0
    for b in d["blocks"]:
        if "lines" not in b:
            continue
        # 把一个 block 内的所有 span 合并为一行文本, 取最大字号代表该块
        line_txt, max_size, bbox = [], 0.0, b["bbox"]
        for l in b["lines"]:
            for s in l["spans"]:
                line_txt.append(s["text"])
                max_size = max(max_size, s["size"])
        text = clean(" ".join(line_txt))
        if not text:
            continue
        # 跳过落在表格区域内的文本(由表格抽取负责)
        if any(bbox_center_in(bbox, r) for r in table_regions):
            continue
        btype, level = classify_block(text, max_size, bbox[1])
        bidx += 1
        blocks.append({
            "block_id": f"p{page_no}_b{bidx}",
            "page": page_no,
            "type": btype,
            "level": level,
            "text": text,
            "font_size": round(max_size, 1),
            "bbox": [round(v, 1) for v in bbox],   # source locator
            "is_ocr": False,
        })
    return blocks


# ----------------------------------------------------------------------------
# 表格抽取(文字页, pdfplumber) + 数值校验
# ----------------------------------------------------------------------------
def verify_table_totals(header, rows):
    """
    合计校验: 找到 合计 行, 对每个数值列检查 明细之和 == 合计值。
    返回 checks 列表 + 总体状态。
    """
    checks = []
    # 找合计行索引
    total_idx = None
    for i, r in enumerate(rows):
        if r and TOTAL_ROW_RE.search(clean(r[0])):
            total_idx = i
            break
    if total_idx is None:
        return {"status": "no_total_row", "columns": []}

    n_cols = max(len(r) for r in rows) if rows else 0
    for c in range(1, n_cols):  # 第 0 列一般是行名
        total_val = to_number(rows[total_idx][c]) if c < len(rows[total_idx]) else None
        if total_val is None:
            continue  # 该列(如"备注")非数值, 跳过
        detail_sum, used = 0.0, 0
        for i, r in enumerate(rows):
            if i == total_idx or c >= len(r):
                continue
            v = to_number(r[c])
            if v is not None:
                detail_sum += v
                used += 1
        if used == 0:
            continue
        ok = abs(detail_sum - total_val) <= TOTAL_TOLERANCE
        col_name = clean(header[c]) if header and c < len(header) else f"col{c}"
        checks.append({
            "column": col_name,
            "detail_sum": round(detail_sum, 2),
            "total_reported": round(total_val, 2),
            "match": ok,
            "diff": round(detail_sum - total_val, 2),
        })
    status = "pass" if checks and all(x["match"] for x in checks) else \
             ("fail" if any(not x["match"] for x in checks) else "inconclusive")
    return {"status": status, "columns": checks}


def detect_period_columns(header):
    """识别期间列(形如 2026-06-30)。"""
    return [clean(h) for h in (header or []) if DATE_COL_RE.search(clean(h or ""))]


def find_table_caption(text_blocks, table_bbox):
    """在表格上方就近找一个以'表'开头的标题块作为表标题。"""
    best, best_dy = None, 1e9
    ty0 = table_bbox[1]
    for b in text_blocks:
        if not b["text"].startswith("表"):
            continue
        dy = ty0 - b["bbox"][3]
        if 0 <= dy < best_dy:
            best, best_dy = b["text"], dy
    return best


def extract_tables(plumber_page, page_no, text_blocks):
    """用 pdfplumber 抽取表格, 附带表标题/期间列/数值校验/来源定位。"""
    tables, regions = [], []
    found = plumber_page.find_tables()
    for ti, tb in enumerate(found, 1):
        data = tb.extract()
        if not data or len(data) < 2:
            continue
        data = [[clean(c) for c in row] for row in data]
        header, rows = data[0], data[1:]
        bbox = [round(v, 1) for v in tb.bbox]
        regions.append(tb.bbox)
        caption = find_table_caption(text_blocks, tb.bbox)
        tables.append({
            "table_id": f"p{page_no}_t{ti}",
            "page": page_no,
            "caption": caption,
            "header": header,
            "rows": rows,
            "n_rows": len(rows),
            "n_cols": len(header),
            "period_columns": detect_period_columns(header),
            "bbox": bbox,                      # source locator
            "is_ocr": False,
            "total_check": verify_table_totals(header, rows),
        })
    return tables, regions


# ----------------------------------------------------------------------------
# 扫描页 OCR fallback: 硅基流动 (SiliconFlow) 上的 PaddleOCR-VL-1.5
# ----------------------------------------------------------------------------
def _page_to_data_uri(page, dpi):
    """把 PDF 页渲染成 PNG 并编码为 data URI(用于多模态 image_url)。"""
    pix = page.get_pixmap(dpi=dpi)
    b64 = base64.b64encode(pix.tobytes("png")).decode("ascii")
    return f"data:image/png;base64,{b64}"


def call_paddleocr_vl(data_uri, api_key, model, base_url, timeout=120):
    """
    调用硅基流动 OpenAI 兼容接口, 用 PaddleOCR-VL 识别图片。
    返回 (markdown_text, usage_dict)。失败抛异常, 由上层降级处理。
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": OCR_PROMPT},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }],
        "temperature": 0.0,   # OCR 要确定性输出, 关掉随机性
        "max_tokens": 4096,
    }
    resp = requests.post(base_url, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    return content, data.get("usage", {})


CELL_NUM_RE = re.compile(r"^-?[\d,]+(\.\d+)?%?$")
TABLE_CAP_RE = re.compile(r"^表\s*\d+\s*[-–]\s*\d+")


def _is_num_cell(s):
    return bool(CELL_NUM_RE.match(s.strip()))


def reconstruct_exploded_tables(md):
    """
    兜底处理 VLM 把表格"炸开"成每个单元格单独一行的情况(无竖线)。
    在"表XX-X"标题后, 按 [表头若干文本格] + [每数据行 = 1个标签 + K个数字]
    的模式重组为标准 Markdown 表格。
    无法可靠判定列结构时, 原样返回(不臆造结构), 由 qa_report 提示人工复核。
    """
    if any("|" in l for l in md.splitlines()):
        return md  # 已有竖线表格, 交给 normalize_md_tables 处理

    raw = md.splitlines()
    out, i = [], 0
    while i < len(raw):
        line = raw[i].strip()
        if not TABLE_CAP_RE.match(line):
            out.append(raw[i])
            i += 1
            continue

        # 命中表标题: 收集其后的"单元格行"(短、非句子)直到遇到注释/句子/空段结束
        out.append(raw[i])
        i += 1
        cells, j = [], i
        while j < len(raw):
            c = raw[j].strip()
            if not c:
                j += 1
                continue
            # 句子/注释 -> 表格结束
            if len(c) > 14 or any(ch in c for ch in "。：，") or c.startswith("注"):
                break
            cells.append(c)
            j += 1

        rebuilt = _cells_to_table(cells)
        if rebuilt:
            out.append("")
            out.extend(rebuilt)
            out.append("")
            i = j                      # 跳过已消费的单元格行
        # 重组失败: 原样保留这些行(不臆造)
        else:
            for k in range(i, j):
                if raw[k].strip():
                    out.append(raw[k])
            i = j
    return "\n".join(out)


def _cells_to_table(cells):
    """把散装单元格按 [表头] + [标签 + K数字] 重组; 失败返回 None。"""
    if len(cells) < 4:
        return None
    # 第一个"后面紧跟数字"的文本格 = 首个数据行标签, 它之前都是表头
    first_data = None
    for idx in range(1, len(cells)):
        if not _is_num_cell(cells[idx]) and idx + 1 < len(cells) \
                and _is_num_cell(cells[idx + 1]):
            first_data = idx
            break
    if first_data is None or first_data < 2:
        return None
    header = cells[:first_data]
    ncols, k = len(header), len(header) - 1
    if k < 1:
        return None

    rows, p = [], first_data
    while p < len(cells):
        label = cells[p]
        nums = cells[p + 1:p + 1 + k]
        if len(nums) < k or not all(_is_num_cell(n) for n in nums):
            return None                # 形状不规整 -> 放弃, 转人工
        rows.append([label] + nums)
        p += 1 + k
    if not rows:
        return None

    md_lines = ["| " + " | ".join(header) + " |",
                "| " + " | ".join(["---"] * ncols) + " |"]
    for r in rows:
        md_lines.append("| " + " | ".join(r) + " |")
    return md_lines


def normalize_md_tables(md):
    """
    规范化 VLM 返回的 Markdown 表格。
    先尝试重组"每格一行"的散装表格(reconstruct_exploded_tables),
    再给 'a | b | c' 这种行首尾缺竖线的行补全为 '| a | b | c |'。
    两者都是为了得到标准 GFM 表格(显示与结构化解析都依赖此格式)。
    """
    md = reconstruct_exploded_tables(md)
    out = []
    for line in md.splitlines():
        s = line.strip()
        if "|" in s and not s.startswith("|"):
            cells = [c.strip() for c in s.split("|")]
            out.append("| " + " | ".join(cells) + " |")
        else:
            out.append(line)
    return "\n".join(out)


def parse_markdown_tables(md, page_no, rect):
    """
    从 VLM 返回的 Markdown 里解析出表格(PaddleOCR-VL 会直接输出 MD 表格)。
    每个表标 is_ocr=True 且 status=NEED_RECHECK, 绝不自动采信。
    """
    tables, lines, i, tcount = [], md.splitlines(), 0, 0
    sep_re = re.compile(r"^\s*\|?[\s:\-|]+\|?\s*$")
    while i < len(lines):
        line = lines[i].strip()
        # 表格 = 以 | 开头的表头行 + 下一行是 |---|---| 分隔行
        if line.startswith("|") and i + 1 < len(lines) and \
                "-" in lines[i + 1] and sep_re.match(lines[i + 1]):
            header = [clean(c) for c in line.strip("|").split("|")]
            rows, j = [], i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                cells = [clean(c) for c in lines[j].strip().strip("|").split("|")]
                rows.append(cells)
                j += 1
            tcount += 1
            tables.append({
                "table_id": f"p{page_no}_ocr_t{tcount}",
                "page": page_no,
                "caption": None,
                "header": header,
                "rows": rows,
                "n_rows": len(rows),
                "n_cols": len(header),
                "period_columns": detect_period_columns(header),
                "bbox": [round(v, 1) for v in rect],   # 扫描页只能定位到整页
                "is_ocr": True,
                "ocr_engine": DEFAULT_OCR_MODEL,
                "total_check": {
                    "status": "NEED_RECHECK",
                    "reason": "OCR(VLM) 结果不可自动采信, 关键数值需人工逐项核对",
                },
            })
            i = j
        else:
            i += 1
    return tables


def ocr_page(page, page_no, dpi, api_key, model, base_url):
    """
    扫描页处理: 渲染 -> 调用 PaddleOCR-VL。返回 (blocks, tables, meta)。
    关键约束: is_ocr=True, needs_ocr=True; VLM 无逐字置信度, 数值强制人工复核。
    任何失败(未配 key / 网络 / 接口错误)都降级为 needs_ocr 标记, 不中断整体流程。
    """
    meta = {"engine": model, "via": "SiliconFlow"}
    rect = page.rect

    def degraded(reason):
        meta["error"] = reason
        block = {
            "block_id": f"p{page_no}_ocr",
            "page": page_no, "type": "scanned_page", "level": None,
            "text": f"[扫描页 / 无文字层] OCR 未产出({reason}), needs_ocr=true。",
            "bbox": [round(v, 1) for v in rect],
            "is_ocr": False, "needs_ocr": True,
        }
        return [block], [], meta

    if not api_key:
        return degraded("未配置 SILICONFLOW_API_KEY(检查 .env)")

    try:
        data_uri = _page_to_data_uri(page, dpi)
        md_text, usage = call_paddleocr_vl(data_uri, api_key, model, base_url)
    except requests.exceptions.RequestException as e:
        return degraded(f"请求失败: {e}")
    except (KeyError, ValueError) as e:
        return degraded(f"响应解析失败: {e}")

    md_text = normalize_md_tables(md_text)   # 规范化 VLM 返回的表格(补全竖线)
    meta["usage"] = usage
    blocks = [{
        "block_id": f"p{page_no}_ocr",
        "page": page_no, "type": "scanned_page", "level": None,
        "text": clean(md_text.replace("\n", " ")),
        "raw_ocr_markdown": md_text,          # 保留 VLM 原始 Markdown, 便于复核
        "bbox": [round(v, 1) for v in rect],
        "is_ocr": True, "needs_ocr": True,
        "ocr_engine": model,
        "ocr_confidence": None,               # VLM 不提供逐字置信度
        "ocr_note": "VLM OCR 无逐字置信度, 关键数值一律 NEED_RECHECK",
    }]
    ocr_tables = parse_markdown_tables(md_text, page_no, rect)
    return blocks, ocr_tables, meta


# ----------------------------------------------------------------------------
# Markdown 渲染
# ----------------------------------------------------------------------------
def table_to_md(t):
    lines = []
    if t.get("caption"):
        lines.append(f"**{t['caption']}**")
        lines.append("")
    header = t.get("header") or (["列%d" % i for i in range(len(t["rows"][0]))]
                                 if t["rows"] else [])
    if header:
        lines.append("| " + " | ".join(header) + " |")
        lines.append("| " + " | ".join(["---"] * len(header)) + " |")
    for r in t["rows"]:
        r = list(r) + [""] * (len(header) - len(r)) if header else r
        lines.append("| " + " | ".join(str(c) for c in r) + " |")
    if t.get("is_ocr"):
        lines.append("")
        lines.append(f"> ⚠️ 本表来自 OCR (扫描页, {t.get('ocr_engine', 'VLM')}); "
                     f"VLM 无逐字置信度, 关键数值需人工复核 (NEED_RECHECK)。")
    return "\n".join(lines)


def tidy_md_blocks(md):
    """
    给 VLM 返回的 Markdown 补块间空行, 使排版与文字页一致。
    规则: 表格行(以 | 开头)之间不留空行以保持表格完整;
          其余每个块(标题/句子/表格整体/注释)之间补一个空行。
    """
    lines = [l.rstrip() for l in md.splitlines() if l.strip()]
    out, prev_is_table = [], False
    for ln in lines:
        is_table = ln.lstrip().startswith("|")
        if out and not (prev_is_table and is_table):
            out.append("")            # 块间空行(表格行之间除外)
        out.append(ln)
        prev_is_table = is_table
    return "\n".join(out)


def render_markdown(pages, src_name):
    out = [f"<!-- generated from: {src_name} at {datetime.now().isoformat(timespec='seconds')} -->",
           ""]
    for p in pages:
        out.append(f"<!-- page: {p['page']} -->")
        if p["kind"] == "scanned":
            out.append("> 🖼️ **扫描页 / 无文字层** — 以下内容由 OCR (VLM) 生成, "
                       "`needs_ocr: true`, 关键数值需人工复核 (NEED_RECHECK)。")
            out.append("")
            # VLM 返回的 Markdown 行间常缺空行, 这里按块补空行使其规范渲染
            b = p["blocks"][0] if p["blocks"] else {}
            out.append(tidy_md_blocks(b.get("raw_ocr_markdown") or b.get("text", "")))
            out.append("")
            out.append("")
            continue
        # 已被用作表标题的文本, 不再作为独立标题输出(避免与表的 caption 重复)
        consumed = {clean(t["caption"]) for t in p["tables"] if t.get("caption")}
        # 标题/正文/脚注按顺序输出
        for b in p["blocks"]:
            if clean(b["text"]) in consumed:
                continue
            if b["type"] == "heading" and b.get("level") == 1:
                out.append(f"# {b['text']}")
            elif b["type"] == "heading":
                out.append(f"## {b['text']}")
            elif b["type"] == "note":
                out.append(f"> {b['text']}")
            elif b["type"] in ("paragraph", "scanned_page"):
                out.append(b["text"])
            # header/footer 不进正文(下方页脚单独用于页码核对)
            out.append("")
        for t in p["tables"]:
            out.append(table_to_md(t))
            out.append("")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# ----------------------------------------------------------------------------
# QA 报告
# ----------------------------------------------------------------------------
def build_qa_report(pages, all_tables, src_name):
    L = []
    L.append("# PDF 转 Markdown 解析质量报告 (qa_report.md)")
    L.append("")
    L.append(f"- 源文件: `{src_name}`")
    L.append(f"- 生成时间: {datetime.now().isoformat(timespec='seconds')}")
    L.append(f"- 总页数: {len(pages)}")
    n_scanned = sum(1 for p in pages if p["kind"] == "scanned")
    L.append(f"- 文字页: {len(pages) - n_scanned}; 扫描页: {n_scanned}")
    L.append(f"- 表格总数: {len(all_tables)}")
    L.append("")

    # 1. 页码核对(来源可追溯)
    L.append("## 1. 页码核对(来源追溯)")
    L.append("")
    L.append("| 物理页序 | 页内打印页码 | 是否一致 |")
    L.append("| --- | --- | --- |")
    for p in pages:
        printed = p.get("printed_page_no")
        ok = "✅" if printed == p["page"] else ("⚠️ 不一致" if printed else "—(未检出)")
        L.append(f"| {p['page']} | {printed if printed else '—'} | {ok} |")
    L.append("")

    # 2. 自动数值校验(合计 = 明细之和)
    L.append("## 2. 合计数自动校验")
    L.append("")
    any_table = False
    for t in all_tables:
        tc = t.get("total_check", {})
        if tc.get("status") in ("pass", "fail"):
            any_table = True
            flag = "✅ 通过" if tc["status"] == "pass" else "❌ 不一致"
            L.append(f"- **{t.get('caption') or t['table_id']}** (page {t['page']}): {flag}")
            for c in tc.get("columns", []):
                mark = "✅" if c["match"] else "❌"
                L.append(f"    - {mark} 列 `{c['column']}`: 明细和 {c['detail_sum']} "
                         f"vs 合计 {c['total_reported']} (差 {c['diff']})")
        elif tc.get("status") == "NEED_RECHECK":
            L.append(f"- **{t['table_id']}** (page {t['page']}): ⚠️ NEED_RECHECK — "
                     f"{tc.get('reason', '')}")
    if not any_table:
        L.append("- (无可自动校验的合计行)")
    L.append("")

    # 3. 自动发现的问题
    L.append("## 3. 自动发现的问题")
    L.append("")
    issues = []
    for t in all_tables:
        # 行名缺失
        for ri, r in enumerate(t["rows"]):
            if not clean(r[0]):
                issues.append(f"表 `{t['table_id']}` (page {t['page']}) 第 {ri+1} 行行名缺失。")
        # 期间列未识别
        if not t.get("is_ocr") and not t.get("period_columns"):
            issues.append(f"表 `{t['table_id']}` (page {t['page']}) 未识别到期间列(形如 2026-06-30), 请人工确认表头。")
        # 合计不一致
        tc = t.get("total_check", {})
        if tc.get("status") == "fail":
            issues.append(f"表 `{t['table_id']}` (page {t['page']}) 合计数与明细之和不一致, 必须人工核对。")
    for p in pages:
        if p["kind"] == "scanned":
            if p.get("ocr_meta", {}).get("error"):
                issues.append(f"page {p['page']} 扫描页 OCR 未成功产出: "
                              f"{p['ocr_meta']['error']}; 需补 OCR 或人工录入。")
            else:
                issues.append(f"page {p['page']} 为扫描页(VLM OCR), 无逐字置信度, "
                              "表格行数与关键数值可能与原图有出入, 需人工核对。")
    if issues:
        for it in issues:
            L.append(f"- ⚠️ {it}")
    else:
        L.append("- 未发现自动可检测的问题。")
    L.append("")

    # 4. 必须人工复核清单
    L.append("## 4. 必须人工复核清单(自动判断不可靠)")
    L.append("")
    review = []
    for p in pages:
        if p["kind"] == "scanned":
            review.append(f"page {p['page']}: 扫描页(VLM OCR)全部内容需人工逐项核对, "
                          "尤其关键数值, 存在漏行/串列风险。")
    for t in all_tables:
        if t.get("is_ocr"):
            review.append(f"表 `{t['table_id']}` (page {t['page']}): OCR 候选表格, 标记 NEED_RECHECK, 不得直接写入 final。")
    # 跨口径行名(规则内置: 包含顿号、可能被拆行的合并项)
    for t in all_tables:
        for r in t["rows"]:
            label = clean(r[0])
            if "、" in label and len(label) >= 6:
                review.append(f"表 `{t['table_id']}` 行 “{label}” 口径较宽, "
                              "在 Excel 中可能被拆成多行, 需人工判断是否同一口径。")
    if review:
        for r in review:
            L.append(f"- 🔍 {r}")
    else:
        L.append("- 无。")
    L.append("")

    # 5. 优化思路
    L.append("## 5. 已知局限与优化思路")
    L.append("")
    L.extend([
        "- **OCR 表格还原**: 扫描页改用硅基流动 PaddleOCR-VL-1.5(VLM)识别, 直接产出 "
        "Markdown 表格; 但 VLM 无逐字置信度, 且可能漏行/串列, 关键数值一律 NEED_RECHECK。",
        "- **跨页表格**: 本样例无跨页表, 但生产中需按表头连续性合并跨页表格。",
        "- **数值单位**: 表注声明“单位:百万元”, 当前未把单位绑定到每个数值, 可在 blocks 中补 `unit` 字段。",
        "- **OCR 数值校验**: OCR 表不参与自动合计校验(防止误判), 改为强制 NEED_RECHECK。",
    ])
    L.append("")
    return "\n".join(L) + "\n"


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def convert(pdf_path, outdir, dpi, api_key, ocr_model, base_url):
    os.makedirs(outdir, exist_ok=True)
    src_name = os.path.basename(pdf_path)
    doc = fitz.open(pdf_path)
    plumber = pdfplumber.open(pdf_path)

    pages, all_tables = [], []

    for idx in range(len(doc)):
        page_no = idx + 1
        fpage = doc[idx]
        kind, text_len, n_imgs = classify_page(fpage)
        printed = None
        m = PAGE_NUM_RE.search(fpage.get_text())
        if m:
            printed = int(m.group(1))

        page_rec = {"page": page_no, "kind": kind, "text_len": text_len,
                    "n_images": n_imgs, "printed_page_no": printed,
                    "blocks": [], "tables": []}

        if kind == "text":
            # 先抽表(拿到表格区域), 再抽文本块(排除表格区域)
            ppage = plumber.pages[idx]
            # 先抽一遍文本块(不排除)用于找表标题
            prelim = extract_text_blocks(fpage, page_no, [])
            tables, regions = extract_tables(ppage, page_no, prelim)
            blocks = extract_text_blocks(fpage, page_no, regions)
            page_rec["blocks"] = blocks
            page_rec["tables"] = tables
            all_tables.extend(tables)
        else:  # scanned
            blocks, ocr_tables, meta = ocr_page(
                fpage, page_no, dpi, api_key, ocr_model, base_url)
            page_rec["blocks"] = blocks
            page_rec["tables"] = ocr_tables
            page_rec["ocr_meta"] = meta
            all_tables.extend(ocr_tables)

        pages.append(page_rec)

    plumber.close()

    # ---- 写出 document.md ----
    md = render_markdown(pages, src_name)
    with open(os.path.join(outdir, "document.md"), "w", encoding="utf-8") as f:
        f.write(md)

    # ---- 写出 blocks.json ----
    blocks_doc = {
        "source": src_name,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "page_count": len(pages),
        "pages": [
            {k: v for k, v in p.items() if k != "tables"}  # 表格单独存 tables.json
            for p in pages
        ],
    }
    with open(os.path.join(outdir, "blocks.json"), "w", encoding="utf-8") as f:
        json.dump(blocks_doc, f, ensure_ascii=False, indent=2)

    # ---- 写出 tables.json ----
    with open(os.path.join(outdir, "tables.json"), "w", encoding="utf-8") as f:
        json.dump({"source": src_name, "tables": all_tables},
                  f, ensure_ascii=False, indent=2)

    # ---- 写出 qa_report.md ----
    qa = build_qa_report(pages, all_tables, src_name)
    with open(os.path.join(outdir, "qa_report.md"), "w", encoding="utf-8") as f:
        f.write(qa)

    # 控制台摘要
    n_scanned = sum(1 for p in pages if p["kind"] == "scanned")
    print(f"[ok] {src_name}: {len(pages)} 页 (文字页 {len(pages)-n_scanned}, "
          f"扫描页 {n_scanned}), 表格 {len(all_tables)} 个")
    print(f"[ok] 输出目录: {outdir}/  -> document.md / blocks.json / tables.json / qa_report.md")


def main():
    # 脚本所在目录(不依赖启动时的工作目录, PyCharm 直接点运行也能找到文件)
    here = os.path.dirname(os.path.abspath(__file__))
    pdf_name = "sample_pdf_to_markdown_note.pdf"
    # 优先用推荐结构 assignment_pdf_markdown_skill_assets/ 下的 PDF, 退回脚本同目录
    asset_pdf = os.path.join(here, "assignment_pdf_markdown_skill_assets", pdf_name)
    default_pdf = asset_pdf if os.path.isfile(asset_pdf) else os.path.join(here, pdf_name)
    default_outdir = os.path.join(here, "outputs")

    ap = argparse.ArgumentParser(description="最小 PDF -> Markdown 转换与结构化抽取")
    ap.add_argument("pdf", nargs="?", default=default_pdf,
                    help="输入 PDF 路径(默认: assignment_pdf_markdown_skill_assets/"
                         "sample_pdf_to_markdown_note.pdf)")
    ap.add_argument("--outdir", default=default_outdir, help="输出目录(默认 outputs)")
    ap.add_argument("--dpi", type=int, default=200, help="扫描页渲染 DPI(默认 200)")
    ap.add_argument("--ocr-model", default=DEFAULT_OCR_MODEL,
                    help=f"硅基流动 OCR 模型(默认 {DEFAULT_OCR_MODEL})")
    ap.add_argument("--base-url", default=SILICONFLOW_BASE_URL,
                    help="OpenAI 兼容接口地址(默认硅基流动)")
    args = ap.parse_args()

    if not os.path.isfile(args.pdf):
        print(f"[err] 找不到文件: {args.pdf}", file=sys.stderr)
        sys.exit(1)
    print(f"[info] 处理: {args.pdf}")

    api_key = os.environ.get("SILICONFLOW_API_KEY", "").strip()
    if not api_key:
        print("[warn] 未找到 SILICONFLOW_API_KEY(检查 .env), 扫描页将仅标记 needs_ocr。",
              file=sys.stderr)

    convert(args.pdf, args.outdir, args.dpi,
            api_key=api_key,
            ocr_model=args.ocr_model,
            base_url=args.base_url)


if __name__ == "__main__":
    main()