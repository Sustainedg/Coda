"""用量：把消息里记录的 token 消耗与技能调用汇总给「用量」页。

token 用量的权威来源是每条助手消息的 ``usage`` 字段（各适配器上报的原始计数）。
各家字段名并不统一：Claude Code 用 ``input_tokens`` / ``cache_read_input_tokens``，
OpenAI 兼容端点用 ``prompt_tokens`` / ``cached_tokens`` —— 这里按与前端
``lib/usage.ts`` 相同的规则归一化，保证页面上「读取 / 输出 / 缓存」口径一致。

技能调用是从已落库的消息 ``parts`` 里数出来的：某次工具调用是不是「技能调用」由
**适配器**判定（见 ``AgentDefinition.skill_invocation``，Claude Code 是内置的 ``Skill`` 工具）。
好处是无需新表、对历史数据同样生效；代价是只能统计到「声明了识别方式」的内核。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..agents import get_agent
from ..db import get_session
from ..models import AgentProfile, Conversation, Message, Skill, User
from ..runtime import skills as skill_service
from .auth import current_user, owner_id_for

router = APIRouter(prefix="/usage", tags=["usage"])

#: 「按对话」「按技能」两张榜单各自最多展示这么多条（再多就该去列表页翻了）。
TOP_CONVERSATIONS = 20
TOP_SKILLS = 20

#: 各家适配器的字段名不一样，按优先级取第一个出现的（与前端 ``lib/usage.ts`` 同一套规则）。
_INPUT_KEYS = ("input_tokens", "prompt_tokens")
_OUTPUT_KEYS = ("output_tokens", "completion_tokens")
_CACHE_READ_KEYS = (
    "cache_read_input_tokens",
    "cached_input_tokens",
    "cached_tokens",
    "prompt_cache_hit_tokens",
    "cache_read_tokens",
)
_CACHE_CREATION_KEYS = ("cache_creation_input_tokens", "cache_creation_tokens")
#: 采用「缓存 token 与 input_tokens 分离」记法（Claude Code 风格）的标记字段。
_EXCLUSIVE_CACHE_MARKERS = ("cache_read_input_tokens", "cache_creation_input_tokens")


def _to_int(value: Any) -> int:
    """把上报的计数字段收成整数；布尔与其它类型一律当 0（别让 ``True`` 混进总量）。"""
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _pick(usage: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        if key in usage:
            return _to_int(usage[key])
    return 0


def _new_bucket() -> dict[str, int]:
    return {
        "input": 0,
        "output": 0,
        "total": 0,
        "cache_read": 0,
        "cache_creation": 0,
        "messages": 0,
        #: 缓存命中率的分母（随记法不同而不同，见 ``_accumulate``）。
        "_cache_base": 0,
    }


def _accumulate(bucket: dict[str, int], usage: dict[str, Any]) -> None:
    """把一条消息的 usage 累加进桶里。空 usage（没有上报计数）直接跳过。"""
    if not usage:
        return
    input_tokens = _pick(usage, _INPUT_KEYS)
    output_tokens = _pick(usage, _OUTPUT_KEYS)
    cache_read = _pick(usage, _CACHE_READ_KEYS)
    cache_creation = _pick(usage, _CACHE_CREATION_KEYS)
    total = _pick(usage, ("total_tokens",)) or (
        input_tokens + output_tokens + cache_read + cache_creation
    )

    bucket["messages"] += 1
    bucket["input"] += input_tokens
    bucket["output"] += output_tokens
    bucket["total"] += total
    bucket["cache_read"] += cache_read
    bucket["cache_creation"] += cache_creation
    # 分离式记法下 input_tokens 不含缓存，需把缓存计入分母；包含式记法则不用。
    exclusive = any(marker in usage for marker in _EXCLUSIVE_CACHE_MARKERS)
    bucket["_cache_base"] += (
        (input_tokens + cache_read + cache_creation) if exclusive else input_tokens
    )


def _finalize(bucket: dict[str, int]) -> dict[str, Any]:
    base = bucket["_cache_base"]
    return {
        "input": bucket["input"],
        "output": bucket["output"],
        "total": bucket["total"],
        "cache_read": bucket["cache_read"],
        "cache_creation": bucket["cache_creation"],
        "cache_ratio": (bucket["cache_read"] / base) if base > 0 else None,
        "messages": bucket["messages"],
    }


def _type_name(type_id: str | None) -> str | None:
    if not type_id:
        return None
    try:
        return get_agent(type_id).name
    except KeyError:
        return None


def _instant(value: Any) -> datetime | None:
    """把库里取出的时间统一成带 UTC 时区的 datetime（SQLite 存的是裸 UTC）。"""
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_instant(value: str | None, label: str) -> datetime | None:
    """解析前端传来的 ISO 8601 时间点（范围过滤用）；非法输入直接 400。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{label} 不是合法的时间：{value}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _count_skill_calls(parts: Any, type_id: str, counts: dict[str, int]) -> None:
    """从一条消息的 ``parts`` 里数出技能调用；识别方式由适配器声明。"""
    try:
        definition = get_agent(type_id)
    except KeyError:
        return
    for part in parts or ():
        if not isinstance(part, dict) or part.get("kind") != "tool_call":
            continue
        slug = definition.skill_invocation(part.get("name") or "", part.get("input") or {})
        if slug:
            skill_id = f"{type_id}:{slug}"
            counts[skill_id] = counts.get(skill_id, 0) + 1


