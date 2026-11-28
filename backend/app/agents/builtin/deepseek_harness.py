"""DeepSeek Harness（``dsh``）CLI 适配器。

DeepSeek Harness 是 DeepSeek AI 开源的一体化智能体框架，官方以 npm 包
``@deepseek-ai/dsh`` 分发。它的 ``headless`` profile 天生适合被平台托管：
一次调用跑一个任务、stdout 逐行输出 JSON 运行事件、``--session-id`` 续接
持久化会话，正好对上本平台的 CLI 运行模型。

这里只声明「怎么启动、怎么解析、配置落在哪」三件事：

* 启动：``dsh headless --patch … --json --session-id …``，任务走 stdin；
* 解析：``session / status / text / thinking / tool_call / tool_result /
  error / final`` 八种事件；
* 隔离：``DSH_HOME`` 指向对话级隔离目录，技能、会话与 MCP 配置都落在里面，
  人格写进 ``$DSH_HOME/AGENTS.md``（dsh 自己的全局指令文件）。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

from ..base import (
    AgentContext,
    AgentDefinition,
    AgentRun,
    Capabilities,
    ConfigField,
    FieldType,
    Invocation,
    Runtime,
    RuntimeProvisioner,
)
from ..events import (
    AgentEvent,
    context_usage,
    error,
    log,
    session,
    text_delta,
    thinking_delta,
    tool_call,
    tool_result,
    usage_raw,
)
from ..mcp import McpMount, McpServerSpec
from ..registry import register_agent
from ..skills import SkillMount, SkillRoot, SkillSpec, scan_skill_roots
from ..metering import PROTOCOL_DEEPSEEK
from .util import (
    as_list,
    context_tokens,
    flatten_content,
    npm_cli,
    npm_install_argv,
    npm_prepare_argv,
)

#: 隔离目录里的两个 patch 文件：模型/技能配置由适配器自己写，MCP 由框架物化。
_OVERLAY_FILE = "coda.patch.yml"
_MCP_FILE = "coda-mcp.patch.yml"

#: 技能目录名；dsh 的 ``skill-filesystem`` 从这里读技能。
_SKILLS_SUBDIR = "skills"

#: dsh 的默认安装包与可执行文件名。
_PACKAGE = "@deepseek-ai/dsh"
_BINARY = "dsh"

#: dsh 的 MCP 服务器名只接受 ``[A-Za-z0-9_-]{1,32}``。
_SERVER_NAME_ALLOWED = re.compile(r"[^A-Za-z0-9_-]+")


def dsh_home() -> Path:
    """dsh 的真实配置目录（认 ``DSH_HOME``，默认 ``~/.dsh``）。

    只用于「扫描本机已有配置」；**运行时**永远使用对话级隔离目录。
    """
    override = os.environ.get("DSH_HOME")
    return Path(override).expanduser() if override else Path.home() / ".dsh"


def _scalar(value: Any) -> str:
    """把一个值写成安全的 YAML 标量。

    JSON 的双引号字符串本身就是合法的 YAML 标量，因此这里直接借 ``json.dumps``
    转义换行、引号与反斜杠，不引入额外的 YAML 依赖（适配器只依赖标准库）。
    """
    return json.dumps(str(value), ensure_ascii=False)


def _server_name(slug: str, index: int, used: set[str]) -> str:
    """把平台里的服务器标识裁成 dsh 接受的名字，并保证同一组合内唯一。"""
    base = _SERVER_NAME_ALLOWED.sub("-", slug or "").strip("-")[:28]
    if not base:
        base = f"mcp-{index}"
    name = base
    suffix = 2
    while name in used:
        tail = f"-{suffix}"
        name = f"{base[: 32 - len(tail)]}{tail}"
        suffix += 1
    used.add(name)
    return name


@register_agent
class DeepSeekHarnessAgent(AgentDefinition):
    id = "deepseek-harness"
    name = "DeepSeek Harness"
    vendor = "DeepSeek"
    description = (
        "DeepSeek AI 开源的一体化智能体框架（dsh）。通过 headless 模式逐行输出 JSON 运行事件，"
        "支持会话续接、沙箱权限、技能与 MCP。"
    )
    runtime = Runtime.CLI
    executable = _BINARY
    provisioner = RuntimeProvisioner(
        manager="npm",
        package=_PACKAGE,
        #: 安装 / 更新走冲突感知包装（本机既有的同名可执行文件会让 npm 失败在 EEXIST）。
        install_argv=npm_install_argv(_PACKAGE, bin_name=_BINARY),
        update_argv=npm_install_argv(_PACKAGE, bin_name=_BINARY, latest=True),
        uninstall_argv=npm_cli("uninstall", "-g", _PACKAGE),
        latest_argv=npm_cli("view", _PACKAGE, "version"),
        prepare_argv=npm_prepare_argv(_PACKAGE),
        homepage="https://github.com/deepseek-ai/deepseek-harness",
        note="需要 Node.js 24.9 或更高版本。",
    )
    #: headless profile 从启动环境读取 DeepSeek 凭据（它禁用了内置凭据库）。
    api_key_env = "DEEPSEEK_API_KEY"
    base_url_env = "DEEPSEEK_BASE_URL"
    #: DeepSeek 的 Messages 接口与 Anthropic 协议兼容。
    default_gateway = "https://api.deepseek.com/anthropic"
    default_model = "deepseek-flash"
    protocol = "anthropic"
    #: dsh 的 usage 是驼峰字段；暂按“输入与缓存分离”处理，待真实样本确认。
    usage_protocol = PROTOCOL_DEEPSEEK
    default_auth_style = "x-api-key"
    default_models = ["deepseek-flash", "deepseek-v4-pro"]
    docs_url = "https://deepseek-harness.github.io/deepseek-harness/"
    logo = "/icons/kernels/deepseek.webp"
    #: 蓝鲸标是真彩色的，按原色显示才认得出来。
    logo_style = "image"
    capabilities = Capabilities(
        streaming=True,
        sessions=True,
        #: 人格写进 $DSH_HOME/AGENTS.md，由内核在系统提示词里原生加载。
        native_system_prompt=True,
        tools=True,
        mcp=True,
        permission_modes=True,
        working_directory=True,
        skills=True,
        # 接受 DEEPSEEK_BASE_URL 覆盖；协议（Anthropic Messages）在网关支持范围内。
        recording_gateway=True,
    )
    config_fields = (
        ConfigField(
            "permission_mode",
            "权限模式",
            FieldType.SELECT,
            default="workspace-write",
            options=[
                {"value": "read-only", "label": "read-only · 只读"},
                {"value": "workspace-write", "label": "workspace-write · 可写工作目录（推荐）"},
                {"value": "danger-full-access", "label": "danger-full-access · 完全访问（危险）"},
            ],
            help="非交互运行没有审批通道，工具调用统一按「从不询问」执行。",
        ),
        ConfigField(
            "reasoning_effort",
            "推理强度",
            FieldType.SELECT,
            default="",
            options=[
                {"value": "", "label": "内核默认"},
                {"value": "off", "label": "off · 关闭"},
                {"value": "low", "label": "low · 低"},
                {"value": "high", "label": "high · 高"},
                {"value": "max", "label": "max · 最高"},
            ],
            advanced=True,
        ),
        ConfigField(
            "extra_args",
            "额外命令行参数",
            FieldType.STRING_LIST,
            advanced=True,
            help="追加到 headless 命令末尾，原样传给内核。",
        ),
        ConfigField("env", "环境变量", FieldType.KEYVALUE, advanced=True),
    )

    # -- 技能 ---------------------------------------------------------------

    def skill_roots(self) -> list[SkillRoot]:
        """本机 dsh 的用户技能根（``$DSH_HOME/skills``）。

        dsh 的文件系统技能提供方默认还会扫描项目目录与 ``~/.agents``；运行时由
        :meth:`_overlay` 关掉那些默认根，只保留本次物化进隔离目录的技能。
        """
        return [
            SkillRoot(
                str(dsh_home() / _SKILLS_SUBDIR),
                source_kind="user",
                installable=True,
                max_depth=1,
            )
        ]

    def discover_skills(self) -> list[SkillSpec]:
        return scan_skill_roots(self.skill_roots(), type_id=self.id)

    def skill_mount(self, ctx: AgentContext) -> SkillMount:
        return SkillMount(home_name="dsh-home", skills_subdir=_SKILLS_SUBDIR)

    def skill_invocation(self, tool_name: str, tool_input: dict[str, Any]) -> str | None:
        """dsh 的技能加载工具叫 ``skill``，入参是 ``{name}``。"""
        if tool_name != "skill":
            return None
        name = tool_input.get("name")
        return str(name) if isinstance(name, str) and name.strip() else None

    # -- MCP ----------------------------------------------------------------

    def discover_mcp_servers(self) -> list[McpServerSpec]:
        """只读扫描本机 profile patch 里的 MCP 配置（尽力而为、不得抛错）。

        dsh 的 MCP 配置有两种写法：``mcp`` 管理器的 ``config.servers`` 表，
        以及一条条独立的 ``dsh-mcp-client`` 行。两种都收，扫到哪算哪。
        """
        try:
            import yaml
        except ImportError:  # pragma: no cover - 平台依赖里始终有 PyYAML
            return []

        specs: list[McpServerSpec] = []
        for path in sorted(dsh_home().glob("profiles/*/cordis.patch.yml")):
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(raw, list):
                continue
            for item in raw:
                if not isinstance(item, dict):
                    continue
                config = item.get("config")
                if not isinstance(config, dict):
                    continue
                servers = config.get("servers")
                if isinstance(servers, dict):
                    for name, entry in servers.items():
                        spec = self._entry_to_spec(str(name), entry)
                        if spec is not None:
                            specs.append(spec)
                if str(item.get("name") or "") == "@deepseek-ai/dsh-mcp-client":
                    name = config.get("serverName")
                    if isinstance(name, str):
                        spec = self._entry_to_spec(name, config)
                        if spec is not None:
                            specs.append(spec)
        return specs

    def _entry_to_spec(self, name: str, entry: Any) -> McpServerSpec | None:
        if not isinstance(entry, dict):
            return None
        transport = str(entry.get("transport") or "").strip().lower()
        command = entry.get("command")
        url = entry.get("url")
        if transport == "streamable-http" or (url and not command):
            transport = "http"
        elif command:
            transport = "stdio"
        else:
            return None
        env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
        headers = entry.get("headers") if isinstance(entry.get("headers"), dict) else {}
        args = entry.get("args") if isinstance(entry.get("args"), list) else []
        return McpServerSpec(
            name=name,
            slug=name,
            transport=transport,
            command=str(command) if command else None,
            args=[str(item) for item in args],
            url=str(url) if url else None,
            env={str(k): str(v) for k, v in env.items() if v not in (None, "")},
            headers={str(k): str(v) for k, v in headers.items() if v not in (None, "")},
            type_id=self.id,
            source_kind="user",
        )

    def mcp_mount(self, ctx: AgentContext) -> McpMount:
        """把分配到的服务器写成一条 ``insert`` 覆盖层。

        dsh 的 headless profile 不挂 MCP 管理器，但挂一条 ``dsh-mcp-client``
        只需要在 profile 覆盖层里插入一行。覆盖层每次运行重建；没有服务器时
        写成空列表，避免上一轮的文件残留让取消分配失效。
        """
        rows: list[str] = []
        notes: list[str] = []
        used: set[str] = set()
        for index, server in enumerate(ctx.mcp_servers):
            if server.transport == "sse":
                notes.append(f"DeepSeek Harness 不支持 SSE 传输，已跳过 MCP「{server.name}」")
                continue
            if server.transport == "http" and not server.url:
                notes.append(f"MCP「{server.name}」缺少地址，已跳过")
                continue
            if server.transport != "http" and not server.command:
                notes.append(f"MCP「{server.name}」缺少启动命令，已跳过")
                continue
            name = _server_name(server.slug, index, used)
            rows += [
                f"    - id: {_scalar(f'coda-mcp-{name}')}",
                "      name: '@deepseek-ai/dsh-mcp-client'",
                "      config:",
                f"        serverName: {_scalar(name)}",
            ]
            if server.transport == "http":
                rows += [
                    "        transport: streamable-http",
                    f"        url: {_scalar(server.url or '')}",
                ]
                if server.headers:
                    rows.append("        headers:")
                    rows += [
                        f"          {_scalar(key)}: {_scalar(value)}"
                        for key, value in server.headers.items()
                    ]
            else:
                rows += [
                    "        transport: stdio",
                    f"        command: {_scalar(server.command or '')}",
                ]
                if server.args:
                    rows.append("        args:")
                    rows += [f"          - {_scalar(arg)}" for arg in server.args]
                if server.env:
                    rows.append("        env:")
                    rows += [
                        f"          {_scalar(key)}: {_scalar(value)}"
                        for key, value in server.env.items()
                    ]
        content = "- insert:\n" + "\n".join(rows) + "\n" if rows else "[]\n"
        return McpMount(
            home_name="dsh-home",
            files={_MCP_FILE: content},
            notes=notes,
        )

    # -- 构造启动命令 --------------------------------------------------------

    def _overlay(self, ctx: AgentContext, home: Path) -> str:
        """模型选择与技能根的运行时覆盖层。

        dsh 的 headless 没有「本次运行指定模型」的开关（模型来自组合默认值），
        因此用 launcher 的 ``--patch`` 覆盖``agent-default-model``；
        同时关掉技能提供方的默认根，保证「只能用到分配到的技能」。
        """
        lines: list[str] = []
        model = ctx.model or None
        effort = ctx.option("reasoning_effort")
        if model or effort:
            lines += [
                "- id: agent-default-model",
                "  config:",
                "    provider: 'deepseek-official'",
                f"    model: {_scalar(model or self.default_model)}",
            ]
            if effort:
                lines.append(f"    reasoningEffort: {_scalar(effort)}")
        lines += [
            "- id: skill-filesystem",
            "  config:",
            "    includeDefaultRoots: false",
            "    watch: false",
            "    customSkillDirs:",
            f"      - {_scalar(str(home / _SKILLS_SUBDIR))}",
        ]
        return "\n".join(lines) + "\n"

    def _write_instructions(self, home: Path, prompt: str | None) -> None:
        """把人格写进 ``$DSH_HOME/AGENTS.md``（dsh 的用户级指令文件）。

        这是 dsh 原生的注入点：它会在第一次请求时把文件内容作为持久基线并入
        系统提示词，恢复会话时还会对账更新。人格为空时删掉旧文件，避免上一份
        残留继续生效。
        """
        target = home / "AGENTS.md"
        if prompt:
            target.write_text(prompt, encoding="utf-8")
        else:
            try:
                target.unlink()
            except FileNotFoundError:
                pass

    def build_invocation(self, ctx: AgentContext) -> Invocation:
        argv = [self.executable or _BINARY, "headless"]
        home = Path(ctx.home_dir) if ctx.home_dir else None
        prompt = ctx.prompt

        if home is not None:
            home.mkdir(parents=True, exist_ok=True)
            overlay = home / _OVERLAY_FILE
            overlay.write_text(self._overlay(ctx, home), encoding="utf-8")
            # launcher 的 --patch 必须排在第一个「它不认识的参数」之前。
            mcp_patch = home / _MCP_FILE
            if mcp_patch.is_file():
                argv += ["--patch", str(mcp_patch)]
            argv += ["--patch", str(overlay)]

        argv.append("--json")
        # dsh 只接受「它自己生成的」会话 id（``--session-id`` 是只采纳语义，未知 id 直接报错）。
        # 换过内核的存量对话可能带着别家的 id（例如 custom-cli 的 ``sess-42``），
        # 这种情况下丢弃它、本轮开新会话，比让整条消息失败更好。
        if ctx.session_id and str(ctx.session_id).startswith("session-"):
            argv += ["--session-id", str(ctx.session_id)]
        argv += ["--permission-mode", str(ctx.option("permission_mode") or "workspace-write")]
        # headless 没有实时审批通道：统一「从不询问」，否则工具调用会卡在授权上。
        # 非交互运行没有审批通道：MCP 工具（含平台内部的任务进度卡）统一按
        # 「从不询问」执行，无需额外的工具白名单。
        argv += ["--approval-policy", "never"]
        argv += [str(item) for item in as_list(ctx.option("extra_args") or [])]
        argv.append("-")

        env = self.effective_env(ctx)
        if home is not None:
            env["DSH_HOME"] = str(home)
            # 隔离共享 agents 根，避免读到本机 ~/.agents 下的技能与指令。
            env["DSH_AGENTS_HOME"] = str(home / "agents")
            self._write_instructions(home, ctx.system_prompt)
        elif ctx.system_prompt:
            # 没有隔离目录（例如单测直接构造 context）时退回「人格拼进提示词」。
            prompt = f"{ctx.system_prompt}\n\n---\n\n{prompt}"

        return Invocation(argv=argv, env=env, cwd=ctx.workspace_dir, stdin=prompt)

    # -- 解析输出 ------------------------------------------------------------

    def parse_line(self, line: str, run: AgentRun) -> Iterable[AgentEvent]:
        line = line.strip()
        if not line:
            return
        if not line.startswith("{"):
            yield log(line, "debug")
            return
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            yield log(line, "debug")
            return
        if not isinstance(payload, dict):
            yield log(line, "debug")
            return
        yield from self._dispatch(payload, run)

    def _dispatch(self, payload: dict[str, Any], run: AgentRun) -> Iterable[AgentEvent]:
        kind = payload.get("type")

        if kind == "session":
            sid = payload.get("sessionId") or payload.get("session_id")
            if sid and run.session_id != sid:
                run.session_id = str(sid)
                yield session(str(sid))
            return

        if kind == "status":
            if payload.get("phase") == "step_end":
                raw_usage = payload.get("usage")
                if isinstance(raw_usage, dict) and raw_usage:
                    # 原样上报（驼峰字段由 metering 层按 deepseek 协议翻译）。
                    yield usage_raw(raw_usage)
                    window = context_tokens(
                        raw_usage, protocol=PROTOCOL_DEEPSEEK
                    )
                    if window:
                        yield context_usage(window)
            return

        if kind == "text":
            text = payload.get("text")
            if text:
                yield text_delta(str(text))
            return

        if kind == "thinking":
            text = payload.get("text")
            if text:
                yield thinking_delta(str(text))
            return

        if kind == "tool_call":
            name = str(payload.get("tool") or payload.get("name") or "tool")
            call_id = payload.get("callId") or payload.get("call_id")
            arguments = payload.get("input")
            if not isinstance(arguments, dict):
                arguments = {"input": arguments}
            yield tool_call(name, call_id, **arguments)
            return

        if kind == "tool_result":
            call_id = payload.get("callId") or payload.get("call_id")
            is_error = str(payload.get("status") or "").lower() == "error"
            output = payload.get("result", payload.get("output"))
            yield tool_result(call_id, flatten_content(output), is_error)
            return

        if kind == "error":
            run.state["is_error"] = True
            yield error(str(payload.get("message") or flatten_content(payload)))
            return

        if kind == "final":
            sid = payload.get("sessionId") or payload.get("session_id")
            if sid and run.session_id != sid:
                run.session_id = str(sid)
                yield session(str(sid))
            text = payload.get("text")
            # 事件流正常时正文已经逐块发过；只有一条汇总时补发，别让落库变空。
            if not run.emitted_text and text:
                yield text_delta(str(text))
            reason = payload.get("reason")
            if isinstance(reason, dict) and str(reason.get("kind") or "") not in ("", "completed"):
                run.state["is_error"] = True
                yield error(f"DeepSeek Harness 以「{reason.get('kind')}」结束")
            return

        # 未知事件不吵用户：dsh 的词汇表会演进，多出来的一律按调试日志记录。
        yield log(json.dumps(payload, ensure_ascii=False), "debug")
