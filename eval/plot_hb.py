# -*- coding: utf-8 -*-
"""HammerBench 中文多轮基准曲线图（配套 analyze_hb.py，miniconda python 运行）。

图 hb_curve.png  双面板：success% 与耗时中位 随预算变化
                 （v1/v3d 曲线 + A/C 不感知预算参考线）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

plt.rcParams["font.family"] = "Microsoft YaHei"
plt.rcParams["axes.unicode_minus"] = False

ROOT = Path(__file__).resolve().parent
FIG = ROOT.parent / "docs" / "figures"
FIG.mkdir(exist_ok=True)

C_STRONG = "#185fa5"; C_FAST = "#888780"; C_MID = "#d85a30"; C_V1 = "#7a6ea8"

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else "ext1"
res = json.loads((ROOT / "results" / f"hb_{RUN_ID}" / "analysis.json")
                 .read_text(encoding="utf-8"))
S = res["summary"]

budgets = sorted({float(k.split("@")[1]) for k in S if "@" in k})
a_pct = S["A"]["success_pct"]; c_pct = S["C"]["success_pct"]
a_med = S["A"]["time_median"]; c_med = S["C"]["time_median"]

series = [
    ("v1",  "dert_v1（时间路由）",        C_V1,  "o"),
    ("v3d", "dert_v3d（中文难度词表原配）", C_MID, "s"),
]

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11.6, 4.6), dpi=150)

# ---- 面板 1：success% ----
for key, label, color, mk in series:
    ys = [S[f"{key}@{b:g}"]["success_pct"] for b in budgets]
    ax1.plot(budgets, ys, marker=mk, color=color, lw=2, label=label)
    for b, y in zip(budgets, ys):
        ax1.annotate(f"{y:.0f}", (b, y), textcoords="offset points",
                     xytext=(0, 7), ha="center", fontsize=8, color=color)
ax1.axhline(a_pct, color=C_STRONG, ls="--", lw=1.4,
            label=f"A 全强档 {a_pct:.1f}%（不感知预算）")
ax1.axhline(c_pct, color=C_FAST, ls=":", lw=1.4,
            label=f"C 全快档 {c_pct:.1f}%（不感知预算）")
ax1.set_xscale("log")
ax1.set_xticks(budgets)
ax1.set_xticklabels([f"{b:g}s" for b in budgets])
ax1.minorticks_off()
ax1.set_xlabel("时间预算 / episode")
ax1.set_ylabel("success%（逐轮调用比对全过）")
ax1.grid(alpha=0.25)
ax1.legend(fontsize=8, loc="lower right")

# ---- 面板 2：耗时中位 ----
for key, label, color, mk in series:
    ys = [S[f"{key}@{b:g}"]["time_median"] for b in budgets]
    ax2.plot(budgets, ys, marker=mk, color=color, lw=2, label=label)
    for b, y in zip(budgets, ys):
        ax2.annotate(f"{y:.1f}", (b, y), textcoords="offset points",
                     xytext=(0, 7), ha="center", fontsize=8, color=color)
ax2.axhline(a_med, color=C_STRONG, ls="--", lw=1.4,
            label=f"A 中位 {a_med:.1f}s")
ax2.axhline(c_med, color=C_FAST, ls=":", lw=1.4,
            label=f"C 中位 {c_med:.1f}s")
ax2.set_xscale("log")
ax2.set_xticks(budgets)
ax2.set_xticklabels([f"{b:g}s" for b in budgets])
ax2.minorticks_off()
ax2.set_xlabel("时间预算 / episode")
ax2.set_ylabel("耗时中位 / s")
ax2.grid(alpha=0.25)
ax2.legend(fontsize=8, loc="upper left")

n_runs = sum(v["n"] for v in S.values())
fig.suptitle(f"HammerBench 中文多轮外部验证（N=20 题 · {n_runs} run）\n"
             "A−C 差 70pp（快档 slot-filling 近全灭）；难度词表跨数据集失效（v3d≡C，5%）；"
             "v1@30 ≡ A 且 100% 按时",
             fontsize=11)
fig.tight_layout(rect=(0, 0, 1, 0.92))
out = FIG / "hb_curve.png"
fig.savefig(out)
plt.close(fig)
print(f"→ {out}")
