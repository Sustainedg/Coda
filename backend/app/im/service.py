"""IM 连接管理：连接的增删改、配对、聊天绑定，以及活着的连接与发送队列。

这里是 IM 域的唯一编排层：

* 适配器只管收发（``platforms/*``），这里负责「哪条连接该活着」「状态怎么落库」；
* 消息进来 → 交给 :mod:`app.im.bridge`（与对话域的唯一边界）；
* 回复出去 → 走每条连接的发送队列（失败重试、永久失败显示原因）。

连接状态只写数据库的 ``last_status`` / ``last_error``（设置页轮询它），
不把运行时对象暴露给接口层。
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..db import SessionLocal
from ..models import AgentProfile
from ..security import decrypt
from . import bridge
from .base import (
    AdapterError,
    ChatRef,
    ConnectionBase,
    Handlers,
    InboundAction,
    InboundMessage,
    OutboundMessage,
    Platform,
    platform_of,
    retry_delay,
    scrub,
)
from .platforms import adapter_for
from .tables import ImChat, ImConnection, ImPeer

logger = logging.getLogger(__name__)

#: ``/stop`` / ``/stop@机器人`` / ``/start`` —— 命令后面可能跟着 @用户名。
_COMMAND = re.compile(r"^/(stop|start|help)(?:@\S+)?$", re.IGNORECASE)

#: 发送失败的重试次数与退避上限（秒）。
SEND_ATTEMPTS = 4
SEND_RETRY_MAX = 30.0
#: 每轮回复期间补发「正在输入」的间隔（秒）。
TYPING_INTERVAL = 4.0
#: 状态写库的节流：连接状态变化很频繁（每次轮询成功都算一次），没必要每条都落库。
STATUS_SAVE_SECONDS = 1.0


class ImError(Exception):
    """面向用户的 IM 域错误；``status_code`` 交给接口层。"""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


# --- 发送队列 ---------------------------------------------------------------


class _Sender:
    """一条连接的发送队列：同一聊天按顺序发，失败按退避重试。

    ``permanent`` 的失败不重试（凭据失效、超出平台回复窗口），把原因记在状态上；
    网络类失败重试，超过次数后同样记状态 —— 一条发不出去的消息不能卡住后面的。
    """

    def __init__(self, connection_id: str, handle: ConnectionBase) -> None:
        self.connection_id = connection_id
        self.handle = handle
        self.queues: dict[str, asyncio.Queue] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        #: 正在发送的那条消息的 future：``close()`` 时也要把它结束掉，
        #: 否则调用方会永远挂在 ``await submit()`` 上（协程泄漏）。
        self.inflight: set[asyncio.Future] = set()
        self.closed = False

    def submit(self, chat: ChatRef, message: OutboundMessage) -> Awaitable[None]:
        done: asyncio.Future = asyncio.get_running_loop().create_future()
        if self.closed:
            done.set_exception(ImError("连接已断开，这条回复没有发出去", status_code=409))
            return done
        queue = self.queues.get(chat.key)
        if queue is None:
            queue = asyncio.Queue()
            self.queues[chat.key] = queue
            self.tasks[chat.key] = asyncio.create_task(self._drain(chat, queue))
        queue.put_nowait((message, done))
        return done

    async def _drain(self, chat: ChatRef, queue: asyncio.Queue) -> None:
        while True:
            message, done = await queue.get()
            self.inflight.add(done)
            try:
                await self._send_with_retry(chat, message)
            except ImError as exc:
                if not done.done():
                    done.set_exception(exc)
            else:
                if not done.done():
                    done.set_result(None)
            finally:
                self.inflight.discard(done)
                queue.task_done()

    async def _send_with_retry(self, chat: ChatRef, message: OutboundMessage) -> None:
        last: Exception | None = None
        for attempt in range(SEND_ATTEMPTS):
            try:
                await self.handle.send(chat, message)
                return
            except AdapterError as exc:
                last = exc
                if exc.permanent:
                    raise ImError(f"发送失败：{exc}", status_code=409) from None
                await asyncio.sleep(exc.retry_after or retry_delay(attempt))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 适配器的意外错误也要记下来
                last = exc
                await asyncio.sleep(retry_delay(attempt))
        raise ImError(f"发送失败：{last}（已重试 {SEND_ATTEMPTS} 次）", status_code=409)

    async def close(self) -> None:
        """关掉队列：排队的与正在发的都明确失败，调用方不会悬空等待。"""
        self.closed = True
        disconnected = ImError("连接已断开，这条回复没有发出去", status_code=409)
        for queue in self.queues.values():
            while not queue.empty():
                _message, done = queue.get_nowait()
                if not done.done():
                    done.set_exception(disconnected)
        # 正在发送的那条：cancel 掉 _drain 后它不会再 set_result，必须由这里收尾。
        for done in list(self.inflight):
            if not done.done():
                done.set_exception(disconnected)
        for task in self.tasks.values():
            task.cancel()
        for task in self.tasks.values():
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 收尾时不再关心
                pass
        self.inflight.clear()
        self.tasks.clear()
        self.queues.clear()


# --- 连接登记表 -------------------------------------------------------------


class _Live:
    """一条活着的连接：适配器句柄 + 发送队列 + 状态。"""

    def __init__(self, connection_id: str, handle: ConnectionBase) -> None:
        self.connection_id = connection_id
        self.handle = handle
        self.sender = _Sender(connection_id, handle)
        self.status = "connecting"
        self.error: str | None = None
        self.typing_task: asyncio.Task | None = None


class ConnectionManager:
    """所有 IM 连接的运行时登记表。

    生命周期跟着应用走（``main.lifespan`` 启动时 ``start_all``、关闭时 ``stop_all``）；
    设置页的增删改会立刻 ``sync``，不必重启服务。
    """

    def __init__(self) -> None:
        self._live: dict[str, _Live] = {}
        self._stopped = False
        #: 每个聊天一把锁：同一聊天的消息按顺序进智能体，不会并发开两轮
        self._locks: dict[str, asyncio.Lock] = {}
        #: 连接级状态的节流落库任务（同一条连接同时只有一个）
        self._state_tasks: dict[str, asyncio.Task] = {}
        #: 上一次落库的状态，用来跳过重复写入
        self._last_saved: dict[str, tuple[str, str | None]] = {}
        #: 入站处理的后台任务（连接停掉时一起取消）
        self._tasks: set[asyncio.Task] = set()
        #: 状态落库任务：持强引用，否则 ``create_task`` 的结果可能被 GC 回收，
        #: 状态就偶尔写不进去（见 ``_make_on_status``）。
        self._save_tasks: set[asyncio.Task] = set()

    # -- 后台任务 -----------------------------------------------------------
    def spawn(self, connection_id: str, coro: Awaitable[None]) -> None:
        """把一条入站处理丢到后台：适配器的读循环必须一直转着。"""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        def report(finished: asyncio.Task) -> None:
            if finished.cancelled():
                return
            error = finished.exception()
            if error is not None:
                logger.warning("IM 处理失败（%s）：%s", connection_id, error, exc_info=error)

        task.add_done_callback(report)

    # -- 查询 ---------------------------------------------------------------
    def status_of(self, connection_id: str) -> dict[str, Any] | None:
        live = self._live.get(connection_id)
        if live is None:
            return None
        return {"state": live.status, "error": live.error}

    # -- 启停 ---------------------------------------------------------------
    def resume(self) -> None:
        """清掉「已停止」标记，让同一个进程里的下一轮（重启 / 测试）还能连。

        ``stop_all`` 是给应用关闭用的：它把标记立起来，防止关闭途中又被重新拉起。
        """
        self._stopped = False
        self._last_saved.clear()
        self._locks.clear()

    async def start_all(self) -> None:
        self.resume()
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(ImConnection).where(ImConnection.enabled == True)  # noqa: E712
                )
            ).scalars().all()
        for row in rows:
            await self.start(row.id)
        if rows:
            logger.info("IM：已启动 %d 条连接", len(rows))

    async def _cancel_tasks(self, tasks: list[asyncio.Task[Any]]) -> None:
        """取消并**等待**一批后台任务真正退出（只处理当前事件循环里的）。

        只取消不等待会留下「正在落库」的连接：任务要等下一轮事件循环才收尾，
        测试删表或进程退出时就会撞上 ``database is locked``。
        """
        loop = asyncio.get_running_loop()
        pending = [task for task in tasks if not task.done() and task.get_loop() is loop]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def stop_all(self) -> None:
        self._stopped = True
        for connection_id in list(self._live):
            await self.stop(connection_id)
        # 入站任务、状态落库任务、节流的运行状态任务都要收干净：
        # 否则它们会在下一个用例删表 / 进程退出时继续写库。
        await self._cancel_tasks([*self._tasks, *self._save_tasks, *self._state_tasks.values()])
        self._tasks.clear()
        self._save_tasks.clear()
        self._state_tasks.clear()

    async def sync(self, connection_id: str) -> None:
        """按库里的最新状态决定这条连接该不该活着。"""
        if self._stopped:
            return
        async with SessionLocal() as session:
            row = await session.get(ImConnection, connection_id)
        if row is None or not row.enabled:
            await self.stop(connection_id)
            return
        adapter = adapter_for(row.platform)
        if adapter is None:
            await self.stop(connection_id)
            await _save_status(connection_id, "error", "这个平台暂不支持连接")
            return
        if not _has_secrets(row):
            await self.stop(connection_id)
            await _save_status(connection_id, "error", "凭据不完整，请重新填写")
            return
        if connection_id in self._live:
            await self.stop(connection_id)
        await self.start(connection_id)

    async def start(self, connection_id: str) -> None:
        if self._stopped or connection_id in self._live:
            return
        async with SessionLocal() as session:
            row = await session.get(ImConnection, connection_id)
        if row is None or not row.enabled:
            return
        adapter = adapter_for(row.platform)
        if adapter is None:
            return
        fields = runtime_fields(row)
        handlers = Handlers(
            on_message=_make_on_message(connection_id),
            on_action=_make_on_action(connection_id),
            on_status=_make_on_status(connection_id),
        )
        live = _Live(connection_id, _PlaceholderHandle(handlers))
        self._live[connection_id] = live
        try:
            handle = await adapter.connect(fields, handlers, dict(row.runtime or {}))
        except AdapterError as exc:
            self._live.pop(connection_id, None)
            await _save_status(connection_id, "error", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - 适配器建不起来也要显示原因
            self._live.pop(connection_id, None)
            await _save_status(connection_id, "error", f"连接失败：{scrub(str(exc))}")
            return
        if self._live.get(connection_id) is not live:
            await handle.stop()
            return
        live.handle = handle
        live.sender.handle = handle
        self._attach_state_saver(connection_id, handle)
        handle.begin()

    def _attach_state_saver(self, connection_id: str, handle: ConnectionBase) -> None:
        """让适配器的运行状态（长轮询游标）能落库：给它一个节流的保存回调。

        游标每条消息都会变，但没必要每条都写库：0.4 秒内只写一次（后到的覆盖先到的）。
        """
        state = getattr(handle, "state", None)
        if state is None:
            return

        async def persist() -> None:
            await asyncio.sleep(0.4)
            self._state_tasks.pop(connection_id, None)
            async with SessionLocal() as session:
                row = await session.get(ImConnection, connection_id)
                if row is None:
                    return
                row.runtime = dict(state.get())
                row.updated_at = datetime.now(timezone.utc)
                session.add(row)
                await session.commit()

        def changed() -> None:
            if connection_id in self._state_tasks:
                return
            self._state_tasks[connection_id] = asyncio.create_task(persist())

        state.bind(changed)

    async def stop(self, connection_id: str) -> None:
        live = self._live.pop(connection_id, None)
        # 这条连接的聊天锁没人用了：不清理的话，长期运行会按聊天数慢慢堆积。
        for key in [item for item in self._locks if item.startswith(f"{connection_id}:")]:
            self._locks.pop(key, None)
        # 节流的运行状态任务可能在 0.4 秒后写库：连接都停了就取消并等它退出。
        state_task = self._state_tasks.pop(connection_id, None)
        if state_task is not None:
            await self._cancel_tasks([state_task])
        if live is None:
            return
        if live.typing_task is not None:
            live.typing_task.cancel()
            live.typing_task = None
        await live.sender.close()
        try:
            await live.handle.stop()
        except Exception:  # noqa: BLE001 - 关连接失败不影响后续
            logger.debug("IM 连接关闭时出错", exc_info=True)
        await _save_status(connection_id, "off", None)

    # -- 发送 ---------------------------------------------------------------
    async def send(self, connection_id: str, chat: ChatRef, message: OutboundMessage) -> None:
        live = self._live.get(connection_id)
        if live is None:
            raise ImError(
                "这个 IM 机器人当前没有连接，请稍后在「智能体 → 设定 → IM」里重试", status_code=409
            )
        await live.sender.submit(chat, message)

    # -- 入站 ---------------------------------------------------------------
    async def handle_message(self, connection_id: str, message: InboundMessage) -> None:
        async with SessionLocal() as session:
            row = await session.get(ImConnection, connection_id)
            if row is None:
                return
            platform = platform_of(row.platform)
            paired = await _peer_state(session, connection_id, message.sender.id)
            if paired != "allowed":
                await _ask_pairing(session, row, message, paired)
                return
            if message.chat.kind == "group" and not message.mentioned:
                return  # 群里没 @ 机器人：不打扰、也不进智能体
            command = _COMMAND.match(message.text.strip())
            if command is not None and command.group(1) == "stop":
                await self._stop_chats(connection_id, message.chat)
                return
            if command is not None and command.group(1) in ("start", "help"):
                target = row.agent_profile_id
                name = ""
                if target:
                    profile = await session.get(AgentProfile, target)
                    name = profile.name if profile is not None else ""
                greeting = (
                    f"你好，我是 {name}。直接说话就行，/stop 可以停下正在做的事。"
                    if name
                    else "这个机器人还没绑定智能体，请在设置里选一个。"
                )
                await self._safe_send(connection_id, message.chat, greeting)
                return
            if not message.text.strip() and not message.images and not message.files:
                return
            chat_row = await bridge.ensure_chat(session, row, message.chat)
            await session.commit()
            target = chat_row.agent_profile_id or row.agent_profile_id
            profile = await session.get(AgentProfile, target) if target else None
        if platform is not None and not platform.groups and message.chat.kind == "group":
            await self._safe_send(connection_id, message.chat, "这个平台只支持私聊。")
            return
        if profile is None or profile.deleted_at is not None:
            await self._safe_send(connection_id, message.chat, _text("unbound", platform))
            return
        lock_key = f"{connection_id}:{message.chat.key}"
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            # 上一条还在跑：先回一句让用户知道排队了，再等它跑完。
            await self._safe_send(
                connection_id, message.chat, "上一条还在处理，这条排在后面。"
            )
        async with lock:
            self._start_typing(connection_id, message.chat)
            try:
                sink = await bridge.dispatch_message(row, message)
            except bridge.ImReplyError as exc:
                await self._safe_send(connection_id, message.chat, str(exc))
                return
            except Exception as exc:  # noqa: BLE001 - 一轮失败不能拖垮连接
                logger.warning(
                    "IM 消息处理失败（%s）：%s", connection_id, exc, exc_info=True
                )
                await self._safe_send(
                    connection_id, message.chat, f"这一轮没能完成：{scrub(str(exc))}"
                )
                return
            finally:
                self._stop_typing(connection_id)
            if sink.text:
                await self._safe_send(connection_id, message.chat, sink.text)

    async def _stop_chats(self, connection_id: str, chat: ChatRef) -> None:
        """``/stop``：停下这个聊天正在跑的那一轮（找不到就如实说）。"""
        stopped = False
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(ImChat).where(
                        ImChat.connection_id == connection_id, ImChat.chat_key == chat.key
                    )
                )
            ).scalars().all()
        for row in rows:
            if row.conversation_id and bridge.cancel_conversation(row.conversation_id):
                stopped = True
        await self._safe_send(
            connection_id, chat, "已停下。" if stopped else "现在没有在做的活。"
        )

    async def handle_action(self, connection_id: str, action: InboundAction) -> None:
        """按钮点击：当前只支持「停止」这一类简单动作，其余按键忽略。"""
        async with SessionLocal() as session:
            row = await session.get(ImConnection, connection_id)
            if row is None:
                return
            paired = await _peer_state(session, connection_id, action.sender.id)
        if paired != "allowed":
            return
        if action.data.strip() == "stop":
            await self._stop_chats(connection_id, action.chat)

    def _start_typing(self, connection_id: str, chat: ChatRef) -> None:
        """边跑边发「正在输入」；不支持这个能力的平台（句柄没覆盖 typing）自动跳过。"""
        live = self._live.get(connection_id)
        if live is None or live.typing_task is not None:
            return
        if type(live.handle).typing is ConnectionBase.typing:
            return

        async def beat() -> None:
            while True:
                try:
                    await live.handle.typing(chat)
                except Exception:  # noqa: BLE001 - 「正在输入」失败无所谓
                    pass
                await asyncio.sleep(TYPING_INTERVAL)

        live.typing_task = asyncio.create_task(beat())

    def _stop_typing(self, connection_id: str) -> None:
        live = self._live.get(connection_id)
        if live is None or live.typing_task is None:
            return
        live.typing_task.cancel()
        live.typing_task = None

    async def _safe_send(self, connection_id: str, chat: ChatRef, text: str) -> None:
        try:
            await self.send(connection_id, chat, OutboundMessage(text=text))
        except ImError as exc:
            logger.warning("IM 回复发送失败（%s）：%s", connection_id, exc)


class _PlaceholderHandle(ConnectionBase):
    """建连之前的占位句柄：发送会明确报错，而不是悄悄丢消息。"""

    async def send(self, chat: ChatRef, message: OutboundMessage) -> None:
        raise AdapterError("这条 IM 连接还没建立起来", permanent=True)

    async def open(self, generation: int) -> None:
        return None


# --- 回调构造 ---------------------------------------------------------------


def _make_on_message(connection_id: str) -> Callable[[InboundMessage], Awaitable[None]]:
    async def on_message(message: InboundMessage) -> None:
        # 一轮运行可能要跑很久，适配器的读循环**不能**等它：否则同一个机器人上
        # 其它聊天的消息、以及「/stop」都会被堵到这一轮结束才处理。
        # 排队由每聊天一把锁负责（见 handle_message），这里只把活丢到后台。
        _manager.spawn(connection_id, _manager.handle_message(connection_id, message))

    return on_message


def _make_on_action(connection_id: str) -> Callable[[InboundAction], Awaitable[None]]:
    async def on_action(action: InboundAction) -> None:
        _manager.spawn(connection_id, _manager.handle_action(connection_id, action))

    return on_action


def _make_on_status(connection_id: str) -> Callable[..., None]:
    def on_status(state: str, error: str | None = None, account: Any = None) -> None:
        live = _manager._live.get(connection_id)  # 同模块内的登记表
        if live is not None:
            live.status = state
            live.error = error
        # 持强引用：只靠事件循环的弱引用，任务可能还没跑就被回收。
        task = asyncio.create_task(_save_status(connection_id, state, error, account))
        _manager._save_tasks.add(task)  # 同模块内的登记表
        task.add_done_callback(_manager._save_tasks.discard)

    return on_status


# --- 状态与库操作 -----------------------------------------------------------


async def _save_status(
    connection_id: str,
    state: str,
    error: str | None,
    account: Any = None,
) -> None:
    last = _manager._last_saved.get(connection_id)  # 同模块内的登记表
    if last == (state, error) and account is None:
        return
    _manager._last_saved[connection_id] = (state, error)
    async with SessionLocal() as session:
        row = await session.get(ImConnection, connection_id)
        if row is None:
            return
        row.last_status = state
        row.last_error = error
        if account is not None:
            row.account_id = str(getattr(account, "id", "") or row.account_id)
            row.account_name = str(getattr(account, "name", "") or row.account_name)
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)
        await session.commit()


def runtime_fields(row: ImConnection) -> dict[str, Any]:
    """把库里的字段与解密后的密钥拼成适配器要的 ``fields``。

    接口层做「先核对再保存」时也用它。
    """
    platform = platform_of(row.platform)
    if platform is None:
        return {}
    fields = dict(row.fields or {})
    for item in platform.fields:
        if not item.secret:
            continue
        value = decrypt((row.secrets or {}).get(item.key))
        if value is not None:
            fields[item.key] = value
    return fields


def _has_secrets(row: ImConnection) -> bool:
    platform = platform_of(row.platform)
    if platform is None:
        return False
    for item in platform.fields:
        if item.secret and not (row.secrets or {}).get(item.key):
            return False
        value = str((row.fields or {}).get(item.key) or "").strip()
        if not item.secret and not item.options and not value:
            return False
    return True


async def _peer_state(session: AsyncSession, connection_id: str, sender_id: str) -> str | None:
    row = (
        await session.execute(
            select(ImPeer).where(
                ImPeer.connection_id == connection_id, ImPeer.sender_id == sender_id
            )
        )
    ).scalars().first()
    return row.state if row is not None else None


async def _ask_pairing(
    session: AsyncSession,
    connection: ImConnection,
    message: InboundMessage,
    state: str | None,
) -> None:
    """没配对的账号：记一条「谁想连接」并回一句，只回一次。"""
    platform = platform_of(connection.platform)
    if message.chat.kind == "group" and not message.mentioned:
        return  # 群里没 @ 机器人，就不打扰
    if state is None:
        session.add(
            ImPeer(
                connection_id=connection.id,
                sender_id=message.sender.id,
                sender_name=message.sender.name,
                state="pending",
                chat_key=message.chat.key,
                chat_kind=message.chat.kind,
                chat_title=message.chat.title,
            )
        )
        await session.commit()
        await _manager._safe_send(  # 同模块
            connection.id,
            message.chat,
            "已向 Coda 发出连接请求，请在电脑上对应智能体的「设定 → IM」标签里允许。"
            if platform is not None
            else "已发出连接请求，请先在设置里允许。",
        )
    elif state == "denied":
        # 同模块
        await _manager._safe_send(connection.id, message.chat, "这个账号的连接请求已被拒绝。")


def _text(key: str, platform: Platform | None) -> str:
    name = platform.name if platform is not None else "IM"
    return {
        "unbound": f"这个 {name} 机器人还没绑定智能体，请到对应智能体的「设定 → IM」标签里选一个。",
    }.get(key, key)


#: 全局唯一的连接管理器（应用生命周期里启动 / 停止）。
_manager = ConnectionManager()


def manager() -> ConnectionManager:
    return _manager


__all__ = [
    "ConnectionManager",
    "ImError",
    "manager",
    "runtime_fields",
]
