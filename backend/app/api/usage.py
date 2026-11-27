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

import csv
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..agents import get_agent
from ..auth_deps import require_owned
from ..db import get_session
from ..models import (
    AgentProfile,
    Conversation,
    Message,
    Skill,
    UsagePricing,
    UsageRecord,
    User,
)
from ..runtime import skills as skill_service
from ..runtime import usage_ledger
from .auth import current_user, owner_id_for

router = APIRouter(prefix="/usage", tags=["usage"])

#: 「按对话」「按技能」两张榜单各自最多展示这么多条（再多就该去列表页翻了）。
TOP_CONVERSATIONS = 20
TOP_SKILLS = 20

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
    """把一条历史消息的 usage 累加进桶里（与账本共用 ``metering`` 的归一化口径）。"""
    if not usage:
        return
    counts = usage_ledger.normalize_usage(usage)
    values = (
        counts["input_tokens"],
        counts["output_tokens"],
        counts["cache_read_tokens"],
        counts["cache_creation_tokens"],
        counts["context_tokens"],
    )
    # 一个计数字段都没认出来 = 该消息没有可用 usage（老数据里偶见），跳过。
    if all(value is None for value in values):
        return

    input_tokens = counts["input_tokens"] or 0
    output_tokens = counts["output_tokens"] or 0
    cache_read = counts["cache_read_tokens"] or 0
    cache_creation = counts["cache_creation_tokens"] or 0
    context = counts["context_tokens"] or 0

    bucket["messages"] += 1
    bucket["input"] += input_tokens
    bucket["output"] += output_tokens
    # 平台口径：总量 = 窗口占用（输入侧含缓存）+ 输出；不再跟着供应商的
    # total_tokens 走，避免“输入含缓存 / 不含缓存”两派各算一套。
    bucket["total"] += context + output_tokens
    bucket["cache_read"] += cache_read
    bucket["cache_creation"] += cache_creation
    # input 已是不含缓存的普通输入，分母统一为 input + cache_read + cache_creation。
    bucket["_cache_base"] += input_tokens + cache_read + cache_creation


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


async def _check_agent(session: AsyncSession, agent_id: str, user: User) -> AgentProfile:
    """校验按智能体过滤的目标存在且属于当前用户。

    不存在 / 已软删除 → 422（这是查询参数里的取值问题，不是路由资源）
    不属于当前用户 → 404（`require_owned` 与其他资产接口口径一致）
    """
    profile = await session.get(AgentProfile, agent_id)
    if profile is None or profile.deleted_at is not None:
        raise HTTPException(status_code=422, detail="指定的智能体不存在")
    require_owned(profile, user)
    return profile


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