async def _skills_summary(
    session: AsyncSession, calls: dict[str, int], user: User
) -> dict[str, Any]:
    """技能用量：所选范围内的调用总次数，以及被调用最多的技能。

    工具调用只带得出 ``<内核>:<目录名>``，同一内核下共享技能与个人技能可能同名：
    这里只在「共享 + 当前用户」的可见范围内解析，并优先取用户自己的那一份。
    """
    statement = select(Skill)
    if user.role != "admin":
        owner = owner_id_for(user)
        if owner is None:
            statement = statement.where(Skill.owner_id.is_(None))
        else:
            statement = statement.where(
                or_(Skill.owner_id.is_(None), Skill.owner_id == owner)
            )
    rows = (await session.execute(statement)).scalars().all()
    by_key: dict[tuple[str, str], Skill] = {}
    for row in rows:
        key = (row.type_id, row.slug)
        current = by_key.get(key)
        # 自己的库优先于共享技能。
        if current is None or (current.owner_id is None and row.owner_id is not None):
            by_key[key] = row
    labels = skill_service.SOURCE_LABELS

    items: list[dict[str, Any]] = []
    for skill_id, count in calls.items():
        type_id, _, slug = skill_id.partition(":")
        row = by_key.get((type_id, slug))
        items.append(
            {
                "id": row.id if row else skill_id,
                "name": row.name if row else slug,
                "type_id": type_id,
                "type_name": _type_name(type_id),
                "source_label": (labels.get(row.source_kind, row.source_kind) if row else ""),
                "calls": count,
            }
        )
    items.sort(key=lambda item: item["calls"], reverse=True)
    return {"calls": sum(calls.values()), "skills_used": len(calls), "top": items[:TOP_SKILLS]}


