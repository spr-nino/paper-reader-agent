"""端到端：analyze() 全管线（stub LLM）验证 P0-2 图表通道接入。"""
import asyncio, json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fitz

import analyzer
import parser
from parser import parse_pdf

# ---- 造带线框表的测试 PDF，走真实 parse_pdf ----
tmp = tempfile.mkdtemp()
pdf_path = os.path.join(tmp, "paper.pdf")
doc = fitz.open()
page = doc.new_page()
page.insert_text(fitz.Point(72, 60), "1. Experiments", fontsize=14)
rows = [["Method", "Accuracy (%)"], ["Baseline", "72.5"], ["Proposed", "93.1"]]
for ri, row in enumerate(rows):
    for ci, cell in enumerate(row):
        r = fitz.Rect(72 + ci*140, 100 + ri*24, 72 + (ci+1)*140, 100 + (ri+1)*24)
        page.draw_rect(r, color=(0, 0, 0), width=0.8)
        page.insert_text(fitz.Point(r.x0+6, r.y1-8), cell, fontsize=10)
page.insert_text(fitz.Point(72, 80), "Table 1: Accuracy comparison", fontsize=11)
p2 = doc.new_page()
p2.insert_text(fitz.Point(72, 100), "2. Conclusion\nThe proposed method achieves 93.1% accuracy, outperforming 72.5% baseline.", fontsize=10)
doc.save(pdf_path); doc.close()

# data/<doc_id>/source.pdf 供 analyze 定位（隔离到临时目录，不污染真实数据）
doc_id = "e2etest12345"
os.makedirs(os.path.join(tmp, "data", doc_id, "pages"), exist_ok=True)
import shutil; shutil.copy(pdf_path, os.path.join(tmp, "data", doc_id, "source.pdf"))
# F16：统一通过 parser.DATA_DIR 重定向数据目录，使 parse_pdf 与 analyzer 落到同一临时目录
parser.DATA_DIR = Path(os.path.join(tmp, "data"))

parsed = parse_pdf(pdf_path, doc_id)

# ---- stub LLM ----
class FakeMsg:
    def __init__(self, c): self.content = c
class FakeChoice:
    def __init__(self, c): self.message = FakeMsg(c)
class FakeResp:
    def __init__(self, c): self.choices = [FakeChoice(c)]

MAP_OUT = json.dumps({
    "key_points": [{"point": "本文方法达 93.1% 准确率", "type": "data",
                    "evidence": "93.1%", "page": 2, "confidence": 0.95}],
    "terms": [{"term": "Accuracy", "definition": "准确率"}],
    "chart_specs": [{"title": "LLM 文本图表", "chart_type": "bar",
                     "labels": ["Baseline", "Proposed"],
                     "values": [72.5, 93.1], "unit": "%", "note": ""}],
}, ensure_ascii=False)
REDUCE_OUT = json.dumps({"summary": "s", "core_question": "q", "rating": 4,
                         "rating_reason": "r", "main_points": [], "limitations": [],
                         "cards": []}, ensure_ascii=False)

class FakeCompletions:
    async def create(self, model, messages, **kw):
        content = messages[-1]["content"]
        if isinstance(content, list):  # 视觉通道：模拟端点不支持图片 -> 抛错走兜底
            raise RuntimeError("model does not support vision")
        if "综合成最终速读报告" in content:
            return FakeResp(REDUCE_OUT)
        return FakeResp(MAP_OUT)

class FakeClient:
    def __init__(self, *a, **k):
        self.chat = type("X", (), {"completions": FakeCompletions()})()

analyzer.AsyncOpenAI = FakeClient

progress = []
async def _cb(m, p):
    progress.append((m, p))
report = asyncio.run(analyzer.analyze(
    parsed, "fake-key", "", "fake-model", progress_cb=_cb))

charts = report["charts"]
stats = report["chart_stats"]
assert any(c["source"] == "table_extract" for c in charts), "表格通道未接入"
assert all(c["source"] != "vision" for c in charts), "视觉通道应兜底为空"
assert next(c for c in charts if c["source"] == "text_llm")["values"] == [72.5, 93.1], "LLM 数值回验后应保留"
assert stats["table_extract"] >= 1 and stats["text_llm"] == 1
assert all(len(c["labels"]) == len(c["values"]) >= 2 for c in charts)
assert all("chart_id" in c for c in charts)
assert isinstance(report.get("chart_unverified"), list)
msgs = [m for m, _ in progress]
assert any("提取图表数据" in m for m in msgs), "缺图表阶段进度"
print("E2E PASS")
print(" charts:", [(c["title"], c["source"], c["values"]) for c in charts])
print(" stats:", stats)
print(" unverified:", report["chart_unverified"])
