"""DeRT LLM 客户端：OpenAI 兼容 + 流式 TTFT 测量 + 响应缓存。

实现 agent.ModelResponse 鸭子类型协议，可直接注入 loop：
    from src.llm import OpenAICompatLLM
    from src.agent import run
    result = run(task, llm=OpenAICompatLLM(), tools=default_tools(), deadline=10)

后端无关：任何 OpenAI 兼容端点（Ollama / DeepSeek / SiliconFlow …）
通过 base_url 切换。配置走环境变量 + 模块内默认值（项目约定：不设 config.py）。

环境变量：
    DERT_BASE_URL   默认 http://127.0.0.1:11434/v1（Ollama；勿改回 localhost，见下方常量注释）
    DERT_API_KEY    默认 "ollama"（本地端点忽略密钥）
    DERT_MODEL      默认 qwen2.5:7b（未显式传 model 时的缺省档）
    DERT_CACHE_DIR  默认 eval/cache；设为空字符串禁用缓存
    DERT_TIMEOUT    默认 120 秒

缓存口径（对齐 benchmarks/tasks-design.md）：
    键 = sha256(model + messages 规范化 JSON + tools 规范化 JSON)。
    开发迭代一律命中缓存（零成本）；trace 中 cache_hit=True 可观测。
    注意：真实 TTFT/延迟分布统计须在禁用缓存下跑。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from openai import OpenAI

from .agent import ModelResponse

# 硬约束：必须用 127.0.0.1，不能写 localhost。
# Windows 上 localhost 会先解析成 IPv6 ::1，而 Ollama 只监听 IPv4 127.0.0.1，
# IPv6 连接超时后回退 IPv4 会**固定烧掉约 2 秒**——2026-09-28 全网格 2520 次
# 运行的 qwen TTFT 因此从 0.148s 涨到 2.222s（min 也有 2.087s，是地板被抬高
# 而非抖动），直接导致 2s/4s 预算档按时率归零、"全档 ≥ 外包络"结论失效。
# 实测（同 payload 同机器）：localhost→2.070s，127.0.0.1→0.027s。
# 注：远端域名（api.deepseek.com）无 AAAA 记录，不会触发该问题。
DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_MODEL = "qwen2.5:7b"
DEFAULT_CACHE_DIR = "eval/cache"
DEFAULT_TIMEOUT = 120.0

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"

# ---- 阿里云百炼（第二强档后端，2026-10-22 接入）----
# 密钥按用户决定存项目内文件（本项目不做开源，见 AGENTS.md §6 当日决策）；
# 优先读环境变量 BAILIAN_API_KEY，便于未来需要时收回密钥不出仓库。
BAILIAN_BASE_URL = "https://llm-voh6f98fi2uy54tl.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
BAILIAN_KEY_FILE = Path(__file__).resolve().parent.parent / "eval" / "aliyun_bailian.key"
BAILIAN_MODEL = "deepseek-v4.1-flash"  # 端点上经探测可用且 3 道难题全对的强档模型


def bailian_llm(model: str | None = None, **kwargs) -> "OpenAICompatLLM":
    """强档（第二后端）：阿里云百炼 OpenAI 兼容端点。

    与 deepseek_llm 的差别只在端点/密钥/模型名，协议完全相同；
    2026-10-22 起 DeepSeek 官方 API 余额耗尽，密集扫描改走本后端
    （deepseek-v4.1-flash = deepseek-chat 的后继代次，家族连续）。
    """
    import os
    key = os.environ.get("BAILIAN_API_KEY")
    if not key:
        if not BAILIAN_KEY_FILE.exists():
            raise RuntimeError("未找到百炼密钥：设 BAILIAN_API_KEY 或创建 "
                               f"{BAILIAN_KEY_FILE}")
        key = BAILIAN_KEY_FILE.read_text(encoding="utf-8").strip()
    return OpenAICompatLLM(base_url=BAILIAN_BASE_URL, api_key=key,
                           model=model or BAILIAN_MODEL, **kwargs)


def deepseek_llm(api_key: str | None = None, model: str | None = None,
                 **kwargs) -> "OpenAICompatLLM":
    """强档：DeepSeek。密钥从环境变量 DEEPSEEK_API_KEY 读取（用户级，不落代码）。"""
    import os
    key = api_key or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("未设置 DEEPSEEK_API_KEY（用户环境变量），strong 档不可用")
    return OpenAICompatLLM(base_url=DEEPSEEK_BASE_URL, api_key=key,
                           model=model or DEEPSEEK_MODEL, **kwargs)


class OpenAICompatLLM:
    """OpenAI 兼容客户端，实现 chat() -> ModelResponse 协议。"""

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, cache_dir: str | None = None,
                 timeout: float | None = None, on_token=None):
        self.base_url = base_url or os.environ.get("DERT_BASE_URL", DEFAULT_BASE_URL)
        self.api_key = api_key or os.environ.get("DERT_API_KEY", "ollama")
        self.model = model or os.environ.get("DERT_MODEL", DEFAULT_MODEL)
        if cache_dir is None:
            cache_dir = os.environ.get("DERT_CACHE_DIR", DEFAULT_CACHE_DIR)
        self.cache_dir = cache_dir  # 空字符串 = 禁用缓存
        # 实时展示用：流式 token 回调（可选；不传则零开销，行为完全不变）
        self.on_token = on_token
        self.timeout = float(timeout or os.environ.get("DERT_TIMEOUT", DEFAULT_TIMEOUT))
        self._client = OpenAI(base_url=self.base_url, api_key=self.api_key,
                              timeout=self.timeout)

    # ---- 缓存 ----

    def _cache_key(self, model: str, messages: list, tools: list | None,
                   max_tokens: int | None = None) -> str:
        payload = json.dumps(
            {"model": model, "messages": messages, "tools": tools or [],
             "max_tokens": max_tokens},
            ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _cache_path(self, key: str) -> Path | None:
        if not self.cache_dir:
            return None
        return Path(self.cache_dir) / f"{key}.json"

    def _load_cache(self, key: str) -> ModelResponse | None:
        path = self._cache_path(key)
        if path is None or not path.exists():
            return None
        t0 = time.perf_counter()
        data = json.loads(path.read_text(encoding="utf-8"))
        dt = time.perf_counter() - t0
        return ModelResponse(
            content=data["content"], tool_calls=data["tool_calls"],
            model=data["model"], ttft=dt, total=dt,
            tokens_in=data["tokens_in"], tokens_out=data["tokens_out"],
            cache_hit=True)

    def _save_cache(self, key: str, resp: ModelResponse) -> None:
        path = self._cache_path(key)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "model": resp.model, "content": resp.content,
            "tool_calls": resp.tool_calls,
            "tokens_in": resp.tokens_in, "tokens_out": resp.tokens_out,
        }, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- 协议入口 ----

    def chat(self, messages: list, model: str | None = None,
             tools: list | None = None, temperature: float = 0.0,
             max_tokens: int | None = None) -> ModelResponse:
        model = model or self.model
        key = self._cache_key(model, messages, tools, max_tokens)

        cached = self._load_cache(key)
        if cached is not None:
            return cached

        resp = self._stream_call(model, messages, tools, temperature, max_tokens)
        self._save_cache(key, resp)
        return resp

    # ---- 流式调用与 TTFT ----

    def _stream_call(self, model: str, messages: list,
                     tools: list | None, temperature: float,
                     max_tokens: int | None = None) -> ModelResponse:
        kwargs = dict(model=model, messages=messages, stream=True,
                      temperature=temperature,
                      stream_options={"include_usage": True})
        if tools:
            kwargs["tools"] = tools
        if max_tokens:
            kwargs["max_tokens"] = max_tokens

        t0 = time.perf_counter()
        ttft: float | None = None
        content_parts: list[str] = []
        # 按 index 累积 tool_call 增量：id/name 只来一次，arguments 分片拼接
        tc_parts: dict[int, dict] = {}
        usage_in = usage_out = 0

        stream = self._client.chat.completions.create(**kwargs)
        for chunk in stream:
            if getattr(chunk, "usage", None):
                usage_in = chunk.usage.prompt_tokens or 0
                usage_out = chunk.usage.completion_tokens or 0
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            piece = delta.content
            if piece:
                if ttft is None:
                    ttft = time.perf_counter() - t0
                content_parts.append(piece)
                if self.on_token is not None:
                    try:
                        self.on_token(piece)
                    except Exception:
                        pass  # 展示层故障不得影响调用本身
            for dtc in delta.tool_calls or []:
                if ttft is None:
                    ttft = time.perf_counter() - t0
                slot = tc_parts.setdefault(dtc.index, {"id": "", "name": "", "args": ""})
                if dtc.id:
                    slot["id"] = dtc.id
                if dtc.function and dtc.function.name:
                    slot["name"] = dtc.function.name
                if dtc.function and dtc.function.arguments:
                    slot["args"] += dtc.function.arguments

        total = time.perf_counter() - t0
        tool_calls = []
        for idx in sorted(tc_parts):
            slot = tc_parts[idx]
            try:
                args = json.loads(slot["args"]) if slot["args"] else {}
            except json.JSONDecodeError:
                args = {"_raw": slot["args"]}  # 模型输出不合法 JSON 时保底不炸 loop
            tool_calls.append({"id": slot["id"] or f"call_{idx}",
                               "name": slot["name"], "arguments": args})

        return ModelResponse(
            content="".join(content_parts) or None,
            tool_calls=tool_calls, model=model,
            ttft=ttft if ttft is not None else total, total=total,
            tokens_in=usage_in, tokens_out=usage_out)
