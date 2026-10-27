"""BFCL 外部验证结果分析（配套 eval/bfcl_run.py，纯标准库）。

口径（与 analyze_budget 同构）：
  success = 逐轮状态比对全部通过（BFCL 口径）
  on_time = total_time <= budget（仅预算策略）
  A/C 与预算无关 → 对每档预算分别判按时

配对检验：McNemar 精确检验（二项 n=k，k=不一致对中少数侧；
p = 2 * sum(C(n,i) 0.5^n, i=0..min(k,n-k))，双侧），纯标准库无 scipy。

用法：python eval/analyze_bfcl.py --run-id bfcl_ext1
输出：控制台表 + eval/results/bfcl_<run_id>/analysis.json
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

RESULTS = Path("eval/results")


def mcnemar_exact(b: int, c: int) -> float:
    """b/c = 两策略中 0→1 与 1→0 的不一致对数；返回双侧精确 p 值。"""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = sum(math.comb(n, i) for i in range(0, k + 1)) / 2 ** n
    return min(1.0, 2 * p)


def load(run_id: str) -> list[dict]:
    path = RESULTS / f"bfcl_{run_id}" / "raw.jsonl"
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="bfcl_ext1")
    args = ap.parse_args()
    recs = load(args.run_id)
    print(f"records: {len(recs)}")

    # ---- 聚合表 ----
    cells: dict[tuple, dict] = defaultdict(
        lambda: {"n": 0, "succ": 0, "on_time": 0, "time": [], "tok_in": 0,
                 "tok_out": 0, "tiers": defaultdict(int), "stops": defaultdict(int)})
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

    order = [("A", None), ("C", None)] + \
            [(s, b) for s in ("v1", "v3d", "v3d_en")
             for b in sorted({b for (s2, b) in cells if s2 == s}, key=lambda x: (x is None, x))]
    print(f"\n{'strategy':22s} {'n':>3} {'succ%':>6} {'on_time%':>8} "
          f"{'time_med':>8} {'tok_in':>8} {'tok_out':>7}")
    summary = {}
    for key in order:
        if key not in cells:
            continue
        c = cells[key]
        ts = sorted(c["time"])
        med = ts[len(ts) // 2] if ts else float("nan")
        name = key[0] if key[1] is None else f"{key[0]}@{key[1]:g}"
        row = {"n": c["n"], "success": c["succ"], "success_pct": round(100 * c["succ"] / c["n"], 1),
               "on_time_pct": round(100 * c["on_time"] / c["n"], 1) if c["n"] else None,
               "time_median": med, "tokens_in": c["tok_in"], "tokens_out": c["tok_out"],
               "tier_usage": dict(c["tiers"]), "stop_reasons": dict(c["stops"])}
        summary[name] = row
        print(f"{name:22s} {c['n']:>3} {row['success_pct']:>6} "
              f"{row['on_time_pct'] if row['on_time_pct'] is not None else '-':>8} "
              f"{med:>8.1f} {c['tok_in']:>8} {c['tok_out']:>7}")

    # ---- 配对 McNemar（按 episode 对齐）----
    by_ep: dict[tuple, dict] = defaultdict(dict)
    for r in recs:
        by_ep[r["episode"]][(r["strategy"], r.get("budget"))] = r

    def pair(p_name, q_name):
        b = c = 0
        for ep, m in by_ep.items():
            if p_name not in m or q_name not in m:
                continue
            ps, qs = bool(m[p_name].get("success")), bool(m[q_name].get("success"))
            if ps and not qs:
                b += 1
            elif qs and not ps:
                c += 1
        return b, c, mcnemar_exact(b, c)

    print("\n配对 McNemar（success）：")
    pairs = {}
    budget_list = sorted({b for (s, b) in cells if b is not None})
    for b in budget_list:
        for s in ("v1", "v3d", "v3d_en"):
            for ref_name, ref_key in (("A", ("A", None)), ("C", ("C", None))):
                key = (s, b)
                if key not in cells:
                    continue
                bb, cc, p = pair(key, ref_key)
                if bb + cc:
                    name = f"{s}@{b:g} vs {ref_name}"
                    pairs[name] = {"b": bb, "c": cc, "p": round(p, 4)}
                    print(f"  {name:24s} +{bb}/-{cc}  p={p:.4f}")

    out = RESULTS / f"bfcl_{args.run_id}" / "analysis.json"
    out.write_text(json.dumps({"summary": summary, "mcnemar": pairs},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ {out}")


if __name__ == "__main__":
    main()
