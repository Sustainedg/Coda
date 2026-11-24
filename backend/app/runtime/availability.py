"""运行时可用性（装没装、是什么版本）的缓存与定时检测。

「某个内核装没装」要跑一次 ``内核 --version`` 才知道：claude / codex 只要十几毫秒，
但 Node 系 CLI（codex 等）冷启动接近 2 秒。后端是**单进程单事件循环**，把这种同步子进程
放在请求路径上会把整站所有接口一起冻住 —— 实测开网页时并发的读数请求被拖到 20 秒。

所以这里采用与技能编目同构的「编目 + 定时刷新」，请求路径永不探测：

* 检测结果落在 ``runtime_status`` 表，接口只读表；
* 读到缺行 / 过期的类型只往内存集合里登记一次，由 ``scheduler()`` 消费补测 ——
  请求既不等待探测，也不会每次新建一个后台任务；
* 表里缺行时用 ``shutil.which`` 兜底（微秒级，只判断装没装），版本等后台补齐；
* 每天 ``DAILY_CHECK_HOUR`` 点（东八区）全量重测一次；
* 服务启动时补测过期 / 缺失的类型 —— 服务重启频繁，只靠定点任务容易整天漏掉；
* 安装 / 更新 / 卸载后立即重测（``runtime/provisioning.py``），界面另有「重新检测」入口。

探测本身在**工作线程**里执行（``asyncio.to_thread``），即使耗时也不占用事件循环。
可用性只是展示用的元数据：一次运行并不依赖它（见 ``runtime/service.prepare_run``），
因此读到旧值最多是界面上版本号晚几小时，不会有静默的行为差异。
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlmodel import select

from ..agents import AgentDefinition, all_agents, get_agent
from ..agents.base import Availability
from ..db import SessionLocal
from ..models import RuntimeStatus

logger = logging.getLogger("coda")

#: 检测结果的有效期：读表时超过它就在后台补测（本次请求仍返回旧值，绝不阻塞）。
CHECK_INTERVAL = timedelta(hours=24)

#: 每天全量重测的时刻（24 小时制的整点）。时区写死东八区，不依赖容器的 localtime。
DAILY_CHECK_HOUR = 7
#: Windows 不自带 IANA 时区库，``ZoneInfo`` 会抛 ZoneInfoNotFoundError。
#: 中国没有夏令时，固定 +08:00 与 Asia/Shanghai **完全等价**，无需额外依赖。
try:
    LOCAL_TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:  # pragma: no cover - 取决于运行环境的时区库
    LOCAL_TZ = timezone(timedelta(hours=8))

#: 调度器查看「有没有排队的补测」的间隔（秒）。请求路径只往 _pending 里登记，
#: 真正的探测由 scheduler 消费 —— 所以请求永远不会等探测，也不会每次建一个后台任务。
WATCH_INTERVAL = 5.0

#: 已登记、等待补测的类型（进程内，无需持久化）。
_pending: set[str] = set()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    """把时间统一成带 UTC 时区的 ``datetime``（``None`` 原样返回）。

    正常情况下 ``models.UTCDateTime`` 出库时已经补好了 ``tzinfo``，这里只是**幂等兜底**：
    老版本写进库的裸时间、或测试里手工构造的 naive 值，也都按 UTC 解释，绝不按本地时区。
    不补的话 ``isoformat()`` 会省掉偏移量，浏览器按本地时间解析，界面上的
    「检测于 …」会整体偏移一个时区。
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def is_stale(row: RuntimeStatus) -> bool:
    """这条检测结果是否已经过期（没有时间戳也算过期）。"""
    checked = _as_utc(row.checked_at)
    return checked is None or _now() - checked >= CHECK_INTERVAL


def cheap_availability(definition: AgentDefinition) -> Availability:
    """没有缓存时的兜底判断：只查 PATH（微秒级），不跑 ``--version``。

    这不复制适配器的判断逻辑：进程内运行的适配器不声明 ``executable``，这里同样按
    「无需本地可执行文件」处理；声明了 ``executable`` 的 CLI 内核才去 PATH 里找。
    版本号留空，等后台补测填上。
    """
    if not definition.executable:
        return Availability(True, detail="进程内运行，无需本地可执行文件")
    path = shutil.which(definition.executable)
    if path is None:
        return Availability(False, detail=f"未在 PATH 中找到 `{definition.executable}`")
    return Availability(True, detail=path)


async def probe(definition: AgentDefinition) -> Availability | None:
    """真实探测一次（不写缓存）：同步的 ``availability()`` 丢进线程执行。

    探测本身出错时返回 ``None``，由调用方决定怎么处理 —— 绝不能因此把缓存里
    上一次的结论改坏（否则一次抖动就会让界面显示「未安装」）。
    """
    try:
        availability = await asyncio.to_thread(definition.availability)
    except Exception as exc:  # noqa: BLE001 - 探测失败不能影响服务
        logger.warning("探测运行时 %s 失败：%s", definition.id, exc)
        return None
    return Availability(
        installed=availability.installed,
        version=availability.version,
        detail=availability.detail,
        checked_at=_now(),
    )


async def _load_rows() -> dict[str, RuntimeStatus]:
    async with SessionLocal() as session:
        rows = (await session.execute(select(RuntimeStatus))).scalars().all()
    return {row.type_id: row for row in rows}


def _from_row(row: RuntimeStatus | None, definition: AgentDefinition) -> Availability:
    if row is None:
        return cheap_availability(definition)
    return Availability(row.installed, row.version, row.detail, _as_utc(row.checked_at))


