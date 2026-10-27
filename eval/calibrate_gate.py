# -*- coding: utf-8 -*-
"""标定卡：为指定的强模型测出推荐时间门（方案 B——固定门 + 标定流程）。

与 v3d 自适应门的关系（2026-10-24 讨论定案）：
  v3d  = 运行时闭环自适应，门只从先验向上调（后端变慢时自保护）；
  本工具 = 部署时一次性标定：跑一批难任务（宽松预算、无时间压力），测强档
  任务耗时的自然完成分布，给出 P60 + 边际 作为该模型的推荐门常数。
  两者估计对象相同（强档任务自然完成耗时），交叉验证：自适应门的收敛值
  应≈标定卡的推荐值。

用法（约 3 分钟、¥0.1）：
  python -m eval.calibrate_gate --provider bailian --n 20
输出：eval/results/gate_calibration_<provider>.json（标定卡）

换强模型 / 强模型明显变慢（运行时监控发现实际 TTFT 偏离标定值）时重跑。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from src.llm import OpenAICompatLLM, bailian_llm, deepseek_llm  # noqa: E402
from src.scheduler import DeadlineSchedulerV3  # noqa: E402
from eval.tasks import TASKS  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from budget_eval import run_once  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent / "results"
CMP_WORDS = DeadlineSchedulerV3.CMP_WORDS
LOOSE_BUDGET = 120.0   # 标定预算：远大于任何强档任务，保证自然完成
MARGIN = 0.8           # 与 v3d 同款不对称边际


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="bailian", choices=["bailian", "deepseek"])
    ap.add_argument("--n", type=int, default=20, help="标定任务数（难任务子集抽样）")
    ap.add_argument("--quantile", type=float, default=0.60)
    args = ap.parse_args()

    strong = (bailian_llm(cache_dir="") if args.provider == "bailian"
              else deepseek_llm(cache_dir=""))
    hard = [t for t in TASKS if any(w in t["prompt"] for w in CMP_WORDS)]
    # 均匀抽样 n 个难任务（覆盖 T1/T2/T3）
    step = max(1, len(hard) // args.n)
    tasks = hard[::step][:args.n]
    print(f"标定模型={strong.model} | 难任务 {len(tasks)}/{len(hard)} | 预算 {LOOSE_BUDGET:g}s（自然完成）")

    durations, skipped = [], 0
    for i, t in enumerate(tasks):
        rec = run_once(t, strong, strong.model, LOOSE_BUDGET)
        if rec["total_time"] is None or rec["wrap_up"]:
            skipped += 1
            continue
        durations.append(rec["total_time"])
        print(f"  [{i+1}/{len(tasks)}] {t['type']}/{t['id']}  {rec['total_time']:.1f}s")

    durations.sort()
    k = min(len(durations) - 1, int(args.quantile * (len(durations) - 1)))
    p60 = durations[k] if durations else None
    gate = p60 + MARGIN if p60 else None
    card = {
        "model": strong.model,
        "provider": args.provider,
        "calibrated_at": datetime.now().isoformat(timespec="seconds"),
        "n_tasks": len(tasks), "n_natural": len(durations), "n_skipped": skipped,
        "p50": durations[len(durations) // 2] if durations else None,
        f"p{int(args.quantile*100)}": p60,
        "margin": MARGIN,
        "recommended_gate": round(gate, 2) if gate else None,
        "range": [durations[0], durations[-1]] if durations else None,
        "note": "推荐门 = P60 + 0.8（与 v3d 同款边际）。写入部署配置后运行时零估计；"
                "运行时监控若发现强档实际耗时持续高于标定值，重跑本工具。",
    }
    dest = RESULTS_DIR / f"gate_calibration_{args.provider}.json"
    dest.write_text(json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n标定卡 -> {dest}")
    print(json.dumps(card, ensure_ascii=False, indent=1))
    sys.exit(0)


if __name__ == "__main__":
    sys.exit(main())
