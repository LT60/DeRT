"""离线分析：budget_eval 产出的 raw.jsonl → 统计检验 + report.md。

设计原则：
    分析与采集分离——分析脚本可以反复重跑/修正，绝不重跑实验（测量红线：
    数字必须可溯源到 raw.jsonl）。纯标准库，无 scipy/numpy 依赖。

统计方法（任务书定案）：
    · 每格成功率：bootstrap 95% CI（按任务重采样 5000 次，seed=2026 固定）
    · 二值配对（success：correct 且按时）：McNemar 精确检验（不一致对计数，
      二项检验 p=0.5 双侧）
    · 1–5 质量分配对：sign test（配对差的正负号，二项精确检验），
      不用 t 检验——5 级序数变量不满足连续/正态假设
    · 时间-性能曲线：B 的 success 随预算变化，A/C 为水平参照线

停止条件的按档解释（2026-10-22 定案，见 budget_eval.py 模块 docstring）：
    3s 档：B 应显著优于 C 或接近 C（降级生效；B≈C 是对的）
    30/60s 档：B 应接近 A（上强档是正确行为）；B 显著差于 A 才是问题
    8s 档：临界区，路由决策最敏感，不预设方向

用法：
    python -u eval/analyze_budget.py eval/results/budget_<run_id>
    python -u eval/analyze_budget.py eval/results/budget_<run_id> --bootstrap 2000
"""
from __future__ import annotations

import json
import math
import random
import sys
from collections import Counter
from pathlib import Path


# ---------- 统计原语（纯标准库） ----------

def bootstrap_ci(vals: list[int], n_boot: int = 5000, seed: int = 2026,
                 alpha: float = 0.05) -> tuple[float, float, float]:
    """0/1 变量的均值 bootstrap 百分位 CI。返回 (mean, lo, hi)，均为百分数。"""
    if not vals:
        return 0.0, 0.0, 0.0
    rng = random.Random(seed)
    n = len(vals)
    means = sorted(sum(rng.choices(vals, k=n)) / n for _ in range(n_boot))
    lo = means[int(alpha / 2 * n_boot)]
    hi = means[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]
    return sum(vals) / n * 100, lo * 100, hi * 100


def mcnemar_exact(x_only: int, y_only: int) -> float:
    """McNemar 精确检验（双侧）：不一致对 x_only vs y_only，二项 p=0.5。"""
    n = x_only + y_only
    if n == 0:
        return 1.0
    k = min(x_only, y_only)
    pmf = lambda i: math.comb(n, i) * 0.5 ** n
    p = sum(pmf(i) for i in range(0, k + 1)) * 2
    return min(1.0, p)


def sign_test(wins: int, losses: int) -> float:
    """配对符号检验（双侧，平局不计）。"""
    return mcnemar_exact(wins, losses)


def fmt_p(p: float) -> str:
    return "<0.001" if p < 0.001 else f"{p:.3f}"


# ---------- 主流程 ----------

