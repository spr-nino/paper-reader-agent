"""PDF 解析：提取结构、章节、页面图像、真实页码 chunk"""
import re
from pathlib import Path

import fitz  # PyMuPDF

DATA_DIR = Path(__file__).parent.parent / "data"

# 扫描版检测阈值：每页平均字符数低于此值时，认为没有可提取的文字层
SCAN_CHARS_PER_PAGE_THRESHOLD = 50
OCR_DPI = 200

HEADING_KEYWORDS = re.compile(
    r"^(abstract|introduction|background|related work|method|methods|methodology|"
    r"experiments?|results?|discussion|conclusion|references|acknowledg|"
    r"摘要|引言|简介|背景|相关工作|方法|实验|结果|讨论|结论|参考文献|致谢|"
    r"\d+(\.\d+)*\.?\s+\S)", re.IGNORECASE)

# P1-4：低价值章节黑名单（跳过分析，节省 token）
SKIP_SECTION_RE = re.compile(
    r"^(references|bibliography|acknowledg(e)?ments|"
    r"appendix|appendices|"
    r"参考文献|致谢|附录|"
    r"author contributions|funding|disclosures?|"
    r"数据可用性|利益冲突)",
    re.IGNORECASE)

class ScannedPDFError(Exception):
    """扫描版 PDF（或 OCR 后仍无文字）"""


class ParseError(Exception):
    """解析结果异常，无法进入分析阶段"""


def _is_heading(text: str, size: float, max_size: float) -> bool:
    t = text.strip()
    if not t or len(t) > 120 or "\n" in t:
        return False
    if HEADING_KEYWORDS.match(t):
        return True
    # 字体明显大于正文
    return size >= max_size * 0.92 and len(t) < 80


def _split_text(text: str, size: int = 5000):
    """单页内超长时按段落/换行边界切分（不跨页，保持页码真实）"""
    parts = re.split(r"(?<=\n)", text)
    buf = ""
    for part in parts:
        if buf and len(buf) + len(part) > size:
            yield buf
            buf = part
        else:
            buf += part
    if buf.strip():
        yield buf


def _ocr_page_texts(doc: fitz.Document, dpi: int = OCR_DPI):
    """可选 OCR 分支：需要 pytesseract + Pillow + Tesseract 引擎。

    返回每页识别到的文本列表；若依赖缺失、引擎未安装或识别失败则返回 None。
    """
    try:
        import pytesseract
        from PIL import Image
        # 早期探测 Tesseract 引擎是否真正可用
        pytesseract.get_tesseract_version()
    except Exception:
        return None

    try:
        available = pytesseract.get_languages()
    except Exception:
        available = ["eng"]
    lang = "chi_sim+eng" if "chi_sim" in available else "eng"

    texts = []
    for page in doc:
        pix = page.get_pixmap(dpi=dpi)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        try:
            txt = pytesseract.image_to_string(img, lang=lang)
        except Exception:
            txt = ""
        texts.append(txt)
    return texts


def _detect_scanned_and_maybe_ocr(doc: fitz.Document, pages: list) -> tuple:
    """检测扫描版 PDF。若文字极少且 OCR 可用，则尝试 OCR 并返回更新后的 pages。

    返回 (pages, ocr_used)。当确定无文字且 OCR 失败或仍无文字时，抛出 ScannedPDFError。
    """
    n_pages = len(pages)
    if n_pages == 0:
        raise ParseError("PDF 没有页面")

    total_chars = sum(len(p["text"].strip()) for p in pages)
    avg_chars = total_chars / n_pages
    if avg_chars >= SCAN_CHARS_PER_PAGE_THRESHOLD:
        return pages, False

    # 文字层稀薄，尝试 OCR（可选依赖）
    ocr_texts = _ocr_page_texts(doc)
    if ocr_texts is None:
        raise ScannedPDFError(
            "该 PDF 为扫描版，未提取到文字。请使用带文字层的 PDF，或先经 OCR 工具处理。"
        )

    for p, txt in zip(pages, ocr_texts):
        p["text"] = txt
        # OCR 无法还原版式块信息，清空旧的 layout blocks
        p["blocks"] = []

    ocr_total = sum(len(t.strip()) for t in ocr_texts)
    if ocr_total / n_pages < SCAN_CHARS_PER_PAGE_THRESHOLD:
        raise ScannedPDFError(
            "OCR 后仍未提取到足够文字，疑似空白页或扫描质量过低。"
        )
    return pages, True


def _should_skip_section(title: str) -> bool:
    return bool(SKIP_SECTION_RE.match(title.strip()))


