"""对话列表的读取模型：参与者聚合与「最近活动」排序。

这些是「列表怎么排、每行带谁」的业务口径，放在 ``runtime`` 而不是 API 薄层：
接口只负责把结果序列化成响应（见 ``api/conversations.py``）。
"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..models import AgentProfile, Conversation, Message


def as_utc(value: datetime) -> datetime:
    """SQLite 不存时区：比大小前先统一成 UTC，别拿 aware 和 naive 直接比。"""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def is_unread(conversation: Conversation, last: Message | None) -> bool:
    """「AI 回复完了、但用户还没看」= 最后一条是已完成的助手消息，且晚于最后已读时间。

    生成中的 ``streaming`` 占位不算 —— 那时列表行显示的是「正在回复」的动画。
    升级前的旧对话没有 ``last_read_at``，一律当已读。
    """
    if last is None or last.role != "assistant" or last.status == "streaming":
        return False
    if conversation.last_read_at is None:
        return False
    return as_utc(last.created_at) > as_utc(conversation.last_read_at)


def participant_brief(profile: AgentProfile) -> dict:
    """列表行的头像只需要「是谁、长什么样」，别把整个智能体配置塞进对话列表。"""
    return {
        "id": profile.id,
        "name": profile.name,
        "avatar": profile.avatar,
        "avatar_preset": profile.avatar_preset,
        "avatar_file": profile.avatar_file,
    }


async def participants_by_conversation(
    session: AsyncSession, conversation_ids: list[str]
) -> dict[str, list[str]]:
    """每条对话被哪些「非主场」智能体答过（@ 召唤），按首次出场先后排列。

    一次分组查询解决，不随对话数打 N+1。主场智能体不在这里返回 —— 它由
    ``Conversation.agent_profile_id`` 直接给出，调用方拼成「主场上、召唤者后」。
    """
    if not conversation_ids:
        return {}
    rows = (
        await session.execute(
            select(
                Message.conversation_id,
                Message.agent_profile_id,
                func.min(Message.created_at).label("first_at"),
            )
            .where(
                Message.conversation_id.in_(conversation_ids),
                Message.agent_profile_id.is_not(None),
            )
            .group_by(Message.conversation_id, Message.agent_profile_id)
            .order_by(func.min(Message.created_at))
        )
    ).all()
    result: dict[str, list[str]] = {}
    for conversation_id, profile_id, _first_at in rows:
        if profile_id:
            result.setdefault(conversation_id, []).append(profile_id)
    return result


def ordered_participants(
    conversation: Conversation,
    home: AgentProfile | None,
    summoned_ids: list[str],
    profiles: Mapping[str, AgentProfile],
) -> list[AgentProfile]:
    """主场永远排第一，后面跟上被 @ 召唤过的智能体（去重、跳过已软删的）。"""
    participants = [home] if home is not None else []
    for profile_id in summoned_ids:
        profile = profiles.get(profile_id)
        if profile is not None and profile.id != conversation.agent_profile_id:
            participants.append(profile)
    return participants


def order_by_recent_activity(
    conversations: list[Conversation],
    latest: Mapping[str, Message],
    running_ids: Collection[str] = (),
) -> list[Conversation]:
    """先「AI 还在回复」，再「已经回复完」；两组内部都按最近活动倒序。

    为什么要把「正在回复」整组提到最前面：一轮长回复期间用户可能又开了好几条新对话，
    只按时间排会把还在出字的那条挤到列表下面去，等它真的回复完用户早就找不到了。
    只要这次运行还没结束（``runtime.manager.registry`` 里还有它），它就压在所有已完成
    的对话上面 —— 判定「在跑」用的是运行表，而不是最后一条消息的 ``streaming`` 状态：
    进程重启后残留的 ``streaming`` 占位不该让一条对话永远钉在顶上。

    组内排序是「对话自身时间戳与最后一条消息时间取较晚者」倒序。正在回复的那条，
    这个时刻就是用户最后一次发消息的时间。取 max 是为了兜住历史数据 —— 某条对话的
    消息很新、但 ``updated_at`` 因为早期版本的写入路径没跟着走时，仍按真正的最近一条排。
    同一时刻的并列项按创建时间兜底，保证每次请求的顺序稳定（不会来回跳）。
    """

    running = set(running_ids)

    def activity(conversation: Conversation) -> datetime:
        moments = [as_utc(conversation.updated_at)]
        last = latest.get(conversation.id)
        if last is not None:
            moments.append(as_utc(last.created_at))
        return max(moments)

    return sorted(
        conversations,
        key=lambda conversation: (
            conversation.id in running,
            activity(conversation),
            as_utc(conversation.created_at),
        ),
        reverse=True,
    )
