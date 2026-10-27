# -*- coding: utf-8 -*-
"""干净复测主线图：五策略 success% 平滑曲线（sweep2, n=100/格，2026-10-24）。

平滑用 PCHIP（Fritsch-Carlson 单调三次插值，纯 numpy 手写，无 scipy 依赖）——
单调数据不会产生过冲假峰，符合测量纪律。
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

RUN_DIR = Path(__file__).resolve().parent / "results" / "budget_sweep2"
data = json.loads((RUN_DIR / "v3_analysis.json").read_text(encoding="utf-8"))
table = data["table"]
BUDGETS = np.array([2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 30.0, 60.0])
LX = np.log10(BUDGETS)


def pchip_slopes(x, y):
    """Fritsch-Carlson 单调三次样条的端点斜率。"""
    h = np.diff(x)
    d = np.diff(y) / h
    m = np.zeros_like(y)
    m[0], m[-1] = d[0], d[-1]
    for i in range(1, len(y) - 1):
        if d[i - 1] * d[i] <= 0:
            m[i] = 0.0
        else:
            w1, w2 = 2 * h[i] + h[i - 1], h[i] + 2 * h[i - 1]
            m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i])
    return m


def pchip_eval(x, y, xq):
    m = pchip_slopes(x, y)
    yq = np.empty_like(xq)
    for j, xv in enumerate(xq):
        i = min(np.searchsorted(x, xv) - 1, len(x) - 2)
        i = max(i, 0)
        h = x[i + 1] - x[i]
        t = (xv - x[i]) / h
        t2, t3 = t * t, t * t * t
        h00 = 2 * t3 - 3 * t2 + 1
        h10 = t3 - 2 * t2 + t
        h01 = -2 * t3 + 3 * t2
        h11 = t3 - t2
        yq[j] = h00 * y[i] + h10 * h * m[i] + h01 * y[i + 1] + h11 * h * m[i + 1]
    return yq


def smooth(key):
    y = np.array([table[f"{b:.1f}"][key] for b in BUDGETS], dtype=float)
    xq = np.linspace(LX[0], LX[-1], 240)
    return xq, np.clip(pchip_eval(LX, y, xq), 0, 100)


def strat_values(strategy):
    """任意策略的 success% 直接从 raw.jsonl 计算。"""
    recs = [json.loads(l) for l in
            (RUN_DIR / "raw.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    out = []
    for b in BUDGETS:
        g = [r for r in recs if r["strategy"] == strategy and r["budget"] == b]
        out.append(100.0 * sum(1 for r in g if r["correct"] and r["on_time"]) / len(g))
    return np.array(out)


fig, ax = plt.subplots(figsize=(9.5, 5.6), dpi=150)

series_cfg = [
    # (key, label, color, lw, ls, src)  src: "table"=分析表 / "raw"=raw.jsonl 现算
    ("A", "A 固定强模型（100% 强档）", "#888780", 1.8, "--", "table"),
    ("C", "C 固定快模型（0% 强档）", "#1D9E75", 1.8, "-", "table"),
    ("dert_v1", "v1.1 时间驱动路由", "#B4B2A9", 1.2, "--", "raw"),
    ("v3b_suc", "v3b 纯难度路由（31% 强档）", "#D85A30", 1.6, "-", "table"),
    ("v3_suc", "v3 难度路由+8s 手拍门（12% 强档）", "#534AB7", 1.8, "-", "table"),
    ("dert_v3d", "v3d 难度路由+自适应门（24% 强档，冠军）", "#185FA5", 2.6, "-", "raw"),
]
for key, label, color, lw, ls, src in series_cfg:
    if src == "table":
        y = np.array([table[f"{b:.1f}"][key] for b in BUDGETS], dtype=float)
    else:
        y = strat_values(key)
    ax.plot(BUDGETS, y, "o", color=color, ms=3.5, mfc="white", mew=1.1)
    ax.plot(BUDGETS, y, color=color, lw=lw, ls=ls, label=label)

ax.set_xscale("log")
ax.set_xticks(BUDGETS)
ax.set_xticklabels([f"{b:g}s" for b in BUDGETS])
ax.minorticks_off()
ax.set_xlabel("时间预算（对数轴）")
ax.set_ylabel("success%（按时且答对）")
ax.set_ylim(0, 103)
ax.set_title("DeRT 难度路由+时间门主结果（sweep2 全量 6000 run，n=100/格，2026-10-24）")
ax.grid(alpha=0.25)
ax.legend(loc="upper left", fontsize=9)

ax.axvline(6.37, color="#D85A30", lw=0.9, ls=":")
ax.annotate("交叉点 ≈ 强档中位延迟 6.4s\n预算 ≥ 强档延迟，难度路由才开始兑现",
            xy=(6.37, 30), fontsize=9, color="#993C1D", ha="left", xytext=(7.0, 14))

fig.tight_layout()
dest = Path(__file__).resolve().parent.parent / "docs" / "figures" / "v3_clean_rerun.png"
fig.savefig(dest)
print(f"-> {dest}")
sys.exit(0)