def build_chunks(sections, target: int = 5000, hard: int = 6000) -> list:
    """按页边界切 chunk：每个 chunk 携带真实页码区间 page_from/page_to。

    - 优先在页边界合并/切分，chunk 绝不跨页推算页码；
    - 单页文本超过 hard 时，在该页内按段落边界切（page_from == page_to）；
    - 目标大小 target 有弹性，容忍单页超短（参考文献页等）。
    """
    chunks = []
    skipped = []
    for sec in sections:
        if _should_skip_section(sec.get("title", "")):
            skipped.append(sec["title"])
            continue
        buf, p_from, p_to = [], None, None

        def flush():
            nonlocal buf, p_from, p_to
            if buf:
                chunks.append({"section": sec["title"], "page_from": p_from,
                               "page_to": p_to, "text": "\n".join(buf)})
            buf, p_from, p_to = [], None, None

        for pno, ptext in sec.get("page_texts", []):
            ptext = ptext.strip()
            if not ptext:
                continue
            if len(ptext) > hard and not buf:
                # 单页超长：页内按段落边界切，页码区间保持该页
                for piece in _split_text(ptext, target):
                    chunks.append({"section": sec["title"], "page_from": pno,
                                   "page_to": pno, "text": piece})
                continue
            if buf and sum(len(x) + 1 for x in buf) + len(ptext) > hard:
                flush()
            buf.append(ptext)
            p_from = p_from or pno
            p_to = pno
            if sum(len(x) + 1 for x in buf) >= target:
                flush()
        flush()
    return chunks


def parse_pdf(file_path: str, doc_id: str) -> dict:
    doc = fitz.open(file_path)
    pages_dir = DATA_DIR / doc_id / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    pages = []
    headings = []  # (page, size, text)

    for i, page in enumerate(doc):
        # 渲染页面缩略图（用于图表/原文对照，文件名与真实页码一一对应）
        pix = page.get_pixmap(dpi=110)
        pix.save(str(pages_dir / f"{i+1}.png"))

        d = page.get_text("dict")
        blocks_text = []
        sizes = []
        for b in d.get("blocks", []):
            if b.get("type") != 0:
                continue
            for line in b.get("lines", []):
                line_text = "".join(s["text"] for s in line.get("spans", [])).strip()
                if not line_text:
                    continue
                max_size = max((s["size"] for s in line.get("spans", [])), default=0)
                sizes.append(max_size)
                blocks_text.append({"text": line_text, "size": round(max_size, 1)})
        body_size = sorted(sizes)[len(sizes) // 2] if sizes else 11
        for bt in blocks_text:
            if _is_heading(bt["text"], bt["size"], max(body_size, 1) + 2):
                headings.append((i + 1, bt["text"]))
        pages.append({
            "page": i + 1,
            "text": page.get_text("text"),
            "blocks": blocks_text,
            "n_images": len([b for b in d.get("blocks", []) if b.get("type") == 1]),
        })

    # P0-4：检测扫描版/无文字 PDF，必要时走 OCR 兜底
    pages, ocr_used = _detect_scanned_and_maybe_ocr(doc, pages)

    # 构建章节：优先用文档自带目录，否则用启发式标题
    toc = doc.get_toc()
    sections = []
    if len(toc) >= 3:
        for lvl, title, pno in toc:
            if pno < 1:
                continue
            sections.append({"title": title.strip(), "level": lvl, "page_start": pno})
    else:
        seen = set()
        for pno, text in headings:
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            sections.append({"title": text, "level": 1, "page_start": pno})

    # 若 OCR 后没有任何章节/标题，退化为单章节“全文”，避免直接空报告
    if not sections and ocr_used:
        sections.append({"title": "全文", "level": 1, "page_start": 1})

    # 合并章节文本（按页码区间），保留 page_texts 以维持"文本 ↔ 页码"映射
    sections.sort(key=lambda s: s["page_start"])
    merged = []
    for idx, s in enumerate(sections):
        end = (sections[idx + 1]["page_start"] - 1) if idx + 1 < len(sections) else len(pages)
        spans = [(p["page"], p["text"]) for p in pages
                 if s["page_start"] <= p["page"] <= min(end, len(pages))]
        text = "\n".join(t for _, t in spans)
        merged.append({
            "title": s["title"], "level": s["level"],
            "page_start": s["page_start"], "page_end": min(end, len(pages)),
            "text": text.strip(),
            "page_texts": spans,
        })
    merged = [m for m in merged if len(m["text"]) > 40]

    skipped_sections = [m["title"] for m in merged if _should_skip_section(m["title"])]
    analyzable = [m for m in merged if not _should_skip_section(m["title"])]

    if not analyzable:
        if skipped_sections:
            raise ParseError("解析到的章节均在黑名单内（参考文献/致谢等），无内容可分析。")
        raise ParseError("未解析到任何章节，PDF 可能为空或格式不支持。")

    meta = doc.metadata or {}
    title = (meta.get("title") or "").strip() or Path(file_path).stem
    return {
        "doc_id": doc_id,
        "title": title,
        "author": (meta.get("author") or "").strip(),
        "n_pages": len(pages),
        "pages": pages,
        "sections": merged,
        "skipped_sections": skipped_sections,
        "chunks": build_chunks(analyzable),
    }