# -*- coding: utf-8 -*-
"""DeRT 服务化：FastAPI + SSE 外壳（①外壳 + ②断点续跑 + ③审批门，2026-10-25）。

端点：
  POST /runs                 提交任务（task_id 或自由 prompt；require_approval 开审批门）
  POST /runs/{id}/resume     从断点续跑（服务重启中断的会话）
  POST /runs/{id}/approve    审批高危工具调用 {"approved": bool}
  GET  /runs                 会话列表（重启不丢）
  GET  /runs/{id}            会话快照（状态/事件/结果/档位用量）
  GET  /runs/{id}/events     SSE 实时事件流（重放+跟随，seq 去重）
  GET  /tasks                内置任务库

设计：
  - 事件流 = Trace(sink=...)（src/instrument.py 既有通道）；token 走 emit_token；
  - 每次 run 独立客户端与调度器（门状态按 run 隔离）；调度器 prompt 注入勿漏；
  - ②断点：on_step 每步落盘 checkpoint.json（messages+elapsed+step）；
    resume 从消息历史继续，预算按已耗时长继续扣；调度器门状态不入断点
    （新实例重新收敛——诚实口径）；
  - ③审批门：risk_level=high 的工具调用挂起等 POST /approve，超时=拒绝；
  - ④工具 MCP 化见 service/mcp_server.py（同一批工具的标准协议出口）；
  - 状态落盘 service/state/<id>/events.jsonl + snapshot.json + checkpoint.json。

启动：
  <venv-python> -m uvicorn service.app:app --port 8630
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.agent import run as agent_run, GENERIC_SYSTEM_PROMPT
from src.instrument import Trace
from src.llm import OpenAICompatLLM, bailian_llm
from src.scheduler import (DeadlineSchedulerV3E, TripleTierLLM,
                           DeadlineSchedulerV3EEmp)
from src.tools import default_tools
from eval.tasks import TASKS
from eval.bfcl_adapter import load_category, replay_ground_truth
from eval.bfcl_run import run_episode
from eval.hammerbench_adapter import (load_conversations,
                                      pick_episodes as _hb_pick)
from eval.hammerbench_run import run_episode as _hb_run_episode

MID_MODEL = "deepseek-v4-flash-0731"
STATE_DIR = Path(os.environ.get("DERT_SERVICE_STATE_DIR",
                                str(Path(__file__).resolve().parent / "state")))
APPROVAL_TIMEOUT = float(os.environ.get("DERT_APPROVAL_TIMEOUT", "300"))

app = FastAPI(title="DeRT Scheduler Service", version="0.2.0")


# --------------------------------------------------------------------------
# 会话存储：events.jsonl 逐事件落盘 + snapshot / checkpoint / request
# --------------------------------------------------------------------------

class SessionStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.queues: dict[str, queue.Queue] = {}
        self._seqs: dict[str, int] = {}
        self.pending: dict[str, dict] = {}   # run_id -> {event, request}
        self.lock = threading.Lock()

    def _dir(self, run_id: str) -> Path:
        d = self.root / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def append(self, run_id: str, ev: dict) -> dict:
        seq = self._seqs.get(run_id)
        if seq is None:  # 重启恢复：以已落盘事件数续排
            seq = len(self.read_events(run_id))
        seq += 1
        self._seqs[run_id] = seq
        ev = {"seq": seq, **ev}
        with (self._dir(run_id) / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return ev

    def read_events(self, run_id: str) -> list[dict]:
        p = self.root / run_id / "events.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
                if l.strip()]

    def _write(self, run_id: str, name: str, data: dict) -> None:
        (self._dir(run_id) / name).write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    def _read(self, run_id: str, name: str) -> dict | None:
        p = self.root / run_id / name
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def save_request(self, run_id: str, meta: dict) -> None:
        self._write(run_id, "request.json", meta)

    def load_request(self, run_id: str) -> dict | None:
        return self._read(run_id, "request.json")

    def save_checkpoint(self, run_id: str, ck: dict) -> None:
        self._write(run_id, "checkpoint.json", ck)

    def load_checkpoint(self, run_id: str) -> dict | None:
        return self._read(run_id, "checkpoint.json")

    def finalize(self, run_id: str, snapshot: dict) -> None:
        self._write(run_id, "snapshot.json", snapshot)

    def snapshot(self, run_id: str) -> dict | None:
        p = self.root / run_id / "snapshot.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        if (self.root / run_id / "events.jsonl").exists():
            return {"run_id": run_id, "status": "interrupted",
                    "resumable": self.load_checkpoint(run_id) is not None,
                    "note": "服务重启前未收尾；有 checkpoint 可 POST /resume 续跑"}
        return None

    def list_ids(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    def mark_interrupted_on_startup(self) -> None:
        """启动扫描：有事件、无快照、无活动线程的历史会话 → 落 interrupted 快照。"""
        for rid in self.list_ids():
            if rid in self.queues:
                continue
            if (self.root / rid / "snapshot.json").exists():
                continue
            if (self.root / rid / "events.jsonl").exists():
                snap = self.snapshot(rid) or {}
                snap["marked_at"] = datetime.now().isoformat(timespec="seconds")
                self.finalize(rid, snap)


store = SessionStore(STATE_DIR)


# --------------------------------------------------------------------------
# LLM 构建（每次 run 独立实例：调度器门状态天然按 run 隔离）
# --------------------------------------------------------------------------

class FakeLLM:
    """脚本化假 LLM：普通任务 = list_segments → FINAL；
    prompt 含「需要改配置」= apply_config_change（高危，供审批门联调）→ FINAL。
    支持 on_token（模拟流式）：内容分片回调，覆盖 token 直播链路的测试。"""

    def __init__(self, model: str, on_token=None):
        self.model = model
        self.base_url = "fake://scripted"
        self._calls = 0
        self.on_token = on_token

    def _emit_content(self, content: str) -> None:
        if self.on_token and content:
            for i in range(0, len(content), 4):
                try:
                    self.on_token(content[i:i + 4])
                except Exception:
                    pass

    def chat(self, messages, model=None, tools=None, temperature=0.0,
             max_tokens=None):
        self._calls += 1
        if self._calls == 1 and tools:
            names = {t["function"]["name"] for t in tools}
            if "apply_config_change" in names and any(
                    "需要改配置" in (m.get("content") or "")
                    for m in messages if m.get("role") == "user"):
                call = {"id": "call_1", "name": "apply_config_change",
                        "arguments": {"key": "fps", "value": "30"}}
            else:
                call = {"id": "call_1", "name": "list_segments", "arguments": {}}
            self._emit_content("好的，我先查一下当前状态。")
            return _fake_resp(model or self.model, content="好的，我先查一下当前状态。",
                              tool_calls=[call])
        content = "FINAL: 演示结论：处理完成（fake）"
        self._emit_content(content)
        return _fake_resp(model or self.model, content=content)


def _fake_resp(model: str, content: str | None = None,
               tool_calls: list | None = None):
    from src.agent import ModelResponse
    return ModelResponse(content=content, tool_calls=tool_calls or [],
                         model=model, ttft=0.01, total=0.02,
                         tokens_in=10, tokens_out=5)


# --------------------------------------------------------------------------
# 公共基准任务源（BFCL v4 多轮）：英文重标定难度 + 多轮执行路径
# --------------------------------------------------------------------------

# 外部验证结论：中文词表跨域恒不触发（≡C）；英文重标定是部署形态。
# 仅用于 BFCL 任务的自建集调度器变体（与 eval/bfcl_run._V3DEn 同词表）。
CMP_WORDS_EN = ("whether", "compare", "versus", " vs.", " vs ", "exceed",
                "threshold", "most", "least", "best", "cheapest", "highest",
                "lowest", "latest", "longest", "shortest", "how many",
                "count", "average", "total")


class _V3EEn(DeadlineSchedulerV3E):
    """V3E 的英文难度词表变体（仅换 CMP_WORDS，阶梯/门机制逐字继承）。"""

    CMP_WORDS = CMP_WORDS_EN


BFCL_CATEGORY = "BFCL_v4_multi_turn_base"
BFCL_N_TASKS = 10          # 服务里展示的 episode 数（均匀采样）
_BFCL_CACHE: dict = {}


def _bfcl_episodes() -> dict[str, dict]:
    """惰性加载并缓存精选 episode（2–5 轮，均匀采样，确定性）。"""
    if "eps" not in _BFCL_CACHE:
        eps = load_category(BFCL_CATEGORY)
        mid = [e for e in eps if 2 <= len(e["question"]) <= 5]
        step = max(1, len(mid) // BFCL_N_TASKS)
        picked = mid[::step][:BFCL_N_TASKS]
        _BFCL_CACHE["eps"] = {e["id"]: e for e in picked}
    return _BFCL_CACHE["eps"]


def bfcl_task_list() -> list[dict]:
    out = []
    for eid, ep in _bfcl_episodes().items():
        out.append({
            "id": f"BFCL/{eid}", "type": "BFCL",
            "prompt": ep["question"][0][0]["content"],
            "turns": len(ep["question"]),
            "classes": ep["involved_classes"],
        })
    return out


# --------------------------------------------------------------------------
# 公共基准任务源（HammerBench 中文多轮）：中文 slot-filling，难度词表原配
# --------------------------------------------------------------------------

HB_N_TASKS = 10            # 服务里展示的 conversation 数（类型轮转采样）
_HB_CACHE: dict = {}


def _hb_episodes() -> dict[str, dict]:
    """惰性加载并缓存精选 conversation（2–5 轮，类型覆盖，确定性）。"""
    if "eps" not in _HB_CACHE:
        picked = _hb_pick(load_conversations(), HB_N_TASKS)
        _HB_CACHE["eps"] = {e["id"]: e for e in picked}
    return _HB_CACHE["eps"]


def hb_task_list() -> list[dict]:
    out = []
    for eid, ep in _hb_episodes().items():
        out.append({
            "id": f"HB/{eid}", "type": "HB",
            "prompt": ep["turns"][0]["user"],
            "turns": len(ep["turns"]),
            "classes": [ep["datatype"]],
        })
    return out


# --------------------------------------------------------------------------
# ③审批门：高危工具调用挂起等人工批准（超时=拒绝）
# --------------------------------------------------------------------------

class ApprovalGate:
    """包住 ToolRegistry：risk_level=high 的调用先emit approval_required，
    等待 POST /runs/{id}/approve（或超时），拒绝则不执行、返回拒绝输出。"""

    def __init__(self, registry, run_id: str, emit, timeout: float):
        self.reg = registry
        self.run_id = run_id
        self.emit = emit
        self.timeout = timeout

    def specs(self):
        return self.reg.specs()

    def execute_many(self, calls: list[dict], trace=None,
                     parallel: bool = True) -> list[str]:
        out = []
        for c in calls:  # 含审批路径时退化为顺序执行（审批天然串行）
            if self.reg.risk_level(c["name"]) == "high":
                out.append(self._gated(c, trace))
            else:
                out.append(self.reg.execute(c["name"], c["arguments"],
                                            trace=trace))
        return out

    def _gated(self, call: dict, trace=None) -> str:
        req_id = uuid.uuid4().hex[:8]
        request = {"request_id": req_id, "tool": call["name"],
                   "arguments": call.get("arguments", {})}
        ev = threading.Event()
        with store.lock:
            store.pending[self.run_id] = {"event": ev, "request": request}
        self.emit({"kind": "approval_required", **request,
                   "timeout_s": self.timeout})
        approved = ev.wait(timeout=self.timeout)
        store.pending.pop(self.run_id, None)
        self.emit({"kind": "approval_result", "request_id": req_id,
                   "tool": call["name"], "approved": bool(approved),
                   "reason": "人工批准" if approved else "超时或拒绝"})
        if approved:
            return self.reg.execute(call["name"], call.get("arguments", {}),
                                    trace=trace)
        return json.dumps({"error": "用户拒绝执行该高危操作（或审批超时）",
                           "tool": call["name"]}, ensure_ascii=False)


# --------------------------------------------------------------------------
# 后台执行线程（②checkpoint / resume）
# --------------------------------------------------------------------------

class RunRequest(BaseModel):
    task_id: str | None = Field(None, description="内置任务库 id（如 T2-1）")
    prompt: str | None = Field(None, description="自由任务文本（与 task_id 二选一）")
    deadline: float | None = Field(
        None, gt=0,
        description="时间预算（秒）。不填 = 自动档：判卷驱动的级联（快档先试→判卷→不过升级强档）")
    provider: str = Field("bailian", description="bailian | fake")
    require_approval: bool = Field(False, description="高危工具需人工审批")


class ApproveRequest(BaseModel):
    approved: bool


def _tier_of(model: str) -> str:
    return "strong" if "v4.1" in model else \
        "mid" if "0731" in model else "fast"


def _tier_usage(run_id: str) -> dict[str, int]:
    """从事件流统计各档调用次数（影子流量归因 + 快照 tier_usage 共用）。"""
    usage: dict[str, int] = {}
    for ev in store.read_events(run_id):
        if ev["kind"] == "llm_call":
            t = _tier_of(ev["model"])
            usage[t] = usage.get(t, 0) + 1
    return usage


# --------------------------------------------------------------------------
# 影子流量难度史（v3emp 的服务化落地）：自动积累 + 调度读取
# --------------------------------------------------------------------------

HISTORY_PATH = STATE_DIR / "difficulty_history.json"
HISTORY_LOCK = threading.Lock()


def load_history() -> dict:
    """{task_id: {"wrong": int, "ok": int, "model": 快档模型名}}。"""
    try:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def hard_set(history: dict) -> dict[str, bool]:
    """1−p 最简形态：任何一次快档错误即记 hard（与 eval 侧口径一致）。"""
    return {k: bool(v.get("wrong")) for k, v in history.items()}


def record_shadow(task_id: str, finished: bool, had_fast: bool,
                  had_strong: bool, fast_model: str,
                  fast_first_attempt_failed: bool = False) -> dict | None:
    """run 收尾后把结果计入影子流量难度史（线程安全）。

    口径：快档参与且最终失败 → wrong（该任务此后路由强档）；
    纯快档完成（无强档参与）且成功 → ok。混合档成功不记（归因不清），
    除非救援触发过（首次快档尝试即失败 → wrong，即使强档救回）。
    """
    if not task_id or not had_fast:
        return None
    if not finished or fast_first_attempt_failed:
        verdict = "wrong"
    elif not had_strong:
        verdict = "ok"
    else:
        return None
    with HISTORY_LOCK:
        h = load_history()
        ent = h.setdefault(task_id, {"wrong": 0, "ok": 0, "model": fast_model})
        ent[verdict] += 1
        ent["model"] = fast_model
        HISTORY_PATH.write_text(json.dumps(h, ensure_ascii=False, indent=1),
                                encoding="utf-8")
        return {"task": task_id, "verdict": verdict,
                "wrong": ent["wrong"], "ok": ent["ok"],
                "history_size": len(h)}


# --------------------------------------------------------------------------
# 验证层（自动档核心）：判卷 = 交付条件，而非时间
# --------------------------------------------------------------------------

AUTO_CAP = float(os.environ.get("DERT_AUTO_CAP", "120"))   # 自动档熔断（防失控）
JUDGE_MODEL = os.environ.get("DERT_JUDGE_MODEL", "qwen2.5:7b")
OLLAMA_BASE = os.environ.get("DERT_OLLAMA_BASE", "http://127.0.0.1:11434/v1")
# 提示词 = 实验 A 校准过的 v2 中性版（召回 96.3% / 误报 13.7%，见收官文档三⅘）
JUDGE_SYSTEM = ("你是答案质检员。根据题目要求判断回答是否完成了任务。"
                "不要因为措辞风格、详细程度或格式不合你的偏好而判未完成；"
                "只有当你确信回答未完成、答非所问或内容错误时才判未完成；"
                "拿不准时倾向判完成。")


def judge_answer(question: str, answer: str) -> dict:
    """运行时判卷：本地 Ollama 快档当质检员（与快档同模型，免费）。

    不可用（Ollama 未启动/超时）→ verdict=unavailable，调用方按 pass 交付
    并在快照里如实标注——验证层失效时降级为纯快档，而不是阻塞服务。
    """
    try:
        from openai import OpenAI
        client = OpenAI(base_url=OLLAMA_BASE, api_key="ollama", timeout=15)
        t0 = time.perf_counter()
        resp = client.chat.completions.create(
            model=JUDGE_MODEL, temperature=0, max_tokens=200,
            messages=[{"role": "system", "content": JUDGE_SYSTEM},
                      {"role": "user", "content":
                       f"【任务】\n{question}\n\n【助手回答】\n{answer or '（空回答）'}\n\n"
                       "请判断这份回答是否完成了任务。缺少必要的执行结果、"
                       "答非所问或编造数据，都算未完成。\n"
                       '只输出一行 JSON：{"verdict":"pass","reason":"一句话"} 或 '
                       '{"verdict":"fail","reason":"一句话"}，不要输出其他内容。'}])
        dt = time.perf_counter() - t0
        m = re.search(r"\{[^{}]*\}", resp.choices[0].message.content or "", re.S)
        if not m:
            return {"verdict": "unavailable", "reason": "判卷输出解析失败",
                    "judge_s": round(dt, 3)}
        obj = json.loads(m.group(0))
        return {"verdict": "pass" if obj.get("verdict") == "pass" else "fail",
                "reason": str(obj.get("reason", ""))[:160],
                "judge_s": round(dt, 3)}
    except Exception as exc:   # Ollama 未启动 / 网络失败 / 超时
        return {"verdict": "unavailable",
                "reason": f"判卷不可用：{type(exc).__name__}",
                "judge_s": None}


def build_llms(provider: str, on_token=None, difficulty: str = "zh",
               task_id: str = ""):
    fast = OpenAICompatLLM(cache_dir="", on_token=on_token)
    if provider == "fake":
        fast = FakeLLM("qwen2.5:7b", on_token=on_token)
        mid = FakeLLM(MID_MODEL, on_token=on_token)
        strong = FakeLLM("deepseek-v4.1-flash", on_token=on_token)
    else:
        strong = bailian_llm(cache_dir="", on_token=on_token)
        mid = bailian_llm(model=MID_MODEL, cache_dir="", on_token=on_token)
    hist = hard_set(load_history())       # 影子流量难度史（冷启动任务默认 easy）
    sched = (_V3EEn(fast.model, strong.model, mid.model) if difficulty == "en"
             else DeadlineSchedulerV3EEmp(fast.model, strong.model,
                                          mid.model, history=hist))
    if task_id and hasattr(sched, "set_task_id"):
        sched.set_task_id(task_id)
    llm = TripleTierLLM(fast, mid, strong, sched)
    return llm, sched, {"fast": fast.model, "mid": mid.model,
                        "strong": strong.model}


def _finalize(run_id: str, result, provider: str, deadline: float,
              shadow: dict | None = None) -> dict:
    events = store.read_events(run_id)
    usage: dict[str, int] = {}
    for ev in events:
        if ev["kind"] == "llm_call":
            t = _tier_of(ev["model"])
            usage[t] = usage.get(t, 0) + 1
    meta = store.load_request(run_id) or {}
    snap = {
        "run_id": run_id, "status": "done",
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "prompt": meta.get("prompt", ""), "deadline": deadline,
        "provider": provider, "require_approval": meta.get("require_approval", False),
        "answer": (result.answer or "")[:2000], "stop_reason": result.stop_reason,
        "finished": result.finished, "steps": result.steps,
        "total_time": round(result.total_time, 3),
        "tier_usage": usage, "summary": result.trace.summary(),
    }
    if shadow:
        snap["shadow"] = shadow
    store.finalize(run_id, snap)
    return snap


def _run_with_rescue(run_once, triple, deadline: float, trace) -> tuple[dict, bool]:
    """公共基准演示形态的失败升级救援（fast-first 级联的第一步）。

    首轮按调度器路由跑；失败且剩余预算足够（≥3s 保守下限）时，用强档
    直连重跑一次。trigger = 结构化判分失败（如 slot-filling 未调工具、
    状态比对不一致）——这类失败零成本可检测，不需要 LLM 验证器。

    只作用于服务演示层：eval/ 各 runner 的正式评估口径不变（官方成绩
    取首次，救援成绩单列）。run_once(llm, scheduler, deadline) -> rec。
    """
    rec = run_once(triple, getattr(triple, "scheduler", None), deadline)
    if rec.get("success"):
        return rec, False
    # deadline=None（自动档/不限预算）→ 剩余视为无界，升级门恒过；
    # 有 deadline 时才按「剩余 ≥ 3s 保守下限」裁断
    remaining = None if deadline is None else \
        deadline - (rec.get("total_time") or 0.0)
    strong = getattr(triple, "strong", None)
    if strong is None:
        return rec, False
    if remaining is not None and remaining < 3.0:
        return rec, False
    trace.emit("escalate", reason=(rec.get("first_bad_diff")
                                   or rec.get("stop_reason") or "")[:200],
               first_time=rec.get("total_time"),
               remaining=round(remaining, 3) if remaining is not None else None,
               model=strong.model)
    strong.strong_model_name = strong.model
    rec2 = run_once(strong, None, remaining)
    rec2["rescue"] = {"attempted": True, "rescued": bool(rec2.get("success")),
                      "first_attempt": {"time": rec.get("total_time"),
                                        "stop": rec.get("stop_reason")}}
    return rec2, True


def _execute_bfcl(run_id: str, meta: dict) -> None:
    """公共基准（BFCL 多轮）执行路径：脚本化用户轮次 + 逐轮状态判分。

    与 CV 任务路径的差异：多轮循环（run_episode）、英文重标定难度信号、
    不走审批门/断点（BFCL 工具为基准内模拟操作，checkpoint 语义不同）。
    """
    q = store.queues[run_id]
    deadline = meta["deadline"]

    def on_event(ev: dict) -> dict:
        stored = store.append(run_id, ev)
        q.put(stored)
        return stored

    trace = Trace(run_id=run_id, sink=on_event)
    _trace_holder: dict = {}

    def on_token(text: str) -> None:
        tr = _trace_holder.get("t")
        if tr is not None:
            tr.emit_token(text)

    ep = _bfcl_episodes()[meta["bfcl"]]
    try:
        llm, sched, _tiers = build_llms(meta["provider"], on_token=on_token,
                                        difficulty="en",
                                        task_id=meta.get("task_id", ""))
        _trace_holder["t"] = trace
        # instances/registry 由 run_episode 内部自建，外层不再重复构建
        gt_snaps = replay_ground_truth(ep["initial_config"],
                                       ep["involved_classes"], ep["_gt"])

        def run_once(llm_arg, sched_arg, dl):
            return run_episode(ep, llm_arg, scheduler=sched_arg,
                               deadline=dl, gt_snaps=gt_snaps, trace=trace)
        rec, _rescued = _run_with_rescue(run_once, llm, deadline, trace)
        usage = _tier_usage(run_id)
        shadow = record_shadow(meta.get("task_id", ""), finished=rec["success"],
                               had_fast=usage.get("fast", 0) > 0,
                               had_strong=usage.get("strong", 0) > 0,
                               fast_model=_tiers["fast"],
                               fast_first_attempt_failed=bool(
                                   (rec.get("rescue") or {}).get("attempted")))
        store.finalize(run_id, {
            "run_id": run_id, "status": "done",
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "prompt": meta.get("prompt", ""), "deadline": deadline,
            "provider": meta["provider"],
            "answer": (rec.get("answer") or "")[:2000],
            "stop_reason": rec["stop_reason"], "finished": rec["success"],
            "steps": rec["steps"], "total_time": rec["total_time"],
            "tier_usage": usage,
            "rescue": rec.get("rescue"),
            **({"shadow": shadow} if shadow else {}),
            "bfcl": {"episode": rec["episode"], "success": rec["success"],
                     "turns_total": rec["turns_total"],
                     "turns_valid": rec["turns_valid"],
                     "first_bad_diff": rec["first_bad_diff"]},
        })
    except Exception as exc:
        store.finalize(run_id, {
            "run_id": run_id, "status": "error",
            "error": f"{type(exc).__name__}: {exc}"[:500],
            "finished_at": datetime.now().isoformat(timespec="seconds")})
    q.put({"kind": "__done__"})


def _execute_hb(run_id: str, meta: dict) -> None:
    """公共基准（HammerBench 中文多轮 slot-filling）执行路径。

    与 BFCL 路径的差异：判分 = 逐轮调用比对（函数名+归一化参数精确匹配，
    无环境状态）；工具执行 = stub success（基准只提供 schema）；
    难度信号 = 中文词表原配（build_llms 默认 zh，无需重标定）。
    同样不走审批门/断点续跑。
    """
    q = store.queues[run_id]
    deadline = meta["deadline"]

    def on_event(ev: dict) -> dict:
        stored = store.append(run_id, ev)
        q.put(stored)
        return stored

    trace = Trace(run_id=run_id, sink=on_event)
    _trace_holder: dict = {}

    def on_token(text: str) -> None:
        tr = _trace_holder.get("t")
        if tr is not None:
            tr.emit_token(text)

    ep = _hb_episodes()[meta["hb"]]
    try:
        llm, sched, _tiers = build_llms(meta["provider"], on_token=on_token,
                                        difficulty="zh",
                                        task_id=meta.get("task_id", ""))
        _trace_holder["t"] = trace
        def run_once(llm_arg, sched_arg, dl):
            return _hb_run_episode(ep, llm_arg, scheduler=sched_arg,
                                   deadline=dl, trace=trace)
        rec, _rescued = _run_with_rescue(run_once, llm, deadline, trace)
        usage = _tier_usage(run_id)
        shadow = record_shadow(meta.get("task_id", ""), finished=rec["success"],
                               had_fast=usage.get("fast", 0) > 0,
                               had_strong=usage.get("strong", 0) > 0,
                               fast_model=_tiers["fast"],
                               fast_first_attempt_failed=bool(
                                   (rec.get("rescue") or {}).get("attempted")))
        store.finalize(run_id, {
            "run_id": run_id, "status": "done",
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "prompt": meta.get("prompt", ""), "deadline": deadline,
            "provider": meta["provider"],
            "answer": (rec.get("answer") or "")[:2000],
            "stop_reason": rec["stop_reason"], "finished": rec["success"],
            "steps": rec["steps"], "total_time": rec["total_time"],
            "tier_usage": usage,
            "rescue": rec.get("rescue"),
            **({"shadow": shadow} if shadow else {}),
            "hb": {"episode": rec["episode"], "success": rec["success"],
                   "turns_total": rec["turns_total"],
                   "turns_valid": rec["turns_valid"],
                   "first_bad_diff": rec["first_bad_diff"]},
        })
    except Exception as exc:
        store.finalize(run_id, {
            "run_id": run_id, "status": "error",
            "error": f"{type(exc).__name__}: {exc}"[:500],
            "finished_at": datetime.now().isoformat(timespec="seconds")})
    q.put({"kind": "__done__"})


def _execute_auto(run_id: str, meta: dict) -> None:
    """自动档（deadline 不填）：交付条件 = 判卷通过，而非时间到。

    流程（实验 B 验证的级联，全部真实现场执行）：
      1. 查影子难度史：hard → 强档直上（熟客效应，省掉注定失败的快档尝试）；
      2. 否则快档先试（熔断上限 AUTO_CAP，正常几秒完）；
      3. 判卷（本地快档当质检员，P50≈0.44s）：
         pass → 交付；Ollama 不可用 → 降级为纯快档交付并如实标注；
      4. fail → 升级强档重跑（自动档无预算约束，升级门恒过；
         AUTO_CAP 是唯一熔断），交付强档结果；
      5. 记账：升级=wrong（下次直上强档）、纯快档成功=ok、混合档不记。
    """
    q = store.queues[run_id]

    def on_event(ev: dict) -> dict:
        stored = store.append(run_id, ev)
        q.put(stored)
        return stored

    _trace_holder: dict = {}

    def on_token(text: str) -> None:
        tr = _trace_holder.get("t")
        if tr is not None:
            tr.emit_token(text)

    prompt = meta["prompt"]
    provider = meta["provider"]
    task_id = meta.get("task_id") or (
        "prompt:" + hashlib.md5(prompt.encode("utf-8")).hexdigest()[:8])
    _t0 = time.perf_counter()
    trace = Trace(run_id=run_id, sink=on_event)
    _trace_holder["t"] = trace
    tools = default_tools()
    if meta.get("require_approval"):
        tools = ApprovalGate(tools, run_id, on_event, APPROVAL_TIMEOUT)
    try:
        llm, _sched, tiers = build_llms(provider, on_token=on_token,
                                        task_id=task_id)
        fast, strong = llm.fast, llm.strong

        def run_tier(llm_arg) -> object:
            # 通用域提示词：自动档可能接到任意自由任务，不能带 CV 角色锁定
            return agent_run(prompt, llm=llm_arg, tools=tools,
                             deadline=AUTO_CAP, scheduler=None, trace=trace,
                             system_prompt=GENERIC_SYSTEM_PROMPT)

        # hard_set 返回 {task_id: bool}（所有有史任务都在），必须取值判断，
        # 不能用 `in`（键存在性）——否则 ok-only 任务也被误判 hard 直上强档
        started_hard = bool(hard_set(load_history()).get(task_id))
        escalated = False
        judge: dict | None = None
        if started_hard:
            # 熟客效应：这个任务快档曾经答错过，直接上强档，省一次必败尝试
            trace.emit("route_hard", task=task_id, model=strong.model)
            result = run_tier(strong)
        else:
            result = run_tier(fast)
            judge = judge_answer(prompt, result.answer or "")
            trace.emit("judge", **judge)
            if judge["verdict"] == "fail":
                trace.emit("escalate", reason=judge["reason"],
                           model=strong.model)
                escalated = True
                result = run_tier(strong)

        usage = _tier_usage(run_id)
        had_fast = usage.get("fast", 0) > 0
        had_strong = usage.get("strong", 0) > 0
        shadow = record_shadow(task_id, finished=bool(result.finished),
                               had_fast=had_fast, had_strong=had_strong,
                               fast_model=tiers["fast"],
                               fast_first_attempt_failed=escalated)
        store.finalize(run_id, {
            "run_id": run_id, "status": "done",
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "prompt": prompt, "deadline": None, "mode": "auto",
            "provider": provider, "require_approval":
                meta.get("require_approval", False),
            "answer": (result.answer or "")[:2000],
            "stop_reason": result.stop_reason, "finished": result.finished,
            "steps": result.steps,
            "total_time": round(time.perf_counter() - _t0, 3),
            "tier_usage": usage, "summary": result.trace.summary(),
            "started_hard": started_hard, "escalated": escalated,
            "judge": judge,
            **({"shadow": shadow} if shadow else {}),
        })
    except Exception as exc:
        store.finalize(run_id, {
            "run_id": run_id, "status": "error",
            "error": f"{type(exc).__name__}: {exc}"[:500],
            "finished_at": datetime.now().isoformat(timespec="seconds")})
    q.put({"kind": "__done__"})


def execute_run(run_id: str, meta: dict, resume: dict | None = None) -> None:
    """执行（或续跑）一次任务。meta: request.json 内容。

    分发优先级：公共基准（BFCL/HB，官方管道+官方判分）> 自动档 >
    CV 预算路径。基准任务即使勾了自动档也走官方管道（自动档的判卷
    不适用于有官方判分的基准），deadline=None 时等效不限预算。
    """
    if meta.get("bfcl"):
        return _execute_bfcl(run_id, meta)
    if meta.get("hb"):
        return _execute_hb(run_id, meta)
    if meta.get("auto"):
        return _execute_auto(run_id, meta)
    q = store.queues[run_id]
    prompt = meta["prompt"]
    deadline = meta["deadline"]
    provider = meta["provider"]

    def on_event(ev: dict) -> dict:
        stored = store.append(run_id, ev)
        q.put(stored)
        return stored

    _trace_holder: dict = {}

    def on_token(text: str) -> None:
        tr = _trace_holder.get("t")
        if tr is not None:
            tr.emit_token(text)

    task_id = meta.get("task_id") or (
        "prompt:" + hashlib.md5(prompt.encode("utf-8")).hexdigest()[:8])
    llm, sched, tiers = build_llms(provider, on_token=on_token,
                                   task_id=task_id)
    sched.set_prompt(prompt)  # 难度路由输入（与 budget_eval harness 同款，勿漏）
    trace = Trace(run_id=run_id, sink=on_event)
    _trace_holder["t"] = trace

    def on_step(ck: dict) -> None:
        store.save_checkpoint(run_id, ck)

    tools = default_tools()
    if meta.get("require_approval"):
        tools = ApprovalGate(tools, run_id, on_event, APPROVAL_TIMEOUT)
    try:
        result = agent_run(prompt, llm=llm, tools=tools, deadline=deadline,
                           scheduler=sched, trace=trace, resume=resume,
                           on_step=on_step)
        usage = _tier_usage(run_id)
        shadow = record_shadow(task_id, finished=bool(result.finished),
                               had_fast=usage.get("fast", 0) > 0,
                               had_strong=usage.get("strong", 0) > 0,
                               fast_model=tiers["fast"])
        _finalize(run_id, result, provider, deadline, shadow=shadow)
    except Exception as exc:
        store.finalize(run_id, {
            "run_id": run_id, "status": "error",
            "error": f"{type(exc).__name__}: {exc}"[:500],
            "finished_at": datetime.now().isoformat(timespec="seconds")})
    q.put({"kind": "__done__"})


def _spawn(run_id: str, meta: dict, resume: dict | None = None) -> None:
    store.queues[run_id] = queue.Queue()
    threading.Thread(target=execute_run, args=(run_id, meta, resume),
                     daemon=True).start()


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@app.post("/runs")
def submit(req: RunRequest):
    prompt = req.prompt
    bfcl_id = None
    hb_id = None
    if req.task_id:
        if req.task_id.startswith("BFCL/"):
            eid = req.task_id[len("BFCL/"):]
            if eid not in _bfcl_episodes():
                raise HTTPException(404, f"episode {eid} 不在精选列表")
            if req.provider == "fake":
                raise HTTPException(422,
                                    "公共基准任务需要真实模型（剧本演示只覆盖内置 CV 任务）")
            bfcl_id = eid
            prompt = _bfcl_episodes()[eid]["question"][0][0]["content"]
        elif req.task_id.startswith("HB/"):
            eid = req.task_id[len("HB/"):]
            if eid not in _hb_episodes():
                raise HTTPException(404, f"conversation {eid} 不在精选列表")
            if req.provider == "fake":
                raise HTTPException(422,
                                    "公共基准任务需要真实模型（剧本演示只覆盖内置 CV 任务）")
            hb_id = eid
            prompt = _hb_episodes()[eid]["turns"][0]["user"]
        else:
            t = next((t for t in TASKS if t["id"] == req.task_id), None)
            if t is None:
                raise HTTPException(404, f"task {req.task_id} 不在任务库")
            prompt = t["prompt"]
    if not prompt:
        raise HTTPException(422, "task_id 与 prompt 至少给一个")
    run_id = uuid.uuid4().hex[:12]
    auto = req.deadline is None
    meta = {"prompt": prompt, "deadline": req.deadline,
            "provider": req.provider, "require_approval": req.require_approval,
            "submitted_at": datetime.now().isoformat(timespec="seconds")}
    if auto:
        meta["auto"] = True
    if req.task_id:
        meta["task_id"] = req.task_id   # 影子流量难度史的键（自由 prompt 在执行期哈希兜底）
    if bfcl_id:
        meta["bfcl"] = bfcl_id
        meta["require_approval"] = False   # BFCL 基准工具不走审批门
    if hb_id:
        meta["hb"] = hb_id
        meta["require_approval"] = False   # HammerBench 同 BFCL：基准管道不走审批门
    store.save_request(run_id, meta)
    store.append(run_id, {"kind": "accepted", "deadline": req.deadline,
                          "provider": req.provider,
                          "require_approval": req.require_approval,
                          **({"auto": True} if auto else {}),
                          **({"bfcl": bfcl_id} if bfcl_id else {}),
                          **({"hb": hb_id} if hb_id else {})})
    _spawn(run_id, meta)
    return {"run_id": run_id, "status": "started",
            "stream": f"/runs/{run_id}/events"}


@app.post("/runs/{run_id}/resume")
def resume_run(run_id: str):
    if run_id in store.queues:
        raise HTTPException(409, "会话正在运行")
    snap = store.snapshot(run_id)
    if snap is None:
        raise HTTPException(404, run_id)
    if snap.get("status") not in ("interrupted", "error"):
        raise HTTPException(409, f"状态 {snap.get('status')} 不可续跑")
    ck = store.load_checkpoint(run_id)
    if ck is None:
        raise HTTPException(409, "无 checkpoint（中断发生得太早），请重新提交")
    meta = store.load_request(run_id)
    if meta is None:
        raise HTTPException(409, "缺 request.json（旧会话），请重新提交")
    store.append(run_id, {"kind": "resume_accepted",
                          "from_step": ck.get("step", 0)})
    _spawn(run_id, meta, resume=ck)
    return {"run_id": run_id, "status": "resumed",
            "stream": f"/runs/{run_id}/events"}


@app.post("/runs/{run_id}/approve")
def approve(run_id: str, req: ApproveRequest):
    with store.lock:
        pend = store.pending.get(run_id)
    if pend is None:
        raise HTTPException(409, "当前没有待审批的调用")
    pend["approved"] = req.approved
    pend["event"].set()
    return {"run_id": run_id, "approved": req.approved}


@app.get("/runs")
def list_runs():
    out = []
    for rid in store.list_ids():
        snap = store.snapshot(rid)
        if snap:
            out.append({k: snap.get(k) for k in
                        ("run_id", "status", "resumable", "deadline",
                         "tier_usage", "stop_reason", "finished_at")})
    return out


@app.get("/runs/{run_id}")
def get_run(run_id: str):
    snap = store.snapshot(run_id)
    if snap is None:
        raise HTTPException(404, run_id)
    snap = dict(snap)
    snap["events"] = store.read_events(run_id)
    return snap


@app.get("/runs/{run_id}/events")
def stream_events(run_id: str):
    if store.snapshot(run_id) is None and run_id not in store.queues:
        raise HTTPException(404, run_id)

    def gen():
        last_seq = 0
        for ev in store.read_events(run_id):
            last_seq = ev.get("seq", 0)
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        if run_id in store.queues:
            q = store.queues[run_id]
            while True:
                try:
                    ev = q.get(timeout=20)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                if ev.get("kind") == "__done__":
                    snap = store.snapshot(run_id) or {}
                    yield f"data: {json.dumps({'kind': 'done', 'snapshot': snap}, ensure_ascii=False)}\n\n"
                    break
                if ev.get("seq", 0) <= last_seq:
                    continue  # 重放与队列衔接处的重复事件
                last_seq = ev.get("seq", 0)
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        else:
            snap = store.snapshot(run_id) or {}
            yield f"data: {json.dumps({'kind': 'done', 'snapshot': snap}, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@app.get("/tasks")
def list_tasks():
    """任务库：公共基准优先（HB 中文 → BFCL 英文）。

    内置 CV 任务不再进任务库（用户指令：UI 只保留公共基准），但
    submit 仍接受其 task_id（向后兼容 + 剧本演示可用自由 prompt）。
    """
    out = hb_task_list()
    out += bfcl_task_list()
    return out


@app.on_event("startup")
def _startup():
    store.mark_interrupted_on_startup()


# 评测曲线图（docs/figures/）直接挂 /figures 供 UI 引用——必须在 "/" 之前挂，
# 否则根 mount 会拦截所有路径
_FIGURES = Path(__file__).resolve().parent.parent / "docs" / "figures"
if _FIGURES.exists():
    app.mount("/figures", StaticFiles(directory=str(_FIGURES)), name="figures")

_STATIC = Path(__file__).resolve().parent / "static"
if (_STATIC / "index.html").exists():
    app.mount("/", StaticFiles(directory=str(_STATIC), html=True), name="ui")
