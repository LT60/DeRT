# -*- coding: utf-8 -*-
"""服务化①测试：fake provider 全链路（提交→事件落盘→快照→SSE 回放→列表）。

全部离线（FakeLLM），不触任何真实 API——CI 可跑。
"""
import json
import time

from fastapi.testclient import TestClient

from service.app import app, store

client = TestClient(app)


def _wait_done(run_id: str, timeout: float = 15.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        snap = store.snapshot(run_id)
        if snap and snap.get("status") in ("done", "error"):
            return snap
        time.sleep(0.1)
    raise TimeoutError(f"{run_id} 未在 {timeout}s 内收尾")


def test_submit_fake_run_completes():
    r = client.post("/runs", json={"task_id": "T1-1", "provider": "fake",
                                   "deadline": 10})
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    snap = _wait_done(run_id)
    assert snap["status"] == "done"
    assert snap["stop_reason"] == "final"
    assert "演示结论" in snap["answer"]
    # easy 任务：两步（1 工具调用 + 1 收尾），全部 fast 档
    assert snap["tier_usage"] == {"fast": 2}


def test_events_persisted_to_disk():
    r = client.post("/runs", json={"prompt": "自定义诊断任务", "provider": "fake",
                                   "deadline": 10})
    run_id = r.json()["run_id"]
    _wait_done(run_id)
    ev_file = store.root / run_id / "events.jsonl"
    assert ev_file.exists()
    kinds = [json.loads(l)["kind"] for l in
             ev_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert "run_start" in kinds and "run_end" in kinds
    assert "accepted" in kinds  # 提交事件也落盘
    assert (store.root / run_id / "snapshot.json").exists()


def test_sse_replay_after_done():
    run_id = client.post("/runs", json={"prompt": "SSE 回放测试",
                                        "provider": "fake",
                                        "deadline": 10}).json()["run_id"]
    _wait_done(run_id)
    with client.stream("GET", f"/runs/{run_id}/events") as resp:
        body = "".join(resp.iter_text())
    assert "run_start" in body
    assert '"kind": "done"' in body or '"kind":"done"' in body
    assert "run_end" in body


def test_list_runs_includes_finished():
    run_id = client.post("/runs", json={"prompt": "列表测试", "provider": "fake",
                                        "deadline": 10}).json()["run_id"]
    _wait_done(run_id)
    runs = client.get("/runs").json()
    assert any(r["run_id"] == run_id and r["status"] == "done" for r in runs)


def test_unknown_task_404():
    r = client.post("/runs", json={"task_id": "NOPE-999", "provider": "fake"})
    assert r.status_code == 404


def test_task_library_endpoint():
    """任务库：公共基准优先（HB 在最前），内置 CV 任务已移出任务库。"""
    tasks = client.get("/tasks").json()
    assert all(t.get("prompt") for t in tasks)  # 带题目原文（前端选中即显示）
    assert tasks[0]["id"].startswith("HB/")     # 中文基准排最前
    ids = [t["id"] for t in tasks]
    hb_end = max(i for i, x in enumerate(ids) if x.startswith("HB/"))
    bfcl_start = min(i for i, x in enumerate(ids) if x.startswith("BFCL/"))
    assert hb_end < bfcl_start                  # HB 整体在 BFCL 之前
    assert not any(x.startswith("T") for x in ids)  # CV 任务已移出


def test_tasks_include_bfcl_episodes():
    """/tasks 应合并公共基准（BFCL v4 多轮）精选 episode。"""
    tasks = client.get("/tasks").json()
    bfcl = [t for t in tasks if t["id"].startswith("BFCL/")]
    assert len(bfcl) == 10                     # 精选 10 题
    assert all(t["type"] == "BFCL" for t in bfcl)
    assert all(2 <= t["turns"] <= 5 for t in bfcl)
    assert all(t["prompt"] and t["classes"] for t in bfcl)


def test_bfcl_rejects_fake_provider():
    """BFCL 是脚本化真实基准，剧本 provider 撑不起多轮工具调用 → 422。"""
    tasks = client.get("/tasks").json()
    eid = next(t["id"] for t in tasks if t["id"].startswith("BFCL/"))
    r = client.post("/runs", json={"task_id": eid, "provider": "fake",
                                   "deadline": 30})
    assert r.status_code == 422
    assert "公共基准" in r.json()["detail"]


def test_tasks_include_hb_conversations():
    """/tasks 应合并 HammerBench 中文多轮精选 conversation。"""
    tasks = client.get("/tasks").json()
    hb = [t for t in tasks if t["id"].startswith("HB/")]
    assert len(hb) == 10
    assert all(t["type"] == "HB" for t in hb)
    assert all(2 <= t["turns"] <= 5 for t in hb)
    assert all(t["prompt"] for t in hb)        # 中文首轮原文


def test_hb_rejects_fake_provider():
    """HammerBench 同 BFCL：剧本 provider → 422。"""
    tasks = client.get("/tasks").json()
    eid = next(t["id"] for t in tasks if t["id"].startswith("HB/"))
    r = client.post("/runs", json={"task_id": eid, "provider": "fake",
                                   "deadline": 30})
    assert r.status_code == 422
    assert "公共基准" in r.json()["detail"]


def test_hb_grade_turn_rules():
    """判分规则：精确匹配 / 幻觉参数 / 缺参数 / 无调用 / 错函数名。"""
    from eval.hammerbench_adapter import grade_turn
    exp = {"name": "A.b", "arguments": {"city": "广州", "n": 3}}
    assert grade_turn([("A.b", {"city": "广州", "n": 3})], exp) == (True, None)
    assert grade_turn([("A.b", {"city": "广州", "n": "3"})], exp)[0]  # 数字归一化
    ok, d = grade_turn([("A.b", {"city": "广州", "n": 3, "x": 1})], exp)
    assert not ok and "幻觉" in d
    ok, d = grade_turn([("A.b", {"city": "广州"})], exp)
    assert not ok and "缺参数" in d
    ok, d = grade_turn([], exp)
    assert not ok and "未发出" in d
    ok, d = grade_turn([("C.d", {})], exp)
    assert not ok and "未调用期望函数" in d


def test_run_with_rescue():
    """失败升级救援：首轮失败且预算够 → 强档重跑；成功/预算不足 → 不触发。"""
    from service.app import _run_with_rescue

    class FakeTrace:
        def __init__(self):
            self.events = []

        def emit(self, kind, **kw):
            self.events.append((kind, kw))

    class FakeTriple:
        def __init__(self, fail_first):
            self.fail_first = fail_first
            self.strong = type("S", (), {"model": "strong-x"})()
            self.scheduler = None

    def run_once(llm, sched, dl):
        if llm is triple and triple.fail_first:
            return {"success": False, "total_time": 6.0,
                    "stop_reason": "call_mismatch", "first_bad_diff": "no call"}
        return {"success": True, "total_time": 5.0, "stop_reason": "done"}

    # ① 首轮失败 + 剩余预算充足 → escalate 并救援成功
    triple = FakeTriple(fail_first=True)
    tr = FakeTrace()
    rec, rescued = _run_with_rescue(run_once, triple, 30.0, tr)
    assert rescued and rec["success"] and rec["rescue"]["rescued"]
    assert tr.events[0][0] == "escalate"
    assert tr.events[0][1]["remaining"] == 24.0

    # ② 首轮就成功 → 不触发救援
    triple = FakeTriple(fail_first=False)
    rec2, rescued2 = _run_with_rescue(run_once, triple, 30.0, FakeTrace())
    assert not rescued2 and rec2["success"] and "rescue" not in rec2

    # ③ 失败但剩余预算不足（<3s 下限）→ 不触发
    triple = FakeTriple(fail_first=True)
    rec3, rescued3 = _run_with_rescue(run_once, triple, 6.5, FakeTrace())
    assert not rescued3 and "rescue" not in rec3


def test_token_stream_reaches_trace():
    """流式直播链路：FakeLLM on_token → Trace.emit_token → token 事件落盘。"""
    from src.instrument import Trace
    from service.app import FakeLLM
    events = []
    tr = Trace(run_id="tok_test", sink=lambda e: events.append(e))
    llm = FakeLLM("qwen2.5:7b", on_token=lambda s: tr.emit_token(s))
    llm.chat([{"role": "user", "content": "随便跑一个"}])
    kinds = [e["kind"] for e in events]
    assert "token" in kinds
    joined = "".join(e["text"] for e in events if e["kind"] == "token")
    assert "演示结论" in joined          # 分片拼接后内容完整


def test_hb_run_episode_emits_tokens():
    """公共基准多轮路径的 token 直播：run_episode 期间 token 事件发射。"""
    from src.instrument import Trace
    from service.app import FakeLLM
    from eval.hammerbench_run import run_episode
    ep = {"id": "t0", "datatype": "Based",
          "tools": [{"name": "list_segments", "description": "查询",
                     "parameters": {"type": "object", "properties": {}}}],
          "turns": [{"user": "查一下当前分段",
                     "expected": {"name": "list_segments", "arguments": {}}}]}
    events = []
    tr = Trace(run_id="hb_tok", sink=lambda e: events.append(e))
    llm = FakeLLM("qwen2.5:7b", on_token=lambda s: tr.emit_token(s))
    llm.strong_model_name = "strong-x"
    rec = run_episode(ep, llm, scheduler=None, deadline=None, trace=tr)
    kinds = [e["kind"] for e in events]
    assert "token" in kinds and "turn_result" in kinds
    assert rec["success"]                # FakeLLM 首轮调 list_segments = 期望调用
    joined = "".join(e["text"] for e in events if e["kind"] == "token")
    assert "先查一下" in joined


def test_interrupted_snapshot_marking():
    """服务重启后：有事件无快照的目录应标记 interrupted（step② 的钩子）。"""
    from service.app import SessionStore
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp(prefix="dert_intr_"))
    s = SessionStore(tmp)
    rid = "deadbeef"
    s.append(rid, {"kind": "run_start", "ts": 0.0})
    snap = s.snapshot(rid)
    assert snap["status"] == "interrupted"
    assert s.list_ids() == [rid]


# ---------------- v3emp：影子流量难度史（服务化⑨） ----------------

def test_scheduler_v3eemp_routes_by_history():
    """难度史驱动分流：hard+预算足→强 / hard+中预算→中 / hard+预算见底→快 / 未知→快。"""
    from src.scheduler import DeadlineSchedulerV3EEmp, MID_GATE_V3E
    s_hard = DeadlineSchedulerV3EEmp("fast", "strong", "mid",
                                     history={"T-hard": True})
    s_hard.set_task_id("T-hard")
    assert s_hard.route(30.0) == "strong"          # 预算充足 → 强档
    s_mid = DeadlineSchedulerV3EEmp("fast", "strong", "mid",
                                    history={"T-hard": True})
    s_mid.set_task_id("T-hard")
    assert s_mid.route(MID_GATE_V3E + 0.1) == "mid"  # 中档阶梯（route 有 _tier 缓存，须新实例）
    s_low = DeadlineSchedulerV3EEmp("fast", "strong", "mid",
                                    history={"T-hard": True})
    s_low.set_task_id("T-hard")
    assert s_low.route(0.5) == "fast"              # 预算见底 → 快档兜底
    s_new = DeadlineSchedulerV3EEmp("fast", "strong", "mid")
    s_new.set_task_id("T-unknown")
    assert s_new.route(30.0) == "fast"             # 冷启动未知 → 快档


def test_shadow_history_record_rules():
    """record_shadow 口径：失败→wrong / 纯快档成功→ok / 救援成功→wrong / 混合档成功不记 / 快档未参与不记。"""
    from service.app import record_shadow, load_history, hard_set, HISTORY_PATH
    if HISTORY_PATH.exists():
        HISTORY_PATH.unlink()                      # 清场
    assert record_shadow("", False, True, False, "m") is None    # 无 task_id
    r1 = record_shadow("X/1", finished=False, had_fast=True,
                       had_strong=True, fast_model="qwen")
    assert r1["verdict"] == "wrong"
    r2 = record_shadow("X/1", finished=True, had_fast=True,
                       had_strong=False, fast_model="qwen")
    assert r2["verdict"] == "ok" and r2["ok"] == 1
    r3 = record_shadow("X/2", finished=True, had_fast=True,
                       had_strong=True, fast_model="qwen",
                       fast_first_attempt_failed=True)
    assert r3["verdict"] == "wrong"                # 救援成功也记 wrong（快档首试即败）
    r4 = record_shadow("X/3", finished=True, had_fast=True,
                       had_strong=True, fast_model="qwen")
    assert r4 is None                              # 混合档成功归因不清
    r5 = record_shadow("X/4", finished=False, had_fast=False,
                       had_strong=True, fast_model="qwen")
    assert r5 is None                              # 快档没参与
    assert hard_set(load_history()) == {"X/1": True, "X/2": True}
    HISTORY_PATH.unlink()                          # 清场


def test_fake_run_records_shadow_history():
    """fake 全链路含影子：纯快档成功 → 快照带 shadow=ok，难度史落盘。"""
    from service.app import load_history
    run_id = client.post("/runs", json={"task_id": "T1-1", "provider": "fake",
                                        "deadline": 10}).json()["run_id"]
    snap = _wait_done(run_id)
    assert snap["status"] == "done"
    assert snap["tier_usage"] == {"fast": 2}       # easy 任务纯快档
    assert snap["shadow"]["verdict"] == "ok"
    assert load_history()["T1-1"]["ok"] >= 1


# ---------------- 自动档（v4 交互：判卷驱动的级联） ----------------

def test_auto_mode_fast_pass(monkeypatch):
    """自动档：判卷 pass → 直接交付，纯快档，影子记 ok。"""
    import service.app as sa
    monkeypatch.setattr(sa, "judge_answer",
                        lambda q, a: {"verdict": "pass", "reason": "ok",
                                      "judge_s": 0.4})
    run_id = client.post("/runs", json={"prompt": "自动档测试任务甲",
                                        "provider": "fake"}).json()["run_id"]
    snap = _wait_done(run_id)
    assert snap["mode"] == "auto" and snap["escalated"] is False
    assert snap["tier_usage"] == {"fast": 2}
    assert snap["judge"]["verdict"] == "pass"
    assert snap["shadow"]["verdict"] == "ok"
    assert snap["deadline"] is None


def test_auto_mode_escalates(monkeypatch):
    """自动档：判卷 fail → 升级强档重答，影子记 wrong（首试即败）。"""
    import service.app as sa
    monkeypatch.setattr(sa, "judge_answer",
                        lambda q, a: {"verdict": "fail", "reason": "未完成",
                                      "judge_s": 0.4})
    run_id = client.post("/runs", json={"prompt": "自动档测试任务乙",
                                        "provider": "fake"}).json()["run_id"]
    snap = _wait_done(run_id)
    assert snap["mode"] == "auto" and snap["escalated"] is True
    assert snap["tier_usage"].get("strong", 0) > 0
    assert snap["shadow"]["verdict"] == "wrong"


def test_auto_mode_hard_history_skips_fast():
    """熟客效应：影子史说 hard → 直接强档，一次快档都不试。"""
    import hashlib
    import service.app as sa
    prompt = "自动档测试任务丙"
    tid = "prompt:" + hashlib.md5(prompt.encode()).hexdigest()[:8]
    sa.HISTORY_PATH.write_text(
        json.dumps({tid: {"wrong": 1, "ok": 0, "model": "qwen2.5:7b"}}),
        encoding="utf-8")
    try:
        run_id = client.post("/runs", json={"prompt": prompt,
                                            "provider": "fake"}).json()["run_id"]
        snap = _wait_done(run_id)
        assert snap["started_hard"] is True and snap["escalated"] is False
        assert "fast" not in snap["tier_usage"]
    finally:
        sa.HISTORY_PATH.unlink(missing_ok=True)


def test_auto_mode_easy_history_stays_fast(monkeypatch):
    """边界回归：影子史只有 ok（答对过）→ 不是 hard，仍快档先试+判卷。

    2026-10-26 抓到的 bug：`task_id in hard_set(...)` 做的是键存在性
    判断，ok-only 的老熟人任务也被误判 hard 直上强档——重复任务
    反而白花强档钱，与「越用越省」直接相悖。
    """
    import hashlib
    import service.app as sa
    monkeypatch.setattr(sa, "judge_answer",
                        lambda q, a: {"verdict": "pass", "reason": "ok",
                                      "judge_s": 0.4})
    prompt = "自动档测试任务丁"
    tid = "prompt:" + hashlib.md5(prompt.encode()).hexdigest()[:8]
    sa.HISTORY_PATH.write_text(
        json.dumps({tid: {"wrong": 0, "ok": 3, "model": "qwen2.5:7b"}}),
        encoding="utf-8")
    try:
        run_id = client.post("/runs", json={"prompt": prompt,
                                            "provider": "fake"}).json()["run_id"]
        snap = _wait_done(run_id)
        assert snap["started_hard"] is False and snap["escalated"] is False
        assert snap["tier_usage"].get("fast", 0) >= 1
        assert "strong" not in snap["tier_usage"]
        kinds = [e["kind"] for e in store.read_events(run_id)]
        assert "route_hard" not in kinds
    finally:
        sa.HISTORY_PATH.unlink(missing_ok=True)


def test_auto_mode_judge_unavailable(monkeypatch):
    """Ollama 挂了 → 判卷降级：按原样交付快档答案，服务不阻塞。"""
    import service.app as sa
    monkeypatch.setattr(sa, "judge_answer",
                        lambda q, a: {"verdict": "unavailable",
                                      "reason": "判卷不可用：ConnectionError",
                                      "judge_s": None})
    run_id = client.post("/runs", json={"prompt": "自动档测试任务丁",
                                        "provider": "fake"}).json()["run_id"]
    snap = _wait_done(run_id)
    assert snap["judge"]["verdict"] == "unavailable"
    assert snap["escalated"] is False and snap["status"] == "done"


def test_agent_run_system_prompt_override():
    """system_prompt 参数：自动档传通用提示词；默认仍为 CV 域（向后兼容）。"""
    from src.agent import run as agent_run, ModelResponse
    captured = {}

    class StubLLM:
        model = "stub"

        def chat(self, messages, **kw):
            captured["messages"] = messages
            return ModelResponse(content="FINAL: done", tool_calls=[],
                                 model="stub", ttft=0.0, total=0.0,
                                 tokens_in=1, tokens_out=1)

    from src.tools import default_tools
    tools = default_tools()
    agent_run("任务", llm=StubLLM(), tools=tools,
              system_prompt="自定义系统提示词XYZ")
    assert captured["messages"][0]["content"] == "自定义系统提示词XYZ"
    agent_run("任务", llm=StubLLM(), tools=tools)          # 不传 = 历史行为
    assert "实时系统诊断" in captured["messages"][0]["content"]


def test_bench_task_overrides_auto_dispatch(monkeypatch):
    """分发优先级：基准任务即使勾了自动档也走官方管道（不被判卷级联劫持）。"""
    import service.app as sa
    called = []
    monkeypatch.setattr(sa, "_execute_hb", lambda rid, meta: called.append("hb"))
    monkeypatch.setattr(sa, "_execute_bfcl", lambda rid, meta: called.append("bfcl"))
    monkeypatch.setattr(sa, "_execute_auto", lambda rid, meta: called.append("auto"))
    sa.execute_run("rid1", {"auto": True, "hb": "X"})
    sa.execute_run("rid2", {"auto": True, "bfcl": "Y"})
    sa.execute_run("rid3", {"auto": True})
    assert called == ["hb", "bfcl", "auto"]


def test_run_with_rescue_unlimited_budget():
    """deadline=None（自动档/不限预算）→ 升级门恒过：快档失败必须升级强档。"""
    from service.app import _run_with_rescue

    class FakeTrace:
        def emit(self, *a, **k):
            pass

    class FakeStrong:
        model = "strong-x"
        strong_model_name = None

    class FakeTriple:
        fast = "qwen"
        strong = FakeStrong()

    triple = FakeTriple()

    def run_once(llm, sched, dl):
        if llm is triple:   # 首轮（快档）失败
            return {"success": False, "total_time": 4.0,
                    "stop_reason": "call_mismatch"}
        return {"success": True, "total_time": 3.0, "stop_reason": "final"}

    rec, rescued = _run_with_rescue(run_once, triple, None, FakeTrace())
    assert rescued and rec["success"] and rec["rescue"]["attempted"]
