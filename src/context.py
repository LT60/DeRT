"""DeRT 上下文管理器：预算感知的三策略压缩（周 4 计划提前，模块补齐架构四模块）。

核心张力：压缩本身有成本——
  truncate / select 几乎零成本；summarize 要花一次快档 LLM 调用
  （紧预算下省下的 prefill 时间可能还不够付这次调用）。
因此"何时压缩、用哪种策略"本身就是 deadline 感知决策，
与调度器共享同一哲学：时间是资源，压缩是拿时间换时间。

三策略（不删消息、只替换旧 tool 消息的 content——保持 OpenAI 协议
assistant.tool_calls ↔ tool 消息配对完整，避免 API 结构错误）：

  truncate  旧 tool 输出 → 占位符（丢信息，零成本）
  select    旧 tool 输出 JSON 只留判分相关字段（字段级白名单；零成本）
  summarize 旧 tool 输出合并为一条快模型摘要（保语义；付一次调用，
             失败/无 llm 自动退化 truncate——优雅降级）

触发：对话总字符超阈值 且 存在"保留窗口"之外的旧 tool 消息。
接线：agent.run(compressor=...)，每步 LLM 调用前 maybe_compress；
不传则行为与 v0 完全一致（历史可比性）。

判分相关字段白名单依据 eval/tasks.py 的判分点（数值 + 判断词）：
判分只查 p50/p95、检出率等数值，故 select 丢弃 n_frames、note 等
过程字段不伤判分。list_segments / alarm_log 两工具为列表特例。
"""

from __future__ import annotations

import json

STRATEGIES = ("truncate", "select", "summarize")

# select 策略字段白名单：{工具名: 保留字段}；列表型工具走特例分支。
#
# 硬约束：白名单必须覆盖 eval/tasks.py 里**任一判分器会读到的全部字段**——
# 漏一个字段，相关任务在 dert_full 策略下就是必挂（测到的是白名单 bug，不是
# 压缩策略的效果）。2026-09-28 补了两个漏项：
#   detection_stats.mean_targets_per_frame —— T1-14/15、T2-4/18/19 的判分点
#   alarm_log.msg —— T3-14 判"是否存在推理超时"要靠 msg 里的事件文本
# 未登记的工具（get_config / render_report 等）走"未知工具原样返回"，不会被裁。
_FIELD_WHITELIST = {
    "latency_stats": {"p50_ms", "p95_ms", "p99_ms"},
    "detection_stats": {"detection_rate", "localization_jitter_px",
                        "mean_targets_per_frame"},
    "segment_latency": {"segment_id", "p50_ms", "p95_ms", "p99_ms"},
    "gpu_stats": {"gpu_util_pct", "mem_used_gb", "temperature_c"},
}
_PLACEHOLDER = "[较早的工具输出已省略（上下文压缩）]"

_SUMMARIZE_PROMPT = (
    "把下面的工具输出合并压缩为一段摘要，要求：保留全部数值与判断结论，"
    "不超过 80 字，直接输出摘要正文，不要任何前缀。工具输出：\n"
)


def _shrink_payload(name: str, content: str) -> str:
    """select 策略：单条 tool 输出的字段级裁剪。未知工具原样返回。"""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content
    if name == "list_segments":  # 列表特例：全表 → id 列表
        ids = [s.get("id") for s in data.get("segments", [])]
        return json.dumps({"segment_ids": ids}, ensure_ascii=False)
    if name == "alarm_log":      # 列表特例：只留 (t, level, source, msg)
        # msg 必须保留：T3-14 判"是否存在推理超时"靠的是 msg 里的事件文本
        # （"inference timeout 1x"），只留 level/source 会让该任务必挂。
        alarms = [{"t": a.get("t"), "level": a.get("level"),
                   "source": a.get("source"), "msg": a.get("msg")}
                  for a in data.get("alarms", [])]
        return json.dumps({"alarms": alarms, "n": data.get("n", len(alarms))},
                          ensure_ascii=False)
    keep = _FIELD_WHITELIST.get(name)
    if not keep:
        return content
    slim = {k: v for k, v in data.items() if k in keep}
    return json.dumps(slim, ensure_ascii=False)


class ContextManager:
    """三策略压缩器。所有策略共享：保留最近 keep_recent 条 tool 消息不动。"""

    def __init__(self, strategy: str = "select", keep_recent: int = 2,
                 threshold_chars: int = 1600, llm=None,
                 summarize_model: str | None = None):
        assert strategy in STRATEGIES, f"unknown strategy: {strategy}"
        self.strategy = strategy
        self.keep_recent = keep_recent
        self.threshold_chars = threshold_chars
        self.llm = llm                    # summarize 策略需要（快档客户端）
        self.summarize_model = summarize_model

    def _tool_indices(self, messages: list[dict]) -> list[int]:
        return [i for i, m in enumerate(messages) if m.get("role") == "tool"]

    @staticmethod
    def _tool_name_map(messages: list[dict]) -> dict[str, str]:
        """从 assistant 消息的 tool_calls 建 {tool_call_id: 工具名} 映射。

        兼容两种元素格式：本项目协议 {"id", "name", "arguments"} 与
        OpenAI 原始格式 {"id", "function": {"name", ...}}。
        """
        mapping: dict[str, str] = {}
        for m in messages:
            for tc in m.get("tool_calls") or []:
                name = tc.get("name") or tc.get("function", {}).get("name", "")
                mapping[tc.get("id", "")] = name
        return mapping

    def maybe_compress(self, messages: list[dict], trace=None) -> bool:
        """超阈值才压缩；返回是否发生了压缩。messages 原地修改。"""
        total = sum(len(m.get("content") or "") for m in messages)
        if total <= self.threshold_chars:
            return False
        tool_idx = self._tool_indices(messages)
        stale = tool_idx[:-self.keep_recent] if self.keep_recent else tool_idx
        if not stale:
            return False
        chars_before = total
        llm_cost = 0.0
        if self.strategy == "truncate":
            for i in stale:
                messages[i]["content"] = _PLACEHOLDER
        elif self.strategy == "select":
            names = self._tool_name_map(messages)
            for i in stale:
                tid = messages[i].get("tool_call_id", "")
                messages[i]["content"] = _shrink_payload(
                    names.get(tid, ""), messages[i]["content"])
        else:  # summarize
            ok, llm_cost = self._summarize_stale(messages, stale)
            if not ok:  # 优雅降级：摘要失败退化为 truncate
                for i in stale:
                    messages[i]["content"] = _PLACEHOLDER
        if trace is not None:
            trace.emit("compress", strategy=self.strategy,
                       n_stale=len(stale),
                       chars_before=chars_before,
                       chars_after=sum(len(m.get("content") or "")
                                       for m in messages),
                       llm_cost=round(llm_cost, 4))
        return True

    def _summarize_stale(self, messages: list[dict], stale: list[int]):
        """旧 tool 输出合并摘要：摘要放入首条位置，其余置占位符。"""
        if self.llm is None:
            return False, 0.0
        payload = "\n".join(messages[i]["content"] for i in stale)[:4000]
        try:
            resp = self.llm.chat(
                [{"role": "user", "content": _SUMMARIZE_PROMPT + payload}],
                model=self.summarize_model, tools=None)
            text = (resp.content or "").strip()
        except Exception:
            return False, 0.0
        if not text:
            return False, 0.0
        messages[stale[0]]["content"] = f"[压缩摘要] {text}"
        for i in stale[1:]:
            messages[i]["content"] = _PLACEHOLDER
        return True, resp.total
