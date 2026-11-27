"""Anthropic Claude Code CLI 适配器。"""
from __future__ import annotations

import json
import os
import uuid
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
    usage_raw,
)
from ..mcp import McpMount, McpServerSpec, parse_json_servers, to_json_config
from ..registry import register_agent
from ..skills import (
    MARKET_SOURCE,
    SkillMount,
    SkillRoot,
    SkillSpec,
    scan_skill_roots,
)
from ..metering import PROTOCOL_ANTHROPIC
from .util import (
    as_list,
    claude_compaction_env,
    context_tokens,
    flatten_content,
    npm_cli,
    npm_install_argv,
    npm_prepare_argv,
    truthy,
    workspace_only,
)


# Claude Code 拒绝以 root 身份使用 bypassPermissions（"cannot be used with root/sudo
# privileges for security reasons"），除非进程声明自己处在一个「有意的沙箱」里 —— 这是内核
# 自带、官方容器场景使用的逃生口 ``IS_SANDBOX=1``。本控制台可能由面板/容器以 root 托管，用户又是
# 显式勾了 bypassPermissions（无人值守），所以这里替它补上；没勾 bypassPermissions 的节点不受影响。

#: ``compact_boundary`` 事件里 ``compact_metadata`` 的字段白名单（官方 stream-json 协议，
#: 键为蛇形命名）。只透传面板与运行层用得上的键：内部遥测字段（precomputed、
#: pre_compact_* 等）不进事件流，避免 payload 无限膨胀。
_COMPACT_FIELDS = (
    "trigger",
    "pre_tokens",
    "post_tokens",
    "messages_summarized",
    "cumulative_dropped_tokens",
    "duration_ms",
)


def _is_root() -> bool:
    """当前进程是否以 root 身份运行（real uid，与内核的 ``process.getuid()`` 判定一致）。"""
    getuid = getattr(os, "getuid", None)
    return bool(getuid) and getuid() == 0


def _root_bypass_escape(ctx: AgentContext) -> str | None:
    """root + bypassPermissions 时要给内核声明的 ``IS_SANDBOX`` 值；不需要时返回 ``None``。

    用户在自己的 ``env`` 里显式配了 ``IS_SANDBOX`` 就以用户为准。
    """
    if (ctx.option("permission_mode") or "acceptEdits") != "bypassPermissions":
        return None
    if not _is_root():
        return None
    return str((ctx.option("env") or {}).get("IS_SANDBOX", "1"))


#: Claude Code 自己的系统级配置文件，按顺序合并（后者覆盖前者）。
_SYSTEM_SETTINGS_FILES = ("settings.json", "settings.local.json")

#: Claude Code 自带的内置插件（它们携带 bundled 技能），严格模式下必须显式关闭。
_BUILTIN_PLUGIN_IDS = ("cc-plugin-agents-md@builtin", "cc-plugin-plugin-authoring@builtin")

#: ``disableBundledSkills`` 关不掉、需要逐个用 ``skillOverrides`` 关闭的内置技能。
_RESIDUAL_BUNDLED_SKILLS = ("doctor",)

#: 已安装插件内部的技能深度：``<插件根>/skills/<技能>/SKILL.md``。
_PLUGIN_SKILL_DEPTH = 2

#: Claude Code 记录「已安装插件」的清单（``plugins/installed_plugins.json``）。
#: ``plugins/marketplaces/<市场>/`` 只是**市场目录的克隆**（可浏览、未安装），
#: 真正的已安装插件在 ``plugins/cache/<市场>/<插件>/<版本>/`` 下，由这份清单指向。
_PLUGIN_STATE_FILE = "installed_plugins.json"

#: 平台「技能市场」安装目录（``<config>/skills/.skillhub/<skill>``），可卸载。
_MARKET_DIRNAME = ".skillhub"

#: 分配到的 MCP 服务器写成的独立配置文件（隔离目录内），配合 ``--mcp-config`` 使用。
_MCP_FILE = "mcp.json"

