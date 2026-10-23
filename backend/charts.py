"""图表双通道提取 + 回验（P0-2）。

架构：
- 通道 A（表格）：PyMuPDF find_tables() 确定性抽取，零幻觉，不经过 LLM；
- 通道 B（图片）：启发式筛页后送视觉模型识别，仅作兜底；
- 后置校验（两通道共用）：结构校验（labels/values 等长、数值、类型白名单）
  + 数值回验（对原文文本模糊匹配，±5% 容差），匹配不到的数值默认不进图表，
  只在报告附录里以"未核验数据"列出。
"""
import base64
import logging
import uuid
import re
from pathlib import Path

import fitz  # PyMuPDF
import parser  # F16：统一引用 parser.DATA_DIR，使 --data-dir 配置对图表视觉通道生效

logger = logging.getLogger("charts")

CHART_TYPES = ("bar", "line", "pie")

# 启发式筛页：有嵌入图片，或文本中出现图表提示词
FIG_HINT = re.compile(r"\b(fig(?:ure)?s?\.?|table\s*\d*|chart|graph)\b|图\s*\d+|表\s*\d+",
                      re.IGNORECASE)
_NUM_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*(?:[eE][-+]?\d+)?")
CAPTION_RE = re.compile(r"^\s*((?:Table|Tab\.|表)\s*[\dIVX]+[\.:]?)\s*(.+)?", re.IGNORECASE)


# ---------------------------------------------------------------- 工具函数

def _to_float(s) -> float | None:
    """宽松数值解析：'1,234.5' / '72%' / '±0.03' -> float；失败返回 None。"""
    if s is None:
        return None
    if isinstance(s, (int, float)) and not isinstance(s, bool):
        return float(s)
    t = str(s).strip().replace(",", "").replace("%", "").replace("±", "").strip()
    if t.startswith(("(", "[")) and t.endswith((")", "]")):
        t = t[1:-1]
    try:
        return float(t)
    except ValueError:
        return None


def _norm_num_str(v: float) -> str:
    """数值归一化字符串：72.0 -> '72'，0.050 -> '0.05'（用于文本精确匹配）。"""
    s = f"{v:.6g}"
    return s


def extract_number_tokens(text: str) -> list[float]:
    """从文本中提取全部数值 token（去逗号/百分号），供回验比对。"""
    out = []
    for m in _NUM_RE.finditer(text or ""):
        v = _to_float(m.group(0))
        if v is not None:
            out.append(v)
    return out


def verify_value(value: float, tokens: list[float], rel_tol: float = 0.05) -> bool:
    """数值回验：归一化字符串相等，或与原文任一数值 ±rel_tol 相对误差内。"""
    if value is None:
        return False
    target = _norm_num_str(value)
    for t in tokens:
        if _norm_num_str(t) == target:
            return True
        if t != 0 and abs(t - value) / abs(t) <= rel_tol:
            return True
        if t == 0 and value == 0:
            return True
    return False


# ---------------------------------------------------------------- 结构校验

def validate_chart_spec(spec: dict) -> bool:
    """结构校验：类型白名单、labels/values 为等长数组、values 全为数值。不合格丢弃。"""
    ctype = str(spec.get("chart_type") or spec.get("type") or "").lower()
    if ctype not in CHART_TYPES:
        logger.info("[charts] 丢弃：chart_type 不在白名单 %s", ctype)
        return False
    labels, values = spec.get("labels"), spec.get("values")
    if (not isinstance(labels, list) or not isinstance(values, list)
            or len(labels) != len(values) or len(labels) < 2):
        logger.info("[charts] 丢弃：labels/values 缺失或长度不一致 (%s vs %s)",
                    len(labels) if isinstance(labels, list) else "-",
                    len(values) if isinstance(values, list) else "-")
        return False
    nums = [_to_float(v) for v in values]
    if any(v is None for v in nums):
        logger.info("[charts] 丢弃：values 含非数值 %s", values)
        return False
    spec["values"] = nums  # 归一化为 float
    spec["chart_type"] = ctype
    return True


# ---------------------------------------------------------------- 通道 A：表格

