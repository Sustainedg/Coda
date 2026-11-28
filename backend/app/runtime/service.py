"""把一个 HTTP 请求变成一次真实的智能体运行，并把结果落库。"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from starlette.concurrency import run_in_threadpool

from ..agents import AgentContext, AgentDefinition, EventType, Runtime, get_agent
from ..agents.events import AgentEvent, error, log, run_finished
from ..attachments import AttachmentError, attachment_relative_path, compose_with_attachments
from ..credentials import with_notice
from ..db import SessionLocal
from ..image_ingest import ingest_images
from ..models import AgentProfile, Conversation, Gateway, Message
from ..settings import get_settings
from . import context as context_service
from . import recording_gateway
from . import task_card_mcp
from . import task_cards as task_card_service
from . import usage_ledger
from .credentials import decrypt_for_profile
from .gateway import GatewayBinding, bind_gateway, bind_recording
from .manager import ActiveRun, registry
from .mcp import prepare_mcp_home
from .runner import stream_run
from .skills import prepare_skill_home
from .trace import TraceRecorder

DEFAULT_TITLE = "新对话"

logger = logging.getLogger(__name__)

#: 账号切换是内核级动作（同一台机器只有一份登录态）：按内核串行化「切换」这个动作本身，
#: 避免两个请求同时写 session 文件把它写成半截。
#: ⚠️ 它**不串行化整个运行**，也**不保证**「子进程启动那一瞬间登录态仍是目标账号」：
#: 切换完到这里真正起进程之间，另一个并发请求完全可以又切了号。这是本功能的固有限制，
#: 要彻底避免只能把锁一直持有到进程启动（会明显牺牲并发），这里不做。
_account_locks: dict[str, asyncio.Lock] = {}


def _account_lock(type_id: str) -> asyncio.Lock:
    lock = _account_locks.get(type_id)
    if lock is None:
        lock = asyncio.Lock()
        _account_locks[type_id] = lock
    return lock


async def activate_selected_account(
    profile: AgentProfile,
    gateway: Gateway | None,
    definition: AgentDefinition,
) -> list[str]:
    """把本轮要用的内核账号切到智能体选定的槽位，返回告警文案。

    只对「官方默认」链路的账号型内核生效：自定义网关自带地址与密钥，不看内核登录态。
    切换失败（槽位被删 / 文件损坏）不阻断运行 —— 沿用当前登录的账号并告警，
    否则删掉一个账号就会让整个智能体不可用。
    """
    return await activate_account_for_kernel(
        profile,
        definition,
        official_route=gateway is None or gateway.kind == "official",
    )


async def activate_account_for_kernel(
    profile: AgentProfile,
    definition: AgentDefinition,
    *,
    official_route: bool,
) -> list[str]:
    """按「这条运行走不走官方链路」切换账号；协作域等拿不到 Gateway 行的调用方用它。"""
    if not (profile.account_id and definition.supports_accounts() and official_route):
        return []
    try:
        async with _account_lock(definition.id):
            await asyncio.to_thread(definition.activate_account, profile.account_id)
    except KeyError:
        return ["指定的内核账号已不存在，本轮沿用当前登录的账号"]
    except RuntimeError as exc:
        return [f"切换到指定内核账号失败：{exc}（沿用当前登录的账号）"]
    return []


@dataclass
class PreparedRun:
    conversation: Conversation
    profile: AgentProfile
    gateway: Gateway | None
    definition: AgentDefinition
    ctx: AgentContext
    run: ActiveRun
    user_message: Message
    assistant_message: Message
    #: 技能物化过程中的说明/告警，运行开始时以 log 事件暴露
    skill_warnings: list[str] = field(default_factory=list)
    #: MCP 物化过程中的说明/告警，同为 log 事件
    mcp_warnings: list[str] = field(default_factory=list)
    #: 指定内核账号切换失败的说明；成功时为空
    account_warnings: list[str] = field(default_factory=list)
    #: 上下文自动压缩的说明（成功 / 失败 / 正在压缩），以 log 事件推给界面
    context_notices: list[str] = field(default_factory=list)
    #: 这条对话是不是「刚开头」（标题还是兜底值）：是就交给调用方安排一次自动起标题
    title_pending: bool = False
    #: 是否把内核回报的 session_id 写回对话。主场运行为 True；@ 召唤轮恒为 False
    #: （召唤的智能体只回答这一轮，绝不续接 / 污染主场内核的会话记忆）。
    persist_session: bool = True
    #: 本轮的轨迹采集器；为 None 表示调试开关关闭（零写库）。
    trace: TraceRecorder | None = None
    #: 本轮任务卡的 MCP 绑定 id；为 None 表示走围栏通道或本轮不建卡。
    #: 收尾时据此判断「是否已通过 MCP 写过卡」并吊销令牌。
    mcp_task_context_id: str | None = None


class ConfigurationError(Exception):
    pass


async def _load(session: AsyncSession, model: Any, pk: str) -> Any:
    entity = await session.get(model, pk)
    if entity is None:
        raise ConfigurationError(f"找不到 {model.__name__}：{pk}")
    return entity


def resolve_workspace(conversation: Conversation) -> str:
    """对话的工作目录：建对话时指定，没指定就按对话 ID 分配一个独立沙箱。"""
    settings = get_settings()
    raw = conversation.workspace_dir
    base = Path(raw).expanduser() if raw else settings.workspaces_root / conversation.id
    try:
        base = base.resolve()
        base.mkdir(parents=True, exist_ok=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigurationError(f"工作目录不可用：{base}（{exc}）") from exc
    return str(base)


def resolve_runtime(
    definition: AgentDefinition,
    profile: AgentProfile,
    gateway: Gateway | None,
    model_override: str | None = None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """解析一次运行要用的网关地址 / API 密钥 / 鉴权方式 / 默认模型。

    地址、密钥与鉴权方式由 ``bind_gateway`` 统一决定（只有自定义网关才注入，
    官方链路与「没选网关」同义、由内核按自己的配置处理）。

    模型优先级：这条对话单独指定的模型 → 智能体的默认模型 → 内核默认模型。
    """
    binding = bind_gateway(definition, gateway)
    model = model_override or profile.model or definition.resolve_model(None)
    return binding.url, binding.api_key, binding.auth_style, model


#: 召唤轮的模型哨兵：召唤的智能体不认这条对话单独指定的模型（那是为主场
#: 智能体的网关挑的），用 ``None`` 之外的一个不可能值告诉 ``build_context``
#: 「显式忽略对话级模型」，与「主场没有对话级模型」（同为 None）区分开。
SUMMON_IGNORE_MODEL = "__summon_ignore_model__"


async def build_context(
    session: AsyncSession,
    definition: AgentDefinition,
    profile: AgentProfile,
    gateway: Gateway | None,
    conversation: Conversation,
    prompt: str,
    history: list[dict[str, str]],
    workspace: str,
    *,
    model_override: str | None = None,
    session_id: str | None = None,
    credentials: dict[str, str] | None = None,
) -> AgentContext:
    """组装一次运行的 :class:`AgentContext`。

    ``model_override`` / ``session_id`` 只在召唤轮使用：召唤的智能体不认这条对话
    单独指定的模型（那是为主场智能体的网关挑的），传 :data:`SUMMON_IGNORE_MODEL`
    表示显式忽略之（落回召唤者自己的默认模型）；也绝不续接主场内核的会话。
    主场运行不传 ``model_override``，沿用对话级模型 → 智能体默认 → 内核默认的优先级。

    ``credentials`` 可由调用方提前解密传入：上下文估算要在压缩前就把凭据说明块
    算进去，避免同一次运行解密两遍。
    """
    # 召唤轮的哨兵 = 显式忽略对话级模型（见 SUMMON_IGNORE_MODEL）。
    if model_override == SUMMON_IGNORE_MODEL:
        model_override = None
    else:
        model_override = model_override or conversation.model
    gateway_url, api_key, auth_style, model = resolve_runtime(
        definition, profile, gateway, model_override
    )
    options = definition.coerce_options(profile.options or {})
    if model:
        options.setdefault("model", model)
    # 凭据：本地加密保存、运行时解密后按环境变量注入（见 app.credentials），永远不进提示词。
    if credentials is None:
        credentials = await decrypt_for_profile(session, profile.id)
    return AgentContext(
        agent_id=profile.id,
        agent_name=profile.name,
        options=options,
        gateway_url=gateway_url,
        api_key=api_key,
        auth_style=auth_style,
        model=model,
        system_prompt=with_notice(profile.system_prompt or None, credentials),
        credentials=credentials,
        workspace_dir=workspace,
        session_id=conversation.session_id if session_id is None else session_id,
        history=history,
        prompt=prompt,
    )


def supports_sessions(definition: AgentDefinition, ctx: AgentContext) -> bool:
    if not definition.capabilities.sessions:
        return False
    probe = getattr(definition, "resume_supported", None)
    return bool(probe(ctx)) if callable(probe) else True


def apply_context_policy(
    ctx: AgentContext,
    settings: context_service.ContextSettings,
    conversation: Conversation,
) -> None:
    """把上下文上限 / 触发阈值写进上下文，供适配器生成内核原生压缩配置。

    - 上限按「自动跟随内核真实窗口」解析（见 ``ContextSettings.for_window``）；
    - 与平台压缩共用同一个 ``threshold_tokens``，避免「面板说没超、内核却提前压」。
    """
    effective = settings.for_window(conversation.context_kernel_window_tokens)
    ctx.context_limit_tokens = effective.limit_tokens
    ctx.context_trigger_tokens = effective.threshold_tokens


def _record_provider(prepared: PreparedRun) -> str:
    """账本行的 provider：网关协议优先，官方直连回落到适配器声明的协议。"""
    return (
        getattr(prepared.gateway, "protocol", None)
        or getattr(prepared.definition, "protocol", None)
        or ""
    )


#: 记录网关代理 URL 的稳定片段：收尾判定「本轮是否走了记录网关」。
RECORDING_GATEWAY_URL_MARKER = "/api/internal/usage-gateway/"


def _recording_gateway_binding(
    definition: AgentDefinition,
    gateway: Gateway | None,
    profile: AgentProfile,
    conversation: Conversation,
    run: ActiveRun,
    model: str | None,
    run_started_at: datetime | None = None,
) -> GatewayBinding | None:
    """按开关 / 能力 / 网关类型判断是否给本轮启用记录网关；不满足返回 ``None``。

    判据是适配器声明的 ``Capabilities.recording_gateway``（「有没有注入缝」），
    不再要求 ``base_url_env``——hermes 走命令行、CodeBuddy 走 ``models.json``，
    它们的注入代码早就写好，只是过去被环境变量判据误杀。

    * 自定义网关：平台持有密钥，``inject`` 模式；
    * 官方网关 + API Key：同样 ``inject``（L2）；
    * 官方网关 + 无密钥：**只在强制 ``on``** 时启用 passthrough（L3，实验性，
      由内核用自己的 OAuth 登录态直通）；``auto`` 不碰，避免默认行为改变。
    """
    mode = recording_gateway.recording_gateway_mode()
    if mode == recording_gateway.MODE_OFF:
        return None
    if not definition.capabilities.recording_gateway:
        return None
    if gateway is None:
        return None
    if gateway.kind == "custom":
        inject_key = True
    elif gateway.api_key_encrypted:
        # 官方链路填了 API Key：平台可以接管密钥注入。
        inject_key = True
    elif mode == recording_gateway.MODE_ON and definition.runtime is Runtime.CLI:
        # 官方 OAuth：平台没有密钥，只能原样透传内核自己的鉴权头。
        # 只对 CLI 内核有意义（账号登录态在那）；API 内核没有「自带登录」这回事。
        inject_key = False
    else:
        return None
    try:
        return bind_recording(
            definition,
            gateway,
            run_id=run.id,
            owner_id=conversation.owner_id,
            conversation_id=conversation.id,
            agent_type_id=definition.id,
            agent_profile_id=profile.id,
            model=model or "",
            provider=gateway.protocol or "",
            run_started_at=run_started_at,
            inject_key=inject_key,
            # 只有 CLI 内核需要网关补轨迹；API 内核自己的采集已经更完整（防双记）。
            capture_trace=definition.runtime is Runtime.CLI,
            usage_protocol=definition.usage_protocol,
        )
    except Exception:  # noqa: BLE001 - 代理注册失败必须回退直连，不能把整轮跑挂
        logger.warning("记录网关注册失败，本轮回退直连（原生解析兜底）", exc_info=True)
        return None


def _apply_recording_gateway(ctx: AgentContext, binding: GatewayBinding | None) -> None:
    """把代理 URL / 一次性 token 写进上下文：子进程只拿代理 token，不拿上游密钥。"""
    if binding is None:
        return
    ctx.gateway_url = binding.url
    ctx.api_key = binding.api_key
    ctx.auth_style = binding.auth_style
    # passthrough（官方 OAuth）：base_url 指向代理，但平台不提供密钥，内核用自己的登录态。
    ctx.gateway_passthrough = binding.passthrough


async def _await_gateway_rows(run_id: str, timeout: float = 2.0) -> bool:
    """短暂等待记录网关把本 run 的行提交。

    代理在自己的会话里写账本；若收尾判定早于它提交，会误判成「网关没数据」而触发
    原生兜底，造成双记。这里只等一个很短的上限，拿不到就按现状继续（不阻塞用户）。

    **每次用独立会话**探测：SQLite 的读事务是快照隔离，复用主会话的旧事务会看不到
    别的连接新提交的账本行，轮询会白等。
    """
    deadline = time.monotonic() + timeout
    while True:
        async with SessionLocal() as probe:
            if await usage_ledger.has_gateway_rows(probe, run_id):
                return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.05)


async def _auto_compact_context(
    conversation: Conversation,
    rows: list[Message],
    *,
    system_prompt: str | None,
    prompt: str,
    settings: context_service.ContextSettings,
) -> list[str]:
    """用量达到阈值时压缩更早的历史；返回给运行日志的说明（失败也不打断本轮）。

    压缩是确定性的裁剪 + 摘要，不调模型、不阻塞用户继续输入；原始消息一条不删
    —— 只把「更早那一段」记进摘要并抬高偏移量，对话记录与导出保持完整。
    失败时**不改任何已存上下文**，本轮沿用原始历史。

    估算与裁剪都是纯 CPU（逐字扫正文）：丢进线程池执行，避免长对话把事件循环
    （以及正在推送的 SSE）堵住。
    """
    active = context_service.active_messages(rows, conversation)
    # 上限按「自动模式跟随内核真实窗口」解析：内核报的是模型真正的上限，
    # 平台默认 128k 只是不知道时的保守值（用户显式设过上限时以用户为准）。
    settings = settings.for_window(conversation.context_kernel_window_tokens)
    # 与面板展示同一个入口：有内核上报的快照就用快照，没有才按内容估算。
    used, _ = await run_in_threadpool(
        context_service.estimate_conversation,
        rows,
        conversation,
        system_prompt,
        prompt,
    )
    if used < settings.threshold_tokens:
        return []
    if not context_service.begin_compaction(conversation.id):
        return ["上下文压缩正在进行中，本轮沿用原上下文"]
    try:
        outcome = await run_in_threadpool(
            context_service.compact_context,
            active,
            previous_summary=conversation.context_summary,
            system_prompt=system_prompt,
            prompt=prompt,
            settings=settings,
            include_parts=bool(conversation.session_id),
            before_tokens=used,
        )
        conversation.context_summary = outcome.summary
        conversation.context_compacted_count += outcome.dropped
        conversation.context_compacted_at = datetime.now(timezone.utc)
        # 内核侧的会话不认我们裁剪后的历史：清掉会话 id，让本轮改用注入的历史（含摘要）。
        conversation.session_id = None
        context_service.finish_compaction(
            conversation.id,
            ok=True,
            before_tokens=outcome.before_tokens,
            after_tokens=outcome.after_tokens,
            dropped=outcome.dropped,
        )
        logger.info(
            "上下文已自动压缩：对话 %s 折叠 %s 条较早消息（估算 %s → %s tokens）",
            conversation.id,
            outcome.dropped,
            outcome.before_tokens,
            outcome.after_tokens,
        )
        return [
            f"上下文已自动压缩：{outcome.dropped} 条较早消息折叠为摘要"
            f"（估算 {outcome.before_tokens} → {outcome.after_tokens} tokens）"
        ]
    except Exception as exc:  # noqa: BLE001 - 压缩失败不能拖垮本轮对话
        context_service.finish_compaction(
            conversation.id, ok=False, before_tokens=used, error=str(exc)
        )
        logger.warning("上下文自动压缩失败，已保留原上下文", exc_info=True)
        return [f"上下文自动压缩失败，已保留原上下文继续本轮：{exc}"]


def _kernel_compact_notice(event: AgentEvent) -> str:
    """把内核原生压缩事件（``context.compacted``）翻成运行日志里的中文提醒。

    数字能对上就带上（压缩前后 token、折叠条数），对不上就只说发生了什么 ——
    各内核上报的字段参差，日志文案不依赖任何字段一定存在。
    """
    payload = event.payload or {}
    trigger = payload.get("trigger")
    label = "手动压缩" if trigger == "manual" else "内核自动压缩"
    pieces: list[str] = []

    def _int(value: Any) -> int | None:
        """数字字段取整；非数字 / 布尔一律 None（各内核上报字段参差）。"""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return int(value)

    pre, post, dropped = (
        _int(payload.get("pre_tokens")),
        _int(payload.get("post_tokens")),
        _int(payload.get("messages_summarized")),
    )
    if pre is not None:
        after = f"{post} tokens" if post is not None else "未知用量"
        pieces.append(f"{pre} → {after} tokens")
    if dropped is not None and dropped > 0:
        pieces.append(f"{dropped} 条较早消息")
    detail = f"（{'，'.join(pieces)}）" if pieces else ""
    return f"{label}：内核已把较早对话折叠为摘要{detail}，会话与上下文口径已按压缩后状态对齐"


def resolve_attachment(relative: str, workspace: str) -> str:
    """校验附件确实在本次运行的工作目录内，返回规范化的相对路径。

    实现与「📎 附件」提示块都在 :mod:`app.attachments`（协作域共用同一份），
    这里只把异常换成运行层的 ``ConfigurationError``。
    """
    try:
        return attachment_relative_path(relative, workspace)
    except AttachmentError as exc:
        raise ConfigurationError(str(exc)) from None


def compose_user_content(content: str, attachments: list[str] | None, workspace: str) -> str:
    """把附件写进用户提示词：文件已经落在工作目录里，这里明确点名让智能体去读。

    这样做的好处是与内核无关 —— 只要能读工作目录的文件（Claude Code / Codex / Gemini CLI /
    custom-cli 都行），附件就生效，不需要适配器或事件协议做任何改动。
    """
    paths = [resolve_attachment(item, workspace) for item in attachments or []]
    return compose_with_attachments(content, paths)


def transcript_prompt(system_prompt: str | None, history: list[dict[str, str]], prompt: str) -> str:
    """用于既不支持系统提示词参数、也不支持会话续接的智能体。"""
    lines = []
    if system_prompt:
        lines.append(f"[system]\n{system_prompt}")
    for turn in history:
        label = "user" if turn["role"] == "user" else "assistant"
        lines.append(f"[{label}]\n{turn['content']}")
    lines.append(f"[user]\n{prompt}\n\n[assistant]")
    return "\n\n".join(lines)


def prior_context_prompt(history: list[dict[str, str]], prompt: str) -> str:
    """session 型内核没有可续接会话时，把库里的最近几轮最终回复作为前置上下文。

    触发场景：刚换了智能体 / 工作目录（会话记忆被有意清掉）、手动重置记忆、
    隔离目录重建。历史只含每轮的最终回复（``context.clip_history`` 的产物），
    工具调用与推理过程不进上下文。一旦本轮跑出新的 session_id，下一轮就恢复
    内核原生续接，此注入自动退场。
    """
    if not history:
        return prompt
    lines = ["以下是本对话此前的交流记录（你尚未参与，作为背景参考）："]
    for turn in history:
        label = "用户" if turn["role"] == "user" else "智能体"
        lines.append(f"[{label}]\n{turn['content']}")
    lines.append("—— 请基于以上背景，回应用户下面这条最新的话：")
    lines.append(prompt)
    return "\n\n".join(lines)


async def load_profile(
    session: AsyncSession, profile_id: str
) -> tuple[AgentProfile, Gateway | None, AgentDefinition]:
    profile = await _load(session, AgentProfile, profile_id)
    if profile.deleted_at is not None:
        raise ConfigurationError(f"智能体「{profile.name}」已删除")
    if not profile.enabled:
        raise ConfigurationError(f"智能体「{profile.name}」已停用")
    gateway = await session.get(Gateway, profile.gateway_id) if profile.gateway_id else None
    try:
        definition = get_agent(profile.type_id)
    except KeyError as exc:
        raise ConfigurationError(str(exc)) from None
    return profile, gateway, definition


async def pick_default_profile(
    session: AsyncSession, owner_id: str | None = None
) -> AgentProfile | None:
    """新建对话时「不问就用它」的智能体，按顺序挑第一个命中的：

    1. 点过五角星的默认智能体（`is_default`）；
    2. 该用户只有一个启用的智能体时，就是它（刚装好、还没点过星的那会儿）；
    3. 都没有 → None，由调用方给出「去智能体页点个星」的中文提示。

    ``owner_id`` 非空时只在那个用户的智能体里挑；管理员传 None 才看全场。
    """
    stmt = select(AgentProfile).where(
        AgentProfile.deleted_at.is_(None),
        AgentProfile.enabled == True,  # noqa: E712
    )
    if owner_id is not None:
        stmt = stmt.where(AgentProfile.owner_id == owner_id)
    active = (await session.execute(stmt.order_by(AgentProfile.created_at))).scalars().all()
    for profile in active:
        if profile.is_default:
            return profile
    return active[0] if len(active) == 1 else None


async def prepare_run(
    session: AsyncSession,
    conversation_id: str,
    payload: Any,
    *,
    trace_enabled: bool = False,
) -> PreparedRun:
    conversation = await _load(session, Conversation, conversation_id)

    # —— @ 召唤：这一轮换被召唤的智能体作答，对话归属与会话记忆保持主场 ——————————
    # 召唤轮的三条铁规矩（都在下面逐条落实）：
    #   1. 不续接主场内核的会话（session_id=None），自己回报的 session_id 也不写回对话；
    #   2. 技能与 MCP 按「严格仅分配」物化到独立的一次性隔离目录（作用域带 run 前缀），
    #      绝不碰主场智能体在这条对话里的隔离目录 —— 同内核时两边会共用目录名；
    #   3. 不用这条对话单独指定的模型（那是为主场智能体的网关挑的），跟召唤者自己的默认。
    summon_profile_id = getattr(payload, "summon_profile_id", None)
    if summon_profile_id:
        if summon_profile_id == conversation.agent_profile_id:
            raise ConfigurationError("这个智能体就是本对话的智能体，直接发消息即可")
        profile, gateway, definition = await load_profile(session, summon_profile_id)
        persist_session = False
        home_scope = f"summon-{uuid.uuid4().hex}"
    else:
        profile, gateway, definition = await load_profile(session, conversation.agent_profile_id)
        persist_session = True
        home_scope = None

    #: 只有「主场运行 + 持久会话 + MCP 内核 + 灰度开关打开」才挂任务卡 MCP。
    mcp_task_card_enabled = bool(
        persist_session
        and not summon_profile_id
        and definition.capabilities.mcp
        and get_settings().task_card_mcp
    )
    mcp_task_context_id: str | None = None

    try:
        account_warnings = await activate_selected_account(profile, gateway, definition)

        workspace = payload.workspace_dir or resolve_workspace(conversation)
        if payload.reset_session:
            conversation.session_id = None

        existing = (
            await session.execute(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.created_at)
            )
        ).scalars().all()
        #: 本轮是该对话第几条用户消息（0 起）；只写进轨迹，不改 Message 表结构。
        turn_index = sum(1 for row in existing if row.role == "user")

        content = compose_user_content(
            payload.content, getattr(payload, "attachments", None), workspace
        )
        # 任务卡协议与凭据说明块都要在估算之前并进提示词：它们同样占窗口。
        notice = ""
        if mcp_task_card_enabled:
            mcp_task_context_id, notice = await _begin_task_card_context(
                session, conversation, payload.content
            )
        if not notice and persist_session:
            notice = await _task_card_notice(session, conversation.id, payload.content)
        prompt_text = f"{content}\n\n{notice}" if notice else content
        credentials = await decrypt_for_profile(session, profile.id)
        effective_system_prompt = with_notice(profile.system_prompt or None, credentials)

        # 落库的仍是用户原文 + 附件说明；任务卡协议只进本轮请求，不污染对话记录。
        user_message = Message(conversation_id=conversation.id, role="user", content=content)
        session.add(user_message)

        # —— 上下文自动压缩：先量一量这一轮会不会顶到阈值，再决定历史怎么裁 ————————
        existing_rows = list(existing)
        context_settings = await context_service.read_settings(session, conversation.owner_id)
        context_notices = await _auto_compact_context(
            conversation,
            existing_rows,
            system_prompt=effective_system_prompt,
            prompt=prompt_text,
            settings=context_settings,
        )
        history = context_service.build_history(existing_rows, conversation)
        if summon_profile_id:
            # 召唤轮：无可续接会话是常态，共享对话历史（只含最终回复）作为前置上下文。
            ctx = await build_context(
                session, definition, profile, gateway, conversation, prompt_text, history, workspace,
                model_override=SUMMON_IGNORE_MODEL, session_id=None, credentials=credentials,
            )
            ctx.prompt = prior_context_prompt(history, ctx.prompt)
        else:
            ctx = await build_context(
                session, definition, profile, gateway, conversation, prompt_text, history, workspace,
                credentials=credentials,
            )
        # 原生压缩口径：上限 / 阈值在估算与压缩都算完之后再下发，保证与平台压缩同一份。
        apply_context_policy(ctx, context_settings, conversation)
        # 适配器据此只预允许任务卡工具（见 Claude Code / CodeBuddy 的权限配置）。
        ctx.task_card_mcp = bool(mcp_task_context_id)
        skill_warnings = await prepare_skill_home(
            session, definition, profile, conversation, ctx, scope_id=home_scope
        )
        mcp_warnings = await prepare_mcp_home(
            session,
            definition,
            profile,
            conversation,
            ctx,
            scope_id=home_scope,
            task_card_context_id=mcp_task_context_id,
        )
        if mcp_task_context_id:
            # 家目录已物化：把令牌落进 task-card/，MCP 子进程才读得到。
            root = (
                task_card_mcp.write_credentials(mcp_task_context_id, ctx.home_dir)
                if ctx.home_dir
                else None
            )
            if root is None:
                logger.warning("任务卡 MCP 凭据无法落盘，本轮工具调用会失败")
                task_card_mcp.finish(mcp_task_context_id, "failed")
                mcp_task_context_id = None
        if not summon_profile_id and not supports_sessions(definition, ctx):
            ctx.session_id = None
            if not definition.capabilities.native_system_prompt:
                ctx.prompt = transcript_prompt(ctx.system_prompt, history, prompt_text)
                ctx.system_prompt = None
        elif not summon_profile_id and ctx.session_id is None:
            # session 型内核但没有可续接的会话（刚换智能体 / 换目录 / 重置记忆 / 隔离目录重建）：
            # 把库里的最近几轮最终回复作为前置上下文兜底，杜绝换智能体后彻底失忆。
            # 一旦本轮建立新的内核会话，下一轮自动恢复原生续接。
            ctx.prompt = prior_context_prompt(history, ctx.prompt)

        assistant_message = Message(
            conversation_id=conversation.id,
            role="assistant",
            content="",
            status="streaming",
            agent_profile_id=summon_profile_id,
        )
        session.add(assistant_message)

        # 标题还是兜底值＝这条对话刚开头：先用首行当临时标题（侧边栏立刻有名字），
        # 之后由调用方安排一次「自动起标题」把它换成概括得更准的名字（见 runtime/titles.py）。
        title_pending = conversation.title in (DEFAULT_TITLE, "")
        if title_pending and payload.content.strip():
            conversation.title = payload.content.strip().splitlines()[0][:80]
        conversation.updated_at = datetime.now(timezone.utc)

        await session.commit()
        await session.refresh(user_message)
        await session.refresh(assistant_message)

        run = registry.register(
            conversation.id,
            profile.name,
            conversation.owner_id,
            agent_profile_id=profile.id,
            agent_avatar=profile.avatar,
            agent_avatar_preset=profile.avatar_preset,
        )
        run.user_message_id = user_message.id
        run.assistant_message_id = assistant_message.id
        if mcp_task_context_id:
            # run_id / 锚点消息只有注册之后才有；此前没有 agent 在跑，不可能收到工具调用。
            task_card_mcp.bind(
                mcp_task_context_id,
                conversation.id,
                conversation.owner_id,
                run.id,
                assistant_message.id,
            )
        # 记录网关（P1，默认 off）：满足条件时把 base_url 指向本机代理、密钥换成一次性 token。
        _apply_recording_gateway(
            ctx,
            _recording_gateway_binding(
                definition, gateway, profile, conversation, run, ctx.model,
                run_started_at=assistant_message.created_at,
            ),
        )
        trace = await _start_trace(
            session,
            conversation=conversation,
            run=run,
            ctx=ctx,
            turn_index=turn_index,
            user_message_id=user_message.id,
            trace_enabled=trace_enabled,
        )
        return PreparedRun(
            conversation=conversation,
            profile=profile,
            gateway=gateway,
            definition=definition,
            ctx=ctx,
            run=run,
            user_message=user_message,
            assistant_message=assistant_message,
            skill_warnings=skill_warnings,
            mcp_warnings=mcp_warnings,
            account_warnings=account_warnings,
            context_notices=context_notices,
            title_pending=title_pending,
            persist_session=persist_session,
            trace=trace,
            mcp_task_context_id=mcp_task_context_id,
        )
    except BaseException:
        # begin() 之后到返回之间的任何异常都不会走到 consume_run 的 finally：
        # 在这里补一次收口，避免失败轮泄漏 active binding 与凭据目录（finish 幂等）。
        if mcp_task_context_id:
            task_card_mcp.finish(mcp_task_context_id, "failed")
        raise


async def _start_trace(
    session: AsyncSession,
    *,
    conversation: Conversation,
    run: ActiveRun,
    ctx: AgentContext,
    turn_index: int,
    user_message_id: str | None,
    trace_enabled: bool,
) -> TraceRecorder | None:
    """开关打开时建采集器并同步写一条 ``context_snapshot``。

    判定点唯一在这里（由 API 层从 ``current_user.trace_debug`` 传入），
    不读请求体里任何开关字段；关闭时返回 ``None``，适配器拿到的是零开销的空值。
    """
    if not trace_enabled:
        return None
    try:
        recorder = await TraceRecorder.create(
            session,
            conversation_id=conversation.id,
            owner_id=conversation.owner_id,
            run_id=run.id,
            turn_index=turn_index,
            ctx=ctx,
            user_message_id=user_message_id,
        )
    except Exception:  # noqa: BLE001 - 采集失败不能影响本轮正常启动
        logger.warning("初始化轨迹采集失败", exc_info=True)
        return None
    ctx.trace = recorder
    # 记录网关转发层按 run_id 反查采集器：CLI 内核的真实报文只能从这里补进轨迹。
    run.trace = recorder
    return recorder


def _merge_part(parts: list[dict[str, Any]], kind: str, text: str) -> None:
    """把增量文本并入时间线：与上一段同类则续写，否则另起一段。

    这样落库的 ``parts`` 会保留「思考 → 正文 → 工具调用 → 工具结果 → 正文 …」
    的真实先后顺序，而不是按类型归组。
    """
    if parts and parts[-1].get("kind") == kind:
        parts[-1]["text"] = (parts[-1].get("text") or "") + text
    else:
        parts.append({"kind": kind, "text": text})


async def _task_card_notice(
    session: AsyncSession, conversation_id: str, user_text: str
) -> str:
    """按设计 §6.2 生成任务卡协议的提示词追加段（没有就返回空串）。

    条件：已有卡 → 每轮都注入（模型要能补报勾选）；无卡且用户正文像多步任务 →
    注入建卡协议；其余不注入，闲聊不产生额外 token。

    必须在上下文估算**之前**调用：这段协议本身也占窗口，后追加会让面板少算。
    """
    try:
        card = await task_card_service.load_card(session, conversation_id)
    except Exception:  # noqa: BLE001 - 读卡失败不影响本轮正常作答
        logger.warning("读取任务进度卡片失败，本轮不注入协议", exc_info=True)
        return ""
    if card is None and not task_card_service.looks_like_task_request(user_text or ""):
        return ""
    notice = task_card_service.render_notice(card, allow_create=card is None)
    return notice or ""


async def _begin_task_card_context(
    session: AsyncSession, conversation: Conversation, user_text: str
) -> tuple[str | None, str]:
    """为 MCP 内核开启本轮任务卡能力，返回 ``(context_id, 提示词段)``。

    只在「有卡 / 用户正文像多步任务」时开启：闲聊轮不挂工具、不产生额外 token。
    读卡失败按「没有卡」处理并记日志，不影响本轮正常运行。
    """
    try:
        card = await task_card_service.load_card(session, conversation.id)
    except Exception:  # noqa: BLE001 - 读卡失败不影响本轮正常作答
        logger.warning("读取任务进度卡片失败，本轮不注入 MCP 协议", exc_info=True)
        card = None
    if card is None and not task_card_service.looks_like_task_request(user_text or ""):
        return None, ""
    context_id, _home = task_card_mcp.begin(conversation.id, conversation.owner_id)
    return context_id, task_card_mcp.render_notice(context_id, card)


async def _execute_prepared(prepared: PreparedRun) -> None:
    """在后台把一轮运行跑完，与 HTTP 连接解耦。

    浏览器刷新 / 关页面只会让 SSE 订阅者离开，**不能**终止这次运行：
    事件仍写进 ``ActiveRun`` 的缓冲与数据库，重新打开对话（或另一台设备）再订阅
    就能从缓冲重放，接着看实时输出。
    """
    run = prepared.run
    try:
        # 请求作用域的 session 在响应返回后就关闭了，后台运行必须自己开一个。
        async with SessionLocal() as session:
            conversation = await session.get(Conversation, prepared.conversation.id)
            assistant = await session.get(Message, prepared.assistant_message.id)
            if conversation is not None:
                prepared.conversation = conversation
            if assistant is not None:
                prepared.assistant_message = assistant
            async for event in consume_run(session, prepared):
                run.publish(event.to_dict())
    except asyncio.CancelledError:
        # 服务关闭：consume_run 的 finally 已经落库并登记结束，这里只补齐事件流。
        # 取消可能打断 consume_run 的 flush，这里再补一次，避免丢最后一批轨迹。
        if prepared.trace is not None:
            with contextlib.suppress(Exception):
                await prepared.trace.flush()
        run.publish(error("运行已中断").to_dict())
        run.publish(run_finished("cancelled").to_dict())
        registry.finish(run, "cancelled")
        raise
    except Exception as exc:  # noqa: BLE001 - 后台任务绝不能静默消失
        run.publish(error(str(exc)).to_dict())
        run.publish(run_finished("error", detail=str(exc)).to_dict())
        registry.finish(run, "error")
        # 运行还没进入 consume_run 就失败时，占位消息会一直停在 streaming，补一次终态。
        try:
            async with SessionLocal() as session:
                assistant = await session.get(Message, prepared.assistant_message.id)
                if assistant is not None and assistant.status == "streaming":
                    assistant.status = "error"
                    assistant.error = str(exc)
                    await session.commit()
        except Exception:  # noqa: BLE001 - 收尾失败不能掩盖上面已经上报的错误
            logger.warning("运行失败后补写助手消息终态失败", exc_info=True)
    finally:
        run.close()


def start_run(prepared: PreparedRun) -> None:
    """把已准备好的一轮运行交给后台任务执行（立即返回，SSE 只负责订阅）。

    ``turn.started`` 是框架自有的传输层事件：在启动后台任务前**同步**写进缓冲，
    保证任何订阅者（包括紧接着打开的另一台设备）都能先看到它。
    """
    run = prepared.run
    run.publish(
        {
            "type": "turn.started",
            "run_id": run.id,
            "conversation_id": prepared.conversation.id,
            "user_message_id": run.user_message_id,
            "assistant_message_id": run.assistant_message_id,
            "agent": prepared.profile.name,
        }
    )
    run.task = asyncio.create_task(_execute_prepared(prepared))


async def consume_run(session: AsyncSession, prepared: PreparedRun) -> AsyncIterator[AgentEvent]:
    """执行运行、产出事件，并把最终状态写入数据库。"""
    text_parts: list[str] = []
    parts: list[dict[str, Any]] = []
    usage_totals: dict[str, Any] = {}
    #: 内核原始 usage 小票（`usage.raw`）：保留嵌套/驼峰，供对账与统一归一化。
    raw_usage_totals: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    status = "complete"
    error_text: str | None = None
    #: 本轮最后一次模型请求的输入 token（上下文窗口真实占用）；没有上报就留空。
    context_tokens: int | None = None
    #: 上面那个快照的采集时刻。必须记「事件到达的时刻」而不是消息创建时刻——本轮消息
    #: 在开始时就建好了，而快照在本轮中间才产生（见 models.Message.context_snapshot_at）。
    context_snapshot_at: datetime | None = None
    #: 本轮内内核是否发生了原生压缩（Claude Code auto-compact 等）；落库后用于
    #: 让旧快照失效（压缩前的 context_tokens 不再代表当前窗口）。
    kernel_compacted_at: datetime | None = None
    #: 内核上报的真实上下文窗口（tokens）。上限设为「自动」时跟随它。
    kernel_window: int | None = None

    # —— 原生事件实时入账（plan A） ——
    # record 模式下 gateway 是权威源，原生 usage 只用于展示；否则每个 context.usage
    # 都实时更新本 run 唯一的一行 native/harness 账本，运行中的上下文面板即可读到。
    record_mode = RECORDING_GATEWAY_URL_MARKER in (
        (prepared.ctx.gateway_url if prepared.ctx else "") or ""
    )
    can_record_native = (
        not record_mode
        and prepared.definition is not None
        and prepared.profile is not None
        and prepared.ctx is not None
    )
    native_source = (
        usage_ledger.SOURCE_HARNESS
        if prepared.definition is not None and prepared.definition.runtime is Runtime.API
        else usage_ledger.SOURCE_NATIVE
    )
    #: 本轮原生 usage 的协议（由适配器声明 / Custom CLI 配置决定）。
    #: 收尾归一化用它，字段名探测只作兜底；未验证协议保持 None。
    native_usage_protocol = (
        prepared.definition.usage_protocol_for(prepared.ctx)
        if prepared.definition is not None and prepared.ctx is not None
        else None
    )
    #: 上一次已写入账本的窗口值：同值重复上报不重复写库（app-server 可能高频上报）。
    live_context_written: int | None = None

    for warning in prepared.skill_warnings:
        yield log(f"技能：{warning}", "warn")
    for warning in prepared.mcp_warnings:
        yield log(f"MCP：{warning}", "warn")
    for warning in prepared.account_warnings:
        yield log(f"账号：{warning}", "warn")
    for notice in prepared.context_notices:
        # 上下文自动压缩的说明：成功 / 失败都要让用户看得到（按钮与详情面板也会展示）。
        yield log(notice, "info")

    try:
        async for event in stream_run(prepared.definition, prepared.ctx, prepared.run):
            # 采集是旁路：工具事件在这里统一落轨迹（两条 runtime 汇合后唯一的稳定位置）。
            if prepared.trace is not None:
                await prepared.trace.on_event(event)
            if event.type is EventType.MESSAGE_DELTA and event.text:
                text_parts.append(event.text)
                _merge_part(parts, "text", event.text)
            elif event.type is EventType.THINKING_DELTA and event.text:
                _merge_part(parts, "thinking", event.text)
            elif event.type is EventType.TOOL_CALL:
                parts.append(
                    {"kind": "tool_call", "id": event.call_id, "name": event.name, **(event.payload or {})}
                )
            elif event.type is EventType.TOOL_RESULT:
                parts.append(
                    {
                        "kind": "tool_result",
                        "id": event.call_id,
                        "output": event.payload.get("output"),
                        "is_error": event.payload.get("is_error", False),
                    }
                )
            elif event.type is EventType.USAGE:
                usage_totals.update({k: v for k, v in (event.payload or {}).items() if v is not None})
            elif event.type is EventType.USAGE_RAW:
                raw_usage_totals.update(
                    {k: v for k, v in (event.payload or {}).items() if v is not None}
                )
            elif event.type is EventType.CONTEXT_USAGE:
                # 只记最后一次（最接近当前窗口的占用）；非法值不能污染落库。
                tokens = (event.payload or {}).get("tokens")
                if (
                    isinstance(tokens, (int, float))
                    and not isinstance(tokens, bool)
                    and tokens > 0
                ):
                    context_tokens = int(tokens)
                    context_snapshot_at = datetime.now(timezone.utc)
                    if can_record_native and context_tokens != live_context_written:
                        # 实时入账：独立会话提交，运行中的面板轮询即可看到原生窗口。
                        live_context_written = context_tokens
                        await usage_ledger.record_native_snapshot_isolated(
                            run_id=prepared.run.id,
                            source=native_source,
                            context_tokens=context_tokens,
                            usage_protocol=native_usage_protocol,
                            started_at_hint=prepared.assistant_message.created_at,
                            owner_id=prepared.conversation.owner_id,
                            conversation_id=prepared.conversation.id,
                            agent_type_id=prepared.definition.id,
                            agent_profile_id=prepared.profile.id,
                            model=prepared.ctx.model or "",
                            provider=_record_provider(prepared),
                        )
            elif event.type is EventType.CONTEXT_WINDOW:
                # 内核上报的真实上下文窗口（Claude Code effective_window / Codex
                # modelContextWindow）。上限设为「自动」时平台跟着它走，避免在 200k / 1M
                # 窗口的内核上仍按 128k 默认值提前压缩。非法值不污染落库。
                window = (event.payload or {}).get("tokens")
                if (
                    isinstance(window, (int, float))
                    and not isinstance(window, bool)
                    and window >= 1
                ):
                    kernel_window = int(window)
            elif event.type is EventType.CONTEXT_COMPACTED:
                # 内核原生压缩（如 Claude Code auto-compact）：旧消息已在内核侧折叠成摘要。
                # 快照口径必须重置——之前上报的 context_tokens 已不代表压缩后的窗口，
                # 下一轮估算回落到「按内容估算」档，避免面板拿着旧快照说「已用 150k」。
                #
                # 时间戳取**事件到达时刻**（而不是本轮收尾时刻）：失效判定要和快照的
                # 采集时刻比大小，用收尾时刻会把本轮压缩后重新上报的快照一起误杀。
                kernel_compacted_at = datetime.now(timezone.utc)
                # 压缩后的窗口占用若内核有报（post_tokens），它就是最新快照。
                post = (event.payload or {}).get("post_tokens")
                if (
                    isinstance(post, (int, float))
                    and not isinstance(post, bool)
                    and post > 0
                ):
                    context_tokens = int(post)
                    context_snapshot_at = datetime.now(timezone.utc)
                yield log(_kernel_compact_notice(event), "info")
            elif event.type is EventType.SESSION:
                # 只有主场运行才把内核回报的会话 id 写回对话；@ 召唤轮回报的
                # session_id 属于召唤者自己的一次性会话，写回去会让主场内核下一轮
                # 拿别人的会话 id 去 resume（直接报错或张冠李戴）。
                if prepared.persist_session:
                    prepared.conversation.session_id = event.payload.get("session_id")
                    session.add(prepared.conversation)
            elif event.type is EventType.ERROR:
                status = "error"
                # 适配器把错误文案放在事件的 ``text`` 上（``events.error()``）；只有个别
                # 适配器会额外塞进 payload，所以两处都看，别丢掉真正的原因。
                error_text = event.text or event.payload.get("message") or "智能体返回了错误"
            elif event.type is EventType.RUN_FINISHED:
                extra = dict(event.payload or {})
                if extra.get("status") in ("error", "cancelled"):
                    status = extra["status"]

            yield event
    except Exception as exc:  # noqa: BLE001 - 绝不让异常静默地中断整条事件流
        error_text = str(exc)
        status = "error"
        yield error(str(exc))
        yield run_finished("error", detail=str(exc))
    finally:
        # —— 计量收尾：记录网关优先、原生兜底、互斥不双记（计划 §5.2 / §5.5） ——
        # 网关在自己的会话里写账本，可能比内核退出晚几十毫秒：短暂等一下再判权。
        authority = "none"
        ledger_context: int | None = None
        ledger_at: datetime | None = None
        try:
            # 独立会话判权 / 读快照：SQLite 读事务是快照隔离，复用主会话看不到
            # 记录网关在别的连接上刚提交的账本行。
            async with SessionLocal() as probe:
                authority = await usage_ledger.resolve_authority(probe, prepared.run.id)
                if record_mode and authority != usage_ledger.SOURCE_GATEWAY:
                    if await _await_gateway_rows(prepared.run.id):
                        authority = usage_ledger.SOURCE_GATEWAY
                if authority == usage_ledger.SOURCE_GATEWAY:
                    ledger_context, ledger_at = await usage_ledger.latest_context_for_run(
                        probe, prepared.run.id
                    )
        except Exception:  # noqa: BLE001 - 判权失败不能影响落库，按原生口径继续
            logger.warning("解析计量权威源失败，按原生口径继续", exc_info=True)

        # 优先用原始小票归一化；没有原始小票的适配器（Codex/Harness/Custom）继续用数值 usage。
        raw_for_ledger = raw_usage_totals or usage_totals
        if authority == usage_ledger.SOURCE_GATEWAY:
            # 一手窗口快照：取该 run 最新一次请求的 context_tokens 校准消息快照。
            if ledger_context:
                context_tokens = ledger_context
                context_snapshot_at = ledger_at or context_snapshot_at
        else:
            # 原生 / harness 收尾：把整轮计费计数补进本 run **唯一**那一行。
            # 运行中若已随 context.usage 实时建过行，这里是 update；否则是 create。
            if (
                prepared.definition is not None
                and prepared.profile is not None
                and (raw_for_ledger or context_tokens)
            ):
                counts = (
                    usage_ledger.normalize_usage(
                        raw_for_ledger, protocol=native_usage_protocol
                    )
                    if raw_for_ledger
                    else None
                )
                await usage_ledger.record_native_snapshot_isolated(
                    run_id=prepared.run.id,
                    source=native_source,
                    context_tokens=context_tokens,
                    counts=counts,
                    raw_usage=raw_for_ledger or None,
                    usage_protocol=native_usage_protocol,
                    finalize=True,
                    started_at_hint=prepared.assistant_message.created_at,
                    owner_id=prepared.conversation.owner_id,
                    conversation_id=prepared.conversation.id,
                    agent_type_id=prepared.definition.id,
                    agent_profile_id=prepared.profile.id,
                    model=(prepared.ctx.model if prepared.ctx else "") or "",
                    provider=_record_provider(prepared),
                )

        # 本轮结束：吊销代理 token（在途代理请求已持有目标对象，不受影响）。
        recording_gateway.registry.revoke_run(prepared.run.id)

        raw_text = "".join(text_parts)
        # 任务卡协议块永远不展示给用户：运行失败 / 解析失败也要剥掉原文再落库。
        content = task_card_service.strip_task_card(raw_text)
        parts = task_card_service.strip_task_card_from_parts(parts)
        # 网关生成的图只在 MCP 侧有一份：落库前取回工作目录并改写为相对路径，
        # 保证对话历史自包含（网关下线/清缓存后老对话仍能显示）。
        prefixes = get_settings().image_ingest_prefix_list
        if prefixes:
            try:
                content, parts, ingested = await ingest_images(
                    content, parts, resolve_workspace(prepared.conversation), prefixes
                )
                if ingested:
                    logger.info("已取回 %d 张生成图片到对话工作目录", ingested)
            except Exception:  # noqa: BLE001 - 取图失败不能影响落库
                logger.warning("取回生成图片失败，保留原链接", exc_info=True)
        message = prepared.assistant_message
        message.content = content
        message.parts = parts
        message.usage = raw_for_ledger
        # 前端按同一协议归一化，避免 DeepSeek 等“分离式”被当成包含式。
        message.usage_protocol = native_usage_protocol
        message.context_tokens = context_tokens
        message.context_snapshot_at = context_snapshot_at
        message.error = error_text
        message.status = "error" if status == "error" else ("interrupted" if status == "cancelled" else "complete")
        if kernel_compacted_at is not None:
            # 内核压缩发生在本轮：记下事件时刻，估算层据此让更早的快照失效
            # （见 runtime/context._snapshot_index 的内核压缩失效线）。
            prepared.conversation.context_kernel_compacted_at = kernel_compacted_at
        if kernel_window is not None:
            # 内核窗口（模型真正的上限）：落库后「自动」模式下限跟随它。
            prepared.conversation.context_kernel_window_tokens = kernel_window
        prepared.conversation.updated_at = datetime.now(timezone.utc)
        session.add(message)
        session.add(prepared.conversation)
        committed = False
        try:
            await session.commit()
            committed = True
        except Exception:  # noqa: BLE001 - 落库失败也要保持客户端事件流不断
            await session.rollback()
        # 任务进度卡片只在「消息已提交 + 本轮成功结束 + 主场运行」时应用。
        # 卡片写入内部吞异常（独立会话），绝不影响上面的消息落库。
        #
        # 双写优先级：本轮已经通过 MCP 成功写过卡 → 忽略正文围栏；否则围栏兜底。
        # 无论哪条通道，上面的 strip_task_card* 都**无条件执行**，防止模型两边都写时
        # 协议 JSON 泄漏到消息里。
        used_mcp = bool(
            prepared.mcp_task_context_id
            and task_card_mcp.was_used(prepared.mcp_task_context_id)
        )
        if committed and status == "complete" and prepared.persist_session and not used_mcp:
            block, parse_error = task_card_service.parse_task_card(raw_text)
            if block is not None:
                await task_card_service.apply_block(
                    prepared.conversation.id,
                    message.id,
                    block,
                    prepared.run.id,
                    source="fence",
                )
            elif prepared.run.task_card_block:
                # 围栏没写 / 写坏：用适配器从内核任务工具归一的快照兜底。
                if parse_error:
                    logger.warning(
                        "任务卡片块解析失败，改用内核任务快照：%s", parse_error
                    )
                kernel_block, kernel_error = task_card_service.normalize_block(
                    prepared.run.task_card_block
                )
                if kernel_block is not None:
                    await task_card_service.apply_block(
                        prepared.conversation.id,
                        message.id,
                        kernel_block,
                        prepared.run.id,
                        source="kernel",
                    )
                else:
                    logger.warning("内核任务卡快照不合法，已忽略：%s", kernel_error)
            elif parse_error:
                logger.warning("任务卡片块解析失败，已忽略：%s", parse_error)
        elif used_mcp:
            logger.info("本轮已通过 MCP 更新任务卡，忽略正文围栏块")
        # 轮末吊销令牌、删除凭据目录：成功 / 失败 / 取消都要走（finish 幂等）。
        if prepared.mcp_task_context_id:
            finish_status = (
                "complete"
                if status == "complete"
                else ("interrupted" if status == "cancelled" else "failed")
            )
            task_card_mcp.finish(prepared.mcp_task_context_id, finish_status)
        # 先落库再登记结束：重连方看到 active_run 消失时，数据库里一定已经是最终消息，
        # 不会出现「运行没了、消息还停在 streaming」的空窗。
        registry.finish(prepared.run, status)
        # 消息 commit 之后再 flush 轨迹：保证「消息已可见 → 轨迹也可读」。
        if prepared.trace is not None:
            try:
                await prepared.trace.flush()
            except Exception:  # noqa: BLE001 - 轨迹落盘失败不能影响运行收尾
                logger.warning("轨迹落盘失败", exc_info=True)


async def regenerate_last(
    session: AsyncSession, conversation_id: str, *, trace_enabled: bool = False
) -> PreparedRun:
    """用当前智能体配置重跑最近一轮用户消息。"""
    conversation = await _load(session, Conversation, conversation_id)
    rows = (
        await session.execute(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.desc())
        )
    ).scalars().all()
    last_user = next((m for m in rows if m.role == "user"), None)
    if last_user is None:
        raise ConfigurationError("没有可用于重新生成的用户消息")

    # 丢弃最后一轮用户消息之后的内容，然后重新重放这一轮
    stale = [m for m in rows if m.created_at >= last_user.created_at and m.role != "user"]
    for message in stale:
        await session.delete(message)
    await session.commit()

    profile, gateway, definition = await load_profile(session, conversation.agent_profile_id)
    #: 重生成恒为主场运行（persist_session=True），只需再看 MCP 内核与灰度开关。
    mcp_task_card_enabled = bool(
        definition.capabilities.mcp and get_settings().task_card_mcp
    )
    mcp_task_context_id: str | None = None

    try:
        account_warnings = await activate_selected_account(profile, gateway, definition)
        workspace = resolve_workspace(conversation)
        history_rows = (
            await session.execute(
                select(Message)
                .where(Message.conversation_id == conversation.id, Message.created_at < last_user.created_at)
                .order_by(Message.created_at)
            )
        ).scalars().all()
        history_rows = list(history_rows)
        # 重生成同样把任务卡协议并入本轮提示词，再参与估算（与正常发送一致）。
        notice = ""
        if mcp_task_card_enabled:
            mcp_task_context_id, notice = await _begin_task_card_context(
                session, conversation, last_user.content
            )
        if not notice:
            notice = await _task_card_notice(session, conversation.id, last_user.content)
        prompt_text = f"{last_user.content}\n\n{notice}" if notice else last_user.content
        credentials = await decrypt_for_profile(session, profile.id)
        effective_system_prompt = with_notice(profile.system_prompt or None, credentials)
        #: 与正常发送同一套上下文压缩判定（重跑一轮也要守住上限）。
        context_settings = await context_service.read_settings(session, conversation.owner_id)
        context_notices = await _auto_compact_context(
            conversation,
            history_rows,
            system_prompt=effective_system_prompt,
            prompt=prompt_text,
            settings=context_settings,
        )
        history = context_service.build_history(history_rows, conversation)
        #: 重生成复用「被重跑那条用户消息」的序号（0 起），与下一次正常发送的 turn_index 对齐。
        turn_index = sum(1 for row in history_rows if row.role == "user")
        ctx = await build_context(
            session, definition, profile, gateway, conversation, prompt_text, history, workspace,
            credentials=credentials,
        )
        apply_context_policy(ctx, context_settings, conversation)
        ctx.task_card_mcp = bool(mcp_task_context_id)
        skill_warnings = await prepare_skill_home(session, definition, profile, conversation, ctx)
        mcp_warnings = await prepare_mcp_home(
            session,
            definition,
            profile,
            conversation,
            ctx,
            task_card_context_id=mcp_task_context_id,
        )
        if mcp_task_context_id:
            root = (
                task_card_mcp.write_credentials(mcp_task_context_id, ctx.home_dir)
                if ctx.home_dir
                else None
            )
            if root is None:
                logger.warning("任务卡 MCP 凭据无法落盘，本轮工具调用会失败")
                task_card_mcp.finish(mcp_task_context_id, "failed")
                mcp_task_context_id = None
        if not supports_sessions(definition, ctx):
            ctx.session_id = None
            if not definition.capabilities.native_system_prompt:
                ctx.prompt = transcript_prompt(ctx.system_prompt, history, prompt_text)
                ctx.system_prompt = None
        elif ctx.session_id is None:
            # 与 prepare_run 同一条兜底：无可续接会话时用最终回复做前置上下文，防止重生成失忆。
            ctx.prompt = prior_context_prompt(history, ctx.prompt)

        assistant_message = Message(
            conversation_id=conversation.id, role="assistant", content="", status="streaming"
        )
        session.add(assistant_message)
        await session.commit()
        await session.refresh(assistant_message)

        run = registry.register(
            conversation.id,
            profile.name,
            conversation.owner_id,
            agent_profile_id=profile.id,
            agent_avatar=profile.avatar,
            agent_avatar_preset=profile.avatar_preset,
        )
        run.user_message_id = last_user.id
        run.assistant_message_id = assistant_message.id
        if mcp_task_context_id:
            task_card_mcp.bind(
                mcp_task_context_id,
                conversation.id,
                conversation.owner_id,
                run.id,
                assistant_message.id,
            )
        # 重生成同样走记录网关（与正常发送一致）。
        _apply_recording_gateway(
            ctx,
            _recording_gateway_binding(
                definition, gateway, profile, conversation, run, ctx.model,
                run_started_at=assistant_message.created_at,
            ),
        )
        trace = await _start_trace(
            session,
            conversation=conversation,
            run=run,
            ctx=ctx,
            turn_index=turn_index,
            user_message_id=last_user.id,
            trace_enabled=trace_enabled,
        )
        return PreparedRun(
            conversation=conversation,
            profile=profile,
            gateway=gateway,
            definition=definition,
            ctx=ctx,
            run=run,
            user_message=last_user,
            assistant_message=assistant_message,
            skill_warnings=skill_warnings,
            mcp_warnings=mcp_warnings,
            account_warnings=account_warnings,
            context_notices=context_notices,
            trace=trace,
            mcp_task_context_id=mcp_task_context_id,
        )
    except BaseException:
        if mcp_task_context_id:
            task_card_mcp.finish(mcp_task_context_id, "failed")
        raise


def new_conversation(profile: AgentProfile, title: str | None, workspace_dir: str | None) -> Conversation:
    return Conversation(
        title=(title or DEFAULT_TITLE).strip() or DEFAULT_TITLE,
        agent_profile_id=profile.id,
        workspace_dir=workspace_dir,
        # 新建即视为已读：之后只有智能体真回复了，才可能变成未读。
        last_read_at=datetime.now(timezone.utc),
    )


def ephemeral_id() -> str:
    return uuid.uuid4().hex
