"""LLM 分析管线：Map-Reduce 提取结构化要点、图表数据、知识卡片"""
import json
import asyncio
import re
from openai import AsyncOpenAI

from parser import build_chunks, DATA_DIR
from charts import (extract_table_charts, vision_chart_specs,
                    process_charts)
from critic import critic_pass
from anki_export import _dedupe_cards

MAP_PROMPT = """你是文献速读助手。请阅读下面这篇文献的一个章节片段，提取结构化信息。

文献标题：{title}
章节：{section_title}
本块文本来自原文第 {page_from}-{page_to} 页（含两端），你标注的页码必须是该区间内的整数，不要推测区间之外的页码。
正文：
{text}

请严格输出 JSON（不要输出其他内容），格式：
{{
  "key_points": [
    {{
      "point": "要点一句话，不超过80字",
      "type": "conclusion|method|data|limitation|innovation|background",
      "evidence": "支撑原文的关键句摘录（简短）",
      "page": 页码数字,
      "confidence": 0.0到1.0
    }}
  ],
  "terms": [{{"term": "术语", "definition": "一句话通俗解释"}}],
  "chart_specs": [
    {{
      "title": "图表标题",
      "chart_type": "bar|line|pie",
      "labels": ["类别1", "类别2"],
      "values": [数值1, 数值2],
      "unit": "单位",
      "note": "数据说明或出处页码"
    }}
  ],
  "flashcards": [
    {{
      "front": "基于本片段核心知识设计的问答卡正面问题（不要只写\"什么是\"）",
      "back": "简洁答案（≤200字）",
      "page": 页码数字
    }}
  ]
}}

要求：
1. key_points 提取 3-8 条最重要的，每条必须标注真实页码 page（整数，且必须在第 {page_from}-{page_to} 页区间内）。
2. 只有正文中确实出现可量化的数据时才输出 chart_specs，否则返回空数组；不要编造数据——你给出的每个数值都会与原文逐数回验，无法在原文中匹配到的数值会被丢弃。
3. 用中文输出 point 和 definition，evidence 保留原文语言。
4. 若章节信息量小，可以少提取。"""

REDUCE_PROMPT = """你是文献速读助手。下面是文献《{title}》各章节提取的要点 JSON 数组：

{points_json}

请综合成最终速读报告，严格输出 JSON：
{{
  "summary": "全文一段话总结（150字以内）",
  "core_question": "这篇文献要解决的核心问题",
  "rating": 1-5 的整数（学术价值/实用价值）,
  "rating_reason": "评分理由一句话",
  "main_points": [从所有要点中挑出最重要的 5-10 条，沿用原字段结构],
  "limitations": ["主要局限或风险"],
  "cards": [
    {{"front": "问题（根据要点设计的记忆卡问题）", "back": "答案", "tag": "章节名或主题"}}
  ]
}}

cards 要求：基于 main_points 生成 5-12 张记忆卡，问答形式，帮助用户记住核心内容。"""


def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def _verify_page(raw_page, page_from: int, page_to: int):
    """页码范围校验：区间内的页码标记 page_verified=true；
    越界的 clamp 回区间并标记 false；缺失/非整数的退到区间首页并标记 false。"""
    if isinstance(raw_page, bool):
        return page_from, False
    if isinstance(raw_page, float) and raw_page.is_integer():
        raw_page = int(raw_page)
    if isinstance(raw_page, int) and page_from <= raw_page <= page_to:
        return raw_page, True
    if isinstance(raw_page, (int, float)):
        return max(page_from, min(int(raw_page), page_to)), False
    return page_from, False


# ---------------- P0-3：重试 / 降级 / 失败可见 ----------------

RETRYABLE_STATUS = {429, 500, 502, 503}
MAX_ATTEMPTS = 3

# 端点不支持 json_object 模式时，附加在 prompt 末尾的降级说明
DEGRADE_NOTE = "\n\n重要：当前接口不支持结构化 JSON 模式，请务必只输出一个合法 JSON 对象，不要输出任何其他文字、不要用 Markdown 代码块包裹。"


class AnalysisError(Exception):
    """分析管线致命错误（如全部章节提取失败）：任务应置为 failed，而不是展示空壳报告。"""


class ChunkError(Exception):
    """单次 LLM 调用在重试后仍失败。kind=异常类型或 json_parse，attempts=已尝试次数。"""

    def __init__(self, kind: str, attempts: int, snippet: str = ""):
        super().__init__(f"{kind}（已重试 {attempts} 次）: {snippet}")
        self.kind = kind
        self.attempts = attempts
        self.snippet = snippet[:200]


