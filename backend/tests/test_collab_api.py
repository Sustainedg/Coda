"""协作组接口：CRUD、校验端点、「有运行记录不能删」的 409，以及运行的图快照语义。"""
from __future__ import annotations

import httpx
import pytest_asyncio

from app.collab import graph as g
from app.collab.schemas import Duty, EdgeKind, EdgeSpec, GraphSpec, NodeSpec
from app.collab.tables import CollabRun, CollabTicket
from app.db import SessionLocal
from app.models import AgentProfile, Gateway


@pytest_asyncio.fixture
async def roles() -> dict[str, str]:
    """直接落库几个智能体，避免把协作用例绑死在「智能体」接口上。"""
    async with SessionLocal() as session:
        gateway = Gateway(name="测试网关", kind="custom", base_url="https://gw.example/v1")
        session.add(gateway)
        await session.commit()
        await session.refresh(gateway)
        created = {}
        for key, name in (("ceo", "CEO"), ("cto", "CTO"), ("se", "SE"), ("qa", "审查员")):
            role = AgentProfile(
                name=name,
                type_id="custom-cli",
                gateway_id=gateway.id,
                options={},
                system_prompt="",
            )
            session.add(role)
            await session.commit()
            await session.refresh(role)
            created[key] = role.id
        return created


def graph_body(roles: dict[str, str]) -> dict:
    spec = GraphSpec(
        nodes=[
            NodeSpec(key="ceo", agent_profile_id=roles["ceo"], label="CEO", duty=Duty.COORDINATOR),
            NodeSpec(key="se", agent_profile_id=roles["se"], label="SE", duty=Duty.ENGINEER),
            NodeSpec(key="qa", agent_profile_id=roles["qa"], label="审查员", duty=Duty.REVIEWER),
        ],
        edges=[
            EdgeSpec(from_key="ceo", to_key="se", kind=EdgeKind.ASSIGN),
            EdgeSpec(from_key="se", to_key="qa", kind=EdgeKind.REVIEW),
            EdgeSpec(from_key="qa", to_key="se", kind=EdgeKind.REWORK),
        ],
    )
    return g.to_storable(spec)


async def test_team_crud_and_validation(client: httpx.AsyncClient, roles):
    created = await client.post(
        "/api/collab/teams",
        json={"name": "客户端研发组", "entry_key": "ceo", "graph": graph_body(roles)},
    )
    assert created.status_code == 201, created.text
    team = created.json()
    assert team["node_count"] == 3
    assert team["validation"]["ok"] is True, team["validation"]
    assert team["budget"]["max_rounds_per_ticket"] == 3  # 未指定时用默认预算
    assert team["id"]

    listed = await client.get("/api/collab/teams")
    assert listed.status_code == 200
    assert [item["name"] for item in listed.json()["items"]] == ["客户端研发组"]

    detail = await client.get(f"/api/collab/teams/{team['id']}")
    assert detail.status_code == 200
    assert detail.json()["entry_key"] == "ceo"

    # 图不合法也能存草稿（编辑器要能边画边存），但要把问题报出来
    broken = graph_body(roles)
    broken["edges"] = [e for e in broken["edges"] if e["kind"] != "rework"]
    saved = await client.put(f"/api/collab/teams/{team['id']}", json={"graph": broken})
    assert saved.status_code == 200, saved.text
    codes = {issue["code"] for issue in saved.json()["validation"]["errors"]}
    assert "edge.review_without_rework" in codes

    # 只校验不保存：传一份合法图应恢复 ok，且不影响库里那份
    checked = await client.post(
        f"/api/collab/teams/{team['id']}/validate",
        json={"entry_key": "ceo", "graph": graph_body(roles)},
    )
    assert checked.status_code == 200 and checked.json()["ok"] is True
    still_broken = await client.get(f"/api/collab/teams/{team['id']}")
    assert still_broken.json()["validation"]["ok"] is False

    copied = await client.post(f"/api/collab/teams/{team['id']}/duplicate")
    assert copied.status_code == 201
    assert copied.json()["name"] == "客户端研发组（副本）"

    deleted = await client.delete(f"/api/collab/teams/{copied.json()['id']}")
    assert deleted.status_code == 204


