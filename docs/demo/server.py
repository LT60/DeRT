"""DeRT 实时调度台后端（展示层，不属于运行时）。

边界（AGENTS.md §5 第 9 项要求处理的「展示层 vs 零框架叙事」）：
    本文件是**展示层**，不是运行时的一部分。运行时 src/ 依然零框架、唯一依赖
    openai；这里也刻意只用 Python 标准库 http.server 写 SSE——新增零依赖，
    requirements.txt 保持一行 openai，且"连 Demo 后端也是零框架手写"反过来
    强化而非削弱项目论点。

代价与约束：Demo 依赖本机 Ollama 与 DeepSeek 密钥，无法部署到 GitHub Pages。
因此它是**本地运行**的演示；对外可托管的仍是 docs/demo/index.html（静态回放，
数据已内联、零后端）。两者是同一份运行时产出的两种展示形态。

启动（项目根目录）：
    python docs/demo/server.py                 # 默认 http://127.0.0.1:8765
    DERT_DEMO_PORT=9000 python docs/demo/server.py
    DERT_DEMO_CACHE=1 python docs/demo/server.py   # 开响应缓存（省成本，但延迟不真实）

环境变量：
    DERT_DEMO_PORT         监听端口（默认 8765）
    DERT_DEMO_STRONG_CAP   1 小时内允许的强档运行次数（默认 24，成本护栏）
    DERT_DEMO_CACHE        非空则启用响应缓存（默认禁用 = 全实时口径）
    DEEPSEEK_API_KEY       强档密钥（缺失则强档不可用，页面只跑快档）

已知取舍：客户端断开后本次运行仍会跑完（agent.run 无中断点），本地演示场景下
可接受；强档调用受 STRONG_CAP 与串行锁双重限制，单次误触不会放大成本。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent          # docs/demo
PROJECT_ROOT = ROOT.parent.parent               # DeRT/
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.agent import run                                   # noqa: E402
from src.instrument import Trace                            # noqa: E402
from src.llm import OpenAICompatLLM, deepseek_llm           # noqa: E402
from src.scheduler import DeadlineScheduler, DualTierLLM    # noqa: E402
from src.tools import default_tools                         # noqa: E402
from eval.tasks import TASKS                                # noqa: E402

# ---- 配置 ----
PORT = int(os.environ.get("DERT_DEMO_PORT", "8765"))
STRONG_CAP = int(os.environ.get("DERT_DEMO_STRONG_CAP", "24"))   # 每小时强档运行数
CACHE_ON = bool(os.environ.get("DERT_DEMO_CACHE"))
MIN_BUDGET, MAX_BUDGET = 1.0, 30.0
MAX_TASK_CHARS = 300
MAX_STEPS = 8
STRONG_MODEL = "deepseek-chat"
FAST_MODEL = os.environ.get("DERT_MODEL", "qwen2.5:7b")


def _log(msg: str) -> None:
    print(f"[demo] {msg}", flush=True)


def _bypass_local_proxy() -> None:
    """本地快档走 127.0.0.1，必须绕过环境注入的 HTTP 代理。

    公司网络 / 开发工具常注入 HTTP_PROXY，openai SDK 会把连 Ollama 的本地请求
    也发给代理 → 连接被拒（演示现场最常见的一次性故障）。这里只把本机地址加入
    NO_PROXY，云端强档仍沿用环境原有的代理设置。
    """
    cur = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    parts = [p.strip() for p in cur.split(",") if p.strip()]
    for host in ("127.0.0.1", "localhost"):
        if host not in parts:
            parts.append(host)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join(parts)


# ---- 预设任务：可规则判分（判分器来自 eval/tasks.py，与正式评估同一套）----

def _build_presets() -> list[dict]:
    out: list[dict] = []
    for ttype, take in (("T1", 2), ("T2", 3), ("T3", 3)):
        for t in [x for x in TASKS if x["type"] == ttype][:take]:
            out.append({"id": t["id"], "type": ttype, "prompt": t["prompt"]})
    return out


PRESETS = _build_presets()

STATE: dict = {
    "warmed": threading.Event(),     # Ollama 预热完成标志
    "run_lock": threading.Lock(),    # 串行化运行：并发会争 GPU/带宽，污染延迟
    "strong_runs": deque(),          # 强档运行时间戳（1 小时滑窗）
    "scheduler": DeadlineScheduler(FAST_MODEL, STRONG_MODEL),
    "strong_error": None,
}


def _cache_dir() -> str | None:
    """None = 走默认缓存目录（开发期零成本）；"" = 禁用缓存（全实时口径）。"""
    return None if CACHE_ON else ""


def _backends(on_token=None):
    fast = OpenAICompatLLM(cache_dir=_cache_dir(), on_token=on_token)
    strong = None
    try:
        strong = deepseek_llm(cache_dir=_cache_dir(), on_token=on_token)
    except RuntimeError as exc:      # 无密钥
        STATE["strong_error"] = str(exc)
    return fast, strong


def _warmup() -> None:
    """Ollama 冷启动首调用 TTFT ~65s，不预热会毁掉第一次演示（AGENTS.md §4）。"""
    try:
        fast = OpenAICompatLLM(cache_dir="")
        t0 = time.perf_counter()
        fast.chat([{"role": "user", "content": "回复 ok"}])
        _log(f"Ollama 预热完成（{time.perf_counter() - t0:.1f}s）")
    except Exception as exc:
        _log(f"Ollama 预热失败：{exc!r}（首次运行可能触发冷启动长 TTFT）")
    finally:
        STATE["warmed"].set()


def _reserve_strong() -> bool:
    now = time.time()
    d = STATE["strong_runs"]
    while d and now - d[0] > 3600:
        d.popleft()
    if len(d) >= STRONG_CAP:
        return False
    d.append(now)
    return True


def _refund_strong() -> None:
    d = STATE["strong_runs"]
    if d:
        d.pop()


def stream_run(push, task: str, budget: float, strategy: str, preset_id: str) -> None:
    """跑一次真实运行，把事件实时推给 push（SSE 单条写入函数）。"""
    trace = Trace(sink=push)
    fast, strong = _backends(on_token=trace.emit_token)

    if strategy in ("dert", "strong") and strong is None:
        push({"kind": "error",
              "message": f"强档不可用（{STATE['strong_error']}）。请设置 DEEPSEEK_API_KEY 或改用固定快档。"})
        return

    if strategy in ("dert", "strong") and not _reserve_strong():
        push({"kind": "error",
              "message": f"成本护栏：1 小时内强档运行上限 {STRONG_CAP} 次已达。"
                         f"请改用固定快档（本地、零成本）或稍后再试。"})
        return
    reserved = strategy in ("dert", "strong")

    scheduler = STATE["scheduler"]
    if strategy == "dert":
        llm, model_arg, sched = DualTierLLM(fast, strong, scheduler), None, scheduler
        deadline = budget                      # DeRT：预算参与调度
    elif strategy == "strong":
        llm, model_arg, sched = strong, strong.model, None
        deadline = None                        # 基线：不感知预算（与 make_demo 口径一致）
    else:
        llm, model_arg, sched = fast, fast.model, None
        deadline = None

    used_strong = False
    try:
        result = run(task, llm=llm, tools=default_tools(), model=model_arg,
                     deadline=deadline, max_steps=MAX_STEPS, scheduler=sched,
                     trace=trace)
    except Exception as exc:
        push({"kind": "error", "message": f"运行异常：{exc!r}"})
        return

    used_strong = any(e.get("model") == STRONG_MODEL
                      for e in result.trace.events if e["kind"] == "llm_call")
    if reserved and not used_strong:           # 实际没花强档的钱 → 退还配额
        _refund_strong()

    grade = None
    if preset_id:
        t = next((x for x in TASKS if x["id"] == preset_id), None)
        if t is not None:
            correct, why = t["check"](result.answer or "")
            grade = {"correct": bool(correct), "why": why}

    try:
        result.trace.save(str(PROJECT_ROOT / "eval" / "traces"))
    except Exception:
        pass

    # kind 用 "result" 而非 "run_end"：后者已被 trace 自身的终止事件占用
    # （字段不同，页面若按同名处理会先渲染一次残缺结果）
    push({"kind": "result", "stop_reason": result.stop_reason, "steps": result.steps,
          "total_time": round(result.total_time, 3), "answer": result.answer or "",
          "budget": budget, "on_time": result.total_time <= budget,
          "used_strong": used_strong, "grade": grade,
          "summary": result.trace.summary()})


class Handler(BaseHTTPRequestHandler):
    server_version = "DeRTDemo/1.0"

    # ---- 路由 ----

    def do_GET(self) -> None:
        u = urlparse(self.path)
        if u.path == "/api/run":
            self._handle_run(parse_qs(u.query))
        elif u.path == "/api/meta":
            self._json(self._meta())
        elif u.path == "/api/presets":
            self._json({"presets": PRESETS})
        else:
            self._static(u.path)

    def _meta(self) -> dict:
        d = STATE["strong_runs"]
        return {"fast_model": FAST_MODEL, "strong_model": STRONG_MODEL,
                "strong_available": STATE["strong_error"] is None,
                "strong_error": STATE["strong_error"],
                "strong_cap": STRONG_CAP, "strong_used_1h": len(d),
                "cache": CACHE_ON, "warmed": STATE["warmed"].is_set(),
                "budget_range": [MIN_BUDGET, MAX_BUDGET],
                "max_task_chars": MAX_TASK_CHARS, "presets": len(PRESETS)}

    # ---- 静态文件 ----

    _TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
              ".css": "text/css; charset=utf-8", ".png": "image/png",
              ".json": "application/json; charset=utf-8"}

    def _static(self, path: str) -> None:
        rel = "live.html" if path in ("", "/") else path.lstrip("/")
        p = (ROOT / rel).resolve()
        if not (p.is_file() and (ROOT == p.parent or ROOT in p.parents)):
            self.send_error(404, "not found")
            return
        body = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", self._TYPES.get(p.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- SSE ----

    def _handle_run(self, q: dict) -> None:
        def one(k: str, default: str = "") -> str:
            return (q.get(k) or [default])[0]

        preset_id = one("preset").strip()
        task = one("task").strip()
        strategy = one("strategy", "dert")
        try:
            budget = float(one("budget", "8"))
        except ValueError:
            budget = 8.0

        if preset_id:
            t = next((x for x in PRESETS if x["id"] == preset_id), None)
            if t is None:
                self._json({"error": f"未知预设任务 {preset_id!r}"}, 400)
                return
            task = t["prompt"]
        if not task:
            self._json({"error": "任务不能为空"}, 400)
            return
        if strategy not in ("dert", "fast", "strong"):
            self._json({"error": f"未知策略 {strategy!r}"}, 400)
            return
        budget = max(MIN_BUDGET, min(MAX_BUDGET, budget))
        if len(task) > MAX_TASK_CHARS:
            task = task[:MAX_TASK_CHARS]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")   # 关掉 Nginx 类缓冲
        self.end_headers()

        state = {"alive": True}

        def push(ev: dict) -> None:
            if not state["alive"]:
                return
            try:
                self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False)
                                  + "\n\n").encode("utf-8"))
                self.wfile.flush()
            except Exception:
                state["alive"] = False       # 客户端断开：后续事件静默丢弃

        try:
            if not STATE["warmed"].wait(timeout=90):
                _log("预热超时，仍继续运行（首调用 TTFT 可能偏长）")
            push({"kind": "run_start", "ts": 0.0, "task": task, "budget": budget,
                  "strategy": strategy, "preset": preset_id,
                  "models": {"fast": FAST_MODEL, "strong": STRONG_MODEL}})
            got = STATE["run_lock"].acquire(timeout=120)
            if not got:
                push({"kind": "error", "message": "上一次运行尚未结束（串行执行），请稍候重试。"})
                return
            try:
                stream_run(push, task, budget, strategy, preset_id)
            finally:
                STATE["run_lock"].release()
        except Exception as exc:
            push({"kind": "error", "message": f"服务端异常：{exc!r}"})
        finally:
            push({"kind": "done"})

    def log_message(self, fmt: str, *args) -> None:   # 安静一点
        _log(fmt % args)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    _bypass_local_proxy()

    try:                                   # 强档可用性探针（不发请求，零成本）
        deepseek_llm(cache_dir="")
        STATE["strong_error"] = None
    except RuntimeError as exc:
        STATE["strong_error"] = str(exc)
        _log(f"强档不可用：{exc}")

    threading.Thread(target=_warmup, daemon=True).start()

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    _log(f"实时调度台已启动： http://127.0.0.1:{PORT}/")
    _log(f"策略=DeRT v1.1 / 固定快档 / 固定强档 · 预算 {MIN_BUDGET:g}–{MAX_BUDGET:g}s · "
         f"强档配额 {STRONG_CAP} 次/小时 · 缓存={'开' if CACHE_ON else '关（全实时）'}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        _log("已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
