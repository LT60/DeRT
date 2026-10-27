"""离线回放：扫描路由阈值 θ，用 sweep2 的真实耗时数据模拟"如果当时阈值是 θ"。

动机（2026-10-22 sweep2 死区发现）：STRONG_REMAINING_MIN=3.5s 按旧强档标定，
换 v4.1-flash 后预算 ∈ [3.5s, 强档实际延迟] 时调度器确定性选择注定迟到的模型。

方法（零 API 成本）：
  每个任务 t 有一次 A 运行（纯强档，S(t)=总耗时，correct=100%）和一次 C 运行
  （纯快档，F(t)=总耗时，correct_C(t)）。回放模型：
    强档路径成功(t,b) = 1[S(t) <= b]          （A 正确率恒 100%，成功=按时）
    快档路径成功(t,b) = 1[correct_C & F(t)<=b] + p_wu * 1[correct_C & F(t)>b]
      其中 p_wu = wrap_up 抢救率，从实测标定：B@2s(26) vs C@2s(16)、
      B@3s(48) vs C@3s(42) → 快档迟到但 wrap_up 强制交草稿约救回 11%。
    B(θ,b) = b >= θ ? 强档路径 : 快档路径   （粘性路由：首步一次定档）

模型校验：θ=3.5（=实测 sweep2 的行为）的预测应逐预算复现 B 的实测值（±5pp）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

RUN_DIR = Path(__file__).resolve().parent / "results" / "budget_sweep2"
BUDGETS = [2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0]
P_WU = 0.11  # wrap_up 抢救率（从 B/C @2s、@3s 差值标定：10/84、6/58）


def load_runs() -> dict[str, dict]:
    """按 task 聚合 A/C 各一次的运行记录。"""
    recs = [json.loads(l) for l in
            (RUN_DIR / "raw.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    runs: dict[str, dict] = {}
    for r in recs:
        t = r["task"]
        d = runs.setdefault(t, {})
        if r["strategy"] == "fixed_strong":
            d["S"] = r["total_time"]          # 强档路径总耗时
            d["S_correct"] = 1 if r["correct"] else 0
        elif r["strategy"] == "fixed_fast":
            d["F"] = r["total_time"]          # 快档路径总耗时
            d["C_correct"] = 1 if r["correct"] else 0
        else:
            d.setdefault("B", {})[r["budget"]] = r
    return runs


def strong_success(d: dict, b: float) -> int:
    return 1 if d["S_correct"] == 1 and d["S"] <= b else 0


def fast_success(d: dict, b: float) -> float:
    if d["C_correct"] != 1:
        return 0.0
    return 1.0 if d["F"] <= b else P_WU


def replay(runs: dict, theta: float) -> dict[float, float]:
    """给定阈值 θ，返回各预算档的预测 success%（全任务均值）。"""
    out = {}
    for b in BUDGETS:
        vals = [strong_success(d, b) if b >= theta else fast_success(d, b)
                for d in runs.values()]
        out[b] = 100.0 * sum(vals) / len(vals)
    return out


def oracle(runs: dict) -> dict[float, float]:
    """每任务每预算取强/快较优者（任何固定阈值的理论上限）。"""
    out = {}
    for b in BUDGETS:
        vals = [max(strong_success(d, b), fast_success(d, b))
                for d in runs.values()]
        out[b] = 100.0 * sum(vals) / len(vals)
    return out


def main() -> None:
    runs = load_runs()
    n = len(runs)
    print(f"tasks={n}  budgets={BUDGETS}  p_wu={P_WU}")

    # ---- 模型校验：θ=3.5 预测 vs 实测 B ----
    pred = replay(runs, 3.5)
    print("\n==== 模型校验（θ=3.5，预测 vs 实测 B）====")
    print("budget  pred%  actual%  diff")
    worst = 0.0
    for b in BUDGETS:
        actuals = [d["B"][b] for d in runs.values() if b in d.get("B", {})]
        actual = 100.0 * sum(1 for r in actuals if r["correct"] and r["on_time"]) / len(actuals)
        diff = pred[b] - actual
        worst = max(worst, abs(diff))
        print(f"  {b:4.0f}s  {pred[b]:5.1f}  {actual:7.1f}  {diff:+5.1f}")
    print(f"最大偏差 {worst:.1f}pp（±5pp 内视为模型可信）")

    # ---- θ 扫描 ----
    print("\n==== 阈值扫描（行=θ，列=预算，值=预测 success%）====")
    thetas = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0,
              6.5, 7.0, 7.5, 8.0, 8.5, 9.0, 10.0, 11.0, 12.0]
    header = "theta " + "".join(f"{b:>6.0f}s" for b in BUDGETS) + "    mean"
    print(header)
    rows = []
    for th in thetas:
        p = replay(runs, th)
        mean = sum(p.values()) / len(p)
        rows.append((th, p, mean))
        print(f"{th:5.1f} " + "".join(f"{p[b]:6.1f}" for b in BUDGETS) + f"  {mean:6.2f}")

    orc = oracle(runs)
    print(f"oracle" + "".join(f"{orc[b]:6.1f}" for b in BUDGETS)
          + f"  {sum(orc.values()) / len(orc):6.2f}")

    best = max(rows, key=lambda r: r[2])
    v11 = next(r for r in rows if r[0] == 3.5)
    print(f"\n最优固定阈值 θ*={best[0]:.1f}s（均值 {best[2]:.2f}%）"
          f" vs 当前 θ=3.5（{v11[2]:.2f}%）→ 提升 {best[2] - v11[2]:+.2f}pp")

    # ---- 落盘 ----
    out = {
        "method": __doc__.strip().split("\n")[0],
        "p_wu": P_WU,
        "validation": {"theta": 3.5,
                       "pred": {str(b): round(pred[b], 1) for b in BUDGETS},
                       "max_abs_diff_pp": round(worst, 1)},
        "sweep": [{"theta": th, "mean": round(m, 2),
                   "by_budget": {str(b): round(p[b], 1) for b in BUDGETS}}
                  for th, p, m in rows],
        "oracle": {str(b): round(orc[b], 1) for b in BUDGETS},
        "best_theta": best[0],
        "best_mean": round(best[2], 2),
        "v11_mean": round(v11[2], 2),
    }
    dest = RUN_DIR / "threshold_replay.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"-> {dest}")


if __name__ == "__main__":
    sys.exit(main())
