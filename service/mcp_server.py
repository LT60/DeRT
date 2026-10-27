# -*- coding: utf-8 -*-
"""DeRT 工具的 MCP server（服务化④）：把 src/tools.py 的诊断工具暴露为
标准 Model Context Protocol 工具，任何 MCP 客户端（Claude Desktop、
WorkBuddy、其它 agent 框架）都能直接挂载。

与 service/app.py 的关系：同一批工具（src/tools.py default_tools）的两个出口——
  HTTP 服务出口（app.py）+ MCP 标准协议出口（本文件）。
apply_config_change 同样暴露且标注高危：MCP 侧无审批流（客户端负责）。

启动（stdio 传输）：
  <venv-python> service/mcp_server.py
MCP 配置示例（客户端 mcpServers）：
  {"command": "<venv-python>", "args": ["service/mcp_server.py"], "cwd": "<项目根>"}
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.mcpserver import MCPServer

from src.tools import default_tools

mcp = MCPServer("dert-tools")

_TOOLS = default_tools()


@mcp.tool(description="查询实时检测系统当前配置（模式、分辨率、检测器）")
def get_config() -> dict:
    return _TOOLS._tools["get_config"].func()


@mcp.tool(description="列出系统中所有已录制片段（id、时间范围、帧数）")
def list_segments() -> dict:
    return _TOOLS._tools["list_segments"].func()


@mcp.tool(description="统计最近 N 帧的处理延迟分布（P50/P95/P99，已排除采集等待）")
def latency_stats(n_frames: int = 200) -> dict:
    return _TOOLS._tools["latency_stats"].func(n_frames=n_frames)


@mcp.tool(description="统计最近 N 帧的检测质量（检出率、定位抖动）")
def detection_stats(n_frames: int = 200) -> dict:
    return _TOOLS._tools["detection_stats"].func(n_frames=n_frames)


@mcp.tool(description="查询指定片段的延迟分布（P50/P95/P99）；segment_id 来自 list_segments")
def segment_latency(segment_id: str) -> dict:
    return _TOOLS._tools["segment_latency"].func(segment_id=segment_id)


@mcp.tool(description="渲染指定片段的检测可视化报告（较慢，仅需要展示时调用）")
def render_report(segment: str = "latest") -> dict:
    return _TOOLS._tools["render_report"].func(segment=segment)


@mcp.tool(description="查询 GPU 资源状态（利用率、显存、温度、功耗）")
def gpu_stats() -> dict:
    return _TOOLS._tools["gpu_stats"].func()


@mcp.tool(description="查询报警日志（可按级别过滤：all/warn/error）")
def alarm_log(level: str = "all") -> dict:
    return _TOOLS._tools["alarm_log"].func(level=level)


@mcp.tool(description="【高危】修改实时检测系统运行配置（模拟写操作）；"
                     "调用前需人工确认——MCP 侧无审批流，由客户端负责")
def apply_config_change(key: str, value: str) -> dict:
    return _TOOLS._tools["apply_config_change"].func(key=key, value=value)


if __name__ == "__main__":
    mcp.run(transport="stdio")
