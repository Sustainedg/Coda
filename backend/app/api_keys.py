"""外部模型 API 的长期密钥：签发、掩码、校验与配额判定。

设计要点（见 ``docs/功能文档/usage-gateway.md`` 的「外部模型 API」章节）：

* **只存哈希**：明文 ``cosk_…`` 只在签发时返回一次；库里存 sha256 与一个展示用掩码。
  校验不走「取哈希再比对」，而是直接按哈希等值查库（哈希本身有 256 位随机熵）。
* **绑定网关**：一把 key 只能用一个自定义网关；平台用网关的上游密钥 ``inject`` 转发。
* **配额按日 / 月**：请求数与 token 数；超限由路由返回 429。token 只能按**已用量**判，
  当前请求的 token 要等响应终态才知道。
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .models import ORIGIN_EXTERNAL, ApiKey, UsageRecord

#: 外部 API Key 前缀：便于人眼识别，也避免和平台 PAT（``pat_``）混淆。
API_KEY_PREFIX = "cosk_"
#: 随机部分字节数：32 字节 = 256 位熵，hex 后 64 字符。
API_KEY_RANDOM_BYTES = 32


def api_key_hash(token: str) -> str:
    """密钥的存储哈希（sha256 一层足够，令牌自带 256 位熵）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _mask_for(raw: str) -> str:
    """展示掩码：``cosk_`` + 前 4 个随机字符 + 省略号 + 后 4 个。"""
    head = raw[: len(API_KEY_PREFIX) + 4]
    return f"{head}…{raw[-4:]}"


def issue_api_key() -> tuple[str, str, str]:
    """生成一把新密钥，返回 ``(明文, 哈希, 掩码)``；明文只应返回调用方一次。"""
    raw = API_KEY_PREFIX + secrets.token_hex(API_KEY_RANDOM_BYTES)
    return raw, api_key_hash(raw), _mask_for(raw)


async def find_active_key(session: AsyncSession, token: str | None) -> ApiKey | None:
    """按明文 key 找到**启用且未吊销**的 :class:`ApiKey`；找不到返回 None。"""
    if not token or not token.startswith(API_KEY_PREFIX):
        return None
    row = (
        await session.execute(
            select(ApiKey).where(ApiKey.key_hash == api_key_hash(token))
        )
    ).scalar_one_or_none()
    if row is None or not row.enabled or row.revoked_at is not None:
        return None
    return row


# ---------------------------------------------------------------------------
# 配额
# ---------------------------------------------------------------------------


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


_TOKEN_SUM = (
    func.coalesce(UsageRecord.input_tokens, 0)
    + func.coalesce(UsageRecord.output_tokens, 0)
    + func.coalesce(UsageRecord.cache_read_tokens, 0)
    + func.coalesce(UsageRecord.cache_creation_tokens, 0)
)


async def period_usage(
    session: AsyncSession, api_key_id: str, since: datetime
) -> tuple[int, int]:
    """该 key 从 ``since`` 起的 ``(请求数, token 总数)``（按完成时间，UTC）。"""
    row = (
        await session.execute(
            select(func.count(UsageRecord.id), func.coalesce(func.sum(_TOKEN_SUM), 0)).where(
                UsageRecord.api_key_id == api_key_id,
                UsageRecord.origin == ORIGIN_EXTERNAL,
                UsageRecord.created_at >= since,
            )
        )
    ).one()
    return int(row[0] or 0), int(row[1] or 0)


async def check_quota(
    session: AsyncSession, api_key: ApiKey, *, now: datetime | None = None
) -> str | None:
    """超限时返回给用户看的中文原因；未超限返回 ``None``。

    请求数与 token 都按**已发生**的用量判：当前请求若把额度顶穿，下一轮才会被拒。
    这是「不预设请求会花多少」的保守做法，避免为了预扣把正常请求挡掉。
    """
    moment = now or datetime.now(timezone.utc)
    day_requests, day_tokens = await period_usage(session, api_key.id, _day_start(moment))
    month_requests, month_tokens = await period_usage(
        session, api_key.id, _month_start(moment)
    )

    if api_key.daily_request_limit and day_requests >= api_key.daily_request_limit:
        return f"今日请求数已达上限（{api_key.daily_request_limit}）"
    if api_key.monthly_request_limit and month_requests >= api_key.monthly_request_limit:
        return f"本月请求数已达上限（{api_key.monthly_request_limit}）"
    if api_key.daily_token_limit and day_tokens >= api_key.daily_token_limit:
        return f"今日 token 已达上限（{api_key.daily_token_limit}）"
    if api_key.monthly_token_limit and month_tokens >= api_key.monthly_token_limit:
        return f"本月 token 已达上限（{api_key.monthly_token_limit}）"
    return None


__all__ = [
    "API_KEY_PREFIX",
    "API_KEY_RANDOM_BYTES",
    "api_key_hash",
    "issue_api_key",
    "find_active_key",
    "period_usage",
    "check_quota",
]