#: 关闭「系统提示词快照」的旗标：Claude Code 默认把系统提示词在会话**第一次请求**时
#: 拍成快照，之后每次 ``--resume`` 都原样重放，**即使后续启动传了不同的人格文本**。
#: 平台每轮运行都按智能体的**当前**人格重新拼 ``--system-prompt`` / ``--append-system-prompt``，
#: 用户改了人格期望立刻生效；快照机制与之冲突，因此有注入人格时必须一并关掉。
#: 关掉后每次请求都重新渲染提示词（该旗标的官方语义就是「迭代提示词文本」场景）。
#: 旗标自 2.1.300（2026-10）起存在，早于它的版本不认这个参数会拒绝启动；
#: 但平台只管理 2.1.3xx 的官方内核，遇见老内核时可让用户在「额外命令行参数」里自行处理。
_SNAPSHOT_OFF_ARGS = ("--system-prompt-snapshot", "off")


def config_dir(home: str | Path | None = None) -> Path:
    """Claude Code 的配置目录（默认 ``~/.claude``）。"""
    base = Path(home).expanduser() if home else Path.home()
    return base / ".claude"


def user_config_file(home: str | Path | None = None) -> Path:
    """Claude Code 的用户级配置文件（``~/.claude.json``）。

    ``CLAUDE_CONFIG_DIR`` 会把配置目录整个挪走，这份文件也跟着走；
    因此扫描 MCP 服务器时要认这个环境变量，否则「界面看到的」和「CLI 用的」会对不上。
    """
    if home:
        return Path(home).expanduser() / ".claude.json"
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(override).expanduser() if override else Path.home()
    return base / ".claude.json"


