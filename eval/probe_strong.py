"""强档探测：只跑本地 7b 判错的那批任务，确认 deepseek 在新任务集上不崩。

目的：正式网格要跑 3–5 小时、花十几块钱。开跑前先花几毛钱确认新任务集对
强档不是"不可达"——若 deepseek 在 T2 阈值类上也大面积答错，说明任务集有
系统性问题，得先修再开跑，而不是白烧几小时。

用法：python -u _probe_strong.py [task_id ...]（默认跑 FAIL_IDS）
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from src.agent import run             # noqa: E402
from src.llm import deepseek_llm      # noqa: E402
from src.tools import default_tools   # noqa: E402
from eval.tasks import TASKS          # noqa: E402

# 第一轮本地验证中 qwen 判错 / 发散的任务
FAIL_IDS = ["T1-1", "T1-5", "T1-14", "T1-20", "T2-1", "T2-3", "T2-6", "T2-7",
            "T2-8", "T2-11", "T2-14", "T2-15", "T2-18", "T2-20"]

DEADLINE = 30.0


def main() -> int:
    ids = sys.argv[1:] or FAIL_IDS
    strong = deepseek_llm(cache_dir="")
    print(f"strong={strong.model}  deadline={DEADLINE}s  tasks={len(ids)}")
    print("[warmup]", end=" ", flush=True)
    t0 = time.time()
    strong.chat([{"role": "user", "content": "回复 ok"}])
    print(f"{time.time() - t0:.2f}s\n")

    n_ok = 0
    slow = []
    for tid in ids:
        t = next(x for x in TASKS if x["id"] == tid)
        r = run(t["prompt"], llm=strong, tools=default_tools(),
                model=strong.model, deadline=DEADLINE, max_steps=8)
        ok, why = t["check"](r.answer or "")
        n_ok += ok
        used = [e.get("tool") for e in r.trace.events if e["kind"] == "tool_call"]
        flag = "✓" if ok else f"✗ {why}"
        if r.total_time > 15:
            slow.append((tid, r.total_time))
        print(f"{tid:6s} {r.total_time:6.2f}s steps={r.steps} tools={len(used)} "
              f"{flag}")

    print(f"\n强档通过 {n_ok}/{len(ids)}")
    if slow:
        print("偏慢（>15s，30s 预算档有迟到风险）:")
        for tid, tt in slow:
            print(f"  {tid} {tt:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
