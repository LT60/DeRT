# -*- coding: utf-8 -*-
"""HammerBench 中文多轮函数调用基准适配器（2026-10-25，服务化⑦）。

来源：HF dataset MadeAgents/HammerBench（OPPO 研究院 + 上海交大，
arXiv:2412.16516），中文多轮子集 data/zh/multi-turn.json（45MB，
17498 快照 / 6420 conversation / 1063 工具）。来源与许可记录见
eval/hammerbench/SOURCES.md。

数据形态（实测确认）：
    每条记录 = 到第 k 轮为止的对话快照（shareGPT 变体）：
        messages 严格遵循  user → function call → assistant → … → function call
        末条 function call = 本轮期望调用 {"name", "arguments"}
        tools = 该 conversation 的候选工具集（OpenAI function schema 风格）
    最深快照即完整轨迹；id = "<类型>_<conv_id>_<turn_id>"，
    类型 ∈ {Based, mQmA, mQsA, sQmA, IS, SO-case1, SO-case2, mSv, External}
    （多轮问答轨迹 / 意图转移 / 参数覆盖 / 外部指代）。

判分（与官方粒度对齐，确定性规则、无 LLM judge、无环境执行）：
    turn 通过 = 本轮模型发出的工具调用中存在一条
      函数名精确匹配 且 归一化参数完全相等（HammerBench 的
      name accuracy + 参数精确匹配口径；多给参数=幻觉=失败）。
    episode success = 所有轮全部通过（与 BFCL 首错即停口径一致）。

与 BFCL 适配器的关键差异：HammerBench 只提供工具 schema、不提供工具实现
（slot-filling 语义：判"调的对不对"，不判"世界变成什么样"），工具执行由
stub 返回 success。难度信号：中文基准 → 主线中文词表原配（v3d 无需重标定）。
"""
from __future__ import annotations

import json
from pathlib import Path

from src.tools import Tool, ToolRegistry

DATA_PATH = Path(__file__).resolve().parent / "hammerbench" / "data" / "zh_multi-turn.json"

# id 里的下划线冲突：类型名本身含 '-' 不含 '_'，conv_id 为数字 → rsplit 安全


def load_conversations(path: Path | None = None) -> list[dict]:
    """把快照流聚合成 conversation 级 episode 列表。

    episode = {"id": "<类型>_<conv>", "datatype": str, "tools": [...],
               "turns": [{"user": str, "expected": {"name", "arguments"}}]}
    聚合取每个 conversation 的最深快照（完整轨迹），逐段切轮。
    """
    path = path or DATA_PATH
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    deepest: dict[str, dict] = {}          # conv_key → 最深快照
    for rec in data:
        key = rec["id"].rsplit("_", 1)[0]  # "<类型>_<conv>"
        cur = deepest.get(key)
        if cur is None or len(rec["messages"]) > len(cur["messages"]):
            deepest[key] = rec
    out = []
    for key, rec in deepest.items():
        datatype, _conv = key.split("_", 1)
        tools = rec["tools"]
        turns: list[dict] = []
        for msg in rec["messages"]:
            if msg["role"] == "user":
                turns.append({"user": msg["content"], "expected": None})
            elif msg["role"] == "function call":
                if turns and turns[-1]["expected"] is None:
                    fc = msg["content"]
                    turns[-1]["expected"] = {"name": fc["name"],
                                             "arguments": fc.get("arguments") or {}}
        # 丢掉无期望调用的尾轮（数据保证配对，防御性兜底）
        turns = [t for t in turns if t["expected"] is not None]
        out.append({"id": key, "datatype": datatype,
                    "tools": tools, "turns": turns})
    return out


def make_registry(tools: list[dict]) -> ToolRegistry:
    """HammerBench 工具 schema → DeRT ToolRegistry（stub 执行）。

    无环境实现：工具调用返回 success stub（官方评估同样无执行环境，
    判分只看调用本身）。走真实 ToolRegistry 以复用 trace 的 tool_call
    事件（UI 直播）与审批门等既有管道。

    required 全部剥空：HammerBench 数据的 slot-filling 轨迹允许「先空参
    调用、后续轮次补全」，但工具 schema 的 required 却全参数必填——保留
    会逼模型编造参数（实测强模型因此全军覆没在幻觉参数上）。参数填充
    本身就是被测对象，与官方评估 prompt 的语义一致。
    """
    reg = ToolRegistry()
    for spec in tools:
        params = dict(spec.get("parameters",
                               {"type": "object", "properties": {}}))
        params = {**params, "required": []}
        reg.register(Tool(
            name=spec["name"],
            description=spec.get("description", ""),
            parameters=params,
            func=lambda *a, **k: {"status": "success"},
        ))
    return reg


def _norm(v) -> str:
    """参数值归一化：数字/布尔统一字符串比较（6697 str / 272 int / 130 float / 103 bool）。"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def grade_turn(seen_calls: list[tuple[str, dict]],
               expected: dict) -> tuple[bool, str | None]:
    """轮级判分：本轮发出的 (name, arguments) 中存在一条与期望完全匹配？

    seen_calls: 该轮模型发出的全部工具调用（含重复——存在匹配即可）。
    返回 (ok, diff)：diff 为首个不匹配原因（诊断展示用，截断友好）。
    """
    if not seen_calls:
        return False, "本轮未发出任何工具调用（slot-filling 应先调用/追问）"
    exp_name = expected["name"]
    exp_args = {k: _norm(v) for k, v in (expected.get("arguments") or {}).items()}
    for name, args in seen_calls:
        if name != exp_name:
            continue
        got = {k: _norm(v) for k, v in (args or {}).items()}
        if got == exp_args:
            return True, None
        missing = sorted(set(exp_args) - set(got))
        extra = sorted(set(got) - set(exp_args))
        wrong = sorted(k for k in set(got) & set(exp_args)
                       if got[k] != exp_args[k])
        parts = []
        if missing:
            parts.append(f"缺参数 {missing}")
        if extra:
            parts.append(f"幻觉参数 {extra}")
        if wrong:
            parts.append("值不符 " + "; ".join(f"{k}: got={got[k]!r} want={exp_args[k]!r}"
                                              for k in wrong[:2]))
        return False, (f"{exp_name}: " + "；".join(parts))[:200]
    called = sorted({n for n, _ in seen_calls})
    return False, f"未调用期望函数 {exp_name}（实际调用 {called[:3]}）"


def pick_episodes(eps: list[dict], n: int,
                  turn_range: tuple[int, int] = (2, 5)) -> list[dict]:
    """精选采样：2–5 轮、类型轮转 + 类型内均匀 stride（确定性、不偏向头部）。

    注意：多轮 conversation 的候选工具集收敛为 1 个（数据本身如此——
    单轮快照才有 5 工具干扰项），故不按工具数过滤。
    """
    pool = [e for e in eps if turn_range[0] <= len(e["turns"]) <= turn_range[1]]
    by_type: dict[str, list[dict]] = {}
    for e in pool:
        by_type.setdefault(e["datatype"], []).append(e)
    types = sorted(by_type)
    per = max(1, n // len(types)) + (1 if n % len(types) else 0)
    picked: list[dict] = []
    for t in types:                        # 类型内均匀 stride（同 BFCL pick）
        lst = sorted(by_type[t], key=lambda e: e["id"])
        step = max(1, len(lst) // per)
        picked += lst[::step][:per]
    return picked[:n]
