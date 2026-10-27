# -*- coding: utf-8 -*-
"""影子流量难度史构建器（经验难度信号的部署前置，2026-10-25）。

从一次已完成的全量采集（raw.jsonl）提取「快档答错的任务集合」——
心理测量学 1−p 难度指数的最简形态（难度 = 历史快档失败）。

口径纪律：
  - 历史源与评估跑必须是**不同夜次**（同任务重复采样，跨会话泛化才是主张；
    同夜提取会变成循环论证——analyze_difficulty.py 的 gold 即同夜口径）
  - 历史绑定快档模型：env.json 里 fast.model 变了就要重建历史（P4 同理）

用法：
    python eval/build_difficulty_history.py \
        --from-run budget_main100 --out eval/results/difficulty_history_main100.json
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-run", required=True, help="results 下 budget_<run> 目录名")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    src = RESULTS / f"budget_{args.from_run}" / "raw.jsonl"
    env = RESULTS / f"budget_{args.from_run}" / "env.json"
    fast_model = strong_model = None
    if env.exists():
        e = json.loads(env.read_text(encoding="utf-8"))
        fast_model = (e.get("fast") or {}).get("model")
        strong_model = (e.get("strong") or {}).get("model")

    wrong: dict[str, bool] = {}
    n_fast = 0
    for line in src.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r["strategy"] != "fixed_fast":
            continue
        n_fast += 1
        if not r.get("correct"):
            wrong[r["task"]] = True

    out = {
        "version": 1,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "source_run": args.from_run,
        "fast_model": fast_model, "strong_model": strong_model,
        "n_fast_runs": n_fast, "n_wrong": len(wrong),
        "usage": "DeadlineSchedulerV3Emp(fast, strong, history=本文件 fast_wrong)；"
                 "未知任务默认 easy（冷启动保守）",
        "fast_wrong": wrong,
    }
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    print(f"history: {len(wrong)} wrong / {n_fast} fast runs "
          f"(fast={fast_model}) → {args.out}")


if __name__ == "__main__":
    main()
