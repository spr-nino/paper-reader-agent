"""P0-2 单测：表格通道 / 结构校验 / 数值回验 / 合并管线。"""
import io, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fitz  # PyMuPDF

from charts import (_to_float, extract_number_tokens, verify_value,
                    validate_chart_spec, extract_table_charts,
                    process_charts, select_vision_pages)


def make_pdf_with_table(path):
    """合成一个带真实线框表格 + 图表文字提示的 PDF。"""
    doc = fitz.open()
    page = doc.new_page()
    rows = [["Method", "Accuracy (%)"], ["Baseline", "72.5"],
            ["Proposed", "93.1"], ["Ablation", "81.0"]]
    x0, y0, cw, rh = 72, 100, 140, 24
    for ri, row in enumerate(rows):
        for ci, cell in enumerate(row):
            rect = fitz.Rect(x0 + ci * cw, y0 + ri * rh,
                             x0 + (ci + 1) * cw, y0 + (ri + 1) * rh)
            page.draw_rect(rect, color=(0, 0, 0), width=0.8)
            page.insert_text(fitz.Point(rect.x0 + 6, rect.y1 - 8), cell, fontsize=10)
    page.insert_text(fitz.Point(72, 80), "Table 1: Accuracy comparison", fontsize=11)
    page.insert_text(fitz.Point(72, 260), "The model reaches 93.1% accuracy.", fontsize=10)
    # 第 2 页：图提示但无线框表（走视觉通道的页）
    p2 = doc.new_page()
    p2.insert_text(fitz.Point(72, 100), "Figure 2 shows the F1 trend over years.", fontsize=10)
    doc.save(path)
    doc.close()


def test_table_channel():
    tmp = tempfile.mkdtemp()
    pdf = os.path.join(tmp, "t.pdf")
    make_pdf_with_table(pdf)
    specs = extract_table_charts(pdf)
    assert specs, "表格通道应抽到至少一张图表"
    s = specs[0]
    assert s["source"] == "table_extract"
    assert s["chart_type"] == "bar"
    assert len(s["labels"]) == len(s["values"]) == 3
    assert 93.1 in s["values"] and 72.5 in s["values"]
    print("PASS test_table_channel:", s["title"], s["labels"], s["values"])


def test_structure_validation():
    bad_cases = [
        {"chart_type": "bar", "labels": ["a", "b"], "values": [1]},          # 长度不等
        {"chart_type": "bar", "labels": ["a"], "values": [1]},               # 点数不足
        {"chart_type": "radar", "labels": ["a", "b"], "values": [1, 2]},     # 类型越白名单
        {"chart_type": "bar", "labels": ["a", "b"], "values": ["x", "y"]},   # 非数值
        {"chart_type": "bar", "labels": None, "values": [1, 2]},             # labels 缺失
    ]
    for c in bad_cases:
        assert not validate_chart_spec(dict(c)), f"应被丢弃: {c}"
    good = {"chart_type": "bar", "labels": ["a", "b"], "values": [1, "2,345"]}
    assert validate_chart_spec(good)
    assert good["values"] == [1.0, 2345.0]  # 归一化
    print("PASS test_structure_validation")


def test_value_verification():
    tokens = extract_number_tokens("accuracy 93.1%, count 1,234, ratio ±0.05")
    assert verify_value(93.1, tokens)
    assert verify_value(1234, tokens)                       # 逗号归一化
    assert verify_value(0.05, tokens)                       # ± 前缀
    assert verify_value(93.6, tokens, rel_tol=0.01)         # 0.5% 误差内
    assert not verify_value(99.9, tokens, rel_tol=0.01)     # 匹配不到
    assert not verify_value(None, tokens)
    print("PASS test_value_verification")


def test_process_pipeline():
    tmp = tempfile.mkdtemp()
    pdf = os.path.join(tmp, "t.pdf")
    make_pdf_with_table(pdf)

    # 构造伪 doc（模拟 parser 输出）
    doc = fitz.open(pdf)
    pages = [{"page": i + 1, "text": doc[i].get_text("text"),
              "n_images": 0} for i in range(len(doc))]
    doc.close()
    fdoc = {"doc_id": "testdoc", "pages": pages}

    table_charts = extract_table_charts(pdf)
    vision_fake = [{"title": "Vision Chart", "chart_type": "bar",
                    "labels": ["A", "B", "C"],
                    "values": [72.5, 999.9, 81.0],  # 999.9 在原文中不存在
                    "source": "vision", "page_from": 1, "page_to": 1}]
    llm_fake = [{"title": "LLM Chart", "chart_type": "line",
                 "labels": ["x", "y"], "values": [93.1, 0.01],
                 "source": "text_llm", "page_from": 1, "page_to": 2}]
    bad = {"title": "bad", "chart_type": "bar",
           "labels": ["a"], "values": [1, 2]}  # 长度不一致

    out = process_charts(table_charts + vision_fake + llm_fake + [bad], fdoc)
    charts, appendix, stats = out["charts"], out["unverified"], out["stats"]

    # 验收 1：表格型图表 100% 来自 find_tables
    assert all(c["source"] == "table_extract" for c in charts if c["title"].startswith("Table"))
    # 验收 2：结构不合法的被丢弃
    assert stats["dropped_structure"] == 1
    # 验收 3：幻觉数值 999.9 未渲染、进附录
    vis = next((c for c in charts if c["title"] == "Vision Chart"), None)
    assert vis is not None and 999.9 not in vis["values"] and 72.5 in vis["values"]
    assert any(u["title"] == "Vision Chart" and any(i["value"] == 999.9 for i in u["items"])
               for u in appendix)
    # 验收 4：LLM 文本图表 0.01 未匹配 -> 只剩 1 点 -> 整图进附录不渲染
    assert not any(c["title"] == "LLM Chart" for c in charts)
    # 验收 5：回验通过率有统计
    assert stats["vision_values"] == 3 and stats["vision_verified"] == 2
    assert stats["vision_pass_rate"] == round(2 / 3, 4)
    # 验收 6：无长度不一致图表被渲染
    for c in charts:
        assert len(c["labels"]) == len(c["values"]) >= 2
        assert all(isinstance(v, float) for v in c["values"])
    print("PASS test_process_pipeline, stats =", stats)


def test_vision_page_selection():
    doc = {"pages": [
        {"page": 1, "text": "plain text", "n_images": 0},
        {"page": 2, "text": "Figure 3: results", "n_images": 0},
        {"page": 3, "text": "nothing", "n_images": 2},
    ]}
    sel = select_vision_pages(doc, max_pages=5)
    assert sel == [3, 2]  # 有图页优先，其次图提示页
    print("PASS test_vision_page_selection")


if __name__ == "__main__":
    test_table_channel()
    test_structure_validation()
    test_value_verification()
    test_process_pipeline()
    test_vision_page_selection()
    print("\nALL PASS")
