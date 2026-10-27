from __future__ import annotations

import json
import os
import time
import uuid


class Trace:
    """全链路埋点：按时间序记录事件，可落盘为 JSONL。

    事件 kind：run_start / llm_start / llm_call / tool_call / wrap_up / run_end

    sink（可选，实时展示用）：callable(event)，emit 时同步调用。
    token 流只走 sink（emit_token），不写入 events——保证落盘 trace 的
    schema 与既有分析口径不变（AGENTS.md §3：向后兼容只增）。
    sink 抛异常一律吞掉：展示层故障不得影响运行本身。
    """

    def __init__(self, run_id: str | None = None, sink=None):
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.t0 = time.perf_counter()
        self.events: list[dict] = []
        self.sink = sink

    def emit(self, kind: str, **fields) -> dict:
        ev = {"ts": round(time.perf_counter() - self.t0, 4), "kind": kind, **fields}
        self.events.append(ev)
        if self.sink is not None:
            try:
                self.sink(ev)
            except Exception:
                pass
        return ev

    def emit_token(self, text: str) -> None:
        """流式 token：只推给 sink（实时展示），不进 events（不污染 trace 口径）。"""
        if self.sink is None or not text:
            return
        try:
            self.sink({"ts": round(time.perf_counter() - self.t0, 4),
                       "kind": "token", "text": text})
        except Exception:
            pass

    def summary(self) -> dict:
        llm_calls = [e for e in self.events if e["kind"] == "llm_call"]
        tool_calls = [e for e in self.events if e["kind"] == "tool_call"]
        llm_time = sum(e["total"] for e in llm_calls)
        # tool_time 墙钟口径：并行批（共享 batch id）取批内 max，串行调用累加
        # （旧 trace 无 batch 字段 → 全部按串行累加，口径不变）
        batches: dict[str | None, float] = {}
        for e in tool_calls:
            key = e.get("batch")
            batches[key] = max(batches.get(key, 0.0), e["duration"])
        tool_time = sum(batches.values())
        parallel_batches = sum(1 for k, _ in batches.items()
                               if k and sum(1 for e in tool_calls
                                            if e.get("batch") == k) > 1)
        return {
            "run_id": self.run_id,
            "total_time": round(self.events[-1]["ts"], 3) if self.events else 0.0,
            "llm_calls": len(llm_calls),
            "llm_time": round(llm_time, 3),
            "tool_calls": len(tool_calls),
            "tool_time": round(tool_time, 3),
            "parallel_batches": parallel_batches,
            "tokens_in": sum(e.get("tokens_in", 0) for e in llm_calls),
            "tokens_out": sum(e.get("tokens_out", 0) for e in llm_calls),
            "cache_hits": sum(1 for e in llm_calls if e.get("cache_hit")),
        }

    def save(self, directory: str) -> str:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{self.run_id}.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for ev in self.events:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return path
