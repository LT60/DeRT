# -*- coding: utf-8 -*-
"""v3emp（经验难度）在线确认分析 + 「又快又准」曲线图（2026-10-25）。

直接读 sweep2 raw.jsonl（含新旧全部策略），不依赖 budget_eval 的 flags：
  表1  各策略 × 预算档 success%（correct 且 on_time）
  表2  v3emp 配对 McNemar（vs v3d / A / C / v1）
  表3  强档用量%（models 序列中强档调用占比）与耗时中位——「又快又准」的量化
  图   docs/figures/v3emp_curve.png（success 曲线 + 强档用量/耗时副面板）

用法（miniconda python）：python eval/analyze_v3emp.py
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SWEEP2 = ROOT / "results" / "budget_sweep2"
FIG = ROOT.parent / "docs" / "figures"

BUDGETS = [2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 30.0, 60.0]
STRATS = ["fixed_strong", "fixed_fast", "dert_v1", "dert_v3d", "dert_v3emp"]


def mcnemar_exact(b: int, c: int) -> float:
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(0, k + 1)) / 2 ** n)


def load():
    recs = [json.loads(l) for l in
            (SWEEP2 / "raw.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()]
    env = json.loads((SWEEP2 / "env.json").read_text(encoding="utf-8"))
    strong_name = (env.get("strong") or {}).get("model", "")
    by = {(r["task"], r["strategy"], r["budget"]): r for r in recs}
    # A/C 不感知预算：唯一一次运行挂在 budget=None 上，按档现算 on_time
    ac: dict[tuple, dict] = {}
    for (t, s, bb), r in by.items():
        if s in ("fixed_strong", "fixed_fast") and bb is None:
            for b in BUDGETS:
                ac[(t, s, b)] = {**r, "on_time": r["total_time"] <= b}
    by.update(ac)
    return recs, by, strong_name


def success(by, strat: str, b):
    out = {}
    for (t, s, bb), r in by.items():
        if s == strat and bb == b and r.get("total_time") is not None:
            out[t] = bool(r["correct"]) and r["total_time"] <= b
    return out


def main():
    recs, by, strong_name = load()
    print(f"records={len(recs)}  strong={strong_name}")

    # ---- 表1 success% ----
    print("\n==== success%（correct 且按时）====")
    succ = {}
    print(f"{'strategy':14s} " + "".join(f"{b:>7g}s" for b in BUDGETS))
    for s in STRATS:
        row = []
        for b in BUDGETS:
            d = success(by, s, b)
            v = 100 * sum(d.values()) / len(d) if d else float("nan")
            succ[(s, b)] = v
            row.append(v)
        print(f"{s:14s} " + "".join(f"{v:>7.1f}" for v in row))

    # ---- 表2 McNemar（success 口径）----
    print("\n==== v3emp 配对 McNemar ====")
    pairs = {}
    for b in BUDGETS:
        e = success(by, "dert_v3emp", b)
        for ref_name, ref_s in (("v3d", "dert_v3d"), ("A", "fixed_strong"),
                                ("C", "fixed_fast"), ("v1", "dert_v1")):
            r = success(by, ref_s, b) if ref_s.startswith("dert") else \
                success(by, ref_s, b)
            common = set(e) & set(r)
            x = sum(1 for t in common if e[t] and not r[t])
            y = sum(1 for t in common if r[t] and not e[t])
            p = mcnemar_exact(x, y)
            pairs[f"v3emp_vs_{ref_name}@{b:g}"] = {"x_only": x, "y_only": y,
                                                   "n": len(common), "p": round(p, 4)}
            if x + y:
                print(f"  @{b:>4g}s vs {ref_name:4s} +{x}/-{y}  p={p:.4f}")

    # ---- 表3 强档用量 + 耗时 ----
    print("\n==== 强档用量% / 耗时中位（又快又准的量化）====")
    usage = {}
    print(f"{'strategy':14s} " + "".join(f"{b:>11g}s" for b in BUDGETS))
    for s in ("fixed_strong", "dert_v1", "dert_v3d", "dert_v3emp"):
        row = []
        for b in BUDGETS:
            rs = [r for (t, st, bb), r in by.items()
                  if st == s and bb == b and r.get("total_time")]
            if not rs:
                row.append((float('nan'), float('nan')))
                continue
            su = statistics.mean(
                sum(1 for m in (r.get("models") or []) if m == strong_name)
                / max(1, len(r.get("models") or [1])) for r in rs)
            med = statistics.median(r["total_time"] for r in rs)
            usage[(s, b)] = (100 * su, med)
            row.append((100 * su, med))
        print(f"{s:14s} " + "".join(f"{su:>5.1f}/{t:>4.1f}s" for su, t in row))

    out = {"success": {f"{s}@{b:g}": v for (s, b), v in succ.items()},
           "mcnemar": pairs,
           "usage": {f"{s}@{b:g}": {"strong_pct": round(su, 1), "time_med": t}
                     for (s, b), (su, t) in usage.items() if su == su}}
    (SWEEP2 / "v3emp_analysis.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n→ {SWEEP2 / 'v3emp_analysis.json'}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
