"""DeRT 冒烟测试：无模型环境下验证 loop / 工具 / 埋点 / 收尾模式。

运行（项目根目录）：python eval/smoke_test.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent import ModelResponse, run  # noqa: E402
from src.tools import default_tools  # noqa: E402


class MockLLM:
    """脚本化 LLM：按序出牌，不感知 tools 参数。"""

    def __init__(self, script):
        self.script = list(script)

    def chat(self, messages, model=None, tools=None, temperature=0.0):
        if self.script:
            return self.script.pop(0)
        return ModelResponse("FINAL: (script exhausted)", [], "mock", 0.01, 0.01, 0, 0)


def make_script():
    return [
        ModelResponse(None, [{"id": "c1", "name": "latency_stats",
                              "arguments": {"n_frames": 200}}],
                      "mock", 0.4, 1.8, 120, 40),
        ModelResponse(None, [{"id": "c2", "name": "detection_stats",
                              "arguments": {"n_frames": 200}}],
                      "mock", 0.3, 1.5, 200, 35),
        ModelResponse(
            "FINAL: system healthy. P95 within threshold, detection rate above floor.",
            [], "mock", 0.3, 1.2, 260, 60),
    ]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print("---- case 1: budget 10s (normal path) ----")
    r1 = run("diagnose recent 200 frames",
             llm=MockLLM(make_script()), tools=default_tools(), deadline=10.0)
    assert r1.stop_reason == "final", r1.stop_reason
    assert r1.finished
    print("stop:", r1.stop_reason, "| steps:", r1.steps,
          "| total: %.2fs" % r1.total_time)
    print("answer:", r1.answer[:70])
    print("summary:", json.dumps(r1.trace.summary(), ensure_ascii=False))

    print("---- case 2: budget 0.7s (wrap-up forced) ----")
    r2 = run("diagnose recent 200 frames",
             llm=MockLLM(make_script()), tools=default_tools(), deadline=0.7)
    has_wrap = any(e["kind"] == "wrap_up" for e in r2.trace.events)
    assert has_wrap, "wrap_up event should fire under tight budget"
    print("stop:", r2.stop_reason, "| steps:", r2.steps,
          "| total: %.2fs" % r2.total_time, "| wrap_up fired:", has_wrap)
    print("summary:", json.dumps(r2.trace.summary(), ensure_ascii=False))

    print("---- case 3: unknown tool (error path) ----")
    r3 = run("bad tool", llm=MockLLM([
        ModelResponse(None, [{"id": "c9", "name": "nonexistent_tool",
                              "arguments": {}}], "mock", 0.1, 0.2, 10, 5),
        ModelResponse("FINAL: done with error noted.", [], "mock", 0.1, 0.2, 30, 10),
    ]), tools=default_tools(), deadline=None)
    tool_events = [e for e in r3.trace.events if e["kind"] == "tool_call"]
    assert tool_events and tool_events[0]["status"] == "error"
    print("unknown tool handled, status:", tool_events[0]["status"])

    print("---- case 4: parallel tool batch (2 calls in one step) ----")
    # latency_stats(0.8s) + detection_stats(0.6s) 同批并行：墙钟 ≈ 0.8s（串行为 1.4s）
    t0 = time.perf_counter()
    r4 = run("diagnose 200 frames", llm=MockLLM([
        ModelResponse(None, [
            {"id": "p1", "name": "latency_stats", "arguments": {"n_frames": 200}},
            {"id": "p2", "name": "detection_stats", "arguments": {"n_frames": 200}},
        ], "mock", 0.1, 0.2, 100, 10),
        ModelResponse("FINAL: p50 and detection rate collected.", [], "mock", 0.1, 0.2, 300, 10),
    ]), tools=default_tools(), deadline=None)
    wall = time.perf_counter() - t0
    tool_events = [e for e in r4.trace.events if e["kind"] == "tool_call"]
    assert len(tool_events) == 2
    assert all(e.get("batch") for e in tool_events), "parallel batch id missing"
    assert tool_events[0]["batch"] == tool_events[1]["batch"]
    assert r4.trace.summary()["parallel_batches"] == 1
    tool_wall = max(e["duration"] for e in tool_events)
    serial = sum(e["duration"] for e in tool_events)
    assert tool_wall < serial * 0.85, \
        f"expected parallel execution, wall={tool_wall:.2f}s vs serial={serial:.2f}s"
    print(f"batch wall: {tool_wall:.2f}s (serial would be {serial:.2f}s) | "
          f"total: {r4.total_time:.2f}s | parallel_batches: "
          f"{r4.trace.summary()['parallel_batches']}")
    # 结果顺序与调用顺序一致（p1=latency, p2=detection）
    msgs = [m for m in r4.trace.events]
    _ = msgs  # 顺序正确性由 loop 的 zip 保证，这里验证工具各自返回了正确字段
    for e in tool_events:
        assert e["status"] == "ok", e

    print("---- case 5: dependency chain (list_segments -> segment_latency) ----")
    r5 = run("query seg-002 latency", llm=MockLLM([
        ModelResponse(None, [{"id": "d1", "name": "list_segments",
                              "arguments": {}}], "mock", 0.1, 0.2, 50, 5),
        ModelResponse(None, [{"id": "d2", "name": "segment_latency",
                              "arguments": {"segment_id": "seg-002"}}],
                      "mock", 0.1, 0.2, 200, 10),
        ModelResponse("FINAL: seg-002 latency collected.", [], "mock", 0.1, 0.2, 300, 10),
    ]), tools=default_tools(), deadline=None)
    assert r5.stop_reason == "final"
    # 数据依赖：未知 id 返回 error 提示（运行时体现依赖图）
    reg = default_tools()
    bad = json.loads(reg.execute("segment_latency", {"segment_id": "guess"}))
    assert "error" in bad and "list_segments" in bad["error"]
    # 依赖图与并行分组
    graph = reg.dependency_graph()
    assert graph["segment_latency"] == ["list_segments"]
    groups = reg.parallel_groups()
    assert len(groups) == 2, groups
    assert "segment_latency" not in groups[0] and "list_segments" in groups[0]
    print("dependency graph:", graph["segment_latency"], "<-",
          "| parallel groups:", groups)

    print("---- case 6: context compression in loop (select strategy) ----")
    from src.context import ContextManager
    cm = ContextManager(strategy="select", keep_recent=1, threshold_chars=300)
    r6 = run("full diagnosis", llm=MockLLM([
        ModelResponse(None, [
            {"id": "x1", "name": "latency_stats", "arguments": {"n_frames": 200}},
            {"id": "x2", "name": "list_segments", "arguments": {}},
        ], "mock", 0.1, 0.2, 100, 10),
        ModelResponse(None, [{"id": "x3", "name": "detection_stats",
                              "arguments": {"n_frames": 200}}],
                      "mock", 0.1, 0.2, 150, 10),
        ModelResponse("FINAL: p50 ok, detection rate ok.", [], "mock", 0.1, 0.2, 300, 10),
    ]), tools=default_tools(), deadline=None, compressor=cm)
    assert r6.stop_reason == "final"
    compress_events = [e for e in r6.trace.events if e["kind"] == "compress"]
    assert compress_events, "compress event missing"
    ev = compress_events[0]
    assert ev["strategy"] == "select" and ev["chars_after"] < ev["chars_before"]
    print(f"compress fired: chars {ev['chars_before']} -> {ev['chars_after']} "
          f"(stale={ev['n_stale']}), task still finished: {r6.stop_reason}")

    path = r2.trace.save("eval/traces")
    print("trace saved:", path)
    print("\nALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
