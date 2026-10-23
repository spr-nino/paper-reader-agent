"""W1-F8 验证：critic 按真实 chunk 区间核验，不再依赖 page_verified。

stdlib-only；critic.py 仅依赖 re/difflib/typing，无需 openai 桩。
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import critic

CHUNKS = [
    {"page_from": 1, "page_to": 3, "text": "accuracy 95 percent dataset 1000 images"},
    {"page_from": 4, "page_to": 6, "text": "the model converges after 200 epochs"},
]


def build_points():
    return [
        # 1) 页码在范围内 + evidence 命中 -> verified
        {"point": "Accuracy is 95%", "evidence": "accuracy 95", "page": 2,
         "page_verified": False},  # 故意 page_verified=False，证明不再依赖该字段
        # 2) 页码越界 -> degraded（原逻辑会误标，新逻辑也应 degraded 但理由正确）
        {"point": "Out of range claim", "evidence": "x", "page": 99, "page_verified": True},
        # 3) 缺页码 -> degraded（理由为缺少页码）
        {"point": "No page claim", "evidence": "x", "page": None},
        # 4) 页码在范围内但 evidence 缺失 + 数字不符 -> rejected
        {"point": "foo 123", "evidence": "nonexistent token", "page": 5},
    ]


async def main():
    out = await critic.critic_pass(build_points(), CHUNKS)
    ok = True

    p1 = out[0]
    if p1["critic_status"] != "verified":
        print(f"[FAIL] p1 应为 verified，实际 {p1['critic_status']} / {p1['critic_reason']}")
        ok = False
    else:
        print("[PASS] p1 (page_verified=False 但在范围内) -> verified，不再误标 degraded")

    p2 = out[1]
    if p2["critic_status"] != "degraded" or "不在任何 chunk" not in p2["critic_reason"]:
        print(f"[FAIL] p2 应为 degraded(越界)，实际 {p2['critic_status']} / {p2['critic_reason']}")
        ok = False
    else:
        print(f"[PASS] p2 (页码越界) -> degraded：{p2['critic_reason']}")

    p3 = out[2]
    if p3["critic_status"] != "degraded" or "缺少页码" not in p3["critic_reason"]:
        print(f"[FAIL] p3 应为 degraded(缺页码)，实际 {p3['critic_status']} / {p3['critic_reason']}")
        ok = False
    else:
        print(f"[PASS] p3 (缺页码) -> degraded：{p3['critic_reason']}")

    p4 = out[3]
    if p4["critic_status"] != "rejected":
        print(f"[FAIL] p4 应为 rejected，实际 {p4['critic_status']} / {p4['critic_reason']}")
        ok = False
    else:
        print(f"[PASS] p4 (范围内但 evidence/数字不符) -> rejected：{p4['critic_reason']}")

    print("W1-F8 验证", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
