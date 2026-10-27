"""预算-正确-按时-质量 四维评估 harness（2026-10-22 评估体系重建）。

对照设计（任务书 v2 + 2026-10-22 修订定案）：
    A fixed_strong  强模型（deepseek-chat），不感知预算；硬上限 180s 防发散
    B dert_v1       DeRT 调度器 v1.1（双档路由 + 自适应收尾），预算档 {3,8,30,60}s
    C fixed_fast    固定快档（qwen2.5:7b），不感知预算

预算档为何是 {3,8,30,60} 而不是任务书 v1 的 {15,30,60}（2026-10-22 定案）：
    调度器路由阈值 STRONG_REMAINING_MIN = 3.5s（src/scheduler.py）。若所有预算档
    都 ≥ 3.5s，B 首步必选强档 → B≈A 是构造使然，测不出任何调度行为；且任务书 v1
    的停止条件"B≈C 说明降级没生效"在松预算下会反向误报（松预算下 B≈A 才是预期，
    B≈C 才说明调度器坏了）。加入 3s 档后预算横跨阈值两侧：
      3s  在阈值之下 → B 应降级快档（该档 B≈C 是对的）
      8s  在临界区   → 路由决策最敏感的档位
      30/60s 在宽松区 → B 应上强档（该档 B≈A 是对的）
    停止条件按预算档分别解释，不再全局套用。

执行顺序（交替轮转）：每任务 6 次运行（A, B@3, B@8, B@30, B@60, C），
按任务序号轮转起始位置（task i 从第 i%6 个开始），避免"固定先 A 后 C"把
服务漂移混淆进策略效应（测量红线：顺序偏差）。

质量分（任务书红线：1–5 rubric + judge 与规则判分独立记录）：
    judge = deepseek-chat（temp=0），输入 = 任务问句 + 工具真实数据参考
    （golden_answer）+ agent 回答，输出 JSON {score, reason}。
    规则判分 correct 与 judge 分互不影响，各自落盘，分析时可分开用。

成功口径：success = correct 且 total_time <= budget（按时交付）。
    A/C 与预算无关 → 每任务各跑 1 次，分析时对每档预算分别判按时；
    B 行为随预算变化 → 每预算各跑 1 次。

输出：eval/results/budget_<run_id>/
    raw.jsonl     每行一条运行记录（含规则判分 + 质量分 + 过程指标，可离线重算）
    env.json      环境（模型、端点、调度器常数、rubric、任务清单、预热 TTFT）
    summary.json  聚合表（各格 correct/on_time/success/quality + 配对原始计数）

断点续跑：raw.jsonl 已有的 (task, strategy, budget) 记录自动跳过——长跑中途
崩溃/断网不报废整轮（测量红线：不许因为工程故障丢数据）。

用法（项目根目录，python -u 防块缓冲）：
    python -u eval/budget_eval.py --smoke            # 10 题冒烟（跨层抽样）
    python -u eval/budget_eval.py                    # 全量 100 题
    python -u eval/budget_eval.py --run-id demo      # 自定义 run_id
    python -u eval/budget_eval.py --no-judge         # 只跑不评质量分（省 API）
"""
from __future__ import annotations

import argparse
import json
import platform
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent import run  # noqa: E402
from src.llm import OpenAICompatLLM, deepseek_llm, bailian_llm  # noqa: E402
from src.scheduler import (DeadlineScheduler, DeadlineSchedulerV21,
                           DeadlineSchedulerV3,
                           DeadlineSchedulerV3B, DeadlineSchedulerV3C,
                           DeadlineSchedulerV3D, DeadlineSchedulerV3E,
                           DeadlineSchedulerV3Emp,
                           DualTierLLM, TripleTierLLM)  # noqa: E402
from src import scheduler as sched_mod  # noqa: E402
from src.tools import default_tools  # noqa: E402
from eval.tasks import TASKS, golden_answer  # noqa: E402

RESULTS_DIR = Path("eval/results")
TRACES_DIR = Path("eval/traces")

BUDGETS = [3.0, 8.0, 30.0, 60.0]   # 2026-10-22 定案（见模块 docstring）
HARD_CAP_A = 180.0                 # A 硬上限：防"无预算"发散（定案④）
MAX_STEPS = 8

