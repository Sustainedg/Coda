"""IM 适配器的契约：平台元数据、入站 / 出站消息、适配器基类。

这一层是叶子层：只依赖标准库与 ``httpx`` / ``websockets``，不碰数据库、HTTP 接口与
运行层 —— 与智能体适配器（``app/agents``）是同一种思路：**「某个平台怎么收发消息」是
适配器的知识**，框架层（``im/service.py``）只负责配对、路由与状态。

平台元数据（``PLATFORMS``）是**唯一来源**：设置页的添加表单由它生成，接口只回它，
后端校验也按它来 —— 新增一个平台 = 加一条元数据 + 一个适配器模块。
"""
from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

#: 断线重连的等待序列（秒）：先短后长，长时间断网时按固定节奏重试而不是死循环。
RETRY_DELAYS = (0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0)


def retry_delay(attempt: int) -> float:
    index = min(max(attempt, 0), len(RETRY_DELAYS) - 1)
    return RETRY_DELAYS[index]


class AdapterError(Exception):
    """适配器报出的错误。

    ``permanent`` 表示「再试也没用」（凭据被拒、超出平台回复时限…）：连接应停在
    ``error`` 状态并把原因显示给用户；否则按重连节奏继续重试。
    """

    def __init__(
        self,
        message: str,
        *,
        permanent: bool = False,
        retry_after: float | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.permanent = permanent
        self.retry_after = retry_after
        self.status = status
        #: 平台自己的业务错误码（如 QQ 的 ``code``），由适配器按需填写。
        self.biz_code: Any = None


@dataclass(slots=True)
class PlatformField:
    """添加表单里的一个字段；``secret`` 的值加密落库，接口只回「已填」。"""

    key: str
    label: str
    secret: bool = False
    hint: str | None = None
    options: list[tuple[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "secret": self.secret,
            "hint": self.hint,
            "options": [{"value": value, "label": label} for value, label in self.options],
        }


@dataclass(slots=True)
class Platform:
    id: str
    name: str
    #: 界面上的小图标（前端静态资源路径，如 ``/icons/im/telegram.webp``）。放这里而不是
    #: 前端按 id 写映射：平台清单是唯一来源，新增平台时前端一行都不用改。
    icon: str = "🔌"
    fields: tuple[PlatformField, ...] = ()
    groups: bool = True
    buttons: bool = True
    available: bool = True
    note: str | None = None
    docs: str | None = None
    steps: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "icon": self.icon,
            "fields": [item.to_dict() for item in self.fields],
            "groups": self.groups,
            "buttons": self.buttons,
            "available": self.available,
            "note": self.note,
            "docs": self.docs,
            "steps": list(self.steps),
        }


#: 可连接的平台。``available=False`` 的只作为「即将支持」展示（不能添加）。
PLATFORMS: tuple[Platform, ...] = (
    Platform(
        id="telegram",
        name="Telegram",
        icon="/icons/im/telegram.webp",
        fields=(PlatformField("token", "Bot token（机器人令牌）", secret=True),),
        docs="https://t.me/BotFather",
        steps=(
            "在 Telegram 里打开 @BotFather，发送 /newbot。",
            "给机器人起名字和用户名。",
            "把 BotFather 发回来的 token 填到这里。",
        ),
    ),
    Platform(
        id="feishu",
        name="飞书 / Lark",
        icon="/icons/im/feishu.webp",
        fields=(
            PlatformField("domain", "版本", options=[("feishu", "飞书"), ("lark", "Lark")]),
            PlatformField("app_id", "App ID（应用标识）"),
            PlatformField("app_secret", "App Secret（应用密钥）", secret=True),
        ),
        docs="https://open.feishu.cn/app",
        steps=(
            "在开发者后台创建企业自建应用，并开启机器人能力。",
            "开通接收与发送消息所需的权限。",
            "把 App ID / App Secret 填到这里。",
            "在「事件与回调」里选择长连接方式，订阅接收消息事件，然后发布版本。",
        ),
    ),
    Platform(
        id="dingtalk",
        name="钉钉",
        icon="/icons/im/dingtalk.webp",
        fields=(
            PlatformField("client_id", "Client ID（应用标识）"),
            PlatformField("client_secret", "Client Secret（应用密钥）", secret=True),
        ),
        docs="https://open-dev.dingtalk.com/",
        buttons=False,
        steps=(
            "在开发者后台创建应用并添加机器人。",
            "把机器人的消息接收方式设为 Stream 模式，然后发布。",
            "把 Client ID / Client Secret 填到这里。",
        ),
    ),
    Platform(
        id="wecom",
        name="企业微信",
        icon="/icons/im/wecom.webp",
        fields=(
            PlatformField("bot_id", "Bot ID（机器人标识）"),
            PlatformField("secret", "Secret（机器人密钥）", secret=True),
        ),
        docs="https://work.weixin.qq.com/",
        steps=(
            "在企业微信里创建一个「智能机器人」，接收方式选长连接。",
            "把 Bot ID 与 Secret 填到这里。",
        ),
    ),
    Platform(
        id="qq",
        name="QQ",
        icon="/icons/im/qq.webp",
        fields=(
            PlatformField("app_id", "App ID（应用标识）"),
            PlatformField("app_secret", "App Secret（应用密钥）", secret=True),
        ),
        docs="https://q.qq.com/qqbot/",
        buttons=False,
        steps=(
            "在 QQ 开放平台创建机器人。",
            "从开发设置里取出 App ID / App Secret 填到这里。",
            "机器人发布之前，先把你的 QQ 号加进它的沙箱。",
        ),
    ),
    Platform(
        id="discord",
        name="Discord",
        icon="/icons/im/discord.webp",
        fields=(PlatformField("token", "Bot token（机器人令牌）", secret=True),),
        docs="https://discord.com/developers/applications",
        steps=(
            "在 Developer Portal 创建应用并打开 Bot 页。",
            "开启 Message Content Intent，重置并复制 token。",
            "用 bot scope 把机器人邀请进你的服务器。",
        ),
    ),
    Platform(
        id="slack",
        name="Slack",
        icon="/icons/im/slack.webp",
        fields=(
            PlatformField("app_token", "App-Level Token（应用令牌）", secret=True, hint="xapp-"),
            PlatformField("bot_token", "Bot Token（机器人令牌）", secret=True, hint="xoxb-"),
        ),
        docs="https://api.slack.com/apps",
        steps=(
            "创建应用并打开 Socket Mode。",
            "建一个带 connections:write 权限的 App-Level Token。",
            "添加 app_mentions:read、chat:write、im:history、files:read 权限，"
            "并订阅 app_mention 与 message.im。",
            "把应用安装到工作区，然后把两个 token 填到这里。",
        ),
    ),
    Platform(
        id="weixin",
        name="微信",
        icon="/icons/im/weixin.webp",
        available=False,
        note="微信个人号（iLink 扫码）暂未支持；企业场景请用企业微信。",
        groups=False,
        buttons=False,
    ),
)

PLATFORM_INDEX = {platform.id: platform for platform in PLATFORMS}


def platform_of(platform_id: str) -> Platform | None:
    return PLATFORM_INDEX.get(platform_id)


def platform_dicts() -> list[dict[str, Any]]:
    return [platform.to_dict() for platform in PLATFORMS]


# --- 消息 -------------------------------------------------------------------


@dataclass(slots=True)
class ChatRef:
    """一个聊天（私聊或群）。``key`` 是平台侧的会话标识。"""

    key: str
    kind: str = "direct"  # direct / group
    title: str = ""


@dataclass(slots=True)
class Sender:
    id: str
    name: str = ""


@dataclass(slots=True)
class InboundFile:
    name: str
    data: bytes
    media_type: str = ""


@dataclass(slots=True)
class InboundMessage:
    chat: ChatRef
    sender: Sender
    text: str = ""
    images: list[InboundFile] = field(default_factory=list)
    files: list[InboundFile] = field(default_factory=list)
    mentioned: bool = False
    message_id: str = ""


@dataclass(slots=True)
class InboundAction:
    chat: ChatRef
    sender: Sender
    data: str = ""
    message_id: str = ""


@dataclass(slots=True)
class Button:
    label: str
    data: str


@dataclass(slots=True)
class OutboundMessage:
    text: str
    buttons: list[Button] | None = None


@dataclass(slots=True)
class Account:
    """连接成功后的平台账号身份（显示用）。"""

    id: str
    name: str


@dataclass(slots=True)
class Handlers:
    """适配器 → 框架的回调。回调自己负责异常，适配器不关心路由细节。"""

    on_message: Callable[[InboundMessage], Awaitable[None]]
    on_action: Callable[[InboundAction], Awaitable[None]]
    on_status: Callable[..., None]


class Adapter:
    """一个平台的收发实现。"""

    id: str = ""

    async def check(self, fields: dict[str, Any]) -> Account:  # pragma: no cover
        raise NotImplementedError

    async def connect(  # pragma: no cover
        self,
        fields: dict[str, Any],
        handlers: Handlers,
        runtime: dict[str, Any] | None = None,
    ) -> "ConnectionBase":
        """建立连接。``runtime`` 是连接级的小状态（如长轮询游标），重启后由框架恢复。"""
        raise NotImplementedError


# --- 小工具 -----------------------------------------------------------------


#: 连接级「发什么」的开关：默认全部关闭，聊天里只有正文。
#: 开启后思考过程 / 工具调用 / 日志会随回复一起发到聊天软件（长内容会截断）。
OPTION_THINKING = "show_thinking"
OPTION_TOOLS = "show_tools"
OPTION_LOGS = "show_logs"

OPTION_KEYS = (OPTION_THINKING, OPTION_TOOLS, OPTION_LOGS)


def clean_options(given: Any) -> dict[str, bool]:
    """只认三个已知开关，缺省一律 False（老行 / 没传都按「都不发」）。"""
    data = given if isinstance(given, dict) else {}
    return {key: data.get(key) is True for key in OPTION_KEYS}


def required_fields(platform: Platform) -> list[str]:
    """没有默认选项、且没给默认值的必填字段。"""
    return [item.key for item in platform.fields if not item.options]


def clean_fields(
    platform: Platform,
    given: dict[str, Any],
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按平台元数据规整用户提交的字段。

    只保留元数据里声明过的键：未知键一律丢掉（防止把任意东西写进库）。
    空串表示「没填」；下拉字段没给值时用默认值或旧值。
    """
    base = base or {}
    out: dict[str, Any] = {}
    for item in platform.fields:
        value = given.get(item.key)
        if item.options:
            allowed = {option for option, _label in item.options}
            if isinstance(value, str) and value in allowed:
                out[item.key] = value
            elif isinstance(base.get(item.key), str) and base[item.key] in allowed:
                out[item.key] = base[item.key]
            else:
                out[item.key] = item.options[0][0]
            continue
        if isinstance(value, str) and value.strip() != "":
            out[item.key] = value.strip()
        elif not item.secret and item.key in base:
            out[item.key] = base[item.key]
        elif not item.secret:
            out[item.key] = ""
        # 密钥字段没给值 = 保留已存的那个（更新时用），不写进 out
    return out


async def sleep(seconds: float) -> None:
    await asyncio.sleep(max(0.0, seconds))


def json_body(response: Any) -> dict[str, Any]:
    """把 httpx 响应体解成字典；不是 JSON / 不是对象都当空字典（脏响应不该抛错）。"""
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def retry_after_of(response: Any, *, only_on_429: bool = False) -> float | None:
    """读 ``Retry-After``（秒）；缺失或格式不对返回 ``None``。"""
    if only_on_429 and response.status_code != 429:
        return None
    value = response.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def int_or(value: Any, default: int) -> int:
    """网络字段转整数；脏数据回落到默认值，而不是让整条读循环崩掉。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


#: 异常文本里出现的凭据类 URL 参数（钉钉 ticket、Slack ticket、飞书 device_id…）。
_SECRET_QUERY = re.compile(
    r"([?&](?:ticket|device_id|access_token|appsecret|secret)=)[^&\s#]+", re.I
)
#: ``scheme://user:password@host`` 形式的 userinfo。
_USERINFO = re.compile(r"(?<=://)[^/\s@]+@")
#: 形如 ``/bot<id>:<token>/`` 的路径段（Telegram 把 token 放在路径里）。
_BOT_PATH = re.compile(r"(/bot)[^/\s]+")


def scrub(text: str) -> str:
    """把异常文本里可能夹带的凭据擦掉。

    ``websockets.InvalidURI`` 之类的异常会把完整 URI 回显出来（``...?ticket=xxx``），
    而这些文本会经 ``on_status`` 落进 ``last_error`` 并原样返回给前端 —— 必须先擦再展示。
    只做「看起来像凭据」的模式替换，正常的中文报错不受影响。
    """
    cleaned = _SECRET_QUERY.sub(r"\1***", str(text or ""))
    cleaned = _USERINFO.sub("***@", cleaned)
    cleaned = _BOT_PATH.sub(r"\1***", cleaned)
    return cleaned


def reason_of(error: BaseException, limit: int = 300) -> str:
    """异常 → 可展示的简短原因（已脱敏）；空文本回落到异常类名。"""
    return scrub(str(error) or error.__class__.__name__)[:limit]


class RuntimeState:
    """连接级的小状态（长轮询游标、上下文令牌…）。

    只在内存里改，由 :class:`ConnectionBase` 负责节流落库 —— 适配器不用关心持久化，
    重启后框架把上一次保存的字典原样传回来。
    """

    def __init__(
        self,
        data: dict[str, Any] | None = None,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._data = dict(data or {})
        self._on_change = on_change

    def get(self) -> dict[str, Any]:
        return self._data

    def set(self, **patch: Any) -> None:
        self._data.update(patch)
        if self._on_change is not None:
            self._on_change()

    def bind(self, on_change: Callable[[], None] | None) -> None:
        """框架用：挂上「状态变了」的回调（节流落库由调用方负责）。"""
        self._on_change = on_change


async def _noop(_item: Any) -> None:
    return None


def _noop_status(**_kwargs: Any) -> None:
    return None


class ConnectionBase:
    """适配器连接基类：管理后台任务、代次（stop 之后旧回调一律作废）与状态上报。

    子类通常只需实现 ``open()``（一次握手）与 ``send()``，需要时覆盖 ``typing()``。
    重连由 :meth:`retry` 统一负责：间隔按 :data:`RETRY_DELAYS` 增长。
    """

    def __init__(self, handlers: Handlers | None = None) -> None:
        self.handlers = handlers if handlers is not None else Handlers(
            on_message=_noop, on_action=_noop, on_status=_noop_status
        )
        self.stopped = False
        self.generation = 0
        self.account: Account | None = None
        self._tasks: set[asyncio.Task] = set()

    # -- 状态 ---------------------------------------------------------------
    def status(self, state: str, error: str | None = None, account: Account | None = None) -> None:
        # 状态会落库并原样回到接口：先擦掉可能夹带的凭据（见 :func:`scrub`）。
        if account is not None:
            self.account = account
        if error is not None:
            self.handlers.on_status(state=state, error=scrub(error), account=account)
        else:
            self.handlers.on_status(state=state, account=account)

    def current(self, generation: int) -> bool:
        return not self.stopped and generation == self.generation

    # -- 后台任务 -----------------------------------------------------------
    def spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def stop(self) -> None:
        self.stopped = True
        self.generation += 1
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            # 只吞「被自己取消」这一种；其它异常留给任务自己上报。
            with suppress(asyncio.CancelledError):
                await task

    # -- 收发 ---------------------------------------------------------------
    async def send(self, chat: ChatRef, message: OutboundMessage) -> None:  # pragma: no cover
        raise NotImplementedError

    async def typing(self, chat: ChatRef) -> None:
        return None

    async def open(self, generation: int) -> None:  # pragma: no cover
        raise NotImplementedError

    def begin(self) -> None:
        """开始建立连接（首次握手）。

        适配器的 ``connect()`` 只负责造出对象，真正握手放在这里：失败按重连节奏处理，
        状态变化经 ``on_status`` 回到框架 —— 于是「建连失败」和「运行中掉线」是同一条路径。
        """
        self.spawn(self._reopen(0.0))

    def retry(
        self,
        generation: int,
        error: str,
        *,
        delay: float | None = None,
        attempt: int = 0,
    ) -> None:
        """关掉当前连接并在稍后重开；``generation`` 不匹配（已被 stop / 换代）时什么都不做。"""
        if not self.current(generation):
            return
        self.generation += 1
        self.status("retrying", error)
        self.spawn(self._reopen(retry_delay(attempt) if delay is None else delay))

    async def _reopen(self, wait: float) -> None:
        await sleep(wait)
        if self.stopped:
            return
        generation = self.generation
        try:
            await self.open(generation)
        except AdapterError as exc:
            if exc.permanent:
                self.status("error", str(exc))
                return
            self.retry(generation, str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络层的各种异常都要显示给用户
            self.retry(generation, reason_of(exc))


__all__ = [
    "Adapter",
    "AdapterError",
    "Account",
    "Button",
    "ChatRef",
    "ConnectionBase",
    "Handlers",
    "InboundAction",
    "InboundFile",
    "InboundMessage",
    "OutboundMessage",
    "PLATFORMS",
    "Platform",
    "PlatformField",
    "RuntimeState",
    "Sender",
    "clean_fields",
    "clean_options",
    "int_or",
    "json_body",
    "OPTION_KEYS",
    "OPTION_LOGS",
    "OPTION_THINKING",
    "OPTION_TOOLS",
    "platform_dicts",
    "platform_of",
    "reason_of",
    "required_fields",
    "retry_after_of",
    "retry_delay",
    "scrub",
    "sleep",
]
