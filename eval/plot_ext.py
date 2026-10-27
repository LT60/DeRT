# -*- coding: utf-8 -*-
"""外部验证与难度方案的两张收官图（2026-10-25，miniconda python 运行）。

图1 bfcl_ext_curves.png      BFCL v4 外部验证曲线（success% vs 预算，3 策略 + A/C 参考线）
图2 difficulty_schemes.png   难度评分方案对比（F1 / 回放 correct% / 强档用量%）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

plt.rcParams["font.family"] = "Microsoft YaHei"
plt.rcParams["axes.unicode_minus"] = False

ROOT = Path(__file__).resolve().parent
FIG = ROOT.parent / "docs" / "figures"
FIG.mkdir(exist_ok=True)

C_STRONG = "#185fa5"; C_FAST = "#888780"; C_MID = "#d85a30"
C_V1 = "#7a6ea8"; C_OK = "#1d9e75"; C_WARN = "#b97517"

# ---------------- 图1：BFCL 外部验证曲线 ----------------
bf = json.loads((ROOT / "results" / "bfcl_bfcl_ext1" / "analysis.json")
                .read_text(encoding="utf-8"))
S = bf["summary"]

fig, ax = plt.subplots(figsize=(7.2, 4.6), dpi=150)
budgets = [10.0, 30.0, 60.0]

series = [
    ("v1",   "dert_v1（时间路由）",        C_V1,   "o"),
    ("v3d",  "dert_v3d（中文难度词表）",   C_MID,  "s"),
    ("v3d_en", "dert_v3d_en（英文重标定）", C_OK,  "^"),
]
for key, label, color, mk in series:
    ys = [S[f"{key}@{b:g}"]["success_pct"] for b in budgets]
    ax.plot(budgets, ys, marker=mk, color=color, lw=2, label=label)
    for b, y in zip(budgets, ys):
        ax.annotate(f"{y:.0f}", (b, y), textcoords="offset points",
                    xytext=(0, 7), ha="center", fontsize=8, color=color)

a_pct = S["A"]["success_pct"]; c_pct = S["C"]["success_pct"]
ax.axhline(a_pct, color=C_STRONG, ls="--", lw=1.4,
           label=f"A 全强档 {a_pct:.1f}%（不感知预算）")
ax.axhline(c_pct, color=C_FAST, ls=":", lw=1.4,
           label=f"C 全快档 {c_pct:.1f}%（不感知预算）")

ax.set_xscale("log")
ax.set_xticks(budgets)
ax.set_xticklabels(["10s", "30s", "60s"])
ax.minorticks_off()
ax.set_xlabel("时间预算 / episode")
ax.set_ylabel("success%（逐轮状态比对全过）")
ax.set_title("BFCL v4 多轮外部验证（N=24 题 × 11 策略格 = 264 run）\n"
             "宽预算 B≡A 与紧预算悬崖均跨域复现；v1@10 显著劣于 A（p=0.03）",
             fontsize=10.5)
ax.set_ylim(0, 58)
ax.grid(alpha=0.25)
ax.legend(fontsize=8, loc="upper left")
fig.tight_layout()
fig.savefig(FIG / "bfcl_ext_curves.png")
plt.close(fig)
print("→ docs/figures/bfcl_ext_curves.png")

# ---------------- 图2：难度评分方案对比 ----------------
ds = json.loads((ROOT / "results" / "budget_sweep2" / "difficulty_schemes.json")
                .read_text(encoding="utf-8"))
order = ["cmp_words", "struct_type", "struct_ntools", "emp_cross_night"]
labels = {"cmp_words": "词表（现状基线）\n绑语言",
          "struct_type": "预注册分层\nT2/T3·语言无关",
          "struct_ntools": "工具链长度\n语言无关",
          "emp_cross_night": "经验难度·留出夜次\n语言无关·需历史"}
f1 = [ds["schemes"][k]["f1"] for k in order]
corr = [ds["schemes"][k]["correct_pct"] for k in order]
strong = [ds["schemes"][k]["strong_pct"] for k in order]
oracle_c = ds["oracle"]["correct_pct"]

x = np.arange(len(order))
w = 0.28
fig, ax = plt.subplots(figsize=(8.6, 4.8), dpi=150)
b1 = ax.bar(x - w, f1, w, color=C_WARN, label="判别 F1（对 gold=真需强档）")
b2 = ax.bar(x, corr, w, color=C_STRONG, label="回放 correct%")
b3 = ax.bar(x + w, strong, w, color=C_FAST, label="强档用量%")
for bars in (b1, b2, b3):
    for r in bars:
        ax.annotate(f"{r.get_height():g}", (r.get_x() + r.get_width() / 2,
                    r.get_height()), textcoords="offset points",
                    xytext=(0, 3), ha="center", fontsize=8)
ax.axhline(oracle_c, color=C_STRONG, ls="--", lw=1.2)
ax.annotate(f"oracle 上限 {oracle_c:.0f}%（强档 26%）",
            (len(order) - 0.5, oracle_c), textcoords="offset points",
            xytext=(0, 4), ha="right", fontsize=8, color=C_STRONG)
ax.set_xticks(x)
ax.set_xticklabels([labels[k] for k in order], fontsize=8.5)
ax.set_ylabel("%（F1 × 100）")
ax.set_ylim(0, 108)
ax.set_title("难度评分方案对比（sweep2 N=100 回放，gold=快档错误 26 题）\n"
             "经验难度（历史快档失败）F1 0.87 完胜手写词表 0.53，且语言无关——通用方案成立",
             fontsize=10.5)
ax.grid(axis="y", alpha=0.25)
ax.legend(fontsize=8.5, loc="lower right")
fig.tight_layout()
fig.savefig(FIG / "difficulty_schemes.png")
plt.close(fig)
print("→ docs/figures/difficulty_schemes.png")