RUBRIC = (
    "你是严格的结果评估员。根据任务要求、参考数据与 Agent 的回答，按 1-5 打分：\n"
    "5 = 关键数值全部正确、判断/结论正确，表述完整清晰\n"
    "4 = 关键数值与结论正确，但表述有瑕疵或含少量无关内容\n"
    "3 = 部分正确：拿到部分数值但结论错误，或漏掉任务要求的次要项\n"
    "2 = 与任务相关但大部分内容错误\n"
    "1 = 空答、跑题、或编造数据\n"
    "只输出一行 JSON，格式：{\"score\": <1-5>, \"reason\": \"<一句话依据>\"}"
)


def warmup(llm: OpenAICompatLLM, label: str) -> float:
    """预热并返回 TTFT（Ollama 冷启动首调用 TTFT ~65s，会污染首个任务，AGENTS.md §4）。"""
    print(f"[warmup] {label} ...", end=" ", flush=True)
    t0 = time.perf_counter()
    llm.chat([{"role": "user", "content": "回复 ok"}])
    ttft = time.perf_counter() - t0
    print(f"done (TTFT {ttft:.2f}s)")
    return ttft


def judge_score(judge: OpenAICompatLLM, task: dict, answer: str):
    """质量分：deepseek temp=0，返回 (score 1-5 | None, reason)。"""
    gold = golden_answer(task)
    if len(gold) > 1500:
        gold = gold[:1500] + "…(截断)"
    if len(answer) > 1500:
        answer = answer[:1500] + "…(截断)"
    prompt = (f"{RUBRIC}\n\n【任务】{task['prompt']}\n\n"
              f"【参考数据（工具真实输出，判分依据）】\n{gold}\n\n"
              f"【Agent 回答】\n{answer}")
    try:
        resp = judge.chat([{"role": "user", "content": prompt}], temperature=0.0)
        text = resp.content or ""
        m = re.search(r"\{[^{}]*\}", text, re.S)
        if not m:
            return None, f"judge_no_json: {text[:120]!r}"
        data = json.loads(m.group(0))
        score = int(data["score"])
        reason = str(data.get("reason", ""))[:200]
        if not 1 <= score <= 5:
            return None, f"judge_out_of_range: {score}"
        return score, reason
    except Exception as exc:  # judge 故障不拖垮数据采集：score 置空可离线补评
        return None, f"judge_error: {exc!r}"


