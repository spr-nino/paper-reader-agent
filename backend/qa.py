"""P2-3：基于已提取文本的 Q&A、多篇对比、主题综述。

- Q&A：对 chunks 做 BM25/关键词检索，top-k chunk + 问题送 LLM，要求引用页码。
- 多篇对比：输入多个 doc_id，reduce 阶段换对比 prompt，输出对比矩阵。
- 主题综述：在对比基础上综合归纳共识、分歧、研究空白，每个论点带 doc_id + 页码溯源。
"""
import json
import math
import re
from typing import Optional

from openai import AsyncOpenAI
import parser  # F16：统一引用 parser.DATA_DIR，使 --data-dir 配置对 Q&A / 对比 / 综述生效


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9]+", text.lower()) + list(re.findall(r"[\u4e00-\u9fff]", text))


def _bm25_score(query: str, docs: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    """简单 BM25 打分，返回与 docs 等长的分数列表。"""
    tokenized = [_tokenize(d) for d in docs]
    doc_len = [len(t) for t in tokenized]
    avg_len = sum(doc_len) / max(len(doc_len), 1)
    q_tokens = _tokenize(query)
    scores = [0.0] * len(docs)
    for qt in q_tokens:
        df = sum(1 for t in tokenized if qt in t)
        idf = math.log((len(docs) - df + 0.5) / (df + 0.5) + 1)
        for i, t in enumerate(tokenized):
            tf = t.count(qt)
            denom = tf + k1 * (1 - b + b * doc_len[i] / avg_len)
            scores[i] += idf * (tf * (k1 + 1)) / denom
    return scores


def _load_doc_data(doc_id: str) -> Optional[dict]:
    d = parser.DATA_DIR / doc_id
    if not d.exists():
        return None
    try:
        chunks = json.loads((d / "chunks.json").read_text(encoding="utf-8"))
    except Exception:
        chunks = []
    try:
        report = json.loads((d / "report.json").read_text(encoding="utf-8"))
    except Exception:
        report = {}
    try:
        doc = json.loads((d / "doc.json").read_text(encoding="utf-8"))
    except Exception:
        doc = {}
    return {"doc_id": doc_id, "chunks": chunks, "report": report, "doc": doc}


async def ask(doc_id: str, question: str, api_key: str, base_url: str, model: str, top_k: int = 5) -> dict:
    data = _load_doc_data(doc_id)
    if not data:
        return {"error": "not_found", "message": "文档不存在"}
    chunks = data["chunks"]
    if not chunks:
        return {"error": "empty", "message": "无可用文本"}
    texts = [c.get("text", "") for c in chunks]
    scores = _bm25_score(question, texts)
    ranked = sorted(zip(scores, chunks), key=lambda x: x[0], reverse=True)[:top_k]
    selected = [c for _, c in ranked]
    context = "\n\n".join(
        f"【第 {c.get('page_from')}-{c.get('page_to')} 页】{c.get('text', '')[:1200]}"
        for c in selected
    )
    prompt = f"""你基于以下文献片段回答问题。要求：
1. 每条结论必须标注原文页码；
2. 如果片段中没有足够信息，明确说明"无法从原文中确认"；
3. 不要编造原文中没有的信息。

文献标题：{data['report'].get('title') or data['doc'].get('title', '')}

相关片段：
{context}

问题：{question}

请输出 JSON：
{{
  "answer": "综合回答（每条结论后标注 '(第 X 页)'）",
  "citations": [
    {{"claim": "结论", "page": 页码, "evidence": "原文摘录"}}
  ]
}}"""
    client = AsyncOpenAI(api_key=api_key, base_url=base_url or None)
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        raw = resp.choices[0].message.content or "{}"
        ans = json.loads(raw)
    except Exception as e:
        return {"error": "llm_error", "message": str(e)}
    return {
        "doc_id": doc_id,
        "question": question,
        "answer": ans.get("answer", ""),
        "citations": ans.get("citations", []),
        "chunks_used": [{"page_from": c.get("page_from"), "page_to": c.get("page_to"),
                         "section": c.get("section")} for c in selected],
    }


async def compare(doc_ids: list[str], api_key: str, base_url: str, model: str) -> dict:
    docs = []
    for did in doc_ids:
        data = _load_doc_data(did)
        if data:
            docs.append(data)
    if not docs:
        return {"error": "not_found", "message": "无有效文档"}
    summaries = []
    for d in docs:
        r = d["report"]
        summaries.append(
            f"doc_id={d['doc_id']}\n"
            f"标题：{r.get('title', '')}\n"
            f"核心问题：{r.get('core_question', '')}\n"
            f"方法：{'；'.join(p.get('point','') for p in r.get('main_points',[]) if p.get('type')=='method')}\n"
            f"数据/结论：{'；'.join(p.get('point','') for p in r.get('main_points',[]) if p.get('type')=='data')}\n"
            f"局限：{'；'.join(r.get('limitations', []))}"
        )
    prompt = f"""请对以下文献进行并排对比，输出 JSON：
{{
  "matrix": [
    {{"dimension": "方法", "{docs[0]['doc_id']}": "...", "{docs[1]['doc_id']}": "..."}}
  ],
  "synthesis": "简短综合",
  "common": ["共同点"],
  "gaps": ["研究空白"]
}}

{chr(10).join(summaries)}"""
    client = AsyncOpenAI(api_key=api_key, base_url=base_url or None)
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        raw = resp.choices[0].message.content or "{}"
        result = json.loads(raw)
    except Exception as e:
        return {"error": "llm_error", "message": str(e)}
    result["doc_ids"] = [d["doc_id"] for d in docs]
    return result


async def review(doc_ids: list[str], api_key: str, base_url: str, model: str) -> dict:
    comp = await compare(doc_ids, api_key, base_url, model)
    if "error" in comp:
        return comp
    prompt = f"""基于以下对比结果，撰写一段主题综述草稿。要求：
1. 归纳共识、分歧、研究空白；
2. 每个论点必须带 doc_id + 页码溯源；
3. 不要编造未提供的信息。

对比结果：
{json.dumps(comp, ensure_ascii=False)}

请输出 JSON：
{{
  "review": "综述正文（每句后标注来源如 doc_id 第 X 页）",
  "claims": [
    {{"claim": "论点", "sources": [{{"doc_id": "...", "page": 1}}]}}
  ]
}}"""
    client = AsyncOpenAI(api_key=api_key, base_url=base_url or None)
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        raw = resp.choices[0].message.content or "{}"
        result = json.loads(raw)
    except Exception as e:
        return {"error": "llm_error", "message": str(e)}
    result["doc_ids"] = comp.get("doc_ids", [])
    return result
