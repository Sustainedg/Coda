"""平台内置 MCP 服务器的声明。

与「从内核配置扫描出来的服务器」不同，内置服务器是 Coda **随包分发**的一份预设：
启动时由 runtime 自动为每个用户播种成一条可编辑的平台通用服务器。用户不用自己找
脚本、拼启动命令；能由平台推导的参数（系统管理 MCP 的平台地址与令牌）会自动填好，
只有部署相关的业务参数（生图网关地址、模型名…）才需要用户填写。

这里只放声明，不碰数据库 / HTTP / 运行时；落库、分配与物化在
:mod:`app.runtime.mcp`。新增一个内置服务器只需要：

1. 在 :data:`BUILTIN_SERVERS` 里加一条 :class:`BuiltinServer`；
2. 确保它的 ``script`` 随 Release 一起打包（见 ``scripts/release.sh`` 与
   ``docker/Dockerfile``）。

会随部署变化的配置一律声明成 :class:`BuiltinParam`，参数值经 :mod:`app.security`
加密保存，**不写死任何模型或密钥**；``managed=True`` 的参数由平台自动维护。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

#: 仓库 / 版本目录的根：本文件位于 ``<root>/backend/app/builtin_mcp.py``。
#: 本地开发是仓库根；Docker 里是 ``/app/releases/<版本>``（或镜像自带的 ``/image-app``）。
_ROOT = Path(__file__).resolve().parent.parent.parent

#: 内置服务器脚本；release.sh / Dockerfile 负责把它们一起打包。
IMAGE_GEN_SCRIPT = _ROOT / "scripts" / "mcp" / "image_mcp_server.py"
ADMIN_SCRIPT = _ROOT / "scripts" / "mcp" / "coda_admin_mcp_server.py"
#: 任务进度卡的内部 stdio server（框架能力；在目录里只展示，不参与分配）。
TASK_CARD_SCRIPT = _ROOT / "scripts" / "mcp" / "task_card_mcp_server.py"


#: 平台托管参数的取值来源。runtime 只按这两个声明值执行，不认识具体服务器，
#: 因此新增内置服务器仍然只需要加一条声明（不需要改框架层）。
MANAGED_PUBLIC_BASE_URL = "public_base_url"
MANAGED_PLATFORM_TOKEN = "platform_token"
#: 受支持的托管来源；写错在导入时就报错，避免「声明了却静默不生效」。
MANAGED_SOURCES: tuple[str, ...] = (MANAGED_PUBLIC_BASE_URL, MANAGED_PLATFORM_TOKEN)


@dataclass(frozen=True, slots=True)
class BuiltinParam:
    """内置服务器的一个业务参数（最终以环境变量注入 stdio 进程）。"""

    key: str
    label: str
    help: str = ""
    placeholder: str = ""
    required: bool = False
    secret: bool = False
    #: 初始值；默认空串 ＝ 不预设（必须由用户按自己的部署填写）。
    default: str = ""
    #: 由平台自动填写 / 刷新（例如系统管理 MCP 的平台地址与令牌）。
    #: 这类参数不在表单里要求用户输入，界面上也标成「平台托管」。
    managed: bool = False
    #: ``managed=True`` 时必须声明取值来源（见 :data:`MANAGED_SOURCES`）。
    managed_source: str = ""

    def __post_init__(self) -> None:
        if self.managed and self.managed_source not in MANAGED_SOURCES:
            raise ValueError(
                f"内置参数 {self.key} 声明为平台托管，但 managed_source="
                f"{self.managed_source!r} 不受支持（只能是 {MANAGED_SOURCES}）"
            )


@dataclass(frozen=True, slots=True)
class BuiltinServer:
    """一个内置 MCP 服务器的声明。

    连接方式（启动命令 / 脚本参数）由平台维护；用户只填 ``params``。
    """

    key: str
    name: str
    slug: str
    description: str
    script: Path
    params: tuple[BuiltinParam, ...] = field(default_factory=tuple)
    #: 是否可以分配给智能体。``False`` = 平台内部能力（例如任务进度卡）：
    #: 由运行层按轮自动挂载，只在内置目录里展示，不参与分配 / 编辑。
    assignable: bool = True

    @property
    def command(self) -> str:
        """启动命令：用当前解释器，避免依赖 PATH 里有没有 ``python3``。"""
        return sys.executable or "python3"

    @property
    def args(self) -> tuple[str, ...]:
        return (str(self.script),)

    def env_defaults(self) -> dict[str, str]:
        """参数的初始值。密钥与模型不写死；``managed`` 参数由 runtime 另行填入。"""
        return {param.key: param.default for param in self.params}

    def params_view(self, masked: dict[str, str | None]) -> list[dict[str, object]]:
        """给界面的参数说明；``masked`` 是已保存值的掩码，不含明文。"""
        return [
            {
                "key": param.key,
                "label": param.label,
                "help": param.help,
                "placeholder": param.placeholder,
                "required": param.required,
                "secret": param.secret,
                "default": param.default,
                "masked": masked.get(param.key),
                "managed": param.managed,
            }
            for param in self.params
        ]

    def missing_required(self, env: dict[str, str]) -> list[str]:
        """返回还没填的必填参数标签（用于运行前的提示）。"""
        return [
            param.label
            for param in self.params
            if param.required and not param.managed and not (env.get(param.key) or "").strip()
        ]


IMAGE_GEN = BuiltinServer(
    key="image-gen",
    name="图片生成",
    slug="image-gen",
    description=(
        "平台内置的生图工具：调用 OpenAI 兼容网关的 /v1/images/generations 生成图片，"
        "带参考图时走 /v1/images/edits 编辑图片；结果保存到对话工作目录的 generated-images/。"
        "分配前请在「参数」里填写网关地址与模型名。"
    ),
    script=IMAGE_GEN_SCRIPT,
    params=(
        BuiltinParam(
            key="IMAGE_GEN_GATEWAY",
            label="网关地址",
            help="OpenAI 兼容的生图网关；带不带 /v1 都可以（会自动归一化）。",
            placeholder="https://api.example.com",
            required=True,
        ),
        BuiltinParam(
            key="IMAGE_GEN_MODEL",
            label="模型名称",
            help="网关上的生图模型名，例如 flux-dev / gpt-image-1；不写死，按你的网关填写。",
            placeholder="flux-dev",
            required=True,
        ),
        BuiltinParam(
            key="IMAGE_GEN_API_KEY",
            label="网关密钥",
            help="需要鉴权的网关才填；无鉴权的本地网关可以留空。",
            secret=True,
        ),
        BuiltinParam(
            key="IMAGE_GEN_TIMEOUT",
            label="超时秒数",
            help="单次生图最长等待时间；留空按 300 秒。",
            placeholder="300",
        ),
    ),
)


ADMIN = BuiltinServer(
    key="coda-admin",
    name="系统管理",
    slug="coda-admin",
    description=(
        "平台内置的系统管理工具：在对话里查看 / 创建 / 编辑智能体与协作组，"
        "管理技能与 MCP 服务器。默认只读；需要写操作时把权限模式改为 rw。"
    ),
    script=ADMIN_SCRIPT,
    params=(
        BuiltinParam(
            key="CODA_BASE_URL",
            label="平台地址",
            help="由平台自动填写（可用 CODA_PUBLIC_BASE_URL 覆盖）；带不带 /api 都可以。",
            placeholder="http://127.0.0.1:8010",
            required=True,
            default="http://127.0.0.1:8010",
            managed=True,
            managed_source=MANAGED_PUBLIC_BASE_URL,
        ),
        BuiltinParam(
            key="CODA_TOKEN",
            label="平台访问令牌",
            help="由平台自动生成并注入，无需填写；与「账号」页的个人访问令牌互不影响。",
            placeholder="pat_…",
            required=True,
            secret=True,
            managed=True,
            managed_source=MANAGED_PLATFORM_TOKEN,
        ),
        BuiltinParam(
            key="CODA_MODE",
            label="权限模式",
            help="ro＝只读（推荐，写操作直接拒绝）；rw＝可读写。",
            placeholder="ro",
            default="ro",
        ),
        BuiltinParam(
            key="CODA_TIMEOUT",
            label="超时秒数",
            help="单次平台请求最长等待时间；留空按 60 秒。",
            placeholder="60",
            default="60",
        ),
    ),
)


TASK_CARD = BuiltinServer(
    key="task-card",
    name="任务进度卡",
    slug="coda-task-card",
    description=(
        "平台内部的任务进度卡工具：MCP 内核在多步骤任务中通过它更新输入框上方的进度卡片。"
        "由平台按每轮自动挂载，不需要分配，也没有需要填写的参数；"
        "灰度开关 CODA_TASK_CARD_MCP 关闭时不会实际生效。"
    ),
    script=TASK_CARD_SCRIPT,
    # 平台内部能力：只在目录里展示，不参与分配 / 编辑（运行层每轮按 context 注入）。
    assignable=False,
)


#: 当前内置的全部服务器；新增内置服务器只需要在这里加一条。
BUILTIN_SERVERS: tuple[BuiltinServer, ...] = (IMAGE_GEN, ADMIN, TASK_CARD)

_BY_KEY = {server.key: server for server in BUILTIN_SERVERS}


def builtin_by_key(key: str | None) -> BuiltinServer | None:
    """按稳定标识取内置声明；普通服务器（``None``）返回 ``None``。"""
    return _BY_KEY.get(key or "")


__all__ = [
    "ADMIN",
    "ADMIN_SCRIPT",
    "BUILTIN_SERVERS",
    "BuiltinParam",
    "BuiltinServer",
    "IMAGE_GEN",
    "IMAGE_GEN_SCRIPT",
    "MANAGED_PLATFORM_TOKEN",
    "MANAGED_PUBLIC_BASE_URL",
    "MANAGED_SOURCES",
    "TASK_CARD",
    "TASK_CARD_SCRIPT",
    "builtin_by_key",
]
