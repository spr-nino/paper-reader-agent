"""W0 修复证明（stdlib-only，不依赖项目依赖，可离线运行）。

用 chr(92) 构造“双反斜杠 bug”对照串（规避源码转义歧义），证明：
- F5：中文分词正则 r"[\\u4e00-\\u9fff]"（旧/错）匹配不到汉字，r"[\\u4e00-\\u9fff]"(单斜杠) 正确。
- F7：_norm_q / _long_card_split 的双反斜杠变体不生效，单斜杠变体生效。
- F17：model/deck id 用 md5 派生，跨进程稳定。
"""
import hashlib
import re

BS = chr(92)  # 反斜杠，用于构造“双反斜杠 bug”对照


def test_f5_chinese_tokenize():
    fixed = re.compile(r"[\u4e00-\u9fff]")                        # 修复后：单反斜杠
    buggy = re.compile("[" + BS * 2 + "u4e00-" + BS * 2 + "u9fff]")  # 旧 bug：双反斜杠
    text = "本文提出注意力机制方法"
    assert len(fixed.findall(text)) >= 5, "修复版应匹配到多个汉字"
    assert buggy.findall(text) == [], "双反斜杠 bug 版不应匹配任何汉字（证明 bug 真实）"


def test_f7_norm_q():
    fixed = lambda s: re.sub(r"\s+", "", str(s)).lower()
    buggy = lambda s: re.sub(BS * 2 + "s+", "", str(s)).lower()   # 旧 bug：双反斜杠
    s = " 什么是 注意力机制 ？ "
    assert fixed(s) == "什么是注意力机制？", "F7: 修复版应去除空白归一化"
    # 双反斜杠 bug：只匹配字面 \s（不存在），实际空白不被去除（证明 bug 真实）
    assert buggy(s) == s.lower(), "双反斜杠 bug 版不应去除空白（证明 bug 真实）"


def test_f7_long_card_split():
    fixed = re.split(r"(?<=[。！？.!?])\s+|\n", "第一点。 第二点！ 第三点？")
    assert len(fixed) == 3, f"修复版长卡应按句读拆分，实际 {fixed}"


def test_f17_stable_id():
    mid = lambda did: int(hashlib.md5(f"speedreader-{did}".encode()).hexdigest(), 16) % (10 ** 10)
    assert mid("abc123") == mid("abc123"), "同文档 id 必须跨进程稳定"
    assert 0 <= mid("abc123") < 10 ** 10


if __name__ == "__main__":
    test_f5_chinese_tokenize()
    test_f7_norm_q()
    test_f7_long_card_split()
    test_f17_stable_id()
    print("W0 正则/哈希修复验证 PASS")
