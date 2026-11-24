"""IM ↔ 对话域的执行桥。

IM 进来的消息**就是用户说的话**：这里把它写成一条普通用户消息交给
``runtime.service.prepare_run`` / ``start_run``，跑完再把助手回复发回来源聊天。
这样 IM 与网页 / 手机走的是同一条运行链路（排队、取消、工具调用、落库全一致），
IM 这一层不需要知道任何智能体或内核的细节。

回复只发给**发起这一轮的那个聊天**：运行结束的事件里带着来源聊天，其它来源
（网页、手机、定时任务）的轮次不会往聊天软件里外发。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .. import attachments
from ..db import SessionLocal
from ..models import AgentProfile, Conversation
from ..runtime import titles
from ..runtime.manager import registry
from ..runtime.task_cards import strip_task_card
from ..runtime.service import (
    ConfigurationError,
    new_conversation,
    prepare_run,
    resolve_workspace,
    start_run,
)
from ..schemas import SendMessage
from .base import (
    OPTION_LOGS,
    OPTION_THINKING,
    OPTION_TOOLS,
    ChatRef,
    InboundFile,
    InboundMessage,
    clean_options,
    platform_of,
)
from .tables import ImChat, ImConnection

logger = logging.getLogger(__name__)

#: 一条 IM 消息里最多带几个附件（每个仍受 ``attachments.MAX_ATTACHMENT_BYTES`` 限制）。
MAX_ATTACHMENTS = 8


class ImReplyError(Exception):
    """回复发不出去（平台拒绝 / 连接已断），由调用方决定是否告诉用户。"""


@dataclass(slots=True)
class ReplySink:
    """一次运行把回复发回哪个聊天。"""

    chat: ChatRef
    #: 攒出来的正文（由 :func:`_collect` 填）；空串表示这一轮没有可发的内容
    text: str = ""
    #: 这一轮的终态：ok / error / cancelled
    status: str = "complete"
    #: 这个连接要不要把过程内容（思考 / 工具 / 日志）也发出去
    options: dict[str, bool] = field(default_factory=dict)


async def ensure_conversation(
    session: AsyncSession,
    connection: ImConnection,
    chat: ImChat,
) -> Conversation:
    """找到（或建出）这个聊天对应的对话：一条聊天固定一条对话，上下文才连续。

    ``Conversation`` 是**智能体快照**（见 AGENTS.md §3.6）：换绑智能体后旧对话仍属于旧智能体，
    所以这里发现目标智能体变了就另开一条新对话 —— 会话记忆本来就绑在「智能体 + 工作目录」上，
    沿用旧会话只会把新的一轮带偏（与网页里换智能体清会话是同一条规则）。
    """
    profile_id = chat.agent_profile_id or connection.agent_profile_id
    profile = await session.get(AgentProfile, profile_id) if profile_id else None
    if profile is None or profile.deleted_at is not None:
        raise ConfigurationError("这个 IM 机器人还没绑定可用的智能体，请在「设置 → IM」里选一个")
    if chat.conversation_id:
        existing = await session.get(Conversation, chat.conversation_id)
        same_agent = existing is not None and existing.agent_profile_id == profile.id
        if existing is not None and existing.archived is False and same_agent:
            return existing
        chat.conversation_id = None
    platform = platform_of(connection.platform)
    label = platform.name if platform is not None else connection.platform
    title = f"{label} · {chat.title or chat.chat_key}"[:120]
    conversation = new_conversation(profile, title, None)
    conversation.owner_id = connection.owner_id
    session.add(conversation)
    await session.flush()
    chat.conversation_id = conversation.id
    return conversation


def store_files(workspace: str, images: list[InboundFile], files: list[InboundFile]) -> list[str]:
    """把图片 / 文件写进对话工作目录，返回可直接写进提示词的相对路径。"""
    root = Path(workspace)
    stored: list[str] = []
    for index, item in enumerate([*images, *files][:MAX_ATTACHMENTS]):
        if not item.data:
            continue
        if len(item.data) > attachments.MAX_ATTACHMENT_BYTES:
            raise attachments.AttachmentError(
                f"附件「{item.name}」过大：单个附件不能超过 "
                f"{attachments.MAX_ATTACHMENT_BYTES // 1024 // 1024} MB",
                status_code=413,
            )
        name = item.name or f"im-{index + 1}.bin"
        if "." not in name:
            suffix = attachments.guess_media_type(name)
            name = f"{name}.{'jpg' if suffix == 'image/jpeg' else 'bin'}"
        stored.append(attachments.store_attachment(root, name, item.data).rel_path)
    return stored


async def dispatch_message(connection: ImConnection, message: InboundMessage) -> ReplySink:
    """把一条 IM 消息交给绑定的智能体，并等这一轮跑完（返回待发送的回复）。"""
    sink = ReplySink(
        chat=message.chat,
        options=clean_options(getattr(connection, "options", None)),
    )
    async with SessionLocal() as session:
        row = await session.get(ImConnection, connection.id)
        if row is None:
            raise ImReplyError("这个 IM 机器人已经被移除了")
        chat_row = await ensure_chat(session, row, message.chat)
        conversation = await ensure_conversation(session, row, chat_row)
        workspace = resolve_workspace(conversation)
        try:
            stored = store_files(workspace, message.images, message.files)
        except attachments.AttachmentError as exc:
            raise ImReplyError(str(exc)) from None
        content = message.text.strip()
        if not content and stored:
            content = "（收到附件，请查看）"
        payload = SendMessage(content=content, attachments=stored)
        prepared = await prepare_run(session, conversation.id, payload)
        await session.commit()
    if prepared.title_pending:
        titles.schedule(
            prepared.conversation.id,
            prepared.conversation.title,
            prepared.user_message.content,
        )
    start_run(prepared)
    await _collect(prepared.run, sink)
    return sink


def cancel_conversation(conversation_id: str) -> bool:
    """停掉某条对话正在跑的那一轮；返回「确实停了一个在跑的」。

    IM 域只经这个模块碰运行层（``service.py`` 不直接 import ``runtime.*``），
    与 :func:`dispatch_message` 是同一条边界。
    """
    return registry.cancel_for_conversation(conversation_id)


async def ensure_chat(
    session: AsyncSession, connection: ImConnection, chat: ChatRef
) -> ImChat:
    """找到（或建出）连接见过的这条聊天；``service`` 与 ``dispatch_message`` 共用这一份。"""
    row = (
        await session.execute(
            select(ImChat).where(ImChat.connection_id == connection.id, ImChat.chat_key == chat.key)
        )
    ).scalars().first()
    if row is None:
        row = ImChat(
            connection_id=connection.id,
            chat_key=chat.key,
            kind=chat.kind,
            title=chat.title,
            agent_profile_id=None,
        )
        session.add(row)
        await session.flush()
    elif chat.title and row.title != chat.title:
        row.title = chat.title
        row.updated_at = datetime.now(timezone.utc)
    return row


#: 过程内容在聊天软件里的单行上限：这些是给手机上看的小字，太长没有意义，
#: 也免得一个工具输出把整条消息撑爆（正文仍按平台上限完整发送）。
TRACE_LINE_LIMIT = 400


def _trace_line(text: str) -> str:
    """过程内容压成一行：去掉多余空行，超长截断。"""
    flat = " ".join(str(text).split())
    if len(flat) > TRACE_LINE_LIMIT:
        return flat[:TRACE_LINE_LIMIT] + "…"
    return flat


async def _collect(run, sink: ReplySink) -> None:
    """订阅这一轮的事件，攒出最终要发回的文本。

    正文（``message.delta``）与失败原因（``error``）一定发；**思考过程 / 工具调用 /
    日志**按这个连接的开关决定（默认都不发，那是控制台的细节）。开启后它们以引用块
    的形式附在正文之后，与正文明确分开，避免和回答混在一起。
    """
    options = sink.options or {}
    parts: list[str] = []
    traces: list[str] = []
    failure: str | None = None
    try:
        async for event in run.subscribe():
            kind = event.get("type")
            if kind == "message.delta" and event.get("text"):
                parts.append(str(event["text"]))
            elif kind == "thinking.delta" and event.get("text") and options.get(OPTION_THINKING):
                traces.append(f"> 💭 {_trace_line(event['text'])}")
            elif kind == "tool.call" and options.get(OPTION_TOOLS):
                name = event.get("name") or "工具"
                traces.append(f"> 🔧 调用 {name}")
            elif kind == "tool.result" and options.get(OPTION_TOOLS):
                payload = event.get("payload") or {}
                output = _trace_line(payload.get("output") or "")
                mark = "⚠️" if payload.get("is_error") else "↩️"
                traces.append(f"> {mark} {output}" if output else f"> {mark} （无输出）")
            elif kind == "log" and options.get(OPTION_LOGS):
                level = str((event.get("payload") or {}).get("level") or "info")
                mark = "⚠️" if level in ("warn", "warning", "error") else "·"
                traces.append(f"> {mark} {_trace_line(event.get('text') or '')}")
            elif kind == "error":
                failure = str(event.get("text") or "").strip() or failure
            elif kind == "run.finished":
                sink.status = str((event.get("payload") or {}).get("status") or "ok")
    finally:
        # 任务卡协议块只属于控制台卡片，聊天软件里必须剥掉，不能把 JSON 发出去。
        body = strip_task_card("".join(parts)).strip()
        # 过程内容跟在正文后面，用空行隔开；没有正文时它就是全部内容。
        sink.text = "\n\n".join(part for part in [body, "\n".join(traces)] if part)
        # 失败而正文为空时必须说清楚，否则用户在聊天软件里只会看到「没反应」。
        if body == "" and not traces:
            if sink.status == "cancelled":
                sink.text = "（这一轮被中断了）"
            elif failure:
                sink.text = f"这一轮没能完成：{failure}"
        elif failure and sink.status == "error":
            sink.text = f"{sink.text}\n\n这一轮没能完成：{failure}"


__all__ = [
    "ImReplyError",
    "ReplySink",
    "cancel_conversation",
    "dispatch_message",
    "ensure_chat",
    "ensure_conversation",
    "store_files",
]
