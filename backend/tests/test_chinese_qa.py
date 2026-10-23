"""W0 验收：中文路径端到端断言（锐评v2.md F5/F6/F7 remediation）。

直接 import 真实模块 qa / anki_export 的纯函数进行断言；缺失 openai/fitz 时打桩，
便于在无依赖环境中离线运行（纯函数本身不依赖这些库）。

- 断言①：中文文本 BM25 top-1 命中含关键词的 chunk（F5 修复后中文分词真正生效）。
- 断言②：含空白/大小写差异的卡片 front 归一化后去重生效（F7 _norm_q 生效）。
- 附带：长卡按句读拆分（F7 修复后 _long_card_split 真正触发）。
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 离线打桩：本测试只用纯函数，缺失依赖时桩入以便直接 import 真实模块
for _mod in ("openai", "fitz"):
    try:
        __import__(_mod)
    except ImportError:
        m = types.ModuleType(_mod)
        if _mod == "openai":
            m.AsyncOpenAI = object
        elif _mod == "fitz":
            # parser.py 在导入期用 fitz.Document 作类型标注，桩需提供该属性
            m.Document = object
            m.open = lambda *a, **k: None
            m.Point = object
            m.Rect = object
        sys.modules[_mod] = m

from qa import _tokenize, _bm25_score
from anki_export import _dedupe_cards, _long_card_split


def test_chinese_bm25_top1():
    """断言①：中文关键词检索 top-1 命中正确 chunk。"""
    chunks = [
        "本文提出了一种基于卷积神经网络的分类结构，在图像任务上取得了提升。",
        "实验在 ImageNet 数据集上进行，对比了多种基线方法。",
        "我们提出的注意力机制在准确率上显著优于传统卷积网络，起到关键作用。",
    ]
    query = "注意力机制"
    scores = _bm25_score(query, chunks)
    top_idx = max(range(len(scores)), key=lambda i: scores[i])
    assert scores[2] > 0, "中文关键词 score 必须为正数（F5 中文分词必须生效）"
    assert top_idx == 2, f"top-1 应为第 3 个 chunk，实际 {top_idx}，分数 {scores}"


def test_card_front_dedup_whitespace():
    """断言②：含空白/大小写差异的卡片 front 归一化后去重生效。"""
    cards = [
        {"front": "什么是注意力机制？", "back": "一种让模型聚焦关键特征的机制。"},
        {"front": " 什么是 注意力机制 ？ ", "back": "更长的解释：注意力机制使模型动态加权输入特征。"},
        {"front": "什么是注意力机制？", "back": "短答案"},  # 与第1张同 front，back 更短 -> 应保留第1张
    ]
    deduped = _dedupe_cards(cards)
    assert len(deduped) == 1, f"应去重为 1 张，实际 {len(deduped)}"
    assert "更长的解释" in deduped[0]["back"], "冲突时应保留 back 更完整的卡片"


def test_long_card_split_fires():
    """F7：长卡背面超过 200 字应按句读拆分（修复前因双反斜杠永不触发）。"""
    long_back = ("第一点结论详细解释说明。 第二点结论补充说明内容。 第三点进一步阐述。 " * 10)
    assert len(long_back) > 200, "测试用长卡背面须超过 200 字以触发拆分"
    card = {
        "front": "复杂概念",
        "back": long_back,
        "page": 3,
        "section": "方法",
    }
    parts = _long_card_split(card)
    assert len(parts) >= 2, f"长卡应按句读拆分，实际 {parts}"


if __name__ == "__main__":
    test_chinese_bm25_top1()
    test_card_front_dedup_whitespace()
    test_long_card_split_fires()
    print("test_chinese_qa PASS")
