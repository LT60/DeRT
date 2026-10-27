# -*- coding: utf-8 -*-
"""中档探针：为三档路由（快/中/强）选「中」档模型。

问题：31% 强档调用里，有多少能降档到更便宜的中档？
方法：对候选中档模型跑 hard 任务族（CMP_WORDS，与标定卡同款抽样），
  宽松预算 120s 自然完成，测 correct 率 + 延迟分布 + token 用量；
  与 sweep2 的 fixed_strong（强档逐题对错）按 task id 对齐。

两阶段（控制配额消耗）：
  smoke：每候选 3 道难题——筛掉不支持工具调用 / 直接崩的；
  full ：幸存者各 20 道难题——正式数据。

用法：
  python -m eval.probe_mid --stage smoke
  python -m eval.probe_mid --stage full --models qwen3.8-flash,qwen3.7-plus

输出：eval/results/mid_probe_<stage>.json
判定标准（写入结论时用）：
  中档可用 ⇔ hard correct 率 ≥ 强档同题 correct 率 × 0.8，
  且 P60 延迟 < 强档 P50（否则失去「补 4–8s 档」的意义）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from src.llm import bailian_llm  # noqa: E402
from src.scheduler import DeadlineSchedulerV3  # noqa: E402
from eval.tasks import TASKS  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from budget_eval import run_once  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent / "results"
CMP_WORDS = DeadlineSchedulerV3.CMP_WORDS
LOOSE_BUDGET = 120.0

# 候选中档（文本对话、能力位介于本地 7b 与 deepseek-v4.1-flash 之间）
CANDIDATES = [
    "deepseek-v4-flash-0731",  # 同家族上代 flash（家族连续性最优）
    "qwen3.8-flash",           # 最新 qwen flash 代（预期最便宜）
    "qwen3.7-plus",            # 经典 plus 档
    "qwen3.5-plus",            # 上一代 plus
    "glm-5.3-flash",           # GLM 家族 flash
]


def hard_tasks(n: int) -> list:
    hard = [t for t in TASKS if any(w in t["prompt"] for w in CMP_WORDS)]
    step = max(1, len(hard) // n)
    return hard[::step][:n]


def strong_baseline(task_ids: set[str]) -> dict[str, bool]:
    """sweep2 fixed_strong 逐题对错（强档基线）。task 键 = 'T1/T1-1' 格式。"""
    raw = RESULTS_DIR / "budget_sweep2" / "raw.jsonl"
    out: dict[str, bool] = {}
    for line in raw.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r["strategy"] == "fixed_strong" and r["task"] in task_ids:
            out[r["task"]] = bool(r["correct"])
    return out


def probe_model(model: str, tasks: list, strong_map: dict[str, bool]) -> dict:
    llm = bailian_llm(model=model, cache_dir="")
    rows = []
    for i, t in enumerate(tasks):
        try:
            rec = run_once(t, llm, model, LOOSE_BUDGET)
            correct, why = t["check"](rec["answer"] or "")
            row = {
                "task": t["id"], "correct": bool(correct), "check_why": why,
                "on_time": (rec["total_time"] is not None
                            and rec["total_time"] <= LOOSE_BUDGET),
                "wrap_up": bool(rec["wrap_up"]),
                "total_time": rec["total_time"],
                "tokens_in": rec["tokens_in"], "tokens_out": rec["tokens_out"],
                "error": rec["error"],
            }
        except Exception as e:  # 模型级异常（不支持 tools 等）也要记录不炸全场
            row = {"task": t["id"], "correct": False, "on_time": False,
                   "wrap_up": False, "total_time": None, "tokens_in": None,
                   "tokens_out": None, "error": f"{type(e).__name__}: {e}"[:200]}
        rows.append(row)
        flag = "ok " if row["correct"] else ("err" if row["error"] else "miss")
        dur = f"{row['total_time']:.1f}s" if row["total_time"] else "-"
        print(f"  [{i+1}/{len(tasks)}] {t['id']:>12s} {flag} {dur}")

    durs = sorted(r["total_time"] for r in rows
                  if r["total_time"] is not None and not r["wrap_up"])
    p50 = durs[len(durs) // 2] if durs else None
    p60 = durs[min(len(durs) - 1, int(0.60 * (len(durs) - 1)))] if durs else None
    ok_rows = [r for r in rows if not r["error"]]
    n_cmp = len(strong_map)
    agree = sum(1 for t, r in zip(tasks, rows)
                if f"{t['type']}/{t['id']}" in strong_map
                and r["correct"] == strong_map[f"{t['type']}/{t['id']}"])
    return {
        "model": model,
        "n": len(tasks), "n_error": sum(1 for r in rows if r["error"]),
        "hard_correct": round(100 * sum(1 for r in ok_rows if r["correct"])
                              / len(ok_rows), 1) if ok_rows else 0.0,
        "p50_s": round(p50, 2) if p50 else None,
        "p60_s": round(p60, 2) if p60 else None,
        "own_gate_s": round(p60 + 0.8, 2) if p60 else None,
        "avg_tokens_in": round(sum(r["tokens_in"] or 0 for r in rows) / len(rows)),
        "avg_tokens_out": round(sum(r["tokens_out"] or 0 for r in rows) / len(rows)),
        "vs_strong_agree_pct": round(100 * agree / n_cmp, 1) if n_cmp else None,
        "rows": rows,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="smoke", choices=["smoke", "full"])
    ap.add_argument("--models", default=",".join(CANDIDATES))
    ap.add_argument("--n", type=int, default=20, help="full 阶段题数")
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    n = 3 if args.stage == "smoke" else args.n
    tasks = hard_tasks(n)
    strong_map = strong_baseline({f"{t['type']}/{t['id']}" for t in tasks})
    print(f"阶段={args.stage} | 候选 {models} | 难题 {len(tasks)} 道 | "
          f"强档基线覆盖 {len(strong_map)}/{len(tasks)}")

    results = [probe_model(m, tasks, strong_map) for m in models]

    strong_correct = round(100 * sum(strong_map.values()) / len(strong_map), 1) \
        if strong_map else None
    doc = {
        "stage": args.stage, "at": datetime.now().isoformat(timespec="seconds"),
        "budget": LOOSE_BUDGET, "task_ids": [t["id"] for t in tasks],
        "strong_model": "deepseek-v4.1-flash",
        "strong_hard_correct_pct": strong_correct,
        "results": results,
    }
    dest = RESULTS_DIR / f"mid_probe_{args.stage}.json"
    dest.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n-> {dest}")
    print(f"{'model':28s} {'correct':>8s} {'P50':>6s} {'P60':>6s} {'门':>6s} {'agree':>7s} {'err':>4s}")
    for r in results:
        print(f"{r['model']:28s} {r['hard_correct']:7.1f}% "
              f"{(r['p50_s'] or 0):5.1f}s {(r['p60_s'] or 0):5.1f}s "
              f"{(r['own_gate_s'] or 0):5.1f}s {r['vs_strong_agree_pct'] or 0:6.1f}% "
              f"{r['n_error']:4d}")
    if strong_correct:
        print(f"\n强档基线（deepseek-v4.1-flash 同题 correct）: {strong_correct}%")
    sys.exit(0)


if __name__ == "__main__":
    sys.exit(main())
