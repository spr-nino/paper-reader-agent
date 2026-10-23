"""P2-1：critic 校验环。

在 reduce 之后对每条 main_point 做核验：
1. 页码合法性（程序判）
2. evidence 在原文 chunk 中是否出现（difflib / rapidfuzz 近似匹配，程序判）
3. 要点中的数字是否出现在 evidence 中（程序判）
4. 结论是否超出 evidence 可支撑范围（LLM 判，仅对程序存疑项）

输出：main_points 增加 critic_status / critic_reason 字段。
"""
import re
import difflib
from typing import Optional


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s)).lower()


def _to_float(s) -> float | None:
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


def _extract_numbers(text: str) -> list[float]:
    nums = []
    for m in re.finditer(r"[-+]?\d[\d,]*\.?\d*(?:[eE][-+]?\d+)?", text or ""):
        v = _to_float(m.group(0))
        if v is not None:
            nums.append(v)
    return nums


def _evidence_exists(evidence: str, chunk_text: str, threshold: float = 0.75) -> bool:
    """evidence 是否在 chunk 文本中有近似匹配（防截断 / 小差异）。"""
    if not evidence or not chunk_text:
        return False
    ev_norm = _norm(evidence)
    text_norm = _norm(chunk_text)
    if ev_norm in text_norm:
        return True
    # 按句子拆分，找最相似的
    sentences = re.split(r"[.!?。！？]", chunk_text)
    best = 0.0
    for sent in sentences:
        ratio = difflib.SequenceMatcher(None, ev_norm, _norm(sent)).ratio()
        if ratio > best:
            best = ratio
    return best >= threshold


def _numbers_consistent(point_text: str, evidence: str) -> bool:
    """要点中提到的数字是否也在 evidence 中（允许 ±5% 容差）。"""
    point_nums = _extract_numbers(point_text)
    if not point_nums:
        return True
    evi_nums = _extract_numbers(evidence)
    if not evi_nums:
        return False
    evi_set = set(evi_nums)
    for pn in point_nums:
        match = any(
            (en != 0 and abs(pn - en) / abs(en) <= 0.05) or (en == 0 and pn == 0)
            for en in evi_set
        )
        if not match:
            return False
    return True


def _find_chunk_for_point(point: dict, chunks: list) -> Optional[dict]:
    """根据页码区间找到 point 对应的 chunk。"""
    page = point.get("page")
    if page is None:
        return None
    for c in chunks:
        pf = c.get("page_from", 1)
        pt = c.get("page_to", pf)
        if pf <= page <= pt:
            return c
    return None


async def critic_pass(main_points: list, chunks: list, client=None, model: str = "") -> list:
    """对 main_points 逐项核验，返回新增 critic_status / critic_reason 的列表。"""
    out = []
    for p in main_points:
        status = "verified"
        reasons = []

        page = p.get("page")
        chunk = _find_chunk_for_point(p, chunks) if page else None
        chunk_text = chunk.get("text", "") if chunk else ""

        # 1. 页码合法性：直接按真实 chunk 区间核验（不再信任 reduce 写入的 page_verified 字段，
        #    避免被改写/丢失该字段的要点被全量误标 degraded，导致核验体系自我贬值）
        if chunk is None:
            status = "degraded"
            if page is None:
                reasons.append("缺少页码，无法定位原文")
            else:
                reasons.append(f"页码 {page} 不在任何 chunk 的真实页码区间内，无法核验")

        # 2. evidence 存在性
        evidence = p.get("evidence", "")
        if evidence and chunk_text and not _evidence_exists(evidence, chunk_text):
            # 可能是 reduce 改写导致 evidence 未原样出现，尝试在 point 中匹配
            if not _evidence_exists(evidence, p.get("point", "")):
                status = "rejected" if status == "verified" else status
                reasons.append("evidence 在原文中找不到近似匹配")

        # 3. 数值一致性
        if not _numbers_consistent(p.get("point", ""), evidence):
            status = "rejected" if status == "verified" else status
            reasons.append("要点中的数字在 evidence 中未匹配")

        # 4. LLM 语义幻觉检查（仅对程序已存疑的项，节省 token）
        if client and model and status != "verified":
            prompt = f"""你是一位严格的审稿人。请判断以下文献要点是否超出其证据所能支撑的范围。

要点：{p.get('point', '')}
Evidence 摘录：{evidence}
Evidence 所在原文段落：{chunk_text[:1200]}

请只回答一个字："是"（存在幻觉/过度推断）或"否"（可被 evidence 支撑）。"""
            try:
                resp = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=2,
                    temperature=0.0,
                )
                ans = resp.choices[0].message.content or ""
                if "是" in ans:
                    status = "rejected"
                    reasons.append("critic 判定存在幻觉/过度推断")
                elif "否" in ans and status == "rejected":
                    # LLM 认为可支撑，降级处理
                    status = "degraded"
            except Exception:
                pass

        p["critic_status"] = status
        p["critic_reason"] = "；".join(reasons) if reasons else ""
        out.append(p)
    return out
