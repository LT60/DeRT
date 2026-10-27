"""HammerBench 中文多轮基准 runner（2026-10-25，服务化⑦）。

与 eval/bfcl_run.py 同构的采集 harness，差异：
    - 判分 = 逐轮调用比对（函数名 + 归一化参数精确匹配），无环境状态
    - 工具执行 = stub success（HammerBench 只提供 schema，slot-filling 语义）
    - SYSTEM_PROMPT 中文（slot-filling：信息不全先追问，拿到结果再答复）
    - 难度信号：中文词表原配（v3d 不需重标定——这正是本基准的实验点：
      「跨数据集、同语言」与 BFCL「跨语言必须重标定」形成对照）

事件协议：与 agent.run / bfcl_run 同款（run_start 带 bench="hb"、
llm_start/llm_call/tool_call/turn_result/wrap_up/run_end），服务化 SSE 直播复用。

输出：eval/results/hb_<run_id>/raw.jsonl + env.json（断点续跑同 BFCL）。

用法（项目根目录，venv python）：
    python -u eval/hammerbench_run.py --smoke     # 2 题 × 精简策略
    python -u eval/hammerbench_run.py             # 20 题 × A/C/v1/v3d
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.llm import OpenAICompatLLM, bailian_llm  # noqa: E402
from src.scheduler import (DeadlineScheduler, DeadlineSchedulerV3D,  # noqa: E402
                           DualTierLLM)
from src import scheduler as sched_mod  # noqa: E402
from eval.hammerbench_adapter import (grade_turn, load_conversations,  # noqa: E402
                                      make_registry, pick_episodes)

RESULTS_DIR = Path("eval/results")
HARD_CAP = 240.0
MAX_STEPS_PER_TURN = 8

SYSTEM_PROMPT = (
    "你是一个通过工具帮用户办事的中文手机助理。规则：\n"
    "1. 用户提出请求后，必须立刻发起对应的工具调用；不要只输出文字追问。\n"
    "2. 只填用户已经明确说出的参数，其余参数留空；绝不编造用户没提供的信息。\n"
    "3. 用户在后续轮次每补充一条信息，就用合并后的最新参数集重新调用该工具。\n"
    "4. 工具结果回来后，用一句话简短答复用户。"
)
WRAP_UP_MSG = "时间预算快用完了。不要再调用任何工具，立刻用一句话汇报目前进展。"


def _tier_key(model: str, strong_name: str) -> str:
    if model == strong_name:
        return "strong"
    if model and ("qwen2.5" in model or "7b" in model):
        return "fast"
    return "other"


def run_episode(ep: dict, llm, scheduler=None, deadline: float | None = None,
                trace=None, max_steps: int | None = None) -> dict:
    """跑一个 conversation（多轮 slot-filling），返回逐轮判分记录。"""
    registry = make_registry(ep["tools"])
    first_user = ep["turns"][0]["user"]
    if scheduler is not None and hasattr(scheduler, "set_prompt"):
        scheduler.set_prompt(first_user)
    if scheduler is not None and hasattr(scheduler, "set_task_id"):
        scheduler.set_task_id(ep["id"])

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    turns_valid: list[bool] = []
    first_bad_diff: str | None = None
    tier_counts: dict[str, int] = {}
    tokens_in = tokens_out = 0
    wrapped = False
    last_used_model: str | None = None
    last_reply = ""
    gstep = 0
    stop = "done"
    steps_cap = max_steps or MAX_STEPS_PER_TURN

    start = time.perf_counter()
    deadline_eff = deadline if deadline is not None else HARD_CAP
    if trace is not None:
        trace.emit("run_start", task=ep["id"], deadline=deadline,
                   max_steps=steps_cap, turns=len(ep["turns"]), bench="hb")

    n_turns = len(ep["turns"])
    for ti, turn in enumerate(ep["turns"]):
        messages.append({"role": "user", "content": turn["user"]})
        if wrapped:
            messages.append({"role": "user", "content": WRAP_UP_MSG})

        seen_calls: list[tuple[str, dict]] = []   # 本轮发出的全部调用
        for _step in range(steps_cap):
            elapsed = time.perf_counter() - start
            remaining = deadline_eff - elapsed
            min_step = scheduler.min_step_budget() if scheduler else 0.8
            if not wrapped and remaining < min_step:
                wrapped = True
                messages.append({"role": "user", "content": WRAP_UP_MSG})
                if trace is not None:
                    trace.emit("wrap_up", remaining=round(remaining, 3),
                               min_step=min_step)
            gstep += 1
            step_model = scheduler.route(remaining, step=1 if gstep == 1 else 2) \
                if scheduler else None
            chat_kwargs = {}
            if scheduler and wrapped:
                limit = scheduler.wrap_up_max_tokens(remaining)
                if limit:
                    chat_kwargs["max_tokens"] = limit
            if trace is not None:
                trace.emit("llm_start", step=gstep,
                           model=step_model or getattr(llm, "model", None),
                           remaining=None if remaining is None else round(remaining, 3),
                           wrap_up=wrapped, turn=ti + 1, **chat_kwargs)
            try:
                resp = llm.chat(messages, model=step_model,
                                tools=None if wrapped else registry.specs(),
                                temperature=0.0, **chat_kwargs)
            except Exception as exc:
                stop = f"llm_error: {type(exc).__name__}"
                break
            if trace is not None:
                trace.emit("llm_call", step=gstep, model=resp.model,
                           ttft=round(resp.ttft, 4), total=round(resp.total, 4),
                           tokens_in=resp.tokens_in, tokens_out=resp.tokens_out,
                           cache_hit=resp.cache_hit, turn=ti + 1)
            strong_name = getattr(llm, "strong_model_name", "")
            tk = _tier_key(resp.model, strong_name)
            tier_counts[tk] = tier_counts.get(tk, 0) + 1
            tokens_in += resp.tokens_in
            tokens_out += resp.tokens_out
            last_used_model = resp.model
            if resp.content:
                last_reply = resp.content

            assistant = {"role": "assistant", "content": resp.content or ""}
            if resp.tool_calls:
                assistant["tool_calls"] = [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"],
                                  "arguments": json.dumps(tc["arguments"],
                                                          ensure_ascii=False)}}
                    for tc in resp.tool_calls]
                seen_calls += [(tc["name"], tc["arguments"] or {})
                               for tc in resp.tool_calls]
            messages.append(assistant)

            if not resp.tool_calls:
                break                       # 本轮答复完成
            outputs = registry.execute_many(resp.tool_calls, trace=trace)
            for tc, out in zip(resp.tool_calls, outputs):
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": out})
        else:
            stop = "max_steps_in_turn"

        if stop != "done" and stop != "max_steps_in_turn":
            break                           # llm_error：后续轮没有意义
        ok, diff = grade_turn(seen_calls, turn["expected"])
        turns_valid.append(ok)
        if trace is not None:
            trace.emit("turn_result", turn=ti + 1, valid=ok,
                       diff=None if ok else diff)
        if not ok and first_bad_diff is None:
            first_bad_diff = (diff or "")[:300]
        if not ok:
            stop = "call_mismatch"          # 首个错轮即终止（与 BFCL 口径一致）
            break

    total = time.perf_counter() - start
    if scheduler is not None:
        scheduler.observe_task(last_used_model or getattr(llm, "model", ""),
                               total, wrapped=wrapped)
    success = all(turns_valid) and len(turns_valid) == n_turns
    on_time = total <= deadline_eff + 0.05 if deadline is not None else None
    if trace is not None:
        trace.emit("run_end", stop_reason=stop, steps=gstep,
                   total_time=round(total, 4), answer=(last_reply or "")[:200])
    return {
        "episode": ep["id"], "turns_total": n_turns,
        "turns_run": len(turns_valid), "turns_valid": turns_valid,
        "success": success, "on_time": on_time,
        "total_time": round(total, 3), "wrapped": wrapped,
        "stop_reason": stop, "tier_usage": tier_counts,
        "tokens_in": tokens_in, "tokens_out": tokens_out,
        "first_bad_diff": first_bad_diff, "answer": last_reply,
        "steps": gstep,
    }


def build_clients():
    fast = OpenAICompatLLM(cache_dir="")
    strong = bailian_llm(cache_dir="")
    return fast, strong


def warmup(llm: OpenAICompatLLM, label: str) -> float:
    print(f"[warmup] {label} ...", end=" ", flush=True)
    t0 = time.perf_counter()
    llm.chat([{"role": "user", "content": "只回复：ok"}])
    ttft = time.perf_counter() - t0
    print(f"done (TTFT {ttft:.2f}s)")
    return ttft


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="hb_ext1")
    ap.add_argument("--episodes", type=int, default=20)
    ap.add_argument("--budgets", type=float, nargs="+", default=[10.0, 30.0, 60.0])
    ap.add_argument("--strategies", nargs="+", default=["A", "C", "v1", "v3d"])
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    if args.smoke:
        args.episodes = 2
        args.budgets = [30.0]
        args.strategies = ["A", "C", "v1"]

    eps = pick_episodes(load_conversations(), args.episodes)
    print(f"[plan] episodes={len(eps)} budgets={args.budgets} "
          f"strategies={args.strategies}")

    out_dir = RESULTS_DIR / f"hb_{args.run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / "raw.jsonl"
    done: set[tuple] = set()
    if raw_path.exists():
        for line in raw_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                done.add((r["episode"], r["strategy"], r.get("budget")))
        print(f"[resume] 已有 {len(done)} 条记录，跳过对应运行")

    fast, strong = build_clients()
    fast.strong_model_name = strong.model
    strong.strong_model_name = strong.model
    ttft_fast = warmup(fast, "fast " + fast.model)
    ttft_strong = warmup(strong, "strong " + strong.model)

    scheds = {}
    if "v1" in args.strategies:
        scheds["v1"] = DeadlineScheduler(fast.model, strong.model)
    if "v3d" in args.strategies:
        scheds["v3d"] = DeadlineSchedulerV3D(fast.model, strong.model)

    env = {
        "date": datetime.now().isoformat(timespec="seconds"),
        "episodes": [e["id"] for e in eps],
        "episode_types": [e["datatype"] for e in eps],
        "budgets": args.budgets, "strategies": args.strategies,
        "fast_model": fast.model, "strong_model": strong.model,
        "ttft": {"fast": round(ttft_fast, 3), "strong": round(ttft_strong, 3)},
        "hard_cap": HARD_CAP, "max_steps_per_turn": MAX_STEPS_PER_TURN,
        "note": "判分=逐轮调用比对（函数名+归一化参数精确匹配），success=全部轮通过",
    }
    (out_dir / "env.json").write_text(json.dumps(env, ensure_ascii=False,
                                                 indent=2), encoding="utf-8")

    plan: list[tuple[dict, str, float | None]] = []
    for ep in eps:
        runs = []
        if "A" in args.strategies:
            runs.append(("A", None))
        if "C" in args.strategies:
            runs.append(("C", None))
        for s in ("v1", "v3d"):
            if s in args.strategies:
                runs += [(s, b) for b in args.budgets]
        off = eps.index(ep) % len(runs)      # 交替轮转（同主线测量纪律）
        runs = runs[off:] + runs[:off]
        plan += [(ep, s, b) for s, b in runs]

    n_done = 0
    for ep, strat, budget in plan:
        key = (ep["id"], strat, budget)
        if key in done:
            continue
        if strat == "A":
            llm, sched, dl = strong, None, None
        elif strat == "C":
            llm, sched, dl = fast, None, None
        else:
            sched = scheds[strat]
            llm = DualTierLLM(fast, strong, sched)
            llm.strong_model_name = strong.model
            dl = budget
        t0 = time.perf_counter()
        try:
            rec = run_episode(ep, llm, scheduler=sched, deadline=dl)
        except Exception as exc:
            rec = {"episode": ep["id"], "strategy": strat, "budget": budget,
                   "success": False, "on_time": None, "total_time": None,
                   "stop_reason": f"harness_error: {type(exc).__name__}: {exc}"[:300],
                   "turns_valid": []}
        rec["strategy"], rec["budget"] = strat, budget
        rec["wall"] = round(time.perf_counter() - t0, 3)
        with raw_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        n_done += 1
        print(f"[{n_done}] {ep['id']} {strat}@{budget} "
              f"success={rec.get('success')} t={rec.get('total_time')}s "
              f"stop={rec.get('stop_reason')}", flush=True)

    print(f"\n[done] {n_done} runs → {raw_path}")


if __name__ == "__main__":
    main()
