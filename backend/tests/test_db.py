"""SQLite 连接级配置：撞上并发写锁时要「排队等」，不能瞬间失败。

同一进程里有多条独立连接（消息落库 / 轨迹采集 / 标题生成），SQLite 同一时刻只允许
一个写者。默认 5 秒等待窗口在高负载下不够用，接口写入会直接抛 "database is locked"；
``db.py`` 因此把等待窗口放宽到 30 秒。
"""
from __future__ import annotations

import pytest

from app import db


@pytest.mark.asyncio
async def test_main_engine_waits_for_a_busy_database():
    """主引擎必须设好 busy_timeout / WAL / NORMAL。

    ``synchronous=NORMAL`` 不是可有可无的优化：默认的 FULL 在慢盘上会把单次提交拖到
    秒级，而 SQLite 只有一个写者，其它写入只能排队直到超时（database is locked）。
    """
    async with db.engine.connect() as conn:
        busy_timeout = (await conn.exec_driver_sql("PRAGMA busy_timeout")).scalar()
        journal_mode = (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar()
        foreign_keys = (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar()
        synchronous = (await conn.exec_driver_sql("PRAGMA synchronous")).scalar()

    assert busy_timeout is not None and int(busy_timeout) >= db._SQLITE_BUSY_TIMEOUT_SECONDS * 1000
    assert str(journal_mode).lower() == "wal"
    assert int(foreign_keys) == 1
    # 0=OFF / 1=NORMAL / 2=FULL：WAL 下要 NORMAL。
    assert int(synchronous) == 1
