"""用户自定义 CLI 智能体 —— 不写代码也能接入新智能体。

用户描述如何调用任意命令行智能体（Aider、自研的内核二进制、
内部工具……）以及如何解析它的输出。这是保持平台开放性的声明式逃生舱。
"""
from __future__ import annotations

import json
import shutil
from typing import Any, Iterable

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
from ..events import AgentEvent, error, log, session, text_delta, tool_call, tool_result, usage
from ..registry import register_agent
from ..metering import (
    PROTOCOL_ANTHROPIC,
    PROTOCOL_DEEPSEEK,
    PROTOCOL_GEMINI,
    PROTOCOL_OPENAI_CHAT,
    PROTOCOL_OPENAI_RESPONSES,
)
from .util import as_list, flatten_content, truthy


@register_agent
class CustomCliAgent(AgentDefinition):
    id = "custom-cli"
    name = "自定义 CLI"
    vendor = "自定义"
    description = (
        "声明式适配器：指向任意命令行智能体，描述它的参数与输出结构，"
        "它就能成为可绑定给智能体的一等公民。"
    )
    runtime = Runtime.CLI
    logo = "/icons/kernels/cli.svg"
    executable = None  # 由实例上的「可执行命令」决定
    auto_provision = False  # 这是模板：未填写命令前不参与播种
    selectable = False  # 逃生舱模板：不在「新建智能体」的内核下拉里推荐
    api_key_env = None
    base_url_env = None
    capabilities = Capabilities(
        streaming=True,
        sessions=True,
        native_system_prompt=False,
        tools=True,
        working_directory=True,
    )
    config_fields = (
        ConfigField(
            "command",
            "可执行命令",
            FieldType.STRING,
            required=True,
            placeholder="aider / my-agent / /opt/bin/my-agent",
            help="按 PATH 查找，与 shell 的行为一致。",
        ),
        ConfigField(
            "args",
            "启动参数",
            FieldType.STRING_LIST,
            placeholder="--model {model} --message {prompt}",
            help="可用占位符：{prompt} {model} {gateway} {api_key} {workspace} {session}",
        ),
        ConfigField(
            "prompt_mode",
            "提示词传递方式",
            FieldType.SELECT,
            default="stdin",
            options=[
                {"value": "stdin", "label": "通过标准输入（最稳妥）"},
                {"value": "arg", "label": "作为命令行参数（配合 {prompt}）"},
            ],
        ),
        ConfigField(
            "output_format",
            "输出格式",
            FieldType.SELECT,
            default="text",
            options=[
                {"value": "jsonl", "label": "JSON Lines（每行一个 JSON）"},
                {"value": "text", "label": "纯文本流"},
            ],
        ),
        ConfigField(
            "text_path",
            "正文的 JSON 路径",
            FieldType.STRING,
            placeholder="item.text / message.content / delta",
            advanced=True,
        ),
        ConfigField(
            "delta_mode",
            "该路径是增量文本",
            FieldType.BOOLEAN,
            default=True,
            advanced=True,
            help="关闭后表示每行 JSON 携带的是「截至目前」的完整文本。",
        ),
        ConfigField(
            "session_path",
            "会话 ID 的 JSON 路径",
            FieldType.STRING,
            placeholder="session_id / thread.id",
            advanced=True,
            help="一旦能取到会话 ID，就会自动启用多轮对话。",
        ),
        ConfigField(
            "tool_path",
            "工具名的 JSON 路径",
            FieldType.STRING,
            placeholder="tool_call.name",
            advanced=True,
        ),
        ConfigField(
            "tool_result_path",
            "工具输出的 JSON 路径",
            FieldType.STRING,
            placeholder="tool_call.output",
            advanced=True,
        ),
        ConfigField(
            "usage_path",
            "用量统计的 JSON 路径",
            FieldType.STRING,
            placeholder="usage",
            advanced=True,
        ),
        ConfigField(
            "usage_style",
            "用量字段口径",
            FieldType.SELECT,
            default="auto",
            advanced=True,
            options=[
                {"value": "auto", "label": "自动识别（字段兜底）"},
                {"value": "anthropic", "label": "Anthropic：输入与缓存分离"},
                {"value": "openai_chat", "label": "OpenAI Chat：缓存含在 prompt_tokens"},
                {"value": "openai_responses", "label": "OpenAI Responses：缓存含在 input_tokens"},
                {"value": "gemini", "label": "Gemini：缓存含在 promptTokenCount"},
                {"value": "deepseek", "label": "DeepSeek：输入与缓存分离（待验证）"},
            ],
            help="auto = 按实际字段识别；如果你的 CLI 字段名有歧义，在这里显式指定。",
        ),
        ConfigField("api_key_env", "API 密钥环境变量名", FieldType.STRING, advanced=True,
                    placeholder="MY_AGENT_API_KEY"),
        ConfigField("gateway_env", "网关环境变量名", FieldType.STRING, advanced=True,
                    placeholder="MY_AGENT_BASE_URL"),
        ConfigField("env", "环境变量", FieldType.KEYVALUE, advanced=True),
    )

    # -- 元数据与自省 --------------------------------------------------------

    def availability(self) -> Availability:
        return Availability(True, detail="由实例上的「可执行命令」决定")

    def usage_protocol_for(self, ctx: AgentContext) -> str | None:
        """按用户配置的 ``usage_style`` 选协议；``auto`` 交给字段兜底。"""
        style = str(ctx.option("usage_style", "auto") or "auto").strip().lower()
        return {
            "anthropic": PROTOCOL_ANTHROPIC,
            "openai_chat": PROTOCOL_OPENAI_CHAT,
            "openai_responses": PROTOCOL_OPENAI_RESPONSES,
            "gemini": PROTOCOL_GEMINI,
            "deepseek": PROTOCOL_DEEPSEEK,
        }.get(style)

    async def probe(self, ctx: AgentContext) -> tuple[bool, str]:
        command = str(ctx.option("command") or "")
        if not command:
            return False, "尚未填写「可执行命令」"
        path = shutil.which(command) or (command if "/" in command else None)
        if not path:
            return False, f"未在 PATH 中找到 `{command}`"
        return True, f"解析到 {path}"

    def resume_supported(self, ctx: AgentContext) -> bool:
        # 只有能解析出会话 ID 时才能续接；否则交给框架回放完整对话记录。
        return bool(ctx.option("session_path"))

    # -- 构造启动命令 --------------------------------------------------------

    def _placeholders(self, ctx: AgentContext) -> dict[str, str]:
        return {
            "prompt": ctx.prompt,
            "model": ctx.model or "",
            "gateway": ctx.gateway_url or "",
            "api_key": ctx.api_key or "",
            "workspace": ctx.workspace_dir or "",
            "session": ctx.session_id or "",
            "agent": ctx.agent_name,
        }

    def build_invocation(self, ctx: AgentContext) -> Invocation:
        command = str(ctx.option("command") or "").strip()
        if not command:
            raise ValueError("自定义 CLI 智能体必须填写「可执行命令」")

        values = self._placeholders(ctx)
        raw_args = as_list(ctx.option("args") or [])
        if not raw_args:
            raw_args = ["-p", "{prompt}"]
        argv = [command] + [_safe_format(arg, values) for arg in raw_args]

        prompt_mode = ctx.option("prompt_mode") or "stdin"
        stdin = None if any("{prompt}" in a for a in raw_args) else None
        if prompt_mode == "stdin":
            stdin = ctx.prompt
        elif not any("{prompt}" in a for a in raw_args):
            argv.append(ctx.prompt)

        env = self.effective_env(ctx)
        if ctx.api_key and ctx.option("api_key_env"):
            env[str(ctx.option("api_key_env"))] = ctx.api_key
        if ctx.gateway_url and ctx.option("gateway_env"):
            env[str(ctx.option("gateway_env"))] = ctx.gateway_url

        return Invocation(argv=argv, env=env, cwd=ctx.workspace_dir, stdin=stdin)

    # -- 解析输出 ------------------------------------------------------------

    def parse_line(self, line: str, run: AgentRun) -> Iterable[AgentEvent]:
        line = line.rstrip("\n")
        if not line.strip():
            return
        fmt = run.ctx.option("output_format") or "text"
        if fmt != "jsonl":
            yield text_delta(line + "\n")
            return
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            yield log(line, "debug")
            return
        yield from self._dispatch_json(payload, run)

    def _dispatch_json(self, payload: Any, run: AgentRun) -> Iterable[AgentEvent]:
        options = run.ctx.options
        kind = payload.get("type") if isinstance(payload, dict) else None

        session_path = options.get("session_path")
        if session_path:
            found = _dig(payload, session_path)
            if found and str(found) != str(run.state.get("session")):
                run.state["session"] = found
                yield session(str(found))

        if kind == "error":
            run.state["is_error"] = True
            yield error(flatten_content(payload))
            return

        result_path = options.get("tool_result_path")
        if kind == "tool_result" or (result_path and _dig(payload, result_path) is not None):
            yield tool_result(
                payload.get("id") or payload.get("tool_id") or _dig(payload, "tool_call.id"),
                flatten_content(
                    _dig(payload, result_path) if result_path else payload.get("output") or payload.get("result")
                ),
                bool(payload.get("is_error") or payload.get("error")),
            )
            return

        tool_path = options.get("tool_path")
        if tool_path:
            name = _dig(payload, tool_path)
            if name:
                args = _dig(payload, tool_path.rsplit(".", 1)[0] + ".args") or payload.get("args") or {}
                yield tool_call(
                    str(name),
                    payload.get("id") or _dig(payload, tool_path.rsplit(".", 1)[0] + ".id"),
                    **(args if isinstance(args, dict) else {}),
                )
                return

        if kind in ("result", "usage", "turn.completed", "done"):
            counters = _dig(payload, options.get("usage_path") or "usage")
            if isinstance(counters, dict) and counters:
                yield usage(**counters)
            return

        text_path = options.get("text_path")
        if not text_path:
            return
        value = _dig(payload, text_path)
        if value in (None, ""):
            return
        text = value if isinstance(value, str) else flatten_content(value)
        if truthy(options.get("delta_mode", True)):
            yield text_delta(text)
        else:
            previous = run.state.get("full_text", "")
            if text.startswith(previous):
                incremental = text[len(previous) :]
                run.state["full_text"] = text
                if incremental:
                    yield text_delta(incremental)
            else:
                run.state["full_text"] = text
                yield text_delta(text)


def _safe_format(template: str, values: dict[str, str]) -> str:
    class _Keep(dict):
        def __missing__(self, key: str) -> str:  # 保留未知占位符原样
            return "{" + key + "}"

    try:
        return template.format_map(_Keep(values))
    except (ValueError, IndexError):
        return template


def _dig(payload: Any, path: str) -> Any:
    cursor = payload
    for part in path.split("."):
        if isinstance(cursor, dict):
            cursor = cursor.get(part)
        elif isinstance(cursor, list) and part.isdigit():
            cursor = cursor[int(part)] if int(part) < len(cursor) else None
        else:
            return None
    return cursor
