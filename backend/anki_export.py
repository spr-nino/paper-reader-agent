"""P2-4：生成原生 Anki .apkg 包。

使用 genanki（纯 Python，无需 Anki 安装）。每张卡片背面支持 HTML、页码溯源链接。
"""
import uuid
import re
import hashlib
from pathlib import Path

try:
    import genanki
except ImportError:  # pragma: no cover
    genanki = None


def _norm_q(s: str) -> str:
    return re.sub(r"\s+", "", str(s)).lower()


def _dedupe_cards(cards: list[dict]) -> list[dict]:
    """按正面问题归一化后去重；背面冲突时保留较长/较完整答案。"""
    seen = {}
    for c in cards:
        front = c.get("front", "").strip()
        key = _norm_q(front)
        if not key:
            continue
        if key not in seen:
            seen[key] = c
        else:
            old = seen[key]
            if len(c.get("back", "")) > len(old.get("back", "")):
                seen[key] = c
    return list(seen.values())


def _long_card_split(card: dict) -> list[dict]:
    """背面超过 200 字尝试拆分；拆不了则标记长答案卡。"""
    back = card.get("back", "")
    if len(back) <= 200:
        return [card]
    # 尝试按句号/换行拆分（F7：修正双反斜杠，使长卡拆分真正触发）
    parts = re.split(r"(?<=[。！？.!?])\s+|\n", back)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) >= 2:
        return [
            {"front": f"{card['front']}（{i+1}/{len(parts)}）",
             "back": part,
             "page": card.get("page"),
             "section": card.get("section", ""),
             "source": card.get("source", "")}
            for i, part in enumerate(parts)
        ]
    card["front"] = "【长答案】" + card["front"]
    return [card]


def build_apkg(report: dict, doc_id: str, output_path: Path) -> Path:
    """从 report 的 cards 生成 .apkg，写入 output_path。"""
    if genanki is None:
        raise RuntimeError("genanki 未安装，无法生成 .apkg")

    raw = report.get("cards") or []
    deduped = _dedupe_cards(raw)
    final = []
    for c in deduped:
        final.extend(_long_card_split(c))

    # F17：用 md5 派生稳定 model/deck id，避免 Python 进程间 hash 随机导致重复牌组
    model_id = int(hashlib.md5(f"speedreader-{doc_id}".encode()).hexdigest(), 16) % (10**10)
    model = genanki.Model(
        model_id,
        "文献速读卡片模型",
        fields=[
            {"name": "Front"},
            {"name": "Back"},
            {"name": "Tags"},
        ],
        templates=[
            {
                "name": "Card 1",
                "qfmt": "{{Front}}",
                "afmt": "{{Back}}<hr><small>{{Tags}}</small>",
            }
        ],
        css="""
        .card { font-family: "PingFang SC", "Microsoft YaHei", sans-serif; font-size: 18px; }
        """,
    )

    deck_id = int(hashlib.md5(f"deck-{doc_id}".encode()).hexdigest(), 16) % (10**10)
    deck = genanki.Deck(deck_id, report.get("title", "文献速读卡片"))

    for c in final:
        front = c.get("front", "")
        back = c.get("back", "")
        page = c.get("page")
        section = c.get("section", "")
        # F7：修正双反斜杠，使章节名空白符正确替换为下划线 tag
        tags = [re.sub(r"\s+", "_", section)[:30]] if section else []
        if page:
            back += f"<br><br><small>来源：{section} · 第 {page} 页</small>"
        note = genanki.Note(model=model, fields=[front, back, " ".join(tags)], tags=tags)
        deck.add_note(note)

    package = genanki.Package(deck)
    package.write_to_file(str(output_path))
    return output_path
