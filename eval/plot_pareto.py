"""Pareto 曲线图：成功率 vs 时间预算（4 策略），支持误差带。

数据源：eval/results/pareto_*.json（默认取最新一份）。
输出：docs/figures/pareto_<时间戳>.png（与数据文件同时间戳命名）。

兼容性：summary 新格式（--repeats>1）= {mean, std, repeats}，画 ±1 std 色带；
旧格式 = 裸成功率数值，不画误差带。

用法（项目根目录）：
    python eval/plot_pareto.py                # 最新结果
    python eval/plot_pareto.py eval/results/pareto_20260825_1856.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

plt.rcParams["font.family"] = ["Microsoft YaHei", "SimHei", "sans-serif"]
plt.rcParams["axes.unicode_minus"] = False

STRATEGY_STYLE = {  # 名称 → (标签, 颜色, 标记, 线型)
    "fixed_fast":   ("固定快档 qwen2.5:7b", "#0e9f6e", "o", "-"),
    "fixed_strong": ("固定强档 deepseek-chat", "#4b3fe3", "s", "-"),
    "dert":         ("DeRT v0（快档+wrap_up）", "#c27803", "^", "-"),
    "dert_v1":      ("DeRT v1（双档路由）", "#d0342c", "D", "-"),
    "dert_v2":      ("DeRT v2（量化路由+数值优先）", "#00838f", "v", "-"),
    "dert_full":    ("DeRT 完整版（路由+并行+压缩）", "#6a1b9a", "P", "-"),
}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if len(sys.argv) > 1:
        data_path = Path(sys.argv[1])
    else:
        candidates = sorted(Path("eval/results").glob("pareto_*.json"))
        if not candidates:
            print("eval/results/ 下没有 pareto_*.json")
            return 1
        data_path = candidates[-1]

    data = json.loads(data_path.read_text(encoding="utf-8"))
    budgets = data["budgets"]
    strategies = [s for s in data["strategies"]]

    fig, axes = plt.subplots(1, 2, figsize=(12.5, 4.8))

    # ---- 左图：主 Pareto（成功率 = 正确 且 按时）----
    ax = axes[0]
    for strat in strategies:
        label, color, marker, ls = STRATEGY_STYLE.get(
            strat, (strat, "#888888", "o", "-"))
        ys, errs = [], []
        for b in budgets:
            cell = data["summary"][f"{strat}@{b:g}s"]
            # 兼容新旧格式：新={mean,std,repeats}，旧=裸数值
            mean = cell["mean"] if isinstance(cell, dict) else cell
            std = cell.get("std", 0.0) if isinstance(cell, dict) else 0.0
            ys.append(mean * 100)
            errs.append(std * 100)
        ax.plot(budgets, ys, marker=marker, color=color, ls=ls,
                label=label, lw=1.8, ms=6)
        if any(e > 0 for e in errs):  # 有重复才画误差带
            ax.fill_between(budgets, [y - e for y, e in zip(ys, errs)],
                            [y + e for y, e in zip(ys, errs)],
                            color=color, alpha=0.13, lw=0)
    ax.set_xscale("log")
    ax.set_xticks(budgets)
    ax.set_xticklabels([f"{b:g}" for b in budgets])
    ax.set_xlabel("时间预算（秒，对数轴）")
    ax.set_ylabel("任务成功率（内容正确 且 按时交付）%")
    n_tasks = data.get("n_tasks", "?")
    reps = data.get("repeats", 1)
    rep_note = f"，{reps} 次重复，色带=±1 std" if reps > 1 else ""
    ax.set_title(f"Pareto 曲线（{n_tasks} 任务，全实时无缓存{rep_note}）")
    ax.set_ylim(-5, 105)
    ax.grid(alpha=0.3, which="both")
    ax.legend(loc="lower right", fontsize=9)

    # ---- 右图：正确率 / 按时率拆解 ----
    ax = axes[1]
    for strat in strategies:
        label, color, marker, ls = STRATEGY_STYLE.get(
            strat, (strat, "#888888", "o", "-"))
        # 拆解数据在 grid 里
        corr, ont = [], []
        for b in budgets:
            cells = data["grid"][f"{strat}@{b:g}s"]
            corr.append(sum(c["correct"] for c in cells) / len(cells) * 100)
            ont.append(sum(c["on_time"] for c in cells) / len(cells) * 100)
        ax.plot(budgets, corr, marker=marker, color=color, ls="-",
                lw=1.4, ms=5, alpha=0.9)
        ax.plot(budgets, ont, marker=marker, color=color, ls="--",
                lw=1.4, ms=5, alpha=0.45)
    ax.set_xscale("log")
    ax.set_xticks(budgets)
    ax.set_xticklabels([f"{b:g}" for b in budgets])
    ax.set_xlabel("时间预算（秒，对数轴）")
    ax.set_ylabel("百分比 %")
    ax.set_title("拆解：实线=内容正确率，虚线=按时率")
    ax.set_ylim(-5, 105)
    ax.grid(alpha=0.3, which="both")

    # 图例（策略 × 线型说明）
    handles = [plt.Line2D([], [], color=c, marker=m, ls="-",
                          label=STRATEGY_STYLE.get(s, (s, "#888", "o", "-"))[0])
               for s, (_, c, m, _) in
               ((s, STRATEGY_STYLE.get(s, (s, "#888888", "o", "-")))
                for s in strategies)]
    handles += [plt.Line2D([], [], color="#555", ls="-", label="内容正确率"),
                plt.Line2D([], [], color="#555", ls="--", label="按时率")]
    ax.legend(handles=handles, loc="lower right", fontsize=8)

    fig.suptitle("DeRT · 时间是和 Token 一样需要调度的资源", y=1.02, fontsize=11,
                 color="#5c5c66")
    fig.tight_layout()

    out_dir = Path("docs/figures")
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = data_path.stem.replace("pareto_", "")
    out = out_dir / f"pareto_{stem}.png"
    fig.savefig(out, dpi=160, bbox_inches="tight")
    print(f"saved -> {out}  (source: {data_path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