def _status_of(exc: BaseException):
    """从 openai 异常（或其代理包装）上尽力取 HTTP 状态码。"""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status


async def _chat_json(client, model, prompt, json_mode: bool = True,
                     max_attempts: int = MAX_ATTEMPTS) -> dict:
    """调用 LLM 并解析 JSON，带指数退避重试。

    - 429/500/502/503 及连接层错误（无 status）按 2**attempt 秒退避重试；
    - 重试耗尽后抛 ChunkError，绝不静默返回 None；
    - json_mode=False 时不传 response_format（端点不支持时的降级路径），
      靠 prompt 约束 + _extract_json 容错解析（提取第一个 {...} 块）。
    """
    kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": "你是严谨的文献分析助手，只输出合法 JSON。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    last_err: ChunkError | None = None
    for attempt in range(max_attempts):
        try:
            resp = await client.chat.completions.create(**kwargs)
            raw = resp.choices[0].message.content or ""
        except Exception as e:
            status = _status_of(e)
            retryable = status in RETRYABLE_STATUS or status is None
            last_err = ChunkError(type(e).__name__, attempt + 1, str(e))
            if retryable and attempt < max_attempts - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            raise last_err from e
        try:
            return _extract_json(raw)
        except json.JSONDecodeError:
            last_err = ChunkError("json_parse", attempt + 1, raw)
            if attempt < max_attempts - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            raise last_err
    raise last_err  # pragma: no cover


async def _probe_json_mode(client, model) -> bool:
    """任务开始前探测端点是否支持 response_format=json_object。

    返回 400（参数错误）→ 视为不支持，降级为普通模式 + 容错解析；
    其他错误按可重试状态退避重试，最终仍失败则抛出（如 401 鉴权错误必须让用户看见）。
    """
    for attempt in range(MAX_ATTEMPTS):
        try:
            await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "{}"}],
                max_tokens=1,
                response_format={"type": "json_object"},
            )
            return True
        except Exception as e:
            if _status_of(e) == 400:
                return False
            if _status_of(e) in RETRYABLE_STATUS or _status_of(e) is None:
                if attempt < MAX_ATTEMPTS - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
            raise
    return False  # pragma: no cover


