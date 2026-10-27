# -*- coding: utf-8 -*-
"""DeRT 并发压测：N 个 fake 会话同时打服务，看吞吐/延迟/档位分布。

用法（项目根目录，服务已跑在 8630）：
    python scripts/load_test.py --concurrency 8 --total 32
    python scripts/load_test.py --concurrency 4 --total 16 --port 8640 --deadline 20

只支持 fake provider（离线、无外部依赖、不花钱）。
输出：成功/失败数、wall 时间、端到端延迟 p50/p95、tier_usage 分布。
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

PROMPTS = [
    "总结一下这段说明文字的要点",
    "帮我检索关于时间预算调度相关的资料再作答",
    "计算 17*23-45/9 的结果并解释步骤",
    "整理本周的工作安排并给出优先级建议",
]


def _post(base: str, path: str, body: dict, timeout: float = 15) -> dict:
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _get(base: str, path: str, timeout: float = 15) -> dict:
    with urllib.request.urlopen(base + path, timeout=timeout) as resp:
        return json.loads(resp.read())


def one_run(base: str, idx: int, deadline: float, poll_timeout: float) -> dict:
    prompt = PROMPTS[idx % len(PROMPTS)]
    t0 = time.time()
    run = _post(base, "/runs", {"prompt": prompt, "provider": "fake",
                                "deadline": deadline})
    rid = run["run_id"]
    deadline_at = time.time() + poll_timeout
    while time.time() < deadline_at:
        snap = _get(base, f"/runs/{rid}")
        if snap.get("status") in ("done", "error"):
            return {"rid": rid, "status": snap["status"],
                    "latency": time.time() - t0,
                    "tiers": tuple(sorted((snap.get("tier_usage") or {}).items())),
                    "total_time": snap.get("total_time")}
        time.sleep(0.15)
    return {"rid": rid, "status": "timeout", "latency": time.time() - t0,
            "tiers": (), "total_time": None}


def main() -> None:
    ap = argparse.ArgumentParser(description="DeRT 并发压测（fake provider）")
    ap.add_argument("--port", type=int, default=8630)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--total", type=int, default=32)
    ap.add_argument("--deadline", type=float, default=15)
    ap.add_argument("--poll-timeout", type=float, default=60)
    args = ap.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    # 就绪探测
    t0 = time.time()
    while time.time() - t0 < 10:
        try:
            _get(base, "/tasks", timeout=2)
            break
        except Exception:
            time.sleep(0.3)
    else:
        sys.exit(f"服务 {base} 不可达（先 python -m uvicorn service.app:app --port {args.port}）")

    print(f"压测开始：{args.total} 个会话 / 并发 {args.concurrency} / "
          f"deadline {args.deadline}s @ {base}")
    wall0 = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(one_run, base, i, args.deadline,
                               args.poll_timeout)
                   for i in range(args.total)]
        results = [f.result() for f in futures]
    wall = time.time() - wall0

    ok = [r for r in results if r["status"] == "done"]
    bad = [r for r in results if r["status"] != "done"]
    lats = sorted(r["latency"] for r in results)
    p = lambda q: lats[min(len(lats) - 1, int(len(lats) * q))]
    tier_dist = collections.Counter(r["tiers"] for r in results)

    print("-" * 60)
    print(f"成功 {len(ok)} / 失败 {len(bad)} / 总数 {args.total}")
    print(f"wall 时间 {wall:.1f}s · 吞吐 {args.total / wall:.1f} 会话/s")
    if lats:
        print(f"端到端延迟 p50={p(0.5):.2f}s · p95={p(0.95):.2f}s · "
              f"max={lats[-1]:.2f}s")
    print("档位分布（tier_usage -> 会话数）:")
    for tiers, n in tier_dist.most_common():
        print(f"  {dict(tiers) or '{}'}: {n}")
    if bad:
        print("异常会话：", [r["rid"] for r in bad][:10])
        sys.exit(1)
    print("压测 PASS")


if __name__ == "__main__":
    main()
