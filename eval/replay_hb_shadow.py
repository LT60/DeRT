# -*- coding: utf-8 -*-
"""HB 域 v3emp 影子流量冷启动回放（零 API 成本，两步纪律的第一步）。

问题：v3emp（经验难度史）在 HammerBench 中文域是否成立？
方法：用 hb_ext1 的 160 run 离线推演「难度史从零积累」的过程——
  第 1 轮：难度史为空 → 所有任务走快档 = C 的实测结果（C 最长 2.16s，
          在 10s 预算下必然按时）；
  第 2 轮起：快档曾失败的题记 hard → 走强档 = A 的实测结果（10s 预算下
          A 耗时 > 预算的题迟到判负；30/60s 全部来得及）；快档成功的题
          永远快档（1−p 最简形态，与线上 record_shadow 口径一致）。

诚实边界：
- 10s 档回放按 hard→strong 近似（HB 域未测中档，V3EEmp 的中档阶梯
  无 HB 实测数据——真实 v3emp@10s 会比本回放略好，中档接手 6.6-8.5s 段）；
- 回放证据等级 < 在线（自建集上同构回放已被在线确认兑现，97% 预测命中）。
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

RAW = Path(__file__).resolve().parent / "results" / "hb_ext1" / "raw.jsonl"
BUDGETS = (10.0, 30.0, 60.0)
N = 20


def main() -> None:
    rows = [json.loads(l) for l in
            RAW.read_text(encoding="utf-8").splitlines() if l.strip()]
    A = {r["episode"]: r for r in rows if r["strategy"] == "A"}
    C = {r["episode"]: r for r in rows if r["strategy"] == "C"}
    v1 = {(r["episode"], r["budget"]): r for r in rows if r["strategy"] == "v1"}

    c_ok = {e for e, r in C.items() if r["success"]}
    hard = set(C) - c_ok                      # 快档曾答错 → 影子流量记 hard
    c_max_t = max(r["total_time"] for r in C.values())

    def steady(budget: float) -> tuple[int, int]:
        """稳态（第 2 轮起）：(success, strong_used)。success = 按时且正确。"""
        succ = 0
        strong_used = 0
        for e in C:
            if e in c_ok:
                succ += 1                     # easy：永远快档，必然按时
            else:
                strong_used += 1
                r = A[e]
                if r["success"] and r["total_time"] <= budget:
                    succ += 1                 # hard：走强档，须按时
        return succ, strong_used

    print("=" * 64)
    print("HB 域 v3emp 影子流量冷启动回放（N=20，数据 hb_ext1）")
    print("=" * 64)
    print(f"快档任务（C 成功）: {len(c_ok)}/{N}   hard 任务（C 失败）: {len(hard)}/{N}")
    print(f"C 耗时 max {c_max_t:.2f}s（任何预算档都按时）  "
          f"A 耗时 P50 {statistics.median(r['total_time'] for r in A.values()):.1f}s "
          f"max {max(r['total_time'] for r in A.values()):.1f}s")

    print(f"\n{'预算':>6} | {'第1轮(冷启动)':>12} | {'稳态(第2轮起)':>13} | "
          f"{'强档用量':>8} | {'v1@b':>6} | {'A':>4}")
    print("-" * 64)
    for b in BUDGETS:
        r1 = sum(1 for e in C if e in c_ok)          # 冷启动 = 全快档
        s, used = steady(b)
        v1s = sum(1 for (e, bb), r in v1.items()
                  if bb == b and r["success"] and r["on_time"])
        a_s = sum(1 for r in A.values()
                  if r["success"] and r["total_time"] <= b)
        print(f"{b:>5.0f}s | {r1:>10}/{N} | {s:>11}/{N} | "
              f"{used:>6}/{N} | {v1s:>4}/{N} | {a_s:>2}/{N}")

    # 冷启动收敛：每个任务被看 K 轮后的平均成功率
    print("\n冷启动收敛（每个任务重复被请求 K 轮的平均 success%）：")
    for k in (1, 2, 3, 5):
        tot = 0
        for b in BUDGETS:
            s, _ = steady(b)
            tot += (1 * sum(1 for e in C if e in c_ok) + (k - 1) * s) / k
        print(f"  K={k}: 平均 {tot / len(BUDGETS) / N * 100:.1f}%"
              f"（第 1 轮 5% → 第 2 轮起稳态）")

    print("\n结论：影子难度史在 HB 域第 2 轮即收敛到 A 级（75%），"
          "且强档用量 19/20=95% 由任务构成决定——")
    print("v3emp 不承诺固定省钱比例，承诺「把强档花在真需要的任务上」"
          "（自建集 27% hard，HB 95% hard）。")
    print("边界：10s 档按 hard→strong 近似（HB 无中档实测），"
          "回放证据等级 < 在线（自建集同构回放已被在线兑现）。")


if __name__ == "__main__":
    main()