def _rows_to_specs(rows: list, page_no: int, caption: str) -> list:
    """把一张抽取出的表（行数组）转成候选 chart_spec（纯程序化，不经 LLM）。"""
    rows = [[("" if c is None else str(c)).strip() for c in r] for r in rows]
    rows = [r for r in rows if any(r)]
    if len(rows) < 3:
        return []
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]

    data = rows[1:]
    # 每列数值密度（排除第 0 列——通常是标签列）
    col_score = []
    for ci in range(1, ncols):
        nums = [ _to_float(r[ci]) for r in data ]
        col_score.append((ci, sum(v is not None for v in nums)))
    col_score = [(ci, s) for ci, s in col_score if s >= max(2, len(data) // 2 + 1)]
    if not col_score:
        return []

    specs = []
    labels = [r[0] for r in data]
    if sum(bool(l) for l in labels) < 2:
        return []
    # 标签像年份/递增序列 -> line，否则 bar
    lab_nums = [_to_float(l) for l in labels]
    is_series = (all(v is not None for v in lab_nums)
                 and len(set(labels)) == len(labels))
    ctype = "line" if is_series else "bar"

    for ci, _ in col_score[:2]:  # 每张表最多取 2 个数值列，控制图表数量
        header = rows[0][ci] or ""
        values = [_to_float(r[ci]) for r in data]
        pairs = [(l, v) for l, v in zip(labels, values) if l and v is not None]
        if len(pairs) < 2:
            continue
        title = caption or (f"第 {page_no} 页表格" + (f"：{header}" if header else ""))
        specs.append({
            "title": title.strip(),
            "chart_type": ctype,
            "labels": [p[0] for p in pairs],
            "values": [p[1] for p in pairs],
            "unit": "",
            "note": f"表格抽取（{header}）" if header else "表格抽取",
            "source": "table_extract",
            "page_from": page_no,
            "page_to": page_no,
        })
    return specs


def extract_table_charts(pdf_path: str, max_tables: int = 8) -> list:
    """通道 A：逐页 find_tables() 确定性抽取表格型图表。零 LLM 调用。"""
    specs = []
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        logger.warning("[charts] 打开 PDF 失败，跳过表格通道：%s", e)
        return []
    with doc:
        for pno in range(len(doc)):
            if len(specs) >= max_tables:
                break
            page = doc[pno]
            try:
                finder = page.find_tables()
            except Exception as e:
                logger.warning("[charts] 第 %d 页 find_tables 失败：%s", pno + 1, e)
                continue
            if not finder.tables:
                continue
            # 页首 Caption（Table 1: xxx / 表 1 xxx）
            caption = ""
            for line in (page.get_text("text") or "").splitlines():
                m = CAPTION_RE.match(line)
                if m:
                    caption = (m.group(2) or "").strip() or m.group(1).strip()
                    break
            for tbl in finder.tables:
                if len(specs) >= max_tables:
                    break
                try:
                    specs.extend(_rows_to_specs(tbl.extract(), pno + 1, caption))
                except Exception as e:
                    logger.warning("[charts] 表格转换失败（第 %d 页）：%s", pno + 1, e)
    logger.info("[charts] 表格通道产出 %d 张图表", len(specs))
    return specs


# ---------------------------------------------------------------- 通道 B：视觉

VISION_PROMPT = """你是论文图表识别助手。请识别这页 PDF 页面中的图表（坐标图、柱状图、饼图等），
把图中可读的量化数据转录为结构化 JSON。

要求：
1. 只输出图中**真实可见**的数据，看不清或没有坐标刻度的图不要输出；
2. 数值尽量精确转录（含单位换算到数值本身），不要估算编造；
3. 严格输出 JSON（不要其他内容），格式：
{{"charts": [{{"title": "图表标题", "chart_type": "bar|line|pie",
  "labels": ["类别1", "类别2"], "values": [数值1, 数值2], "unit": "单位"}}]}}
4. 若本页没有可量化转录的图表，输出 {{"charts": []}}。"""


def select_vision_pages(doc: dict, max_pages: int = 6) -> list:
    """启发式筛页：优先选含嵌入图片或图表提示词的页，控制视觉调用成本。"""
    scored = []
    for p in doc.get("pages", []):
        score = 0
        if p.get("n_images"):
            score += 2
        if FIG_HINT.search(p.get("text") or ""):
            score += 1
        if score:
            scored.append((score, p["page"]))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [p for _, p in scored[:max_pages]]


async def vision_chart_specs(doc: dict, client, model: str, max_pages: int = 6) -> list:
    """通道 B：对候选页渲染 PNG 送视觉模型识别（兜底通道）。

    视觉模型不可用时（不支持图片/调用失败）返回空列表并记日志，不影响主流程。
    """
    from openai import AsyncOpenAI  # noqa: F401  （类型提示用）
    pages_dir = parser.DATA_DIR / doc["doc_id"] / "pages"
    candidates = select_vision_pages(doc, max_pages)
    specs = []

    async def one(pno: int):
        png = pages_dir / f"{pno}.png"
        if not png.exists():
            return
        b64 = base64.b64encode(png.read_bytes()).decode()
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": VISION_PROMPT},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ]}],
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            import json as _json
            raw = resp.choices[0].message.content
            data = _json.loads(raw)
        except Exception as e:
            logger.warning("[charts] 视觉识别第 %d 页失败：%s", pno, e)
            return
        for c in data.get("charts", []) if isinstance(data, dict) else []:
            if isinstance(c, dict):
                c["source"] = "vision"
                c["page_from"] = c["page_to"] = pno
                specs.append(c)

    import asyncio
    await asyncio.gather(*(one(p) for p in candidates))
    logger.info("[charts] 视觉通道产出 %d 张图表（候选页 %s）", len(specs), candidates)
    return specs


# ---------------------------------------------------------------- 回验与合并

def verify_spec_values(spec: dict, page_tokens: dict) -> list[bool]:
    """对 spec 的每个 value 在所属页文本中做模糊回验，返回逐值 verified 标志。"""
    p_from = spec.get("page_from") or 1
    p_to = spec.get("page_to") or p_from
    tokens = []
    for p in range(p_from, p_to + 1):
        tokens.extend(page_tokens.get(p, []))
    return [verify_value(v, tokens) for v in spec.get("values", [])]


def process_charts(specs: list, doc: dict) -> dict:
    """统一入口：结构校验 -> 数值回验 -> 过滤未核验值 -> 去重 -> 产出可渲染图表。

    返回 {"charts": 可渲染, "unverified": 附录未核验, "stats": 统计}。
    """
    page_tokens = {p["page"]: extract_number_tokens(p.get("text") or "")
                   for p in doc.get("pages", [])}
    stats = {"table_extract": 0, "vision": 0, "text_llm": 0,
             "vision_values": 0, "vision_verified": 0,
             "dropped_structure": 0, "dropped_unverified": 0}

    valid, seen = [], set()
    for spec in specs:
        if not validate_chart_spec(spec):
            stats["dropped_structure"] += 1
            continue
        key = re.sub(r"\s+", "", str(spec.get("title", ""))).lower()
        if key and key in seen:  # 同标题去重：先到的优先（表格通道最先）
            continue
        seen.add(key)
        valid.append(spec)

    charts, appendix = [], []
    for spec in valid:
        src = spec.get("source", "text_llm")
        if src in stats:
            stats[src] += 1
        flags = verify_spec_values(spec, page_tokens)
        if src == "vision":
            stats["vision_values"] += len(flags)
            stats["vision_verified"] += sum(flags)

        pairs = [(l, v, f) for l, v, f in
                 zip(spec["labels"], spec["values"], flags)]
        good = [(l, v) for l, v, f in pairs if f]
        bad = [(l, v) for l, v, f in pairs if not f]

        if src == "table_extract":
            # 确定性抽取：即使文本层缺数值（扫描字形等）也保留，仅如实标记
            spec["verified"] = all(flags)
        elif bad:
            spec["verified"] = False
            stats["dropped_unverified"] += len(bad)
            if bad:
                appendix.append({
                    "title": spec.get("title", ""),
                    "source": src,
                    "page_from": spec.get("page_from"),
                    "items": [{"label": l, "value": v} for l, v in bad],
                    "reason": "数值在原文中未匹配到，未渲染",
                })
        else:
            spec["verified"] = True

        if len(good) >= 2:
            spec["labels"] = [l for l, _ in good]
            spec["values"] = [v for _, v in good]
            spec["chart_id"] = f"ch_{uuid.uuid4().hex[:8]}"
            charts.append(spec)
        else:
            stats["dropped_unverified"] += len(good)
            appendix.append({
                "title": spec.get("title", ""),
                "source": src,
                "page_from": spec.get("page_from"),
                "items": [{"label": l, "value": v} for l, v in good + bad],
                "reason": "核验后剩余数据点不足 2 个，未渲染",
            })

    stats["vision_pass_rate"] = (round(stats["vision_verified"] / stats["vision_values"], 4)
                                 if stats["vision_values"] else None)
    charts = charts[:12]
    return {"charts": charts, "unverified": appendix, "stats": stats}
