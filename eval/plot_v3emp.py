# -*- coding: utf-8 -*-
"""「又快又准」收官曲线：v3emp vs 各策略（sweep2，miniconda python 运行）。

图1 v3emp_curve.png  双面板：
  上：success% vs 预算（v3emp/v3d/v1 + A/C 虚线）——准
  下：耗时中位 vs 预算（同策略 + A 参考）——快
「又快又准」= v3emp 的 success 曲线贴住 A、耗时曲线贴住 C。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams["font.family"] = "Microsoft YaHei"
plt.rcParams["axes.unicode_minus"] = False

ROOT = Path(__file__).resolve().parent
SWEEP2 = ROOT / "results" / "budget_sweep2"
FIG = ROOT.parent / "docs" / "figures"

BUDGETS = np.array([2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 30.0, 60.0])
C_STRONG = "#185fa5"; C_FAST = "#888780"; C_MID = "#d85a30"
C_V1 = "#7a6ea8"; C_EMP = "#1d9e75"

data = json.loads((SWEEP2 / "v3emp_analysis.json").read_text(encoding="utf-8"))
succ = data["success"]; usage = data["usage"]

series = [("dert_v1", "dert_v1（时间路由）", C_V1, "o"),
          ("dert_v3d", "dert_v3d（词表难度）", C_MID, "s"),
          ("dert_v3emp", "dert_v3emp（经验难度）", C_EMP, "D")]

fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(7.6, 7.0), dpi=150,
                               sharex=True)
for key, label, color, mk in series:
    ys = [succ[f"{key}@{b:g}"] for b in BUDGETS]
    ax1.plot(BUDGETS, ys, marker=mk, color=color, lw=2, label=label)
a = [succ[f"fixed_strong@{b:g}"] for b in BUDGETS]
c = [succ[f"fixed_fast@{b:g}"] for b in BUDGETS]
ax1.plot(BUDGETS, a, ls="--", color=C_STRONG, lw=1.4, label="A 全强档")
ax1.plot(BUDGETS, c, ls=":", color=C_FAST, lw=1.4, label="C 全快档")
ax1.set_ylabel("success%（correct 且按时）")
ax1.set_title("v3emp 在线确认：经验难度路由的「又快又准」（sweep2，N=100/格）\n"
              "影子流量失败史替代手写词表——回放预测 97%@强档27% 的在线检验",
              fontsize=10.5)
ax1.set_ylim(0, 105)
ax1.grid(alpha=0.25)
ax1.legend(fontsize=8.5, loc="lower right")

for key, label, color, mk in series:
    ts = [usage[f"{key}@{b:g}"]["time_med"] for b in BUDGETS
          if f"{key}@{b:g}" in usage]
    bs = [b for b in BUDGETS if f"{key}@{b:g}" in usage]
    ax2.plot(bs, ts, marker=mk, color=color, lw=2, label=label)
a_t = [usage[f"fixed_strong@{b:g}"]["time_med"] for b in BUDGETS
       if f"fixed_strong@{b:g}" in usage]
ax2.plot(BUDGETS, a_t, ls="--", color=C_STRONG, lw=1.4, label="A 全强档")
ax2.set_xscale("log")
ax2.set_xticks(BUDGETS)
ax2.set_xticklabels([f"{b:g}s" for b in BUDGETS], fontsize=8.5)
ax2.minorticks_off()
ax2.set_xlabel("时间预算")
ax2.set_ylabel("耗时中位（秒）")
ax2.grid(alpha=0.25)
ax2.legend(fontsize=8.5, loc="upper left")

fig.tight_layout()
fig.savefig(FIG / "v3emp_curve.png")
plt.close(fig)
print("→ docs/figures/v3emp_curve.png")