def installed_plugin_paths(home: str | Path | None = None) -> list[tuple[str, str]] | None:
    """已安装插件：``(插件标识, 安装目录)`` 列表；清单缺失/损坏时返回 ``None``。

    Claude Code 把「插件市场目录」（``plugins/marketplaces/<市场>/``，可浏览的货架）
    与「已安装插件」（``plugins/cache/<市场>/<插件>/<版本>/``）分开放，两者都带
    ``skills/`` 子目录。只有后者是用户真正装上的，编目时只能看后者。

    插件标识取自清单的键（``<插件>@<市场>``）：不同插件可能各带一个同名技能
    （如 discord / telegram 各有一个 ``access``），它用作技能目录名的前缀，
    让它们能同时存在、也能被分别调用。

    **必须区分「清单说没装插件」（返回空列表）和「没有清单」（返回 ``None``）**：
    前者是权威结论（缓存里可能还留着已卸载插件的目录，不能再列出来），
    后者才需要按缓存布局兜底扫描（旧版本内核没有这份清单）。
    """
    base = config_dir(home) / "plugins"
    try:
        data = json.loads((base / _PLUGIN_STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entries = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return None

    found: list[tuple[str, str]] = []
    for plugin_id, records in entries.items():
        plugin_name = str(plugin_id).split("@", 1)[0].strip() or "plugin"
        for record in records if isinstance(records, list) else [records]:
            if not isinstance(record, dict):
                continue
            path = record.get("installPath")
            if isinstance(path, str) and path.strip():
                found.append((plugin_name, path.strip()))
    # 同一个插件可能有 user / project 两条安装记录：按「插件 + 路径」去重。
    return list(dict.fromkeys(found))


def _plugin_dirs_in_cache(cache: Path) -> list[tuple[str, Path]]:
    """缓存布局里的 ``(插件名, 插件根)``：``cache/<市场>/<插件>/<版本>/``（尽力而为）。

    只在旧版本内核没有 ``installed_plugins.json`` 时兜底使用。取最新的版本目录，
    因为同一个插件可能同时存在多个版本。
    """
    found: list[tuple[str, Path]] = []
    try:
        markets = sorted(p for p in cache.iterdir() if p.is_dir())
    except OSError:
        return []
    for market in markets:
        try:
            plugins = sorted(p for p in market.iterdir() if p.is_dir())
        except OSError:
            continue
        for plugin in plugins:
            try:
                versions = sorted(p for p in plugin.iterdir() if p.is_dir())
            except OSError:
                continue
            if not versions:
                continue
            # 版本目录名按字典序排，取最后一个（1.10.0 > 1.9.0 在这里不严格，但够用）。
            found.append((plugin.name, versions[-1]))
    return found


def read_system_settings(home: str | Path | None = None) -> dict[str, Any]:
    """读取 Claude Code 安装目录下的系统级配置，供界面与运行时对齐真实默认值。"""
    base = Path(home).expanduser() if home else Path.home()
    merged: dict[str, Any] = {}
    merged_env: dict[str, Any] = {}
    for name in _SYSTEM_SETTINGS_FILES:
        try:
            data = json.loads((base / ".claude" / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 配置文件缺失或损坏时按「未配置」处理：这是尽力而为的元数据读取，
            # 绝不能因为本机没有 settings.json 就阻断智能体列表或一次真实运行。
            continue
        if not isinstance(data, dict):
            continue
        # env 需要逐键合并（settings.local.json 通常只覆盖其中几项）。
        if isinstance(data.get("env"), dict):
            merged_env.update(data["env"])
        merged.update({k: v for k, v in data.items() if k != "env"})
    if merged_env:
        merged["env"] = merged_env
    return merged


@register_agent
class ClaudeCodeAgent(AgentDefinition):
    id = "claude-code"
    name = "Claude Code"
    vendor = "Anthropic"
    description = (
        "Anthropic 官方编码智能体。以 CLI 子进程运行，支持完整工具调用、"
        "会话续接与原生系统提示词。"
    )
    runtime = Runtime.CLI
    executable = "claude"
    #: 官方通过 npm 分发；平台代劳安装/更新/卸载时用的就是这套命令。
    provisioner = RuntimeProvisioner(
        manager="npm",
        package="@anthropic-ai/claude-code",
        #: 安装 / 更新走包装命令：本机既有的同名可执行文件（另一个 node 装的、
        #: 用户手写的垫片）会让 npm 直接失败在 EEXIST，先把这种占位挪开。
        install_argv=npm_install_argv("@anthropic-ai/claude-code", bin_name="claude"),
        update_argv=npm_install_argv("@anthropic-ai/claude-code", bin_name="claude", latest=True),
        uninstall_argv=npm_cli("uninstall", "-g", "@anthropic-ai/claude-code"),
        latest_argv=npm_cli("view", "@anthropic-ai/claude-code", "version"),
        prepare_argv=npm_prepare_argv("@anthropic-ai/claude-code"),
        homepage="https://docs.claude.com/en/docs/claude-code/setup",
    )
    api_key_env = "ANTHROPIC_AUTH_TOKEN"
    base_url_env = "ANTHROPIC_BASE_URL"
    default_gateway = "https://api.anthropic.com"
    default_model = "sonnet"
    protocol = "anthropic"
    #: 原始 usage 的协议：input 与缓存分离（真实样本已验证）。
    usage_protocol = PROTOCOL_ANTHROPIC
    default_auth_style = "x-api-key"
    default_models = ["sonnet"]
    docs_url = "https://docs.claude.com/en/docs/claude-code"
    logo = "/icons/kernels/claude.webp"
    #: Claude 的橙色放射标是真彩色的，按原色显示才认得出来。
    logo_style = "image"
    capabilities = Capabilities(
        streaming=True,
        sessions=True,
        native_system_prompt=True,
        tools=True,
        mcp=True,
        permission_modes=True,
        working_directory=True,
        skills=True,
        # 接受 ANTHROPIC_BASE_URL 覆盖，协议（Anthropic Messages）在记录网关支持范围内。
        recording_gateway=True,
    )
    config_fields = (
        ConfigField(
            "permission_mode",
            "权限模式",
            FieldType.SELECT,
            default="acceptEdits",
            options=[
                {"value": "acceptEdits", "label": "acceptEdits · 自动接受文件修改"},
                {"value": "auto", "label": "auto · 自动判断"},
                {"value": "manual", "label": "manual · 手动确认"},
                {"value": "plan", "label": "plan · 只做计划不改动"},
                {"value": "dontAsk", "label": "dontAsk · 不询问，直接拒绝需确认的操作"},
                {"value": "bypassPermissions", "label": "bypassPermissions · 跳过全部权限检查"},
            ],
            help=(
                "bypassPermissions 会让智能体无人值守地执行操作，请只在沙箱环境中使用；"
                "以 root 运行时本适配器会自动设置 IS_SANDBOX=1，否则 Claude Code 会拒绝启动。"
            ),
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
                    "label": "仅工作目录 · 文件工具不可越界读写",
                },
            ],
            help=(
                "「仅工作目录」把 Read/Grep/Glob/Edit/Write 锁定在对话的工作目录内"
                "（每次运行注入权限设置，智能体无法在工作目录里自写配置来解除）。"
                "注意：这是内核应用层约束，Bash 命令仍可能经 shell 访问其他路径；"
                "需要强保证时请在容器等环境级隔离中运行。"
            ),
        ),
        ConfigField(
            "system_prompt_mode",
            "人格注入方式",
            FieldType.SELECT,
            default="append",
            options=[
                {"value": "append", "label": "追加到 Claude Code 原有系统提示词之后"},
                {"value": "replace", "label": "完全替换系统提示词"},
            ],
        ),
        ConfigField("partial_messages", "逐字流式输出", FieldType.BOOLEAN, default=True),
        ConfigField(
            "autocompact",
            "原生自动压缩窗口",
            FieldType.STRING,
            advanced=True,
            placeholder="auto 或 ≥100k 的 token 数（如 200k / 200000）",
            help=(
                "透传给 Claude Code 的 --autocompact：上下文逼近该窗口时由内核自己"
                "把旧对话折叠成摘要（压缩后平台会收到 compact_boundary 事件并提示）。"
                "留空 = 用内核默认；填 auto = 按模型自动选择。注意与平台「上下文」"
                "面板里的压缩是两套机制：平台压缩会重开会话，内核压缩在会话内完成。"
            ),
        ),
        ConfigField(
            "allowed_tools",
            "工具白名单",
            FieldType.STRING_LIST,
            advanced=True,
            placeholder="例如 Bash(git *) Edit Read",
        ),
        ConfigField("disallowed_tools", "工具黑名单", FieldType.STRING_LIST, advanced=True),
        ConfigField("extra_args", "额外命令行参数", FieldType.STRING_LIST, advanced=True),
        ConfigField("env", "环境变量", FieldType.KEYVALUE, advanced=True),
    )

    def system_defaults(self) -> dict[str, Any]:
        """从 Claude Code 的真实配置（进程环境变量 + ~/.claude/settings*.json）取默认值。

        这样界面里的网关地址、模型与凭据提示就会反映本机真实配置，而不是适配器里
        写死的占位值；运行时也用同一份配置，避免「界面一套、CLI 另一套」。
        """
        settings = read_system_settings()
        file_env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
        file_env = {str(k): str(v) for k, v in file_env.items() if v not in (None, "")}

        def pick(name: str) -> str | None:
            # 进程环境变量优先级高于配置文件（与 Claude Code 自身一致）。
            return os.environ.get(name) or file_env.get(name) or None

        gateway = pick("ANTHROPIC_BASE_URL")
        model = pick("ANTHROPIC_MODEL") or (str(settings["model"]) if settings.get("model") else None)
        api_key = pick("ANTHROPIC_AUTH_TOKEN") or pick("ANTHROPIC_API_KEY")

        defaults: dict[str, Any] = {}
        if gateway:
            defaults["gateway_url"] = gateway
        if model:
            defaults["model"] = model
        if api_key:
            defaults["api_key"] = api_key
        return defaults

    def skill_roots(self) -> list[SkillRoot]:
        """Claude Code 的技能根：平台技能市场 + 个人技能 + **已安装**插件的技能。

        「技能放在哪」只在这里声明（框架据此编目与安装）；``.skillhub`` 是平台自己的
        技能市场目录，装进来的技能（``source_kind=market``）可以卸载，其余只读编目。

        插件只认 ``installed_plugins.json`` 里登记过的安装目录（``plugins/cache/...``）：
        ``plugins/marketplaces/<市场>/`` 是市场目录的克隆，里面的技能**没有安装**，
        把它们编进技能页会让用户以为能分配、还能「删除」——删的是市场货架本身。
        清单缺失（旧版本内核）时才退回按缓存目录布局扫描，仍然不碰市场克隆。

        每个已安装插件单独成根，并以插件名作命名空间：不同插件可能各带一个同名技能
        （discord / telegram 各有一个 ``access``），加前缀后变成 ``discord-access`` /
        ``telegram-access``，既能同时存在，也能被内核分别调用。
        """
        root = config_dir()
        roots = [
            SkillRoot(str(root / "skills" / _MARKET_DIRNAME), source_kind=MARKET_SOURCE),
            SkillRoot(str(root / "skills"), source_kind="user"),
        ]
        installed = installed_plugin_paths()
        if installed is not None:
            # 清单存在就是权威结论：即使为空（一个插件都没装）也不回退扫缓存，
            # 否则会列出已卸载插件留在缓存里的残留。
            roots += [
                SkillRoot(
                    path,
                    source_kind="plugin",
                    installable=False,
                    max_depth=_PLUGIN_SKILL_DEPTH,
                    namespace=plugin_name,
                )
                for plugin_name, path in installed
            ]
        else:
            # 旧版本内核没有清单：按缓存布局兜底扫描（cache/<市场>/<插件>/<版本>/skills/<技能>）。
            # 此时插件名仍在路径里，逐插件声明根并同样加命名空间前缀。
            roots += [
                SkillRoot(
                    str(plugin_dir),
                    source_kind="plugin",
                    installable=False,
                    max_depth=_PLUGIN_SKILL_DEPTH,
                    namespace=plugin_name,
                )
                for plugin_name, plugin_dir in _plugin_dirs_in_cache(root / "plugins" / "cache")
            ]
        return roots

    def discover_skills(self) -> list[SkillSpec]:
        """扫描本内核声明的技能根（个人技能 + 插件技能 + 平台市场技能）。"""
        return scan_skill_roots(self.skill_roots(), type_id=self.id)

    def skill_mount(self, ctx: AgentContext) -> SkillMount:
        """严格仅分配：只让内核看到智能体分配到的技能。

        做法是把运行环境指向一个隔离的配置目录（只放分配到的技能 + 本机
        ``settings*.json``），并关闭内核自带的 bundled 技能与内置插件。
        """
        notes: list[str] = []
        if ctx.workspace_dir:
            extra = Path(ctx.workspace_dir) / ".claude" / "skills"
            if extra.is_dir() and any(extra.iterdir()):
                notes.append(
                    f"工作目录自带的 {extra} 会被内核额外加载，这些技能不在智能体分配范围内"
                )
        root = config_dir()
        settings_patch: dict[str, Any] = {
            "enabledPlugins": {plugin_id: False for plugin_id in _BUILTIN_PLUGIN_IDS},
            "disableBundledSkills": True,
            "skillOverrides": {slug: "off" for slug in _RESIDUAL_BUNDLED_SKILLS},
        }
        if workspace_only(ctx):
            # 「仅工作目录」：文件工具的读写都锁在对话工作目录内。
            # 这三个设置从 user 级配置读入后，工作目录里的 project settings 无法放宽
            # （内核只认 user/--settings/managed 来源，且任一来源为 true 即生效）。
            settings_patch["permissions"] = {
                "blockReadsOutsideWorkingDirectories": True,
                "blockFileToolReadsOutsideWorkingDirectories": True,
                "blockWritesOutsideWorkingDirectories": True,
            }
            notes.append("已限制文件访问范围：文件工具只能读写本对话的工作目录")
        return SkillMount(
            home_name="claude-home",
            carry=[(str(root / name), name) for name in _SYSTEM_SETTINGS_FILES],
            patch={"settings.json": settings_patch},
            notes=notes,
        )

    def skill_invocation(self, tool_name: str, tool_input: dict[str, Any]) -> str | None:
        """Claude Code 通过内置的 ``Skill`` 工具调用技能，参数 ``skill`` 是技能目录名。"""
        if tool_name != "Skill":
            return None
        name = tool_input.get("skill") or tool_input.get("command") or tool_input.get("name")
        return str(name).strip() or None

    def discover_mcp_servers(self) -> list[McpServerSpec]:
        """扫描用户级配置 ``~/.claude.json`` 里的 ``mcpServers``（只读、尽力而为）。"""
        try:
            raw = json.loads(user_config_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return parse_json_servers(raw, type_id=self.id, source_kind="user")

    def mcp_mount(self, ctx: AgentContext) -> McpMount:
        """把分配到的服务器写成隔离目录里的一份配置，并关掉其它所有 MCP 配置。

        ``--strict-mcp-config`` 与 ``--mcp-config`` 一起用时，Claude Code **只**认这份文件：
        本机 ``~/.claude.json``、工作目录的 ``.mcp.json`` 都不会被加载。
        """
        notes: list[str] = []
        if ctx.workspace_dir:
            project = Path(ctx.workspace_dir) / ".mcp.json"
            if project.is_file():
                notes.append(
                    f"工作目录自带的 {project} 不会生效（本次运行只认平台分配到的 MCP 服务器）"
                )
        return McpMount(
            home_name="claude-home",
            files={_MCP_FILE: to_json_config(ctx.mcp_servers)},
            notes=notes,
        )

    # -- 构造启动命令 --------------------------------------------------------

    def build_invocation(self, ctx: AgentContext) -> Invocation:
        options = ctx.options
        argv: list[str] = [
            self.executable or "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if truthy(options.get("partial_messages", True)):
            argv.append("--include-partial-messages")

        if ctx.model:
            argv += ["--model", ctx.model]

        mode = options.get("permission_mode") or "acceptEdits"
        argv += ["--permission-mode", mode]
        if mode == "bypassPermissions":
            argv.append("--dangerously-skip-permissions")

        if ctx.system_prompt:
            flag = (
                "--system-prompt"
                if options.get("system_prompt_mode") == "replace"
                else "--append-system-prompt"
            )
            argv += [flag, ctx.system_prompt]
            # 见 _SNAPSHOT_OFF_ARGS：不关快照的话，续接会话（--resume）会无视本轮
            # 传进去的人格文本，仍然重放会话第一次请求时记录的旧版本。
            argv += _SNAPSHOT_OFF_ARGS

        if ctx.session_id:
            argv += ["--resume", ctx.session_id]

        # 原生 auto-compact 窗口：配置了才传，未配置走 Claude Code 自己的默认。
        # 内核压缩发生时会发 compact_boundary 事件（见 _dispatch），面板据此提醒用户。
        autocompact = options.get("autocompact")
        if autocompact not in (None, ""):
            argv += ["--autocompact", str(autocompact)]

        if options.get("allowed_tools"):
            argv += ["--allowedTools", *as_list(options["allowed_tools"])]
        if options.get("disallowed_tools"):
            argv += ["--disallowedTools", *as_list(options["disallowed_tools"])]
        argv += [str(a) for a in as_list(options.get("extra_args") or [])]

        env = self.effective_env(ctx)
        self._apply_auth_style(env, ctx)
        # 原生压缩：把平台上限 / 阈值翻成 Claude Code 的环境变量，让内核按同一口径自己压。
        # 用户显式设了 --autocompact 时不覆盖（那是更明确的意图）。
        if options.get("autocompact") in (None, ""):
            env.update(
                claude_compaction_env(ctx.context_limit_tokens, ctx.context_trigger_tokens)
            )
        # 以 root 跑 bypassPermissions 需要内核认作「有意的沙箱」，见 _root_bypass_escape
        sandbox_escape = _root_bypass_escape(ctx)
        if sandbox_escape is not None:
            env.setdefault("IS_SANDBOX", sandbox_escape)
        env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
        env.setdefault("CI", "1")
        if ctx.home_dir:
            # 严格仅分配：内核只从隔离配置目录里的 skills/ 读取技能。
            env["CLAUDE_CONFIG_DIR"] = ctx.home_dir
            env.setdefault("CLAUDE_CODE_DISABLE_BUNDLED_SKILLS", "1")
            # 严格仅分配：MCP 也只认平台写下的那份配置（见 mcp_mount）。
            mcp_file = Path(ctx.home_dir) / _MCP_FILE
            if mcp_file.is_file():
                argv += ["--mcp-config", str(mcp_file), "--strict-mcp-config"]
        return Invocation(argv=argv, env=env, cwd=ctx.workspace_dir, stdin=ctx.prompt)

    def _apply_auth_style(self, env: dict[str, str], ctx: AgentContext) -> None:
        """按网关声明的鉴权方式，把密钥放进内核期望的请求头。

        直连 Anthropic 官方用 ``x-api-key``；中转网关几乎都用 ``Authorization: Bearer``
        —— 选哪一种是**网关**的属性（``Gateway.auth_style``），不在智能体上重复配置。
        """
        if not ctx.api_key:
            return
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        if ctx.auth_style == "x-api-key":
            env["ANTHROPIC_API_KEY"] = ctx.api_key
        else:
            env["ANTHROPIC_AUTH_TOKEN"] = ctx.api_key

    def new_session_hint(self, ctx: AgentContext) -> str | None:
        return None if ctx.session_id else str(uuid.uuid4())

    # -- 解析输出 ------------------------------------------------------------

    def parse_line(self, line: str, run: AgentRun) -> Iterable[AgentEvent]:
        # 只提示一次：root + bypassPermissions 时补了 IS_SANDBOX，否则用户看到内核拒绝启动会一头雾水
        if not run.state.get("_sandbox_noted") and _root_bypass_escape(run.ctx) == "1":
            run.state["_sandbox_noted"] = True
            yield log(
                "当前以 root 运行且勾选了 bypassPermissions：已按 Claude Code 的约定设置 IS_SANDBOX=1",
                "info",
            )
        line = line.strip()
        if not line or not line.startswith("{"):
            if line:
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
        sid = payload.get("session_id")
        if sid and sid != run.state.get("session"):
            run.state["session"] = sid
            yield session(sid)

        if kind == "autocompact_state":
            # 内核自报的上下文窗口（顶层帧）：value.effective_window 是内核解析后的
            # auto-compact 窗口（已按 /autocompact、模型窗口等夹取）。平台上限设为
            # 「自动」时跟随它，就不用在 200k / 1M 窗口的内核上仍按 128k 默认值提前压缩。
            #
            # 注意：2.1.327 的 ``-p`` print 模式**实测不下发**该帧（只出现在 SDK / 云端
            # 会话路径），所以现在多数情况下仍走兜底值。解析器留着：一旦某个版本或模式
            # 开始下发，无需改平台即可自动接上；接不上也不影响任何行为。
            value = payload.get("value")
            if isinstance(value, dict):
                window = value.get("effective_window")
                if (
                    isinstance(window, (int, float))
                    and not isinstance(window, bool)
                    and window >= 1
                ):
                    yield context_window(int(window))
            return

        if kind == "stream_event":
            yield from self._from_stream_event(payload.get("event") or {}, run)
        elif kind == "assistant":
            yield from self._from_assistant(payload.get("message") or {}, run)
        elif kind == "user":
            yield from self._from_tool_results(payload.get("message") or {})
        elif kind == "result":
            yield from self._from_result(payload, run)
        elif kind == "system":
            subtype = payload.get("subtype")
            if subtype == "compact_boundary":
                # Claude Code 原生压缩（auto-compact / /compact）的分界事件：旧消息已被
                # 折叠成摘要，窗口占用随之下降。上报给运行层做提醒与快照失效；元数据
                # （trigger、pre/post tokens、折叠条数）透传，面板才能展示真实数字。
                metadata = payload.get("compact_metadata") or {}
                if isinstance(metadata, dict):
                    yield context_compacted(
                        **{k: v for k, v in metadata.items() if k in _COMPACT_FIELDS}
                    )
                else:
                    yield context_compacted()
                return
            if subtype == "init":
                run.state["model"] = payload.get("model")
                tools = payload.get("tools") or []
                # 内核只认「平台注入到配置目录里的技能」，把这一轮真实加载到的技能回显出来，
                # 用户才能确认「智能体分配」是否生效。
                skills = [str(name) for name in (payload.get("skills") or [])]
                run.state["skills"] = skills
                if tools:
                    preview = "、".join(skills[:8]) + ("…" if len(skills) > 8 else "")
                    detail = f"（{preview}）" if preview else ""
                    yield log(
                        f"会话已就绪 · 模型 {payload.get('model')} · {len(tools)} 个工具"
                        f" · 技能 {len(skills)} 个{detail}"
                    )

    def _from_stream_event(self, event: dict[str, Any], run: AgentRun) -> Iterable[AgentEvent]:
        kind = event.get("type")
        if kind == "message_start":
            # 单次请求的输入 token 在 ``message_start.message.usage`` 里（不是 ``event.usage``），
            # 它比 result 里的整轮累计更能代表「当前上下文离上限还有多远」（见 runtime.context）。
            # 注意：2.1.327 起 message_start 的 usage 常常是全 0（真实数字推迟到
            # message_delta 才给），所以这里取到 0 是正常的，由下面的 message_delta 兜底。
            tokens = context_tokens(
                (event.get("message") or {}).get("usage") or {},
                protocol=PROTOCOL_ANTHROPIC,
            )
            if tokens:
                yield context_usage(tokens)
            return
        if kind == "message_delta":
            # **窗口占用真正的来源**：Claude Code 2.1.327 的 message_delta.usage 带的是
            # 「本次请求」的输入侧 token（input + cache_read + cache_creation），
            # 同一轮工具循环里每次请求各报各的（实测：第 1 次 75+14848、第 2 次 232+14848，
            # 而 result 里是整轮累计 307+29696）。
            #
            # 早先这里直接跳过 message_delta（注释说"它只有 output_tokens"）——那是旧版行为；
            # 按现在的内核，跳过它就等于**永远拿不到窗口快照**，界面只能一直显示按内容估算。
            # 老内核若确实只有 output_tokens，context_tokens() 会算出 0，不会误报。
            tokens = context_tokens(
                event.get("usage") or {}, protocol=PROTOCOL_ANTHROPIC
            )
            if tokens:
                yield context_usage(tokens)
            return
        if kind != "content_block_delta":
            return
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta" and delta.get("text"):
            run.state["partial"] = True
            yield text_delta(delta["text"])
        elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
            yield thinking_delta(delta["thinking"])

    def _from_assistant(self, message: dict[str, Any], run: AgentRun) -> Iterable[AgentEvent]:
        tokens = context_tokens(
            message.get("usage") or {}, protocol=PROTOCOL_ANTHROPIC
        )
        if tokens:
            yield context_usage(tokens)
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                # 已有逐字增量，不再重复输出整段文本。
                if not run.state.get("partial"):
                    yield text_delta(block["text"])
            elif block.get("type") == "tool_use":
                yield tool_call(
                    block.get("name") or "tool", block.get("id"), **(block.get("input") or {})
                )

    def _from_tool_results(self, message: dict[str, Any]) -> Iterable[AgentEvent]:
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                yield tool_result(
                    block.get("tool_use_id"),
                    flatten_content(block.get("content")),
                    bool(block.get("is_error")),
                )

    def _from_result(self, payload: dict[str, Any], run: AgentRun) -> Iterable[AgentEvent]:
        raw_usage = payload.get("usage")
        if isinstance(raw_usage, dict) and raw_usage:
            # 原样上报：嵌套的 cache_creation / output_tokens_details 由运行层
            # 统一归一化，适配器不再提前丢字段（见 docs/usage-normalization-design.md）。
            yield usage_raw(raw_usage)
        for src, dst in (("total_cost_usd", "cost_usd"), ("duration_ms", "duration_ms"),
                         ("num_turns", "num_turns")):
            if payload.get(src) is not None:
                run.result[dst] = payload[src]
        run.state["is_error"] = bool(payload.get("is_error"))
        if payload.get("is_error"):
            yield error(payload.get("result") or "Claude Code 报告了一个错误")
