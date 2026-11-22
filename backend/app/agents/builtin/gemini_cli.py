"""Google Gemini CLI 适配器。"""
from __future__ import annotations

import json
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
    error,
    log,
    session,
    text_delta,
    tool_call,
    tool_result,
    usage,
)
from ..mcp import McpMount, McpServerSpec, parse_json_servers, to_json_servers
from ..registry import register_agent
from .util import (
    as_list,
    flatten_content,
    npm_cli,
    npm_install_argv,
    npm_prepare_argv,
    workspace_only,
)


#: Gemini CLI 的 MCP 配置只认用户级 / 项目级 ``settings.json``（没有独立的配置目录开关）。
#: 平台因此把它写进**工作目录**的项目级设置，并用 ``--allowed-mcp-server-names`` 限定启用范围。
SETTINGS_RELPATH = ".gemini/settings.json"


@register_agent
class GeminiCliAgent(AgentDefinition):
    id = "gemini-cli"
    name = "Gemini CLI"
    vendor = "Google"
    description = (
        "Google 的 Gemini CLI 智能体。输出流式 JSONL，支持会话检查点续接，"
        "并可通过 GEMINI_SYSTEM_MD 注入人格。"
    )
    runtime = Runtime.CLI
    executable = "gemini"
    provisioner = RuntimeProvisioner(
        manager="npm",
        package="@google/gemini-cli",
        #: 安装 / 更新走包装命令：本机既有的同名可执行文件（另一个 node 装的、
        #: 用户手写的垫片）会让 npm 直接失败在 EEXIST，先把这种占位挪开。
        install_argv=npm_install_argv("@google/gemini-cli", bin_name="gemini"),
        update_argv=npm_install_argv("@google/gemini-cli", bin_name="gemini", latest=True),
        uninstall_argv=npm_cli("uninstall", "-g", "@google/gemini-cli"),
        latest_argv=npm_cli("view", "@google/gemini-cli", "version"),
        prepare_argv=npm_prepare_argv("@google/gemini-cli"),
        homepage="https://github.com/google-gemini/gemini-cli",
    )
    api_key_env = "GEMINI_API_KEY"
    base_url_env = "GOOGLE_GEMINI_BASE_URL"
    default_gateway = "https://generativelanguage.googleapis.com"
    default_model = "gemini-2.5-pro"
    protocol = "gemini"
    default_auth_style = "query"
    default_models = ["gemini-2.5-pro"]
    docs_url = "https://github.com/google-gemini/gemini-cli"
    logo = "/icons/kernels/gemini.webp"
    #: Gemini CLI 的渐变描边图标是真彩色的，按原色显示才认得出来。
    logo_style = "image"
    capabilities = Capabilities(
        streaming=True,
        sessions=True,
        native_system_prompt=True,
        tools=True,
        mcp=True,
        permission_modes=True,
        working_directory=True,
    )
    config_fields = (
        ConfigField(
            "approval_mode",
            "审批模式",
            FieldType.SELECT,
            default="yolo",
            options=[
                {"value": "yolo", "label": "yolo · 自动批准所有工具调用"},
                {"value": "auto_edit", "label": "auto_edit · 自动应用文件编辑"},
                {"value": "default", "label": "default · 每次询问"},
            ],
        ),
        ConfigField(
            "system_prompt_mode",
            "人格注入方式",
            FieldType.SELECT,
            default="system_file",
            options=[
                {"value": "system_file", "label": "写入 GEMINI_SYSTEM_MD 系统提示词文件"},
                {"value": "prepend", "label": "拼接到提示词最前面"},
            ],
        ),
        ConfigField(
            "sandbox",
            "启用沙箱",
            FieldType.BOOLEAN,
            default=False,
            advanced=True,
            help="用容器后端（docker/podman）隔离执行；需要本机装好并配置好对应后端。",
        ),
        ConfigField(
            "file_access_scope",
            "文件访问范围",
            FieldType.SELECT,
            default="unrestricted",
            options=[
                {"value": "unrestricted", "label": "不限制 · 可访问本机任意路径"},
                {
                    "value": "workspace_only",
                    "label": "仅工作目录 · 强制启用沙箱（需 docker/podman）",
                },
            ],
            help=(
                "「仅工作目录」会强制启用沙箱，把文件访问限定在对话的工作目录内。"
                "Gemini CLI 的沙箱依赖容器后端（docker 或 podman），未安装时内核会启动失败；"
                "没有容器后端时请保持「不限制」。"
            ),
        ),
        ConfigField(
            "extensions",
            "扩展插件",
            FieldType.STRING_LIST,
            advanced=True,
            placeholder="例如 gemini-cli-extensions/mcp-toolbox",
        ),
        ConfigField("extra_args", "额外命令行参数", FieldType.STRING_LIST, advanced=True),
        ConfigField("env", "环境变量", FieldType.KEYVALUE, advanced=True),
    )

    def discover_mcp_servers(self) -> list[McpServerSpec]:
        """扫描用户级 ``~/.gemini/settings.json`` 里的 ``mcpServers``（只读、尽力而为）。"""
        try:
            raw = json.loads((Path.home() / ".gemini" / "settings.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return parse_json_servers(raw, type_id=self.id, source_kind="user")

    def mcp_mount(self, ctx: AgentContext) -> McpMount:
        """把分配到的服务器写进工作目录的项目级设置，并限定只启用它们。

        Gemini CLI 没有「换个配置目录」的开关，MCP 配置只能走它自己的
        ``settings.json``（用户级 ∪ 项目级）。平台写项目级的一份，并用
        ``--allowed-mcp-server-names`` 把可用范围收窄到分配到的服务器。
        """
        names = [spec.slug for spec in ctx.mcp_servers]
        content = {
            "mcpServers": to_json_servers(ctx.mcp_servers),
            "mcp": {"allowed": names},
        }
        return McpMount(
            home_name="gemini-home",
            workspace_files={
                SETTINGS_RELPATH: json.dumps(content, ensure_ascii=False, indent=2) + "\n"
            },
        )

    # -- 构造启动命令 --------------------------------------------------------

    def build_invocation(self, ctx: AgentContext) -> Invocation:
        options = ctx.options
        argv = [self.executable or "gemini", "--output-format", "stream-json"]

        mode = options.get("approval_mode") or "yolo"
        if mode == "yolo":
            argv.append("--yolo")
        elif mode == "auto_edit":
            argv.append("--approval-mode=auto_edit")

        if ctx.model:
            argv += ["-m", ctx.model]
        if ctx.session_id:
            argv += ["--resume", ctx.session_id]
        elif ctx.session_id is not None:
            argv += ["--session-summary", ctx.session_id]
        # 「仅工作目录」强制启用沙箱（未单独勾选沙箱也要带）。
        if options.get("sandbox") or workspace_only(ctx):
            argv.append("--sandbox")
        for ext in as_list(options.get("extensions") or []):
            argv += ["--extensions", ext]
        # 严格仅分配：只启用智能体分配到的 MCP 服务器（写在工作目录的项目级设置里）。
        names = [spec.slug for spec in ctx.mcp_servers]
        if names:
            argv += ["--allowed-mcp-server-names", *names]
        argv += [str(a) for a in as_list(options.get("extra_args") or [])]

        prompt = ctx.prompt
        env = self.effective_env(ctx)
        files: dict[str, str] = {}

        # Gemini CLI 通过一个系统提示词 markdown 文件注入人格。
        if ctx.system_prompt:
            if (options.get("system_prompt_mode") or "system_file") == "prepend":
                prompt = f"{ctx.system_prompt}\n\n---\n\n{prompt}"
            else:
                files[".coda/system.md"] = ctx.system_prompt
                env["GEMINI_SYSTEM_MD"] = "{{RUN_DIR}}/.coda/system.md"

        # 用 -p 让提示词走 stdin，避免长上下文把参数长度撑爆。
        argv += ["-p", ""]
        return Invocation(argv=argv, env=env, cwd=ctx.workspace_dir, stdin=prompt, files=files)

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
        yield from self._dispatch(payload, run)

    def _dispatch(self, payload: dict[str, Any], run: AgentRun) -> Iterable[AgentEvent]:
        kind = payload.get("type")

        if kind == "init":
            sid = payload.get("session_id")
            if sid:
                run.state["session"] = sid
                yield session(sid)
            if payload.get("model"):
                yield log(f"Gemini 已就绪 · 模型 {payload['model']}")
            return

        if kind == "message":
            role = payload.get("role")
            text = payload.get("content")
            if not text:
                return
            if role in (None, "assistant"):
                yield text_delta(text)
            return

        if kind in ("tool_call", "tool_use"):
            name = payload.get("tool_name") or payload.get("name") or "tool"
            args = payload.get("parameters") or payload.get("args") or {}
            yield tool_call(name, payload.get("tool_id") or payload.get("id"), **(args if isinstance(args, dict) else {"input": args}))
            return

        if kind == "tool_result":
            yield tool_result(
                payload.get("tool_id") or payload.get("id"),
                flatten_content(payload.get("output") or payload.get("result")),
                bool(payload.get("is_error") or payload.get("error")),
            )
            return

        if kind == "result":
            stats = payload.get("stats") or {}
            if stats:
                counters = {
                    k: v
                    for k, v in stats.items()
                    if k.endswith("_tokens") or k in ("duration_ms", "total_tokens")
                }
                if counters:
                    yield usage(**counters)
            if payload.get("error"):
                run.state["is_error"] = True
                yield error(flatten_content(payload["error"]))
            return

        if kind == "error":
            run.state["is_error"] = True
            yield error(payload.get("message") or flatten_content(payload))
