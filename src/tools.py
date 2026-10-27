from __future__ import annotations

import json
import random
import time
import uuid
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    func: Callable
    simulated_latency: float = 0.0  # 模拟延迟：调度实验的核心旋钮
    depends_on: tuple[str, ...] = ()  # 数据依赖：本工具参数域来自哪些工具的输出
    risk_level: str = "low"  # low=只读诊断 / high=写操作（需人工审批，HITL）


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def specs(self) -> list[dict]:
        return [{"type": "function", "function": {
            "name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in self._tools.values()]

    def dependency_graph(self) -> dict[str, list[str]]:
        """工具数据依赖图：{tool: [其参数域来源工具]}。"""
        return {name: list(t.depends_on) for name, t in self._tools.items()}

    def parallel_groups(self) -> list[list[str]]:
        """按依赖层级分组：同层工具互不依赖，可同批并行调用。"""
        remaining = dict(self.dependency_graph())
        groups: list[list[str]] = []
        while remaining:
            ready = sorted(n for n, deps in remaining.items()
                           if all(d not in remaining for d in deps))
            groups.append(ready)
            for n in ready:
                del remaining[n]
        return groups

    def risk_level(self, name: str) -> str:
        """工具风险等级（审批门用）：未知工具按 low 处理（错误由 execute 报）。"""
        t = self._tools.get(name)
        return t.risk_level if t else "low"

    def execute(self, name: str, arguments: dict, trace=None,
                batch: str | None = None) -> str:
        tool = self._tools.get(name)
        t0 = time.perf_counter()
        if tool is None:
            result, status = {"error": f"unknown tool: {name}"}, "error"
        else:
            if tool.simulated_latency:
                time.sleep(tool.simulated_latency)
            try:
                result = tool.func(**(arguments or {}))
                status = "ok"
            except Exception as exc:
                result, status = {"error": repr(exc)}, "error"
        duration = time.perf_counter() - t0
        if trace is not None:
            ev = {"tool": name, "duration": round(duration, 4), "status": status}
            if batch:
                ev["batch"] = batch  # 同批并行调用共享 batch id（向后兼容：只增字段）
            trace.emit("tool_call", **ev)
        return json.dumps(result, ensure_ascii=False)

    def execute_many(self, calls: list[dict], trace=None,
                     parallel: bool = True) -> list[str]:
        """执行同批 tool_calls（模型一轮发出的调用）。

        parallel=True 时用线程池并发（工具延迟为 I/O 型 sleep，GIL 无碍）；
        单调用或 parallel=False 退化为顺序执行（v0 行为）。
        返回值顺序与 calls 一一对应。
        """
        if len(calls) <= 1 or not parallel:
            return [self.execute(c["name"], c["arguments"], trace=trace)
                    for c in calls]
        batch = uuid.uuid4().hex[:8]
        with ThreadPoolExecutor(max_workers=min(4, len(calls))) as ex:
            futures = [ex.submit(self.execute, c["name"], c["arguments"],
                                 trace, batch) for c in calls]
            return [f.result() for f in futures]


# ---- 占位工具：模拟实时检测系统（合成数据 + 可控延迟），供埋点与预算实验 ----

def _get_config() -> dict:
    return {"mode": "realtime-stream", "resolution": "1280x720", "fps": 60,
            "pipeline_stages": 4, "detector": "rt-detect-v2"}


def _latency_stats(n_frames: int = 200) -> dict:
    rng = random.Random(n_frames)
    p50 = round(rng.uniform(8, 12), 2)
    return {"n_frames": n_frames, "p50_ms": p50,
            "p95_ms": round(p50 * rng.uniform(1.3, 1.6), 2),
            "p99_ms": round(p50 * rng.uniform(1.7, 2.2), 2),
            "wait_time_excluded": True}


def _detection_stats(n_frames: int = 200) -> dict:
    rng = random.Random(n_frames + 1)
    return {"n_frames": n_frames,
            "detection_rate": round(rng.uniform(0.9, 0.99), 4),
            "mean_targets_per_frame": round(rng.uniform(1.5, 3.5), 1),
            "localization_jitter_px": round(rng.uniform(0.8, 2.5), 2)}


def _render_report(segment: str = "latest") -> dict:
    return {"report": f"reports/{segment}.png", "frames_rendered": 100}


# ---- 周 2 扩展：依赖链工具（list_segments → segment_latency / render_report）----

_SEGMENTS = [  # 固定片段目录：依赖链入口（确定性，无随机）
    {"id": "seg-001", "start_s": 0, "end_s": 30, "n_frames": 1800},
    {"id": "seg-002", "start_s": 30, "end_s": 60, "n_frames": 1800},
    {"id": "seg-003", "start_s": 60, "end_s": 90, "n_frames": 1800},
    {"id": "seg-004", "start_s": 90, "end_s": 120, "n_frames": 1800},
    {"id": "seg-005", "start_s": 120, "end_s": 150, "n_frames": 1800},
    {"id": "seg-006", "start_s": 150, "end_s": 180, "n_frames": 1800},
]
_SEGMENT_IDS = {s["id"] for s in _SEGMENTS}


def _list_segments() -> dict:
    return {"segments": _SEGMENTS,
            "note": "segment id 供 segment_latency / render_report 使用"}


def _segment_latency(segment_id: str = "") -> dict:
    """指定片段的延迟分布（数据依赖：segment_id 必须来自 list_segments 输出）。"""
    if segment_id not in _SEGMENT_IDS:
        return {"error": f"unknown segment_id: {segment_id!r}; "
                         f"valid ids: {sorted(_SEGMENT_IDS)}（请先调用 list_segments）"}
    rng = random.Random(zlib.crc32(segment_id.encode()))
    p50 = round(rng.uniform(9, 14), 2)
    return {"segment_id": segment_id,
            "p50_ms": p50,
            "p95_ms": round(p50 * rng.uniform(1.3, 1.7), 2),
            "p99_ms": round(p50 * rng.uniform(1.8, 2.4), 2),
            "wait_time_excluded": True}


def _gpu_stats() -> dict:
    return {"gpu_util_pct": 87, "mem_used_gb": 9.2, "mem_total_gb": 16.0,
            "temperature_c": 71, "power_w": 142}


_ALARM_LOG = [  # 固定报警日志（确定性）
    {"t": 12.3, "level": "warn", "source": "pipeline", "msg": "frame drop 2/1800"},
    {"t": 45.8, "level": "error", "source": "detector", "msg": "inference timeout 1x"},
    {"t": 78.1, "level": "warn", "source": "pipeline", "msg": "queue depth 8"},
    {"t": 132.6, "level": "warn", "source": "gpu", "msg": "temperature 78C"},
    {"t": 155.2, "level": "error", "source": "detector", "msg": "inference timeout 3x"},
]


def _alarm_log(level: str = "all") -> dict:
    """报警日志查询（level 可选 all/warn/error）。"""
    level = (level or "all").lower()
    alarms = _ALARM_LOG if level == "all" else \
        [a for a in _ALARM_LOG if a["level"] == level]
    return {"alarms": alarms, "n": len(alarms)}


# ---- HITL 演示：模拟高危写操作（仅改内存 dict，不触真实系统；审批门用它演示）----

_APPLIED_CONFIG: dict = {}


def _apply_config_change(key: str = "", value: str = "") -> dict:
    """模拟配置变更：写入内存 dict（重启即失）。高危演示工具，勿接真实系统。"""
    if not key:
        return {"error": "key 不能为空"}
    _APPLIED_CONFIG[key] = value
    return {"applied": {key: value}, "note": "模拟写操作，仅内存生效"}


def default_tools() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool("get_config", "查询实时检测系统当前配置（模式、分辨率、检测器）",
                      {"type": "object", "properties": {}}, _get_config, 0.05))
    reg.register(Tool("latency_stats", "统计最近 N 帧的处理延迟分布（P50/P95/P99，已排除采集等待）",
                      {"type": "object",
                       "properties": {"n_frames": {"type": "integer", "default": 200}}},
                      _latency_stats, 0.8))
    reg.register(Tool("detection_stats", "统计最近 N 帧的检测质量（检出率、定位抖动）",
                      {"type": "object",
                       "properties": {"n_frames": {"type": "integer", "default": 200}}},
                      _detection_stats, 0.6))
    reg.register(Tool("render_report", "渲染指定片段的检测可视化报告，较慢，仅在需要展示时调用",
                      {"type": "object",
                       "properties": {"segment": {"type": "string", "default": "latest"}}},
                      _render_report, 1.5,
                      depends_on=("list_segments",)))
    reg.register(Tool("list_segments", "列出系统中所有已录制片段（id、时间范围、帧数）",
                      {"type": "object", "properties": {}}, _list_segments, 0.1))
    reg.register(Tool("segment_latency", "查询指定片段的延迟分布（P50/P95/P99）；"
                                         "segment_id 来自 list_segments 的输出",
                      {"type": "object",
                       "properties": {"segment_id": {"type": "string"}},
                       "required": ["segment_id"]},
                      _segment_latency, 0.7,
                      depends_on=("list_segments",)))
    reg.register(Tool("gpu_stats", "查询 GPU 资源状态（利用率、显存、温度、功耗）",
                      {"type": "object", "properties": {}}, _gpu_stats, 0.4))
    reg.register(Tool("alarm_log", "查询报警日志（可按级别过滤：all/warn/error）",
                      {"type": "object",
                       "properties": {"level": {"type": "string", "default": "all"}}},
                      _alarm_log, 0.3))
    reg.register(Tool("apply_config_change",
                      "【高危】修改实时检测系统运行配置（如 fps、detector）；"
                      "写操作，调用前需人工确认",
                      {"type": "object",
                       "properties": {"key": {"type": "string"},
                                      "value": {"type": "string"}},
                       "required": ["key", "value"]},
                      _apply_config_change, 0.2, risk_level="high"))
    return reg
