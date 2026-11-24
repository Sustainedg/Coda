"""轨迹调试视图的采集与持久化（旁路，不参与对话业务）。

设计约束（见 ``docs/trace-debug-view-plan.md`` §3/§5）：

* 采集只在运行层判定一次（``prepare_run`` / ``regenerate_last`` 的 ``trace_enabled``）；
  关闭时不创建 :class:`TraceRecorder`，零开销、零写库。
* 写库使用**独立引擎/会话**（见下方 ``_trace_engine``），不与消息落库共用事务；
  任何写入失败只 ``logger.warning(..., exc_info=True)``，绝不打断正常回复与 SSE。
* 入库前统一脱敏：复用 :func:`app.credentials.scrub` 擦掉凭据值，并按字段名掩码
  ``Authorization`` / ``Cookie`` / ``api_key`` / ``token`` / ``secret`` 等敏感键。
* 体积上限写死在本模块：单事件 256 KiB、单会话 2000 条 / 20 MiB；超限按
  ``created_at`` 从旧到新裁剪，保证最新一轮仍然完整可查。
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
from typing import Any, Iterable, Sequence

from sqlalchemy import delete, event, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ..agents.events import EventType
from ..credentials import scrub
from ..models import TraceEvent, utcnow
from ..settings import get_settings

logger = logging.getLogger(__name__)

#: 轨迹写入用**独立引擎与会话**：与消息落库彻底解开事务，且给 SQLite 明确的等待窗口。
#: 主引擎（``db.py``）的 busy_timeout 较长，而轨迹是旁路采集、不能拖慢接口，所以这里
#: 只等 5 秒就退避重试：撞上瞬时写锁重试即可，绝不让整批轨迹丢掉。
_settings = get_settings()
_sqlite = _settings.sqlalchemy_url.startswith("sqlite")
_trace_engine = create_async_engine(
    _settings.sqlalchemy_url,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False, "timeout": 30} if _sqlite else {},
)
if _sqlite:  # pragma: no cover - 仅在 SQLite 部署下注册

    @event.listens_for(_trace_engine.sync_engine, "connect")
    def _trace_sqlite_pragmas(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=5000")
        # 与主引擎同一套提交成本：轨迹是旁路写入，更要避免一次 commit 卡住几秒占着写锁。
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


_TraceSessionLocal = async_sessionmaker(_trace_engine, expire_on_commit=False)

#: 单条轨迹事件 payload 的上限（序列化后的 UTF-8 字节数）。
MAX_EVENT_BYTES = 256 * 1024
#: 单对话最多保留的事件条数。
MAX_EVENTS_PER_CONV = 2000
#: 单对话轨迹数据的总体积上限（按 ``bytes`` 累加）。
MAX_CONV_BYTES = 20 * 1024 * 1024

#: 超限 payload 保留头 / 尾的字节数。
_TRUNCATE_HEAD_BYTES = 128 * 1024
_TRUNCATE_TAIL_BYTES = 64 * 1024

#: 上下文快照里每条历史正文与历史正文总量的上限。
_HISTORY_ENTRY_BYTES = 8 * 1024
_HISTORY_TOTAL_BYTES = 32 * 1024

#: 批量提交策略：积攒 20 条或距第一条 200 ms 时落一次库。
_BATCH_SIZE = 20
_FLUSH_INTERVAL = 0.2

#: 分页接口单页上限（防止 ``limit`` 被无限放大）。
MAX_PAGE_LIMIT = 500

_DATA_URL = re.compile(r"^data:([^;,]+)?;base64,", re.IGNORECASE)
_BASE64_BODY = re.compile(r"[A-Za-z0-9+/=\r\n]+")

#: 键名精确命中即掩码（统一转小写、``-`` 换 ``_`` 后比较）。
_SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "set_cookie",
    "x_api_key",
    "api_key",
    "apikey",
    "token",
    "access_token",
    "refresh_token",
    "auth_token",
    "secret",
    "client_secret",
    "passwd",
    "password",
}
#: 后缀命中即掩码（避免误伤 ``prompt_tokens`` / ``max_tokens`` 这类用量字段）。
_SENSITIVE_SUFFIXES = ("_token", "_secret", "_api_key", "_password", "_passwd")


def _normalise_key(key: str) -> str:
    return key.strip().lower().replace("-", "_")


def _is_sensitive_key(key: str) -> bool:
    normalised = _normalise_key(key)
    return normalised in _SENSITIVE_KEYS or normalised.endswith(_SENSITIVE_SUFFIXES)


def _binary_placeholder(text: str) -> dict[str, Any] | None:
    """把内联的二进制 / base64 内容换成可读引用，避免整段图片进库。"""
    match = _DATA_URL.match(text)
    if match:
        return {"__omitted__": "binary", "mime": match.group(1), "bytes": len(text)}
    stripped = text.strip()
    if (
        len(stripped) >= 1024
        and len(stripped) % 4 == 0
        and _BASE64_BODY.fullmatch(stripped)
    ):
        return {"__omitted__": "binary", "mime": None, "bytes": len(text)}
    return None


def sanitize(value: Any, *, secrets: Sequence[str] = (), _path: str = "") -> tuple[Any, list[str]]:
    """递归脱敏：擦掉凭据值、掩码敏感键、替换内联二进制。

    返回 ``(脱敏后的值, 被掩码的字段路径列表)``；路径形如
    ``headers.Authorization``，调用方会把它写进 payload 的 ``redacted_fields``。
    """
    redacted: list[str] = []

    def walk(item: Any, path: str) -> Any:
        if isinstance(item, dict):
            out: dict[str, Any] = {}
            for key, val in item.items():
                name = str(key)
                child = f"{path}.{name}" if path else name
                if _is_sensitive_key(name):
                    redacted.append(child)
                    out[name] = "***"
                else:
                    out[name] = walk(val, child)
            return out
        if isinstance(item, (list, tuple)):
            return [walk(val, f"{path}[{index}]") for index, val in enumerate(item)]
        if isinstance(item, (bytes, bytearray, memoryview)):
            return {"__omitted__": "binary", "mime": None, "bytes": len(item)}
        if isinstance(item, str):
            placeholder = _binary_placeholder(item)
            if placeholder is not None:
                return placeholder
            return scrub(item, secrets)
        return item

    result = walk(value, _path)
    return result, redacted


def _serialise(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))


def _truncate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """超过单事件上限时保留头 128 KiB + 尾 64 KiB（JSON 文本层面截断）。"""
    raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    head = raw[:_TRUNCATE_HEAD_BYTES].decode("utf-8", "ignore")
    tail = raw[-_TRUNCATE_TAIL_BYTES:].decode("utf-8", "ignore")
    return {
        "__truncated__": True,
        "head": head,
        "tail": tail,
        "original_bytes": len(raw),
    }


class RawCollector:
    """旁路累积原始输出行，只保留头 / 尾两段，避免长输出撑爆内存。"""

    def __init__(self, head_bytes: int = _TRUNCATE_HEAD_BYTES, tail_bytes: int = _TRUNCATE_TAIL_BYTES):
        self._head_limit = head_bytes
        self._tail_limit = tail_bytes
        self._head: list[str] = []
        self._tail: list[str] = []
        self._head_bytes = 0
        self._tail_bytes = 0
        self.truncated = False
        self.count = 0

    def add(self, line: str) -> None:
        self.count += 1
        size = len(line.encode("utf-8")) + 1
        if not self.truncated and self._head_bytes + size <= self._head_limit:
            self._head.append(line)
            self._head_bytes += size
            return
        self.truncated = True
        self._tail.append(line)
        self._tail_bytes += size
        while self._tail and self._tail_bytes > self._tail_limit:
            old = self._tail.pop(0)
            self._tail_bytes -= len(old.encode("utf-8")) + 1

    def lines(self) -> list[str]:
        return [*self._head, *self._tail]


class TraceWriter:
    """独立短会话的批量写入器：写失败只告警，绝不向调用方抛异常。"""

    def __init__(self) -> None:
        self._pending: list[dict[str, Any]] = []
        self._timer: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def enqueue(self, record: dict[str, Any]) -> None:
        self._pending.append(record)
        if len(self._pending) >= _BATCH_SIZE:
            await self.flush()
        else:
            self._ensure_timer()

    async def write_now(self, record: dict[str, Any]) -> None:
        self._pending.append(record)
        await self.flush()

    def _ensure_timer(self) -> None:
        if self._timer is not None and not self._timer.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # 没有事件循环（理论上不会发生）时退化为调用方 flush
            return
        self._timer = loop.create_task(self._flush_after())

    async def _flush_after(self) -> None:
        try:
            await asyncio.sleep(_FLUSH_INTERVAL)
            await self.flush()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 旁路采集绝不能打断运行
            logger.warning("轨迹后台写入失败", exc_info=True)

    async def flush(self) -> None:
        current = asyncio.current_task()
        timer = self._timer
        if timer is not None and timer is not current:
            if not timer.done():
                timer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await timer
            self._timer = None
        # 串行化并发 flush：定时器与运行收尾可能同时到达；取出的批次在失败时放回队列，
        # 绝不让一次取消/写失败把待落盘的事件悄悄弄丢。
        async with self._lock:
            await self._write_batch()

    async def _write_batch(self) -> None:
        if not self._pending:
            return
        batch = self._pending
        self._pending = []
        # 轨迹与消息落库是两条独立连接。SQLite 同一时刻只允许一个写者：若恰好撞上
        # 消息 commit，会短暂 "database is locked"。重试几次即可，绝不把整批轨迹丢掉。
        for attempt in range(5):
            try:
                async with _TraceSessionLocal() as session:
                    session.add_all([TraceEvent(**record) for record in batch])
                    await session.commit()
                    # 裁剪失败不影响这批数据已经落库；下一批再尝试即可（避免重试导致重复插入）。
                    try:
                        await self._prune(session, {record["conversation_id"] for record in batch})
                        await session.commit()
                    except Exception:  # noqa: BLE001 - 裁剪是尽力而为
                        await session.rollback()
                        logger.warning("轨迹裁剪失败", exc_info=True)
                return
            except OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 4:
                    self._pending = batch + self._pending
                    logger.warning("轨迹写入失败", exc_info=True)
                    return
                await asyncio.sleep(0.05 * (attempt + 1))
            except BaseException as exc:  # noqa: BLE001 - 被取消时把批次放回，交给下一次 flush
                self._pending = batch + self._pending
                if not isinstance(exc, Exception):
                    raise
                logger.warning("轨迹写入失败", exc_info=True)
                return

    @staticmethod
    async def _prune(session: Any, conversation_ids: Iterable[str]) -> None:
        """按 created_at 从旧到新删除，直到条数与体积都回到上限内。"""
        for conversation_id in conversation_ids:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(TraceEvent)
                    .where(TraceEvent.conversation_id == conversation_id)
                )
            ).scalar() or 0
            total = (
                await session.execute(
                    select(func.coalesce(func.sum(TraceEvent.bytes), 0)).where(
                        TraceEvent.conversation_id == conversation_id
                    )
                )
            ).scalar() or 0
            if count <= MAX_EVENTS_PER_CONV and total <= MAX_CONV_BYTES:
                continue
            rows = (
                await session.execute(
                    select(TraceEvent.id, TraceEvent.bytes)
                    .where(TraceEvent.conversation_id == conversation_id)
                    .order_by(TraceEvent.created_at, TraceEvent.seq)
                )
            ).all()
            doomed: list[str] = []
            remaining_count = count
            remaining_bytes = int(total)
            for row_id, size in rows:
                if remaining_count <= MAX_EVENTS_PER_CONV and remaining_bytes <= MAX_CONV_BYTES:
                    break
                # 至少保留最新一条，避免把正在看的那一轮裁空。
                if remaining_count <= 1:
                    break
                doomed.append(row_id)
                remaining_count -= 1
                remaining_bytes -= int(size or 0)
            # 分批删除：SQLite 对单条语句的绑定变量数有上限，积压很多时不能一次 IN 上千个 id。
            for start in range(0, len(doomed), 500):
                chunk = doomed[start : start + 500]
                await session.execute(delete(TraceEvent).where(TraceEvent.id.in_(chunk)))


def _sha256_prefix(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _history_snapshot(history: list[dict[str, str]]) -> tuple[list[dict[str, Any]], bool]:
    """历史只存元数据 + 截断正文：单条 ≤8 KiB、总量 ≤32 KiB。"""
    entries: list[dict[str, Any]] = []
    budget = _HISTORY_TOTAL_BYTES
    truncated = False
    for turn in history:
        content = str(turn.get("content") or "")
        raw = content.encode("utf-8")
        piece = raw[:_HISTORY_ENTRY_BYTES]
        if len(piece) < len(raw):
            truncated = True
        if len(piece) > budget:
            piece = piece[:budget]
            truncated = True
        budget -= len(piece)
        entry: dict[str, Any] = {"role": turn.get("role"), "chars": len(content)}
        if piece:
            entry["content"] = piece.decode("utf-8", "ignore")
        entries.append(entry)
    return entries, truncated


def build_context_snapshot(ctx: Any, user_message_id: str | None) -> dict[str, Any]:
    """按裁决 3 组装 ``context_snapshot``：系统提示词只留长度与哈希，不存正文。"""
    system_prompt = ctx.system_prompt or None
    history, truncated = _history_snapshot(list(ctx.history or []))
    payload: dict[str, Any] = {
        "system_prompt": (
            {"chars": len(system_prompt), "sha256_prefix": _sha256_prefix(system_prompt)}
            if system_prompt
            else None
        ),
        "prompt": ctx.prompt,
        "history": history,
        "history_count": len(history),
        "model": ctx.model,
        "workspace_dir": ctx.workspace_dir,
        "kernel_session_id": ctx.session_id,
        "agent_profile_id": ctx.agent_id,
        "skills": [getattr(skill, "name", None) or getattr(skill, "slug", "") for skill in ctx.skills or []],
        "mcp_servers": [getattr(server, "name", "") for server in ctx.mcp_servers or []],
        "user_message_id": user_message_id,
    }
    if truncated:
        payload["truncated"] = True
    return payload


def _secrets_from_ctx(ctx: Any) -> list[str]:
    secrets = list((getattr(ctx, "credentials", None) or {}).values())
    if getattr(ctx, "api_key", None):
        secrets.append(ctx.api_key)
    return [value for value in secrets if value]


class TraceRecorder:
    """一次运行的轨迹采集器；``enabled`` 为假时所有方法都是空操作。"""

    def __init__(
        self,
        *,
        conversation_id: str,
        owner_id: str | None = None,
        run_id: str | None = None,
        turn_index: int = 0,
        start_seq: int = 0,
        secrets: Sequence[str] = (),
    ) -> None:
        self.conversation_id = conversation_id
        self.owner_id = owner_id
        self.run_id = run_id
        self.turn_index = turn_index
        self.enabled = True
        self._seq = int(start_seq or 0)
        self._secrets = tuple(secrets)
        self._writer = TraceWriter()

    @classmethod
    async def create(
        cls,
        session: Any,
        *,
        conversation_id: str,
        owner_id: str | None,
        run_id: str | None,
        turn_index: int,
        ctx: Any,
        user_message_id: str | None,
    ) -> "TraceRecorder":
        """建采集器并**同步**写下本轮 ``context_snapshot``。"""
        max_seq = (
            await session.execute(
                select(func.max(TraceEvent.seq)).where(
                    TraceEvent.conversation_id == conversation_id
                )
            )
        ).scalar()
        recorder = cls(
            conversation_id=conversation_id,
            owner_id=owner_id,
            run_id=run_id,
            turn_index=turn_index,
            start_seq=max_seq or 0,
            secrets=_secrets_from_ctx(ctx),
        )
        await recorder.context(build_context_snapshot(ctx, user_message_id))
        return recorder

    async def context(self, payload: dict[str, Any]) -> None:
        await self._emit("context_snapshot", payload, immediate=True)

    async def request(
        self,
        payload: dict[str, Any],
        *,
        duration_ms: int | None = None,
        error: str | None = None,
        truncated: bool = False,
        immediate: bool = False,
    ) -> None:
        await self._emit(
            "request", payload, duration_ms=duration_ms, error=error, truncated=truncated, immediate=immediate
        )

    async def response(
        self,
        payload: dict[str, Any],
        *,
        duration_ms: int | None = None,
        error: str | None = None,
        truncated: bool = False,
        immediate: bool = False,
    ) -> None:
        await self._emit(
            "response", payload, duration_ms=duration_ms, error=error, truncated=truncated, immediate=immediate
        )

    async def on_event(self, event: Any) -> None:
        """把规范化的工具事件落成独立的 ``tool_call`` / ``tool_result`` 条目。"""
        if not self.enabled:
            return
        event_type = getattr(event, "type", None)
        payload = getattr(event, "payload", None) or {}
        if event_type is EventType.TOOL_CALL:
            await self._emit(
                "tool_call",
                {
                    "name": getattr(event, "name", None),
                    "call_id": getattr(event, "call_id", None),
                    "input": payload.get("input") or {},
                    "step": payload.get("step"),
                },
            )
        elif event_type is EventType.TOOL_RESULT:
            await self._emit(
                "tool_result",
                {
                    "call_id": getattr(event, "call_id", None),
                    "output": payload.get("output"),
                    "is_error": payload.get("is_error", False),
                },
            )

    async def flush(self) -> None:
        if not self.enabled:
            return
        await self._writer.flush()

    async def _emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        duration_ms: int | None = None,
        error: str | None = None,
        truncated: bool = False,
        immediate: bool = False,
    ) -> None:
        if not self.enabled:
            return
        try:
            safe_payload, redacted = sanitize(payload, secrets=self._secrets)
            if not isinstance(safe_payload, dict):
                safe_payload = {"value": safe_payload}
            if redacted:
                safe_payload["redacted_fields"] = redacted
            size = _serialise(safe_payload)
            event_truncated = bool(truncated)
            if size > MAX_EVENT_BYTES:
                safe_payload = _truncate_payload(safe_payload)
                event_truncated = True
            self._seq += 1
            record = {
                "conversation_id": self.conversation_id,
                "owner_id": self.owner_id,
                "run_id": self.run_id,
                "turn_index": self.turn_index,
                "seq": self._seq,
                "type": event_type,
                "payload": safe_payload,
                "duration_ms": duration_ms,
                "error": error,
                "truncated": event_truncated,
                "bytes": size,
                "created_at": utcnow(),
            }
            if immediate:
                await self._writer.write_now(record)
            else:
                await self._writer.enqueue(record)
        except Exception:  # noqa: BLE001 - 采集失败不能影响正常回复
            logger.warning("轨迹采集失败", exc_info=True)


__all__ = [
    "MAX_CONV_BYTES",
    "MAX_EVENTS_PER_CONV",
    "MAX_EVENT_BYTES",
    "MAX_PAGE_LIMIT",
    "RawCollector",
    "TraceRecorder",
    "TraceWriter",
    "build_context_snapshot",
    "sanitize",
]
