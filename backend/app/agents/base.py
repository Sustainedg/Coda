"""每个智能体集成都要实现的契约。

接入一个全新的智能体（Gemini CLI、Aider…），只需在
``app/agents/builtin`` 下新增一个模块，定义 :class:`AgentDefinition` 的子类
并用 :func:`register_agent` 装饰即可，代码库其他部分无需改动。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, AsyncIterator, ClassVar, Iterable, Protocol, Sequence

from .events import AgentEvent, EventType
from .mcp import McpMount, McpServerSpec
from .skills import SkillMount, SkillRoot, SkillSpec


class FieldType(str, Enum):
    STRING = "string"
    SECRET = "secret"
    NUMBER = "number"
    BOOLEAN = "boolean"
    SELECT = "select"
    TEXTAREA = "textarea"
    KEYVALUE = "keyvalue"
    STRING_LIST = "string_list"


class Runtime(str, Enum):
    """适配器与智能体的通信方式。"""

    CLI = "cli"  # 启动子进程
    API = "api"  # 进程内直接发起 HTTP 调用（OpenAI 兼容端点）


class TraceSink(Protocol):
    """轨迹采集的**叶子协议**：适配器只看到这组方法，不依赖存储实现。

    真正的写库实现在 ``runtime.trace.TraceRecorder``；协议放在 ``agents/base.py``
    是为了让适配器（叶子层）能可选地记录请求/响应，同时不反向依赖 runtime。
    所有方法都必须**尽力而为**：采集失败不能影响正常回复与事件流。
    """

    async def request(
        self,
        payload: dict[str, Any],
        *,
        duration_ms: int | None = None,
        error: str | None = None,
        truncated: bool = False,
    ) -> None: ...

    async def response(
        self,
        payload: dict[str, Any],
        *,
        duration_ms: int | None = None,
        error: str | None = None,
        truncated: bool = False,
    ) -> None: ...

    async def on_event(self, event: AgentEvent) -> None: ...

    async def flush(self) -> None: ...


@dataclass(slots=True)
class ConfigField:
    """一个可由用户编辑的配置项的声明式描述。

    界面会统一按声明渲染这些字段，因此新增智能体自带表单，无需改动前端。
    """

    key: str
    label: str
    type: FieldType = FieldType.STRING
    required: bool = False
    default: Any = None
    placeholder: str | None = None
    help: str | None = None
    options: list[dict[str, str]] | None = None
    advanced: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "type": self.type.value,
            "required": self.required,
            "default": self.default,
            "placeholder": self.placeholder,
            "help": self.help,
            "options": self.options,
            "advanced": self.advanced,
        }


@dataclass(slots=True)
class Capabilities:
    streaming: bool = True
    sessions: bool = False          # 能否在服务端续接会话 / 线程
    native_system_prompt: bool = False
    tools: bool = False
    mcp: bool = False
    permission_modes: bool = False
    working_directory: bool = False
    #: 是否支持技能（声明即表示适配器实现了 discover_skills/skill_mount）
    skills: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {
            "streaming": self.streaming,
            "sessions": self.sessions,
            "native_system_prompt": self.native_system_prompt,
            "tools": self.tools,
            "mcp": self.mcp,
            "permission_modes": self.permission_modes,
            "working_directory": self.working_directory,
            "skills": self.skills,
        }


@dataclass(slots=True)
class Availability:
    installed: bool
    version: str | None = None
    detail: str | None = None
    #: 这个结论是什么时候检测出来的。适配器现探时留空；框架层从缓存表读出时带上时间戳，
    #: 界面据此提示「检测于 …」（见 ``runtime/availability.py``）。
    checked_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "installed": self.installed,
            "version": self.version,
            "detail": self.detail,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
        }


@dataclass(slots=True)
class RuntimeAccount:
    """账号型内核的一份已保存登录（平台侧的「账号槽位」）。

    内核自己通常只维护「当前这一个」登录态；平台把每次登录的凭据原样存成槽位后，
    就能在多个账号之间切换。``id`` 只在本运行时内唯一，前端用它调切换 / 删除接口。
    """

    id: str
    #: 展示名（昵称 / 邮箱 + 企业），界面上直接显示
    label: str
    #: 补充说明（企业、保存时间等），可空
    detail: str | None = None
    #: 是否是当前正在使用的账号
    active: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "detail": self.detail, "active": self.active}


@dataclass(slots=True)
class AuthStatus:
    """账号型内核的登录状态（扫码 / OAuth 这类没有 API Key 的内核）。

    有些内核靠「本机登录态」工作（豆包 CLI、CodeBuddy 官方链路…），平台没有可注入的
    密钥。适配器用 :meth:`AgentDefinition.auth_status` 现探一次，内核详情页据此显示
    「已登录 / 未登录」并给出登录入口。
    """

    #: 是否真的需要登录（``False`` 时 ``logged_in`` 没有意义）
    required: bool = True
    #: ``True`` / ``False``；``None`` 表示探测失败，原因写在 ``detail``
    logged_in: bool | None = None
    detail: str | None = None
    #: 可选的账号标识（邮箱 / 昵称），没有就不展示
    account: str | None = None
    #: 已保存的账号槽位；``None`` 表示该内核不支持多账号
    accounts: list[RuntimeAccount] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "logged_in": self.logged_in,
            "detail": self.detail,
            "account": self.account,
            "accounts": None if self.accounts is None else [a.to_dict() for a in self.accounts],
        }


@dataclass(slots=True)
class RuntimeProvisioner:
    """声明「平台如何安装 / 更新 / 卸载这个内核的运行时」。

    这里只描述**命令**，不描述策略：框架负责在受控子进程里执行、逐行流式回显、
    总超时与脱敏。「用哪个包管理器、装到哪儿、包名叫什么」是**适配器**的知识
    （与技能目录同理），框架不认识任何具体内核。

    未声明（``None``）表示该运行时不由平台管理：进程内 API 内核无需安装，
    ``custom-cli`` 这类由用户自备可执行文件的适配器也不该被平台改环境。
    """

    #: 包管理器标识（npm / pipx / brew / 手动…），仅用于界面展示
    manager: str
    #: 包名，用于界面展示与「检查更新」的默认命令
    package: str | None = None
    #: 安装 / 更新 / 卸载 / 查询最新版本的完整命令；空列表表示该项不支持
    install_argv: list[str] = field(default_factory=list)
    update_argv: list[str] = field(default_factory=list)
    uninstall_argv: list[str] = field(default_factory=list)
    latest_argv: list[str] = field(default_factory=list)
    #: 账号型内核的登录命令（例如 ``doubao login --qr=png``）。它不改变安装状态，
    #: 只把登录态写进内核自己的配置目录；登录命令输出 ``QR_IMAGE=<图片路径>`` 时，
    #: 框架会把图片读出来转成 data URL 推给界面（见 ``runtime/provisioning.py``）。
    login_argv: list[str] = field(default_factory=list)
    #: 执行上面各动作命令**之前**先跑的「清扫」命令，用来清掉上一次被中断的动作留下的
    #: 残留（例如 npm 的 rename 中转目录）。空列表表示无需清扫；框架照常执行、逐行回显，
    #: 但它失败**不**中断动作 —— 动作的成败只由动作自己的退出码决定。
    prepare_argv: list[str] = field(default_factory=list)
    homepage: str | None = None
    #: 适配器想提醒用户的事项（例如「本机当前不是用该包管理器装的」）
    note: str | None = None

    def argv_for(self, action: str) -> list[str]:
        return {
            "install": self.install_argv,
            "update": self.update_argv,
            "uninstall": self.uninstall_argv,
            "login": self.login_argv,
        }.get(action, [])

    def supports(self, action: str) -> bool:
        return bool(self.argv_for(action))

    def to_dict(self) -> dict[str, Any]:
        return {
            "manager": self.manager,
            "package": self.package,
            "homepage": self.homepage,
            "note": self.note,
            "commands": {
                "install": list(self.install_argv),
                "update": list(self.update_argv),
                "uninstall": list(self.uninstall_argv),
                "latest": list(self.latest_argv),
                "login": list(self.login_argv),
            },
        }


@dataclass(slots=True)
class Invocation:
    """一次运行所需的完整启动描述（子进程或 HTTP 目标）。"""

    argv: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    stdin: str | None = None
    #: 为 True 时运行器保持 stdin 打开：适配器可以在解析过程中用 ``AgentRun.send()``
    #: 把后续请求写回内核（双向 JSON-RPC 协议，例如 Codex app-server）。
    #: 为 False 时照旧写完 ``stdin`` 立刻关闭，让内核看到 EOF。
    stdin_hold: bool = False
    #: 运行器在启动前必须写入的文件（相对路径 -> 内容）
    files: dict[str, str] = field(default_factory=dict)
    #: 适配器期望事件载荷出现在哪条流上
    stream: str = "stdout"


@dataclass(slots=True)
class AgentContext:
    """从数据库解析出的、适配器构建一次运行所需的全部上下文。"""

    agent_id: str
    agent_name: str
    #: 智能体上合并后的 options
    options: dict[str, Any] = field(default_factory=dict)
    gateway_url: str | None = None
    api_key: str | None = None
    #: 智能体自己的凭据（解密后的「环境变量名 → 明文」）；由框架合并进子进程环境变量，
    #: 值不得出现在提示词、日志、事件与接口响应里（见 app.credentials）
    credentials: dict[str, str] = field(default_factory=dict)
    #: 所选网关声明的鉴权方式（bearer / x-api-key / query / none）；官方默认时为 None
    auth_style: str | None = None
    model: str | None = None
    #: 人格（智能体的系统提示词，已由框架解析成最终文本）
    system_prompt: str | None = None
    #: 工作目录
    workspace_dir: str | None = None
    #: 多轮会话支持
    session_id: str | None = None
    #: 对话记录；注入给无法在服务端续接会话的智能体
    history: list[dict[str, str]] = field(default_factory=list)
    prompt: str = ""
    #: 智能体分配到的技能（框架按内核类型过滤后注入）
    skills: list[SkillSpec] = field(default_factory=list)
    #: 智能体分配到的 MCP 服务器（框架解密后注入；值不得写进提示词、日志或事件）
    mcp_servers: list[McpServerSpec] = field(default_factory=list)
    #: 框架物化好的内核隔离配置目录（技能与 MCP 共用同一个「家」）；
    #: 适配器据此设置内核的配置目录环境变量。
    home_dir: str | None = None
    #: 可选的轨迹采集器（``None`` = 本轮没开调试，适配器零开销、零写库）。
    #: 协议见 :class:`TraceSink`，实现见 ``runtime.trace.TraceRecorder``。
    trace: TraceSink | None = None

    def option(self, key: str, default: Any = None) -> Any:
        value = self.options.get(key, default)
        return default if value in (None, "") else value


class AgentDefinition(ABC):
    """单个智能体集成的静态描述与运行行为。"""

    id: ClassVar[str]
    name: ClassVar[str]
    #: 该适配器取代过的旧 id（已下线 / 改名的内核）。启动时框架据此把存量智能体迁到本适配器；
    #: 只影响迁移、不参与运行。这是**声明**：框架层不再硬编码任何具体内核 id。
    legacy_ids: ClassVar[tuple[str, ...]] = ()
    description: ClassVar[str] = ""
    vendor: ClassVar[str] = ""
    runtime: ClassVar[Runtime] = Runtime.CLI
    capabilities: ClassVar[Capabilities] = Capabilities()
    config_fields: ClassVar[Sequence[ConfigField]] = ()
    #: 按「自带网关 / 自带密钥」约定使用的环境变量名
    api_key_env: ClassVar[str | None] = None
    base_url_env: ClassVar[str | None] = None
    default_gateway: ClassVar[str | None] = None
    default_model: ClassVar[str | None] = None
    #: 官方链路的协议与鉴权方式（决定「获取模型」怎么请求，以及播种官方网关的默认值）
    protocol: ClassVar[str] = "openai"
    default_auth_style: ClassVar[str] = "bearer"
    #: 官方链路可选的模型清单（播种官方网关时作为默认白名单；用户可再改）
    default_models: ClassVar[list[str]] = []
    docs_url: ClassVar[str | None] = None
    #: 该内核的官方 Logo（前端静态资源路径，如 ``/icons/kernels/claude.webp``）；
    #: ``None`` 表示界面退回只用文字名。它只是「一张图」，不参与任何运行行为。
    logo: ClassVar[str | None] = None
    #: Logo 的渲染方式：
    #: * ``mask`` —— 单色 SVG 当遮罩，染成当前文字色（深浅外观自适应，旧内核默认）；
    #: * ``image`` —— 真彩色位图（PNG / WebP）或彩色 SVG，按原色显示。
    logo_style: ClassVar[str] = "mask"
    #: 必须存在于 PATH 中的可执行文件（仅 CLI 适配器）
    executable: ClassVar[str | None] = None
    #: 该内核的运行时如何安装 / 更新 / 卸载；``None`` 表示不由平台管理
    provisioner: ClassVar[RuntimeProvisioner | None] = None
    #: 首次启动播种时是否允许为该适配器创建实例
    #: （需要用户先填写配置才能工作的模板设为 False）
    auto_provision: ClassVar[bool] = True
    #: 是否出现在「新建智能体」的内核下拉里。
    #: ``False`` 只影响新建选择：存量智能体仍可正常编辑、运行，
    #: 内核管理与运行时页也照旧展示（它们是「逃生舱」型模板，不再作为推荐入口）。
    selectable: ClassVar[bool] = True

    # -- 元数据与自省 --------------------------------------------------------

    def descriptor(self, availability: Availability | None = None) -> dict[str, Any]:
        """适配器的静态自省 + 本机真实可用性。

        ``availability`` 由框架层注入：框架把检测结果缓存在 ``runtime_status`` 表里并定时刷新，
        请求路径只读缓存。不传则现探一次 —— 那是同步阻塞的子进程调用，只允许出现在
        启动播种与测试里，**请求路径禁止**走到这一步（否则会冻住整个事件循环）。
        """
        availability = availability or self.availability()
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "vendor": self.vendor,
            "runtime": self.runtime.value,
            "capabilities": self.capabilities.to_dict(),
            "config_fields": [f.to_dict() for f in self.config_fields],
            "api_key_env": self.api_key_env,
            "base_url_env": self.base_url_env,
            "default_gateway": self.default_gateway,
            "default_model": self.default_model,
            "protocol": self.protocol,
            "default_auth_style": self.default_auth_style,
            "default_models": list(self.default_models),
            "docs_url": self.docs_url,
            "logo": self.logo,
            "logo_style": self.logo_style,
            "executable": self.executable,
            "selectable": self.selectable,
            "availability": availability.to_dict(),
            "provisioning": self.provisioning(availability),
        }

    def provisioning(self, availability: Availability | None = None) -> dict[str, Any]:
        """本机真实的安装状态 + 适配器声明的维护方式，供「运行时」页展示。

        ``availability`` 同 :meth:`descriptor`：由框架层注入缓存值，不传则现探一次。
        """
        availability = availability or self.availability()
        data: dict[str, Any] = {
            "supported": self.provisioner is not None,
            "manager": None,
            "package": None,
            "homepage": None,
            "note": None,
            "commands": None,
            "installed": availability.installed,
            "version": availability.version,
            "path": availability.detail if availability.installed else None,
            #: 这份安装状态是什么时候检测的（界面提示「检测于 …」）。
            "checked_at": availability.checked_at.isoformat() if availability.checked_at else None,
        }
        if self.provisioner is not None:
            data.update(self.provisioner.to_dict())
        return data

    def auth_status(self) -> AuthStatus | None:
        """账号型内核的登录状态；``None`` 表示该内核不需要登录（默认）。

        实现必须是**尽力而为**的：起子进程探测、超时、解析失败都不能抛异常，
        失败时返回 ``logged_in=None`` 并把原因写进 ``detail``。
        框架既用它渲染内核详情，也在一次「登录」动作结束后重新取结论。
        """
        return None

    def supports_auth(self) -> bool:
        """是否实现了登录态探测（即覆盖了 :meth:`auth_status`）。

        框架据此决定「内核管理」卡片要不要显示登录状态：实现了探测的内核即便
        没有可代跑的登录命令（例如只有交互式 TUI 才能登录的内核），也值得把
        「已登录 / 未登录 / 无法判断」摆给管理员看，而不是藏起来。
        """
        return type(self).auth_status is not AgentDefinition.auth_status

    def activate_account(self, account_id: str) -> None:
        """切换到某个已保存的账号（多账号内核才需要覆盖）。

        同步执行、可能做文件 / 子进程操作，调用方会放进线程池。未知 ``account_id``
        抛 ``KeyError``；切换失败抛 ``RuntimeError``（消息直接回给界面）。
        """
        raise KeyError(account_id)

    def remove_account(self, account_id: str) -> None:
        """删除一个已保存的账号。未知 ``account_id`` 抛 ``KeyError``。"""
        raise KeyError(account_id)

    def supports_accounts(self) -> bool:
        """是否支持多账号切换（覆盖了 :meth:`activate_account` 即为支持）。"""
        return type(self).activate_account is not AgentDefinition.activate_account

    def availability(self) -> Availability:
        if not self.executable:
            return Availability(True, detail="进程内运行，无需本地可执行文件")
        path = shutil.which(self.executable)
        if not path:
            #: 用户安装脚本（curl | bash 等）常把可执行文件放到 ~/.local/bin，
            #: 服务进程的 PATH 可能不含该目录，这里兜底查一次。
            local_bin = os.path.join(os.environ.get("HOME", ""), ".local", "bin")
            candidate = os.path.join(local_bin, self.executable)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                path = candidate
        if not path:
            return Availability(False, detail=f"未在 PATH 中找到 `{self.executable}`")
        return Availability(True, version=self._probe_version(path), detail=path)

    def _probe_version(self, path: str) -> str | None:
        try:
            out = subprocess.run(
                [path, "--version"], capture_output=True, text=True, timeout=8, check=False
            )
            return (out.stdout or out.stderr).strip().splitlines()[0][:80] or None
        except Exception:  # noqa: BLE001 - 可用性探测绝不能向调用方抛异常
            return None

    def system_defaults(self) -> dict[str, Any]:
        """本机真实配置里的默认值（网关地址 / 模型 / 凭据 / 选项）。

        适配器可以覆盖此钩子，从 CLI 自身的系统级配置（例如 Claude Code 的
        ``~/.claude/settings.json``）读取真实默认值。框架在「新建智能体」或
        「字段仍是内置默认值」时用它兜底，从而让界面展示与一次真实运行都对齐
        本机的真实配置。适配器只允许依赖标准库，因此在这里读文件是安全的。
        默认返回空字典。
        """
        return {}

    def resolve_gateway(self, gateway_url: str | None) -> str | None:
        """把等于内置默认值的网关视为「未自定义」，回落到本机真实配置。"""
        if gateway_url and gateway_url != self.default_gateway:
            return gateway_url
        return self.system_defaults().get("gateway_url") or gateway_url

    def resolve_model(self, model: str | None) -> str | None:
        """把等于内置默认值的模型视为「未自定义」，回落到本机真实配置。"""
        if model and model != self.default_model:
            return model
        return self.system_defaults().get("model") or model

    def fields_by_key(self) -> dict[str, ConfigField]:
        return {f.key: f for f in self.config_fields}

    def coerce_options(self, raw: dict[str, Any]) -> dict[str, Any]:
        """按 config_fields 校验并规整用户提交的 options。"""
        coerced: dict[str, Any] = {}
        for field_ in self.config_fields:
            if field_.key in raw:
                value = raw[field_.key]
                if field_.type is FieldType.NUMBER and value is not None:
                    value = float(value) if isinstance(value, str) and "." in value else int(value)
                elif field_.type is FieldType.BOOLEAN:
                    value = bool(value)
            else:
                value = field_.default
            if value not in (None, "", [], {}):
                coerced[field_.key] = value
        for key, value in (raw or {}).items():  # 保留未知键，保持前向兼容
            coerced.setdefault(key, value)
        return coerced

    def effective_env(self, ctx: AgentContext) -> dict[str, str]:
        """按「自带网关 / 自带 API 密钥」约定生成环境变量，并叠加用户自定义项。"""
        env: dict[str, str] = {}
        if self.base_url_env and ctx.gateway_url:
            env[self.base_url_env] = ctx.gateway_url
        if self.api_key_env and ctx.api_key:
            env[self.api_key_env] = ctx.api_key
        for key, value in (ctx.option("env") or {}).items():
            env[str(key)] = str(value)
        return env

    # -- 运行时 --------------------------------------------------------------

    @abstractmethod
    def build_invocation(self, ctx: AgentContext) -> Invocation:
        """把一次运行请求解析成具体的启动描述。"""

    @abstractmethod
    def parse_line(self, line: str, ctx: AgentContext) -> Iterable[AgentEvent]:
        """把一行原始输出翻译成零个或多个规范化事件。

        解析状态统一放在 ``run.state`` 这个字典里，因此适配器本身保持无状态。
        """

    def create_run(self, ctx: AgentContext) -> "AgentRun":
        return AgentRun(self, ctx)

    async def stream(self, ctx: AgentContext) -> AsyncIterator[AgentEvent]:
        """``Runtime.API`` 型适配器使用的进程内执行入口。"""
        raise NotImplementedError(f"{self.id} 未实现进程内流式接口")
        yield  # pragma: no cover - 使其成为异步生成器

    async def probe(self, ctx: AgentContext) -> tuple[bool, str]:
        """给「连通性测试」按钮用的轻量探测：检查运行环境、凭据与连通性。"""
        if self.runtime is Runtime.CLI:
            # 探测要起一次子进程（`内核 --version`），丢到线程里跑，别占用事件循环。
            availability = await asyncio.to_thread(self.availability)
            if not availability.installed:
                return False, availability.detail or "缺少可执行文件"
            if not ctx.api_key and not self.api_key_env:
                return True, "CLI 已就绪（该适配器无需 API 密钥）"
            if not ctx.api_key:
                return True, "CLI 已就绪（使用环境中的默认凭据）"
            return True, f"配置校验通过（{availability.version or '本地 CLI'}）"
        return await self.probe_api(ctx)

    async def probe_api(self, ctx: AgentContext) -> tuple[bool, str]:
        return True, "该适配器未提供探测实现"

    # -- 技能 ----------------------------------------------------------------

    def skill_roots(self) -> list[SkillRoot]:
        """本内核的技能根（「技能放在哪」的权威声明）。

        适配器只声明**路径与来源**，框架据此统一扫描（编目）。导入 / 市场安装 / 跨内核
        复制都写进平台托管的用户技能库（按用户隔离），**不再**写这些内核全局目录；
        :attr:`SkillRoot.installable` 仍标记本机可写根，用于升级前老市场技能的卸载护栏。
        默认没有技能根；有技能目录的内核（Claude Code、Codex）覆盖此钩子。实现必须是
        尽力而为的：目录不存在、权限不足都不允许抛错，返回空列表即可。
        """
        return []

    def discover_skills(self) -> list[SkillSpec]:
        """交给平台统一编目的技能（只读扫描本机文件系统）。

        默认没有技能；据有技能目录的内核（Claude Code、Codex）覆盖此钩子。
        实现必须是尽力而为的：目录不存在、权限不足、frontmatter 损坏都不允许抛错。
        """
        return []

    def seed_skills(self) -> list[str]:
        """让内核把**自带的系统技能**落到它自己的技能目录里（幂等、尽力而为）。

        有些内核的内置技能不随安装包分发，要由内核自己「首次运行」时才写进
        ``~/.<内核>/skills``（Codex 的 ``.system``、Hermes 的 ``skills`` 都是这样）。
        平台在**安装 / 更新之后**和**启动扫描之前**调用此钩子补齐，否则「技能」页
        永远只有市场技能，看不到内核自带的那些。

        返回本次实际执行的动作描述（用于日志）；无需播种时返回空列表。
        实现绝不能抛错：播种失败只意味着少几个技能，不能影响安装或启动。
        """
        return []

    def skill_mount(self, ctx: AgentContext) -> SkillMount | None:
        """声明「把智能体分配到的技能物化到哪里」；返回 ``None`` 表示该内核不支持技能。

        适配器负责声明自己内核的隔离方式（配置目录、需要携带的配置文件、
        需要关闭的内核自带技能）；框架只按声明执行，不关心具体是哪个内核。
        """
        return None

    def skill_invocation(self, tool_name: str, tool_input: dict[str, Any]) -> str | None:
        """把一次工具调用识别成「技能调用」时返回技能目录名（slug），否则返回 ``None``。

        技能在各内核里怎么被调用是**适配器**的知识：Claude Code 走内置的 ``Skill`` 工具，
        别家可能压根没有可识别的形式。框架在统计技能用量时只问这一句，命中才算一次调用 ——
        默认不识别，未声明该能力的内核不会被计入技能调用。
        """
        return None

    # -- MCP 服务器 ----------------------------------------------------------

    def discover_mcp_servers(self) -> list[McpServerSpec]:
        """交给平台统一编目的 MCP 服务器（只读扫描本机已有的内核配置）。

        默认没有；能挂 MCP 的内核（Claude Code、Codex、Gemini CLI）覆盖此钩子。
        实现必须是尽力而为的：配置不存在、损坏、权限不足都不允许抛错。
        """
        return []

    def mcp_mount(self, ctx: AgentContext) -> McpMount | None:
        """声明「把智能体分配到的 MCP 服务器写到哪里」；``None`` 表示该内核不支持 MCP。

        ``ctx.mcp_servers`` 是框架解密后注入的分配结果（含明文密钥），适配器负责按自己
        内核的配置形状把它序列化进 ``McpMount.files`` / ``workspace_files``；
        框架只按声明写文件，不关心具体是哪个内核。
        """
        return None


class AgentRun:
    """把无状态的 :class:`AgentDefinition` 绑定到一次具体运行上。

    ``state`` 是本次运行私有的临时字典，适配器可以在里面保存解析状态
    （去重标记、工具调用记账等），无需任何全局可变结构。
    """

    def __init__(self, definition: AgentDefinition, ctx: AgentContext) -> None:
        self.definition = definition
        self.ctx = ctx
        self.state: dict[str, Any] = {}
        self.emitted_text = ""
        self.thinking = ""
        self.session_id = ctx.session_id
        self.tool_calls: list[dict[str, Any]] = []
        self.result: dict[str, Any] = {}
        #: 适配器声明「本次运行的事件流已经结束」，运行层据此收工（适用于常驻的 JSON-RPC 内核）。
        self.done = False
        self._outbound: list[str] = []

    def build_invocation(self) -> Invocation:
        return self.definition.build_invocation(self.ctx)

    def feed(self, line: str) -> Iterable[AgentEvent]:
        for event in self.definition.parse_line(line, self):
            self._observe(event)
            yield event

    def send(self, payload: str) -> None:
        """把一行原始内容排进「写回内核 stdin」的队列。

        适配器只生产数据，不自己碰子进程；真正的写入由运行层统一执行，
        取消、超时与凭据擦除因此仍然只有一份实现。仅对 ``Invocation.stdin_hold`` 的启动有效。
        """
        self._outbound.append(payload)

    def take_outbound(self) -> list[str]:
        """取出并清空待写队列（运行层每读完一行都会调用一次）。"""
        if not self._outbound:
            return []
        outbound = self._outbound
        self._outbound = []
        return outbound

    def mark_done(self) -> None:
        """声明事件流已结束；运行层会停止读取、关闭 stdin 并等待内核退出。"""
        self.done = True

    def _observe(self, event: AgentEvent) -> None:
        if event.type is EventType.MESSAGE_DELTA and event.text:
            self.emitted_text += event.text
        elif event.type is EventType.THINKING_DELTA and event.text:
            self.thinking += event.text
        elif event.type is EventType.SESSION:
            self.session_id = event.payload.get("session_id", self.session_id)
        elif event.type is EventType.TOOL_CALL:
            self.tool_calls.append({"id": event.call_id, "name": event.name, **(event.payload or {})})

    def finish(self) -> Iterable[AgentEvent]:
        """给需要在结束时补发事件的适配器使用的钩子。"""
        return ()
