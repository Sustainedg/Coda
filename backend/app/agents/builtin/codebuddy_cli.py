"""腾讯 CodeBuddy CLI 适配器。

CodeBuddy Code 是 Claude Code 家族的 headless 内核：``-p --output-format stream-json``
的事件形状、``--resume`` 续接、``Skill`` 工具与 ``--mcp-config --strict-mcp-config``
都与 Claude Code 同构，因此本适配器在结构上刻意与 ``claude_code.py`` 对齐。

三处必须区别对待：

* **配置目录**是 ``CODEBUDDY_CONFIG_DIR``（不是 ``CLAUDE_CONFIG_DIR``）；
* **自定义网关**只能走 ``models.json``（CodeBuddy 仅支持 OpenAI 兼容接口），
  不能像 Claude Code 那样用 ``base_url_env`` —— ``CODEBUDDY_BASE_URL`` 会被内核拼成
  ``<base>/v2/chat/completions``，那是它自家协议，不是平台的 OpenAI 网关；
* **官方链路**是「本机账号登录 + 站点选择」：交互式 TUI 里 ``/login`` 完成授权，
  session 落盘在 ``Data/Public/auth/*.info``（跟随真实 HOME，与
  ``CODEBUDDY_CONFIG_DIR`` 无关）。平台没有 TUI，但可以借 ``--serve`` 的本地 HTTP API
  做**可视化登录**（授权链接 → 二维码 → 轮询完成），实现见 ``codebuddy_login.py``。
  无头部署也可以改用 ``CODEBUDDY_API_KEY``（中国版必须同时设
  ``CODEBUDDY_INTERNET_ENVIRONMENT=internal``）或 ``CODEBUDDY_AUTH_TOKEN``，
  否则请求会打到海外站 www.codebuddy.ai 而鉴权失败。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable

from ..base import (
    AgentContext,
    AgentDefinition,
    AgentRun,
    AuthStatus,
    Capabilities,
    ConfigField,
    FieldType,
    Invocation,
    Runtime,
    RuntimeAccount,
    RuntimeProvisioner,
)
from ..events import (
    AgentEvent,
    context_compacted,
    context_usage,
    error,
    log,
    session,
    text_delta,
    thinking_delta,
    tool_call,
    tool_result,
    usage,
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
#: 账号槽位（平台侧多账号）：登录态文件是单账号的，槽位负责存档 / 切换。
#: ``auth_store_dir`` 从这里再导出，测试与文档都按这个入口找。
from .codebuddy_accounts import (
    activate as _activate_saved_account,
    auth_store_dir,
    is_logged_out,
    list_saved,
    read_live_auth,
    remove as _remove_saved_account,
)
from .util import (
    as_list,
    context_tokens,
    flatten_content,
    npm_cli,
    npm_install_argv,
    npm_prepare_argv,
    truthy,
)


#: ``compact_boundary`` 事件里 ``compact_metadata`` 的字段白名单（与 Claude Code 同一套
#: 官方 stream-json 协议，键为蛇形命名）。CodeBuddy 的 ``-p`` 会话在手动 ``/compact`` 后
#: 会发这条事件，只带 ``trigger`` / ``pre_tokens``；其余字段若将来出现也一并透传。
_COMPACT_FIELDS = (
    "trigger",
    "pre_tokens",
    "post_tokens",
    "messages_summarized",
    "cumulative_dropped_tokens",
    "duration_ms",
)

#: 分配到的 MCP 服务器写成的独立配置文件（隔离目录内），配合 ``--mcp-config`` 使用。
_MCP_FILE = "mcp.json"

#: 自定义网关写成的模型清单（隔离目录内）。CodeBuddy 只从配置目录读它。
_MODELS_FILE = "models.json"

#: 内核自己的用户级配置文件，按顺序合并（后者覆盖前者）。
_SYSTEM_SETTINGS_FILES = ("settings.json", "settings.local.json")

#: 本机的凭据缓存文件（插件选项 / MCP OAuth 等）。**账号登录**的 session 不在
#: ``CODEBUDDY_CONFIG_DIR`` 里，而是写进下面 :func:`auth_store_dir` 指向的共享数据目录
#: （见 ``FileAuthenticationStorage``）。这两份缓存仍会跟着隔离目录走，避免插件 / MCP
#: 的 OAuth token 每次对话都要重新授权。
_CREDENTIAL_FILES = (".credentials.json", "credentials.json")

#: 登录助手：内核没有 ``login`` 子命令，但 ``--serve`` 暴露了账号登录的本地 HTTP API。
#: 助手把「拉起服务 → 拿授权链接 → 渲染二维码 → 轮询完成」包成通用登录协议。
_LOGIN_HELPER = Path(__file__).resolve().with_name("codebuddy_login.py")

#: 让凭据只留进程内存的开关（内核支持，适合一次性沙箱）。
_CREDENTIALS_IN_MEMORY_ENV = "CODEBUDDY_CREDENTIALS_IN_MEMORY"

#: 官方链路的站点选择。国际站不设；中国版 / iOA / 专享 / 私有化各有取值。
_NETWORK_ENV = "CODEBUDDY_INTERNET_ENVIRONMENT"
#: 官方链路的两种无头凭据（优先级：AUTH_TOKEN > apiKeyHelper > API_KEY）。
_AUTH_TOKEN_ENV = "CODEBUDDY_AUTH_TOKEN"
_API_KEY_ENV = "CODEBUDDY_API_KEY"

#: 平台「技能市场」安装目录（``<config>/skills/.skillhub/<skill>``），可卸载。
_MARKET_DIRNAME = ".skillhub"

#: 已安装插件内部的技能深度：``<插件根>/skills/<技能>/SKILL.md``。
_PLUGIN_SKILL_DEPTH = 2

#: 内核在「本机没有可用登录态」时抛的英文原文（实测来自 ``codebuddy.js`` 的常量）。
#: 它出现在 ``result.errors`` 与 assistant 文本里，是「登录助手没能把登录态写下来」
#: 的最终表现——平台侧不翻译的话，用户只看到一句英文，不知道该去哪儿登录。
_AUTH_REQUIRED_TEXT = (
    "Authentication required. Please use /login command to sign in to your account"
)

#: 给用户看的中文提示：说清「去哪儿登录」与「登录态保存在哪台机器上」。
_AUTH_REQUIRED_HINT = (
    "CodeBuddy 尚未登录：请到「内核管理 → CodeBuddy CLI」点「登录」，"
    "用手机扫码完成授权后重试（登录态保存在本机，所有对话共用）。"
)


def _translate_auth_error(text: Any) -> str:
    """把内核的「未登录」英文报错换成中文指引。

    其他错误原样透出——内核自己的报错信息通常比平台翻译更准。
    ``result`` 在不同版本里可能是字符串、字典或数组，先统一成字符串再判断，
    免得 ``in`` 在非字符串上抛 ``TypeError`` 把整轮运行打断。
    """
    detail = text if isinstance(text, str) else str(text or "")
    if _AUTH_REQUIRED_TEXT in detail:
        return _AUTH_REQUIRED_HINT
    return detail


def config_dir(home: str | Path | None = None) -> Path:
    """CodeBuddy 的配置目录（默认 ``~/.codebuddy``，可用 ``CODEBUDDY_CONFIG_DIR`` 覆盖）。"""
    if home:
        return Path(home).expanduser()
    override = os.environ.get("CODEBUDDY_CONFIG_DIR")
    return Path(override).expanduser() if override else Path.home() / ".codebuddy"


def user_mcp_file(home: str | Path | None = None) -> Path:
    """用户级 MCP 配置（``<config>/.mcp.json``）。"""
    return config_dir(home) / ".mcp.json"


def installed_plugin_paths(home: str | Path | None = None) -> list[tuple[str, str]]:
    """已安装插件：``(插件标识, 安装目录)`` 列表；清单缺失 / 损坏时返回空列表。

    CodeBuddy 与 Claude Code 共用 ``plugins/installed_plugins.json`` 的形状
    （``{"plugins": {"<插件>@<市场>": [{installPath, scope, ...}]}}``）。
    只认清单里登记过的目录：``plugins/marketplaces/<市场>/`` 是市场货架，
    里面的技能并没有安装，编进来会让用户以为能分配、还能删。
    """
    try:
        data = json.loads(
            (config_dir(home) / "plugins" / "installed_plugins.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return []
    entries = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return []
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


def builtin_skills_dir() -> Path | None:
    """内核自带的技能目录（npm 包内的 ``builtin/skills``）；找不到时返回 ``None``。

    这个目录由``可执行文件``反推：npm 全局安装时
    ``<prefix>/bin/codebuddy`` 指向 ``<prefix>/lib/node_modules/@tencent-ai/codebuddy-code/bin/codebuddy``。
    Windows 的 ``.cmd`` 垫片不是软链，改从 ``<prefix>/node_modules/...`` 找。
    尽力而为：任何一步失败都只意味着少几个只读技能，不能影响智能体列表。
    """
    exe = shutil.which("codebuddy")
    if not exe:
        candidate = Path(os.environ.get("HOME", "")) / ".local" / "bin" / "codebuddy"
        exe = str(candidate) if candidate.is_file() else None
    if not exe:
        return None
    raw = Path(exe)
    candidates = [
        raw.resolve().parent.parent / "builtin" / "skills",          # POSIX 软链已解析
        raw.parent.parent / "builtin" / "skills",                    # 直接就是包内 bin/
        raw.parent / "node_modules" / "@tencent-ai" / "codebuddy-code" / "builtin" / "skills",
        raw.parent / ".." / "lib" / "node_modules" / "@tencent-ai" / "codebuddy-code"
        / "builtin" / "skills",
    ]
    for candidate in candidates:
        try:
            if candidate.is_dir():
                return candidate
        except OSError:  # noqa: PERF203 - 单个候选不可访问不影响其它候选
            continue
    return None


def read_system_settings(home: str | Path | None = None) -> dict[str, Any]:
    """读取 CodeBuddy 用户级配置，供界面与运行时对齐真实默认值。"""
    merged: dict[str, Any] = {}
    merged_env: dict[str, Any] = {}
    for name in _SYSTEM_SETTINGS_FILES:
        try:
            data = json.loads((config_dir(home) / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 配置缺失 / 损坏按「未配置」处理：这是尽力而为的元数据读取，
            # 绝不能因为本机没有 settings.json 就阻断智能体列表或一次真实运行。
            continue
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("env"), dict):
            merged_env.update(data["env"])
        merged.update({k: v for k, v in data.items() if k != "env"})
    if merged_env:
        merged["env"] = merged_env
    return merged


def _setting_present(name: str, file_env: dict[str, Any]) -> bool:
    """进程环境变量或内核配置的 ``env`` 里是否存在非空的同名项。"""
    value = os.environ.get(name) or file_env.get(name)
    return bool(str(value or "").strip())


def _is_root() -> bool:
    """当前进程是否以 root 身份运行（real uid，与内核的判定一致）。"""
    getuid = getattr(os, "getuid", None)
    return bool(getuid) and getuid() == 0


def _root_bypass_escape(ctx: AgentContext) -> str | None:
    """root + bypassPermissions 时要给内核声明的 ``CODEBUDDY_IS_SANDBOX`` 值。

    它在 bypass 模式下会跳过 HIGH/CRITICAL 命令的二次确认；内核只认**进程环境变量**
    （不从 settings 的 ``env`` 字段注入），平台恰好是直接写子进程 env。
    用户自己的 ``env`` 里显式配了就听用户的。
    """
    if (ctx.option("permission_mode") or "bypassPermissions") != "bypassPermissions":
        return None
    if not _is_root():
        return None
    return str((ctx.option("env") or {}).get("CODEBUDDY_IS_SANDBOX", "1"))


@register_agent
class CodeBuddyCliAgent(AgentDefinition):
    id = "codebuddy-cli"
    name = "CodeBuddy CLI"
    vendor = "Tencent"
    description = (
        "腾讯 CodeBuddy Code 的终端智能体。流式 JSONL 输出，支持会话续接、"
        "原生系统提示词、技能与 MCP；自定义网关通过 models.json 注入。"
    )
    runtime = Runtime.CLI
    executable = "codebuddy"
    provisioner = RuntimeProvisioner(
        manager="npm",
        package="@tencent-ai/codebuddy-code",
        #: 安装 / 更新走包装命令：本机既有的同名可执行文件（另一个 node 装的、
        #: 用户手写的垫片）会让 npm 直接失败在 EEXIST，先把这种占位挪开。
        install_argv=npm_install_argv("@tencent-ai/codebuddy-code", bin_name="codebuddy"),
        update_argv=npm_install_argv(
            "@tencent-ai/codebuddy-code", bin_name="codebuddy", latest=True
        ),
        uninstall_argv=npm_cli("uninstall", "-g", "@tencent-ai/codebuddy-code"),
        latest_argv=npm_cli("view", "@tencent-ai/codebuddy-code", "version"),
        prepare_argv=npm_prepare_argv("@tencent-ai/codebuddy-code"),
        #: 内核没有 ``login`` 子命令，但 ``--serve`` 的本地 HTTP API 能拿到浏览器授权
        #: 链接：助手把它渲染成二维码，按平台的通用 ``QR_IMAGE=`` 协议推给界面。
        login_argv=[sys.executable or "python3", str(_LOGIN_HELPER)],
        homepage="https://cnb.cool/codebuddy/codebuddy-code",
        note=(
            "登录：在「内核管理」点「登录」会启动一次临时的 CodeBuddy 登录服务，"
            "显示授权二维码；用手机扫码在浏览器里完成账号授权即可（登录态保存在服务"
            "所在机器上，之后所有对话共用）。默认走中国版站点，可用"
            " CODEBUDDY_LOGIN_METHOD 或 CODEBUDDY_INTERNET_ENVIRONMENT 切换。"
            "无桌面的部署也可以改用 CODEBUDDY_API_KEY（中国版需把「官方网络环境」"
            "设为 internal）或 CODEBUDDY_AUTH_TOKEN，填在智能体的「环境变量」里。"
            "自定义网关则直接走 models.json，与账号登录无关。"
        ),
    )
    #: CodeBuddy 的官方链路是「本机账号登录」，不是 API Key 网关；
    #: 这里声明的变量只用于自定义网关（models.json 里的 ``${CODEBUDDY_API_KEY}``）。
    api_key_env = "CODEBUDDY_API_KEY"
    #: 故意不声明 ``CODEBUDDY_BASE_URL``：它会被内核拼成 ``<base>/v2/chat/completions``，
    #: 与平台的 OpenAI 兼容网关语义不一致（自定义网关见 ``_sync_models_json``）。
    base_url_env = None
    #: 没有官方 API 地址：启动播种因此不会为它建「官方网关 + 默认智能体」
    #: （``bootstrap.seed_if_empty`` 对 ``default_gateway=None`` 直接跳过）。
    default_gateway = None
    default_model = "default-model"
    protocol = "openai"
    default_auth_style = "bearer"
    default_models = ["default-model"]
    docs_url = "https://cnb.cool/codebuddy/codebuddy-code"
    logo = "/icons/kernels/codebuddy.webp"
    #: 紫青渐变的方形标是真彩色的，按原色显示才认得出来。
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
            "permission_mode",
            "权限模式",
            FieldType.SELECT,
            default="bypassPermissions",
            options=[
                {"value": "bypassPermissions", "label": "bypassPermissions · 跳过全部权限检查"},
                {"value": "acceptEdits", "label": "acceptEdits · 自动接受文件修改"},
                {"value": "auto", "label": "auto · 自动判断"},
                {"value": "dontAsk", "label": "dontAsk · 不询问，直接拒绝需确认的操作"},
                {"value": "plan", "label": "plan · 只做计划不改动"},
                {"value": "default", "label": "default · 每次询问（非交互下会被拒绝）"},
            ],
            help=(
                "非交互运行（-p）没有授权弹窗：只有 bypassPermissions 之类的放行策略才能"
                "真正执行文件读写与命令。bypassPermissions 会让智能体无人值守地操作，"
                "请只在受信任 / 沙箱环境中使用；以 root 运行时本适配器会自动设置"
                " CODEBUDDY_IS_SANDBOX=1。"
            ),
        ),
        ConfigField(
            "system_prompt_mode",
            "人格注入方式",
            FieldType.SELECT,
            default="append",
            options=[
                {"value": "append", "label": "追加到 CodeBuddy 原有系统提示词之后"},
                {"value": "replace", "label": "完全替换系统提示词"},
            ],
        ),
        ConfigField(
            "network_environment",
            "官方网络环境",
            FieldType.SELECT,
            default="",
            options=[
                {"value": "", "label": "不注入 · 跟随内核默认（国际站 www.codebuddy.ai）"},
                {"value": "internal", "label": "中国版 · copilot.tencent.com"},
                {"value": "ioa", "label": "iOA 企业版"},
                {"value": "cloudhosted", "label": "专享版 · cloudhosted"},
                {"value": "selfhosted", "label": "私有化部署 · selfhosted"},
            ],
            help=(
                "只影响官方链路（「官方默认」网关或环境变量凭据），绑定自定义网关时不注入。"
                "用中国版 API 密钥必须选 internal，否则请求会打到海外站 www.codebuddy.ai "
                "而鉴权失败；专享版 / 私有化部署还需要在 settings.json 里另行配置 endpoint。"
            ),
        ),
        ConfigField("partial_messages", "逐字流式输出", FieldType.BOOLEAN, default=True),
        ConfigField(
            "autocompact",
            "原生自动压缩窗口",
            FieldType.STRING,
            advanced=True,
            placeholder="auto 或 ≥100k 的 token 数（如 400k / 400000）",
            help=(
                "透传给 CodeBuddy 的 --autocompact：上下文逼近该窗口时由内核自己把旧对话"
                "折叠成摘要。留空 = 用内核默认；填 auto = 跟随模型窗口。注意与平台「上下文」"
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

    # -- 元数据 --------------------------------------------------------------

    def system_defaults(self) -> dict[str, Any]:
        """从本机 CodeBuddy 配置里取默认模型，让界面与真实运行对齐。

        只回模型，不回网关地址：``CODEBUDDY_BASE_URL`` 是内核自家协议的入口，
        把它当平台网关会让一次运行悄悄打到错误的端点上。
        """
        settings = read_system_settings()
        file_env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
        model = (
            os.environ.get("CODEBUDDY_MODEL")
            or str(file_env.get("CODEBUDDY_MODEL") or "").strip()
            or (str(settings["model"]) if settings.get("model") else None)
        )
        return {"model": model} if model else {}

    # -- 登录状态 ------------------------------------------------------------

    def auth_status(self) -> AuthStatus:
        """尽力而为地判断官方链路是否已就绪（不联网、不消耗积分）。

        只做「配置层」判断，不验证凭据有效性：

        * 环境变量凭据 / ``apiKeyHelper`` 已配置 → ``logged_in=None``（未知，附说明）；
        * 内核的账号登录 session 已落盘（``Data/Public/auth/*.info``）→ ``logged_in=True``；
        * 凭据只留进程内存（``CODEBUDDY_CREDENTIALS_IN_MEMORY=1``）→ ``logged_in=None``；
        * 都没有 → ``logged_in=False``，并给出「点登录按钮扫码」的可直接照做建议。

        真正的有效性由内核在运行时确认；失败会以错误事件回到对话里，不会被这里掩盖。
        """
        settings = read_system_settings()
        file_env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
        if _setting_present(_AUTH_TOKEN_ENV, file_env):
            return AuthStatus(
                required=True,
                logged_in=None,
                detail="已通过 CODEBUDDY_AUTH_TOKEN 配置凭据（平台不联网校验，以运行结果为准）",
            )
        if _setting_present(_API_KEY_ENV, file_env):
            return AuthStatus(
                required=True,
                logged_in=None,
                detail="已通过 CODEBUDDY_API_KEY 配置凭据（平台不联网校验，以运行结果为准）",
            )
        if str(settings.get("apiKeyHelper") or "").strip():
            return AuthStatus(
                required=True,
                logged_in=None,
                detail="已配置 apiKeyHelper 动态获取 token（平台不联网校验）",
            )
        in_memory = str(
            file_env.get(_CREDENTIALS_IN_MEMORY_ENV)
            or os.environ.get(_CREDENTIALS_IN_MEMORY_ENV)
            or ""
        ).strip().lower()
        if in_memory in {"1", "true", "yes", "on"}:
            return AuthStatus(
                required=True,
                logged_in=None,
                detail=(
                    "已开启 CODEBUDDY_CREDENTIALS_IN_MEMORY：凭据只保存在进程内存，"
                    "平台无法从磁盘判断登录态（以运行结果为准）"
                ),
            )
        saved = list_saved()
        accounts = [
            RuntimeAccount(
                id=str(item["id"]),
                label=str(item.get("label") or item["id"]),
                detail=item.get("detail"),
                active=bool(item.get("active")),
            )
            for item in saved
        ]
        if read_live_auth() is not None and not is_logged_out():
            active = next((item for item in saved if item.get("active")), None)
            detail = "检测到本机账号登录凭据（以运行结果为准）"
            if len(saved) > 1:
                detail += f"；已保存 {len(saved)} 个账号，可在详情里切换"
            return AuthStatus(
                required=True,
                logged_in=True,
                detail=detail,
                account=str(active.get("label")) if active else None,
                accounts=accounts,
            )
        return AuthStatus(
            required=True,
            logged_in=False,
            detail=(
                "未检测到登录凭据。可在「内核管理」里点「登录」：平台会启动一次 CodeBuddy "
                "登录服务并显示授权二维码，用手机扫码完成账号授权即可"
                "（登录态保存在服务所在机器上，之后所有对话共用；已保存的账号可直接切换）。\n"
                "也可以改用环境变量凭据：在智能体的「环境变量」里配置"
                " CODEBUDDY_API_KEY（中国版需把「官方网络环境」设为 internal）或 CODEBUDDY_AUTH_TOKEN。"
            ),
            accounts=accounts,
        )

    # -- 多账号 --------------------------------------------------------------

    def activate_account(self, account_id: str) -> None:
        """把一个已保存的账号切换到「当前登录」；失败时抛 ``KeyError`` / ``RuntimeError``。"""
        _activate_saved_account(account_id)

    def remove_account(self, account_id: str) -> None:
        """删除一个已保存的账号槽位（不影响当前正在用的登录态）。"""
        _remove_saved_account(account_id)

    # -- 技能 ----------------------------------------------------------------

    def skill_roots(self) -> list[SkillRoot]:
        """本内核的技能根：平台技能市场 + 个人技能 + 自带技能 + 已安装插件技能。"""
        root = config_dir()
        roots = [
            SkillRoot(str(root / "skills" / _MARKET_DIRNAME), source_kind=MARKET_SOURCE),
            SkillRoot(str(root / "skills"), source_kind="user"),
        ]
        builtin = builtin_skills_dir()
        if builtin is not None:
            roots.append(SkillRoot(str(builtin), source_kind="system", installable=False))
        roots += [
            SkillRoot(
                path,
                source_kind="plugin",
                installable=False,
                max_depth=_PLUGIN_SKILL_DEPTH,
                namespace=plugin_name,
            )
            for plugin_name, path in installed_plugin_paths()
        ]
        return roots

    def discover_skills(self) -> list[SkillSpec]:
        """扫描本内核声明的技能根（个人技能 + 自带技能 + 插件技能 + 平台市场技能）。"""
        return scan_skill_roots(self.skill_roots(), type_id=self.id)

    def skill_mount(self, ctx: AgentContext) -> SkillMount:
        """严格仅分配：把运行环境指向只放分配技能的隔离配置目录。"""
        notes: list[str] = []
        if ctx.workspace_dir:
            extra = Path(ctx.workspace_dir) / ".codebuddy" / "skills"
            if extra.is_dir() and any(extra.iterdir()):
                notes.append(
                    f"工作目录自带的 {extra} 会被内核额外加载，这些技能不在智能体分配范围内"
                )
        root = config_dir()
        return SkillMount(
            home_name="codebuddy-home",
            # 只携带用户偏好与插件 / MCP 的 OAuth 缓存，不携带技能目录：技能由平台按分配
            # 严格重建。账号登录的 session 不在这里（它跟随真实 HOME，见 auth_store_dir）。
            carry=[
                (str(root / name), name)
                for name in (*_SYSTEM_SETTINGS_FILES, *_CREDENTIAL_FILES)
            ],
            notes=notes,
        )

    def skill_invocation(self, tool_name: str, tool_input: dict[str, Any]) -> str | None:
        """CodeBuddy 通过内置的 ``Skill`` 工具调用技能，参数 ``skill`` 是技能目录名。"""
        if tool_name != "Skill":
            return None
        name = tool_input.get("skill") or tool_input.get("command") or tool_input.get("name")
        return str(name).strip() or None

    # -- MCP -----------------------------------------------------------------

    def discover_mcp_servers(self) -> list[McpServerSpec]:
        """扫描用户级 MCP 配置（``<config>/.mcp.json`` 与旧版 ``~/.codebuddy.json``）。"""
        for path in (user_mcp_file(), Path.home() / ".codebuddy.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            specs = parse_json_servers(raw, type_id=self.id, source_kind="user")
            if specs:
                return specs
        return []

    def mcp_mount(self, ctx: AgentContext) -> McpMount:
        """把分配到的服务器写成隔离目录里的一份配置，并用 ``--strict-mcp-config`` 收窄。

        ``--mcp-config`` 与 ``--strict-mcp-config`` 一起用时，CodeBuddy **只**认这份文件：
        本机 ``<config>/.mcp.json``、工作目录的 ``.mcp.json`` 都不会被加载。
        """
        notes: list[str] = []
        if ctx.workspace_dir:
            project = Path(ctx.workspace_dir) / ".mcp.json"
            if project.is_file():
                notes.append(
                    f"工作目录自带的 {project} 不会生效（本次运行只认平台分配到的 MCP 服务器）"
                )
        return McpMount(
            home_name="codebuddy-home",
            files={_MCP_FILE: to_json_config(ctx.mcp_servers)},
            notes=notes,
        )

    # -- 构造启动命令 --------------------------------------------------------

    def build_invocation(self, ctx: AgentContext) -> Invocation:
        options = ctx.options
        argv: list[str] = [
            self.executable or "codebuddy",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if truthy(options.get("partial_messages", True)):
            argv.append("--include-partial-messages")

        if ctx.model:
            argv += ["--model", ctx.model]

        mode = options.get("permission_mode") or "bypassPermissions"
        argv += ["--permission-mode", mode]
        if mode == "bypassPermissions":
            # 非交互下必须显式跳过审批，否则文件 / 命令类工具会被直接拒绝。
            argv.append("--dangerously-skip-permissions")

        if ctx.system_prompt:
            flag = (
                "--system-prompt"
                if options.get("system_prompt_mode") == "replace"
                else "--append-system-prompt"
            )
            argv += [flag, ctx.system_prompt]

        if ctx.session_id:
            argv += ["--resume", ctx.session_id]

        # 原生 auto-compact 窗口：配置了才传，未配置走 CodeBuddy 自己的默认。
        autocompact = options.get("autocompact")
        if autocompact not in (None, ""):
            argv += ["--autocompact", str(autocompact)]

        if options.get("allowed_tools"):
            argv += ["--allowedTools", *as_list(options["allowed_tools"])]
        if options.get("disallowed_tools"):
            argv += ["--disallowedTools", *as_list(options["disallowed_tools"])]
        argv += [str(a) for a in as_list(options.get("extra_args") or [])]

        env = self.effective_env(ctx)
        # 官方链路的站点选择：只在「没绑自定义网关」时注入，第三方模型服务不需要它。
        network = str(options.get("network_environment") or "").strip()
        if network and not (ctx.gateway_url and ctx.api_key):
            env.setdefault(_NETWORK_ENV, network)
        # root + bypassPermissions 需要内核认作「有意的沙箱」，见 _root_bypass_escape
        sandbox_escape = _root_bypass_escape(ctx)
        if sandbox_escape is not None:
            env.setdefault("CODEBUDDY_IS_SANDBOX", sandbox_escape)
        # 无人值守：关掉遥测、错误上报与自动更新，避免子进程做无关的网络与磁盘动作。
        env.setdefault("DISABLE_TELEMETRY", "1")
        env.setdefault("DISABLE_ERROR_REPORTING", "1")
        env.setdefault("DISABLE_AUTOUPDATER", "1")
        env.setdefault("CI", "1")

        if ctx.home_dir:
            # 严格仅分配：内核只从隔离配置目录里读技能与 MCP。
            env["CODEBUDDY_CONFIG_DIR"] = ctx.home_dir
            mcp_file = Path(ctx.home_dir) / _MCP_FILE
            if mcp_file.is_file():
                argv += ["--mcp-config", str(mcp_file), "--strict-mcp-config"]
            self._sync_models_json(Path(ctx.home_dir), ctx)
        return Invocation(argv=argv, env=env, cwd=ctx.workspace_dir, stdin=ctx.prompt)

    def _sync_models_json(self, home: Path, ctx: AgentContext) -> None:
        """把平台网关写进 ``models.json``；没有网关时清掉上一次的残留。

        CodeBuddy 只支持 OpenAI 兼容接口，且自定义模型的 ``url`` 必须是**完整**的
        ``/chat/completions`` 路径。密钥用 ``${CODEBUDDY_API_KEY}`` 引用环境变量，
        明文不落盘（框架已把网关密钥注入子进程 env）。
        """
        target = home / _MODELS_FILE
        if not (ctx.gateway_url and ctx.api_key):
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            return
        base = ctx.gateway_url.rstrip("/")
        url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        model_id = ctx.model or self.default_model
        payload = {
            "models": [
                {
                    "id": model_id,
                    "name": model_id,
                    "vendor": "custom",
                    "url": url,
                    "apiKey": "${CODEBUDDY_API_KEY}",
                    "supportsToolCall": True,
                    "supportsImages": True,
                }
            ],
            "availableModels": [model_id],
        }
        try:
            home.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.chmod(target, 0o600)
        except OSError as exc:
            # 静默跳过会让这次运行打到官方登录态上、报一个看不懂的鉴权错误；
            # 直接把原因抛给用户，比「看起来能跑但用错了模型」好。
            raise RuntimeError(f"无法写入 CodeBuddy 的 models.json（{target}）：{exc}") from exc

    # -- 解析输出 ------------------------------------------------------------

    def parse_line(self, line: str, run: AgentRun) -> Iterable[AgentEvent]:
        # 只提示一次：root + bypassPermissions 时补了 CODEBUDDY_IS_SANDBOX
        if not run.state.get("_sandbox_noted") and _root_bypass_escape(run.ctx) == "1":
            run.state["_sandbox_noted"] = True
            yield log(
                "当前以 root 运行且勾选了 bypassPermissions：已按 CodeBuddy 的约定设置"
                " CODEBUDDY_IS_SANDBOX=1",
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
                # 内核原生压缩的分界事件（CodeBuddy 在 ``-p`` 会话执行 ``/compact`` 后发出）：
                # 旧消息已被折叠成摘要，窗口占用随之下降。与 Claude Code 同一套协议，
                # 元数据白名单透传；运行层据此提醒用户并让压缩前的快照失效。
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
                servers = payload.get("mcp_servers") or []
                if tools:
                    yield log(
                        f"会话已就绪 · 模型 {payload.get('model')} · {len(tools)} 个工具"
                        f" · MCP {len(servers)} 个"
                    )

    def _from_stream_event(self, event: dict[str, Any], run: AgentRun) -> Iterable[AgentEvent]:
        kind = event.get("type")
        if kind == "message_start":
            # 输入 token 在 ``message_start.message.usage`` 里（不是 ``event.usage``）；
            # result 里的是整轮累计，不能拿来当窗口占用。
            tokens = context_tokens((event.get("message") or {}).get("usage") or {})
            if tokens:
                yield context_usage(tokens)
            return
        if kind == "message_delta":
            # 窗口占用真正的来源：message_delta.usage 带的是「本次请求」的输入侧 token
            # （input + cache_read + cache_creation），同一轮工具循环里每次请求各报各的；
            # 而 result 里的是整轮累计，不能当窗口占用。早先这里直接跳过（以为它只有
            # output_tokens），那会让内核永远报不出窗口快照、界面只能一直显示按内容估算。
            # 若某个版本确实只有 output_tokens，context_tokens() 会算出 0，不会误报。
            tokens = context_tokens(event.get("usage") or {})
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
        tokens = context_tokens(message.get("usage") or {})
        if tokens:
            yield context_usage(tokens)
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                # 已有逐字增量，不再重复输出整段文本。
                if not run.state.get("partial"):
                    yield text_delta(_translate_auth_error(block["text"]))
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
        if payload.get("usage"):
            # 内核的用量里可能嵌套字典，只保留数值，界面更干净。
            scalar = {k: v for k, v in payload["usage"].items() if isinstance(v, (int, float))}
            if scalar:
                yield usage(**scalar)
        for src, dst in (
            ("total_cost_usd", "cost_usd"),
            ("duration_ms", "duration_ms"),
            ("num_turns", "num_turns"),
        ):
            if payload.get(src) is not None:
                run.result[dst] = payload[src]
        run.state["is_error"] = bool(payload.get("is_error"))
        if payload.get("is_error"):
            detail = payload.get("result")
            if not detail:
                # 部分版本把原因放在 errors 数组里（实测「未登录」就是这么报的）。
                errors = payload.get("errors")
                if isinstance(errors, list) and errors:
                    detail = "; ".join(str(item) for item in errors)
            yield error(_translate_auth_error(detail or "CodeBuddy 报告了一个错误"))