async def test_unknown_team_is_404(client: httpx.AsyncClient):
    assert (await client.get("/api/collab/teams/nope")).status_code == 404
    assert (await client.put("/api/collab/teams/nope", json={"name": "x"})).status_code == 404
    assert (await client.delete("/api/collab/teams/nope")).status_code == 404
    assert (await client.post("/api/collab/teams/nope/duplicate")).status_code == 404


async def test_structurally_invalid_graph_is_422(client: httpx.AsyncClient):
    response = await client.post(
        "/api/collab/teams",
        json={"name": "坏图", "graph": {"nodes": [{"key": "ceo", "agent_profile_id": "r1", "duty": "老板"}]}},
    )
    assert response.status_code == 422


async def test_team_with_runs_cannot_be_deleted(client: httpx.AsyncClient, roles):
    created = await client.post("/api/collab/teams", json={"name": "有运行的组"})
    team_id = created.json()["id"]
    async with SessionLocal() as session:
        session.add(CollabRun(team_id=team_id, title="跑过一次", goal="目标"))
        await session.commit()

    response = await client.delete(f"/api/collab/teams/{team_id}")
    assert response.status_code == 409
    assert "运行记录" in response.json()["detail"]


async def test_editing_a_key_used_by_a_run_is_saved_and_run_keeps_its_graph(
    client: httpx.AsyncClient, roles
):
    """改节点 key 不再被拦：下过工单的运行已经冻结了当时的图，靠自己的快照继续跑。

    以前的 409 是怕「改名后老工单找不到岗位」；现在老运行有独立快照，
    保存照常成功，存量运行与工单都不受影响。
    """
    created = await client.post(
        "/api/collab/teams",
        json={"name": "会改名的组", "entry_key": "ceo", "graph": graph_body(roles)},
    )
    team_id = created.json()["id"]
    async with SessionLocal() as session:
        # 模拟升级前的老运行：直接落一行，没有图快照
        run = CollabRun(team_id=team_id, title="跑过一次", goal="目标")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id
        session.add(
            CollabTicket(
                run_id=run.id,
                to_agent_profile_id=roles["se"],
                owner_key="ceo",
                holder_key="se",
                title="干活",
            )
        )
        await session.commit()

    renamed = graph_body(roles)
    renamed["nodes"][1]["key"] = "sde"  # se → sde
    response = await client.put(f"/api/collab/teams/{team_id}", json={"graph": renamed})
    assert response.status_code == 200, response.text
    stored = await client.get(f"/api/collab/teams/{team_id}")
    assert {node["key"] for node in stored.json()["graph"]["nodes"]} == {"ceo", "sde", "qa"}

    # 保存前先把「改之前」的图拍给了老运行：工单里的 se 仍能找到岗位
    async with SessionLocal() as session:
        stored_run = await session.get(CollabRun, run_id)
        assert {
            node["key"] for node in (stored_run.graph or {}).get("nodes", [])
        } == {"ceo", "se", "qa"}


async def test_unstarted_run_follows_the_team_graph(client: httpx.AsyncClient, roles):
    """还没下过工单的运行跟着协作组的最新图：先建任务、再回去修图也能救回来。"""
    created = await client.post(
        "/api/collab/teams",
        json={"name": "还在改图的组", "entry_key": "ceo", "graph": graph_body(roles)},
    )
    team_id = created.json()["id"]
    first = (await client.post("/api/collab/runs", json={"team_id": team_id})).json()

    renamed = graph_body(roles)
    renamed["nodes"][1]["key"] = "sde"
    assert (await client.put(f"/api/collab/teams/{team_id}", json={"graph": renamed})).status_code == 200

    # 没开跑就没有快照，详情展示协作组的最新图
    first_detail = (await client.get(f"/api/collab/runs/{first['id']}")).json()
    assert "sde" in {node["key"] for node in first_detail["graph"]["nodes"]}

    second = (await client.post("/api/collab/runs", json={"team_id": team_id})).json()
    second_detail = (await client.get(f"/api/collab/runs/{second['id']}")).json()
    assert "sde" in {node["key"] for node in second_detail["graph"]["nodes"]}


