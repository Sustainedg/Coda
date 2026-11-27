"""Nous Research Hermes Agent CLI 适配器。"""
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
    error,
    log,
    session,
    text_delta,
    tool_call,
    tool_result,
    usage_raw,
)
from ..mcp import McpMount, McpServerSpec, parse_json_servers, to_json_servers
from ..registry import register_agent
from ..skills import (
    MARKET_SOURCE,
    SkillMount,
    SkillRoot,
    SkillSpec,
    scan_skill_roots,
)
from ..metering import PROTOCOL_UNKNOWN
from .util import as_list, flatten_content, truthy


#: Hermes 的配置目录（默认 ``~/.hermes``），``HERMES_HOME`` 可整体挪走。
_CONFIG_DIR = Path.home() / ".hermes"

#: Hermes 的主配置文件（YAML 格式）。
_CONFIG_FILE = "config.yaml"

#: 人格文件：内核从 ``HERMES_HOME`` 读它，并把内容注入系统提示词
#: （``hermes chat --ignore-rules`` 的帮助里写明会自动注入 SOUL.md）。
_SOUL_FILE = "SOUL.md"

#: 平台「技能市场」安装目录（``<config>/skills/.skillhub/<skill>``），可卸载。
_MARKET_DIRNAME = ".skillhub"

#: 内核自带技能的清单（``skills/.bundled_manifest``，每行 ``name:hash``）。
#: Hermes 把内置技能与用户自己的技能放在**同一个** ``skills/`` 目录里，只能靠这份清单
#: 区分归属：清单里的算内核自带（只读），不在的才是用户自己的技能。
_BUNDLED_MANIFEST = ".bundled_manifest"

#: 内核整理器（curator）裁掉的技能清单；它只记录内置技能，同样算「自带」。
_CURATOR_SUPPRESSED = ".curator_suppressed"

#: **运行时**目录：内核自带解释器（``tools``）与按工程根哈希存的依赖环境（``installs``）。
#: 它们与用户配置、技能、会话无关，可以跨隔离目录共享（见 :meth:`HermesAgent._share_runtime`）。
_RUNTIME_DIRS = ("tools", "installs")


def hermes_home() -> Path:
    """Hermes 的配置目录（认 ``HERMES_HOME`` 环境变量）。"""
    override = os.environ.get("HERMES_HOME")
    return Path(override).expanduser() if override else _CONFIG_DIR


def bundled_skill_names(home: str | Path | None = None) -> set[str]:
    """Hermes 内核自带技能的名字（读 ``skills/.bundled_manifest``，尽力而为）。

    Hermes 把内置技能和用户自己的技能都放在 ``skills/`` 下，靠清单区分归属：
    清单每行是 ``name:hash``（老版本是纯名字），``name`` 是 SKILL.md 里的 frontmatter 名。
    清单缺失或损坏时返回空集合（此时全部按用户技能处理，与旧行为一致）。
    """
    skills_dir = (Path(home).expanduser() if home else hermes_home()) / "skills"
    names: set[str] = set()
    for filename in (_BUNDLED_MANIFEST, _CURATOR_SUPPRESSED):
        try:
            text = (skills_dir / filename).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # v2 是 ``name:hash``，v1 只有名字；两种都取冒号前的部分。
            name = line.split(":", 1)[0].strip()
            if name:
                names.add(name)
    return names


