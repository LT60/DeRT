"""Pareto 评估：N 任务 × 2–6 策略 × 4 预算 × R 重复。

策略（对照设计见 benchmarks/tasks-design.md）：
    fixed_fast    固定快档（qwen2.5:7b），不感知预算，跑完为止
    fixed_strong  固定强档（deepseek-chat），不感知预算，跑完为止（需 --with-strong）
    dert          快档 + 截止期感知（wrap_up 收尾，调度器 v0）
    dert_v1       双档路由 + 自适应收尾（调度器 v1.1，需 --dert-v1）
    dert_v2       v1.1 + 量化路由 + 收尾数值优先（调度器 v2，需 --dert-v2）
    dert_full     v1.1 + select 上下文压缩（tasks-design 的完整 DeRT：
                  路由+并行+压缩三件套，需 --compress）

成功判定 = 内容判分通过 且 实际耗时 <= 预算（按时交付）。

成本口径：
    基线策略行为与预算无关 → 每任务每重复跑 1 次，对 4 档预算分别判"是否按时"；
    dert / dert_v1 / dert_v2 行为随预算变化 → 每预算各跑 1 次。
    全实时无缓存（缓存目录传空）。60 任务 × 3 重复四策略网格（2026-09-28 任务集
    由 50 扩到 60）的 deepseek 运行约 720 次（fixed_strong 180 + dert_v1 宽预算档
    540），成本上限约 ¥15；本地 qwen 侧零成本但耗时占大头。

重复与噪声量化（--repeats，默认 1）：
    每配置跑 R 次，汇总输出跨重复的成功率 mean ± std（样本标准差），
    records 里每条带 repeat 编号；结束后打印各策略 TTFT 分布（P50/P95）。

用法（项目根目录，建议 python -u 防块缓冲）：
    python -u eval/pareto_routine.py                          # 默认跑全部 60 任务
    python -u eval/pareto_routine.py --with-strong --dert-v1 --dert-v2 --repeats 3
    python -u eval/pareto_routine.py --budgets 2,4,8,30
    python -u eval/pareto_routine.py --tasks T1-1,T2-1        # 子集调试
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent import run  # noqa: E402
from src.context import ContextManager  # noqa: E402
from src.llm import OpenAICompatLLM, deepseek_llm  # noqa: E402
from src.scheduler import (DeadlineScheduler, DeadlineSchedulerV2,  # noqa: E402
                           DualTierLLM)
from src.tools import default_tools  # noqa: E402
from eval.tasks import TASKS  # noqa: E402

RESULTS_DIR = Path("eval/results")
TRACES_DIR = Path("eval/traces")


def warmup(llm: OpenAICompatLLM, label: str) -> None:
    """预热：Ollama 冷启动首调用 TTFT ~65s，不预热会污染首个任务的数据（AGENTS.md §4）。"""
    print(f"[warmup] {label} ...", end=" ", flush=True)
    t0 = time.perf_counter()
    llm.chat([{"role": "user", "content": "回复 ok"}])
    print(f"done ({time.perf_counter() - t0:.1f}s)")


def run_once(task: dict, llm, model: str, deadline: float | None, tag: str,
             scheduler=None, compressor=None) -> dict:
    """跑一个任务，返回单次运行记录（不含判分）。"""
    try:
        r = run(task["prompt"], llm=llm, tools=default_tools(),
                model=model, deadline=deadline, max_steps=8,
                scheduler=scheduler, compressor=compressor)
    except Exception as exc:  # 单任务失败不拖垮整个网格
        return {"tag": tag, "error": repr(exc), "answer": "", "total_time": None,
                "stop_reason": "error", "steps": 0, "wrap_up": False, "ttfts": []}
    ttfts = [e["ttft"] for e in r.trace.events if e["kind"] == "llm_call"]
    rec = {"tag": tag, "answer": r.answer, "total_time": r.total_time,
           "stop_reason": r.stop_reason, "steps": r.steps,
           "wrap_up": any(e["kind"] == "wrap_up" for e in r.trace.events),
           "ttfts": [round(t, 3) for t in ttfts], "error": None}
    r.trace.save(str(TRACES_DIR))  # trace 全量落盘（.gitignore 已排除）
    return rec


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser()
    ap.add_argument("--budgets", default="2,4,8,30", help="预算档（秒，逗号分隔）")
    ap.add_argument("--tasks", default="", help="任务子集（逗号分隔 id），默认全部 60 个")
    ap.add_argument("--with-strong", action="store_true",
                    help="加入 fixed_strong（deepseek-chat）基线，产生真实 API 费用")
    ap.add_argument("--dert-v1", action="store_true",
                    help="加入 dert_v1（双档路由，强档在宽预算下产生 API 费用）")
    ap.add_argument("--dert-v2", action="store_true",
                    help="加入 dert_v2（量化路由 + 收尾数值优先，同 v1 费用量级）")
    ap.add_argument("--compress", action="store_true",
                    help="加入 dert_full（v1.1 路由 + select 上下文压缩——"
                         "tasks-design 定义的完整 DeRT：路由+并行+压缩三件套）")
    ap.add_argument("--repeats", type=int, default=1,
                    help="每配置重复次数（噪声量化，tasks-design.md 上限 3）")
    args = ap.parse_args()
    repeats = max(1, args.repeats)

    budgets = [float(b) for b in args.budgets.split(",")]
    tasks = TASKS
    if args.tasks:
        keep = {t.strip() for t in args.tasks.split(",")}
        tasks = [t for t in TASKS if t["id"] in keep]

    strategies = ["fixed_fast", "dert"]
    if args.with_strong:
        strategies.append("fixed_strong")
    if args.dert_v1:
        strategies.append("dert_v1")
    if args.dert_v2:
        strategies.append("dert_v2")
    if args.compress:
        strategies.append("dert_full")

    # 客户端：全实时（缓存禁用），qwen 免费 / deepseek 受门控
    fast = OpenAICompatLLM(cache_dir="")
    strong = deepseek_llm(cache_dir="") if (args.with_strong or args.dert_v1
                                            or args.dert_v2
                                            or args.compress) else None
    duals = []  # (策略名, DualTierLLM, compressor)
    if strong is not None:
        if args.dert_v1:
            duals.append(("dert_v1",
                          DualTierLLM(fast, strong,
                                      DeadlineScheduler(fast.model, strong.model)),
                          None))
        if args.dert_v2:
            duals.append(("dert_v2",
                          DualTierLLM(fast, strong,
                                      DeadlineSchedulerV2(fast.model, strong.model)),
                          None))
        if args.compress:
            duals.append(("dert_full",
                          DualTierLLM(fast, strong,
                                      DeadlineScheduler(fast.model, strong.model)),
                          ContextManager(strategy="select")))
    print(f"fast={fast.model} | strong={'on' if strong else 'off'} "
          f"| dual={[n for n, _, _ in duals]} "
          f"| budgets={budgets} | tasks={len(tasks)} | repeats={repeats} | cache=off")

    warmup(fast, "fast")
    if strong:
        warmup(strong, "strong")

    records: list[dict] = []
    t_start = time.time()

    for task in tasks:
        tid = task["type"] + "/" + task["id"]

        # ---- 基线：每任务每重复 1 次（deadline=None），对每档预算分别判按时 ----
        for r in range(repeats):
            for strat, llm in (("fixed_fast", fast), ("fixed_strong", strong)):
                if llm is None:
                    continue
                rec = run_once(task, llm, llm.model, None,
                               f"{strat}:{tid}#r{r}")
                records.append({"task": tid, "strategy": strat, "budget": None,
                                "repeat": r, **rec})
                print(f"  [{strat:13s}] {tid:8s} r{r} stop={rec['stop_reason']:16s} "
                      f"t={rec['total_time'] and round(rec['total_time'], 2)}s "
                      f"steps={rec['steps']}")

        # ---- dert / dert_v1 / dert_v2：每预算每重复 1 次 ----
        for r in range(repeats):
            for b in budgets:
                rec = run_once(task, fast, fast.model, b, f"dert@{b:g}s:{tid}#r{r}")
                records.append({"task": tid, "strategy": "dert", "budget": b,
                                "repeat": r, **rec})
                print(f"  [dert@{b:g}s{'':6}] {tid:8s} r{r} stop={rec['stop_reason']:16s} "
                      f"t={rec['total_time'] and round(rec['total_time'], 2)}s "
                      f"steps={rec['steps']} wrap_up={rec['wrap_up']}")
        for name, dual, cmpr in duals:
            for r in range(repeats):
                for b in budgets:
                    rec = run_once(task, dual, None, b, f"{name}@{b:g}s:{tid}#r{r}",
                                   scheduler=dual.scheduler, compressor=cmpr)
                    records.append({"task": tid, "strategy": name,
                                    "budget": b, "repeat": r, **rec})
                    print(f"  [{name}@{b:g}s{'':6}] {tid:8s} r{r} stop={rec['stop_reason']:16s} "
                          f"t={rec['total_time'] and round(rec['total_time'], 2)}s "
                          f"steps={rec['steps']} wrap_up={rec['wrap_up']}")

    # ---- 判分与聚合 ----
    # success(task, strategy, budget) = 内容正确 且 耗时 <= budget
    grid: dict[tuple[str, float], list[dict]] = {}
    for strat in strategies:
        for b in budgets:
            grid[(strat, b)] = []

    for task in tasks:
        tid = task["type"] + "/" + task["id"]
        for strat in strategies:
            task_recs = [r for r in records
                         if r["task"] == tid and r["strategy"] == strat]
            for rec in task_recs:
                correct, why = task["check"](rec["answer"] or "")
                # 判定方式按记录本身有无 budget 决定，**不写死策略名**：
                # 2026-09-29 修 bug——原写法 strat in ("dert","dert_v1","dert_v2")
                # 漏了 9/26 新增的 dert_full，导致它被当成"与预算无关的基线"，
                # 720 条记录被塞进每个预算格（每格应为 180），正确率恒定成同一
                # 个假值。改为鸭子判断后，以后新增策略也不会重蹈覆辙。
                if rec["budget"] is not None:
                    # 预算感知策略（dert 系：每预算各跑一次）：只归入对应预算格
                    b = rec["budget"]
                    ot = rec["total_time"] is not None and rec["total_time"] <= b
                    grid[(strat, b)].append(
                        {"task": tid, "correct": correct, "on_time": ot,
                         "success": correct and ot, "why": why,
                         "total_time": rec["total_time"], "wrap_up": rec["wrap_up"],
                         "repeat": rec["repeat"]})
                else:
                    # 基线与预算无关：同一条记录对每档预算分别判按时
                    for b in budgets:
                        ot = (rec["total_time"] is not None
                              and rec["total_time"] <= b)
                        grid[(strat, b)].append(
                            {"task": tid, "correct": correct, "on_time": ot,
                             "success": correct and ot, "why": why,
                             "total_time": rec["total_time"],
                             "repeat": rec["repeat"]})

    # ---- 汇总表（跨重复成功率 mean ± std）----
    print("\n==== Pareto 网格（成功率 = 内容正确 且 按时；mean±std 跨重复）====")
    header = "strategy      " + "".join(f"{b:>12g}s" for b in budgets)
    print(header)
    summary = {}
    for strat in strategies:
        row = f"{strat:13s}"
        for b in budgets:
            cells = grid[(strat, b)]
            by_rep: dict[int, list[bool]] = {}
            for c in cells:
                by_rep.setdefault(c["repeat"], []).append(c["success"])
            rates = [sum(v) / len(v) for _, v in sorted(by_rep.items())]
            mean = statistics.mean(rates) if rates else 0.0
            std = statistics.stdev(rates) if len(rates) > 1 else 0.0
            row += f"{mean * 100:>6.0f}±{std * 100:<5.0f}"
            summary[f"{strat}@{b:g}s"] = {
                "mean": round(mean, 4), "std": round(std, 4),
                "repeats": [round(x, 4) for x in rates]}
        print(row)

    # 过程指标：正确率 vs 按时率拆开看（全重复合并）
    print("\n==== 拆解（correct% / on_time%，全重复合并）====")
    for strat in strategies:
        row = f"{strat:13s}"
        for b in budgets:
            cells = grid[(strat, b)]
            cp = sum(c["correct"] for c in cells) / len(cells) if cells else 0
            op = sum(c["on_time"] for c in cells) / len(cells) if cells else 0
            row += f"  {cp * 100:3.0f}/{op * 100:3.0f}"
        print(row)

    # ---- TTFT 分布（噪声量化：服务波动 + 采样噪声）----
    print("\n==== TTFT 分布（秒，P50 / P95 / max，n=调用数）====")
    ttft_stats = {}
    for strat in strategies:
        ttfts = sorted(t for r in records if r["strategy"] == strat
                       for t in (r.get("ttfts") or []))
        if not ttfts:
            continue

        def pct(xs, p):
            k = min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))
            return xs[k]

        ttft_stats[strat] = {
            "n": len(ttfts),
            "p50": round(pct(ttfts, 50), 3), "p95": round(pct(ttfts, 95), 3),
            "max": round(ttfts[-1], 3), "min": round(ttfts[0], 3)}
        s = ttft_stats[strat]
        print(f"  {strat:13s} n={s['n']:4d}  P50={s['p50']:.3f}  "
              f"P95={s['p95']:.3f}  max={s['max']:.3f}  min={s['min']:.3f}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"pareto_{datetime.now():%Y%m%d_%H%M}.json"
    out.write_text(json.dumps({
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "budgets": budgets, "strategies": strategies, "repeats": repeats,
        "n_tasks": len(tasks), "summary": summary,
        "ttft": ttft_stats,
        "grid": {f"{s}@{b:g}s": v for (s, b), v in grid.items()},
        "records": records,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nresults -> {out} | total {time.time() - t_start:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
