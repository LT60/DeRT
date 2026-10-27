# -*- coding: utf-8 -*-
"""实验 A：验证层 judge 体检——快档模型能否廉价地发现「答错了」。

回答的问题：没有判分器的自由任务，能否用快档模型当判卷老师，
从「问题+回答」判断答案是否完成了任务？这是级联（fast-first 级联）
唯一缺失的前提：发现错了，才谈得上升级补救。

设计：
- 样本 = main100 C 策略 100 条（问题+回答+规则判分真值）+ HB 20 条
  （同样有真值）。judge 只看问题+回答（不看工具调用记录、不看期望）——
  生产环境里结构信号（没调工具等）是零成本的额外层，这里测的是纯文本
  judge 的下界。
- judge = 本地 Ollama qwen2.5:7b（与快档同模型，temp=0），严格 JSON 输出。
- 指标（以「答错」为正类）：召回 = 真错被抓出比例；误报率 = 真对被错杀比例。
- 通过线（预注册）：召回 >= 0.60 且误报率 <= 0.25。
  达标 → 验证层用快档 judge 建；不达标 → 换中档 judge 复测或放弃级联。

输出：eval/results/judge_audit/{samples.jsonl,summary.json}，支持断点续跑。
用法：python -m eval.judge_audit [--model qwen2.5:7b] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openai import OpenAI  # noqa: E402

from eval.tasks import TASKS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "eval" / "results" / "judge_audit"
MAIN_RAW = ROOT / "eval" / "results" / "budget_main100" / "raw.jsonl"
HB_RAW = ROOT / "eval" / "results" / "hb_ext1" / "raw.jsonl"

JUDGE_SYSTEM_V1 = ("你是严格的答案质检员。你拿不到标准答案，只能凭常识、逻辑"
                   "和题目要求判断回答是否完成了任务。宁可存疑，也不要放过明显"
                   "未完成或编造的回答。")

JUDGE_SYSTEM_V2 = ("你是答案质检员。根据题目要求判断回答是否完成了任务。"
                   "不要因为措辞风格、详细程度或格式不合你的偏好而判未完成；"
                   "只有当你确信回答未完成、答非所问或内容错误时才判未完成；"
                   "拿不准时倾向判完成。")

PROMPTS = {"v1": JUDGE_SYSTEM_V1, "v2": JUDGE_SYSTEM_V2}


def _build_samples() -> list[dict]:
    """CV 100 条 + HB 20 条：{domain,id,question,answer,truth_correct}。"""
    samples = []
    prompts = {t["id"]: t["prompt"] for t in TASKS}
    for line in MAIN_RAW.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r["strategy"] != "fixed_fast":
            continue
        tid = r["task"].split("/")[-1]
        samples.append({"domain": "cv", "id": r["task"],
                        "question": prompts[tid], "answer": r.get("answer") or "",
                        "truth_correct": bool(r["correct"])})
    from eval.hammerbench_adapter import load_conversations, pick_episodes
    eps = {e["id"]: e for e in pick_episodes(load_conversations(), 20)}
    for line in HB_RAW.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r["strategy"] != "C":
            continue
        ep = eps[r["episode"]]
        samples.append({"domain": "hb", "id": r["episode"],
                        "question": ep["turns"][0]["user"],
                        "answer": r.get("answer") or "",
                        "truth_correct": bool(r["success"])})
    return samples


def _judge(client: OpenAI, model: str, question: str, answer: str,
           prompt_version: str = "v1") -> tuple[str, float, str]:
    """返回 (verdict, 延迟s, reason)。verdict ∈ {pass, fail, parse_error}。"""
    user = (f"【任务】\n{question}\n\n【助手回答】\n{answer or '（空回答）'}\n\n"
            "请判断这份回答是否完成了任务。注意：缺少必要的执行结果、答非所问、"
            "或编造数据，都算未完成。\n"
            '只输出一行 JSON：{"verdict":"pass","reason":"一句话"} 或 '
            '{"verdict":"fail","reason":"一句话"}，不要输出任何其他内容。')
    t0 = time.perf_counter()
    resp = client.chat.completions.create(
        model=model, temperature=0, max_tokens=200,
        messages=[{"role": "system", "content": PROMPTS[prompt_version]},
                  {"role": "user", "content": user}])
    dt = time.perf_counter() - t0
    text = (resp.choices[0].message.content or "").strip()
    m = re.search(r"\{[^{}]*\}", text, re.S)
    if not m:
        return "parse_error", dt, text[:120]
    try:
        obj = json.loads(m.group(0))
        v = str(obj.get("verdict", "")).lower()
        return ("pass" if v == "pass" else "fail"), dt, str(obj.get("reason", ""))[:160]
    except json.JSONDecodeError:
        return "parse_error", dt, text[:120]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen2.5:7b")
    ap.add_argument("--prompt-version", default="v1", choices=["v1", "v2"])
    ap.add_argument("--limit", type=int, default=0, help="调试用：只跑前 N 条")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"samples_{args.prompt_version}.jsonl"
    done: dict[str, dict] = {}
    if out_path.exists():  # 断点续跑
        for line in out_path.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            done[r["id"]] = r

    samples = _build_samples()
    if args.limit:
        samples = samples[:args.limit]
    todo = [s for s in samples if s["id"] not in done]
    print(f"样本 {len(samples)} 条（CV {sum(1 for s in samples if s['domain']=='cv')} / "
          f"HB {sum(1 for s in samples if s['domain']=='hb')}），"
          f"prompt={args.prompt_version}，已跑 {len(done)}，待跑 {len(todo)}")

    client = OpenAI(base_url="http://127.0.0.1:11434/v1", api_key="ollama")
    with out_path.open("a", encoding="utf-8") as f:
        for i, s in enumerate(todo):
            verdict, dt, reason = _judge(client, args.model,
                                         s["question"], s["answer"],
                                         args.prompt_version)
            rec = {**s, "verdict": verdict, "judge_s": round(dt, 3),
                   "reason": reason, "model": args.model,
                   "prompt_version": args.prompt_version}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            done[s["id"]] = rec
            if (i + 1) % 20 == 0:
                print(f"  进度 {i + 1}/{len(todo)}（最近 judge {dt:.1f}s）")

    # ---- 汇总（以「答错」为正类）----
    recs = [done[s["id"]] for s in samples if s["id"] in done]
    summary: dict = {"model": args.model, "prompt_version": args.prompt_version,
                     "n": len(recs)}
    for dom in ("cv", "hb", "all"):
        rs = [r for r in recs if dom == "all" or r["domain"] == dom]
        if not rs:
            continue
        tp = sum(1 for r in rs if not r["truth_correct"] and r["verdict"] == "fail")
        fn = sum(1 for r in rs if not r["truth_correct"] and r["verdict"] != "fail")
        fp = sum(1 for r in rs if r["truth_correct"] and r["verdict"] == "fail")
        tn = sum(1 for r in rs if r["truth_correct"] and r["verdict"] != "fail")
        pe = sum(1 for r in rs if r["verdict"] == "parse_error")
        recall = tp / (tp + fn) if tp + fn else None
        fpr = fp / (fp + tn) if fp + tn else None
        summary[dom] = {
            "n": len(rs), "真错": tp + fn, "真对": fp + tn,
            "召回(抓错)": round(recall, 3) if recall is not None else None,
            "误报率(错杀)": round(fpr, 3) if fpr is not None else None,
            "parse_error": pe,
            "judge延迟P50": round(statistics.median(r["judge_s"] for r in rs), 2),
        }
    passed = (summary.get("cv", {}).get("召回(抓错)") or 0) >= 0.60 and \
             (summary.get("cv", {}).get("误报率(错杀)") or 1) <= 0.25
    summary["预注册通过线"] = "召回>=0.60 且 误报率<=0.25（CV 域）"
    summary["判定"] = "PASS" if passed else "FAIL"
    (OUT_DIR / f"summary_{args.prompt_version}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
