"""测试夹具：每个用例都从干净的数据库开始。"""
from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from app.db import engine, init_db
from app.main import app
from app.models import SQLModel


@pytest_asyncio.fixture
async def client():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _isolated_skill_libraries(tmp_path, monkeypatch):
    """每个用例一份独立的用户技能库目录。

    技能库是平台数据目录里的真实文件，跨用例复用会让「导入的技能还在」干扰下一次
    扫描与同名判断。这里只替换 ``runtime.skills`` 看到的 ``skill_libraries_root``，
    其余配置（智能体隔离目录等）沿用真实设置。
    """
    from types import SimpleNamespace

    import app.runtime.skills as skill_service

    real = skill_service.get_settings()
    stub = SimpleNamespace(
        skill_libraries_root=(tmp_path / "skill-libraries").resolve(),
        agent_homes_root=real.agent_homes_root,
    )
    monkeypatch.setattr(skill_service, "get_settings", lambda: stub)


@pytest_asyncio.fixture(autouse=True)
async def _drain_title_tasks():
    """每个用例收尾时取消还在跑的「自动起标题」后台任务。

    发一条消息就会丢一个后台任务出去（见 `runtime/titles.py`）：它要起一次真实内核。
    任务跨用例存活会占着数据库与子进程，把下一个用例的建库 / 删表堵成 database is locked。
    """
    yield
    from app.runtime import titles

    await titles.cancel_pending()


@pytest_asyncio.fixture(autouse=True)
async def _drain_collab_handles():
    """每个用例收尾时清空协作域的进程级句柄表。

    工单步骤与入口对话各有一张「正在跑」的句柄表（`collab/execution.py` 的
    `registry` / `chat_registry`）。用例里手工 `register` 的句柄如果忘了 `finish`，
    会泄漏进后面的用例 —— 「正在运行的智能体」会把幽灵岗位也列出来，
    `test_collab_api.py` 的收尾断言（列表应为空）就挂了。
    """
    yield
    from app.collab import execution

    execution.registry.clear()
    execution.chat_registry.clear()
