# -*- coding: utf-8 -*-
"""HammerBench 中文多轮基准结果分析（配套 eval/hammerbench_run.py，纯标准库）。

口径与 analyze_bfcl.py 同构：
  success = 逐轮调用比对全部通过（函数名 + 归一化参数精确匹配）
  on_time = total_time <= budget（仅预算策略）
  A/C 与预算无关 → 对每档预算分别判按时

配对检验：McNemar 精确检验（纯标准库无 scipy）。

用法：python eval/analyze_hb.py --run-id ext1
输出：控制台表 + eval/results/hb_<run_id>/analysis.json
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

RESULTS = Path("eval/results")


def mcnemar_exact(b: int, c: int) -> float:
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = sum(math.comb(n, i) for i in range(0, k + 1)) / 2 ** n
    return min(1.0, 2 * p)


def load(run_id: str) -> list[dict]:
    path = RESULTS / f"hb_{run_id}" / "raw.jsonl"
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="ext1")
    args = ap.parse_args()
    recs = load(args.run_id)
    print(f"records: {len(recs)}")

    # ---- 聚合表 ----
    cells: dict[tuple, dict] = defaultdict(
        lambda: {"n": 0, "succ": 0, "on_time": 0, "time": [], "tok_in": 0,
                 "tok_out": 0, "tiers": defaultdict(int), "stops": defaultdict(int),
                 "turns": []})
    for r in recs:
        c = cells[(r["strategy"], r.get("budget"))]
        c["n"] += 1
        c["succ"] += bool(r.get("success"))
        if r.get("on_time"):
            c["on_time"] += 1
        if r.get("total_time") is not None:
            c["time"].append(r["total_time"])
        c["tok_in"] += r.get("tokens_in") or 0
        c["tok_out"] += r.get("tokens_out") or 0
        for k, v in (r.get("tier_usage") or {}).items():
            c["tiers"][k] += v
        c["stops"][r.get("stop_reason")] += 1
        c["turns"].append(r.get("turns_total") or 0)

    order = [("A", None), ("C", None)] + \
            [(s, b) for s in ("v1", "v3d")
             for b in sorted({b for (s2, b) in cells if s2 == s},
                             key=lambda x: (x is None, x))]
    print(f"\n{'strategy':16s} {'n':>3} {'succ%':>6} {'on_time%':>8} "
          f"{'time_med':>8} {'tok_in':>8} {'turns_med':>9}")
    summary = {}
    for key in order:
        if key not in cells:
            continue
        c = cells[key]
        ts = sorted(c["time"])
        med = ts[len(ts) // 2] if ts else float("nan")
        tn = sorted(c["turns"])
        tmed = tn[len(tn) // 2] if tn else 0
        name = key[0] if key[1] is None else f"{key[0]}@{key[1]:g}"
        row = {"n": c["n"], "success": c["succ"],
               "success_pct": round(100 * c["succ"] / c["n"], 1),
               "on_time_pct": round(100 * c["on_time"] / c["n"], 1) if c["n"] else None,
               "time_median": round(med, 2), "tokens_in": c["tok_in"],
               "tokens_out": c["tok_out"], "turns_median": tmed,
               "tier_usage": dict(c["tiers"]), "stop_reasons": dict(c["stops"])}
        summary[name] = row
        print(f"{name:16s} {c['n']:>3} {row['success_pct']:>6} "
              f"{row['on_time_pct'] if row['on_time_pct'] is not None else '-':>8} "
              f"{med:>8.1f} {c['tok_in']:>8} {tmed:>9}")

    # ---- 配对 McNemar（按 episode 对齐）----
    by_ep: dict[tuple, dict] = defaultdict(dict)
    for r in recs:
        by_ep[r["episode"]][(r["strategy"], r.get("budget"))] = r

    def pair(p_key, q_key):
        b = c = 0
        for ep, m in by_ep.items():
            if p_key not in m or q_key not in m:
                continue
            ps, qs = bool(m[p_key].get("success")), bool(m[q_key].get("success"))
            if ps and not qs:
                b += 1
            elif qs and not ps:
                c += 1
        return b, c, mcnemar_exact(b, c)

    print("\n配对 McNemar（success）：")
    pairs = {}
    budget_list = sorted({b for (s, b) in cells if b is not None})
    for b in budget_list:
        for s in ("v1", "v3d"):
            for ref_name, ref_key in (("A", ("A", None)), ("C", ("C", None))):
                key = (s, b)
                if key not in cells:
                    continue
                bb, cc, p = pair(key, ref_key)
                if bb + cc:
                    name = f"{s}@{b:g} vs {ref_name}"
                    pairs[name] = {"b": bb, "c": cc, "p": round(p, 4)}
                    print(f"  {name:20s} +{bb}/-{cc}  p={p:.4f}")

    # ---- stop_reason 分布（快档「不调工具」病理的量化）----
    print("\nstop_reason 分布：")
    for name in sorted(summary):
        print(f"  {name:16s} {summary[name]['stop_reasons']}")

    out = RESULTS / f"hb_{args.run_id}" / "analysis.json"
    out.write_text(json.dumps({"summary": summary, "mcnemar": pairs},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ {out}")


if __name__ == "__main__":
    main()
