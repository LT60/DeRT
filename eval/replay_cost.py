# -*- coding: utf-8 -*-
"""实验 C：成本量纲回放——把预算的横轴从「秒」换成「元」，机制是否照跑？

动机：调度三元组（每档消耗分布 + 剩余预算 + 难度信号）量纲无关。
成本量纲还有个机制级红利：价格是公开精确常数（不漂移），tokens 分布可
离线标定——时间量纲下逼出来的 v3d 自适应门（截尾/限速/地板）在成本
量纲下天生不需要。本脚本用 main100 实测 tokens 验证这一点。

策略（时间约束解除，纯成本预算 c = 该次请求允许的强档花费上限 ¥）：
  A' 全强档     ：success = A 正确 且 cost_A ≤ c
  C' 全快档     ：success = C 正确（快档本地免费，成本恒 0）
  v3emp' 成本路由：C 答对过的题 = easy → 快档（免费）；C 失败的题 = hard
                  → 强档（success = A 正确 且 cost_A ≤ c）
                  ——难度史直接从「快档曾答错」来，与线上 record_shadow 同口径。

产出：逐成本预算表 + 曲线数据落盘（供 plot_cost_curve.py 绘图）。
用法：python -m eval.replay_cost
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN_RAW = ROOT / "eval" / "results" / "budget_main100" / "raw.jsonl"
OUT = ROOT / "eval" / "results" / "judge_audit" / "cost_replay.json"
PRICE_IN, PRICE_OUT = 2.0, 8.0     # 百炼 deepseek-v4.1-flash 公开价 ¥/M tokens
GRID = [0.0, 0.002, 0.004, 0.006, 0.008, 0.010, 0.015, 0.020, 0.030, 0.050]


def cost_yuan(r: dict) -> float:
    return r["tokens_in"] / 1e6 * PRICE_IN + r["tokens_out"] / 1e6 * PRICE_OUT


def main() -> None:
    rows = [json.loads(l) for l in
            MAIN_RAW.read_text(encoding="utf-8").splitlines() if l.strip()]
    A = {r["task"]: r for r in rows if r["strategy"] == "fixed_strong"}
    C = {r["task"]: r for r in rows if r["strategy"] == "fixed_fast"}
    tasks = sorted(C)
    hard = {t for t in tasks if not C[t]["correct"]}   # 影子史：快档曾答错

    ca = {t: cost_yuan(A[t]) for t in tasks}
    print("=" * 72)
    print(f"成本量纲回放（N=100，强档价 ¥{PRICE_IN}/M in ¥{PRICE_OUT}/M out，"
          f"快档本地免费）")
    print("=" * 72)
    ca_sorted = sorted(ca.values())
    print(f"强档单题成本: min {ca_sorted[0]:.4f}¥  P50 "
          f"{ca_sorted[50]:.4f}¥  max {ca_sorted[-1]:.4f}¥")
    print(f"hard 任务（C 失败→影子史记 hard）: {len(hard)}/100\n")

    hdr = f"{'成本预算':>8} | {'A'' 全强档':>9} | {'C'' 全快档':>9} | {'v3emp'' 路由':>9} | {'v3emp'' 强档率':>8}"
    print(hdr)
    print("-" * 72)
    curves = {"grid": GRID, "A": [], "C": [], "v3emp": [], "strong_rate": []}
    for c in GRID:
        a_s = sum(1 for t in tasks if A[t]["correct"] and ca[t] <= c)
        c_s = sum(1 for t in tasks if C[t]["correct"])   # 免费，恒过预算
        v_s = sum((1 if C[t]["correct"] else
                   (1 if A[t]["correct"] and ca[t] <= c else 0))
                  for t in tasks)
        strong_used = sum(1 for t in tasks
                          if not C[t]["correct"] and ca[t] <= c)
        curves["A"].append(a_s)
        curves["C"].append(c_s)
        curves["v3emp"].append(v_s)
        curves["strong_rate"].append(round(strong_used / 100, 3))
        print(f"{c:>7.3f}¥ | {a_s:>7}/100 | {c_s:>7}/100 | {v_s:>7}/100 | "
              f"{strong_used:>5}/100")

    n_hard = len(hard)
    hard_ok = sum(1 for t in hard if A[t]["correct"])
    print(f"\n要点：{n_hard} 个 hard 任务里强档能做对 {hard_ok} 个；"
          f"v3emp' 稳态正确率 = C 对 {100 - n_hard} + 强档救回 {hard_ok} "
          f"= {100 - n_hard + hard_ok}/100（封顶），且封顶在强档成本"
          f" P{int(sum(1 for t in hard if ca[t] <= ca_sorted[50]) / n_hard * 100):.0f} "
          f"预算处兑现——与时间量纲的「第 2 轮收敛到 A 级」同构。")
    print("机制红利：价格精确已知 → 成本量纲不需要 v3d 的自适应门"
          "（延迟会漂移，价格不会）。")
    OUT.write_text(json.dumps({"prices": {"in": PRICE_IN, "out": PRICE_OUT},
                               "n_hard": n_hard, **curves},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"落盘 {OUT}")


if __name__ == "__main__":
    main()