def _in_range(instant: datetime, lower: datetime | None, upper: datetime | None) -> bool:
    """时间窗过滤：``lower`` 含、``upper`` 开区间（与消息 / 账本两段共用）。"""
    if lower is not None and instant < lower:
        return False
    if upper is not None and instant >= upper:
        return False
    return True


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
    agent_id: str | None = None,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """汇总 token 消耗与技能调用（「用量」页与智能体设定页「统计」标签的数据源）。

    ``start`` / ``end`` 是 ISO 8601 时间点（``end`` 为开区间），只统计这个窗口内的消息；
    都不传则统计全部历史。``tz_offset`` 是「按天」分桶用的本地时区偏移（分钟，东为正）——
    过滤用绝对时刻，分桶按用户所在时区，两者才不会在跨零点的日子里打架。
    ``agent_id`` 传了就只统计这一个智能体（智能体设定页的「统计」标签用）；
    不存在的返回 422，不属于当前用户的返回 404（见 `_check_agent`）。
    非管理员只统计自己的对话；管理员统计平台总量，但「按对话」榜单仍只列自己的
    （对话标题取自用户正文，属于隐私，不能因为管理员角色就露出去）。
    """
    lower = _parse_instant(start, "开始时间")
    upper = _parse_instant(end, "结束时间")
    group_tz = timezone(timedelta(minutes=tz_offset))

    conv_stmt = select(Conversation)
    if user.role != "admin":
        conv_stmt = conv_stmt.where(Conversation.owner_id == user.id)
    target: AgentProfile | None = None
    if agent_id is not None:
        target = await _check_agent(session, agent_id, user)
        conv_stmt = conv_stmt.where(Conversation.agent_profile_id == target.id)
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
    per_source: dict[str, dict[str, int]] = {}
    #: 每个对话的归属：账本行可能对应**已删除的对话**（消息已级联删除，账本仍在）。
    conv_owner: dict[str, str | None] = {}
    skill_calls: dict[str, int] = {}
    #: 成本总额（只有真正记到价的账本行才累加）与覆盖率（reported / estimated / unknown）。
    cost_total = 0.0
    cost_has_value = False
    cost_coverage: dict[str, int] = {"reported": 0, "estimated": 0, "unknown": 0}

    #: 账本启用时刻：用它把「旧消息段」与「账本段」切开，同一笔用量只算一次。
    ledger_started = await usage_ledger.get_ledger_started_at(session)

    for conversation_id, usage, created_at, parts in rows:
        # 非管理员的会话集合已按 owner_id 过滤过；不在集合里的消息一律跳过
        # （包括已删除的对话：管理员也不把它们的 token 计入别人的榜单）。
        if conversation_id not in conversations:
            continue
        instant = _instant(created_at)
        if instant is None or not _in_range(instant, lower, upper):
            continue

        conversation = conversations.get(conversation_id)
        profile = profiles.get(conversation.agent_profile_id) if conversation else None
        # 技能调用始终从消息 parts 统计：账本不存正文，只有消息里有工具调用。
        if profile is not None:
            _count_skill_calls(parts, profile.type_id, skill_calls)

        if not usage:
            continue  # 技能调用已计；没有 token 计数的消息不参与 token 统计

        # 账本启用后的消息：token 已由账本段负责，这里跳过，避免双记。
        if ledger_started is not None and instant >= ledger_started:
            continue

        _accumulate(totals, usage)
        _accumulate(per_source.setdefault(usage_ledger.SOURCE_LEGACY, _new_bucket()), usage)
        # 历史消息没有成本信息：计入覆盖率的 unknown，而不是假装免费。
        cost_coverage["unknown"] += 1
        agent_key = conversation.agent_profile_id if conversation else ""
        _accumulate(per_agent.setdefault(agent_key, _new_bucket()), usage)
        _accumulate(per_conversation.setdefault(conversation_id, _new_bucket()), usage)
        conv_owner.setdefault(conversation_id, conversation.owner_id if conversation else None)
        _accumulate(
            per_day.setdefault(instant.astimezone(group_tz).date().isoformat(), _new_bucket()),
            usage,
        )

    # —— 账本段：created_at >= ledger_started（一手数据优先，旧消息段已跳过这一区间） ——
    if ledger_started is not None:
        ledger_stmt = select(UsageRecord).where(UsageRecord.created_at >= ledger_started)
        if lower is not None:
            ledger_stmt = ledger_stmt.where(UsageRecord.created_at >= lower)
        if upper is not None:
            ledger_stmt = ledger_stmt.where(UsageRecord.created_at < upper)
        if target is not None:
            ledger_stmt = ledger_stmt.where(UsageRecord.agent_profile_id == target.id)
        if user.role != "admin":
            # 非管理员只看自己的；管理员看平台总量。
            ledger_stmt = ledger_stmt.where(UsageRecord.owner_id == user.id)
        for record in (await session.execute(ledger_stmt)).scalars().all():
            # 成本覆盖率：按账本行的 cost_source 计数；有金额才累加总额。
            source_cost = record.cost_source or usage_ledger.COST_UNKNOWN
            cost_coverage[source_cost] = cost_coverage.get(source_cost, 0) + 1
            if record.cost_usd is not None:
                cost_total += record.cost_usd
                cost_has_value = True
            usage_ledger.accumulate_bucket(totals, record)
            usage_ledger.accumulate_bucket(
                per_source.setdefault(record.source, _new_bucket()), record
            )
            usage_ledger.accumulate_bucket(
                per_agent.setdefault(record.agent_profile_id or "", _new_bucket()), record
            )
            if record.conversation_id:
                usage_ledger.accumulate_bucket(
                    per_conversation.setdefault(record.conversation_id, _new_bucket()), record
                )
                conv_owner.setdefault(record.conversation_id, record.owner_id)
            instant = _instant(record.created_at)
            if instant is not None:
                usage_ledger.accumulate_bucket(
                    per_day.setdefault(
                        instant.astimezone(group_tz).date().isoformat(), _new_bucket()
                    ),
                    record,
                )

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
        if conversation is not None:
            if conversation.owner_id != own_owner_id:
                continue
        elif user.role == "admin" and conv_owner.get(conversation_id) not in (None, own_owner_id):
            # 已删除的对话：标题不可见，但账本行仍带 owner_id，按归属过滤防止越权展示。
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

    by_source = {
        source: _finalize(bucket)
        for source, bucket in per_source.items()
        if bucket["messages"] > 0
    }

    return {
        "tokens": _finalize(totals),
        "conversations": len(per_conversation),
        "by_agent": by_agent,
        "by_conversation": by_conversation[:TOP_CONVERSATIONS],
        "daily": daily,
        # 新增可选字段：按权威源拆分（gateway / native / harness / legacy）。
        # 旧前端不认识它会忽略，不影响现有展示。
        "by_source": by_source,
        # 成本：没有任何已定价行时给 None（未知），给了金额才是已知；coverage 说明覆盖情况。
        "cost_usd": round(cost_total, 8) if cost_has_value else None,
        "cost_coverage": cost_coverage,
        "skills": await _skills_summary(session, skill_calls, user),
    }