async def analyze(doc: dict, api_key: str, base_url: str, model: str,
                  progress_cb=None, error_sink=None, event_cb=None) -> dict:
    """Map-Reduce 分析。api_key='demo' 时返回演示数据。

    error_sink: 可选的可调用对象，接收一行失败日志文本（由调用方决定写盘位置）。
    """
    if api_key == "demo":
        return _demo_report(doc)

    client = AsyncOpenAI(api_key=api_key, base_url=base_url or None)
    title = doc["title"]

    async def report(msg, pct, event_type="progress", payload=None):
        if progress_cb:
            await progress_cb(msg, pct)
        if event_cb:
            data = {"message": msg, "progress": pct}
            if payload:
                data.update(payload)
            await event_cb(event_type, data)

    def log_error(line: str):
        if error_sink is None:
            return
        try:
            error_sink(line)
        except Exception as e:  # 日志写入失败不应拖垮分析主流程
            print(f"[P0-3] errors.log 写入失败：{e}")

    # ---- 能力探测：端点不支持 json_object 时降级（P0-3 第 2 条）----
    json_mode = await _probe_json_mode(client, model)
    degraded = not json_mode
    if degraded:
        log_error("[probe] 端点不支持 response_format=json_object，已降级为普通模式 + 容错解析")

    # ---- Map 阶段：逐 chunk 提取（chunk 自带真实页码区间）----
    chunks = doc.get("chunks") or build_chunks(doc.get("sections", []))

    all_points, all_terms, all_charts, all_flashcards = [], [], [], []
    failed_sections: list[dict] = []
    done = 0

    async def run_one(chunk):
        nonlocal done
        page_from, page_to = chunk["page_from"], chunk["page_to"]
        success_result = None
        prompt = MAP_PROMPT.format(
            title=title, section_title=chunk["section"],
            page_from=page_from, page_to=page_to,
            text=chunk["text"],
        )
        if degraded:
            prompt += DEGRADE_NOTE
        try:
            r = await _chat_json(client, model, prompt, json_mode=json_mode)
        except ChunkError as e:
            # 失败要被看见：收集到 failed_sections + errors.log，绝不静默吞掉
            failed_sections.append({
                "section": chunk["section"],
                "page_from": page_from,
                "page_to": page_to,
                "error_type": e.kind,
                "attempts": e.attempts,
                "detail": e.snippet,
            })
            log_error(f"[map] section={chunk['section']!r} pages={page_from}-{page_to} "
                      f"error={e.kind} attempts={e.attempts} detail={e.snippet!r}")
        else:
            success_result = r
            for p in r.get("key_points", []):
                p["section"] = chunk["section"]
                p["page"], p["page_verified"] = _verify_page(
                    p.get("page"), page_from, page_to)
                p["page"] = max(1, min(p["page"], doc["n_pages"]))
                all_points.append(p)
            all_terms.extend(r.get("terms", []))
            for c in r.get("chart_specs", []):
                if isinstance(c, dict):
                    c.setdefault("source", "text_llm")
                    c.setdefault("page_from", page_from)
                    c.setdefault("page_to", page_to)
                    all_charts.append(c)
            for card in r.get("flashcards", []):
                if isinstance(card, dict):
                    card.setdefault("section", chunk["section"])
                    card.setdefault("page", page_from)
                    card.setdefault("source", "map")
                    all_flashcards.append(card)
        finally:
            # 无论成败都推进真实进度，避免失败章节卡住进度条
            done += 1
            pct = 10 + 60 * done // max(len(chunks), 1)
            if success_result is not None:
                await report(f"提取章节要点 ({done}/{len(chunks)})", pct,
                             event_type="section_done",
                             payload={"section": chunk["section"],
                                      "page_from": page_from, "page_to": page_to,
                                      "points": success_result.get("key_points", [])})
            else:
                await report(f"提取章节要点 ({done}/{len(chunks)})", pct,
                             event_type="failed_section",
                             payload={"section": chunk["section"],
                                      "page_from": page_from, "page_to": page_to})

    # 并发 8 个（DeepSeek 速率足够，可显著缩短 map 阶段墙钟时间）
    sem = asyncio.Semaphore(8)

    async def guarded(c):
        async with sem:
            await run_one(c)

    await asyncio.gather(*(guarded(c) for c in chunks))

    # ---- 空报告拦截（P0-3 第 5 条）：全部章节失败 → 致命错误，不产出空壳报告 ----
    if chunks and not all_points and len(failed_sections) >= len(chunks):
        first = failed_sections[0]
        raise AnalysisError(
            f"全部 {len(chunks)} 个章节提取均失败（首个错误：{first['error_type']} "
            f"{first['detail'][:120]}）。请检查 API Key / Base URL / 模型名是否正确，"
            f"或查看 data/{doc['doc_id']}/errors.log。"
        )

    await report("综合全文要点 + 提取图表（并行）…", 75)

    # ---- Reduce 阶段：失败时保留 map 结果，返回部分报告（P0-3 第 4 条）----
    # ---- 图表：与 reduce 并行跑（两者互不依赖），缩短墙钟时间 ----
    pts = all_points[:80]

    async def _reduce_task():
        try:
            return await _chat_json(
                client, model,
                REDUCE_PROMPT.format(title=title,
                                     points_json=json.dumps(pts, ensure_ascii=False)),
                json_mode=json_mode), None
        except ChunkError as e:
            err = {"stage": "reduce", "error_type": e.kind,
                   "attempts": e.attempts, "detail": e.snippet}
            log_error(f"[reduce] error={e.kind} attempts={e.attempts} detail={e.snippet!r}")
            return {}, err

    async def _vision_task():
        try:
            return await vision_chart_specs(doc, client, model)
        except Exception:
            return []

    (reduce_result, reduce_error), vision_charts = await asyncio.gather(
        _reduce_task(), _vision_task())

    # ---- P2-1：critic 校验环（依赖 reduce 的 main_points，必须在 reduce 之后）----
    await report("校验要点证据…", 85)
    reduce_main_points = reduce_result.get("main_points") or all_points
    main_points_with_critic = await critic_pass(reduce_main_points, chunks, client, model)
    # 统计写入由调用方在 _save_meta 处理；这里保留原始列表用于导出
    critic_stats = {
        "total": len(main_points_with_critic),
        "verified": sum(1 for p in main_points_with_critic if p.get("critic_status") == "verified"),
        "degraded": sum(1 for p in main_points_with_critic if p.get("critic_status") == "degraded"),
        "rejected": sum(1 for p in main_points_with_critic if p.get("critic_status") == "rejected"),
    }

    # ---- 合并图表通道：表格确定性抽取 + 视觉识别兜底 + LLM 提取（P0-2）----
    await report("合并图表数据…", 92)
    pdf_path = str(DATA_DIR / doc["doc_id"] / "source.pdf")
    table_charts = extract_table_charts(pdf_path)
    merged = process_charts(table_charts + vision_charts + all_charts, doc)

    await report("生成完成", 100)

    main_points = main_points_with_critic

    # 双保险：reduce 成功后仍无 main_points 且失败章节覆盖全部 → 同样判 failed
    if not main_points and chunks and len(failed_sections) >= len(chunks):
        raise AnalysisError(
            f"全部 {len(chunks)} 个章节提取均失败，无法生成报告。"
            f"详见 data/{doc['doc_id']}/errors.log。"
        )

    return {
        "doc_id": doc["doc_id"],
        "title": title,
        "author": doc.get("author", ""),
        "n_pages": doc["n_pages"],
        "summary": reduce_result.get("summary") or (
            "（全文综合阶段失败，以下为按章节提取的原始要点汇总。）"
            if reduce_error else ""),
        "core_question": reduce_result.get("core_question", ""),
        "rating": reduce_result.get("rating", 3),
        "rating_reason": reduce_result.get("rating_reason", ""),
        "main_points": main_points,
        "all_points": all_points,
        "limitations": reduce_result.get("limitations", []),
        "terms": all_terms[:30],
        "charts": merged["charts"],
        "chart_unverified": merged["unverified"],
        "chart_stats": merged["stats"],
        "critic_stats": critic_stats,
        "cards": _dedupe_cards((reduce_result.get("cards") or []) + all_flashcards),
        "sections": [{"title": s["title"], "page_start": s["page_start"],
                      "page_end": s["page_end"]} for s in doc["sections"]],
        # P0-3 新增：失败可见 / 降级标记
        "failed_sections": failed_sections,
        "partial": bool(reduce_error),
        "reduce_error": reduce_error,
        "degraded": degraded,
    }


