"""BFCL v4 多轮外部验证 harness（2026-10-25，任务 #18/#19）。

目的：把主线结论放到**外部学术基准**上检验（此前 7000 条记录全部来自自建
CV 运维任务集，面试可质疑"任务覆盖面窄"）。评测对象不变——预算/调度策略
在另一域任务上的 success（判分 = BFCL 官方逐轮状态比对，确定性、无 LLM judge）。

策略（对照设计，与主线 budget_eval 同构）：
    A  fixed_strong   全程强档（百炼 v4.1-flash），不感知预算；硬上限 240s
    C  fixed_fast     全程快档（本地 qwen2.5:7b），不感知预算；硬上限 240s
    B1 dert_v1@B      双档时间路由（3.5s 门 + 粘性），预算档 {10,30,60}s
    B3 dert_v3d@B     难度路由 + 自适应门（原样迁移，中文难度词——预期不触发）
    B3E dert_v3d_en@B 同 v3d 但难度词表换英文（换域重标定的部署形态）

多轮语义：episode 的 user 轮次逐条注入；agent 本轮不再发工具调用即视为
本轮答复，进入下一轮；时间预算跨全 episode 掐表；调度器粘性档位在
episode 首步一次定死（与主线一致）。

判分口径：每轮边界把模型实例公开属性与 ground truth 重放实例比对
（BFCL multi_turn_checker 同款）；success = 所有轮全部通过。模型在轮内
发出的全部工具调用照常执行（含错误调用——错误会破坏状态，正是判分对象）。

输出：eval/results/bfcl_<run_id>/
    raw.jsonl   每行一条运行记录（断点续跑按 (episode, strategy, budget) 跳过）
    env.json    环境（模型/端点/常数/episode 清单/预热 TTFT）

用法（项目根目录）：
    python -u eval/bfcl_run.py --smoke      # 2 题 × 2 策略 冒烟
    python -u eval/bfcl_run.py              # 24 题 × 全策略
    python -u eval/bfcl_run.py --episodes 40 --budgets 30 60
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
from eval.bfcl_adapter import (build_instances, compare_states,  # noqa: E402
                               make_registry, replay_ground_truth,
                               snapshot_state, _freeze)
from eval.bfcl_adapter import load_category  # noqa: E402

RESULTS_DIR = Path("eval/results")
HARD_CAP = 240.0
MAX_STEPS_PER_TURN = 8

SYSTEM_PROMPT = (
    "You are a helpful assistant operating a set of tools across multiple user "
    "requests. Rules:\n"
    "1. Use tools to accomplish what the user asks; never fabricate results.\n"
    "2. Issue multiple independent tool calls in the same turn when possible.\n"
    "3. When the current request is fully handled (all needed tool results are "
    "in), reply with a short confirmation or answer and DO NOT call any tool.\n"
    "4. If a tool call returns an error, fix the arguments and retry.\n"
)
WRAP_UP_MSG = ("Time budget is nearly exhausted. Do NOT call any more tools. "
               "Reply now with a brief status of what has been done.")

# v3d 英文重标定词表（B3E）：v3 的难度信号 = 问句含数值对比/择优措辞。
# 中文词表（是否超过/SLO/红线/…）在英文基准上恒不命中——这是"换域必须
# 重标定难度信号"的部署结论，B3 vs B3E 的差就是证据。
CMP_WORDS_EN = ("whether", "compare", "versus", " vs.", " vs ", "exceed",
                "threshold", "most", "least", "best", "cheapest", "highest",
                "lowest", "latest", "longest", "shortest", "how many",
                "count", "average", "total")


class _V3DEn(sched_mod.DeadlineSchedulerV3D):
    """v3d 的英文重标定变体（仅换难度词表，其余机制逐字继承）。"""

    CMP_WORDS = CMP_WORDS_EN


def _tier_key(model: str, strong_name: str) -> str:
    if model == strong_name:
        return "strong"
    if model and ("qwen2.5" in model or "7b" in model):
        return "fast"
    return "other"


def run_episode(ep: dict, llm, scheduler=None, deadline: float | None = None,
                gt_snaps: list[dict] | None = None,
                trace=None, max_steps: int | None = None) -> dict:
    """跑一个 episode（多轮），返回原始运行记录（含逐轮状态判分）。

    trace: 可选 Trace——发射与 agent.run 同款事件（run_start/llm_start/
    llm_call/tool_call/wrap_up/run_end，另加 turn_result），供服务化 SSE
    直播复用同一前端。max_steps: 每轮步数上限（缺省用模块常量）。"""
    instances = build_instances(ep["initial_config"], ep["involved_classes"])
    registry = make_registry(instances, ep["involved_classes"],
                             ep.get("excluded_function") or [],
                             allowed_funcs=ep.get("path") or None)
    if gt_snaps is None:
        gt_snaps = replay_ground_truth(ep["initial_config"],
                                       ep["involved_classes"], ep["_gt"])

    first_user = ep["question"][0][0]["content"]
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
                   max_steps=steps_cap, turns=len(ep["question"]), bfcl=True)

    n_turns = len(ep["question"])
    for ti, turn in enumerate(ep["question"]):
        user_text = turn[0]["content"]
        messages.append({"role": "user", "content": user_text})
        if wrapped:                          # 预算已尽：本轮只许文字答复
            messages.append({"role": "user", "content": WRAP_UP_MSG})

        for _step in range(steps_cap):
            elapsed = time.perf_counter() - start
            remaining = deadline_eff - elapsed
            min_step = scheduler.min_step_budget() if scheduler else 0.8
            if not wrapped and remaining < min_step:
                wrapped = True               # 收尾翻转（每 episode 只发生一次）
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
            tier_counts[_tier_key(resp.model, getattr(llm, "strong_model_name", ""))] = \
                tier_counts.get(_tier_key(resp.model, getattr(llm, "strong_model_name", "")), 0) + 1
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
            messages.append(assistant)

            if not resp.tool_calls:
                break                       # 本轮答复完成
            outputs = registry.execute_many(resp.tool_calls, trace=trace)
            for tc, out in zip(resp.tool_calls, outputs):
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": out})
        else:
            stop = "max_steps_in_turn"      # 单轮步数耗尽仍未答复

        elapsed = time.perf_counter() - start
        if stop != "done" and stop != "max_steps_in_turn":
            break                           # llm_error：后续轮没有意义
        # 轮边界状态比对（BFCL 逐轮口径）
        model_snap = {cn: {k: _freeze(v) for k, v in vars(inst).items()
                           if not k.startswith("_")}
                      for cn, inst in instances.items()}
        ok, diffs = compare_states(model_snap, gt_snaps[ti])
        turns_valid.append(ok)
        if trace is not None:
            trace.emit("turn_result", turn=ti + 1, valid=ok,
                       diff=None if ok else "; ".join(diffs[:3])[:200])
        if not ok and first_bad_diff is None:
            first_bad_diff = "; ".join(diffs[:3])[:300]
        if not ok:
            stop = "state_mismatch"         # BFCL 同款：首个错轮即终止
            break

    total = time.perf_counter() - start
    if scheduler is not None:
        scheduler.observe_task(last_used_model or getattr(llm, "model", ""),
                               total, wrapped=wrapped)
    success = all(turns_valid) and len(turns_valid) == len(gt_snaps)
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
    llm.chat([{"role": "user", "content": "Reply with just: ok"}])
    ttft = time.perf_counter() - t0
    print(f"done (TTFT {ttft:.2f}s)")
    return ttft


def pick_episodes(eps: list[dict], n: int) -> list[dict]:
    """均匀采样（确定性，覆盖全集顺序，不偏向文件头部）。"""
    if n >= len(eps):
        return eps
    step = len(eps) / n
    return [eps[min(len(eps) - 1, int(i * step))] for i in range(n)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="bfcl_ext1")
    ap.add_argument("--category", default="BFCL_v4_multi_turn_base")
    ap.add_argument("--episodes", type=int, default=24)
    ap.add_argument("--budgets", type=float, nargs="+", default=[10.0, 30.0, 60.0])
    ap.add_argument("--strategies", nargs="+",
                    default=["A", "C", "v1", "v3d", "v3d_en"],
                    help="A/C=固定档；v1/v3d/v3d_en=调度策略(每预算档一跑)")
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    if args.smoke:
        args.episodes = 2
        args.budgets = [30.0]
        args.strategies = ["A", "C", "v1", "v3d_en"]

    eps = pick_episodes(load_category(args.category), args.episodes)
    print(f"[plan] episodes={len(eps)} budgets={args.budgets} "
          f"strategies={args.strategies}")

    out_dir = RESULTS_DIR / f"bfcl_{args.run_id}"
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
    fast.strong_model_name = strong.model     # tier 计数用（A/C 直连客户端时）
    strong.strong_model_name = strong.model
    ttft_fast = warmup(fast, "fast " + fast.model)
    ttft_strong = warmup(strong, "strong " + strong.model)

    # 每策略一个调度器实例（跨 episode 在线适应，与主线同构）
    scheds = {}
    if "v1" in args.strategies:
        scheds["v1"] = DeadlineScheduler(fast.model, strong.model)
    if "v3d" in args.strategies:
        scheds["v3d"] = DeadlineSchedulerV3D(fast.model, strong.model)
    if "v3d_en" in args.strategies:
        scheds["v3d_en"] = _V3DEn(fast.model, strong.model)

    env = {
        "date": datetime.now().isoformat(timespec="seconds"),
        "category": args.category, "episodes": [e["id"] for e in eps],
        "budgets": args.budgets, "strategies": args.strategies,
        "fast_model": fast.model, "strong_model": strong.model,
        "ttft": {"fast": round(ttft_fast, 3), "strong": round(ttft_strong, 3)},
        "hard_cap": HARD_CAP, "max_steps_per_turn": MAX_STEPS_PER_TURN,
        "cmp_words_en": CMP_WORDS_EN,
        "gate_consts": {"STRONG_REMAINING_MIN": sched_mod.STRONG_REMAINING_MIN,
                        "GATE_FLOOR_V3D": sched_mod._GATE_FLOOR_V3D},
        "note": "判分=BFCL 逐轮状态比对（possible_answer 重放），success=全部轮通过",
    }
    (out_dir / "env.json").write_text(json.dumps(env, ensure_ascii=False,
                                                 indent=2), encoding="utf-8")

    gt_cache: dict[str, list[dict]] = {}
    plan: list[tuple[dict, str, float | None]] = []
    for ep in eps:
        runs = []
        if "A" in args.strategies:
            runs.append(("A", None))
        if "C" in args.strategies:
            runs.append(("C", None))
        for s in ("v1", "v3d", "v3d_en"):
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
        if ep["id"] not in gt_cache:
            gt_cache[ep["id"]] = replay_ground_truth(
                ep["initial_config"], ep["involved_classes"], ep["_gt"])
        t0 = time.perf_counter()
        try:
            rec = run_episode(ep, llm, scheduler=sched, deadline=dl,
                              gt_snaps=gt_cache[ep["id"]])
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
