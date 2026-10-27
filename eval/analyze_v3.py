# -*- coding: utf-8 -*-
"""v3/v3b 难度感知路由的在线对照分析：dert_v3、dert_v3b vs v1/A/C（success 口径）。

v3  = 难度路由 + 时间门（remaining>=8s 才放行强档）——首测被游戏污染，62.4% 不作数
v3b = 纯难度路由（不看时间），预算含 30/60s——本分析的干净复测对象

预期 headline（回放预测，对 v3b 同构）：correct ≈89%，强档用量 ≈31%，平均耗时 ≈5.5s。
本脚本另报 quality 口径（不看时间，纯 correct%）——那是难度路由的主战场。
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_budget import mcnemar_exact  # noqa: E402

RUN_DIR = Path(__file__).resolve().parent / "results" / "budget_sweep2"
BUDGETS = [2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0]
V3B_BUDGETS = BUDGETS + [30.0, 60.0]
BUDGETED = ("dert_v1", "dert_v2", "dert_v3", "dert_v3b")
CMP_WORDS = ("是否超过", "SLO", "红线", "对照", "是否达标", "超过")


def success_v(r: dict) -> bool:
    return bool(r["correct"]) and bool(r["on_time"])


def success_ac(r: dict, b: float) -> bool:
    return bool(r["correct"]) and r["total_time"] is not None and r["total_time"] <= b


def mcnemar_pairs(rs_v, rs_ref):
    """逐任务配对 correct 口径；返回 (x, y, n)：x=v3系对 ref 翻盘数，y=反方向。"""
    x = y = n = 0
    by_task = {r["task"]: r for r in rs_ref}
    for rv in rs_v:
        ra = by_task.get(rv["task"])
        if not ra:
            continue
        n += 1
        if rv["correct"] and not ra["correct"]:
            x += 1
        elif ra["correct"] and not rv["correct"]:
            y += 1
    return x, y, n


def main() -> None:
    recs = [json.loads(l) for l in
            (RUN_DIR / "raw.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    runs = {}
    for r in recs:
        key = (r["task"], r["strategy"],
               r["budget"] if r["strategy"] in BUDGETED else None)
        runs[key] = r
    tasks = sorted({k[0] for k in runs})
    print(f"records={len(recs)} tasks={len(tasks)}")

    A = [runs.get((t, "fixed_strong", None)) for t in tasks]
    C = [runs.get((t, "fixed_fast", None)) for t in tasks]

    def v_stat(rs):
        rs = [r for r in rs if r]
        if not rs:
            return None, None, None
        return (100.0 * sum(success_v(r) for r in rs) / len(rs),
                100.0 * sum(r["correct"] for r in rs) / len(rs),
                100.0 * sum(1 for r in rs if any("deepseek" in m for m in r["models"])) / len(rs))

    def ac_stat(rs, b):
        rs = [r for r in rs if r]
        return (100.0 * sum(success_ac(r, b) for r in rs) / len(rs),
                100.0 * sum(r["correct"] for r in rs) / len(rs), None) if rs else (None,) * 3

    table = {}
    print("\n==== success% 与 correct% / 强档调用率 ====")
    print("budget    A_suc   C_suc    v3_suc  v3_corr v3_str%   v3b_suc v3b_corr v3b_str%")
    for b in V3B_BUDGETS:
        a = ac_stat(A, b)
        c = ac_stat(C, b)
        s, cor, stg = v_stat([runs.get((t, "dert_v3", b)) for t in tasks])
        sb, corb, stgb = v_stat([runs.get((t, "dert_v3b", b)) for t in tasks])
        table[b] = {"A": a[0], "C": c[0],
                    "v3_suc": s, "v3_corr": cor, "v3_strong": stg,
                    "v3b_suc": sb, "v3b_corr": corb, "v3b_strong": stgb}
        print(f"  {b:4.0f}s {a[0]:6.1f} {c[0]:6.1f}  {s:8.1f} {cor:8.1f} {stg:8.1f}"
              f"  {sb:8.1f} {corb:8.1f} {stgb:8.1f}")

    # 质量口径（correct，不看时间）与强档用量
    v3_all = [r for r in recs if r["strategy"] == "dert_v3"]
    v3b_all = [r for r in recs if r["strategy"] == "dert_v3b"]
    A_all = [r for r in recs if r["strategy"] == "fixed_strong"]
    C_all = [r for r in recs if r["strategy"] == "fixed_fast"]
    v1_all = [r for r in recs if r["strategy"] == "dert_v1"]
    q = lambda rs: 100.0 * sum(r["correct"] for r in rs) / len(rs)
    strong = lambda rs: 100.0 * sum(1 for r in rs if any("deepseek" in m for m in r["models"])) / len(rs)
    tt = lambda rs: statistics.median(r["total_time"] for r in rs if r["total_time"])
    print("\n==== 全局质量口径（correct%，不看时间）====")
    print(f"  A 全强档: {q(A_all):.1f}% | 强档 {strong(A_all):.0f}% | 中位耗时 {tt(A_all):.2f}s")
    print(f"  C 全快档: {q(C_all):.1f}% | 强档 {strong(C_all):.0f}% | 中位耗时 {tt(C_all):.2f}s")
    print(f"  v1.1:     {q(v1_all):.1f}% | 强档 {strong(v1_all):.0f}%")
    print(f"  v3:       {q(v3_all):.1f}% | 强档 {strong(v3_all):.0f}% | 中位耗时 {tt(v3_all):.2f}s  n={len(v3_all)}")
    print(f"  v3b:      {q(v3b_all):.1f}% | 强档 {strong(v3b_all):.0f}% | 中位耗时 {tt(v3b_all):.2f}s  n={len(v3b_all)}")

    # 难度分组（v3b，用路由器同款信号 = 真实 prompt 文本，与 set_prompt 口径一致）
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from eval.tasks import TASKS  # noqa: E402
    prompt_of = {f"{t['type']}/{t['id']}": t["prompt"] for t in TASKS}

    def is_hard(r):
        return any(w in prompt_of.get(r["task"], "") for w in CMP_WORDS)
    print("\n==== 难度分组（v3b，按路由器同款 prompt 信号）====")
    for label, pick in (("硬（含对比词）", is_hard), ("易（不含）", lambda r: not is_hard(r))):
        g = [r for r in v3b_all if pick(r)]
        if g:
            gs = [r for r in g if any("deepseek" in m for m in r["models"])]
            gf = [r for r in g if not any("deepseek" in m for m in r["models"])]
            print(f"  {label}: n={len(g)} correct={q(g):.1f}% strong={strong(g):.1f}%"
                  f" | 走强档 correct={q(gs) if gs else float('nan'):.1f}% (n={len(gs)})"
                  f" | 走快档 correct={q(gf) if gf else float('nan'):.1f}% (n={len(gf)})")
    g_loose = [r for r in v3b_all if is_hard(r) and r["budget"] in (30.0, 60.0)]
    g_tight = [r for r in v3b_all if is_hard(r) and r["budget"] <= 6.0]
    if g_loose:
        print(f"  硬任务@30/60s: n={len(g_loose)} correct={q(g_loose):.1f}% strong={strong(g_loose):.1f}%")
    if g_tight:
        print(f"  硬任务@2-6s:   n={len(g_tight)} correct={q(g_tight):.1f}% strong={strong(g_tight):.1f}%")

    # McNemar（correct 口径，逐任务配对；全局 + 每预算档）
    print("\n==== McNemar（correct 口径，逐任务）====")
    for name, rs_v in (("v3_vs_C", v3_all), ("v3_vs_A", v3_all),
                       ("v3b_vs_C", v3b_all), ("v3b_vs_A", v3b_all)):
        ref = C_all if name.endswith("_C") else A_all
        x, y, n = mcnemar_pairs(rs_v, ref)
        print(f"  {name}: {x}/{y} (n={n}) p={mcnemar_exact(x, y):.3g}")
    per_budget_mcn = {}
    print("\n==== McNemar v3b_vs_C 每预算档 ====")
    for b in V3B_BUDGETS:
        rs_v = [runs.get((t, "dert_v3b", b)) for t in tasks]
        rs_v = [r for r in rs_v if r]
        ref = [runs.get((t, "fixed_fast", None)) for t in tasks]
        x, y, n = mcnemar_pairs(rs_v, [r for r in ref if r])
        per_budget_mcn[str(b)] = {"x": x, "y": y, "n": n, "p": mcnemar_exact(x, y)}
        print(f"  @{b:4.0f}s: {x}/{y} (n={n}) p={mcnemar_exact(x, y):.3g}")

    dest = RUN_DIR / "v3_analysis.json"
    dest.write_text(json.dumps(
        {"table": {str(b): table[b] for b in V3B_BUDGETS},
         "quality": {
             "A": {"correct": q(A_all), "strong": strong(A_all), "median_tt": tt(A_all)},
             "C": {"correct": q(C_all), "strong": strong(C_all), "median_tt": tt(C_all)},
             "v1": {"correct": q(v1_all), "strong": strong(v1_all)},
             "v3": {"correct": q(v3_all), "strong": strong(v3_all), "median_tt": tt(v3_all), "n": len(v3_all)},
             "v3b": {"correct": q(v3b_all), "strong": strong(v3b_all), "median_tt": tt(v3b_all), "n": len(v3b_all)},
         },
         "mcnemar": {name: {"x": x, "y": y, "n": n, "p": mcnemar_exact(x, y)}
                     for name, rs_v in (("v3_vs_C", v3_all), ("v3b_vs_C", v3b_all), ("v3b_vs_A", v3b_all))
                     for x, y, n in [mcnemar_pairs(rs_v, C_all if name.endswith("_C") else A_all)]},
         "mcnemar_v3b_vs_C_per_budget": per_budget_mcn},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"-> {dest}")


if __name__ == "__main__":
    sys.exit(main())
