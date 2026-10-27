# -*- coding: utf-8 -*-
"""服务化②③④测试：断点续跑 / 审批门 / MCP 工具出口。

全部离线（fake provider / 内存客户端），不触真实 API。
"""
import asyncio
import time

from fastapi.testclient import TestClient

from src.agent import run as agent_run, SYSTEM_PROMPT
from src.tools import default_tools

from service.app import app, store

client = TestClient(app)


# ---------- ② checkpoint / resume（agent 层）----------

class FinalOnlyLLM:
    """总是直接 FINAL 的假 LLM（续跑测试用）。"""
    model = "fake-final"
    base_url = "fake://test"

    def chat(self, messages, model=None, tools=None, **kw):
        from src.agent import ModelResponse
        return ModelResponse(content="FINAL: 续跑结论 ok", tool_calls=[],
                             model=self.model, ttft=0.01, total=0.02,
                             tokens_in=5, tokens_out=3)


def test_agent_resume_continues_from_checkpoint():
    """断点 = 消息历史 + 已耗时长：续跑后预算继续扣、步骤接续。"""
    # 手工构造 step1 完成后的消息栈（工具已调用、结果已入栈）
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "诊断任务"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "list_segments", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "{\"segments\": []}"},
    ]
    ck = {"messages": messages, "elapsed": 2.5, "step": 1}
    result = agent_run("诊断任务", llm=FinalOnlyLLM(), tools=default_tools(),
                       deadline=10, resume=ck)
    assert result.stop_reason == "final"
    assert result.steps == 2          # 接续 step1 → step2 收尾
    assert result.total_time < 10     # 预算按已耗 2.5s 继续扣，未爆


def test_agent_on_step_captures_checkpoints():
    """on_step 每步回调：消息栈单调增长，可存为 checkpoint。"""
    class ToolThenFinalLLM(FinalOnlyLLM):
        calls = 0

        def chat(self, messages, model=None, tools=None, **kw):
            self.calls += 1
            if self.calls == 1 and tools:
                from src.agent import ModelResponse
                return ModelResponse(content=None, tool_calls=[
                    {"id": "c1", "name": "list_segments", "arguments": {}}],
                    model=self.model, ttft=0.01, total=0.02,
                    tokens_in=5, tokens_out=3)
            return super().chat(messages, model=model, tools=tools, **kw)

    seen = []
    llm = ToolThenFinalLLM()
    result = agent_run("任意任务", llm=llm, tools=default_tools(),
                       deadline=20, on_step=seen.append)
    # 只有「执行了工具的步」产生断点（收尾步即运行结束，无需断点）——
    # 本例：step1 工具调用（有断点），step2 FINAL（无断点）
    assert len(seen) == 1
    assert seen[0]["step"] == 1
    assert len(seen[0]["messages"]) >= 4  # system+user+assistant(tool_call)+tool
    assert result.stop_reason == "final"


# ---------- ③审批门 ----------

def test_risk_level_marking():
    reg = default_tools()
    assert reg.risk_level("apply_config_change") == "high"
    assert reg.risk_level("latency_stats") == "low"
    assert reg.risk_level("不存在的工具") == "low"


def _wait_done(run_id, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        snap = store.snapshot(run_id)
        if snap and snap.get("status") in ("done", "error"):
            return snap
        time.sleep(0.1)
    raise TimeoutError(run_id)


def _submit(prompt, **kw):
    r = client.post("/runs", json={"prompt": prompt, "provider": "fake",
                                   "deadline": 20, **kw})
    assert r.status_code == 200
    return r.json()["run_id"]


def test_approval_timeout_denies_high_risk_tool():
    """审批超时（conftest 里 DERT_APPROVAL_TIMEOUT=2s）→ 自动拒绝，agent 继续收尾。"""
    run_id = _submit("需要改配置：把 fps 调成 30", require_approval=True)
    snap = _wait_done(run_id)
    assert snap["status"] == "done"
    kinds = [e["kind"] for e in store.read_events(run_id)]
    assert "approval_required" in kinds
    results = [e for e in store.read_events(run_id)
               if e["kind"] == "approval_result"]
    assert results and results[0]["approved"] is False
    # 拒绝后 run 正常收尾（拒绝信息作为 tool 输出喂回模型）


def test_approval_accept_executes_tool():
    run_id = _submit("需要改配置：把 fps 调成 30", require_approval=True)
    # 等待审批请求出现后批准
    t0 = time.time()
    while time.time() - t0 < 10:
        evs = [e for e in store.read_events(run_id)
               if e["kind"] == "approval_required"]
        if evs:
            break
        time.sleep(0.1)
    r = client.post(f"/runs/{run_id}/approve", json={"approved": True})
    assert r.status_code == 200
    _wait_done(run_id)
    tool_evs = [e for e in store.read_events(run_id)
                if e["kind"] == "tool_call" and e.get("tool") == "apply_config_change"]
    assert tool_evs and tool_evs[0]["status"] == "ok"
    results = [e for e in store.read_events(run_id)
               if e["kind"] == "approval_result"]
    assert results[0]["approved"] is True


def test_low_risk_tools_bypass_approval():
    """不开 require_approval 时，高危工具也不拦（默认行为不变）。"""
    run_id = _submit("需要改配置：把 fps 调成 30", require_approval=False)
    _wait_done(run_id)
    kinds = [e["kind"] for e in store.read_events(run_id)]
    assert "approval_required" not in kinds


# ---------- ②服务级 resume ----------

def test_service_resume_from_checkpoint():
    """模拟中断会话：手工写 request/events/checkpoint → POST /resume 续跑。"""
    import uuid
    rid = uuid.uuid4().hex[:12]
    meta = {"prompt": "被中断的诊断任务", "deadline": 20, "provider": "fake",
            "require_approval": False}
    store.save_request(rid, meta)
    store.append(rid, {"kind": "run_start", "task": meta["prompt"]})
    ck = {"step": 1, "elapsed": 1.5, "messages": [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "被中断的诊断任务"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "list_segments", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
    ]}
    store.save_checkpoint(rid, ck)  # 无 snapshot = interrupted
    assert store.snapshot(rid)["status"] == "interrupted"
    assert store.snapshot(rid)["resumable"] is True

    r = client.post(f"/runs/{rid}/resume")
    assert r.status_code == 200
    snap = _wait_done(rid)
    assert snap["status"] == "done"
    kinds = [e["kind"] for e in store.read_events(rid)]
    assert "resume_accepted" in kinds and "resumed" in kinds


# ---------- ④MCP 工具出口 ----------

def test_mcp_server_lists_all_tools():
    from service.mcp_server import mcp
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert {"latency_stats", "detection_stats", "list_segments",
            "segment_latency", "render_report", "gpu_stats", "alarm_log",
            "apply_config_change", "get_config"} <= names
    high = [t for t in tools if t.name == "apply_config_change"]
    assert "高危" in high[0].description


def test_mcp_tool_roundtrip():
    """MCP 工具实际执行一次（走 MCPServer.call_tool 全链路）。"""
    from service.mcp_server import mcp
    result = asyncio.run(mcp.call_tool("latency_stats", {"n_frames": 100}))
    assert result.is_error is False
    text = result.content[0].text
    assert "p50_ms" in text
