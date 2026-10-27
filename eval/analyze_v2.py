"""v2.1 死区实验的专属分析：dert_v2 vs dert_v1（θ=3.5 固定阈值）vs A/C。

背景（2026-10-22 sweep2 死区发现 + 2026-10-23 离线回放）：
  v1.1 固定阈值 3.5s 在慢强档（v4.1-flash）下产生路由死区——预算 ∈ [3.5, 强档
  实际延迟] 时确定性上强档然后迟到，4s 档惨败快档基线（0/56，p<0.001）。
  v2.1 = v2 的滑窗 P60+0.5 自适应路由 + v1.1 收尾。回放预测：自适应阈值落在
  6.5-10s 高原内即可消掉死区（4s 档 3% → ~60%）。

采集：v2 记录与 sweep2 的 A/C/v1 同 run-id 同天追加（resume 补采 800 格），
同任务配对。注意 v2 滑窗冷启动（前 8 个强档样本前退化为先验 3.5s），
本脚本单独统计冷启动期（每档前 10 次）的行为以如实呈现。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_budget import mcnemar_exact  # noqa: E402

RUN_DIR = Path(__file__).resolve().parent / "results" / "budget_sweep2"
BUDGETS = [2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0]
DEAD_ZONE = [4.0, 5.0, 6.0]  # 死区主战场


def success_v(r: dict) -> bool:
    """dert_* 记录：correct 且按时（on_time 采集时已判）。"""
    return bool(r["correct"]) and bool(r["on_time"])


def success_ac(r: dict, b: float) -> bool:
    """A/C 无预算字段：correct 且 total_time <= b。"""
    return bool(r["correct"]) and r["total_time"] is not None and r["total_time"] <= b


def main() -> None:
    recs = [json.loads(l) for l in
            (RUN_DIR / "raw.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    runs = {}
    for r in recs:
        key = (r["task"], r["strategy"],
               r["budget"] if r["strategy"] in ("dert_v1", "dert_v2") else None)
        runs[key] = r
    tasks = sorted({k[0] for k in runs})
    print(f"records={len(recs)} tasks={len(tasks)}")

    v1 = {b: [runs.get((t, "dert_v1", b)) for t in tasks] for b in BUDGETS}
    v2 = {b: [runs.get((t, "dert_v2", b)) for t in tasks] for b in BUDGETS}
    A = [runs.get((t, "fixed_strong", None)) for t in tasks]
    C = [runs.get((t, "fixed_fast", None)) for t in tasks]

    def v_pct(rs):
        rs = [r for r in rs if r]
        return 100.0 * sum(success_v(r) for r in rs) / len(rs) if rs else None

    def ac_pct(rs, b):
        rs = [r for r in rs if r]
        return 100.0 * sum(success_ac(r, b) for r in rs) / len(rs) if rs else None

    print("\n==== success%（correct 且按时）====")
    print("budget     A%      C%     v1%     v2%   v2-v1")
    table = {}
    for b in BUDGETS:
        a, c = ac_pct(A, b), ac_pct(C, b)
        p1, p2 = v_pct(v1[b]), v_pct(v2[b])
        table[b] = {"A": a, "C": c, "v1": p1, "v2": p2}
        print(f"  {b:4.0f}s {a:6.1f} {c:6.1f} {p1:6.1f} {p2:6.1f} {p2 - p1:+6.1f}")

    # ---- McNemar 配对 ----
    print("\n==== McNemar 精确检验（success 口径，x/y = 仅 x 成 / 仅 y 成）====")
    print("budget     v2_vs_v1             v2_vs_C              v2_vs_A")
    tests = {}
    for b in BUDGETS:
        row = [f"  {b:4.0f}s"]
        for name, xs, ys, x_is_ac, y_is_ac in (
                ("v2_vs_v1", v2[b], v1[b], False, False),
                ("v2_vs_C", v2[b], C, False, True),
                ("v2_vs_A", v2[b], A, False, True)):
            x_only = y_only = n = 0
            for rx, ry in zip(xs, ys):
                if not rx or not ry:
                    continue
                n += 1
                sx = success_ac(rx, b) if x_is_ac else success_v(rx)
                sy = success_ac(ry, b) if y_is_ac else success_v(ry)
                if sx and not sy:
                    x_only += 1
                elif sy and not sx:
                    y_only += 1
            p = mcnemar_exact(x_only, y_only)
            tests[f"{name}@{b:g}"] = {"x_only": x_only, "y_only": y_only,
                                      "n": n, "p": p}
            row.append(f"{x_only:3d}/{y_only:<3d} p={p:<8.3g}")
        print("  ".join(row))

    # ---- v2 路由行为：强档调用率（冷启动期 vs 窗口填充后）----
    print("\n==== v2 路由行为（强档调用率 / on_time / success，冷启动期 vs 之后）====")
    v2_recs = [r for r in recs if r["strategy"] == "dert_v2"]
    beh = {}
    for b in BUDGETS:
        rs = sorted((r for r in v2_recs if r["budget"] == b), key=lambda r: r["ts"])
        first10, rest = rs[:10], rs[10:]

        def stat(rs_):
            if not rs_:
                return {"strong": None, "on_time": None, "success": None}
            return {
                "strong": round(100.0 * sum(1 for r in rs_
                                            if any("deepseek" in m for m in r["models"])) / len(rs_), 1),
                "on_time": round(100.0 * sum(bool(r["on_time"]) for r in rs_) / len(rs_), 1),
                "success": round(100.0 * sum(success_v(r) for r in rs_) / len(rs_), 1),
            }
        beh[str(b)] = {"first10": stat(first10), "rest": stat(rest)}
        f, r_ = beh[str(b)]["first10"], beh[str(b)]["rest"]
        print(f"  {b:4.0f}s  strong: {f['strong']}→{r_['strong']}  "
              f"on_time: {f['on_time']}→{r_['on_time']}  "
              f"success: {f['success']}→{r_['success']}")

    dz = {str(b): table[b] for b in DEAD_ZONE}
    print(f"\n死区三档（4/5/6s）v2 vs v1 提升："
          f"{', '.join(f'{b}s {dz[str(b)]['v2'] - dz[str(b)]['v1']:+.1f}pp' for b in DEAD_ZONE)}")

    dest = RUN_DIR / "v2_analysis.json"
    dest.write_text(json.dumps({"table": {str(b): table[b] for b in BUDGETS},
                                "tests": tests, "behavior": beh,
                                "dead_zone": dz},
                               ensure_ascii=False, indent=1),
                    encoding="utf-8")
    print(f"-> {dest}")


if __name__ == "__main__":
    sys.exit(main())