def _record_to_dict(record: UsageRecord) -> dict[str, Any]:
    """账本行转成接口结构（token 字段保持「普通输入 / 缓存分离」口径）。"""
    return {
        "id": record.id,
        "run_id": record.run_id,
        "request_id": record.request_id,
        "source": record.source,
        "telemetry_source": record.telemetry_source,
        "origin": record.origin,
        "conversation_id": record.conversation_id,
        "agent_type_id": record.agent_type_id,
        "agent_profile_id": record.agent_profile_id,
        "model": record.model,
        "provider": record.provider,
        "usage_protocol": record.usage_protocol,
        "normalizer_version": record.normalizer_version,
        "response_shape": record.response_shape,
        "upstream_request_id": record.upstream_request_id,
        "raw_usage": record.raw_usage,
        "input_tokens": record.input_tokens,
        "output_tokens": record.output_tokens,
        "cache_read_tokens": record.cache_read_tokens,
        "cache_creation_tokens": record.cache_creation_tokens,
        "reasoning_tokens": record.reasoning_tokens,
        "context_tokens": record.context_tokens,
        "api_calls": record.api_calls,
        "api_duration_ms": record.api_duration_ms,
        "cost_usd": record.cost_usd,
        "cost_source": record.cost_source,
        "cost_pricing": record.cost_pricing,
        "is_estimated": record.is_estimated,
        "created_at": record.created_at,
    }


