"""把一个 HTTP 请求变成一次真实的智能体运行，并把结果落库。"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..agents import AgentContext, AgentDefinition, EventType, get_agent
from ..agents.events import AgentEvent, error, log, run_finished
from ..attachments import AttachmentError, attachment_relative_path, compose_with_attachments
from ..credentials import with_notice
from ..db import SessionLocal
from ..image_ingest import ingest_images
from ..models import AgentProfile, Conversation, Gateway, Message
from ..settings import get_settings
from .credentials import decrypt_for_profile
from .gateway import bind_gateway
from .manager import ActiveRun, registry
from .mcp import prepare_mcp_home
from .runner import stream_run
from .skills import prepare_skill_home
from .trace import TraceRecorder

MAX_HISTORY_MESSAGES = 24
MAX_HISTORY_CHARS = 40_000
DEFAULT_TITLE = "新对话"

logger = logging.getLogger(__name__)


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
    #: 这条对话是不是「刚开头」（标题还是兜底值）：是就交给调用方安排一次自动起标题
    title_pending: bool = False
    #: 是否把内核回报的 session_id 写回对话。主场运行为 True；@ 召唤轮恒为 False
    #: （召唤的智能体只回答这一轮，绝不续接 / 污染主场内核的会话记忆）。
    persist_session: bool = True
    #: 本轮的轨迹采集器；为 None 表示调试开关关闭（零写库）。
    trace: TraceRecorder | None = None


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
) -> AgentContext:
    """组装一次运行的 :class:`AgentContext`。

    ``model_override`` / ``session_id`` 只在召唤轮使用：召唤的智能体不认这条对话
    单独指定的模型（那是为主场智能体的网关挑的），传 :data:`SUMMON_IGNORE_MODEL`
    表示显式忽略之（落回召唤者自己的默认模型）；也绝不续接主场内核的会话。
    主场运行不传 ``model_override``，沿用对话级模型 → 智能体默认 → 内核默认的优先级。
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


def _clip_history(rows: list[Message]) -> list[dict[str, str]]:
    picked: list[dict[str, str]] = []
    budget = MAX_HISTORY_CHARS
    for row in reversed(rows[-MAX_HISTORY_MESSAGES:]):
        if row.role not in ("user", "assistant") or not row.content:
            continue
        budget -= len(row.content)
        if budget < 0:
            break
        picked.append({"role": row.role, "content": row.content})
    return list(reversed(picked))


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
    隔离目录重建。历史只含每轮的最终回复（``_clip_history`` 的产物），
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
    user_message = Message(conversation_id=conversation.id, role="user", content=content)
    session.add(user_message)

    history = _clip_history(list(existing))
    if summon_profile_id:
        # 召唤轮：无可续接会话是常态，共享对话历史（只含最终回复）作为前置上下文。
        ctx = await build_context(
            session, definition, profile, gateway, conversation, content, history, workspace,
            model_override=SUMMON_IGNORE_MODEL, session_id=None,
        )
        ctx.prompt = prior_context_prompt(history, ctx.prompt)
    else:
        ctx = await build_context(session, definition, profile, gateway, conversation, content, history, workspace)
    skill_warnings = await prepare_skill_home(
        session, definition, profile, conversation, ctx, scope_id=home_scope
    )
    mcp_warnings = await prepare_mcp_home(
        session, definition, profile, conversation, ctx, scope_id=home_scope
    )
    if not summon_profile_id and not supports_sessions(definition, ctx):
        ctx.session_id = None
        if not definition.capabilities.native_system_prompt:
            ctx.prompt = transcript_prompt(ctx.system_prompt, history, content)
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
        title_pending=title_pending,
        persist_session=persist_session,
        trace=trace,
    )


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
    extra: dict[str, Any] = {}
    status = "complete"
    error_text: str | None = None

    for warning in prepared.skill_warnings:
        yield log(f"技能：{warning}", "warn")
    for warning in prepared.mcp_warnings:
        yield log(f"MCP：{warning}", "warn")

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
        content = "".join(text_parts)
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
        message.usage = usage_totals
        message.error = error_text
        message.status = "error" if status == "error" else ("interrupted" if status == "cancelled" else "complete")
        prepared.conversation.updated_at = datetime.now(timezone.utc)
        session.add(message)
        session.add(prepared.conversation)
        try:
            await session.commit()
        except Exception:  # noqa: BLE001 - 落库失败也要保持客户端事件流不断
            await session.rollback()
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
    workspace = resolve_workspace(conversation)
    history_rows = (
        await session.execute(
            select(Message)
            .where(Message.conversation_id == conversation.id, Message.created_at < last_user.created_at)
            .order_by(Message.created_at)
        )
    ).scalars().all()
    history = _clip_history(list(history_rows))
    #: 重生成复用「被重跑那条用户消息」的序号（0 起），与下一次正常发送的 turn_index 对齐。
    turn_index = sum(1 for row in history_rows if row.role == "user")
    ctx = await build_context(
        session, definition, profile, gateway, conversation, last_user.content, history, workspace
    )
    skill_warnings = await prepare_skill_home(session, definition, profile, conversation, ctx)
    mcp_warnings = await prepare_mcp_home(session, definition, profile, conversation, ctx)
    if not supports_sessions(definition, ctx):
        ctx.session_id = None
        if not definition.capabilities.native_system_prompt:
            ctx.prompt = transcript_prompt(ctx.system_prompt, history, last_user.content)
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
        trace=trace,
    )


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
