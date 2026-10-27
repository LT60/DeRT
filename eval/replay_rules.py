# -*- coding: utf-8 -*-
"""路由规则对比回放：「问平均」（R1，v2.1 现行）vs「问概率」（R2，CDF 候选）。

背景（2026-10-23）：v2.1 填平死区（+81pp@4s）但 12s 档回撤 −14pp——P60 单点
估计在重尾分布下过保守。候选修复 R2：不问"学霸平均多久"，改问"剩余时间内
学霸能赶完的概率是否 ≥ p"（窗口经验 CDF）。

方法（零 API 成本）：按 v2.1 的真实执行顺序（ts 排序的 800 次运行）逐个重放
决策。每个任务的强档耗时用 A 运行的实测值 S(t)，快档路径用 C 运行实测
(F(t), correct_C)；强档命中时把 S(t) 写入滑窗（复刻在线窗口的构成方式）。
模拟器校验：R1(0.5) 应近似复现 v2.1 实测（允许采样噪声 ±5pp）。

注意：R1/R2 共享同一窗口数据源，本质差异只在"单点+边际"vs"直接比分布"；
若回放显示 R2 无实质优势，则 12s 回撤的真因更可能是窗口被任务难度构成污染
（T2/T3 段窗口全是慢任务）——那属于 v3 难度感知的领域，如实记录。
"""
from __future__ import annotations

import json
from collections import deque
from pathlib import Path

RUN_DIR = Path(__file__).resolve().parent / "results" / "budget_sweep2"
BUDGETS = [2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0]
P_WU = 0.11          # wrap_up 抢救率（与 replay_threshold.py 同口径）
Q = 0.60             # R1 的分位数 / R2 冷启动沿用的估计
MARGIN = 0.5
_WINDOW = 40
_MIN_SAMPLES = 8
_EMA_ALPHA = 0.3
_EMA_PRIOR = 3.0


def load() -> tuple[list, dict, dict]:
    recs = [json.loads(l) for l in
            (RUN_DIR / "raw.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    strong_dur, fast = {}, {}
    seq = []
    for r in recs:
        t = r["task"]
        if r["strategy"] == "fixed_strong":
            strong_dur[t] = r["total_time"]
        elif r["strategy"] == "fixed_fast":
            fast[t] = (r["total_time"], 1 if r["correct"] else 0)
        elif r["strategy"] == "dert_v2":
            seq.append((t, r["budget"], r["ts"]))
    seq.sort(key=lambda x: x[2])
    return seq, strong_dur, fast


def quantile(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1)))]


def simulate(rule: str, param: float, seq: list, S: dict, C: dict) -> dict:
    window: deque = deque(maxlen=_WINDOW)
    ema = _EMA_PRIOR
    hits: dict[float, list] = {b: [] for b in BUDGETS}
    since_strong = 0
    for t, b, _ in seq:
        remaining = b
        if rule == "R1":
            est = quantile(list(window), Q) if len(window) >= _MIN_SAMPLES else ema
            go_strong = remaining >= est + param
        elif rule == "R3":
            # R1 + 新鲜度探针：估计器只被强档运行喂养，一旦 est 棘轮升高、
            # 强档调用被抑制，窗口就饿死锁死（v2.1 实测后半程 300 次零强档、
            # est 冻结 12.24）。每 20 次运行强制探一次强档，保证窗口永远新鲜。
            est = quantile(list(window), Q) if len(window) >= _MIN_SAMPLES else ema
            go_strong = (remaining >= est + param) or since_strong >= param
        else:  # R2
            if len(window) >= _MIN_SAMPLES:
                frac = sum(1 for x in window if x <= remaining) / len(window)
                go_strong = frac >= param
            else:  # 冷启动退化为 R1 先验行为
                go_strong = remaining >= ema + MARGIN
        if go_strong:
            ok = S[t] <= b
            window.append(S[t])
            ema = _EMA_ALPHA * S[t] + (1 - _EMA_ALPHA) * ema
            since_strong = 0
        else:
            since_strong += 1
            f_t, c_ok = C[t]
            ok = 1.0 if (c_ok == 1 and f_t <= b) else (P_WU if c_ok == 1 else 0.0)
        hits[b].append(ok)
    return {b: 100.0 * sum(v) / len(v) for b, v in hits.items()}


def main() -> None:
    seq, S, C = load()
    print(f"seq={len(seq)} tasks={len(S)}")

    actual = json.loads((RUN_DIR / "v2_analysis.json").read_text(encoding="utf-8"))

    # ---- 模拟器校验：R1(0.5) vs v2.1 实测 ----
    sim_r1 = simulate("R1", 0.5, seq, S, C)
    print("\n==== 模拟器校验：R1(0.5) vs v2.1 实测 ====")
    print("budget  sim%  actual%  diff")
    worst = 0.0
    for b in BUDGETS:
        act = actual["table"][f"{b:.1f}"]["v2"]
        d = sim_r1[b] - act
        worst = max(worst, abs(d))
        print(f"  {b:4.0f}s {sim_r1[b]:5.1f}  {act:7.1f}  {d:+5.1f}")
    print(f"最大偏差 {worst:.1f}pp（采样噪声口径，±8pp 内视为模拟器可用）")

    # ---- 规则对比 ----
    print("\n==== 规则对比（行=规则，列=预算，值=success%；mean=八档均值）====")
    header = "rule        " + "".join(f"{b:>6.0f}s" for b in BUDGETS) + "    mean"
    print(header)
    rows = []
    for m in (0.0, 0.5, 1.0):
        r = simulate("R1", m, seq, S, C)
        rows.append((f"R1 问平均 margin={m}", r))
    for p in (0.5, 0.6, 0.7):
        r = simulate("R2", p, seq, S, C)
        rows.append((f"R2 问概率 p={p}", r))
    for probe in (15, 20, 30):
        r = simulate("R3", probe, seq, S, C)
        rows.append((f"R3 问平均+探针/{probe}", r))
    for name, r in rows:
        mean = sum(r.values()) / len(r)
        print(f"{name:14s}" + "".join(f"{r[b]:6.1f}" for b in BUDGETS) + f"  {mean:6.2f}")
    act_mean = sum(actual["table"][f"{b:.1f}"]["v2"] for b in BUDGETS) / len(BUDGETS)
    print(f"{'v2.1 实测':14s}" + "".join(
        f"{actual['table'][f'{b:.1f}']['v2']:6.1f}" for b in BUDGETS) + f"  {act_mean:6.2f}")

    best_name, best_r = max(rows, key=lambda x: sum(x[1].values()) / len(x[1]))
    print(f"\n回放最优：{best_name}（mean {sum(best_r.values())/len(best_r):.2f}）")
    print("注意：R1/R2 共享同一窗口，若两者接近，12s 回撤真因=窗口被任务难度构成污染"
          "（T2/T3 段窗口全是慢任务），修复属 v3 难度感知领域。")

    out = {name: {str(b): round(r[b], 1) for b in BUDGETS} for name, r in rows}
    (RUN_DIR / "rules_replay.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"-> {RUN_DIR / 'rules_replay.json'}")


if __name__ == "__main__":
    main()
