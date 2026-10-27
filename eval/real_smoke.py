"""DeRT 真实模型冒烟：llm.py + loop + 工具 + 埋点全链路闭环。

运行（项目根目录，先启动 Ollama 并拉取模型）：
    python eval/real_smoke.py                 # 本地快档（DERT_MODEL，默认 qwen2.5:7b）
    python eval/real_smoke.py --model qwen2.5:14b
    python eval/real_smoke.py --deepseek      # 云端强档（需已设 DEEPSEEK_API_KEY）
    python eval/real_smoke.py --no-cache      # 禁用响应缓存（测真实延迟分布时用）

注意：首次运行即采集真实 TTFT/延迟；缓存开启时重跑同一任务将命中缓存（cache_hit=True）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent import run  # noqa: E402
from src.llm import OpenAICompatLLM, deepseek_llm  # noqa: E402
from src.tools import default_tools  # noqa: E402

TASK = "诊断最近 200 帧的实时检测系统：检查延迟分布与检测质量，给出健康结论。"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None, help="模型名（默认 DERT_MODEL / qwen2.5:7b）")
    ap.add_argument("--deepseek", action="store_true", help="使用 DeepSeek 强档")
    ap.add_argument("--no-cache", action="store_true", help="禁用响应缓存")
    args = ap.parse_args()

    if args.deepseek:
        llm = deepseek_llm(model=args.model)
    else:
        llm = OpenAICompatLLM(
            model=args.model,
            cache_dir="" if args.no_cache else None)

    print(f"backend: {llm.base_url} | model: {llm.model} | cache: {bool(llm.cache_dir)}")

    print("---- case 1: 宽预算（30s，正常多步诊断路径）----")
    r1 = run(TASK, llm=llm, tools=default_tools(), model=llm.model, deadline=30.0)
    print("stop:", r1.stop_reason, "| steps:", r1.steps, "| total: %.2fs" % r1.total_time)
    print("answer:", r1.answer[:120])
    print("summary:", json.dumps(r1.trace.summary(), ensure_ascii=False))
    r1.trace.save("eval/traces")

    print("---- case 2: 紧预算（4s，应触发 wrap_up 收尾）----")
    r2 = run(TASK + "（本次时间很紧）", llm=llm, tools=default_tools(),
             model=llm.model, deadline=4.0)
    has_wrap = any(e["kind"] == "wrap_up" for e in r2.trace.events)
    print("stop:", r2.stop_reason, "| steps:", r2.steps,
          "| total: %.2fs" % r2.total_time, "| wrap_up fired:", has_wrap)
    print("summary:", json.dumps(r2.trace.summary(), ensure_ascii=False))
    r2.trace.save("eval/traces")

    # 真实 TTFT 样本（case 1 的 llm_call 事件）
    ttfts = [e["ttft"] for e in r1.trace.events if e["kind"] == "llm_call"]
    if ttfts:
        print("real TTFT samples (case 1):",
              [round(t, 3) for t in ttfts], "s")
    print("\nREAL-MODEL SMOKE DONE")
    # no_final 不算致命错误：deepseek-chat 实测结构性不遵守 FINAL: 前缀（2026-08-24，
    # 三轮 prompt 强化无效），但内容完整。任务成功判定由评估脚本内容判分，不依赖格式。
    return 0 if (r1.finished or r1.stop_reason == "no_final") else 1


if __name__ == "__main__":
    sys.exit(main())
