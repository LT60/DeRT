"""全 60 条任务的本地验证（qwen，零成本）：抓措辞改回归。

判据（任一条命中即需人工看）：
  ① 判分不过（模型没答对）
  ② 期望工具没被全部调用（工具路由被措辞带偏）
  ③ 步数 ≥5 或耗时 >20s（模型发散）
deadline=60 仅用于兜底防爆（避免单条跑出几百秒），不代表正式评估口径。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from src.agent import run                # noqa: E402
from src.llm import OpenAICompatLLM      # noqa: E402
from src.tools import default_tools      # noqa: E402
from eval.tasks import TASKS             # noqa: E402

fast = OpenAICompatLLM(cache_dir="")
print("[warmup]", end=" ", flush=True)
fast.chat([{"role": "user", "content": "回复 ok"}])
print("done\n")

bad = []
coverage = {}   # tool -> 调用它的任务数（验证"8/8 工具覆盖"是否真的成立）
for t in TASKS:
    r = run(t["prompt"], llm=fast, tools=default_tools(), model=fast.model,
            deadline=60.0, max_steps=8)
    ok, why = t["check"](r.answer or "")
    used = [e.get("tool") for e in r.trace.events if e["kind"] == "tool_call"]
    for u in used:
        coverage[u] = coverage.get(u, 0) + 1
    missing = [x for x in t["expect_tools"] if x not in used]
    flags = []
    if not ok:
        flags.append("判分✗")
    if missing:
        flags.append(f"缺工具{missing}")
    if r.steps >= 5 or r.total_time > 20:
        flags.append("发散")
    tag = "  ".join(flags)
    print(f"{t['id']:6s} {r.total_time:6.2f}s steps={r.steps} "
          f"{'✓' if ok else '✗'} {tag}")
    if flags:
        bad.append((t["id"], tag, why, used, (r.answer or "")[:120]))

print("\n==== 工具覆盖（实际被调用的任务数）====")
for name in sorted(default_tools().keys()):
    print(f"  {name:18s} {coverage.get(name, 0)}")

print(f"\n==== 需人工确认 {len(bad)}/{len(TASKS)} 条 ====")
for tid, tag, why, used, ans in bad:
    print(f"\n{tid} [{tag}]")
    print(f"  期望: {why}")
    print(f"  实调: {used}")
    print(f"  答案: {ans!r}")
