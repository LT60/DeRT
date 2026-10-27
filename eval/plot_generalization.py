# -*- coding: utf-8 -*-
"""泛用性验证总图：左 = 级联端到端模拟（时间量纲），右 = 成本量纲回放。

数据源：eval/results/judge_audit/{cascade_sim.json, cost_replay.json}
输出：docs/figures/generalization.png
用法：miniconda python eval/plot_generalization.py（需 matplotlib）
"""
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
matplotlib.rcParams["axes.unicode_minus"] = False

ROOT = Path(__file__).resolve().parent.parent
SIM = json.loads((ROOT / "eval/results/judge_audit/cascade_sim.json")
                 .read_text(encoding="utf-8"))
COST = json.loads((ROOT / "eval/results/judge_audit/cost_replay.json")
                  .read_text(encoding="utf-8"))
OUT = ROOT / "docs" / "figures" / "generalization.png"

C_CAS, C_A, C_C, C_B = "#B4751B", "#5F5E5A", "#0F6E56", "#185FA5"


def pct(v):
    return [x for x in v]


fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))

# ---- 左：级联端到端模拟（时间量纲）----
ax = axes[0]
bs = [3, 8, 30, 60]
k = lambda b: ("3.0" if b == 3 else "8.0" if b == 8 else
               "30.0" if b == 30 else "60.0")
ax.plot(bs, [SIM["budgets"][k(b)]["A"] for b in bs], "-o", color=C_A,
        lw=1.4, ms=4, label="A 全强档")
ax.plot(bs, [SIM["budgets"][k(b)]["C"] for b in bs], "-o", color=C_C,
        lw=1.4, ms=4, label="C 全快档")
ax.plot(bs, [SIM["budgets"][k(b)]["B"] for b in bs], "--s", color=C_B,
        lw=1.2, ms=4, label="B v1.1 时间路由")
ax.plot(bs, [SIM["budgets"][k(b)]["cascade_success"] for b in bs], "-o",
        color=C_CAS, lw=2.6, ms=6, label="级联（judge+带门升级）")
ax.set_xscale("log")
ax.set_xticks(bs)
ax.set_xticklabels([str(b) for b in bs])
ax.set_xlabel("时间预算（s，对数轴）")
ax.set_ylabel("success%（N=100，离线模拟）")
ax.set_title("级联模拟：宽预算逼近 A、紧预算退化到 C\n"
             "(30s 档 98≈A−2，强档成本/对题 0.0019¥ vs A 0.0057¥)", fontsize=10)
ax.legend(fontsize=8.5, loc="lower right")
ax.grid(alpha=0.25)
ax.set_ylim(0, 105)

# ---- 右：成本量纲回放 ----
ax = axes[1]
g = COST["grid"]
ax.plot(g, [v for v in COST["A"]], "-o", color=C_A, lw=1.4, ms=4,
        label="A′ 全强档")
ax.plot(g, [v for v in COST["C"]], "-o", color=C_C, lw=1.4, ms=4,
        label="C′ 全快档")
ax.plot(g, [v for v in COST["v3emp"]], "-o", color=C_CAS, lw=2.6, ms=6,
        label="v3emp′ 成本路由")
ax.set_xscale("log")
ax.set_xlabel("强档成本预算（¥/请求，对数轴）")
ax.set_ylabel("正确%（N=100，离线回放）")
ax.set_title("成本量纲：v3emp′ 全程 ≥ A′\n"
             "(0.010¥ 即 100/100 封顶，A′ 需 0.020¥；强档率恒 27%)",
             fontsize=10)
ax.legend(fontsize=8.5, loc="lower right")
ax.grid(alpha=0.25)
ax.set_ylim(0, 105)

fig.suptitle("DeRT 泛用性验证：验证层（级联）与量纲泛化（成本）——全部离线零 API 成本",
             fontsize=12)
fig.tight_layout(rect=(0, 0, 1, 0.94))
OUT.parent.mkdir(parents=True, exist_ok=True)
fig.savefig(OUT, dpi=150)
print(f"已输出 {OUT}")
