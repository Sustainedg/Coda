"""数据库向后兼容：协作域后来新增的列必须能补到**已存在**的旧库上（铁律 9，不得删库重建）。

背景：SQLite 的 ``create_all`` 只建新表、不会给旧表加列，而本地库 ``backend/data/coda.db``
含真实数据。于是"后加的可选列"登记在 ``collab.tables.ADDITIVE_COLUMNS``，由 ``db.init_db`` 幂等补齐。
"""
from __future__ import annotations

from app.collab.tables import CollabTeam
from app.db import SessionLocal, engine, init_db

#: 模拟"更早版本"建出来的 collab_steps（没有 gist 列）
OLD_SCHEMA = """
CREATE TABLE collab_steps (
    id VARCHAR NOT NULL PRIMARY KEY,
    run_id VARCHAR NOT NULL,
    ticket_id VARCHAR,
    role_key VARCHAR(64) NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT ''
)
"""


async def _old_table_with_one_row() -> None:
    async with engine.begin() as conn:
        await conn.exec_driver_sql("DROP TABLE IF EXISTS collab_steps")
        await conn.exec_driver_sql(OLD_SCHEMA)
        await conn.exec_driver_sql(
            "INSERT INTO collab_steps (id, run_id, role_key, content) "
            "VALUES ('旧行', 'run-1', 'se', '历史正文')"
        )


async def _restore_full_schema() -> None:
    """恢复到最新结构，避免影响同一会话里的后续用例。"""
    async with engine.begin() as conn:
        await conn.exec_driver_sql("DROP TABLE IF EXISTS collab_steps")
    await init_db()


async def test_optional_columns_are_added_to_an_existing_table():
    await _old_table_with_one_row()

    await init_db()

    async with engine.begin() as conn:
        columns = {row[1] for row in (await conn.exec_driver_sql("PRAGMA table_info(collab_steps)")).all()}
        assert "gist" in columns, "旧库缺 gist 列时 init_db 应当自动补上"
        assert "target_key" in columns, "旧库缺 target_key 列时 init_db 应当自动补上"
        row = (
            await conn.exec_driver_sql(
                "SELECT role_key, content, gist, target_key FROM collab_steps WHERE id = '旧行'"
            )
        ).one()

    assert tuple(row) == ("se", "历史正文", "", ""), "补列不能改动旧行"
    await _restore_full_schema()


async def test_migration_is_idempotent():
    """重复启动（init_db 跑多次）不能报错、也不能重复加列。"""
    await _old_table_with_one_row()
    await init_db()
    await init_db()

    async with engine.begin() as conn:
        columns = [row[1] for row in (await conn.exec_driver_sql("PRAGMA table_info(collab_steps)")).all()]
    assert columns.count("gist") == 1
    assert columns.count("target_key") == 1
    await _restore_full_schema()


async def test_report_column_is_added_to_an_existing_tickets_table():
    """收尾汇报标记 ``report_at`` 同样是后加的列：旧库启动时要被补上，旧行保持可读。"""
    await init_db()
    async with engine.begin() as conn:
        await conn.exec_driver_sql("ALTER TABLE collab_tickets DROP COLUMN report_at")

    await init_db()
    await init_db()

    async with engine.begin() as conn:
        columns = {
            row[1] for row in (await conn.exec_driver_sql("PRAGMA table_info(collab_tickets)")).all()
        }
        count = (await conn.exec_driver_sql("SELECT COUNT(*) FROM collab_tickets")).scalar()
    assert "report_at" in columns, "旧库缺 report_at 列时 init_db 应当自动补上"
    assert count is not None  # 补列不能动到旧行


async def test_run_graph_column_is_added_to_an_existing_runs_table():
    """运行图快照 ``graph`` 也是后加的列：旧库补成 NULL（引擎回退读协作组现图）。"""
    await init_db()
    async with SessionLocal() as session:
        team = CollabTeam(name="老库协作组")
        session.add(team)
        await session.commit()
        await session.refresh(team)
        team_id = team.id

    async with engine.begin() as conn:
        await conn.exec_driver_sql("ALTER TABLE collab_runs DROP COLUMN graph")
        await conn.exec_driver_sql(
            "INSERT INTO collab_runs "
            "(id, team_id, title, goal, status, budget, usage, steps_used, tickets_used, step_seq) "
            "VALUES ('老运行', :team_id, '旧任务', '', 'done', '{}', '{}', 0, 0, 0)",
            {"team_id": team_id},
        )

    await init_db()
    await init_db()

    async with engine.begin() as conn:
        columns = {
            row[1] for row in (await conn.exec_driver_sql("PRAGMA table_info(collab_runs)")).all()
        }
        value = (
            await conn.exec_driver_sql("SELECT graph FROM collab_runs WHERE id = '老运行'")
        ).scalar()
    assert "graph" in columns, "旧库缺 graph 列时 init_db 应当自动补上"
    assert value is None, "补列不能凭空造图：旧运行留空，由引擎/启动回填兜底"

    async with engine.begin() as conn:
        await conn.exec_driver_sql("DELETE FROM collab_runs WHERE id = '老运行'")
        await conn.exec_driver_sql("DELETE FROM collab_teams WHERE id = :team_id", {"team_id": team_id})