@router.get("/records")
async def usage_records(
    run_id: str | None = None,
    conversation_id: str | None = None,
    source: str | None = None,
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """账本明细分页（排障 / 审计）。非管理员只返回自己的记录。

    支持按 ``run_id`` / ``conversation_id`` / ``source`` 过滤；按完成时间倒序。
    """
    if source is not None and source not in usage_ledger.VALID_SOURCES:
        raise HTTPException(status_code=422, detail="source 只能是 gateway / native / harness / legacy")
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    stmt = select(UsageRecord)
    if run_id:
        stmt = stmt.where(UsageRecord.run_id == run_id)
    if conversation_id:
        stmt = stmt.where(UsageRecord.conversation_id == conversation_id)
    if source:
        stmt = stmt.where(UsageRecord.source == source)
    if user.role != "admin":
        stmt = stmt.where(UsageRecord.owner_id == user.id)
    stmt = stmt.order_by(UsageRecord.created_at.desc(), UsageRecord.id.desc())
    rows = (
        await session.execute(stmt.offset(offset).limit(limit + 1))
    ).scalars().all()
    has_more = len(rows) > limit
    return {
        "records": [_record_to_dict(row) for row in rows[:limit]],
        "has_more": has_more,
        "limit": limit,
        "offset": offset,
    }


# ---------------------------------------------------------------------------
# P3-3：成本单价（手工价 > 目录价；只对新写入的账本行生效，不重算历史）
# ---------------------------------------------------------------------------

#: CSV 导出的最大行数（本地单用户够用；再大请按时间窗分段导出）。
EXPORT_LIMIT = 50_000


class PricingCreate(BaseModel):
    model_pattern: str
    input_per_million: float = 0.0
    output_per_million: float = 0.0
    cache_read_per_million: float = 0.0
    cache_creation_per_million: float = 0.0
    #: 仅管理员可用：写一条平台级（所有用户可见）单价。
    owner_global: bool = False


def _pricing_view(row: UsagePricing) -> dict[str, Any]:
    return {
        "id": row.id,
        "model_pattern": row.model_pattern,
        "input_per_million": row.input_per_million,
        "output_per_million": row.output_per_million,
        "cache_read_per_million": row.cache_read_per_million,
        "cache_creation_per_million": row.cache_creation_per_million,
        "owner_global": row.owner_id is None,
        "enabled": row.enabled,
        "created_at": row.created_at,
    }


@router.get("/pricing")
async def list_pricing(
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """单价列表：非管理员看「自己的 + 平台级」，管理员看全部。"""
    stmt = select(UsagePricing)
    if user.role != "admin":
        stmt = stmt.where(
            or_(UsagePricing.owner_id.is_(None), UsagePricing.owner_id == owner_id_for(user))
        )
    rows = (await session.execute(stmt.order_by(UsagePricing.created_at.desc()))).scalars().all()
    return {"pricing": [_pricing_view(row) for row in rows]}


@router.post("/pricing")
async def create_pricing(
    body: PricingCreate,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """新增一条手工单价。价格单位：美元 / 每百万 token。"""
    pattern = (body.model_pattern or "").strip()
    if not pattern:
        raise HTTPException(status_code=422, detail="model_pattern 不能为空")
    for value in (
        body.input_per_million,
        body.output_per_million,
        body.cache_read_per_million,
        body.cache_creation_per_million,
    ):
        if isinstance(value, bool) or value < 0:
            raise HTTPException(status_code=422, detail="单价必须是非负数")
    if body.owner_global and user.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员能创建平台级单价")
    row = UsagePricing(
        owner_id=None if body.owner_global else owner_id_for(user),
        model_pattern=pattern,
        input_per_million=body.input_per_million,
        output_per_million=body.output_per_million,
        cache_read_per_million=body.cache_read_per_million,
        cache_creation_per_million=body.cache_creation_per_million,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _pricing_view(row)


@router.delete("/pricing/{pricing_id}")
async def delete_pricing(
    pricing_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    row = await session.get(UsagePricing, pricing_id)
    if row is None:
        raise HTTPException(status_code=404, detail="单价不存在")
    require_owned(row, user)
    await session.delete(row)
    await session.commit()
    return {"ok": True, "id": pricing_id}


# ---------------------------------------------------------------------------
# P3-5：账本 CSV 导出（管理员看全部，普通用户只看自己）
# ---------------------------------------------------------------------------

_EXPORT_HEADER = (
    "created_at", "source", "origin", "api_key_id", "agent_type_id", "agent_profile_id",
    "model", "provider", "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_creation_tokens", "reasoning_tokens", "context_tokens", "cost_usd",
    "cost_source", "run_id", "request_id",
)


@router.get("/records/export.csv")
async def export_usage_records(
    start: str | None = None,
    end: str | None = None,
    source: str | None = None,
    run_id: str | None = None,
    conversation_id: str | None = None,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> Response:
    """把账本明细导出成 CSV（按完成时间升序，最多 ``EXPORT_LIMIT`` 行）。"""
    lower = _parse_instant(start, "开始时间")
    upper = _parse_instant(end, "结束时间")
    if source is not None and source not in usage_ledger.VALID_SOURCES:
        raise HTTPException(status_code=422, detail="source 不合法")

    stmt = select(UsageRecord)
    if run_id:
        stmt = stmt.where(UsageRecord.run_id == run_id)
    if conversation_id:
        stmt = stmt.where(UsageRecord.conversation_id == conversation_id)
    if source:
        stmt = stmt.where(UsageRecord.source == source)
    if lower is not None:
        stmt = stmt.where(UsageRecord.created_at >= lower)
    if upper is not None:
        stmt = stmt.where(UsageRecord.created_at < upper)
    if user.role != "admin":
        stmt = stmt.where(UsageRecord.owner_id == user.id)
    stmt = stmt.order_by(UsageRecord.created_at).limit(EXPORT_LIMIT)
    rows = (await session.execute(stmt)).scalars().all()

    buffer = StringIO()
    buffer.write("﻿")  # Excel 识别 UTF-8 中文
    writer = csv.writer(buffer)
    writer.writerow(_EXPORT_HEADER)
    for row in rows:
        writer.writerow(
            [
                row.created_at.isoformat() if row.created_at else "",
                row.source,
                row.origin,
                row.api_key_id or "",
                row.agent_type_id or "",
                row.agent_profile_id or "",
                row.model or "",
                row.provider or "",
                row.input_tokens if row.input_tokens is not None else "",
                row.output_tokens if row.output_tokens is not None else "",
                row.cache_read_tokens if row.cache_read_tokens is not None else "",
                row.cache_creation_tokens if row.cache_creation_tokens is not None else "",
                row.reasoning_tokens if row.reasoning_tokens is not None else "",
                row.context_tokens if row.context_tokens is not None else "",
                row.cost_usd if row.cost_usd is not None else "",
                row.cost_source,
                row.run_id,
                row.request_id,
            ]
        )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="usage-records.csv"'},
    )
