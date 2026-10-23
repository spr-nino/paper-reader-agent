"""P0-3 验收测试：重试 / 降级 / 失败可见 / reduce 兜底 / 空报告拦截。

全部使用假 client，无网络依赖：python -m unittest test_analyzer_p0_3 -v
"""
import json
import unittest
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace

import analyzer
from analyzer import (_chat_json, _probe_json_mode, analyze,
                      AnalysisError, ChunkError, MAX_ATTEMPTS)


class FakeApiError(Exception):
    """模拟 openai.APIStatusError：带 status_code 属性即可。"""

    def __init__(self, status_code, msg="boom"):
        super().__init__(msg)
        self.status_code = status_code


def ok_resp(payload: dict):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False)))])


class FakeCompletions:
    """按脚本依次返回结果；脚本是 callable 则每次调用它。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.kwargs_log = []

    async def create(self, **kwargs):
        self.kwargs_log.append(kwargs)
        self.calls += 1
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if callable(item):
            return item(kwargs)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, script):
        self.chat = SimpleNamespace(completions=FakeCompletions(script))


def make_doc(n_chunks=2):
    chunks = [{
        "section": f"sec{i}", "text": f"正文内容 {i} " * 50,
        "page_from": i + 1, "page_to": i + 1,
    } for i in range(n_chunks)]
    return {
        "doc_id": "testdoc001", "title": "测试文献", "author": "",
        "n_pages": n_chunks,
        "sections": [{"title": c["section"], "page_start": c["page_from"],
                      "page_end": c["page_to"], "level": 1} for c in chunks],
        "chunks": chunks,
    }



def route_by_prompt(kwargs, map_fail_sections=(), reduce_outcome=None):
    """按 prompt 内容路由响应，与重试次数/并发顺序解耦。

    - max_tokens==1（探针）→ 返回 {}
    - 包含"综合成最终速读报告"（reduce）→ reduce_outcome
    - 章节 prompt 中命中 map_fail_sections 的章节名 → 抛 FakeApiError(500)
    - 其余 → MAP_OK
    """
    if kwargs.get("max_tokens") == 1:
        return ok_resp({})
    prompt = kwargs["messages"][-1]["content"]
    if "综合成最终速读报告" in prompt:
        if isinstance(reduce_outcome, Exception):
            raise reduce_outcome
        return ok_resp(reduce_outcome or REDUCE_OK)
    for name in map_fail_sections:
        if f"章节：{name}" in prompt:
            raise FakeApiError(500)
    return ok_resp(MAP_OK)


def run_analyze_with(responder, doc=None):
    """同步包装：用假 client 跑 analyze，返回 (report, logs, client)。"""
    import asyncio
    client = FakeClient([responder])
    logs = []
    real = analyzer.AsyncOpenAI
    analyzer.AsyncOpenAI = lambda **kw: client
    try:
        rep = asyncio.run(analyze(doc or make_doc(), "sk-test", "", "m",
                                  error_sink=logs.append))
    finally:
        analyzer.AsyncOpenAI = real
    return rep, logs, client


MAP_OK = {"key_points": [{"point": "要点A", "type": "conclusion",
                          "evidence": "e", "page": 1, "confidence": 0.9}],
          "terms": [], "chart_specs": []}
REDUCE_OK = {"summary": "总结", "core_question": "问题", "rating": 4,
             "rating_reason": "理由", "main_points": [{"point": "要点A", "type": "conclusion",
             "evidence": "e", "page": 1, "confidence": 0.9}],
             "limitations": [], "cards": []}


class TestRetry(unittest.IsolatedAsyncioTestCase):
    async def test_429_then_success(self):
        client = FakeClient([FakeApiError(429), lambda *a, **kw: ok_resp(MAP_OK)])
        with patch("analyzer.asyncio.sleep", new=AsyncMock()):
            r = await _chat_json(client, "m", "p")
        self.assertEqual(r["key_points"][0]["point"], "要点A")
        self.assertEqual(client.chat.completions.calls, 2)  # 重试生效

    async def test_500_then_success(self):
        client = FakeClient([FakeApiError(500), FakeApiError(500), lambda *a, **kw: ok_resp(MAP_OK)])
        with patch("analyzer.asyncio.sleep", new=AsyncMock()):
            r = await _chat_json(client, "m", "p")
        self.assertEqual(client.chat.completions.calls, 3)

    async def test_retry_exhausted_raises_chunk_error(self):
        client = FakeClient([FakeApiError(500)])
        with patch("analyzer.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(ChunkError) as ctx:
                await _chat_json(client, "m", "p")
        self.assertEqual(ctx.exception.attempts, MAX_ATTEMPTS)
        self.assertEqual(ctx.exception.kind, "FakeApiError")

    async def test_non_retryable_400_raises_immediately(self):
        client = FakeClient([FakeApiError(400)])
        with self.assertRaises(ChunkError):
            await _chat_json(client, "m", "p")
        self.assertEqual(client.chat.completions.calls, 1)

    async def test_tolerant_parse_with_surrounding_text(self):
        payload = json.dumps(MAP_OK, ensure_ascii=False)
        raw = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=f"好的，结果如下：\n{payload}\n以上。"))])
        client = FakeClient([raw])
        r = await _chat_json(client, "m", "p", json_mode=False)
        self.assertEqual(r["key_points"][0]["point"], "要点A")

    async def test_unparseable_raises_json_parse_error(self):
        raw = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="完全不是 JSON"))])
        client = FakeClient([raw])
        with patch("analyzer.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(ChunkError) as ctx:
                await _chat_json(client, "m", "p", json_mode=False)
        self.assertEqual(ctx.exception.kind, "json_parse")


class TestProbe(unittest.IsolatedAsyncioTestCase):
    async def test_probe_unsupported_400_degrades(self):
        client = FakeClient([FakeApiError(400)])
        self.assertFalse(await _probe_json_mode(client, "m"))

    async def test_probe_supported(self):
        client = FakeClient([ok_resp({})])
        self.assertTrue(await _probe_json_mode(client, "m"))


class TestAnalyze(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sleep_patch = patch("analyzer.asyncio.sleep", new=AsyncMock())
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def test_degraded_mode_full_run(self):
        """mock 端点不支持 json_object：全程降级，报告非空且 degraded=true。"""

        def responder(kwargs):
            if kwargs.get("max_tokens") == 1:
                raise FakeApiError(400)  # 探针：不支持 json_object
            if "综合成最终速读报告" in kwargs["messages"][-1]["content"]:
                return ok_resp(REDUCE_OK)
            return ok_resp(MAP_OK)

        rep, logs, client = run_analyze_with(responder)
        self.assertTrue(rep["degraded"])
        self.assertFalse(rep["partial"])
        self.assertTrue(rep["main_points"])
        self.assertEqual(rep["failed_sections"], [])
        # 降级后所有正式调用都不应再带 response_format
        real_calls = [k for k in client.chat.completions.kwargs_log
                      if k.get("max_tokens") != 1]
        self.assertTrue(all("response_format" not in k for k in real_calls))

    def test_one_section_fails_others_intact(self):
        """一个章节持续 500：failed_sections 记录详情，其余章节完整，任务仍成功。"""

        def responder(kwargs):
            return route_by_prompt(kwargs, map_fail_sections=("sec1",))

        rep, logs, client = run_analyze_with(responder)
        self.assertEqual(len(rep["failed_sections"]), 1)
        f = rep["failed_sections"][0]
        self.assertEqual(f["section"], "sec1")
        self.assertEqual(f["error_type"], "FakeApiError")
        self.assertEqual(f["attempts"], MAX_ATTEMPTS)
        self.assertTrue(any("sec1" in line for line in logs))  # errors.log 有记录
        self.assertTrue(rep["main_points"])                   # 其余章节完整
        self.assertFalse(rep["partial"])

    def test_all_sections_fail_raises(self):
        """全部章节持续失败：AnalysisError，不产出空壳报告。"""

        def responder(kwargs):
            return route_by_prompt(kwargs, map_fail_sections=("sec0", "sec1"))

        with self.assertRaises(AnalysisError):
            run_analyze_with(responder)

    def test_reduce_failure_keeps_map_results(self):
        """reduce 阶段 500：返回部分报告，map 结果保留。"""

        def responder(kwargs):
            return route_by_prompt(kwargs, reduce_outcome=FakeApiError(502))

        rep, logs, client = run_analyze_with(responder)
        self.assertTrue(rep["partial"])
        self.assertIsNotNone(rep["reduce_error"])
        self.assertEqual(rep["reduce_error"]["stage"], "reduce")
        self.assertTrue(rep["all_points"])                       # map 结果保留
        self.assertEqual(rep["main_points"], rep["all_points"])  # 降级为原始要点
        self.assertIn("综合阶段失败", rep["summary"])
        self.assertTrue(any("[reduce]" in line for line in logs))


def asyncio_run(coro):
    import asyncio
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class TestNoSilentExcept(unittest.TestCase):
    def test_no_bare_except_pass_in_analyzer(self):
        """裸 except Exception: pass 清零审计。"""
        import pathlib, re
        src = pathlib.Path(analyzer.__file__).read_text(encoding="utf-8")
        for m in re.finditer(r"except[^:]*:\s*pass", src):
            self.fail(f"发现裸捕获 pass：{src[m.start()-60:m.end()+20]!r}")


if __name__ == "__main__":
    unittest.main()
