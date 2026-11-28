"""一手账本：token 归一化、窗口口径、去重写入与**权威源互斥**。

本模块是「平台自己过磅」的落点（见 ``docs/功能文档/usage-gateway.md`` 的「计量口径」章节）。
与 ``Message.usage`` 的分工：``Message.usage`` 是**展示快照**（会随对话 / 消息删除而消失），
本模块写入的 :class:`~app.models.UsageRecord` 是**权威账本**（用量页、审计、成本、外部 API
都读它）。**不要在 ``Message.usage`` 上继续加统计逻辑。**

**权威源互斥（冻结口径，P0-3）**

一次 run 只能有一个「计量权威源」，绝不双记：

* ``gateway`` —— 记录网关在代理响应终态读到的上游 ``usage``（一手，最可信）；
* ``native``  —— 适配器解析内核原生事件上报（二手兜底）；
* ``harness`` —— 平台自己的进程内 HTTP 客户端（本身就是一手）。

收尾判定（见 :func:`resolve_authority`）：

1. 该 run 在账本里已有任意 ``gateway`` 行 → 权威源 = gateway，原生 ``usage`` 事件**丢弃入账**
   （只保留流式展示）；
2. 否则已有 ``native`` 行 → 权威源 = native，按现有逻辑入账；
3. 两边都没有 → **不伪造 0**，记 ``unknown``，界面显示「未记录」。

这样既避免双记，又不会因为网关解析失败而把整轮用量判成 0。

**去重**：账本唯一键是 ``(run_id, source, request_id)``，写入走 SQLite 的
``INSERT OR IGNORE``（见 :func:`record`）。重试时上游若复用同一响应 id 只记一次；
上游为每次尝试生成新 id 则记两行（那是真实消耗，不能吞）。同一流式响应多次上报 usage
快照（``message_start`` 全 0、``message_delta`` 有值）以最终有效值覆盖同一行，不新增行。

**窗口口径**（:func:`normalize_usage` / :func:`window_tokens`）：各家协议记法不同——

* Anthropic Messages：``input_tokens`` 与缓存**分离**，窗口占用 = 输入 + 缓存读 + 缓存写；
* OpenAI Chat：``prompt_tokens`` **包含** ``cached_tokens``，窗口占用 = ``prompt_tokens``；
* OpenAI Responses：``input_tokens`` **包含**缓存，窗口占用 = ``input_tokens``
  （普通输入需减去 ``input_tokens_details.cached_tokens``）。

缺数据不假装是 0：归一化后字段为 ``None``，界面按「未记录」处理。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import insert, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..db import SessionLocal
from ..agents.metering import (
    NORMALIZER_VERSION,
    TELEMETRY_SOURCE_AGENT_REPORT,
    TELEMETRY_SOURCE_GATEWAY_PROXY,
    normalize_usage as _metering_normalize_usage,
    response_shape_for,
    truncate_raw_usage,
    upstream_request_id as _sanitize_upstream_request_id,
)
from ..models import (
    ORIGIN_EXTERNAL,
    ORIGIN_INTERNAL,
    PlatformSetting,
    UsagePricing,
    UsageRecord,
    utcnow,
)

logger = logging.getLogger("coda")

# ---------------------------------------------------------------------------
# 来源 / 口径常量
# ---------------------------------------------------------------------------

#: 权威源：记录网关读到的上游 usage（一手）。
SOURCE_GATEWAY = "gateway"
#: 权威源：适配器解析内核原生事件（二手兜底）。
SOURCE_NATIVE = "native"
#: 权威源：平台自己的进程内 HTTP 客户端（本身就是一手）。
SOURCE_HARNESS = "harness"
#: 权威源：历史数据回填（可选脚本，默认不跑）。
SOURCE_LEGACY = "legacy"
VALID_SOURCES = (SOURCE_GATEWAY, SOURCE_NATIVE, SOURCE_HARNESS, SOURCE_LEGACY)


def telemetry_source_for(source: str) -> str | None:
    """把账本来源翻成双轨标记。

    * ``gateway`` → ``gateway_proxy``（旁路抓取，一手）；
    * ``native`` / ``harness`` → ``agent_report``（平台内部直连观察，非代理）；
    * ``legacy`` / 未知 → ``None``（老数据没有这个信息，不猜）。
    """
    if source == SOURCE_GATEWAY:
        return TELEMETRY_SOURCE_GATEWAY_PROXY
    if source in (SOURCE_NATIVE, SOURCE_HARNESS):
        return TELEMETRY_SOURCE_AGENT_REPORT
    return None


#: 归属常量（``internal`` / ``external``）定义在 ``models`` 叶子层，这里直接复用。

#: 成本来源：上游 / 本地明确给出的单价。
COST_REPORTED = "reported"
#: 成本来源：按本地 tokenizer / 目录价估算。
COST_ESTIMATED = "estimated"
#: 成本来源：没有单价信息，保持未知（不假装是 0）。
COST_UNKNOWN = "unknown"

#: 平台设置键：账本启用时刻。首次写入账本时落库，用量页据此分段（见 P0-4）。
LEDGER_STARTED_AT_KEY = "usage_ledger_started_at"

#: 归一化后的字段名（也是 :class:`UsageRecord` 的 token 列）。
COUNT_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "reasoning_tokens",
    "context_tokens",
)

# ---------------------------------------------------------------------------
# 归一化：唯一实现在 app.agents.metering（本文件只做字段裁剪与落库）
# ---------------------------------------------------------------------------


def _empty_counts() -> dict[str, int | None]:
    return {field: None for field in COUNT_FIELDS}


def normalize_usage(
    raw: Any, *, protocol: str | None = None
) -> dict[str, int | None]:
    """把各协议的 ``usage`` 归一化成统一计数（薄包装）。

    唯一实现在 :mod:`app.agents.metering`。这里保留原来的六个键，方便账本 /
    成本 / 接口继续按字段读取；``total_tokens`` 是新增的派生展示值，不在这里返回。

    ``protocol`` 由调用方按来源传入（网关响应形状 / 适配器声明）；
    为空时由核心按字段形状兜底。没有可用计数时返回全 ``None``（记 unknown，不假装 0）。
    """
    counts = _metering_normalize_usage(raw, protocol=protocol)
    return {field: counts.get(field) for field in COUNT_FIELDS}


def window_tokens(raw: Any, *, protocol: str | None = None) -> int | None:
    """一次请求的**上下文窗口占用**（输入侧含缓存）。没有可用计数时返回 ``None``。"""
    return normalize_usage(raw, protocol=protocol)["context_tokens"]


# ---------------------------------------------------------------------------
# 账本写入
# ---------------------------------------------------------------------------


def build_record(
    *,
    run_id: str,
    request_id: str,
    source: str,
    counts: dict[str, int | None] | None = None,
    owner_id: str | None = None,
    conversation_id: str | None = None,
    agent_type_id: str = "",
    agent_profile_id: str | None = None,
    model: str = "",
    provider: str = "",
    base_url: str = "",
    api_calls: int = 1,
    api_duration_ms: int | None = None,
    request_started_at: datetime | None = None,
    created_at: datetime | None = None,
    origin: str = ORIGIN_INTERNAL,
    api_key_id: str | None = None,
    cost_usd: float | None = None,
    cost_source: str = COST_UNKNOWN,
    is_estimated: bool = False,
    raw_usage: Any = None,
    usage_protocol: str | None = None,
    telemetry_source: str | None = None,
    response_shape: str | None = None,
    upstream_request_id: str | None = None,
    normalizer_version: str | None = NORMALIZER_VERSION,
) -> UsageRecord:
    """组装一行账本记录。``counts`` 缺省时按「无计数」处理（记 unknown）。

    ``created_at`` 可显式指定（历史回填 / 测试需要把行落在真实时间上）。

    原始留痕 / 双轨字段：
    * ``raw_usage`` 只做对账留痕，超过 16KB 会写结构化截断占位对象；
    * ``telemetry_source`` 缺省按 ``source`` 推导（gateway→gateway_proxy，
      native/harness→agent_report）；
    * ``upstream_request_id`` 是可选唯一键，用于跨来源幂等去重。
    """
    if counts is None:
        counts = _empty_counts()
    if telemetry_source is None:
        telemetry_source = telemetry_source_for(source)
    if response_shape is None and usage_protocol:
        response_shape = response_shape_for(usage_protocol)
    raw_payload = None
    if raw_usage is not None:
        raw_payload = truncate_raw_usage(raw_usage) or None
    return UsageRecord(
        owner_id=owner_id,
        conversation_id=conversation_id,
        run_id=run_id,
        request_id=request_id,
        source=source,
        telemetry_source=(
            str(telemetry_source)[:24] if telemetry_source else None
        ),
        upstream_request_id=_sanitize_upstream_request_id(upstream_request_id),
        origin=origin,
        api_key_id=api_key_id,
        agent_type_id=agent_type_id,
        agent_profile_id=agent_profile_id,
        model=model,
        provider=provider,
        base_url=base_url,
        usage_protocol=(
            str(usage_protocol)[:32] if usage_protocol else None
        ),
        normalizer_version=(
            str(normalizer_version)[:16] if normalizer_version else None
        ),
        response_shape=(
            str(response_shape)[:24] if response_shape else None
        ),
        input_tokens=counts["input_tokens"],
        output_tokens=counts["output_tokens"],
        cache_read_tokens=counts["cache_read_tokens"],
        cache_creation_tokens=counts["cache_creation_tokens"],
        reasoning_tokens=counts["reasoning_tokens"],
        context_tokens=counts["context_tokens"],
        raw_usage=raw_payload,
        api_calls=api_calls,
        api_duration_ms=api_duration_ms,
        cost_usd=cost_usd,
        cost_source=cost_source,
        is_estimated=is_estimated,
        request_started_at=request_started_at or utcnow(),
        created_at=created_at or utcnow(),
    )


#: 网关后到覆盖自报时要搬的字段（身份 / 时间列保持第一次观察到的值）。
_PROMOTE_FIELDS = (
    "source",
    "telemetry_source",
    "origin",
    "api_key_id",
    "agent_type_id",
    "agent_profile_id",
    "model",
    "provider",
    "base_url",
    "usage_protocol",
    "normalizer_version",
    "response_shape",
    "upstream_request_id",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "reasoning_tokens",
    "context_tokens",
    "raw_usage",
    "api_calls",
    "api_duration_ms",
    "cost_usd",
    "cost_source",
    "cost_pricing",
    "is_estimated",
)


async def _promote_to_gateway(
    session: AsyncSession, incoming: UsageRecord
) -> bool:
    """同一上游请求先写了 Agent 自报、后到网关旁路：用一手网关数据覆盖该行。

    只在**有上游响应 ID** 时生效（没有稳定身份就不猜）；返回 ``False``（不是新增行）。
    """
    upstream_id = incoming.upstream_request_id
    if incoming.source != SOURCE_GATEWAY or not upstream_id:
        return False
    # 网关没解析出任何计费计数时，不能拿空值覆盖 Agent 自报的有效数据。
    if all(
        getattr(incoming, field) is None
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
        )
    ):
        return False
    existing = (
        await session.execute(
            select(UsageRecord).where(
                UsageRecord.upstream_request_id == upstream_id
            )
        )
    ).scalar_one_or_none()
    if existing is None or existing.source == SOURCE_GATEWAY:
        return False
    # 只允许一手网关覆盖自报 / 历史；其它组合按“先到先得”处理。
    if existing.source not in (SOURCE_NATIVE, SOURCE_HARNESS, SOURCE_LEGACY):
        return False
    for field in _PROMOTE_FIELDS:
        setattr(existing, field, getattr(incoming, field))
    session.add(existing)
    return False


async def record(session: AsyncSession, record: UsageRecord) -> bool:
    """写入一行账本；已存在则忽略（返回是否**新增**）。

    去重键有两层：

    1. ``(run_id, source, request_id)`` —— 老机制，保证同一来源同一请求只一行；
    2. ``upstream_request_id`` —— 双轨去重，保证同一笔上游请求不会被「自报 + 网关」
       记两次。若先到的是 Agent 自报、后到网关，:func:`_promote_to_gateway` 会用
       一手网关数据覆盖该行。

    **不自动 commit**——由调用方统一提交，与 ``runtime.service`` 的落库约定一致。
    """
    values = {
        column.name: getattr(record, column.name)
        for column in UsageRecord.__table__.columns
        if column.name != "id"
    }
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        # 不指定 index_elements：两个唯一约束（老去重键 + upstream_request_id）都忽略，
        # 然后由 _promote_to_gateway 判断要不要用一手网关覆盖。
        stmt = sqlite_insert(UsageRecord).values(**values).on_conflict_do_nothing()
    else:  # pragma: no cover - 本项目只跑 SQLite；非 SQLite 留通用兜底
        stmt = insert(UsageRecord).values(**values)
    try:
        result = await session.execute(stmt)
    except IntegrityError:  # pragma: no cover - SQLite 走 on_conflict，不会到这里
        await session.rollback()
        return await _promote_to_gateway(session, record)
    added = (result.rowcount or 0) > 0
    if added:
        return True
    return await _promote_to_gateway(session, record)


async def record_usage(
    session: AsyncSession,
    *,
    run_id: str,
    request_id: str,
    source: str,
    raw_usage: Any,
    usage_protocol: str | None = None,
    **meta: Any,
) -> bool:
    """归一化 ``raw_usage`` 后写一行账本（去重）。``meta`` 透传 :func:`build_record`。"""
    record_row = build_record(
        run_id=run_id,
        request_id=request_id,
        source=source,
        counts=normalize_usage(raw_usage, protocol=usage_protocol),
        raw_usage=raw_usage,
        usage_protocol=usage_protocol,
        **meta,
    )
    return await record(session, record_row)


async def update_cost(
    session: AsyncSession,
    *,
    run_id: str,
    source: str,
    request_id: str,
    cost_usd: float | None,
    cost_source: str,
) -> bool:
    """只补成本字段（``cost_usd`` / ``cost_source``），**不改 token 计数**。

    同一笔账后续拿到单价时调用：token 是已发生的事实，成本是后补的口径，两者解耦。
    """
    row = (
        await session.execute(
            select(UsageRecord).where(
                UsageRecord.run_id == run_id,
                UsageRecord.source == source,
                UsageRecord.request_id == request_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    row.cost_usd = cost_usd
    row.cost_source = cost_source
    await session.commit()
    return True


# ---------------------------------------------------------------------------
# 权威源互斥判定
# ---------------------------------------------------------------------------


def _pricing_matches(pattern: str | None, model: str | None) -> bool:
    """单价匹配：精确等于或以它开头的模型名命中；``*`` 结尾按前缀处理。"""
    prefix = (pattern or "").strip().rstrip("*")
    name = (model or "").strip()
    if not prefix or not name:
        return False
    return name == prefix or name.startswith(prefix)


def _cost_from_rates(record_row: UsageRecord, rates: dict[str, float]) -> float:
    """按费率（美元 / 每百万 token）算一次成本。"""
    return round(
        (record_row.input_tokens or 0) / 1_000_000 * rates.get("input", 0.0)
        + (record_row.output_tokens or 0) / 1_000_000 * rates.get("output", 0.0)
        + (record_row.cache_read_tokens or 0) / 1_000_000 * rates.get("cache_read", 0.0)
        + (record_row.cache_creation_tokens or 0) / 1_000_000 * rates.get("cache_creation", 0.0),
        8,
    )


async def apply_cost(session: AsyncSession, record_row: UsageRecord) -> None:
    """给新写入的账本行补成本：**手工价 > models.dev 目录价 > unknown**。

    - 只在写入时算：**不重算历史**（目录刷新 / 改价都不会回改旧行）；
    - 手工价按 owner（个人 + 平台级）与**最长前缀**匹配；
    - 目录价按 provider/model 精确 / 别名匹配；不认识的中转**不继承**官方价；
    - 只有窗口快照、没有计费计数的行不算成本（避免“假装 0”）。
    """
    if record_row.cost_usd is not None or not record_row.model:
        return
    # 只有窗口快照、没有任何计费计数（例如运行中的原生 context-only 行）时不算成本：
    # 算出来会是 0，等于「假装有成本」，应保持 unknown。
    if all(
        getattr(record_row, field) is None
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
        )
    ):
        return

    # 1) 手工价（个人 + 平台级），最长前缀优先。
    stmt = select(UsagePricing).where(UsagePricing.enabled.is_(True))
    if record_row.owner_id is None:
        stmt = stmt.where(UsagePricing.owner_id.is_(None))
    else:
        stmt = stmt.where(
            or_(UsagePricing.owner_id.is_(None), UsagePricing.owner_id == record_row.owner_id)
        )
    rows = (await session.execute(stmt)).scalars().all()
    matches = [row for row in rows if _pricing_matches(row.model_pattern, record_row.model)]
    if matches:
        pricing = max(matches, key=lambda row: len((row.model_pattern or "").rstrip("*")))
        rates = {
            "input": pricing.input_per_million,
            "output": pricing.output_per_million,
            "cache_read": pricing.cache_read_per_million,
            "cache_creation": pricing.cache_creation_per_million,
        }
        record_row.cost_usd = _cost_from_rates(record_row, rates)
        # 手工价是“配置的估算”，不是供应商账单 → estimated（reported 留给上游明确报价）。
        record_row.cost_source = COST_ESTIMATED
        record_row.cost_pricing = {
            "source": "manual",
            "pricing_id": pricing.id,
            "model_pattern": pricing.model_pattern,
            "rates": rates,
        }
        return

    # 2) models.dev 目录价（只认 provider/model 对得上，或模型名自带 provider 前缀）。
    from ..model_catalog import lookup  # 延迟导入：启动期不依赖目录模块

    price = lookup(record_row.provider, record_row.model)
    if price is None:
        return
    record_row.cost_usd = _cost_from_rates(record_row, price.rates())
    record_row.cost_source = COST_ESTIMATED
    record_row.cost_pricing = {
        "source": "models.dev",
        "provider": price.provider,
        "model": price.model,
        "catalog_sha256": price.sha256,
        "catalog_downloaded_at": price.downloaded_at,
        "rates": price.rates(),
    }


async def _has_rows(session: AsyncSession, run_id: str, source: str) -> bool:
    row = (
        await session.execute(
            select(UsageRecord.id)
            .where(UsageRecord.run_id == run_id, UsageRecord.source == source)
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def has_gateway_rows(session: AsyncSession, run_id: str) -> bool:
    """该 run 是否已有记录网关入账的行。"""
    return await _has_rows(session, run_id, SOURCE_GATEWAY)


async def resolve_authority(session: AsyncSession, run_id: str) -> str:
    """一次 run 的计量权威源：``gateway`` > ``native`` > ``none``（见模块 docstring）。

    调用方据此决定原生 ``usage`` 事件是否还要入账：已判定为 gateway 时，原生事件只用于
    流式展示、**不再写账本**，避免双记。
    """
    if await has_gateway_rows(session, run_id):
        return SOURCE_GATEWAY
    if await _has_rows(session, run_id, SOURCE_NATIVE):
        return SOURCE_NATIVE
    if await _has_rows(session, run_id, SOURCE_HARNESS):
        return SOURCE_HARNESS
    return "none"


# ---------------------------------------------------------------------------
# 分段策略（P0-4）：账本启用时刻 + 账本侧汇总
# ---------------------------------------------------------------------------


def _parse_instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def get_ledger_started_at(session: AsyncSession) -> datetime | None:
    """账本启用时刻；还没启用过返回 ``None``（此时用量页应全部走旧 ``messages`` 汇总）。"""
    row = (
        await session.execute(
            select(PlatformSetting).where(PlatformSetting.key == LEDGER_STARTED_AT_KEY)
        )
    ).scalar_one_or_none()
    return _parse_instant(row.value if row else None)


async def mark_ledger_started(session: AsyncSession, when: datetime | None = None) -> datetime:
    """首次启用账本时记录时刻（幂等：已存在则不覆盖，返回既有值）。"""
    existing = await get_ledger_started_at(session)
    if existing is not None:
        return existing
    moment = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
    value = moment.isoformat()
    # 并发首次写入会抢同一个主键：SQLite 用 INSERT OR IGNORE，先到者生效、后到者不受影响。
    # 若直接 add + commit，后到者会抛 IntegrityError，把整笔账一起丢掉。
    if session.get_bind().dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = (
            sqlite_insert(PlatformSetting)
            .values(key=LEDGER_STARTED_AT_KEY, value=value, updated_at=moment)
            .on_conflict_do_nothing(index_elements=["key"])
        )
        await session.execute(stmt)
    else:  # pragma: no cover - 本项目只跑 SQLite
        row = (
            await session.execute(
                select(PlatformSetting).where(PlatformSetting.key == LEDGER_STARTED_AT_KEY)
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(PlatformSetting(key=LEDGER_STARTED_AT_KEY, value=value,
                                        updated_at=moment))
    await session.commit()
    return await get_ledger_started_at(session) or moment


def _new_bucket() -> dict[str, int]:
    #: 与 ``api/usage.py`` 的桶同构，方便两段（账本 + 旧消息）直接相加。
    return {
        "input": 0,
        "output": 0,
        "total": 0,
        "cache_read": 0,
        "cache_creation": 0,
        "messages": 0,
        "_cache_base": 0,
    }


def accumulate_bucket(bucket: dict[str, int], row: UsageRecord) -> None:
    """把一行账本累加进与 ``api/usage.py`` 同构的桶里（账本里的输入已是「普通输入」）。"""
    input_tokens = row.input_tokens or 0
    output_tokens = row.output_tokens or 0
    cache_read = row.cache_read_tokens or 0
    cache_creation = row.cache_creation_tokens or 0
    bucket["messages"] += 1
    bucket["input"] += input_tokens
    bucket["output"] += output_tokens
    bucket["cache_read"] += cache_read
    bucket["cache_creation"] += cache_creation
    bucket["total"] += input_tokens + output_tokens + cache_read + cache_creation
    bucket["_cache_base"] += input_tokens + cache_read + cache_creation


def finalize_bucket(bucket: dict[str, int]) -> dict[str, Any]:
    """把累加桶收成对外结构（与 ``api/usage.py`` 的 ``_finalize`` 同形）。"""
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


async def latest_context_for_run(
    session: AsyncSession, run_id: str
) -> tuple[int | None, datetime | None]:
    """该 run 最近一次请求的**窗口占用**与采集时刻（gateway / native / harness 皆可）。

    运行收尾用它校准 ``Message.context_tokens``：网关一手数据比内核原生上报更可信，
    但两边都没有时返回 ``(None, None)``，让调用方回退现有快照 / 估算，**不伪造 0**。
    """
    row = (
        await session.execute(
            select(UsageRecord)
            .where(
                UsageRecord.run_id == run_id,
                UsageRecord.source.in_(
                    (SOURCE_GATEWAY, SOURCE_NATIVE, SOURCE_HARNESS)
                ),
            )
            .order_by(UsageRecord.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return (None, None)
    return (row.context_tokens, row.created_at)


async def record_isolated(
    record_row: UsageRecord, *, started_at_hint: datetime | None = None
) -> bool:
    """用**独立会话**写入一行已组装好的账本：首次写入顺便记 ``usage_ledger_started_at``。

    - ``started_at_hint`` 用于把账本启用边界对齐到「本 run 的消息创建时刻」，避免边界轮
      被旧消息段重复统计；缺省用记录的请求开始时间。
    - 与消息落库解耦：消息 commit 失败 / 进程崩溃不影响已测用量；
    - 任何异常只告警、不向调用方抛出（账本失败不能影响本轮对话）。
    """
    try:
        async with SessionLocal() as session:
            await mark_ledger_started(
                session, when=started_at_hint or record_row.request_started_at
            )
            await apply_cost(session, record_row)
            added = await record(session, record_row)
            await session.commit()
            return added
    except Exception:  # noqa: BLE001 - 账本写入失败不能影响本轮对话
        logger.warning("账本写入失败（不影响本轮对话）", exc_info=True)
        return False


async def record_usage_isolated(**kwargs: Any) -> bool:
    """归一化 ``raw_usage`` 后，用独立会话写一行账本（``kwargs`` 透传 :func:`build_record`）。

    ``usage_protocol`` 可选：网关 / 适配器知道协议时传入，避免字段兜底误判。
    """
    raw_usage = kwargs.pop("raw_usage")
    usage_protocol = kwargs.pop("usage_protocol", None)
    return await record_isolated(
        build_record(
            counts=normalize_usage(raw_usage, protocol=usage_protocol),
            raw_usage=raw_usage,
            usage_protocol=usage_protocol,
            **kwargs,
        )
    )


def _context_only_counts(tokens: int | None) -> dict[str, int | None]:
    counts = _empty_counts()
    counts["context_tokens"] = tokens
    return counts


def _has_billing_counts(counts: dict[str, int | None] | None) -> bool:
    if not counts:
        return False
    return any(
        counts.get(field) is not None
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "reasoning_tokens",
        )
    )


async def record_native_snapshot_isolated(
    *,
    run_id: str,
    source: str = SOURCE_NATIVE,
    context_tokens: int | None = None,
    counts: dict[str, int | None] | None = None,
    finalize: bool = False,
    started_at_hint: datetime | None = None,
    raw_usage: Any = None,
    usage_protocol: str | None = None,
    upstream_request_id: str | None = None,
    **meta: Any,
) -> bool:
    """运行中创建 / 更新本 run 的**唯一** native / harness 账本行。

    这是「原生事件实时入账」的落点（plan A）：

    * 每收到一次原生 ``context.usage`` 就更新同一行的 ``context_tokens``，独立会话提交，
      面板在运行中轮询即可看到最新窗口；
    * 收尾（``finalize=True``）时把整轮计费计数补进同一行并计算成本；**不新增行**，
      所以不会和逐轮累计重复计；
    * 边界轮仍然只有一行，``usage_ledger_started_at`` 不会被反复改写；
    * 任何异常只告警，不影响本轮对话。
    """
    try:
        request_id = f"{run_id}:native"
        async with SessionLocal() as session:
            await mark_ledger_started(session, when=started_at_hint or utcnow())
            row = (
                await session.execute(
                    select(UsageRecord).where(
                        UsageRecord.run_id == run_id,
                        UsageRecord.source == source,
                        UsageRecord.request_id == request_id,
                    )
                )
            ).scalar_one_or_none()

            if row is None:
                initial = counts if _has_billing_counts(counts) else _context_only_counts(context_tokens)
                row = build_record(
                    run_id=run_id,
                    request_id=request_id,
                    source=source,
                    counts=initial,
                    request_started_at=started_at_hint,
                    raw_usage=raw_usage,
                    usage_protocol=usage_protocol,
                    upstream_request_id=upstream_request_id,
                    **meta,
                )
                if context_tokens is not None:
                    # 窗口口径以「最后一次请求的输入」为准，不被整轮累计覆盖。
                    row.context_tokens = context_tokens
                if finalize:
                    await apply_cost(session, row)
                added = await record(session, row)
                await session.commit()
                return added

            if context_tokens is not None:
                row.context_tokens = context_tokens
            elif counts and counts.get("context_tokens") is not None:
                row.context_tokens = counts["context_tokens"]
            if usage_protocol is not None:
                row.usage_protocol = str(usage_protocol)[:32]
                row.response_shape = response_shape_for(usage_protocol)
                row.normalizer_version = NORMALIZER_VERSION
            if upstream_request_id is not None:
                row.upstream_request_id = _sanitize_upstream_request_id(
                    upstream_request_id
                )
            if raw_usage is not None:
                row.raw_usage = truncate_raw_usage(raw_usage) or None
            row.telemetry_source = telemetry_source_for(source)
            if _has_billing_counts(counts):
                row.input_tokens = counts.get("input_tokens")
                row.output_tokens = counts.get("output_tokens")
                row.cache_read_tokens = counts.get("cache_read_tokens")
                row.cache_creation_tokens = counts.get("cache_creation_tokens")
                row.reasoning_tokens = counts.get("reasoning_tokens")
            if finalize:
                # 按天统计以“本轮结束”为准（与旧的原生兜底行一致）；
                # 运行中面板按 run_id 查，不受 created_at 影响。
                row.created_at = utcnow()
                await apply_cost(session, row)
            session.add(row)
            await session.commit()
            return True
    except Exception:  # noqa: BLE001 - 账本写入失败不能影响本轮对话
        logger.warning("原生上下文快照写入失败（不影响本轮对话）", exc_info=True)
        return False


async def ledger_totals(
    session: AsyncSession,
    *,
    owner_id: str | None = None,
    agent_profile_id: str | None = None,
    conversation_ids: set[str] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    """账本侧汇总。归属 / 时间过滤与 ``api/usage.py`` 的旧消息口径一致，两段可直接相加。

    P0 只提供查询原语；真正「账本优先 + 旧消息回退」的分段拼装在 P2-1 接到用量页。
    """
    stmt = select(UsageRecord)
    if owner_id is not None:
        stmt = stmt.where(UsageRecord.owner_id == owner_id)
    if agent_profile_id is not None:
        stmt = stmt.where(UsageRecord.agent_profile_id == agent_profile_id)
    if conversation_ids is not None:
        stmt = stmt.where(UsageRecord.conversation_id.in_(conversation_ids))
    if since is not None:
        stmt = stmt.where(UsageRecord.created_at >= since)
    if until is not None:
        stmt = stmt.where(UsageRecord.created_at < until)
    rows = (await session.execute(stmt)).scalars().all()
    bucket = _new_bucket()
    for row in rows:
        accumulate_bucket(bucket, row)
    return finalize_bucket(bucket)


__all__ = [
    "SOURCE_GATEWAY",
    "SOURCE_NATIVE",
    "SOURCE_HARNESS",
    "SOURCE_LEGACY",
    "VALID_SOURCES",
    "ORIGIN_INTERNAL",
    "ORIGIN_EXTERNAL",
    "COST_REPORTED",
    "COST_ESTIMATED",
    "COST_UNKNOWN",
    "LEDGER_STARTED_AT_KEY",
    "COUNT_FIELDS",
    "normalize_usage",
    "window_tokens",
    "build_record",
    "record",
    "record_usage",
    "update_cost",
    "apply_cost",
    "has_gateway_rows",
    "resolve_authority",
    "get_ledger_started_at",
    "mark_ledger_started",
    "latest_context_for_run",
    "record_isolated",
    "record_usage_isolated",
    "record_native_snapshot_isolated",
    "ledger_totals",
    "accumulate_bucket",
    "finalize_bucket",
]
