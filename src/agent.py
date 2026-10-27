"""DeRT minimal agent loop（周 1 核心）。

loop 不依赖具体 LLM 客户端：任何实现
chat(messages, model=None, tools=None, temperature=0.0) -> ModelResponse
的对象均可注入（真实客户端 / mock / 缓存回放器）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from .instrument import Trace

SYSTEM_PROMPT = (
    "你是一个实时系统诊断 Agent，负责分析实时检测系统的运行数据。\n"
    "规则：\n"
    "1. 诊断必须基于工具返回的真实数据，禁止编造数字\n"
    "2. 每轮只做一件事：调用下一步需要的工具，或给出最终诊断\n"
    "3. 相互独立的工具（互不依赖对方输出）必须在同一轮并行调用，"
    "例如 latency_stats 与 detection_stats\n"
    "4. 数据足够时立即结束。When you have enough information and will NOT call any tool, "
    "you MUST begin your reply with the exact word 'FINAL:' followed by your conclusion. "
    "Any reply without a tool call that does not start with 'FINAL:' is invalid.\n"
)

# 通用域提示词（自动档用）：不锁定角色，工具箱里没有合适工具就直接回答。
# FINAL: 协议与 CV 域完全一致（判分/收尾逻辑共用）。
GENERIC_SYSTEM_PROMPT = (
    "你是一个能调用工具的通用任务助手。\n"
    "规则：\n"
    "1. 需要数据或执行操作时调用提供的工具，基于工具返回的真实内容作答，禁止编造；"
    "如果工具箱里没有适合本任务的工具，就直接凭你自己的知识回答，不要拒绝任务\n"
    "2. 每轮只做一件事：调用下一步需要的工具，或给出最终回答\n"
    "3. 相互独立的工具（互不依赖对方输出）必须在同一轮并行调用\n"
    "4. When you have enough information and will NOT call any tool, "
    "you MUST begin your reply with the exact word 'FINAL:' followed by your conclusion. "
    "Any reply without a tool call that does not start with 'FINAL:' is invalid.\n"
)

# 预算余量低于该值时进入收尾模式：不再发工具，强制总结（调度器 v0 雏形，周 3 替换）
MIN_STEP_BUDGET = 0.5


@dataclass
class ModelResponse:
    """LLM 单次响应的统一协议（真实客户端需提供相同属性名）。"""
    content: str | None
    tool_calls: list          # [{id, name, arguments(dict)}]
    model: str
    ttft: float
    total: float
    tokens_in: int
    tokens_out: int
    cache_hit: bool = False


@dataclass
class AgentResult:
    answer: str
    finished: bool            # 是否以 FINAL: 正常收尾
    stop_reason: str          # final | no_final | max_steps | budget_exhausted
    steps: int
    total_time: float
    budget: float | None
    trace: Trace


def run(task: str, *, llm, tools, model: str | None = None,
        deadline: float | None = None, max_steps: int = 8,
        trace: Trace | None = None, scheduler=None,
        compressor=None, resume: dict | None = None,
        on_step=None, system_prompt: str | None = None) -> AgentResult:
    """运行一次诊断任务。

    scheduler: 可选的截止期调度器（src.scheduler.DeadlineScheduler）。
    传入后每步模型由 scheduler.route() 决定（覆盖 model 参数），
    wrap_up 触发阈值与收尾 max_tokens 亦由 scheduler 提供；
    不传则保持 v0 行为（单一 model + 固定 MIN_STEP_BUDGET）。

    compressor: 可选的上下文管理器（src.context.ContextManager）。
    每步 LLM 调用前按需压缩旧 tool 输出；不传则完全不压缩（v0 行为）。

    resume: 可选断点（服务化②）：{"messages": [...], "elapsed": float,
    "step": int}——从中断处的消息历史继续跑（计时按已耗时长继续扣预算）。
    服务重启前由 on_step 落盘的 checkpoint 传入即可续跑；调度器门状态
    不在断点内（新实例重新收敛，文档口径）。

    on_step: 可选回调 callable(checkpoint_dict)，每步结束（工具输出已入栈）
    同步调用，供服务侧落盘断点；异常吞掉不影响运行。

    system_prompt: 可选系统提示词覆盖（自动档用 GENERIC_SYSTEM_PROMPT）。
    默认 None = CV 运维域提示词（主线任务集的历史行为，向后兼容）。
    """
    trace = trace or Trace()
    if resume:
        messages = [dict(m) for m in resume["messages"]]
        start = time.perf_counter() - float(resume.get("elapsed", 0.0))
        first_step = int(resume.get("step", 0)) + 1
        trace.emit("resumed", from_step=first_step - 1,
                   elapsed=round(float(resume.get("elapsed", 0.0)), 3))
    else:
        messages = [
            {"role": "system",
             "content": system_prompt if system_prompt is not None
             else SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]
        start = time.perf_counter()
        first_step = 1
        trace.emit("run_start", task=task, deadline=deadline,
                   model=model, max_steps=max_steps)

    force_final = False
    last_model = model

    for step in range(first_step, max_steps + 1):
        elapsed = time.perf_counter() - start
        remaining = None if deadline is None else deadline - elapsed
        if compressor is not None:
            compressor.maybe_compress(messages, trace=trace)
        min_step = scheduler.min_step_budget() if scheduler else MIN_STEP_BUDGET

        if not force_final and remaining is not None and remaining < min_step:
            force_final = True
            trace.emit("wrap_up", remaining=round(remaining, 3),
                       min_step=min_step)
            wrap_msg = ("时间预算即将耗尽。不要再调用工具，"
                        "立即基于已有信息给出最终结论，以 FINAL: 开头。")
            hint = scheduler.wrap_up_hint() if scheduler else None
            if hint:  # v2 数值优先指令；v0/v1.1 返回 None，行为不变
                wrap_msg += hint
            messages.append({"role": "user", "content": wrap_msg})

        step_model = scheduler.route(remaining, step) if scheduler else model
        chat_kwargs = {}
        if scheduler and force_final:
            limit = scheduler.wrap_up_max_tokens(remaining)
            if limit:
                # 仅在确有限制时传参：保持 chat() 鸭子类型协议向后兼容
                chat_kwargs["max_tokens"] = limit
        # 实时展示用：调用前打点「调度器此刻决定了什么」（新增 kind llm_start，
        # 既有分析按 kind 过滤，不受影响；不传 sink 时零开销）
        trace.emit("llm_start", step=step,
                   model=step_model or getattr(llm, "model", None),
                   remaining=None if remaining is None else round(remaining, 3),
                   wrap_up=force_final, **chat_kwargs)
        resp = llm.chat(messages, model=step_model,
                        tools=None if force_final else tools.specs(),
                        **chat_kwargs)
        last_model = resp.model
        trace.emit("llm_call", step=step, model=resp.model,
                   ttft=round(resp.ttft, 4), total=round(resp.total, 4),
                   tokens_in=resp.tokens_in, tokens_out=resp.tokens_out,
                   cache_hit=resp.cache_hit)

        assistant_msg = {"role": "assistant", "content": resp.content or ""}
        if resp.tool_calls:
            assistant_msg["tool_calls"] = [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["name"],
                              "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
                for tc in resp.tool_calls
            ]
        messages.append(assistant_msg)

        if not resp.tool_calls:
            answer = (resp.content or "").strip()
            finished = answer.upper().startswith("FINAL")
            stop = "final" if finished else "no_final"
            total = time.perf_counter() - start
            if scheduler:
                scheduler.observe_task(last_model, total, wrapped=force_final)
            trace.emit("run_end", stop_reason=stop, steps=step,
                       total_time=round(total, 4), answer=answer[:200])
            return AgentResult(answer, finished, stop, step, total, deadline, trace)

        outputs = tools.execute_many(resp.tool_calls, trace=trace)
        for tc, output in zip(resp.tool_calls, outputs):
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": output})
        if on_step is not None:
            try:
                on_step({"step": step, "messages": [dict(m) for m in messages],
                         "elapsed": round(time.perf_counter() - start, 3),
                         "force_final": force_final})
            except Exception:
                pass  # 断点落盘故障不得影响运行

    total = time.perf_counter() - start
    stop = "budget_exhausted" if force_final else "max_steps"
    if scheduler:
        scheduler.observe_task(last_model, total, wrapped=force_final)
    trace.emit("run_end", stop_reason=stop, steps=max_steps,
               total_time=round(total, 4), answer=None)
    return AgentResult("", False, stop, max_steps, total, deadline, trace)