@register_agent
class HermesAgent(AgentDefinition):
    id = "hermes-agent"
    name = "Hermes Agent"
    vendor = "Nous Research"
    description = (
        "Nous Research 的自进化智能体。内置学习循环——从经验中创建技能、"
        "使用中改进技能，支持流式 JSONL 输出、会话续接、技能与 MCP。"
    )
    runtime = Runtime.CLI
    executable = "hermes"
    provisioner = RuntimeProvisioner(
        manager="script",
        package="hermes-agent",
        #: 官方安装脚本：自动获取 uv / Python、克隆仓库、发布命令到 PATH。
        #: 不依赖 pipx / npm，只需 curl + bash。
        install_argv=[
            "sh", "-c",
            "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash",
        ],
        #: Hermes 自带更新命令（重新跑上游安装器）。
        update_argv=["hermes", "update", "--yes"],
        #: 卸载：删掉安装目录与配置目录。
        uninstall_argv=[
            "sh", "-c",
            'rm -rf "${HERMES_INSTALL_DIR:-$HOME/.local/share/hermes-agent}" "$HOME/.hermes"',
        ],
        #: 查询远端最新版本（不安装，只检查）。需网络访问 GitHub；
        #: 若服务器无法直连，可在设置页配置代理。
        latest_argv=["hermes", "update", "--check"],
        homepage="https://github.com/NousResearch/hermes-agent",
    )
    api_key_env = "OPENROUTER_API_KEY"
    #: Hermes **没有**「换个端点」的环境变量：``OPENROUTER_BASE_URL`` 它不读，
    #: 自定义端点只能走命令行（``--provider custom --base-url …``），见 :meth:`build_invocation`。
    base_url_env = None
    default_gateway = "https://openrouter.ai/api/v1"
    default_model = "anthropic/claude-sonnet-4.6"
    protocol = "openai"
    #: 真实 usage 协议尚未用样本确认：显式 unknown，账本保持 unknown 而不是猜。
    usage_protocol = PROTOCOL_UNKNOWN
    default_auth_style = "bearer"
    default_models = [
        "anthropic/claude-sonnet-4.6",
        "anthropic/claude-haiku-4.6",
        "openai/gpt-5.5",
        "google/gemini-2.5-pro",
    ]
    docs_url = "https://hermes-agent.nousresearch.com/docs/"
    logo = "/icons/kernels/hermes.webp"
    #: 黑白线稿头像，按原色显示（遮罩染色会把留白处也涂掉）。
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
    )
    config_fields = (
        ConfigField(
            "approval_mode",
            "审批模式",
            FieldType.SELECT,
            default="yolo",
            options=[
                {"value": "yolo", "label": "yolo · 自动批准所有工具调用"},
                {"value": "safe", "label": "safe · 排障模式（禁用个人配置 / 规则 / 插件 / MCP）"},
                {"value": "default", "label": "default · 每次询问（非交互运行下可能卡在授权提示）"},
            ],
        ),
        ConfigField(
            "system_prompt_mode",
            "人格注入方式",
            FieldType.SELECT,
            default="prepend",
            options=[
                {"value": "prepend", "label": "拼接到提示词最前面"},
                {"value": "soul", "label": "写入隔离配置目录的 SOUL.md（内核自己的身份文件）"},
            ],
        ),
        ConfigField(
            "toolsets",
            "启用的工具集",
            FieldType.STRING_LIST,
            placeholder="web,terminal,skills",
            help="逗号分隔；留空则用 Hermes 默认工具集。",
        ),
        ConfigField(
            "provider",
            "模型供应商",
            FieldType.STRING,
            advanced=True,
            placeholder="openrouter / openai / anthropic",
            help="没选自定义网关时生效；选了网关则由平台接管路由（--provider custom）。",
        ),
        ConfigField(
            "oneshot",
            "单次模式",
            FieldType.BOOLEAN,
            default=True,
            help="回答后即退出，不启动交互式会话（平台默认开启）。",
        ),
        ConfigField("extra_args", "额外命令行参数", FieldType.STRING_LIST, advanced=True),
        ConfigField("env", "环境变量", FieldType.KEYVALUE, advanced=True),
    )

    # -- 技能 ---------------------------------------------------------------

    def skill_roots(self) -> list[SkillRoot]:
        """Hermes 的技能根：``~/.hermes/skills/`` 是主目录，``.skillhub`` 是市场安装目录。"""
        home = hermes_home()
        skills_dir = home / "skills"
        return [
            SkillRoot(str(skills_dir), source_kind="user", installable=True, max_depth=2),
            SkillRoot(
                str(skills_dir / _MARKET_DIRNAME),
                source_kind=MARKET_SOURCE,
                installable=True,
                max_depth=2,
            ),
        ]

    def discover_skills(self) -> list[SkillSpec]:
        """只读扫描 ``~/.hermes/skills/`` 下的技能（尽力而为、不得抛错）。

        内核自带技能与用户技能混在同一个 ``skills/`` 目录里，所以扫完之后要按
        ``.bundled_manifest`` 重新归类：清单里登记过的标成 ``system``（只读、不可删），
        其余才是 ``user``（用户自己的，可以删）。
        """
        specs = scan_skill_roots(self.skill_roots(), type_id=self.id)
        bundled = bundled_skill_names()
        if not bundled:
            return specs
        market_dir = (hermes_home() / "skills" / _MARKET_DIRNAME).resolve()
        for spec in specs:
            if spec.source_kind != "user":
                continue
            # 市场目录也躺在 skills/ 下（会被父根以 user 身份再扫一遍），
            # 它归平台市场管，不能因为名字碰巧在清单里就标成内核自带。
            try:
                if Path(spec.path).resolve().is_relative_to(market_dir):
                    continue
            except (OSError, ValueError):
                continue
            if spec.name in bundled or spec.slug in bundled:
                spec.source_kind = "system"
        return specs

    def seed_skills(self) -> list[str]:
        """让 Hermes 自己把内置技能同步到 ``~/.hermes/skills``。

        Hermes 的内置技能由 ``hermes --run-module tools.skills_sync`` 落盘，
        不播种的话「技能」页看不到内核自带的那批。已有技能时跳过（幂等）。
        """
        skills_dir = hermes_home() / "skills"
        if skills_dir.is_dir() and any(skills_dir.rglob("SKILL.md")):
            return []
        binary = self.executable or "hermes"
        if shutil.which(binary) is None:
            return []
        try:
            subprocess.run(
                [binary, "--run-module", "tools.skills_sync"],
                capture_output=True,
                timeout=300,
                check=False,
            )
        except Exception:  # noqa: BLE001 - 播种失败只意味着少几个技能，不能影响安装
            return []
        found = skills_dir.is_dir() and any(skills_dir.rglob("SKILL.md"))
        return [f"{binary} --run-module tools.skills_sync（播种内置技能）"] if found else []

    def skill_mount(self, ctx: AgentContext) -> SkillMount:
        """把分配到的技能物化进隔离配置目录的 ``skills/`` 子目录。

        Hermes 通过 ``HERMES_HOME`` 环境变量认配置目录，技能放在其下的 ``skills/`` 里。
        """
        return SkillMount(
            home_name="hermes-home",
            skills_subdir="skills",
        )

    # -- MCP 服务器 ----------------------------------------------------------

    def discover_mcp_servers(self) -> list[McpServerSpec]:
        """扫描 ``~/.hermes/config.yaml`` 里的 ``mcp_servers`` 块（只读、尽力而为）。"""
        try:
            import yaml

            raw = yaml.safe_load(
                (hermes_home() / _CONFIG_FILE).read_text(encoding="utf-8")
            )
        except (OSError, ValueError, ImportError):
            return []
        if not isinstance(raw, dict):
            return []
        servers = raw.get("mcp_servers")
        if not isinstance(servers, dict):
            return []
        # Hermes 的 MCP 配置形状与 Claude Code 的 mcpServers 一致。
        return parse_json_servers(
            {"mcpServers": servers}, type_id=self.id, source_kind="user"
        )

    def mcp_mount(self, ctx: AgentContext) -> McpMount:
        """把分配到的服务器写进隔离配置目录的 ``config.yaml``。

        Hermes 的 MCP 配置在 ``config.yaml`` 的 ``mcp_servers`` 块里，
        形状与 Claude Code 的 ``mcpServers`` 一致（command/args/env 或 url/headers）。
        """
        import yaml

        content = {
            "mcp_servers": to_json_servers(ctx.mcp_servers),
        }
        return McpMount(
            home_name="hermes-home",
            files={
                _CONFIG_FILE: yaml.dump(content, default_flow_style=False, allow_unicode=True),
            },
        )

    # -- 隔离目录 ------------------------------------------------------------

    def _share_runtime(self, home: Path) -> None:
        """把真实家目录里的**运行时**软链进隔离家目录，避免内核在每个新家里重装一遍依赖。

        Hermes 的 ``HERMES_HOME`` 不只是配置目录，它同时是运行时根：``tools/`` 放它自带的
        解释器与 uv，``installs/`` 按工程根哈希放依赖环境。平台的隔离目录是「一条对话一份」，
        若不同步这两项，内核会在新家里完整装一遍依赖（数百 MB、要联网、要几分钟）。

        只共享这两类**运行时代码**；技能、配置、会话与记忆仍留在隔离家目录里。
        链接失败不阻断运行（内核会退化成在自己的家里装一遍依赖），因此这里只跳过、不抛错。
        """
        source = hermes_home()
        if source == home:
            return
        for name in _RUNTIME_DIRS:
            src, dst = source / name, home / name
            if not src.exists() or dst.exists() or dst.is_symlink():
                continue
            try:
                dst.symlink_to(src, target_is_directory=True)
            except OSError:
                continue

    @staticmethod
    def _runtime_store() -> Path | None:
        """真实家目录里装着解释器的运行时目录（``tools/facts.json`` 的 ``python.entry``）。

        ``HERMES_RUNTIME_DIR`` 让内核把**当前进程的运行时**认成真实路径。隔离家目录是
        「一条对话一份」的临时目录：内核启动自检时若发现 ``sys.executable`` 与
        ``<HERMES_HOME>/tools`` 拼出来的解释器不是同一个路径，就会当场重写**共享**的
        ``hermes-agent/.hermes/bin/hermes`` 启动器，把这条临时路径写死进去——目录一删，
        所有对话乃至 shell 里的 ``hermes`` 都会 ``exec: ... not found``。

        尽力而为：读不到 ``facts.json`` 就当本机没有真实运行时，不设这个变量，
        让内核按自己的默认值走（它会退化成在自己的家里装一遍依赖）。
        """
        store = hermes_home() / "tools"
        rel = "python.exe" if os.name == "nt" else "bin/python3"
        try:
            facts = json.loads((store / "facts.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None
        packages = facts.get("packages") if isinstance(facts, dict) else None
        python = packages.get("python") if isinstance(packages, dict) else None
        entry = python.get("entry") if isinstance(python, dict) else None
        return store if entry and (store / str(entry) / rel).is_file() else None

    def _write_soul(self, home: Path, text: str) -> None:
        """把人写进隔离家目录的 ``SOUL.md``（Hermes 从 ``HERMES_HOME`` 读它注入提示词）。"""
        home.mkdir(parents=True, exist_ok=True)
        (home / _SOUL_FILE).write_text(text, encoding="utf-8")

    # -- 构造启动命令 --------------------------------------------------------

    def build_invocation(self, ctx: AgentContext) -> Invocation:
        options = ctx.options
        argv = [self.executable or "hermes", "chat"]

        # 单次模式：回答后退出（平台默认）
        if truthy(options.get("oneshot", True)):
            argv.append("--oneshot")

        # 流式 JSONL 输出
        argv += ["--format", "stream-json"]

        # 模型
        if ctx.model:
            argv += ["--model", ctx.model]

        # 网关：Hermes 只认命令行上的「自定义端点」，环境变量不生效
        # （实测 OPENROUTER_BASE_URL 被忽略：它会拿网关密钥去请求真 OpenRouter，直接 401）。
        if ctx.gateway_url and ctx.api_key:
            argv += [
                "--provider", "custom",
                "--base-url", ctx.gateway_url,
                "--api-key", ctx.api_key,
            ]
        elif options.get("provider"):
            argv += ["--provider", str(options["provider"])]

        # 审批模式：非交互子进程里不自动批准，工具调用会卡在授权提示上。
        mode = options.get("approval_mode") or "yolo"
        if mode == "yolo":
            argv.append("--yolo")
        elif mode == "safe":
            argv.append("--safe-mode")

        # 工具集
        toolsets = as_list(options.get("toolsets") or [])
        if toolsets:
            argv += ["--toolsets", ",".join(toolsets)]

        # 会话续接（Hermes 的续接开关是 --resume，不是 --session）
        if ctx.session_id:
            argv += ["--resume", ctx.session_id]

        # 额外参数
        argv += [str(a) for a in as_list(options.get("extra_args") or [])]

        # 提示词与人格
        prompt = ctx.prompt
        env = self.effective_env(ctx)

        if ctx.home_dir:
            # 隔离配置目录（技能与 MCP 共用），并复用它所需的运行时。
            home = Path(ctx.home_dir)
            env["HERMES_HOME"] = ctx.home_dir
            self._share_runtime(home)
            # 运行时必须锚定真实家目录：否则内核会把隔离家目录当成自己的完整数据根，
            # 用它（临时的）路径重写共享启动器，见 _runtime_store()。
            store = self._runtime_store()
            if store is not None:
                env["HERMES_RUNTIME_DIR"] = str(store)

        if ctx.system_prompt:
            sp_mode = options.get("system_prompt_mode") or "prepend"
            # 旧的 "config"（曾试图写 config.yaml —— Hermes 没有这个键，等于静默丢弃人格）
            # 按 SOUL.md 处理，让存量智能体的人格真正生效。
            if sp_mode in ("soul", "config") and ctx.home_dir:
                self._write_soul(Path(ctx.home_dir), ctx.system_prompt)
            else:
                prompt = f"{ctx.system_prompt}\n\n---\n\n{prompt}"

        # 用 -q 把提示词走命令行参数（Hermes 要求 --format stream-json 配合 -q）
        argv += ["-q", prompt]

        return Invocation(argv=argv, env=env, cwd=ctx.workspace_dir)

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

        # system: 初始化事件。Hermes 的 init 里 session_id 是空的，
        # 真正的会话 id 在末尾的 result 里补发（见下）。
        if kind == "system":
            subtype = payload.get("subtype")
            if subtype == "init":
                sid = payload.get("session_id")
                if sid and run.state.get("session") != sid:
                    run.state["session"] = sid
                    yield session(sid)
                if payload.get("model"):
                    yield log(f"Hermes 已就绪 · 模型 {payload['model']}")
            return

        # text: 流式文本增量
        if kind == "text":
            text = payload.get("text")
            if text:
                yield text_delta(text)
            return

        # tool_use: 工具调用（Hermes 的调用 id 字段叫 tool_call_id）
        if kind == "tool_use":
            name = payload.get("name") or "tool"
            input_ = payload.get("input") or {}
            yield tool_call(
                name,
                payload.get("tool_call_id") or payload.get("id") or payload.get("tool_id"),
                **(input_ if isinstance(input_, dict) else {"input": input_}),
            )
            return

        # tool_result: 工具结果
        if kind == "tool_result":
            yield tool_result(
                payload.get("tool_call_id") or payload.get("id") or payload.get("tool_id"),
                flatten_content(payload.get("output") or payload.get("result")),
                bool(payload.get("is_error") or payload.get("error")),
            )
            return

        # result: 终止事件，携带会话 id、用量统计与退出码
        if kind == "result":
            sid = payload.get("session_id")
            if sid and run.state.get("session") != sid:
                run.state["session"] = sid
                yield session(sid)
            raw_usage = payload.get("tokens")
            if isinstance(raw_usage, dict) and raw_usage:
                # 原样上报；Hermes 协议尚未验证，账本侧会保持 unknown（pending）。
                yield usage_raw(raw_usage)
            exit_code = payload.get("exit_code")
            if payload.get("error"):
                run.state["is_error"] = True
                yield error(flatten_content(payload["error"]))
            elif exit_code is not None and exit_code not in (0, 130):
                run.state["is_error"] = True
                yield error(f"Hermes 以退出码 {exit_code} 结束")
            elif not run.emitted_text and payload.get("text"):
                # 没走流式增量（例如回答太短只有一条汇总）时补发正文，
                # 否则界面与落库都会是空回复。
                yield text_delta(str(payload["text"]))
            return

        if kind == "error":
            run.state["is_error"] = True
            yield error(payload.get("message") or flatten_content(payload))
            return
