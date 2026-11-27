"""OpenAI Codex CLI 适配器。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
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
    context_compacted,
    context_usage,
    context_window,
    error,
    log,
    session,
    text_delta,
    thinking_delta,
    tool_call,
    tool_result,
    usage,
)
from ..mcp import McpMount, McpServerSpec, parse_toml_servers, render_toml_servers, strip_toml_table
from ..registry import register_agent
from ..skills import (
    MARKET_SOURCE,
    SkillMount,
    SkillRoot,
    SkillSpec,
    scan_skill_roots,
)
from ..metering import PROTOCOL_OPENAI_RESPONSES
from .util import (
    as_list,
    codex_compaction_overrides,
    context_tokens,
    flatten_content,
    npm_cli,
    npm_install_argv,
    npm_prepare_argv,
    truthy,
    workspace_only,
)


#: Codex 的登录态与配置文件名；隔离运行时要一并带过去，否则会丢登录或丢自定义供应商。
_CARRY_FILES = ("config.toml", "auth.json")

#: 平台「技能市场」安装目录（``$CODEX_HOME/skills/.skillhub/<skill>``），可卸载。
_MARKET_DIRNAME = ".skillhub"

#: Codex 的 MCP 服务器表名（``[mcp_servers.<name>]``）。
_MCP_TABLE = "mcp_servers"

#: 给自定义网关生成的 ``model_provider`` 的默认名（用户可用「自定义供应商名称」覆盖）。
#: 新版 Codex 不再读 ``OPENAI_BASE_URL``（内置 provider 会直奔 api.openai.com），
#: 自定义网关只有落到一个具名 provider 上才会真正生效。
_DEFAULT_PROVIDER_NAME = "coda"

#: 两种接口模式：``app-server`` 走 Codex 自己的 JSON-RPC（能逐字流式），
#: ``exec`` 走 ``codex exec --json``（整条消息完成才给一次事件，仅用于兼容旧版）。
_TRANSPORT_APP_SERVER = "app-server"
_TRANSPORT_EXEC = "exec"

#: 仅用于 app-server ``initialize`` 的客户端标识，不参与任何版本判断。
_CLIENT_VERSION = "0.1"


def codex_home() -> Path:
    """Codex 的配置目录（``CODEX_HOME`` 优先，其次 ``~/.codex``）。"""
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex"


@register_agent
class CodexAgent(AgentDefinition):
    id = "codex"
    name = "Codex CLI"
    vendor = "OpenAI"
    description = (
        "OpenAI 的 Codex 编码智能体。通过 codex exec --json 输出 JSONL，"
        "支持沙箱模式、审批策略与线程续接。"
    )
    runtime = Runtime.CLI
    executable = "codex"
    #: 官方通过 npm 分发（包内含原生二进制）。
    provisioner = RuntimeProvisioner(
        manager="npm",
        package="@openai/codex",
        #: 安装 / 更新走包装命令：本机既有的同名可执行文件（另一个 node 装的、
        #: 用户手写的垫片）会让 npm 直接失败在 EEXIST，先把这种占位挪开。
        install_argv=npm_install_argv("@openai/codex", bin_name="codex"),
        update_argv=npm_install_argv("@openai/codex", bin_name="codex", latest=True),
        uninstall_argv=npm_cli("uninstall", "-g", "@openai/codex"),
        latest_argv=npm_cli("view", "@openai/codex", "version"),
        prepare_argv=npm_prepare_argv("@openai/codex"),
        homepage="https://developers.openai.com/codex/cli/",
    )
    api_key_env = "OPENAI_API_KEY"
    base_url_env = "OPENAI_BASE_URL"
    default_gateway = "https://api.openai.com/v1"
    default_model = "gpt-5-codex"
    protocol = "openai"
    #: app-server 的 tokenUsage 是 OpenAI Responses 形状：缓存包含在输入里。
    #: 注意 ``_app_usage`` 会输出 ``cache_write_input_tokens`` 键，但它只是别名，
    #: **不能**据此判成“分离式”（见 docs/usage-normalization-design.md）。
    usage_protocol = PROTOCOL_OPENAI_RESPONSES
    default_auth_style = "bearer"
    default_models = ["gpt-5-codex"]
    docs_url = "https://developers.openai.com/codex/cli/"
    logo = "/icons/kernels/openai.webp"
    #: OpenAI 的立体徽标是真彩色的，按原色显示才认得出来。
    logo_style = "image"
    capabilities = Capabilities(
        streaming=True,
        sessions=True,
        native_system_prompt=False,
        tools=True,
        mcp=True,
        permission_modes=True,
        working_directory=True,
        skills=True,
        # 经 model_providers.<name>.base_url 接受覆盖；记录网关支持 responses 协议。
        recording_gateway=True,
    )
    config_fields = (
        ConfigField(
            "transport",
            "接口模式",
            FieldType.SELECT,
            default=_TRANSPORT_APP_SERVER,
            options=[
                {"value": _TRANSPORT_APP_SERVER, "label": "app-server · 逐字流式（推荐）"},
                {"value": _TRANSPORT_EXEC, "label": "exec · 整段返回（兼容旧版）"},
            ],
            help=(
                "app-server 是 Codex 自带的 JSON-RPC 接口，能逐字推送回复；"
                "exec 要等整条消息完成才返回一次，旧版 Codex 不支持 app-server 时改选它。"
            ),
        ),
        ConfigField(
            "sandbox_mode",
            "沙箱模式",
            FieldType.SELECT,
            default="workspace-write",
            options=[
                {"value": "read-only", "label": "read-only · 只读"},
                {"value": "workspace-write", "label": "workspace-write · 可写工作区"},
                {"value": "danger-full-access", "label": "danger-full-access · 完全访问（危险）"},
            ],
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
                    "label": "仅工作目录 · 强制 workspace-write 沙箱",
                },
            ],
            help=(
                "「仅工作目录」把沙箱强制为 workspace-write（OS 级 landlock+seccomp），"
                "读写都限定在对话的工作目录内，且会覆盖上面单独选择的沙箱模式。"
            ),
        ),
        ConfigField(
            "full_auto",
            "全自动（不询问）",
            FieldType.BOOLEAN,
            default=False,
            help="不再逐步征求同意（等价 approval_policy=never）；能写到哪里由「沙箱模式」决定。",
        ),
        ConfigField(
            "provider_name",
            "自定义供应商名称",
            FieldType.STRING,
            advanced=True,
            placeholder="例如 openrouter、ollama",
            help=(
                "给「所选网关」生成的 model_provider 起个名字，留空则用内置名 "
                f"{_DEFAULT_PROVIDER_NAME}。只在配置了自定义网关时生效。"
            ),
        ),
        ConfigField(
            "resume_style",
            "多轮对话策略",
            FieldType.SELECT,
            default="subcommand",
            options=[
                {"value": "subcommand", "label": "续接内核侧线程（推荐）"},
                {"value": "transcript", "label": "每轮重放完整对话记录"},
                {"value": "none", "label": "仅单轮，不带上下文"},
            ],
            help=(
                "「续接内核侧线程」在 app-server 模式用 thread/resume、exec 模式用 exec resume；"
                "旧版 Codex 续接不了时可选「每轮重放完整对话记录」。"
            ),
        ),
        ConfigField(
            "reasoning_effort",
            "推理强度",
            FieldType.SELECT,
            default="",
            options=[
                {"value": "", "label": "跟随默认"},
                {"value": "minimal", "label": "minimal · 最小"},
                {"value": "low", "label": "low · 低"},
                {"value": "medium", "label": "medium · 中"},
                {"value": "high", "label": "high · 高"},
            ],
            advanced=True,
        ),
        ConfigField("extra_args", "额外命令行参数", FieldType.STRING_LIST, advanced=True),
        ConfigField("env", "环境变量", FieldType.KEYVALUE, advanced=True),
    )

    def skill_roots(self) -> list[SkillRoot]:
        """Codex 的技能根：平台技能市场 + 用户技能 + ``.system`` 系统技能。

        ``.skillhub`` 是平台自己的市场目录（其中的技能可卸载）；``.system`` 归内核所有，
        只读编目、不作为安装目标。
        """
        root = codex_home() / "skills"
        return [
            SkillRoot(str(root / _MARKET_DIRNAME), source_kind=MARKET_SOURCE),
            SkillRoot(str(root), source_kind="user"),
            SkillRoot(str(root / ".system"), source_kind="system", installable=False),
        ]

    def discover_skills(self) -> list[SkillSpec]:
        """扫描本内核声明的技能根（用户技能 + 系统技能 + 平台市场技能）。"""
        return scan_skill_roots(self.skill_roots(), type_id=self.id)

    def seed_skills(self) -> list[str]:
        """让 Codex 自己把 ``.system`` 内置技能写进 ``~/.codex/skills``。

        Codex 的内置技能不随 npm 包分发，而是首次运行 ``codex debug prompt-input``
        时才落盘。不播种的话，「技能」页只看得到市场技能。已存在则跳过（幂等）。
        """
        system_dir = codex_home() / "skills" / ".system"
        if system_dir.is_dir():
            return []
        binary = self.executable or "codex"
        if shutil.which(binary) is None:
            return []
        try:
            subprocess.run(
                [binary, "debug", "prompt-input"],
                capture_output=True,
                timeout=120,
                check=False,
            )
        except Exception:  # noqa: BLE001 - 播种失败只意味着少几个技能，不能影响安装
            return []
        return [f"{binary} debug prompt-input（播种系统技能）"] if system_dir.is_dir() else []

    def skill_mount(self, ctx: AgentContext) -> SkillMount:
        """严格仅分配：把运行环境指向只含「智能体分配到的技能」的隔离 ``CODEX_HOME``。

        ``config.toml`` 与登录态（``auth.json``）会被带过去，避免丢登录或丢自定义供应商。
        """
        root = codex_home()
        carry = [(str(root / name), name) for name in _CARRY_FILES if (root / name).is_file()]
        return SkillMount(home_name="codex-home", carry=carry)

    # -- 构造启动命令 --------------------------------------------------------

    def discover_mcp_servers(self) -> list[McpServerSpec]:
        """扫描本机 ``config.toml`` 里的 ``[mcp_servers.*]``（只读、尽力而为）。"""
        try:
            text = (codex_home() / "config.toml").read_text(encoding="utf-8")
        except OSError:
            return []
        return parse_toml_servers(text, type_id=self.id, source_kind="user")

    def mcp_mount(self, ctx: AgentContext) -> McpMount:
        """严格仅分配：隔离 ``CODEX_HOME`` 的 config.toml 里只留分配到的 MCP 服务器。

        技能物化会把本机 config.toml 原样带进隔离目录（登录态与自定义供应商都靠它），
        这里再写一遍：剥掉本机自带的 ``[mcp_servers.*]``，追加分配到的那些。
        """
        try:
            base = (codex_home() / "config.toml").read_text(encoding="utf-8")
        except OSError:
            base = ""
        content = strip_toml_table(base, table=_MCP_TABLE)
        sections = render_toml_servers(ctx.mcp_servers, table=_MCP_TABLE)
        if sections:
            content = f"{content}\n{sections}" if content else sections
        return McpMount(home_name="codex-home", files={"config.toml": content})

    def build_invocation(self, ctx: AgentContext) -> Invocation:
        if self._transport(ctx) == _TRANSPORT_EXEC:
            return self._exec_invocation(ctx)
        return self._app_server_invocation(ctx)

    @staticmethod
    def _transport(ctx: AgentContext) -> str:
        value = (ctx.option("transport") or _TRANSPORT_APP_SERVER).strip()
        return value if value in (_TRANSPORT_APP_SERVER, _TRANSPORT_EXEC) else _TRANSPORT_APP_SERVER

    def _exec_invocation(self, ctx: AgentContext) -> Invocation:
        options = ctx.options
        base = [self.executable or "codex", "exec"]
        argv = list(base)

        resume_style = options.get("resume_style") or "subcommand"
        prompt = ctx.prompt
        resuming = bool(ctx.session_id) and resume_style == "subcommand"
        if resuming:
            argv += ["resume", ctx.session_id]

        argv.append("--json")
        argv.append("--skip-git-repo-check")
        # codex exec 是非交互运行，本身不会弹审批；MCP 工具（含任务进度卡）
        # 直接执行，不需要额外的工具白名单。

        if ctx.model:
            argv += ["-m", ctx.model]

        # `codex exec` 与 `codex exec resume` 认的旗标不一样：`-s/--sandbox` 与 `-C/--cd`
        # 只挂在 `exec` 上，resume 只认配置覆盖 —— 所以恢复会话时把沙箱写成配置项
        # （工作目录本来就是进程 cwd，由 runner 设好，不必再传）。
        sandbox = self._effective_sandbox(ctx)
        if sandbox:
            if resuming:
                argv += ["-c", f'sandbox_mode="{sandbox}"']
            else:
                argv += ["-s", sandbox]
        if truthy(options.get("full_auto")):
            # 0.4x 起 `codex exec` 不再有 `--full-auto`（非交互本来就不询问），
            # 改用它原本表达的 approval_policy=never；能写到哪里由「沙箱模式」决定。
            argv += ["-c", 'approval_policy="never"']
        if ctx.workspace_dir and not resuming:
            argv += ["-C", ctx.workspace_dir]

        for key, value in self._config_overrides(ctx).items():
            argv += ["-c", f"{key}={value}"]

        argv += [str(a) for a in as_list(options.get("extra_args") or [])]

        # Codex 没有系统提示词参数，人格只能折进提示词里。
        if ctx.system_prompt:
            prompt = f"{ctx.system_prompt}\n\n---\n\n{prompt}"

        env = self.effective_env(ctx)
        env.setdefault("CODEX_DISABLE_TELEMETRY", "1")
        if ctx.home_dir:
            # 严格仅分配：Codex 只从隔离 CODEX_HOME 的 skills/ 读取技能。
            env["CODEX_HOME"] = ctx.home_dir
        return Invocation(argv=argv, env=env, cwd=ctx.workspace_dir, stdin=prompt)

    def _app_server_invocation(self, ctx: AgentContext) -> Invocation:
        """常驻的 JSON-RPC 内核：``initialize`` 之后由适配器按响应逐步下发请求。

        ``stdin`` 只放第一帧 ``initialize``；后续的 ``thread/start`` / ``turn/start``
        由 :meth:`_dispatch_app_server` 排进 ``AgentRun.send()``，运行层统一代写。
        """
        argv = [self.executable or "codex", "app-server", "--listen", "stdio://"]
        for key, value in self._config_overrides(ctx).items():
            argv += ["-c", f"{key}={value}"]
        argv += [str(a) for a in as_list(ctx.option("extra_args") or [])]

        env = self.effective_env(ctx)
        env.setdefault("CODEX_DISABLE_TELEMETRY", "1")
        if ctx.home_dir:
            env["CODEX_HOME"] = ctx.home_dir
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "coda",
                    "title": "Coda",
                    "version": _CLIENT_VERSION,
                }
            },
        }
        return Invocation(
            argv=argv,
            env=env,
            cwd=ctx.workspace_dir,
            stdin=json.dumps(initialize, ensure_ascii=False) + "\n",
            stdin_hold=True,
        )

    def _config_overrides(self, ctx: AgentContext) -> dict[str, str]:
        overrides: dict[str, str] = {}
        # Codex 默认把 OTEL 指标导出到它的分析后端（ab.chatgpt.com）。内网 / 国内网络
        # 连不上时，这一步会在**退出前**干等满 10 秒超时 —— 表现为「回复完要等一会儿才结束」。
        # 关掉指标导出即可（`otel.metrics_exporter` 的取值：none / statsig / otlp-http / otlp-grpc）；
        # 认不出这个键的旧版 Codex 会直接忽略它（`-c` 的未知键默认不报错），
        # 需要自己接 OTEL 的可以在「额外命令行参数」里再覆盖回来。
        overrides["otel.metrics_exporter"] = '"none"'
        if ctx.gateway_url:
            # 新版 Codex 不再认 ``OPENAI_BASE_URL``：内置 provider 拿着它也会直奔
            # api.openai.com。自定义网关必须经一个具名 model_provider 才落得到用户地址上。
            provider = (ctx.option("provider_name") or "").strip() or _DEFAULT_PROVIDER_NAME
            env_key = self.api_key_env or "OPENAI_API_KEY"
            overrides["model_provider"] = provider
            overrides[f"model_providers.{provider}.name"] = f'"{provider}"'
            overrides[f"model_providers.{provider}.base_url"] = f'"{ctx.gateway_url}"'
            overrides[f"model_providers.{provider}.env_key"] = f'"{env_key}"'
            # 不下发 wire_api：新版 Codex 只剩 responses、旧版默认 chat ——
            # 写死任何一个都会在另一个版本上直接报错，交给 Codex 按自身版本决定。
        effort = ctx.option("reasoning_effort")
        if effort:
            overrides["model_reasoning_effort"] = f'"{effort}"'
        # 原生压缩：把平台上限 / 阈值翻成 Codex 的 model_context_window /
        # model_auto_compact_token_limit；Codex 自己会在 90% 上限内再夹取。
        overrides.update(
            codex_compaction_overrides(ctx.context_limit_tokens, ctx.context_trigger_tokens)
        )
        return overrides

    def resume_supported(self, ctx: AgentContext) -> bool:
        return (ctx.option("resume_style") or "subcommand") == "subcommand"

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
            # 脏行（合法 JSON 但不是对象）不能把整轮运行带崩，按调试信息丢掉即可。
            yield log(line, "debug")
            return
        if self._transport(run.ctx) == _TRANSPORT_EXEC:
            yield from self._dispatch(payload, run)
        else:
            yield from self._dispatch_app_server(payload, run)

    def _dispatch(self, payload: dict[str, Any], run: AgentRun) -> Iterable[AgentEvent]:
        if "msg" in payload and isinstance(payload["msg"], dict):  # 旧版事件结构
            payload = {**payload["msg"], "type": payload["msg"].get("type")}

        kind = payload.get("type")

        if kind == "thread.started":
            thread_id = payload.get("thread_id")
            if thread_id:
                run.state["thread"] = thread_id
                yield session(thread_id)
            return

        if kind in ("item.started", "item.completed", "item.updated"):
            yield from self._from_item(payload.get("item") or {}, kind, run)
            return

        if kind == "turn.completed":
            if payload.get("usage"):
                yield usage(**payload["usage"])
            return

        if kind == "turn.failed":
            yield error(flatten_content(payload.get("error") or "本轮执行失败"))
            run.state["is_error"] = True
            return

        if kind == "error":
            yield error(payload.get("message") or "Codex 报错")
            run.state["is_error"] = True
            return

        if kind == "agent_message":  # 旧版事件结构
            if payload.get("message"):
                yield text_delta(payload["message"])
            return

    def _from_item(self, item: dict[str, Any], kind: str, run: AgentRun) -> Iterable[AgentEvent]:
        item_type = item.get("type")
        item_id = item.get("id")

        if item_type == "agent_message":
            # item.completed 携带完整文本，增量则通过 agent_message_delta 到达。
            if kind == "item.completed" and item.get("text"):
                if not run.state.get("saw_delta"):
                    yield text_delta(item["text"])
            return

        if item_type == "agent_message_delta":
            if item.get("delta"):
                run.state["saw_delta"] = True
                yield text_delta(item["delta"])
            return

        if item_type in ("reasoning", "reasoning_delta"):
            text = item.get("text") or item.get("delta")
            if text and kind != "item.started":
                yield thinking_delta(text)
            return

        if item_type in ("command_execution", "local_shell_call", "shell"):
            command = item.get("command")
            if kind == "item.started":
                yield tool_call("shell", item_id, command=flatten_content(command))
            elif kind == "item.completed":
                yield tool_result(
                    item_id,
                    flatten_content(item.get("aggregated_output") or item.get("output") or ""),
                    bool(item.get("exit_code")),
                )
            return

        if item_type in ("file_change", "patch_apply", "apply_patch"):
            if kind == "item.completed":
                yield tool_call("patch", item_id, changes=item.get("changes") or item)
            return

        if item_type in ("mcp_tool_call", "web_search", "todo_list"):
            if kind == "item.completed":
                yield tool_call(item_type, item_id, **{k: v for k, v in item.items() if k != "type"})
            return

    # -- app-server（JSON-RPC）解析 ----------------------------------------

    def _dispatch_app_server(
        self, payload: dict[str, Any], run: AgentRun
    ) -> Iterable[AgentEvent]:
        """解析 ``codex app-server`` 的一帧。

        帧分三类：带 ``id`` 的响应（initialize / thread / turn 的应答）、
        带 ``id`` 与 ``method`` 的服务端反向请求（审批、提问）、
        以及只有 ``method`` 的通知（回复增量、工具进度、token 用量…）。
        """
        state = run.state.setdefault("appserver", {})
        state.setdefault("text_items", set())
        state.setdefault("reasoning_items", set())

        method = payload.get("method")
        if method and "id" in payload:
            # 没有人类在终端前：审批 / 提问一律按「拒绝」处理，否则本轮会一直等一个
            # 永远不会到来的答复，直到运行超时。
            yield from self._answer_server_request(payload, run)
            return

        if not method and "id" in payload and ("result" in payload or "error" in payload):
            yield from self._handle_app_response(payload, run)
            return

        if not method:
            return
        params = payload.get("params") or {}

        if method == "thread/started":
            yield from self._app_thread_started(params.get("thread") or {}, run)
            return

        if method == "item/agentMessage/delta":
            delta = params.get("delta")
            if delta:
                state["text_items"].add(params.get("itemId") or "")
                yield text_delta(delta)
            return

        if method in ("item/reasoning/textDelta", "item/reasoning/summaryTextDelta"):
            delta = params.get("delta")
            if delta:
                state["reasoning_items"].add(params.get("itemId") or "")
                yield thinking_delta(delta)
            return

        if method == "item/started":
            yield from self._app_item(params.get("item") or {}, "started", run)
            return

        if method == "item/completed":
            yield from self._app_item(params.get("item") or {}, "completed", run)
            return

        if method == "thread/tokenUsage/updated":
            counters = self._app_usage(params.get("tokenUsage") or {})
            if counters:
                yield usage(**counters)
                window = context_tokens(counters, protocol=PROTOCOL_OPENAI_RESPONSES)
                if window:
                    yield context_usage(window)
            # 模型真正的上下文窗口：上限设为「自动」时平台跟随它。
            model_window = (params.get("tokenUsage") or {}).get("modelContextWindow")
            if (
                isinstance(model_window, (int, float))
                and not isinstance(model_window, bool)
                and model_window >= 1
            ):
                yield context_window(int(model_window))
            return

        if method == "thread/compacted":
            # 官方 schema 标为 Deprecated（改用 item/completed 的 contextCompaction），
            # 但老版本 app-server 仍会发；两个入口都认，避免内核升级前后漏接。
            # 载荷只有 threadId / turnId，没有压缩前后的 token 数。
            yield context_compacted()
            return

        if method == "turn/completed":
            yield from self._app_turn_completed(params, run)
            return

        if method == "error":
            err = params.get("error") or {}
            message = err.get("message") or "Codex 报错"
            if params.get("willRetry"):
                # 会自动重试的错误只是提醒，不该把整轮运行标成失败。
                yield log(f"{message}（Codex 将自动重试）", "warn")
            else:
                run.state["is_error"] = True
                yield error(message)
            return

        if method in ("warning", "guardianWarning", "configWarning"):
            message = params.get("summary") or params.get("message") or method
            yield log(flatten_content(message), "warn")
            return

    def _handle_app_response(
        self, payload: dict[str, Any], run: AgentRun
    ) -> Iterable[AgentEvent]:
        rid = payload.get("id")
        err = payload.get("error")
        if err:
            run.state["is_error"] = True
            yield error(f"Codex 接口返回错误：{err.get('message') or '未知错误'}")
            run.mark_done()
            return

        result = payload.get("result") or {}
        if rid == 1:
            # initialize 完成：开新线程，或按快照恢复上一次的线程。
            resume_style = run.ctx.option("resume_style") or "subcommand"
            params = self._thread_params(run.ctx)
            if run.ctx.session_id and resume_style == "subcommand":
                params["threadId"] = run.ctx.session_id
                self._send_app(run, 2, "thread/resume", params)
            else:
                self._send_app(run, 2, "thread/start", params)
            return

        if rid == 2:
            yield from self._app_thread_started(result.get("thread") or {}, run)
            state = run.state.setdefault("appserver", {})
            thread_id = state.get("thread")
            if not thread_id:
                run.state["is_error"] = True
                yield error("Codex 没能建立会话线程")
                run.mark_done()
                return
            self._send_app(run, 3, "turn/start", self._turn_params(run, thread_id))

    @staticmethod
    def _thread_params(ctx: AgentContext) -> dict[str, Any]:
        # approvalPolicy=never：MCP 工具（含平台内部的任务进度卡）不会被逐次拦截。
        # 不为此单独放宽沙箱；能写到哪里仍由 sandbox_mode / 「仅工作目录」决定。
        params: dict[str, Any] = {"approvalPolicy": "never"}
        if ctx.workspace_dir:
            params["cwd"] = ctx.workspace_dir
        if ctx.model:
            params["model"] = ctx.model
        sandbox = CodexAgent._effective_sandbox(ctx)
        if sandbox:
            params["sandbox"] = sandbox
        return params

    @staticmethod
    def _effective_sandbox(ctx: AgentContext) -> str | None:
        """本次运行生效的沙箱模式：「仅工作目录」覆盖用户单独选择的沙箱模式。

        workspace-write 由内核用 OS 级隔离（Linux 上是 landlock+seccomp）保证
        读写都出不了工作目录，比应用层约束更强，所以直接以它为准。
        """
        if workspace_only(ctx):
            return "workspace-write"
        return ctx.option("sandbox_mode") or None

    @staticmethod
    def _turn_params(run: AgentRun, thread_id: str) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": run.ctx.prompt or ""}],
        }
        effort = run.ctx.option("reasoning_effort")
        if effort:
            params["effort"] = effort
        return params

    @staticmethod
    def _send_app(run: AgentRun, request_id: int, method: str, params: dict[str, Any]) -> None:
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        run.send(json.dumps(payload, ensure_ascii=False) + "\n")

    @staticmethod
    def _send_app_raw(run: AgentRun, payload: dict[str, Any]) -> None:
        run.send(json.dumps(payload, ensure_ascii=False) + "\n")

    def _answer_server_request(
        self, payload: dict[str, Any], run: AgentRun
    ) -> Iterable[AgentEvent]:
        """自动拒绝服务端反向请求，让本轮继续跑完而不是挂住。"""
        method = payload.get("method")
        request_id = payload.get("id")
        result: dict[str, Any] | None
        if method == "item/commandExecution/requestApproval":
            result = {"decision": "decline"}
        elif method == "item/fileChange/requestApproval":
            result = {"decision": "decline"}
        elif method == "item/tool/requestUserInput":
            result = {"answers": {}}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "decline", "content": None}
        elif method in ("applyPatchApproval", "execCommandApproval"):
            # 旧版（v1）审批协议：abort 让对方停手，等下一轮用户指令。
            result = {"decision": "abort"}
        else:
            result = None

        if result is None:
            self._send_app_raw(
                run,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Coda 不支持 {method}"},
                },
            )
        else:
            self._send_app_raw(run, {"jsonrpc": "2.0", "id": request_id, "result": result})
        yield log(f"Codex 请求「{method}」，平台没有人工确认入口，已自动拒绝", "warn")

    def _app_thread_started(self, thread: dict[str, Any], run: AgentRun) -> Iterable[AgentEvent]:
        state = run.state.setdefault("appserver", {})
        thread_id = thread.get("id")
        if not thread_id:
            return
        state["thread"] = thread_id
        if state.get("session_emitted") == thread_id:
            return
        state["session_emitted"] = thread_id
        yield session(thread_id)

    def _app_turn_completed(
        self, params: dict[str, Any], run: AgentRun
    ) -> Iterable[AgentEvent]:
        turn = params.get("turn") or {}
        status = turn.get("status")
        if status == "failed":
            err = turn.get("error") or {}
            run.state["is_error"] = True
            yield error(err.get("message") or "本轮执行失败")
        # 到这里本轮内容已经全部落盘（含 item/completed 的兜底文本）。
        run.mark_done()

    def _app_item(self, item: dict[str, Any], kind: str, run: AgentRun) -> Iterable[AgentEvent]:
        """把 app-server 的 ``item`` 条目翻译成规范化事件（条目字段是 camelCase）。"""
        item_type = item.get("type")
        item_id = item.get("id")
        state = run.state.setdefault("appserver", {})
        state.setdefault("text_items", set())
        state.setdefault("reasoning_items", set())

        if item_type == "contextCompaction":
            # 内核原生压缩（自动或手动 /compact）完成的条目。只在 completed 上报一次：
            # started 时压缩还没落地，此时让快照失效没有意义。
            if kind == "completed":
                yield context_compacted()
            return

        if item_type == "agentMessage":
            # 有逐字增量时不再补发整段，避免内容翻倍。
            if kind == "completed" and item.get("text") and item_id not in state["text_items"]:
                yield text_delta(item["text"])
            return

        if item_type == "reasoning":
            if kind == "completed" and item_id not in state["reasoning_items"]:
                parts = list(item.get("content") or []) + list(item.get("summary") or [])
                text = "\n".join(str(part) for part in parts if part)
                if text:
                    yield thinking_delta(text)
            return

        if item_type == "commandExecution":
            if kind == "started":
                yield tool_call("shell", item_id, command=item.get("command") or "")
            elif kind == "completed":
                is_error = item.get("status") == "failed" or item.get("exitCode") not in (0, None)
                yield tool_result(item_id, item.get("aggregatedOutput") or "", is_error)
            return

        if item_type == "fileChange":
            changes = item.get("changes") or []
            if kind == "started":
                yield tool_call("patch", item_id, changes=changes)
            elif kind == "completed":
                yield tool_result(
                    item_id,
                    json.dumps(changes, ensure_ascii=False) if changes else "",
                    item.get("status") == "failed",
                )
            return

        if item_type == "mcpToolCall":
            if kind == "started":
                yield tool_call(
                    item.get("tool") or "mcp",
                    item_id,
                    server=item.get("server"),
                    arguments=item.get("arguments"),
                )
            elif kind == "completed":
                err = item.get("error") or {}
                result = item.get("result")
                if err:
                    output = err.get("message") or json.dumps(err, ensure_ascii=False)
                elif result is None:
                    output = ""
                else:
                    output = json.dumps(result, ensure_ascii=False)
                yield tool_result(
                    item_id, output, bool(err) or item.get("status") == "failed"
                )
            return

        if item_type == "dynamicToolCall":
            if kind == "started":
                yield tool_call(
                    item.get("tool") or "tool", item_id, arguments=item.get("arguments")
                )
            elif kind == "completed":
                output = json.dumps(item.get("contentItems") or [], ensure_ascii=False)
                yield tool_result(
                    item_id, output, not item.get("success", True)
                )
            return

        if item_type == "webSearch":
            if kind == "started":
                yield tool_call("web_search", item_id, query=item.get("query"))
            elif kind == "completed":
                yield tool_result(
                    item_id, json.dumps(item.get("results") or [], ensure_ascii=False), False
                )
            return

        if item_type == "functionCallOutput" and kind == "completed":
            yield tool_result(item_id, flatten_content(item.get("output") or ""), False)
            return

    @staticmethod
    def _app_usage(token_usage: dict[str, Any]) -> dict[str, Any]:
        last = token_usage.get("last") or token_usage.get("total") or {}
        if not last:
            return {}
        return {
            "input_tokens": last.get("inputTokens"),
            "cached_input_tokens": last.get("cachedInputTokens"),
            "cache_write_input_tokens": last.get("cacheWriteInputTokens"),
            "output_tokens": last.get("outputTokens"),
            "reasoning_output_tokens": last.get("reasoningOutputTokens"),
            "total_tokens": last.get("totalTokens"),
        }
