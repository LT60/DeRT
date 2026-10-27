# -*- coding: utf-8 -*-
"""难度评分方案的通用化对比（2026-10-25，用户指令：难度评分应使用公用/通用方案）。

动机：v3 家族的难度信号 = 手写中文语义词表（CMP_WORDS）——在自建集上有效，
但在 BFCL 英文基准上恒不触发（外部验证实证）。用户指出应改用公用的通用方案。

候选方案（按"部署时需要什么"分层，通用性递增）：
  cmp_words   现状基线：中文语义词表（域内词表，跨域失效——BFCL 已实证）
  struct_type 结构难度·预注册分层：type ∈ {T2,T3}（tasks-design.md 定义；对标
              GAIA level / 基准自带难度层——任何规范基准都自带分层）
  struct_ntools 结构难度·工具链长：len(expect_tools) ≥ 2（从 ground truth 元数据
              可数出——任何带 gt 的基准都可计算，语言无关）
  emp_cross   经验难度：另一夜次全量（budget_main100）的快档失败史（心理测量学
              标准难度指数 1−p 的最简形态；部署形态 = 影子流量统计，冷启动不可用）

Gold（真需要强模型）：sweep2 快档错误的任务（A 全量 100% 正确已验证，
needs_strong ≡ C wrong）。

指标：
  ① 判别质量：对 gold 的 precision/recall/F1（混淆矩阵）
  ② 路由回放（30s 宽预算口径，同 replay_v3）：hard→强档（correct=1，耗时 S），
     easy→快档（correct=C，耗时 F）；报告 correct% / 强档用量% / 平均耗时
  ③ 通用性注记（语言无关？需要 gt 元数据？需要历史流量？）

诚实性：与 replay_v3 同款声明——经验难度用了留出夜次（main100），但同一快档
模型（qwen2.5:7b），样本间部分相关；回放结论若要上线须在线对照确认。

输出：控制台表 + eval/results/budget_sweep2/difficulty_schemes.json
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from eval.tasks import TASKS  # noqa: E402

ROOT = Path(__file__).resolve().parent
SWEEP2 = ROOT / "results" / "budget_sweep2" / "raw.jsonl"
MAIN100 = ROOT / "results" / "budget_main100" / "raw.jsonl"
OUT = ROOT / "results" / "budget_sweep2" / "difficulty_schemes.json"

CMP_WORDS = ("是否超过", "SLO", "红线", "对照", "是否达标", "超过")


def load_sweep2():
    C, S, F = {}, {}, {}
    a_correct = 0
    for line in SWEEP2.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        t = r["task"]
        if r["strategy"] == "fixed_fast":
            C[t] = bool(r["correct"])
            F[t] = r["total_time"]
        elif r["strategy"] == "fixed_strong":
            a_correct += bool(r["correct"])
            S[t] = r["total_time"]
    return C, S, F, a_correct


def load_main100_c() -> dict[str, bool]:
    out = {}
    for line in MAIN100.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r["strategy"] == "fixed_fast":
            out[r["task"]] = bool(r["correct"])
    return out


def confusion(hard: set[str], gold: set[str], tasks: list[str]):
    tp = len(hard & gold)
    fp = len(hard - gold)
    fn = len(gold - hard)
    tn = len(tasks) - tp - fp - fn
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": round(prec, 3), "recall": round(rec, 3),
            "f1": round(f1, 3)}


def routing_replay(hard: set[str], C: dict, S: dict, F: dict, s_med: float):
    """hard→强档（correct=1.0，A 实测全对）；easy→快档（C 实测）。"""
    correct = strong_used = 0
    times = []
    for t, c_ok in C.items():
        if t in hard:
            correct += 1
            strong_used += 1
            times.append(S.get(t, s_med))
        else:
            correct += int(c_ok)
            times.append(F.get(t, 0.0))
    n = len(C)
    return {"correct_pct": round(100 * correct / n, 1),
            "strong_pct": round(100 * strong_used / n, 1),
            "time_mean": round(statistics.mean(times), 2)}


def main():
    C, S, F, a_ok = load_sweep2()
    tasks = sorted(C)
    print(f"tasks={len(tasks)}  A correct={a_ok}/{len(tasks)}"
          f"  （gold = sweep2 快档错误，共 {sum(1 for t in tasks if not C[t])} 题）")
    main100_c = load_main100_c()

    gold = {t for t in tasks if not C[t]}
    s_med = statistics.median(S.values())

    schemes = {
        "cmp_words": {
            "desc": "现状基线：中文语义词表（v3 家族现用）",
            "hard": {t for t in tasks
                     if any(w in next(x["prompt"] for x in TASKS
                                      if x["type"] + "/" + x["id"] == t)
                            for w in CMP_WORDS)},
            "lang_free": False, "needs_gt": False, "needs_history": False},
        "struct_type": {
            "desc": "预注册难度分层：T2/T3 = hard（对标 GAIA level 等基准自带分层）",
            "hard": {t for t in tasks if t.split("/")[0] != "T1"},
            "lang_free": True, "needs_gt": True, "needs_history": False},
        "struct_ntools": {
            "desc": "工具链长度：expect_tools ≥ 2 = hard（gt 元数据可数，语言无关）",
            "hard": {t for t in tasks
                     if len(next(x["expect_tools"] for x in TASKS
                                 if x["type"] + "/" + x["id"] == t)) >= 2},
            "lang_free": True, "needs_gt": True, "needs_history": False},
        "emp_cross_night": {
            "desc": "经验难度：留出夜次（main100）快档失败史（1−p 最简形态）",
            "hard": {t for t in tasks if t in main100_c and not main100_c[t]},
            "lang_free": True, "needs_gt": False, "needs_history": True},
    }

    rows = {}
    print(f"\n{'方案':16s} {'P':>5} {'R':>5} {'F1':>5} | {'回放correct%':>10} "
          f"{'强档%':>5} {'均耗s':>6} | 通用性")
    base_fast = routing_replay(set(), C, S, F, s_med)          # all-fast 下界
    base_strong = routing_replay(set(tasks), C, S, F, s_med)   # all-strong 上界
    for name, sc in schemes.items():
        cm = confusion(sc["hard"], gold, tasks)
        rr = routing_replay(sc["hard"], C, S, F, s_med)
        tags = []
        tags.append("语言无关" if sc["lang_free"] else "绑语言")
        tags.append("需gt元数据" if sc["needs_gt"] else "无需gt")
        tags.append("需历史流量" if sc["needs_history"] else "无需历史")
        rows[name] = {"desc": sc["desc"], **cm, **rr,
                      "lang_free": sc["lang_free"], "needs_gt": sc["needs_gt"],
                      "needs_history": sc["needs_history"]}
        print(f"{name:16s} {cm['precision']:>5.2f} {cm['recall']:>5.2f} "
              f"{cm['f1']:>5.2f} | {rr['correct_pct']:>10} {rr['strong_pct']:>5} "
              f"{rr['time_mean']:>6} | {'/'.join(tags)}")
    print(f"{'(oracle=gold)':16s} {'1.00':>5} {'1.00':>5} {'1.00':>5} | "
          f"{routing_replay(gold, C, S, F, s_med)['correct_pct']:>10} "
          f"{routing_replay(gold, C, S, F, s_med)['strong_pct']:>5} "
          f"{routing_replay(gold, C, S, F, s_med)['time_mean']:>6} | 理论上限")
    print(f"{'(all-fast)':16s} {'-':>5} {'-':>5} {'-':>5} | "
          f"{base_fast['correct_pct']:>10} {base_fast['strong_pct']:>5} "
          f"{base_fast['time_mean']:>6} | 下界")
    print(f"{'(all-strong)':16s} {'-':>5} {'-':>5} {'-':>5} | "
          f"{base_strong['correct_pct']:>10} {base_strong['strong_pct']:>5} "
          f"{base_strong['time_mean']:>6} | 上界")

    OUT.write_text(json.dumps({
        "gold_note": "needs_strong = sweep2 快档错误（A 100% 正确已验证）",
        "a_correct": a_ok, "gold_n": len(gold),
        "schemes": rows,
        "oracle": routing_replay(gold, C, S, F, s_med),
        "all_fast": base_fast, "all_strong": base_strong,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ {OUT}")


if __name__ == "__main__":
    main()
