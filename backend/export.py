"""导出：Markdown 报告 / Anki CSV / JSON"""
import json
import csv
import io

TYPE_LABEL = {
    "conclusion": "结论", "method": "方法", "data": "数据",
    "limitation": "局限", "innovation": "创新", "background": "背景",
}


def to_markdown(r: dict) -> str:
    L = []
    L.append(f"# {r['title']}")
    if r.get("author"):
        L.append(f"> 作者：{r['author']}  |  {r['n_pages']} 页")
    L.append("")
    L.append(f"**价值评分**：{int(r.get('rating', 0))}/5（{r.get('rating_reason', '')}）")
    L.append("")
    L.append("## 核心问题")
    L.append(r.get("core_question", ""))
    L.append("")
    L.append("## 一段话总结")
    L.append(r.get("summary", ""))
    L.append("")
    L.append("## 主要要点")
    for p in r.get("main_points", []):
        t = TYPE_LABEL.get(p.get("type", ""), p.get("type", ""))
        L.append(f"- **[{t}]** {p.get('point', '')}（第 {p.get('page', '?')} 页）")
        if p.get("evidence"):
            L.append(f"  > 原文：{p['evidence']}")
    L.append("")
    if r.get("limitations"):
        L.append("## 局限与风险")
        for x in r["limitations"]:
            L.append(f"- {x}")
        L.append("")
    if r.get("terms"):
        L.append("## 术语表")
        L.append("| 术语 | 解释 |")
        L.append("|---|---|")
        for t in r["terms"]:
            L.append(f"| {t.get('term','')} | {t.get('definition','')} |")
        L.append("")
    if r.get("charts"):
        L.append("## 数据图表")
        src_label = {"table_extract": "表格抽取", "vision": "视觉识别",
                     "text_llm": "文本提取", "demo": "演示"}
        for c in r["charts"]:
            src = src_label.get(c.get("source"), c.get("source", ""))
            page = (f"第 {c['page_from']} 页" if c.get("page_from")
                    else c.get("note", ""))
            L.append(f"### {c.get('title','')}（{page}）")
            L.append(f"- 类型：{c.get('chart_type')} | 单位：{c.get('unit','')}"
                     f" | 来源：{src} | 数值回验："
                     f"{'通过' if c.get('verified') else '未完全通过（见附录）'}")
            for lab, val in zip(c.get("labels", []), c.get("values", [])):
                L.append(f"- {lab}: {val}")
            L.append("")
    if r.get("chart_unverified"):
        L.append("## 未核验数据（未渲染进图表，仅供参照）")
        for u in r["chart_unverified"]:
            L.append(f"### {u.get('title','')}（来源：{u.get('source','?')}，"
                     f"{u.get('reason','')}）")
            for it in u.get("items", []):
                L.append(f"- {it.get('label')}: {it.get('value')}")
            L.append("")
    if r.get("cards"):
        L.append("## 知识卡片")
        for c in r["cards"]:
            L.append(f"- **Q: {c.get('front','')}**")
            L.append(f"  A: {c.get('back','')}")
        L.append("")
    return "\n".join(L)


def to_cards_csv(r: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Front", "Back", "Tags"])
    for c in r.get("cards", []):
        w.writerow([c.get("front", ""), c.get("back", ""), c.get("tag", "文献速读")])
    return buf.getvalue()


def to_json(r: dict) -> str:
    return json.dumps(r, ensure_ascii=False, indent=2)
