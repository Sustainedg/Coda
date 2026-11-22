"""面向任意 OpenAI 兼容对话端点的进程内智能体内核。

这个适配器家族覆盖了没有官方 CLI 的模型供应商：
Kimi、GLM、Qwen、OpenRouter、vLLM/Ollama…
所谓「内核」就是智能体循环本身：流式补全 + 沙箱化工具循环，
让一个普通的对话模型表现得像一个编码智能体。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator, Iterable

import httpx

from ..base import (
    AgentContext,
    AgentDefinition,
    AgentRun,
    Availability,
    Capabilities,
    ConfigField,
    FieldType,
    Invocation,
    Runtime,
)
from ..events import (
    AgentEvent,
    error,
    text_delta,
    thinking_delta,
    tool_call,
    tool_result,
    usage,
)
from ..registry import register_agent
from .harness_tools import ToolError, WorkspaceTools
from ...credentials import scrub
from ...egress import wrap_client

DEFAULT_MAX_STEPS = 12

#: 轨迹里允许原样保留的 HTTP 头（其余一律掩码为 ``***``）。
_TRACE_SAFE_HEADERS = {
    "content-type",
    "accept",
    "anthropic-version",
    "anthropic-beta",
    "user-agent",
    "http-referer",
    "x-title",
}


def _trace_headers(headers: dict[str, str]) -> dict[str, str]:
    """白名单脱敏：安全头原样保留，其余（含 Authorization / x-api-key）掩码。"""
    return {
        key: value if key.lower() in _TRACE_SAFE_HEADERS else "***"
        for key, value in headers.items()
    }


class _RawSse:
    """SSE 原始行旁路累积：只留头 128 KiB + 尾 64 KiB，避免长响应撑爆内存。"""

    _HEAD_LIMIT = 128 * 1024
    _TAIL_LIMIT = 64 * 1024

    def __init__(self) -> None:
        self._head: list[str] = []
        self._tail: list[str] = []
        self._head_bytes = 0
        self._tail_bytes = 0
        self.truncated = False

    def add(self, line: str) -> None:
        size = len(line.encode("utf-8")) + 1
        if not self.truncated and self._head_bytes + size <= self._HEAD_LIMIT:
            self._head.append(line)
            self._head_bytes += size
            return
        self.truncated = True
        self._tail.append(line)
        self._tail_bytes += size
        while self._tail and self._tail_bytes > self._TAIL_LIMIT:
            old = self._tail.pop(0)
            self._tail_bytes -= len(old.encode("utf-8")) + 1

    def lines(self) -> list[str]:
        return [*self._head, *self._tail]


class OpenAIHarnessBase(AgentDefinition):
    runtime = Runtime.API
    executable = None
    capabilities = Capabilities(
        streaming=True,
        sessions=False,  # 由数据库回放对话记录（transcript）
        native_system_prompt=True,
        tools=True,
        mcp=False,
        permission_modes=False,
        working_directory=True,
    )
    config_fields = (
        ConfigField(
            "max_steps",
            "单轮最大步数",
            FieldType.NUMBER,
            default=DEFAULT_MAX_STEPS,
            help="一轮用户消息内，最多允许多少次「调用工具 → 再推理」的往返。",
        ),
        ConfigField("allow_shell", "启用 shell 工具", FieldType.BOOLEAN, default=True),
        ConfigField(
            "shell_timeout",
            "shell 超时（秒）",
            FieldType.NUMBER,
            default=120,
            advanced=True,
        ),
        ConfigField(
            "temperature",
            "采样温度",
            FieldType.NUMBER,
            default=0.3,
            advanced=True,
        ),
        ConfigField("max_tokens", "最大输出 token 数", FieldType.NUMBER, advanced=True),
        ConfigField(
            "extra_headers",
            "额外 HTTP 请求头",
            FieldType.KEYVALUE,
            advanced=True,
            help="部分网关需要特定路由请求头时使用。",
        ),
        ConfigField(
            "extra_body",
            "额外请求体字段",
            FieldType.KEYVALUE,
            advanced=True,
        ),
    )

    def availability(self) -> Availability:
        return Availability(True, detail="进程内运行，无需本地可执行文件")

    def create_client(self) -> httpx.AsyncClient:
        """可被子类覆盖，便于测试或自定义传输层注入 httpx 客户端。"""
        return httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=20.0))

    # -- HTTP 请求 -----------------------------------------------------------

    def chat_completions_url(self, ctx: AgentContext) -> str:
        base = (ctx.gateway_url or self.default_gateway or "").rstrip("/")
        if not base:
            raise RuntimeError("进程内 API 型智能体必须填写网关地址")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def build_messages(self, ctx: AgentContext) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if ctx.system_prompt:
            messages.append({"role": "system", "content": ctx.system_prompt})
        for turn in ctx.history:
            role = turn.get("role")
            if role in ("user", "assistant") and turn.get("content"):
                messages.append({"role": role, "content": turn["content"]})
        messages.append({"role": "user", "content": ctx.prompt})
        return messages

    def request_payload(self, ctx: AgentContext, messages: list[dict[str, Any]]) -> dict[str, Any]:
        options = ctx.options
        payload: dict[str, Any] = {
            "model": ctx.model or self.default_model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "tools": self.tool_specs(ctx),
        }
        if options.get("temperature") is not None:
            payload["temperature"] = float(options["temperature"])
        if options.get("max_tokens"):
            payload["max_tokens"] = int(options["max_tokens"])
        for key, value in (options.get("extra_body") or {}).items():
            payload[key] = value
        return payload

    def tool_specs(self, ctx: AgentContext) -> list[dict[str, Any]] | None:
        from .harness_tools import TOOL_SPECS

        return list(TOOL_SPECS)

    def http_headers(self, ctx: AgentContext) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if ctx.api_key:
            headers["Authorization"] = f"Bearer {ctx.api_key}"
        for key, value in (ctx.option("extra_headers") or {}).items():
            headers[str(key)] = str(value)
        return headers

    # -- 智能体循环 ----------------------------------------------------------

    async def stream(self, ctx: AgentContext) -> AsyncIterator[AgentEvent]:
        messages = self.build_messages(ctx)
        workspace = WorkspaceTools(
            root=ctx.workspace_dir or ".",
            allow_shell=bool(ctx.option("allow_shell", True)),
            timeout=int(ctx.option("shell_timeout", 120) or 120),
            # 智能体凭据只在子 shell 里可见；模型只知道变量名（见 app.credentials）。
            env=ctx.credentials,
        )
        max_steps = int(ctx.option("max_steps", DEFAULT_MAX_STEPS) or DEFAULT_MAX_STEPS)
        totals: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        url = self.chat_completions_url(ctx)

        async with wrap_client(self.create_client(), extra_hosts=[url]) as client:
            for step in range(max_steps + 1):
                payload = self.request_payload(ctx, messages)
                if step == max_steps:
                    payload.pop("tools", None)
                collected: dict[str, Any] = {"content": "", "reasoning": "", "tool_calls": []}
                try:
                    async for event in self._stream_request(
                        client, url, payload, ctx, collected, totals, step=step
                    ):
                        yield event
                except httpx.HTTPStatusError as exc:
                    body = exc.response.text[:500]
                    yield error(f"网关返回 {exc.response.status_code}：{body}")
                    return
                except httpx.HTTPError as exc:
                    yield error(f"网关请求失败：{exc}")
                    return

                calls = collected["tool_calls"]
                if not calls:
                    break

                messages.append(
                    {
                        "role": "assistant",
                        "content": collected["content"] or None,
                        "tool_calls": [
                            {
                                "id": c["id"],
                                "type": "function",
                                "function": {"name": c["name"], "arguments": c["arguments"]},
                            }
                            for c in calls
                        ],
                    }
                )
                async for event in self._execute_tools(calls, workspace, messages, ctx):
                    yield event

        if totals["prompt_tokens"] or totals["completion_tokens"]:
            yield usage(**totals)

    async def _stream_request(
        self,
        client: httpx.AsyncClient,
        url: str,
        payload: dict[str, Any],
        ctx: AgentContext,
        collected: dict[str, Any],
        totals: dict[str, int],
        *,
        step: int,
    ) -> AsyncIterator[AgentEvent]:
        trace = ctx.trace
        headers = self.http_headers(ctx)
        started = time.monotonic()
        raw = _RawSse()
        status_code: int | None = None
        finish_reason: str | None = None
        chunk_count = 0
        step_usage: dict[str, int] = {}

        if trace is not None:
            await trace.request(
                {
                    "transport": "http",
                    "method": "POST",
                    "url": url,
                    "model": payload.get("model"),
                    "headers": _trace_headers(headers),
                    "body": payload,
                    "step": step,
                },
                immediate=True,
            )

        def payload_for_response(error_text: str | None = None) -> dict[str, Any]:
            data: dict[str, Any] = {
                "transport": "http",
                "status_code": status_code,
                "finish_reason": finish_reason,
                "usage": step_usage or None,
                "raw_sse": raw.lines(),
                "chunk_count": chunk_count,
                "step": step,
            }
            if error_text:
                data["error"] = error_text
            return data

        try:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                status_code = response.status_code
                if response.status_code >= 400:
                    await response.aread()
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    raw.add(line)
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    chunk_count += 1
                    if chunk.get("usage"):
                        for key in ("prompt_tokens", "completion_tokens"):
                            value = chunk["usage"].get(key, 0) or 0
                            totals[key] += value
                            step_usage[key] = step_usage.get(key, 0) + value
                    for choice in chunk.get("choices") or []:
                        # 补读 finish_reason（全仓库此前完全没读，见方案 §10 偏差 4）。
                        reason = choice.get("finish_reason")
                        if reason:
                            finish_reason = reason
                        delta = choice.get("delta") or {}
                        if delta.get("reasoning_content"):
                            collected["reasoning"] += delta["reasoning_content"]
                            yield thinking_delta(delta["reasoning_content"])
                        if delta.get("content"):
                            collected["content"] += delta["content"]
                            yield text_delta(delta["content"])
                        for call in delta.get("tool_calls") or []:
                            _merge_tool_call(collected["tool_calls"], call)
            if trace is not None:
                await trace.response(
                    payload_for_response(),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    truncated=raw.truncated,
                )
        except (Exception, asyncio.CancelledError) as exc:
            if trace is not None:
                await trace.response(
                    payload_for_response(str(exc)),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error=str(exc),
                    truncated=raw.truncated,
                )
            raise

    async def _execute_tools(
        self,
        calls: list[dict[str, Any]],
        workspace: WorkspaceTools,
        messages: list[dict[str, Any]],
        ctx: AgentContext,
    ) -> AsyncIterator[AgentEvent]:
        # 工具输出可能回显凭据值（如 printenv），回填给模型的 messages 前先擦掉，
        # 否则模型在后续推理的上下文里能看到明文（事件流已有 scrub_event，这里补 messages 侧）。
        secrets = list(ctx.credentials.values()) if ctx.credentials else []
        if ctx.api_key:
            secrets.append(ctx.api_key)
        for call in calls:
            name = call["name"] or "tool"
            try:
                arguments = json.loads(call["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = {}
            yield tool_call(name, call["id"], **arguments)
            try:
                output = await workspace.call(name, arguments)
                is_error = False
            except ToolError as exc:
                output, is_error = str(exc), True
            except TypeError as exc:
                output, is_error = f"工具入参不合法：{exc}", True
            if secrets:
                output = scrub(output, secrets)
            yield tool_result(call["id"], output, is_error)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": output if not is_error else f"ERROR: {output}",
                }
            )
        return

    async def probe_api(self, ctx: AgentContext) -> tuple[bool, str]:
        url = self.chat_completions_url(ctx)
        models_url = url[: -len("/chat/completions")] + "/models"
        try:
            async with wrap_client(httpx.AsyncClient(timeout=15.0), extra_hosts=[models_url]) as client:
                response = await client.get(models_url, headers=self.http_headers(ctx))
            if response.status_code < 400:
                return True, f"网关可达（HTTP {response.status_code}）"
            return False, f"网关返回 {response.status_code}：{response.text[:200]}"
        except httpx.HTTPError as exc:
            return False, f"无法连接网关：{exc}"

    # API 型智能体用不到，仅为满足基类契约而保留
    def build_invocation(self, ctx: AgentContext) -> Invocation:  # pragma: no cover
        raise NotImplementedError("进程内 API 型智能体不启动子进程")

    def parse_line(self, line: str, run: AgentRun) -> Iterable[AgentEvent]:  # pragma: no cover
        return ()


def _merge_tool_call(bucket: list[dict[str, Any]], chunk: dict[str, Any]) -> None:
    """OpenAI 会按 index 分片下发工具调用，这里做合并。"""
    index = chunk.get("index", 0)
    while len(bucket) <= index:
        bucket.append({"id": "", "name": "", "arguments": ""})
    slot = bucket[index]
    if chunk.get("id"):
        slot["id"] = chunk["id"]
    function = chunk.get("function") or {}
    if function.get("name"):
        slot["name"] = function["name"]
    if function.get("arguments"):
        slot["arguments"] += function["arguments"]


@register_agent
class OpenAICompatibleHarness(OpenAIHarnessBase):
    id = "openai-harness"
    name = "OpenAI 兼容内核"
    vendor = "通用"
    description = (
        "任意兼容 /chat/completions 的端点，由内置工具循环驱动。"
        "覆盖 OpenRouter、vLLM、Ollama、Kimi、GLM 等。"
    )
    api_key_env = "OPENAI_API_KEY"
    base_url_env = "OPENAI_BASE_URL"
    default_gateway = "https://api.openai.com/v1"
    default_model = "gpt-4o-mini"
    protocol = "openai"
    default_auth_style = "bearer"
    default_models = ["gpt-4o-mini"]
    logo = "/icons/kernels/openai.webp"
    #: OpenAI 的立体徽标是真彩色的，按原色显示才认得出来。
    logo_style = "image"
    #: 通用兼容模板：不在「新建智能体」的内核下拉里推荐，存量智能体照旧可用。
    selectable = False