@router.get("/overview")
async def usage_overview(
    start: str | None = None,
    end: str | None = None,
    tz_offset: int = 0,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """汇总 token 消耗与技能调用（「用量」页的数据源）。

    ``start`` / ``end`` 是 ISO 8601 时间点（``end`` 为开区间），只统计这个窗口内的消息；
    都不传则统计全部历史。``tz_offset`` 是「按天」分桶用的本地时区偏移（分钟，东为正）——
    过滤用绝对时刻，分桶按用户所在时区，两者才不会在跨零点的日子里打架。
    非管理员只统计自己的对话；管理员统计平台总量，但「按对话」榜单仍只列自己的
    （对话标题取自用户正文，属于隐私，不能因为管理员角色就露出去）。
    """
    lower = _parse_instant(start, "开始时间")
    upper = _parse_instant(end, "结束时间")
    group_tz = timezone(timedelta(minutes=tz_offset))

    conv_stmt = select(Conversation)
    if user.role != "admin":
        conv_stmt = conv_stmt.where(Conversation.owner_id == user.id)
    rows = (
        await session.execute(
            select(
                Message.conversation_id, Message.usage, Message.created_at, Message.parts
            ).where(Message.role == "assistant")
        )
    ).all()
    conversations = {
        row.id: row for row in (await session.execute(conv_stmt)).scalars().all()
    }
    profiles = {
        row.id: row for row in (await session.execute(select(AgentProfile))).scalars().all()
    }

    totals = _new_bucket()
    per_agent: dict[str, dict[str, int]] = {}
    per_conversation: dict[str, dict[str, int]] = {}
    per_day: dict[str, dict[str, int]] = {}
    skill_calls: dict[str, int] = {}

    for conversation_id, usage, created_at, parts in rows:
        # 非管理员的会话集合已按 owner_id 过滤过；不在集合里的消息一律跳过
        # （包括已删除的对话：管理员也不把它们的 token 计入别人的榜单）。
        if conversation_id not in conversations:
            continue
        instant = _instant(created_at)
        if instant is not None:
            if lower is not None and instant < lower:
                continue
            if upper is not None and instant >= upper:
                continue

        conversation = conversations.get(conversation_id)
        profile = profiles.get(conversation.agent_profile_id) if conversation else None
        if profile is not None:
            _count_skill_calls(parts, profile.type_id, skill_calls)

        if not usage:
            continue  # 技能调用已计；没有 token 计数的消息不参与 token 统计

        _accumulate(totals, usage)
        agent_key = conversation.agent_profile_id if conversation else ""
        _accumulate(per_agent.setdefault(agent_key, _new_bucket()), usage)
        _accumulate(per_conversation.setdefault(conversation_id, _new_bucket()), usage)
        if instant is not None:
            _accumulate(per_day.setdefault(instant.astimezone(group_tz).date().isoformat(), _new_bucket()), usage)

    by_agent: list[dict[str, Any]] = []
    for agent_id, bucket in per_agent.items():
        profile = profiles.get(agent_id)
        by_agent.append(
            {
                "id": agent_id,
                "name": profile.name if profile else "（已移除的智能体）",
                "avatar": profile.avatar if profile else "👤",
                "avatar_preset": profile.avatar_preset if profile else None,
                "type_id": profile.type_id if profile else None,
                "type_name": _type_name(profile.type_id) if profile else None,
                "tokens": _finalize(bucket),
            }
        )
    by_agent.sort(key=lambda item: item["tokens"]["total"], reverse=True)

    by_conversation: list[dict[str, Any]] = []
    own_owner_id = owner_id_for(user)
    for conversation_id, bucket in per_conversation.items():
        conversation = conversations.get(conversation_id)
        # 对话标题可能来自用户正文：管理员也只在自己的对话里看到名字，
        # 平台总量 / 按智能体 / 按天仍按管理员口径汇总（不含具体对话内容）。
        if conversation is not None and conversation.owner_id != own_owner_id:
            continue
        profile = profiles.get(conversation.agent_profile_id) if conversation else None
        by_conversation.append(
            {
                "id": conversation_id,
                "title": conversation.title if conversation else "（已删除的对话）",
                "agent_name": profile.name if profile else None,
                "agent_avatar": profile.avatar if profile else None,
                "agent_avatar_preset": profile.avatar_preset if profile else None,
                "updated_at": conversation.updated_at if conversation else None,
                "tokens": _finalize(bucket),
            }
        )
    by_conversation.sort(key=lambda item: item["tokens"]["total"], reverse=True)

    daily = [
        {
            "date": day,
            "input": bucket["input"],
            "output": bucket["output"],
            "total": bucket["total"],
        }
        for day, bucket in sorted(per_day.items())
    ]

    return {
        "tokens": _finalize(totals),
        "conversations": len(per_conversation),
        "by_agent": by_agent,
        "by_conversation": by_conversation[:TOP_CONVERSATIONS],
        "daily": daily,
        "skills": await _skills_summary(session, skill_calls, user),
    }