async def statuses(definitions: Sequence[AgentDefinition]) -> dict[str, Availability]:
    """读缓存：一次查询拿到这批适配器的可用性。

    缺行或过期的类型**只排进后台补测**，本次调用立刻返回兜底值 / 旧值 ——
    请求路径绝不等待探测（这正是「打开网站慢」的根因）。
    """
    rows = await _load_rows()
    result: dict[str, Availability] = {}
    pending: list[str] = []
    for definition in definitions:
        row = rows.get(definition.id)
        result[definition.id] = _from_row(row, definition)
        if row is None or is_stale(row):
            pending.append(definition.id)
    if pending:
        enqueue(pending)
    return result


async def for_types(type_ids: Iterable[str]) -> dict[str, Availability]:
    """按 ``type_id`` 取可用性；未注册的类型不会出现在结果里。"""
    definitions = []
    for type_id in dict.fromkeys(type_ids):
        try:
            definitions.append(get_agent(type_id))
        except KeyError:
            continue
    return await statuses(definitions)


def enqueue(type_ids: Iterable[str]) -> None:
    """把待补测的类型登记进内存集合，交给 ``scheduler`` 稍后处理。

    请求路径专用：这里只写一个 set —— 不建后台任务、不碰数据库、不等待任何 I/O，
    这样一次请求的耗时与「探测要跑多久」彻底无关。
    """
    _pending.update(type_ids)


def take_pending() -> list[str]:
    """取走（并清空）当前排队的类型，供调度器补测。"""
    pending = sorted(_pending)
    _pending.clear()
    return pending


async def refresh(
    type_ids: Iterable[str] | None = None, *, only_stale: bool = False
) -> dict[str, int]:
    """真实探测并写入缓存表。

    ``type_ids`` 省略表示全部内置适配器；``only_stale=True`` 只补测缺行 / 过期的类型
    （启动补齐用它，这样频繁重启也不会每次都全量重测）。
    """
    known = {definition.id: definition for definition in all_agents()}
    wanted = list(dict.fromkeys(type_ids)) if type_ids is not None else list(known)
    unknown = [type_id for type_id in wanted if type_id not in known]
    if unknown:
        logger.warning("跳过未注册的运行时：%s", "、".join(unknown))

    checked = skipped = failed = 0
    async with SessionLocal() as session:
        stored = await session.execute(select(RuntimeStatus))
        rows = {row.type_id: row for row in stored.scalars().all()}
        for type_id in wanted:
            definition = known.get(type_id)
            if definition is None:
                continue
            row = rows.get(type_id)
            if only_stale and row is not None and not is_stale(row):
                skipped += 1
                continue
            availability = await probe(definition)
            if availability is None:
                failed += 1
                if row is None:
                    continue
                # 探测出错：保留上一次的结论，只把时间戳推后，避免每次读表都重排一次。
                row.checked_at = _now()
                continue
            if row is None:
                row = RuntimeStatus(type_id=type_id)
                session.add(row)
                rows[type_id] = row
            row.installed = availability.installed
            row.version = availability.version
            row.detail = availability.detail
            row.checked_at = availability.checked_at
            checked += 1
        await session.commit()
    if checked or failed:
        logger.info("运行时检测完成：更新 %s、跳过 %s、失败 %s", checked, skipped, failed)
    return {"checked": checked, "skipped": skipped, "failed": failed}


async def refresh_type(type_id: str) -> Availability:
    """强制重测一个类型并返回最新结果；未注册的类型抛 ``KeyError``（由接口层转 404）。"""
    definition = get_agent(type_id)
    await refresh([type_id])
    return _from_row((await _load_rows()).get(type_id), definition)


async def refresh_definition(definition: AgentDefinition) -> Availability:
    """重测一个具体定义，并尽力把结果写回缓存。

    正常路径就是 ``refresh_type(definition.id)``；适配器也可能不在注册表里
    （测试替身、或运行时动态构造的定义），这时退回「只探测、不写缓存」。
    """
    try:
        return await refresh_type(definition.id)
    except KeyError:
        return await probe(definition) or cheap_availability(definition)


def seconds_until_daily(now: datetime | None = None) -> float:
    """距离下一个「每天定点检测」还有多少秒（东八区）。"""
    current = now or datetime.now(LOCAL_TZ)
    next_run = current.replace(hour=DAILY_CHECK_HOUR, minute=0, second=0, microsecond=0)
    if next_run <= current:
        next_run += timedelta(days=1)
    return (next_run - current).total_seconds()


async def scheduler() -> None:
    """后台任务：启动补齐 → 循环「消费排队的补测 + 每天定点全量重测」。

    由 ``main.lifespan`` 启动、随服务关闭被取消，因此进程里只有这一个自循环任务，
    没有「每个请求建一个任务」的孤儿任务问题。
    """
    try:
        await refresh(only_stale=True)
    except Exception:  # noqa: BLE001 - 后台任务绝不能因为一次失败就消失
        logger.warning("启动补齐运行时状态失败，改为等待下一次检测", exc_info=True)

    next_daily = time.monotonic() + seconds_until_daily()
    while True:
        await asyncio.sleep(WATCH_INTERVAL)
        pending = take_pending()
        if pending:
            await _refresh_quietly(pending)
        if time.monotonic() >= next_daily:
            next_daily = time.monotonic() + seconds_until_daily()
            await _refresh_quietly(None)


async def _refresh_quietly(type_ids: Iterable[str] | None) -> None:
    """后台检测：失败只记日志，下一次读表会重新排队。"""
    try:
        await refresh(type_ids)
    except Exception:  # noqa: BLE001
        logger.warning("后台检测运行时状态失败，稍后会自动重试", exc_info=True)