def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    run_dir = Path(sys.argv[1])
    n_boot = int(sys.argv[sys.argv.index("--bootstrap") + 1]) \
        if "--bootstrap" in sys.argv else 5000

    raw_path = run_dir / "raw.jsonl"
    env = json.loads((run_dir / "env.json").read_text(encoding="utf-8"))
    records = [json.loads(l) for l in
               raw_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    budgets = env["budgets"]
    run_id = env["run_id"]

    by_key = {(r["task"], r["strategy"],
               r["budget"] if r["strategy"] == "dert_v1" else None): r
              for r in records}
    task_ids = sorted({r["task"] for r in records})
    print(f"run={run_id}  tasks={len(task_ids)}  records={len(records)}")
    if len(records) < env.get("n_planned_runs", 0) * 0.99:
        print(f"⚠ 记录数少于计划（{env.get('n_planned_runs')}），结果按实际记录计算")

    # ---- 每格指标 + bootstrap CI ----
    def cell_recs(strat: str, b: float) -> list[dict]:
        if strat == "dert_v1":
            return [r for r in records if r["strategy"] == strat
                    and r["budget"] == b]
        return [r for r in records if r["strategy"] == strat]

    cells = {}
    for strat in ("fixed_strong", "dert_v1", "fixed_fast"):
        for b in budgets:
            rs = cell_recs(strat, b)
            if strat == "dert_v1":
                succ = [int(bool(r["correct"]) and bool(r["on_time"])) for r in rs]
                corr = [int(r["correct"]) for r in rs]
                ot = [int(bool(r["on_time"])) for r in rs]
            else:
                succ = [int(r["correct"] and r["total_time"] is not None
                            and r["total_time"] <= b) for r in rs]
                corr = [int(r["correct"]) for r in rs]
                ot = [int(r["total_time"] is not None and r["total_time"] <= b)
                      for r in rs]
            scores = [r["quality_score"] for r in rs if r["quality_score"]]
            times = sorted(r["total_time"] for r in rs
                           if r["total_time"] is not None)
            m, lo, hi = bootstrap_ci(succ, n_boot=n_boot)
            cells[(strat, b)] = {
                "n": len(rs),
                "correct": sum(corr) / len(rs) * 100 if rs else 0,
                "on_time": sum(ot) / len(rs) * 100 if rs else 0,
                "success": m, "lo": lo, "hi": hi,
                "quality_mean": (sum(scores) / len(scores)) if scores else None,
                "quality_ge4": (sum(s >= 4 for s in scores) / len(scores) * 100)
                               if scores else None,
                "p50": times[len(times) // 2] if times else None,
                "p95": times[min(len(times) - 1, int(0.95 * (len(times) - 1)))]
                       if times else None,
            }

    # ---- 配对检验（success 口径）----
    def succ_of(r: dict, b: float) -> bool:
        if r is None:
            return False
        if r["strategy"] == "dert_v1":
            return bool(r["correct"]) and bool(r["on_time"])
        return bool(r["correct"]) and r["total_time"] is not None \
            and r["total_time"] <= b

    pairs = {}
    for b in budgets:
        for name, x, y in (("B_vs_A", "dert_v1", "fixed_strong"),
                           ("B_vs_C", "dert_v1", "fixed_fast"),
                           ("A_vs_C", "fixed_strong", "fixed_fast")):
            x_only = y_only = both = neither = 0
            qw = ql = qt = 0
            for tid in task_ids:
                ry = by_key.get((tid, y, b if y == "dert_v1" else None))
                rx = by_key.get((tid, x, b if x == "dert_v1" else None))
                if not rx or not ry:
                    continue
                sx, sy = succ_of(rx, b), succ_of(ry, b)
                if sx and sy:
                    both += 1
                elif sx:
                    x_only += 1
                elif sy:
                    y_only += 1
                else:
                    neither += 1
                qxs = rx.get("quality_score")
                qys = ry.get("quality_score")
                if qxs and qys:
                    if qxs > qys:
                        qw += 1
                    elif qxs < qys:
                        ql += 1
                    else:
                        qt += 1
            pairs[(name, b)] = {
                "n": both + x_only + y_only + neither,
                "x_only": x_only, "y_only": y_only, "both": both,
                "neither": neither,
                "p_mcnemar": mcnemar_exact(x_only, y_only),
                "q_wins": qw, "q_losses": ql, "q_ties": qt,
                "p_sign": sign_test(qw, ql),
            }

    # ---- 过程指标 ----
    stop_dist = {s: Counter(r["stop_reason"] for r in records
                            if r["strategy"] == s)
                 for s in ("fixed_strong", "dert_v1", "fixed_fast")}
    wrap_rate = {}
    switch_mean = {}
    for s in ("fixed_strong", "dert_v1", "fixed_fast"):
        rs = [r for r in records if r["strategy"] == s]
        wrap_rate[s] = sum(r["wrap_up"] for r in rs) / len(rs) * 100 if rs else 0
        switch_mean[s] = (sum(r["switch_count"] for r in rs) / len(rs)) if rs else 0
    b_tier = {}
    for b in budgets:
        rs = [r for r in records if r["strategy"] == "dert_v1" and r["budget"] == b]
        strong_calls = sum(1 for r in rs
                           for m in r.get("models", [])
                           if m == env["strong"]["model"])
        total_calls = sum(len(r.get("models", [])) for r in rs)
        b_tier[b] = {"strong_call_pct": round(strong_calls / total_calls * 100, 1)
                     if total_calls else 0,
                     "wrap_up_pct": round(sum(r["wrap_up"] for r in rs)
                                          / len(rs) * 100, 1) if rs else 0}

    # ---- report.md ----
    lines = []
    w = lines.append
    w(f"# DeRT 预算-性能评估报告（run `{run_id}`）")
    w("")
    w(f"- 采集时间：{env['started_at']} → {env['finished_at']}"
      f"（共 {len(records)} 次运行，{len(task_ids)} 任务）")
    w(f"- A 强档：`{env['strong']['model']}`（不感知预算，硬上限 {env['hard_cap_A']:g}s）"
      f"；B：DeRT 调度器 v1.1；C 快档：`{env['fast']['model']}`（不感知预算）")
    w(f"- 预算档：{budgets} 秒（3s 在路由阈值 {env['scheduler_v1.1']['STRONG_REMAINING_MIN']}s "
      f"之下，8s 临界，30/60s 宽松——设计依据见 budget_eval.py）")
    w("- 设计：N 任务配对（每任务 6 次运行，交替轮转）；缓存全关；temp=0")
    w("- 质量：deepseek judge（temp=0，1–5 rubric，参考答案=工具真实数据），"
      "与规则判分独立记录")
    w("")
    w("## 主结果：success% = 内容正确 且 按时交付（bootstrap 95% CI）")
    w("")
    header = "| 策略 | " + " | ".join(f"{b:g}s" for b in budgets) + " |"
    w(header)
    w("|" + "---|" * (len(budgets) + 1))
    for strat, label in (("fixed_strong", "A 固定强档"),
                         ("dert_v1", "**B DeRT v1.1**"),
                         ("fixed_fast", "C 固定快档")):
        row = f"| {label} |"
        for b in budgets:
            c = cells[(strat, b)]
            row += f" {c['success']:.0f} [{c['lo']:.0f},{c['hi']:.0f}] (n={c['n']}) |"
        w(row)
    w("")
    w("拆解：correct% / on_time%（同批记录）")
    w("")
    w(header)
    w("|" + "---|" * (len(budgets) + 1))
    for strat, label in (("fixed_strong", "A 固定强档"),
                         ("dert_v1", "B DeRT v1.1"),
                         ("fixed_fast", "C 固定快档")):
        row = f"| {label} |"
        for b in budgets:
            c = cells[(strat, b)]
            row += f" {c['correct']:.0f} / {c['on_time']:.0f} |"
        w(row)
    w("")
    w("## 时间-性能关系（本研究核心问题）")
    w("")
    w("| 预算 | B success% | A% | C% | B 强档调用占比 | B wrap_up 触发率 |")
    w("|---|---|---|---|---|---|")
    for b in budgets:
        c = cells[("dert_v1", b)]
        bt = b_tier[b]
        w(f"| {b:g}s | {c['success']:.0f} | {cells[('fixed_strong', b)]['success']:.0f} "
          f"| {cells[('fixed_fast', b)]['success']:.0f} "
          f"| {bt['strong_call_pct']}% | {bt['wrap_up_pct']}% |")
    w("")
    w("## 配对检验（success 口径，McNemar 精确检验）")
    w("")
    w("| 预算 | 比较 | 不一致对 (x/y) | p 值 | 质量分符号检验 (W/L/T, p) |")
    w("|---|---|---|---|---|")
    for b in budgets:
        for name, xl, yl in (("B_vs_A", "B vs A", "B>A"),
                             ("B_vs_C", "B vs C", "B>C"),
                             ("A_vs_C", "A vs C", "A>C")):
            p = pairs[(name, b)]
            w(f"| {b:g}s | {xl} | {p['x_only']} / {p['y_only']} (n={p['n']}) "
              f"| {fmt_p(p['p_mcnemar'])} "
              f"| {p['q_wins']}/{p['q_losses']}/{p['q_ties']}, "
              f"{fmt_p(p['p_sign'])} |")
    w("")
    w("## 过程指标")
    w("")
    w("| 策略 | stop_reason 分布 | wrap_up 触发率 | 平均换档次数 |")
    w("|---|---|---|---|")
    for s, label in (("fixed_strong", "A"), ("dert_v1", "B"), ("fixed_fast", "C")):
        w(f"| {label} | {dict(stop_dist[s])} | {wrap_rate[s]:.0f}% "
          f"| {switch_mean[s]:.2f} |")
    w("")
    w("## 质量分（1–5，deepseek judge）")
    w("")
    w("| 策略 | " + " | ".join(f"{b:g}s" for b in budgets) + " |")
    w("|" + "---|" * (len(budgets) + 1))
    for strat, label in (("fixed_strong", "A"), ("dert_v1", "B"),
                         ("fixed_fast", "C")):
        row = f"| {label} |"
        for b in budgets:
            c = cells[(strat, b)]
            row += (f" {c['quality_mean']:.2f} / ≥4占{c['quality_ge4']:.0f}% |"
                    if c["quality_mean"] is not None else " - |")
        w(row)
    w("")
    w("## 按预算档解释（2026-10-22 定案的停止条件）")
    w("")
    p_ba_3 = pairs[("B_vs_A", 3.0)]["p_mcnemar"] if 3.0 in budgets else None
    w("- **3s 档**：B 在路由阈值之下，应降级快档——若 B 显著优于 C，说明"
      "「预算感知 + 强档兜底」在极紧预算下换来了正确率；若 B≈C 属预期。")
    w("- **30/60s 档**：B 应接近 A（上强档是正确行为）；B 显著差于 A 才说明"
      "调度开销有害。B≈A 在此**不是**「降级没生效」，是构造使然的预期结果。")
    w("- **8s 档**：临界区，观察 B 相对 A/C 的位置即可，不预设方向。")
    w("")
    w("## 局限（对外引用时必须随附）")
    w("")
    w("1. 任务集为自建合成集（实时检测系统运维域，8 个模拟工具），"
      "未接外部公开基准；BFCL v3 多轮（800 题，外部验证）列为后续工作。")
    w("2. judge 与规则判分独立，但 judge 本身也是 LLM（deepseek, temp=0），"
      "存在系统性偏好风险；规则判分（correct）不经过 judge，可直接引用。")
    w("3. 单机单卡环境（本地 7b + 云端 deepseek），结论不外推到其他后端组合。")
    w("4. 每格每任务 1 次运行，任务数即样本量（N=任务数）；"
      "单任务内的采样噪声未重复测量（原 60 任务网格已有 3 重复数据可交叉参照）。")
    w("")
    w("## 复现")
    w("")
    w("```bash")
    w(f"python -u eval/budget_eval.py --run-id {run_id}   # 采集（可断点续跑）")
    w(f"python -u eval/analyze_budget.py eval/results/budget_{run_id}")
    w("```")
    w("")
    w("*本报告由 eval/analyze_budget.py 从 raw.jsonl 离线生成；"
      "所有数字可溯源到逐条运行记录。*")

    report = "\n".join(lines) + "\n"
    (run_dir / "report.md").write_text(report, encoding="utf-8")

    # 机器可读统计（与 harness 的 summary.json 分开，避免覆盖采集端快照）
    stats = {
        "cells": {f"{s}@{b:g}s": c for (s, b), c in cells.items()},
        "pairs": {f"{n}@{b:g}s": p for (n, b), p in pairs.items()},
        "b_tier": {str(k): v for k, v in b_tier.items()},
        "process": {"stop_dist": {k: dict(v) for k, v in stop_dist.items()},
                    "wrap_rate": wrap_rate, "switch_mean": switch_mean},
    }
    (run_dir / "summary_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n==== success%（CI95）====")
    for strat in ("fixed_strong", "dert_v1", "fixed_fast"):
        row = f"{strat:13s}"
        for b in budgets:
            c = cells[(strat, b)]
            row += f"  {c['success']:5.1f} [{c['lo']:.0f},{c['hi']:.0f}]"
        print(row)
    print("\n==== McNemar（B_vs_C / B_vs_A）====")
    for b in budgets:
        for name in ("B_vs_A", "B_vs_C"):
            p = pairs[(name, b)]
            print(f"  {b:>5g}s {name}: x={p['x_only']} y={p['y_only']} "
                  f"p={fmt_p(p['p_mcnemar'])}")
    print(f"\nreport -> {run_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
