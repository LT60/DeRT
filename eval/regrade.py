"""离线重算已跑完网格的聚合结果（不用重跑）。

用途：聚合逻辑修 bug 后，原始 records 还在，直接重判分 + 重聚合即可，
不必再烧几小时重跑。2026-09-29 首次使用：修 pareto_routine.py 的
"dert_full 未被识别为预算感知策略" bug（720 条塞进每格，正确率恒定假值）。

用法：
    python eval/regrade.py eval/results/pareto_20260929_1820.json
    python eval/regrade.py <结果.json> --out <新文件>   # 默认原地覆写
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.tasks import TASKS  # noqa: E402


def regrade(path: Path, out: Path | None) -> int:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    budgets = d["budgets"]
    strategies = d["strategies"]
    records = d["records"]

    by_id = {t["type"] + "/" + t["id"]: t for t in TASKS}

    grid: dict[tuple[str, float], list[dict]] = {}
    for strat in strategies:
        for b in budgets:
            grid[(strat, b)] = []

    n_missing = 0
    for rec in records:
        task = by_id.get(rec["task"])
        if task is None:
            n_missing += 1
            continue
        correct, why = task["check"](rec["answer"] or "")
        strat = rec["strategy"]
        # 与 pareto_routine.py 修复后一致：按记录有无 budget 决定归格方式
        if rec["budget"] is not None:
            b = rec["budget"]
            ot = rec["total_time"] is not None and rec["total_time"] <= b
            grid[(strat, b)].append(
                {"task": rec["task"], "correct": correct, "on_time": ot,
                 "success": correct and ot, "why": why,
                 "total_time": rec["total_time"], "wrap_up": rec.get("wrap_up"),
                 "repeat": rec["repeat"]})
        else:
            for b in budgets:
                ot = rec["total_time"] is not None and rec["total_time"] <= b
                grid[(strat, b)].append(
                    {"task": rec["task"], "correct": correct, "on_time": ot,
                     "success": correct and ot, "why": why,
                     "total_time": rec["total_time"],
                     "repeat": rec["repeat"]})

    print("==== 重算后 Pareto 网格（成功率 = 内容正确 且 按时；mean±std）====")
    header = "strategy      " + "".join(f"{b:>12g}s" for b in budgets)
    print(header)
    summary = {}
    for strat in strategies:
        row = f"{strat:13s}"
        for b in budgets:
            cells = grid[(strat, b)]
            by_rep: dict[int, list[bool]] = {}
            for c in cells:
                by_rep.setdefault(c["repeat"], []).append(c["success"])
            rates = [sum(v) / len(v) for _, v in sorted(by_rep.items())]
            mean = statistics.mean(rates) if rates else 0.0
            std = statistics.stdev(rates) if len(rates) > 1 else 0.0
            row += f"{mean * 100:>6.0f}±{std * 100:<5.0f}"
            summary[f"{strat}@{b:g}s"] = {
                "mean": round(mean, 4), "std": round(std, 4),
                "repeats": [round(x, 4) for x in rates]}
        print(row)

    print("\n==== 拆解（correct% / on_time%，全重复合并）====")
    for strat in strategies:
        row = f"{strat:13s}"
        for b in budgets:
            cells = grid[(strat, b)]
            cp = sum(c["correct"] for c in cells) / len(cells) if cells else 0
            op = sum(c["on_time"] for c in cells) / len(cells) if cells else 0
            row += f"  {cp * 100:3.0f}/{op * 100:3.0f}"
        print(row)

    print("\n==== 每格记录数（应全部等于 任务数×重复数）====")
    for strat in strategies:
        print(f"  {strat:13s} " +
              " ".join(f"{len(grid[(strat, b)]):5d}" for b in budgets))

    d["summary"] = summary
    d["grid"] = {f"{s}@{b:g}s": v for (s, b), v in grid.items()}
    d["regraded"] = True
    target = out or path
    target.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    print(f"\n已写入 {target}"
          f"{'（原地覆写）' if out is None else ''}"
          f"  | 未匹配任务 {n_missing} 条")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("result", help="结果 JSON 路径")
    ap.add_argument("--out", default="", help="输出到新文件（默认原地覆写）")
    a = ap.parse_args()
    return regrade(Path(a.result), Path(a.out) if a.out else None)


if __name__ == "__main__":
    sys.exit(main())
