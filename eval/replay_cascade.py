# -*- coding: utf-8 -*-
"""实验 B：级联端到端离线模拟——补上验证层后，两个「赌博」是否被解决？

模拟的完整级联策略（不跑任何新模型调用，全部用已有实测数据拼装）：
  1. 快档先试          → C 策略的实测结果（main100 fixed_fast，快档/判卷本地免费）
  2. judge 验证        → 实验 A 的实测判卷结果（逐任务 pass/fail + 实测延迟）
  3. 未通过则升级强档  → A 策略的实测结果（fixed_strong）
  成功口径 = 内容正确 且 (快档+判卷[+强档]) 总耗时 ≤ 预算。

诚实偏差（写进结论）：升级路径用 A 的历史结果近似「真实重跑」——同一任务
从零跑强档，语义一致，但忽略了重跑时的上下文复用可能，偏乐观。

对照：A 全强档 / C 全快档 / B=dert_v1.1（均为 main100 实测）。
产出：逐预算指标表 + 强档用量 + 每答对一题的强档成本；JSON 落盘供绘图。
用法：python -m eval.replay_cascade
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN_RAW = ROOT / "eval" / "results" / "budget_main100" / "raw.jsonl"
JUDGE_OUT = ROOT / "eval" / "results" / "judge_audit" / "samples_v2.jsonl"
OUT = ROOT / "eval" / "results" / "judge_audit" / "cascade_sim.json"
BUDGETS = (3.0, 8.0, 30.0, 60.0)
PRICE_IN, PRICE_OUT = 2.0, 8.0     # 百炼 deepseek-v4.1-flash 公开价 ¥/M tokens


def cost_yuan(r: dict) -> float:
    return r["tokens_in"] / 1e6 * PRICE_IN + r["tokens_out"] / 1e6 * PRICE_OUT


def main() -> None:
    rows = [json.loads(l) for l in
            MAIN_RAW.read_text(encoding="utf-8").splitlines() if l.strip()]
    A = {r["task"]: r for r in rows if r["strategy"] == "fixed_strong"}
    C = {r["task"]: r for r in rows if r["strategy"] == "fixed_fast"}
    B = {(r["task"], r["budget"]): r for r in rows if r["strategy"] == "dert_v1"}
    tasks = sorted(C)

    judge: dict[str, dict] = {}
    for line in JUDGE_OUT.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r["domain"] == "cv":
            judge[r["id"]] = r
    missing = [t for t in tasks if t not in judge]
    assert not missing, f"judge 结果缺 {len(missing)} 条: {missing[:3]}"
    t_judge = statistics.median(r["judge_s"] for r in judge.values())
    # 升级门 = 强档延迟的模型级估计（生产中来自标定卡/v3d 门；这里用 A 实测
    # 中位数，不用逐任务真值——避免 oracle 作弊）。剩余预算不够塞下一次
    # 强档尝试时，不升级、原样交付快档答案（宁可错也不迟到）。
    gate = statistics.median(A[t]["total_time"] for t in tasks)

    def cascade(task: str, budget: float, gated: bool = True) -> dict:
        c, a, j = C[task], A[task], judge[task]
        # 微观规则：剩余预算连一次判卷都装不下 → 判卷没有意义（不可能升级），
        # 直接交付快档答案，省下判卷时间
        if budget - c["total_time"] < t_judge:
            return {"correct": bool(c["correct"]) and
                    c["total_time"] <= budget,
                    "escalated": False, "strong_cost": 0.0,
                    "total": c["total_time"]}
        if j["verdict"] == "pass":                      # judge 放行 → 直接交付
            total = c["total_time"] + t_judge
            return {"correct": bool(c["correct"]) and total <= budget,
                    "escalated": False,
                    "strong_cost": 0.0,
                    "total": total}
        total = c["total_time"] + t_judge + a["total_time"]   # 升级强档重跑
        if gated and c["total_time"] + t_judge + gate > budget:
            # 门拦住：预算塞不下强档 → 不升级，原样交付快档答案
            total = c["total_time"] + t_judge
            return {"correct": bool(c["correct"]) and total <= budget,
                    "escalated": False, "strong_cost": 0.0, "total": total}
        return {"correct": bool(a["correct"]) and total <= budget,
                "escalated": True,
                "strong_cost": cost_yuan(a),
                "total": total}

    report = {"t_judge_P50": round(t_judge, 3), "gate_P50_strong":
              round(gate, 3), "budgets": {}, "budgets_nogate": {}}
    print("=" * 78)
    print(f"级联端到端离线模拟（N=100，judge P50={t_judge:.2f}s，"
          f"升级门=强档 P50 {gate:.2f}s，快档/判卷本地免费）")
    print("=" * 78)
    hdr = (f"{'预算':>5} | {'带门级联':>8} {'升级率':>6} | {'无门级联':>8} | "
           f"{'A':>4} {'C':>4} {'B':>4} | {'强档成本/对题':>12}")
    print(hdr)
    print("-" * 78)
    for b in BUDGETS:
        res = [cascade(t, b) for t in tasks]
        res_ng = [cascade(t, b, gated=False) for t in tasks]
        succ = sum(r["correct"] for r in res)
        esc = sum(r["escalated"] for r in res)
        succ_ng = sum(r["correct"] for r in res_ng)
        a_s = sum(1 for t in tasks if A[t]["correct"] and
                  A[t]["total_time"] <= b)
        c_s = sum(1 for t in tasks if C[t]["correct"] and
                  C[t]["total_time"] <= b)
        b_s = sum(1 for (t, bb), r in B.items()
                  if bb == b and r["correct"] and r["on_time"])
        cas_cost = sum(r["strong_cost"] for r in res) / max(succ, 1)
        report["budgets"][str(b)] = {
            "cascade_success": succ, "escalated": esc,
            "A": a_s, "C": c_s, "B": b_s,
            "cascade_strong_cost_per_correct": round(cas_cost, 4)}
        report["budgets_nogate"][str(b)] = succ_ng
        print(f"{b:>4.0f}s | {succ:>6}/100 {esc:>4}/100 | {succ_ng:>6}/100 | "
              f"{a_s:>2}/100 {c_s:>2}/100 {b_s:>2}/100 | {cas_cost:>9.4f}¥")

    # judge 失误的代价拆解（@30s，宽预算代表档）
    b = 30.0
    miss = [t for t in tasks
            if judge[t]["verdict"] == "pass" and not C[t]["correct"]]
    kill = [t for t in tasks
            if judge[t]["verdict"] == "fail" and C[t]["correct"]]
    report["judge_errors_at30"] = {
        "漏检(错答案被放行)": miss, "错杀(对的被升级)": kill}
    print(f"\njudge 失误拆解 @30s：漏检 {len(miss)} 个（错答案被放行），"
          f"错杀 {len(kill)} 个（对的被升级——浪费但不致错）")
    if miss:
        print(f"  漏检任务：{miss}")
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"\n落盘 {OUT}")


if __name__ == "__main__":
    main()
