"""qwen TTFT 复测：判断 2026-09-28 网格数据是否被环境异常污染。

历史基线（2026-08-24 实测）：冷启动首调用 TTFT ~65s，热身 TTFT ~0.148s。
2026-09-28 网格里 qwen TTFT P50=2.222s、min=2.087s —— 高了 15 倍且分布很窄
（不像是抖动，像地板被抬高）。本脚本用项目自己的 llm.py（与评估同一代码路径）
复测，确认机器当前状态是否健康。

用法：python -u _ttft_check.py [重复次数]
"""
from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from src.llm import OpenAICompatLLM          # noqa: E402
from src.tools import default_tools  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 6

llm = OpenAICompatLLM(cache_dir="")
print(f"model={llm.model} base_url={llm.base_url}  n={N}")

# 与评估同构的一次调用：带 8 个工具 schema + 运维口吻的系统提示，
# 这样测到的 TTFT 才和网格里的口径一致（空 prompt 会低估）。
SYSTEM = (
    "你是一个实时检测系统的运维助手。可以调用工具查询指标，拿到结果后用 "
    "FINAL: 开头给出简短结论。"
)
msgs = [{"role": "system", "content": SYSTEM},
        {"role": "user", "content": "最近 200 帧（约 3.3 秒）的 P95 延迟是多少毫秒？"}]
tools = default_tools().specs()   # 与 agent.py 同一入口（tools.specs()）

print("\n--- 冷启动（首次调用，含模型加载）---")
t0 = time.time()
r = llm.chat(msgs, tools=tools)
print(f"TTFT={r.ttft:.3f}s  total={r.total:.3f}s  (墙钟 {time.time() - t0:.3f}s)")

print(f"\n--- 热身后续 {N} 次 ---")
ttfts, totals = [], []
for i in range(N):
    r = llm.chat(msgs, tools=tools)
    ttfts.append(r.ttft)
    totals.append(r.total)
    print(f"  #{i + 1}  TTFT={r.ttft:.3f}s  total={r.total:.3f}s")


def stat(name, xs):
    xs = sorted(xs)
    p50 = statistics.median(xs)
    p95 = xs[max(0, int(round(0.95 * (len(xs) - 1))))]
    print(f"{name:7s} min={xs[0]:.3f}  P50={p50:.3f}  P95={p95:.3f}  "
          f"max={xs[-1]:.3f}")


stat("TTFT", ttfts)
stat("total", totals)

# 判定阈值的来历（别照抄历史 0.148s）：
#   0.148s 是 2026-08-24 用**4 个工具**测的；8/26 工具集扩到 8 个后，schema
#   prefill 变长，实测 0 工具 0.026s / 8 工具 0.439s —— 这 0.4s 是真实开销。
#   真正要拦的是 2026-09-28 那次 localhost→::1 的 2 秒地板（TTFT 2.222s）。
#   故阈值取 0.8s：低于它说明没有人为地板，可以重跑网格。
p50 = statistics.median(ttfts)
print()
if p50 < 0.8:
    print(f"✅ 健康：TTFT P50={p50:.3f}s（8 工具口径的真实量级，"
          f"已排除 2 秒 IPv6 地板），可重跑网格")
else:
    print(f"❌ 异常：TTFT P50={p50:.3f}s 远超 8 工具口径的 ~0.45s；"
          f"先修环境再重跑，否则数据不可用于 README")