async def test_backfill_run_graphs_pins_only_started_legacy_runs(client: httpx.AsyncClient, roles):
    """升级前没有快照的运行：只有已经下过工单的才回填冻结，重复执行幂等。"""
    from app.collab import store as collab_store

    created = await client.post(
        "/api/collab/teams",
        json={"name": "要回填的组", "entry_key": "ceo", "graph": graph_body(roles)},
    )
    team_id = created.json()["id"]
    async with SessionLocal() as session:
        started = CollabRun(team_id=team_id, title="下过工单的老运行", goal="目标")
        waiting = CollabRun(team_id=team_id, title="还没开跑的老运行", goal="")
        session.add(started)
        session.add(waiting)
        await session.commit()
        await session.refresh(started)
        await session.refresh(waiting)
        session.add(
            CollabTicket(
                run_id=started.id,
                to_agent_profile_id=roles["se"],
                owner_key="ceo",
                holder_key="se",
                title="干活",
            )
        )
        await session.commit()

        assert await collab_store.backfill_run_graphs(session) == 1
        await session.refresh(started)
        await session.refresh(waiting)
        assert {node["key"] for node in (started.graph or {}).get("nodes", [])} == {"ceo", "se", "qa"}
        assert waiting.graph is None, "还没开跑的老运行不应被冻结"
        assert await collab_store.backfill_run_graphs(session) == 0


async def test_deleted_role_is_reported_as_validation_error(client: httpx.AsyncClient, roles):
    body = {"name": "智能体被删的组", "entry_key": "ceo", "graph": graph_body(roles)}
    body["graph"]["nodes"][1]["agent_profile_id"] = "r-deleted"
    created = await client.post("/api/collab/teams", json=body)
    codes = {issue["code"] for issue in created.json()["validation"]["errors"]}
    assert "node.agent_missing" in codes


async def test_active_runs_lists_collab_agents_with_the_run_title(
    client: httpx.AsyncClient, roles
):
    """「正在运行的智能体」要能查到协作里跑着的岗位（对话域那张运行表看不到它们）。

    协作的运行登记在协作域自己的句柄表里（工单步骤 `registry` / 入口对话 `chat_registry`），
    所以这里由协作域给一份汇总，并补上运行标题 —— 界面上要能认得出是哪次协作。
    """
    from app.collab import execution

    created = await client.post(
        "/api/collab/teams",
        json={"name": "在跑的组", "entry_key": "ceo", "graph": graph_body(roles)},
    )
    run = await client.post(
        "/api/collab/runs", json={"team_id": created.json()["id"], "title": "登录页改造"}
    )
    run_id = run.json()["id"]

    execution.registry.register(run_id, "step-1", "SE", "se")
    execution.chat_registry.register(run_id, "chat:msg-1", "CEO", "ceo")
    try:
        items = (await client.get("/api/collab/active-runs")).json()["items"]
        by_agent = {item["agent_name"]: item for item in items}
        assert by_agent["SE"]["kind"] == "step"
        assert by_agent["SE"]["node_key"] == "se"
        assert by_agent["SE"]["run_id"] == run_id
        assert by_agent["SE"]["title"] == "登录页改造"
        assert by_agent["CEO"]["kind"] == "chat"
        assert by_agent["CEO"]["elapsed_seconds"] >= 0
    finally:
        # 句柄表是进程级的：用例收尾必须清干净，否则会漏进别的用例。
        execution.registry.finish("step-1")
        execution.chat_registry.finish("chat:msg-1")

    after = (await client.get("/api/collab/active-runs")).json()["items"]
    assert all(item["agent_name"] not in {"SE", "CEO"} for item in after)
