"""上下文窗口：近似 token 估算、上限设置与自动压缩。

**口径（唯一一份）**：所有「已用 / 上限 / 阈值 / 压缩决策」都走 :func:`estimate_tokens`。

* 中日韩文字（含全角标点、假名、谚文）按 **1 字 ≈ 1 token**；
* 其余字符按 **4 字符 ≈ 1 token**；
* 每条消息再加 :data:`MESSAGE_OVERHEAD_TOKENS` 的角色 / 分隔开销。

这是近似值，不是计费口径：各内核上报的 ``usage`` 才是真实消耗（见
``app/api/usage.py``）。上下文窗口需要的是「还没发出去的这一轮会有多大」，
只能按正文估算；因此展示、阈值判断、压缩决策必须共用本模块，禁止各处各算一套。

配置沿用仓库现有的键值配置表（:class:`app.models.PlatformSetting`，与
``runtime/titles.py`` 的标题设置同一套读写约定）：

* 键 ``context_settings:<owner_id>``，值为 JSON ``{"limit_tokens", "threshold_ratio"}``；
* 键不存在 / 值损坏 = 用默认值（上限 128k、阈值 80%），不让坏数据把界面弄崩。

压缩是**确定性裁剪 + 摘要**：保留系统提示与最近若干条消息（按目标用量从新往旧
保留，最多 :data:`KEEP_RECENT_MESSAGES`、最少 :data:`MIN_KEEP_MESSAGES` 条），
更早的消息折叠成一段短摘要；原始消息仍完整留在 ``messages`` 表里（对话记录不丢），
失败时也不改任何已存的上下文。
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from starlette.concurrency import run_in_threadpool

from ..models import Conversation, Message, PlatformSetting

logger = logging.getLogger("coda")

#: 默认上下文上限（tokens）。用户可在详情面板里改，存进平台设置表。
DEFAULT_LIMIT_TOKENS = 128_000
#: 默认触发阈值：上限的 80%。
DEFAULT_THRESHOLD_RATIO = 0.8
#: 上限的合理下限：低于这个数连一轮正常对话都放不下，直接判非法。
MIN_LIMIT_TOKENS = 1_000
#: 压缩后最多保留的最近消息条数（约 :data:`KEEP_RECENT_MESSAGES` // 2 轮对话）。
KEEP_RECENT_MESSAGES = 8
#: 压缩后至少保留的消息条数（一轮问答）：再少就不像「最近若干轮」了，
#: 也因此「最近两条本身就超上限」时判定为无法压缩（保留原上下文）。
MIN_KEEP_MESSAGES = 2
#: 压缩后的目标用量 = 上限 × 这个比例；留出余量，避免刚压完又立刻触发。
TARGET_AFTER_RATIO = 0.5
#: 折叠摘要的最大字符数；再长就只留尾部（越靠近现在越重要）。
MAX_SUMMARY_CHARS = 1_200
#: 摘要里每条消息保留的首段字符数。
MAX_SUMMARY_LINE_CHARS = 80
#: 每条消息的角色 / 分隔开销（近似值）。
MESSAGE_OVERHEAD_TOKENS = 4
#: 摘要块的开头（同时用于展示与估算，保证两处口径一致）。
SUMMARY_HEADER = "【较早对话摘要（自动压缩，原文仍保留在对话记录中）】"

_SETTING_PREFIX = "context_settings:"

#: 中日韩文字范围：CJK 部首/假名/统一表意文字、谚文、兼容表意文字、全角形式。
_CJK_RE = re.compile(r"[⺀-鿿가-힯豈-﫿＀-￯]")


class ContextSettingsError(ValueError):
    """上限 / 阈值不合法：消息给用户看，调用方按 422 返回。"""


@dataclass(slots=True)
class ContextSettings:
    """上下文窗口配置；缺省值就是仓库约定的默认值。"""

    limit_tokens: int = DEFAULT_LIMIT_TOKENS
    threshold_ratio: float = DEFAULT_THRESHOLD_RATIO

    @property
    def threshold_tokens(self) -> int:
        """达到这个估算值就触发压缩；永远至少 1，避免除零式的「永远触发」。"""
        return max(1, int(math.floor(self.limit_tokens * self.threshold_ratio)))


@dataclass(slots=True)
class CompactionOutcome:
    """一次压缩的结果：新摘要、折叠掉的条数、前后估算量。"""

    summary: str
    dropped: int
    before_tokens: int
    after_tokens: int


# ---------------------------------------------------------------------------
# 口径：近似 token 估算
# ---------------------------------------------------------------------------


def estimate_tokens(text: str | None) -> int:
    """按模块 docstring 的口径估算一段文字的 token 数。"""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    return math.ceil(cjk + (len(text) - cjk) / 4)


def estimate_message_tokens(message: Message) -> int:
    """一条消息的估算量：正文 + 固定角色开销。"""
    return estimate_tokens(message.content) + MESSAGE_OVERHEAD_TOKENS


def estimate_usage(
    system_prompt: str | None,
    summary: str | None,
    messages: Iterable[Message],
    prompt: str = "",
) -> int:
    """估算下一次请求的上下文大小（系统提示 + 摘要 + 历史 + 本轮提示）。

    展示（``GET /conversations/{id}/context``）与压缩决策（``prepare_run``）都用它。
    """
    total = estimate_tokens(system_prompt)
    total += estimate_tokens(_with_header(summary))
    total += estimate_tokens(prompt)
    for message in messages:
        total += estimate_message_tokens(message)
    return total


# ---------------------------------------------------------------------------
# 配置读写（PlatformSetting，与 titles.py 同一套约定）
# ---------------------------------------------------------------------------


def _setting_key(owner_id: str | None) -> str:
    """一个用户一条配置；未登录 / 鉴权关闭时落到 ``default``。"""
    return f"{_SETTING_PREFIX}{owner_id or 'default'}"


def validate_settings(limit_tokens: object, threshold_ratio: object) -> ContextSettings:
    """校验并归一化配置；非法输入抛 :class:`ContextSettingsError`，调用方**不得落盘**。"""
    if isinstance(limit_tokens, bool) or not isinstance(limit_tokens, int):
        raise ContextSettingsError("上下文上限必须是整数（tokens）")
    if limit_tokens <= 0:
        raise ContextSettingsError("上下文上限必须大于 0")
    if limit_tokens < MIN_LIMIT_TOKENS:
        raise ContextSettingsError(f"上下文上限至少为 {MIN_LIMIT_TOKENS} tokens")
    if isinstance(threshold_ratio, bool) or not isinstance(threshold_ratio, (int, float)):
        raise ContextSettingsError("触发阈值必须是数字（上限的比例）")
    ratio = float(threshold_ratio)
    if not (0 < ratio <= 1):
        raise ContextSettingsError("触发阈值必须满足 0 < 阈值 ≤ 上限（比例在 0 到 1 之间）")
    return ContextSettings(limit_tokens=limit_tokens, threshold_ratio=ratio)


async def read_settings(session: AsyncSession, owner_id: str | None) -> ContextSettings:
    """读配置：键不存在、为空或损坏时一律回到默认值。"""
    row = (
        await session.execute(
            select(PlatformSetting).where(PlatformSetting.key == _setting_key(owner_id))
        )
    ).scalar_one_or_none()
    if row is None or not row.value:
        return ContextSettings()
    try:
        data = json.loads(row.value)
        return validate_settings(
            int(data["limit_tokens"]), float(data["threshold_ratio"])
        )
    except (ValueError, KeyError, TypeError):
        # 坏数据不能让设置页 / 输入区直接崩：按默认值继续，日志留痕。
        logger.warning("上下文配置损坏，已回退默认值：%s", row.value[:200])
        return ContextSettings()


async def write_settings(
    session: AsyncSession, owner_id: str | None, settings: ContextSettings
) -> ContextSettings:
    """保存配置（调用前必须已 :func:`validate_settings`）。"""
    key = _setting_key(owner_id)
    value = json.dumps(
        {"limit_tokens": settings.limit_tokens, "threshold_ratio": settings.threshold_ratio},
        ensure_ascii=False,
    )
    row = (
        await session.execute(select(PlatformSetting).where(PlatformSetting.key == key))
    ).scalar_one_or_none()
    if row is None:
        session.add(PlatformSetting(key=key, value=value))
    else:
        row.value = value
        row.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return settings


# ---------------------------------------------------------------------------
# 压缩：确定性裁剪 + 摘要（不调模型、不阻塞输入）
# ---------------------------------------------------------------------------


async def conversation_usage(
    session: AsyncSession, conversation: Conversation, system_prompt: str | None
) -> int:
    """一条对话当前的上下文估算量（展示与「上限不得低于已用」校验共用）。"""
    rows = (
        await session.execute(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at)
        )
    ).scalars().all()
    active = active_messages(list(rows), conversation)
    # 估算要逐字扫全量正文，长对话时是纯 CPU 活：丢到线程池，别堵住事件循环
    # （这个接口会被前端在运行中定时轮询，堵住会拖慢正在推送的 SSE）。
    return await run_in_threadpool(
        estimate_usage, system_prompt, conversation.context_summary, active
    )


def active_messages(rows: Iterable[Message], conversation: Conversation) -> list[Message]:
    """本轮真正还会进上下文的消息：跳过已折叠进摘要的那一段。"""
    messages = [row for row in rows if row.role in ("user", "assistant") and row.content]
    return messages[max(0, conversation.context_compacted_count) :]


def build_history(
    rows: Iterable[Message],
    conversation: Conversation,
    clip: Callable[[list[Message]], list[dict[str, str]]],
) -> list[dict[str, str]]:
    """把有效消息裁成历史；有摘要时作为第一段注在前面。

    ``clip`` 由调用方传入（``runtime.service._clip_history``），保持既有的
    24 条 / 40k 字符硬上限，不在压缩之外再放大提示词。
    """
    active = active_messages(rows, conversation)
    history = clip(active)
    summary = _with_header(conversation.context_summary)
    if summary:
        history.insert(0, {"role": "user", "content": summary})
    return history


def _with_header(summary: str | None) -> str:
    if not summary:
        return ""
    return f"{SUMMARY_HEADER}\n{summary}"


def _digest(previous_summary: str | None, dropped: list[Message], cap: int) -> str:
    """把更早的消息折成一行一条的短摘要；保留已有的摘要内容。"""
    lines: list[str] = []
    if previous_summary:
        lines.append(previous_summary.strip())
    for message in dropped:
        text = " ".join((message.content or "").split())
        if len(text) > MAX_SUMMARY_LINE_CHARS:
            text = text[:MAX_SUMMARY_LINE_CHARS] + "…"
        label = "用户" if message.role == "user" else "智能体"
        lines.append(f"{label}：{text}")
    digest = "\n".join(lines)
    if len(digest) > cap:
        # 越靠近现在的对话越重要：超长时保留尾部。
        digest = "…" + digest[-cap:]
    return digest


def _summary_cap(limit_tokens: int) -> int:
    """摘要最多占上限的 1/5（并封顶 :data:`MAX_SUMMARY_CHARS`），给最近几轮留够空间。"""
    return min(MAX_SUMMARY_CHARS, max(200, limit_tokens // 5))


def compact_context(
    messages: list[Message],
    *,
    previous_summary: str | None,
    system_prompt: str | None,
    prompt: str,
    settings: ContextSettings,
) -> CompactionOutcome:
    """把较早消息折叠成摘要，保留最近若干条（按目标用量从新往旧收）。

    保留条数不是死数：从最新一条往前装，装到「上限的一半」为止，最多
    :data:`KEEP_RECENT_MESSAGES` 条、最少 :data:`MIN_KEEP_MESSAGES` 条。这样
    压缩后真的落在阈值以下，而不是压完还超标、下一轮又立刻触发。

    - 失败（有效消息太少、或最近两条本身就超过目标）抛 ``ValueError``；
      调用方必须**保留原上下文**并给出可理解的提示。
    - 只返回新的摘要与统计，不改数据库、不改传进来的消息列表。
    """
    before = estimate_usage(system_prompt, previous_summary, messages, prompt)
    if len(messages) <= MIN_KEEP_MESSAGES:
        raise ValueError(f"只有 {len(messages)} 条有效消息，已无法再安全压缩")

    target = max(1, int(settings.limit_tokens * TARGET_AFTER_RATIO))
    budget = target - estimate_tokens(system_prompt) - estimate_tokens(prompt)
    keep_count = 0
    for message in reversed(messages):
        if keep_count >= KEEP_RECENT_MESSAGES:
            break
        cost = estimate_message_tokens(message)
        if keep_count >= MIN_KEEP_MESSAGES and budget - cost < 0:
            break
        budget -= cost
        keep_count += 1
    keep_count = max(MIN_KEEP_MESSAGES, min(keep_count, len(messages)))
    if keep_count >= len(messages):
        raise ValueError("所有消息都在最近若干轮之内，无法再安全压缩")

    keep = messages[-keep_count:]
    dropped = messages[: len(messages) - keep_count]
    summary = _digest(previous_summary, dropped, _summary_cap(settings.limit_tokens))
    after = estimate_usage(system_prompt, summary, keep, prompt)
    return CompactionOutcome(
        summary=summary, dropped=len(dropped), before_tokens=before, after_tokens=after
    )


# ---------------------------------------------------------------------------
# 压缩状态（供按钮 / 详情面板展示「进行中 / 成功 / 失败」）
# ---------------------------------------------------------------------------

#: 每条对话最近一次压缩的结果。进程内即可：重启后按钮回到「尚未压缩」，
#: 真正的上下文并没有依赖它（摘要与折叠条数已落库）。
_compaction_state: dict[str, dict] = {}
#: 正在压缩的对话：防止同一对话重复触发。
_compacting: set[str] = set()


def begin_compaction(conversation_id: str) -> bool:
    """占住这条对话的压缩位；已在压缩中返回 False（本轮沿用原上下文）。"""
    if conversation_id in _compacting:
        return False
    _compacting.add(conversation_id)
    _compaction_state[conversation_id] = {
        "status": "compacting",
        "at": datetime.now(timezone.utc).isoformat(),
    }
    return True


def finish_compaction(
    conversation_id: str,
    *,
    ok: bool,
    before_tokens: int = 0,
    after_tokens: int = 0,
    dropped: int = 0,
    error: str | None = None,
) -> None:
    """记下压缩终态并释放占位。"""
    _compacting.discard(conversation_id)
    _compaction_state[conversation_id] = {
        "status": "ok" if ok else "error",
        "at": datetime.now(timezone.utc).isoformat(),
        "before_tokens": before_tokens,
        "after_tokens": after_tokens,
        "dropped": dropped,
        "error": error,
    }


def compaction_state(conversation_id: str) -> dict | None:
    """最近一次压缩状态；从未压缩过返回 None。"""
    return _compaction_state.get(conversation_id)


def reset_state(conversation_id: str) -> None:
    """对话被清空记忆 / 删除时清掉状态（摘要本身由调用方清）。"""
    _compaction_state.pop(conversation_id, None)
    _compacting.discard(conversation_id)


def usage_view(
    used_tokens: int,
    settings: ContextSettings,
    conversation: Conversation,
) -> dict:
    """界面需要的上下文用量视图（展示与阈值共用同一份估算口径）。"""
    limit = settings.limit_tokens
    # 真实百分比（可能超过 100，说明已经超限）；进度条宽度由界面自己截断。
    percent = round((used_tokens / limit * 100) if limit > 0 else 0.0, 2)
    return {
        "used_tokens": used_tokens,
        "limit_tokens": limit,
        "threshold_tokens": settings.threshold_tokens,
        "threshold_ratio": settings.threshold_ratio,
        "percent": percent,
        "compacted_count": conversation.context_compacted_count,
        "compacted_at": (
            conversation.context_compacted_at.isoformat()
            if conversation.context_compacted_at
            else None
        ),
        "strategy": (
            "保留系统提示与最近若干轮消息（按上限从新往旧保留，"
            f"最多 {KEEP_RECENT_MESSAGES} 条、至少 {MIN_KEEP_MESSAGES} 条），"
            "更早的对话折叠成一段摘要（原文仍留在对话记录里）"
        ),
        "estimation": "按字符近似估算：中日韩文字约 1 字 = 1 token，其余约 4 字符 = 1 token",
        "compaction": compaction_state(conversation.id),
    }