def run_once(task: dict, llm, model: str | None, deadline: float | None,
             scheduler=None) -> dict:
    """跑一个任务一次，返回原始运行数据（不含判分与质量分）。"""
    try:
        r = run(task["prompt"], llm=llm, tools=default_tools(),
                model=model, deadline=deadline, max_steps=MAX_STEPS,
                scheduler=scheduler)
    except Exception as exc:  # 单任务失败不拖垮整轮
        return {"answer": "", "total_time": None, "stop_reason": "error",
                "steps": 0, "wrap_up": False, "ttfts": [], "models": [],
                "switch_count": 0, "tokens_in": 0, "tokens_out": 0,
                "error": repr(exc)}
    evs = r.trace.events
    models = [e["model"] for e in evs if e["kind"] == "llm_call"]
    ttfts = [e["ttft"] for e in evs if e["kind"] == "llm_call"]
    r.trace.save(str(TRACES_DIR))
    return {
        "answer": (r.answer or "")[:3000],
        "total_time": r.total_time, "stop_reason": r.stop_reason,
        "steps": r.steps,
        "wrap_up": any(e["kind"] == "wrap_up" for e in evs),
        "ttfts": [round(t, 3) for t in ttfts],
        "models": models,
        # 换档次数：相邻两次 LLM 调用模型不同的次数（调度行为的直接观测量）
        "switch_count": sum(1 for a, b in zip(models, models[1:]) if a != b),
        "tokens_in": sum(e.get("tokens_in", 0) for e in evs
                         if e["kind"] == "llm_call"),
        "tokens_out": sum(e.get("tokens_out", 0) for e in evs
                          if e["kind"] == "llm_call"),
        "error": None,
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="10 题冒烟（跨层抽样）")
    ap.add_argument("--tasks", default="", help="任务子集（逗号分隔 id）")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--budgets", default="", help="覆盖默认预算档（逗号分隔秒）")
    ap.add_argument("--no-judge", action="store_true", help="跳过质量分（省 API）")
    ap.add_argument("--strong-provider", default="deepseek",
                    choices=["deepseek", "bailian"],
                    help="强档后端：deepseek 官方 API（main100 口径）或 "
                         "阿里云百炼 deepseek-v4.1-flash（2026-10-22 起，官方余额耗尽）")
    ap.add_argument("--with-v2", action="store_true",
                    help="在计划中加入 dert_v2（v2.1 自适应路由 + v1.1 收尾）；"
                         "配合同 run-id 续跑时只为缺失的 v2 格补采集")
    ap.add_argument("--with-v3", action="store_true",
                    help="在计划中加入 dert_v3（难度感知路由：问句含数值对比"
                         "措辞的任务在剩余≥8s 时走强档，其余永远快档）")
    ap.add_argument("--with-v3b", action="store_true",
                    help="在计划中加入 dert_v3b（纯难度路由：难任务永远强档、"
                         "易任务永远快档，不看时间——解耦后的质量口径实验）")
    ap.add_argument("--with-v3c", action="store_true",
                    help="在计划中加入 dert_v3c（难度路由 + 自适应时间门："
                         "强档任务耗时在线 P60+0.5 替代手拍 8s，冷启动先验=8.0）")
    ap.add_argument("--with-v3d", action="store_true",
                    help="在计划中加入 dert_v3d（v3c 修复版：删失过滤+离群截断"
                         "+变速率门+先验地板 8.5，门只向上自适应）")
    ap.add_argument("--with-v3e", action="store_true",
                    help="在计划中加入 dert_v3e（三档阶梯路由：hard 任务按剩余"
                         "预算 strong≥8.5 / mid≥6.6 / fast 分层，中档="
                         "deepseek-v4-flash-0731 标定卡常数门 6.6）")
    ap.add_argument("--with-v3emp", action="store_true",
                    help="在计划中加入 dert_v3emp（v3d 机制 + 经验难度信号："
                         "影子流量失败史替代中文词表——难度评分通用化的在线确认步）")
    ap.add_argument("--difficulty-history",
                    default="eval/results/difficulty_history_main100.json",
                    help="经验难度史文件（build_difficulty_history.py 产物）")
    args = ap.parse_args()

    budgets = ([float(b) for b in args.budgets.split(",")] if args.budgets
               else BUDGETS)

    if args.smoke:
        # 跨层抽样 10 题：新旧任务都要覆盖到（新题的判分/可达性已由 self_test 保证，
        # 冒烟重点验证 harness 链路 + 新题在真实模型下的行为）
        smoke_ids = ["T1-1", "T1-21", "T1-29", "T1-31",
                     "T2-1", "T2-21", "T2-29",
                     "T3-1", "T3-11", "T3-21"]
        keep = set(smoke_ids)
        tasks = [t for t in TASKS if t["id"] in keep]
    elif args.tasks:
        keep = {t.strip() for t in args.tasks.split(",")}
        tasks = [t for t in TASKS if t["id"] in keep]
    else:
        tasks = TASKS

    run_id = args.run_id or f"{datetime.now():%Y%m%d_%H%M}"
    out_dir = RESULTS_DIR / f"budget_{run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / "raw.jsonl"
    env_path = out_dir / "env.json"

    # ---- 断点续跑：读已有记录的 key ----
    done_keys: set[tuple] = set()
    if raw_path.exists():
        for line in raw_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                done_keys.add((d["task"], d["strategy"], d["budget"]))
        print(f"[resume] 已有 {len(done_keys)} 条记录，跳过对应运行")

    # ---- 客户端（全实时，缓存禁用）----
    fast = OpenAICompatLLM(cache_dir="")
    strong = (deepseek_llm(cache_dir="") if args.strong_provider == "deepseek"
              else bailian_llm(cache_dir=""))
    judge = strong  # judge 与强档同后端同模型（质量分只做独立记录，不参与规则判分）
    dual = DualTierLLM(fast, strong, DeadlineScheduler(fast.model, strong.model))
    # v2.1：独立调度器实例（滑窗状态不与 v1 共享），共享同一对客户端
    dual2 = (DualTierLLM(fast, strong,
                         DeadlineSchedulerV21(fast.model, strong.model))
             if args.with_v2 else None)
    dual3 = (DualTierLLM(fast, strong,
                         DeadlineSchedulerV3(fast.model, strong.model))
             if args.with_v3 else None)
    dual3b = (DualTierLLM(fast, strong,
                          DeadlineSchedulerV3B(fast.model, strong.model))
              if args.with_v3b else None)
    dual3c = (DualTierLLM(fast, strong,
                          DeadlineSchedulerV3C(fast.model, strong.model))
              if args.with_v3c else None)
    dual3d = (DualTierLLM(fast, strong,
                          DeadlineSchedulerV3D(fast.model, strong.model))
              if args.with_v3d else None)
    mid = (bailian_llm(model="deepseek-v4-flash-0731", cache_dir="")
           if args.with_v3e else None)
    triple3e = (TripleTierLLM(fast, mid, strong,
                              DeadlineSchedulerV3E(fast.model, strong.model,
                                                   mid.model))
                if args.with_v3e else None)
    dual3emp = None
    if args.with_v3emp:
        hist = json.loads(Path(args.difficulty_history)
                          .read_text(encoding="utf-8"))
        print(f"[v3emp] 难度史：{hist['n_wrong']} wrong / {hist['n_fast_runs']} "
              f"runs（源 {hist['source_run']}，fast={hist['fast_model']}）")
        dual3emp = DualTierLLM(fast, strong,
                               DeadlineSchedulerV3Emp(fast.model, strong.model,
                                                      hist["fast_wrong"]))

    ttft_fast = warmup(fast, "fast")
    ttft_strong = warmup(strong, "strong")
    ttft_mid = warmup(mid, "mid") if args.with_v3e else None

    env = {
        "run_id": run_id,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": None,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "fast": {"model": fast.model, "base_url": fast.base_url},
        "strong": {"model": strong.model, "base_url": strong.base_url,
                   "provider": args.strong_provider},
        "mid": ({"model": mid.model, "base_url": mid.base_url,
                 "gate": "6.6s（probe_mid P60 5.8+0.8 标定卡常数）"}
                if args.with_v3e else None),
        "judge": {"model": judge.model, "base_url": judge.base_url,
                  "temperature": 0.0, "rubric": RUBRIC},
        "budgets": budgets,
        "hard_cap_A": HARD_CAP_A,
        "max_steps": MAX_STEPS,
        "scheduler_v1.1": {
            "STRONG_REMAINING_MIN": sched_mod.STRONG_REMAINING_MIN,
            "MIN_STEP_BUDGET_V1": sched_mod.MIN_STEP_BUDGET_V1,
            "WRAP_UP_MAX_TOKENS": sched_mod.WRAP_UP_MAX_TOKENS,
            "WRAP_UP_TOK_PER_SEC": sched_mod.WRAP_UP_TOK_PER_SEC,
            "WRAP_UP_MIN_TOKENS": sched_mod.WRAP_UP_MIN_TOKENS},
        "warmup_ttft": {"fast": round(ttft_fast, 3),
                        "strong": round(ttft_strong, 3)},
        "design": {
            "A": "fixed_strong：强模型，不感知预算，deadline=180s 硬上限",
            "B": "dert_v1：调度器 v1.1，每预算档各跑一次",
            "C": "fixed_fast：快模型，不感知预算",
            "interleaving": "每任务 6 次运行按任务序号轮转起始位置"},
        "tasks": [t["id"] for t in tasks],
        "n_tasks": len(tasks),
        "n_planned_runs": len(tasks) * (2 + len(budgets) * (1 + int(args.with_v2) + int(args.with_v3) + int(args.with_v3b) + int(args.with_v3c) + int(args.with_v3d) + int(args.with_v3e) + int(args.with_v3emp))),
        "with_v2": args.with_v2, "with_v3": args.with_v3, "with_v3b": args.with_v3b,
        "with_v3c": args.with_v3c, "with_v3d": args.with_v3d, "with_v3e": args.with_v3e,
        "with_v3emp": args.with_v3emp,
        "difficulty_history": (args.difficulty_history if args.with_v3emp else None),
    }
    env_path.write_text(json.dumps(env, ensure_ascii=False, indent=1),
                        encoding="utf-8")

    print(f"fast={fast.model} | strong={strong.model} | judge={judge.model}")
    print(f"budgets={budgets} | hard_cap_A={HARD_CAP_A:g}s | tasks={len(tasks)} "
          f"| planned runs={env['n_planned_runs']} | judge={'on' if not args.no_judge else 'off'}")

    t_start = time.time()
    n_new = 0

    # 每任务 N 次运行的轮转序列：A, B@每预算, [v2@每预算], C
    def plan_for(i: int) -> list[tuple]:
        base = [("fixed_strong", None)] + \
               [("dert_v1", b) for b in budgets] + \
               ([("dert_v2", b) for b in budgets] if args.with_v2 else []) + \
               ([("dert_v3", b) for b in budgets] if args.with_v3 else []) + \
               ([("dert_v3b", b) for b in budgets] if args.with_v3b else []) + \
               ([("dert_v3c", b) for b in budgets] if args.with_v3c else []) + \
               ([("dert_v3d", b) for b in budgets] if args.with_v3d else []) + \
               ([("dert_v3e", b) for b in budgets] if args.with_v3e else []) + \
               ([("dert_v3emp", b) for b in budgets] if args.with_v3emp else []) + \
               [("fixed_fast", None)]
        k = i % len(base)
        return base[k:] + base[:k]

    raw_f = raw_path.open("a", encoding="utf-8")
    try:
        for i, task in enumerate(tasks):
            tid = task["type"] + "/" + task["id"]
            for strat, b in plan_for(i):
                if (tid, strat, b) in done_keys:
                    continue
                t0 = time.perf_counter()
                if strat == "fixed_strong":
                    rec = run_once(task, strong, strong.model, HARD_CAP_A)
                elif strat == "fixed_fast":
                    rec = run_once(task, fast, fast.model, None)
                elif strat == "dert_v2":
                    rec = run_once(task, dual2, None, b, scheduler=dual2.scheduler)
                elif strat == "dert_v3":
                    dual3.scheduler.set_prompt(task["prompt"])
                    rec = run_once(task, dual3, None, b, scheduler=dual3.scheduler)
                elif strat == "dert_v3b":
                    dual3b.scheduler.set_prompt(task["prompt"])
                    rec = run_once(task, dual3b, None, b, scheduler=dual3b.scheduler)
                elif strat == "dert_v3c":
                    dual3c.scheduler.set_prompt(task["prompt"])
                    rec = run_once(task, dual3c, None, b, scheduler=dual3c.scheduler)
                elif strat == "dert_v3d":
                    dual3d.scheduler.set_prompt(task["prompt"])
                    rec = run_once(task, dual3d, None, b, scheduler=dual3d.scheduler)
                elif strat == "dert_v3e":
                    triple3e.scheduler.set_prompt(task["prompt"])
                    rec = run_once(task, triple3e, None, b,
                                   scheduler=triple3e.scheduler)
                elif strat == "dert_v3emp":
                    dual3emp.scheduler.set_task_id(tid)
                    dual3emp.scheduler.set_prompt(task["prompt"])
                    rec = run_once(task, dual3emp, None, b,
                                   scheduler=dual3emp.scheduler)
                else:  # dert_v1
                    rec = run_once(task, dual, None, b, scheduler=dual.scheduler)

                correct, why = task["check"](rec["answer"] or "")
                on_time = (rec["total_time"] is not None and b is not None
                           and rec["total_time"] <= b)
                score = reason = None
                if not args.no_judge and rec["answer"] and not rec["error"]:
                    score, reason = judge_score(judge, task, rec["answer"])

                row = {
                    "run_id": run_id,
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "task": tid, "task_type": task["type"],
                    "strategy": strat, "budget": b,
                    "hard_cap": HARD_CAP_A if strat == "fixed_strong" else None,
                    **rec,
                    "correct": correct, "check_why": why,
                    "on_time": on_time if b is not None else None,
                    "quality_score": score, "quality_reason": reason,
                }
                raw_f.write(json.dumps(row, ensure_ascii=False) + "\n")
                raw_f.flush()
                n_new += 1
                wall = time.perf_counter() - t0
                print(f"  [{strat:13s}@{b if b is not None else '-':>4}] {tid:8s} "
                      f"stop={rec['stop_reason']:16s} "
                      f"t={rec['total_time'] and round(rec['total_time'], 2)}s "
                      f"steps={rec['steps']} corr={int(correct)} "
                      f"q={score} wall={wall:.1f}s")
    finally:
        raw_f.close()

    # ---- 汇总（可离线由 analyze_budget.py 重算，这里给即时快照）----
    records = [json.loads(l) for l in
               raw_path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def cell(strat: str, b: float | None) -> list[dict]:
        if strat in ("dert_v1", "dert_v2", "dert_v3", "dert_v3b", "dert_v3c", "dert_v3d", "dert_v3e"):
            return [r for r in records if r["strategy"] == strat
                    and r["budget"] == b]
        # A/C 与预算无关：唯一一次运行对每档预算分别判按时
        return [r for r in records if r["strategy"] == strat]

    def metrics(strat: str, b: float | None) -> dict:
        rs = cell(strat, b)
        if not rs:
            return {"n": 0}
        if strat in ("dert_v1", "dert_v2", "dert_v3", "dert_v3b", "dert_v3c", "dert_v3d", "dert_v3e"):
            ot = [bool(r["on_time"]) for r in rs]
        else:
            ot = [r["total_time"] is not None and r["total_time"] <= b
                  for r in rs]
        scores = [r["quality_score"] for r in rs if r["quality_score"]]
        times = sorted(r["total_time"] for r in rs if r["total_time"] is not None)
        return {
            "n": len(rs),
            "correct_pct": round(sum(r["correct"] for r in rs) / len(rs) * 100, 1),
            "on_time_pct": round(sum(ot) / len(rs) * 100, 1) if b is not None else None,
            "success_pct": round(sum(c and o for c, o in
                                     zip([r["correct"] for r in rs], ot))
                                 / len(rs) * 100, 1) if b is not None else None,
            "quality_mean": round(statistics.mean(scores), 2) if scores else None,
            "quality_ge4_pct": round(sum(s >= 4 for s in scores) / len(scores) * 100, 1)
                               if scores else None,
            "p50_time": round(times[len(times) // 2], 2) if times else None,
            "p95_time": round(times[min(len(times) - 1, int(0.95 * (len(times) - 1)))], 2)
                        if times else None,
        }

    strats = ("fixed_strong", "dert_v1", "fixed_fast") \
        + (("dert_v2",) if args.with_v2 else ()) \
        + (("dert_v3",) if args.with_v3 else ()) \
        + (("dert_v3b",) if args.with_v3b else ()) \
        + (("dert_v3c",) if args.with_v3c else ()) \
        + (("dert_v3d",) if args.with_v3d else ()) \
        + (("dert_v3e",) if args.with_v3e else ()) \
        + (("dert_v3emp",) if args.with_v3emp else ())
    summary = {"budgets": budgets, "cells": {}, "paired_counts": {}}
    for strat in strats:
        for b in budgets:
            summary["cells"][f"{strat}@{b:g}s"] = metrics(strat, b)

    # 配对原始计数（success 口径，供 analyze_budget.py 算 McNemar / sign test）。
    # x/y 命名：x=first, y=second；dert_* 取其预算档运行，A/C 取唯一一次运行。
    by_key = {(r["task"], r["strategy"],
               r["budget"] if r["strategy"] in ("dert_v1", "dert_v2", "dert_v3", "dert_v3b", "dert_v3c", "dert_v3d", "dert_v3e", "dert_v3emp") else None): r
              for r in records}
    for b in budgets:
        pairs = [("strong_vs_dert", "fixed_strong", "dert_v1"),
                 ("fast_vs_dert", "fixed_fast", "dert_v1"),
                 ("strong_vs_fast", "fixed_strong", "fixed_fast")]
        if args.with_v2:
            pairs += [("v2_vs_strong", "fixed_strong", "dert_v2"),
                      ("v2_vs_fast", "fixed_fast", "dert_v2"),
                      ("v2_vs_v1", "dert_v1", "dert_v2")]
        if args.with_v3:
            pairs += [("v3_vs_strong", "fixed_strong", "dert_v3"),
                      ("v3_vs_fast", "fixed_fast", "dert_v3"),
                      ("v3_vs_v1", "dert_v1", "dert_v3")]
        if args.with_v3b:
            pairs += [("v3b_vs_strong", "fixed_strong", "dert_v3b"),
                      ("v3b_vs_fast", "fixed_fast", "dert_v3b"),
                      ("v3b_vs_v1", "dert_v1", "dert_v3b")]
        if args.with_v3c:
            pairs += [("v3c_vs_strong", "fixed_strong", "dert_v3c"),
                      ("v3c_vs_fast", "fixed_fast", "dert_v3c"),
                      ("v3c_vs_v1", "dert_v1", "dert_v3c"),
                      ("v3c_vs_v3", "dert_v3", "dert_v3c")]
        if args.with_v3d:
            pairs += [("v3d_vs_strong", "fixed_strong", "dert_v3d"),
                      ("v3d_vs_fast", "fixed_fast", "dert_v3d"),
                      ("v3d_vs_v1", "dert_v1", "dert_v3d"),
                      ("v3d_vs_v3", "dert_v3", "dert_v3d")]
        if args.with_v3e:
            pairs += [("v3e_vs_strong", "fixed_strong", "dert_v3e"),
                      ("v3e_vs_fast", "fixed_fast", "dert_v3e"),
                      ("v3e_vs_v1", "dert_v1", "dert_v3e"),
                      ("v3e_vs_v3", "dert_v3", "dert_v3e"),
                      ("v3e_vs_v3d", "dert_v3d", "dert_v3e")]
        if args.with_v3emp:
            pairs += [("v3emp_vs_strong", "fixed_strong", "dert_v3emp"),
                      ("v3emp_vs_fast", "fixed_fast", "dert_v3emp"),
                      ("v3emp_vs_v1", "dert_v1", "dert_v3emp"),
                      ("v3emp_vs_v3d", "dert_v3d", "dert_v3emp")]
        for name, x, y in pairs:
            key = f"{name}@{b:g}s"
            cnt = {"n": 0, "x_only": 0, "y_only": 0, "both": 0, "neither": 0}
            for t in tasks:
                tid = t["type"] + "/" + t["id"]
                rx = by_key.get((tid, x, b if x in ("dert_v1", "dert_v2", "dert_v3", "dert_v3b", "dert_v3c", "dert_v3d", "dert_v3e", "dert_v3emp") else None))
                ry = by_key.get((tid, y, b if y in ("dert_v1", "dert_v2", "dert_v3", "dert_v3b", "dert_v3c", "dert_v3d", "dert_v3e", "dert_v3emp") else None))
                if not rx or not ry:
                    continue
                sx = rx["correct"] and rx["total_time"] is not None \
                     and rx["total_time"] <= b
                sy = ry["correct"] and ry["on_time"]
                cnt["n"] += 1
                if sx and sy:
                    cnt["both"] += 1
                elif sx:
                    cnt["x_only"] += 1
                elif sy:
                    cnt["y_only"] += 1
                else:
                    cnt["neither"] += 1
            summary["paired_counts"][key] = cnt

    env["finished_at"] = datetime.now().isoformat(timespec="seconds")
    env["n_actual_runs"] = len(records)
    env["n_new_runs"] = n_new
    env_path.write_text(json.dumps(env, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n==== 快照（correct% / on_time% / success% / quality_mean）====")
    header = "strategy      " + "".join(f"{b:>16g}s" for b in budgets)
    print(header)
    for strat in strats:
        row = f"{strat:13s}"
        for b in budgets:
            m = summary["cells"][f"{strat}@{b:g}s"]
            row += (f"  {m['correct_pct']:>3.0f}/{m['on_time_pct']:>3.0f}/"
                    f"{m['success_pct']:>3.0f}/q{m['quality_mean'] if m['quality_mean'] else '-'}")
        print(row)
    print(f"\nrecords={len(records)} (new {n_new}) | total {time.time() - t_start:.0f}s")
    print(f"results -> {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
