"""演示数据采集：跑一小批真实运行，把回放数据注入 docs/demo/index.html。

用途
    给求职展示用的「调度回放页」提供真实数据——页面上的每根条都是真的 LLM 调用，
    不是示意图。选定任务 T3-5（两个互相独立的工具 → 会产生并行批），
    跑「DeRT 四档预算 + 两条固定基线」共 6 次，覆盖三种结局：
    紧预算收尾截断 / 宽预算完整按时 / 基线不感知预算。

成本
    仅 4 次 deepseek 运行（dert 4s/8s/30s + 固定强档），每次 2–3 次调用；
    dert 2s 档与固定快档走本地 qwen，零成本。成本上限约 ¥0.5。

用法（项目根目录）
    python -u eval/make_demo.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent import run  # noqa: E402
from src.llm import OpenAICompatLLM, deepseek_llm  # noqa: E402
from src.scheduler import DeadlineScheduler, DualTierLLM  # noqa: E402
from src.tools import default_tools  # noqa: E402
from eval.tasks import TASKS, _detection_stats  # noqa: E402

DEMO_TASK_ID = "T3-5"
BUDGETS = [2.0, 4.0, 8.0, 30.0]
HTML_PATH = Path("docs/demo/index.html")


def warmup(llm: OpenAICompatLLM, label: str) -> None:
    """预热：Ollama 冷启动首调用 TTFT ~65s，不预热会污染首个任务的数据（AGENTS.md §4）。"""
    print(f"[warmup] {label} ...", end=" ", flush=True)
    t0 = time.perf_counter()
    llm.chat([{"role": "user", "content": "回复 ok"}])
    print(f"done ({time.perf_counter() - t0:.1f}s)")


def entry(label: str, group: str, strategy: str, budget, result, task) -> dict:
    """把一次运行整理成页面所需的数据结构。"""
    events = result.trace.events
    correct, why = task["check"](result.answer or "")
    on_time = budget is None or result.total_time <= budget
    return {
        "label": label,
        "group": group,
        "strategy": strategy,
        "budget": budget,
        "total": round(result.total_time, 3),
        "stop_reason": result.stop_reason,
        "steps": result.steps,
        "answer": result.answer or "",
        "expected": why,
        "correct": bool(correct),
        "on_time": bool(on_time),
        "wrap_up": any(e["kind"] == "wrap_up" for e in events),
        "compress": any(e["kind"] == "compress" for e in events),
        "events": events,
    }


def inject(payload: dict) -> None:
    """把数据写进页面的 demo-data 块（幂等：已有数据会被覆盖）。"""
    s = HTML_PATH.read_text(encoding="utf-8")
    anchor = s.index('id="demo-data"')
    a = s.index(">", anchor) + 1
    b = s.index("</script>", a)
    HTML_PATH.write_text(s[:a] + json.dumps(payload, ensure_ascii=False) + s[b:],
                         encoding="utf-8")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    task = next(t for t in TASKS if t["id"] == DEMO_TASK_ID)
    print(f"演示任务 {DEMO_TASK_ID}: {task['prompt']}")

    fast = OpenAICompatLLM(cache_dir="")          # 本地 qwen，零成本
    strong = deepseek_llm(cache_dir="")           # 云端强档，全实时
    dual = DualTierLLM(fast, strong, DeadlineScheduler(fast.model, strong.model))
    warmup(fast, "fast")
    warmup(strong, "strong")

    runs: list[dict] = []
    t0 = time.time()

    # DeRT v1.1：行为随预算改变 → 每档各跑一次
    for b in BUDGETS:
        r = run(task["prompt"], llm=dual, tools=default_tools(), model=None,
                deadline=b, max_steps=8, scheduler=dual.scheduler)
        e = entry(f"DeRT · {b:g}s 预算", "dert", "DeRT v1.1 · 双档路由", b, r, task)
        runs.append(e)
        print(f"  [dert @{b:g}s] stop={r.stop_reason:<15} t={r.total_time:.2f}s "
              f"steps={r.steps} wrap_up={e['wrap_up']} 判分={e['correct']} "
              f"按时={e['on_time']}")

    # 固定基线：不感知预算（deadline=None），行为与预算无关 → 各跑一次
    for label, llm, strat in (("固定快档 · qwen2.5:7b", fast, "固定快档 · 不感知预算"),
                              ("固定强档 · deepseek-chat", strong, "固定强档 · 不感知预算")):
        r = run(task["prompt"], llm=llm, tools=default_tools(), model=llm.model,
                deadline=None, max_steps=8)
        e = entry(label, "baseline", strat, None, r, task)
        runs.append(e)
        print(f"  [{label}] stop={r.stop_reason:<15} t={r.total_time:.2f}s "
              f"steps={r.steps} 判分={e['correct']}")

    inject({
        "task": task["prompt"],
        "expected": f"需含 reports/latest.png 与检出率 "
                    f"{_detection_stats(200)['detection_rate']:.1%}（规则判分，不看格式前缀）",
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "runs": runs,
    })
    print(f"\n页面数据已注入 {HTML_PATH}（{len(runs)} 次运行，耗时 {time.time() - t0:.0f}s）")
    print("直接用浏览器打开该文件即可回放。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
