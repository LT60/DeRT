# -*- coding: utf-8 -*-
"""v3 难度感知路由的离线回放：用廉价问句特征预测"该不该请强模型"。

动机（2026-10-23）：快档正确失败高度集中——T2（数值对比类）52%，T1 74%，
T3 94%。若难度可从问句特征预测，则"难任务走强档、易任务走快档"可同时拿到
质量与速度（用户核心诉求"又快又准"的可行形态）。

方法（零 API 成本）：每任务的快档正确性（C 运行，单样本）、快/强耗时
（F/S 运行）来自 sweep2 实测；特征来自 eval/tasks.py 的问句文本与
expect_tools。对每条规则输出：正确率（快档对→对；强档路由→按 A 实测全对）、
强档用量%、平均耗时。规则间对比共享单样本噪声，结论以显著差距为准。

诚实性声明：特征在看过结果后挑选，100 样本单次测量存在过拟合与采样噪声
风险——回放选出的规则必须经在线对照确认（进行中）。
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from eval.tasks import TASKS  # noqa: E402

RUN_DIR = Path(__file__).resolve().parent / "results" / "budget_sweep2"
CMP_WORDS = ("是否超过", "SLO", "红线", "对照", "是否达标", "超过")


def load() -> dict:
    recs = [json.loads(l) for l in
            (RUN_DIR / "raw.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    C, S, F = {}, {}, {}
    for r in recs:
        t = r["task"]
        if r["strategy"] == "fixed_fast" and r["total_time"]:
            C[t] = 1 if r["correct"] else 0
            F[t] = r["total_time"]
        elif r["strategy"] == "fixed_strong" and r["total_time"]:
            S[t] = r["total_time"]
    s_med = statistics.median(S.values())
    feats = {}
    for t in TASKS:
        tid = t["type"] + "/" + t["id"]
        feats[tid] = {
            "type": t["type"],
            "n_tools": len(t["expect_tools"]),
            "len": len(t["prompt"]),
            "cmp": any(w in t["prompt"] for w in CMP_WORDS),
            "multi_tool": len(t["expect_tools"]) >= 2,
        }
    return {"C": C, "F": F, "S": S, "s_med": s_med, "feats": feats}


def evaluate(name: str, hard_fn, data: dict) -> dict:
    C, F, S, s_med = data["C"], data["F"], data["S"], data["s_med"]
    tasks = sorted(C)
    n = len(tasks)
    correct = strong_used = 0
    total_time = 0.0
    for t in tasks:
        hard = hard_fn(t, data["feats"][t])
        if hard:
            correct += 1                       # 强档路由：按 A 实测 correct=100%
            strong_used += 1
            total_time += S.get(t, s_med)
        else:
            correct += C[t]                    # 快档路由：按 C 实测正确性
            total_time += F[t]
    return {"rule": name, "correct_pct": round(100 * correct / n, 1),
            "strong_pct": round(100 * strong_used / n, 1),
            "avg_time": round(total_time / n, 2)}


def main() -> None:
    data = load()
    n = len(data["C"])
    print(f"tasks={n}")

    # 单特征预测力：各特征取值下的快档错误率
    print("\n==== 特征预测力（快档错误率 | 组内任务数）====")
    for feat, groups in (("type", ("T1", "T2", "T3")),):
        for g in groups:
            sub = [t for t in data["C"] if data["feats"][t][feat] == g]
            err = 100 * sum(1 - data["C"][t] for t in sub) / len(sub)
            print(f"  {feat}={g}: 快档错 {err:.0f}%  (n={len(sub)})")
    for feat, gname in (("cmp", "数值对比措辞"), ("multi_tool", "需≥2工具")):
        for v in (True, False):
            sub = [t for t in data["C"] if data["feats"][t][feat] == v]
            err = 100 * sum(1 - data["C"][t] for t in sub) / len(sub)
            print(f"  {feat}({gname})={v}: 快档错 {err:.0f}%  (n={len(sub)})")

    # 规则对比
    print("\n==== 规则对比（hard_fn 决定哪些任务走强档）====")
    print("rule                          correct%  strong%  avg_time")
    rules = [
        ("基线：全快档", lambda t, f: False),
        ("基线：全强档", lambda t, f: True),
        ("上限：完美难度路由", lambda t, f: data["C"][t] == 0),
        ("类型：T2 强", lambda t, f: f["type"] == "T2"),
        ("类型：T2/T3 强", lambda t, f: f["type"] in ("T2", "T3")),
        ("特征：数值对比措辞→强", lambda t, f: f["cmp"]),
        ("特征：≥2工具→强", lambda t, f: f["multi_tool"]),
        ("特征：对比措辞∨≥2工具", lambda t, f: f["cmp"] or f["multi_tool"]),
        ("特征：措辞∧类型=T2", lambda t, f: f["cmp"] and f["type"] == "T2"),
    ]
    out = []
    for name, fn in rules:
        r = evaluate(name, fn, data)
        out.append(r)
        print(f"{name:26s}  {r['correct_pct']:6.1f}  {r['strong_pct']:6.1f}  {r['avg_time']:7.2f}")

    (RUN_DIR / "v3_replay.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"-> {RUN_DIR / 'v3_replay.json'}")


if __name__ == "__main__":
    main()
