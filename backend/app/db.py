"""异步 SQLite 引擎与会话工具。"""
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from .settings import get_settings

#: SQLite 同一时刻只允许一个写者。轨迹采集 / 标题生成 / 运行落库与接口写入各用各的连接，
#: 撞上瞬时写锁很常见：给一个明确的等待窗口，比默认 5 秒后抛 "database is locked" 稳得多。
_SQLITE_BUSY_TIMEOUT_SECONDS = 30

_settings = get_settings()
engine = create_async_engine(
    _settings.sqlalchemy_url,
    echo=False,
    future=True,
    connect_args=(
        {"check_same_thread": False, "timeout": _SQLITE_BUSY_TIMEOUT_SECONDS}
        if _settings.sqlalchemy_url.startswith("sqlite")
        else {}
    ),
)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record) -> None:
    """SQLite 连接级配置：外键生效、WAL、提交成本与撞锁时的等待窗口。"""
    if _settings.sqlalchemy_url.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")  # 不开它，ON DELETE CASCADE 不生效
        cursor.execute("PRAGMA journal_mode=WAL")
        # WAL 下 NORMAL 已经足够：提交写进 WAL 就返回，崩溃只会丢最后几条已提交事务，
        # 库本身不会坏。默认的 FULL 每次提交都 fsync 主库，磁盘一慢（同步盘 / 容器 / 高 I/O）
        # 单次提交能拖到秒级，而 SQLite 只有一个写者 —— 这段时间里所有写入都在排队，
        # 排队超时就报 "database is locked"（实测高 I/O 下 10 个用例要 200 秒且必失败，
        # 换成 NORMAL 后 14 秒全绿）。
        cursor.execute("PRAGMA synchronous=NORMAL")
        # 毫秒单位；显式设置是为了让「等锁」的语义不受驱动默认值影响。
        cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_SECONDS * 1000}")
        cursor.close()


def _add_missing_columns(connection, columns_by_table: dict[str, dict[str, str]]) -> None:
    """给**已存在**的表补齐后加的可选列（幂等）。

    ``create_all`` 只会建缺失的表，不会 ``ALTER`` 旧表；而本地库含真实数据、禁止重建，
    因此"后加的可选列"登记在 ``models.ADDITIVE_COLUMNS``（对话域）与
    ``collab.tables.ADDITIVE_COLUMNS``（协作域），由这里安全地补上：
    带默认值、旧行留空，既不丢数据也不改约束。
    """
    for table, columns in columns_by_table.items():
        existing = {
            row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})").all()
        }
        if not existing:  # 表还不存在：create_all 已经按最新结构建好
            continue
        for name, ddl in columns.items():
            if name not in existing:
                connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


async def init_db() -> None:
    from . import models  # 注册表结构，并取用对话域的后加列声明
    from .collab import tables as collab_tables  # 协作域的表结构与后加列声明
    from .im import tables as im_tables  # IM 域的表结构与后加列声明

    additive_columns = {
        **models.ADDITIVE_COLUMNS,
        **collab_tables.ADDITIVE_COLUMNS,
        **im_tables.ADDITIVE_COLUMNS,
    }
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        if _settings.sqlalchemy_url.startswith("sqlite"):
            await conn.run_sync(_add_missing_columns, additive_columns)
            # 双轨去重键：老库补列后用模型的索引定义建一次（create_all 只对新表建索引）。
            # checkfirst=True 对已有的新表是 no-op；多个 NULL 的唯一索引在 SQLite 合法。
            index = next(
                (
                    idx
                    for idx in models.UsageRecord.__table__.indexes
                    if idx.name == "uq_usage_records_upstream_request_id"
                ),
                None,
            )
            if index is not None:
                await conn.run_sync(
                    lambda sync_conn: index.create(sync_conn, checkfirst=True)
                )


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