def _demo_report(doc: dict) -> dict:
    """无 API key 时的演示数据，用于预览界面。"""
    secs = doc["sections"] or [{"title": "全文", "page_start": 1, "page_end": doc["n_pages"]}]
    pts = []
    for i, s in enumerate(secs[:8]):
        pts.append({
            "point": f"【演示】{s['title']}：这里是该章节的核心要点示例，接入大模型后将生成真实内容。",
            "type": ["conclusion", "method", "data", "innovation"][i % 4],
            "evidence": "demo evidence",
            "page": s["page_start"], "confidence": 0.9, "section": s["title"],
            "page_verified": True,
        })
    return {
        "doc_id": doc["doc_id"], "title": doc["title"], "author": doc.get("author", ""),
        "n_pages": doc["n_pages"],
        "summary": "【演示模式】这是示例总结。在右上角设置中填入 OpenAI 兼容的 API Key 后，将生成真实速读报告。",
        "core_question": "（演示）核心问题占位",
        "rating": 4, "rating_reason": "演示评分",
        "main_points": pts[:6], "all_points": pts,
        "limitations": ["演示局限 1"],
        "terms": [{"term": "演示术语", "definition": "接入模型后自动生成术语表"}],
        "charts": [{
            "chart_id": "ch_demo0001",
            "title": "演示图表（真实数据来自文献中的量化结果）",
            "chart_type": "bar", "labels": ["方法A", "方法B", "本文方法"],
            "values": [72, 81, 93], "unit": "%", "note": "示例数据",
            "source": "demo", "verified": True,
        }],
        "chart_unverified": [],
        "chart_stats": {"table_extract": 0, "vision": 0, "vision_pass_rate": None},
        "cards": [
            {"front": "【演示】这篇文献的核心问题是什么？", "back": "接入模型后自动生成记忆卡。", "tag": "演示", "source": "reduce"},
            {"front": "【演示】主要方法的关键创新？", "back": "示例答案。", "tag": "演示", "source": "reduce"},
            {"front": "【演示-map】某个章节的核心概念？", "back": "map 阶段示例卡。", "tag": "演示", "source": "map"},
        ],
        "sections": [{"title": s["title"], "page_start": s["page_start"],
                      "page_end": s["page_end"]} for s in secs],
        "failed_sections": [], "partial": False,
        "reduce_error": None, "degraded": False,
        "critic_stats": {"total": 0, "verified": 0, "degraded": 0, "rejected": 0},
    }
