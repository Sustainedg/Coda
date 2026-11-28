"""波次并行调度：兄弟工单同时推进、同岗位串行、串行路径完全不回归。

这些用例守住三件产品承诺（docs/计划任务/collaboration-design.md §3.4）：

* ``max_parallel > 1`` 时，同一父工单拆出、持有者互不相同的兄弟工单**真的同时在跑**
  （用替身智能体的 gate 闸门卡住两张，验证它们都处于 streaming）；
* 同一岗位拿到的两张工单**不会**并行（一个岗位一个对话通道）；
* ``max_parallel=1``（默认）时行为与过去完全一致——既有 32 个引擎用例就是回归线。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from sqlmodel import select

from app.collab import graph as graph_service
from app.collab.schemas import Duty, EdgeKind, EdgeSpec, GraphSpec, NodeSpec
from app.collab.tables import CollabStep
from app.db import SessionLocal
from helpers import collab_options

from test_collab_guards import create_team, make_env, read_sse, split

#: 「领导者 + 两个平级实施者」：CEO 把两张子工单都派给 CTO，CTT 再拆给 se1 / se2。
#: se1 / se2 是平级岗位，两张子工单互不依赖——这正是并行的目标场景。
def fanout_graph(roles: dict[str, str]) -> GraphSpec:
    return GraphSpec(
        nodes=[
            NodeSpec(key="ceo", agent_profile_id=roles["ceo"], label="CEO", duty=Duty.COORDINATOR),
            NodeSpec(key="cto", agent_profile_id=roles["cto"], label="CTO", duty=Duty.COORDINATOR),
            NodeSpec(key="se1", agent_profile_id=roles["se1"], label="实施者A", duty=Duty.ENGINEER),
            NodeSpec(key="se2", agent_profile_id=roles["se2"], label="实施者B", duty=Duty.ENGINEER),
        ],
        edges=[
            EdgeSpec(from_key="ceo", to_key="cto", kind=EdgeKind.ASSIGN),
            EdgeSpec(from_key="cto", to_key="se1", kind=EdgeKind.ASSIGN),
            EdgeSpec(from_key="cto", to_key="se2", kind=EdgeKind.ASSIGN),
        ],
    )


ROLE_NAMES = (("ceo", "CEO"), ("cto", "CTO"), ("se1", "实施者A"), ("se2", "实施者B"))


async def make_fanout_env(script: dict, tmp_path: Path) -> dict:
    """建「领导者 + 两个平级实施者」的替身环境。"""
    script_path = tmp_path / "script.json"
    script_path.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    from app.models import AgentProfile

    roles: dict[str, str] = {}
    async with SessionLocal() as session:
        for key, name in ROLE_NAMES:
            role = AgentProfile(
                name=name,
                avatar="🧑",
                system_prompt=f"你是 {name}。",
                type_id="custom-cli",
                options=collab_options(script_path, state_dir),
            )
            session.add(role)
            await session.commit()
            await session.refresh(role)
            roles[key] = role.id
    return {"agent_profiles": roles, "state_dir": state_dir}


#: CTO 一次 split 拆两张子工单，每张自带 assignee（分给两个平级岗位并行）
SPLIT_TWO = {
    "action": "delegate",
    "assignee": "se1",
    "mode": "split",
    "tickets": [
        {"title": "实现主题开关", "assignee": "se1", "goal": "做开关", "acceptance": ["能切换"]},
        {"title": "实现持久化", "assignee": "se2", "goal": "做持久化", "acceptance": ["刷新保持"]},
    ],
}


async def _start_in_background(
    client: httpx.AsyncClient, team_id: str, goal: str
) -> tuple[str, asyncio.Task[None]]:
    """在后台任务里启动推进（gate 会把指定岗位卡在运行中）。"""
    run = await client.post("/api/collab/runs", json={"team_id": team_id})
    assert run.status_code == 201, run.text
    run_id = run.json()["id"]

    async def drive() -> None:
        async with client.stream(
            "POST", f"/api/collab/runs/{run_id}/messages", json={"content": goal}
        ) as response:
            assert response.status_code == 200, response.text
            async for _ in response.aiter_lines():
                pass

    return run_id, asyncio.create_task(drive())


async def _wait_until(predicate, *, timeout: float = 15.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if predicate():
            return True
        if loop.time() >= deadline:
            return predicate()
        await asyncio.sleep(0.02)


def _prompts(state_dir: Path) -> list[str]:
    path = state_dir / "prompts.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)["prompt"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


import re

POST_LINE = re.compile(r"\[协作任务\][^\n]*你的岗位：[^（\n]*（(?P<key>[^）\n]+)）")


def _prompts_of(all_prompts: list[str], key: str) -> list[str]:
    return [
        text
        for text in all_prompts
        if (match := POST_LINE.search(text)) and match.group("key") == key
    ]


async def test_sibling_tickets_run_concurrently_with_max_parallel(
    client: httpx.AsyncClient, tmp_path: Path
):
    """一次 split 拆两张、持有者不同 → 两步同时处于 streaming。

    用 gate 把 se1 / se2 都卡住：若调度串行，se2 的 prompt 永远不会出现
    （要等 se1 放行后才开始）；若并行，两个 prompt 都已写入、两张工单都在跑。
    """
    script = {
        "ceo": [
            {"action": "delegate", "assignee": "cto", "mode": "handoff", "ticket": {"title": "改造登录页"}},
            {"action": "done", "remark": "已交付：开关与持久化都完成了"},
        ],
        "cto": [SPLIT_TWO, {"action": "done", "remark": "两张子工单都已交付"}],
        "se1": [{"action": "submit", "summary": "开关完成"}],
        "se2": [{"action": "submit", "summary": "持久化完成"}],
    }
    env = await make_fanout_env(script, tmp_path)
    budget = {
        "max_steps": 30,
        "max_tickets": 10,
        "max_rounds_per_ticket": 2,
        "max_tokens": 0,
        "timeout_seconds": 600,
        "max_parallel": 2,
    }
    team = await create_team(client, fanout_graph(env["agent_profiles"]), budget=budget)

    # 两张 gate 都在：se1 与 se2 都会被卡在运行中
    for key in ("se1", "se2"):
        (env["state_dir"] / f"gate-{key}").write_text("wait", encoding="utf-8")

    run_id, drive_task = await _start_in_background(client, team["id"], "把登录页改成暗色主题")
    try:
        # 两个岗位都进入了执行（prompt 已写入）——串行调度下 se2 不可能出现
        assert await _wait_until(
            lambda: bool(_prompts_of(_prompts(env["state_dir"]), "se1"))
            and bool(_prompts_of(_prompts(env["state_dir"]), "se2"))
        ), "se1 与 se2 没有同时进入执行"

        # 两张工单同时处于 working（并行推进的直接证据）
        async with SessionLocal() as session:
            steps = (
                await session.execute(
                    select(CollabStep).where(CollabStep.run_id == run_id).order_by(CollabStep.sequence)
                )
            ).scalars().all()
            streaming = [s for s in steps if s.status == "streaming"]
            assert {s.role_key for s in streaming} == {"se1", "se2"}, (
                f"并行波次里应当只有 se1/se2 在跑，实际：{[(s.role_key, s.status) for s in steps]}"
            )
    finally:
        for key in ("se1", "se2"):
            (env["state_dir"] / f"gate-{key}").unlink(missing_ok=True)
        await asyncio.wait_for(drive_task, timeout=30)

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "done", detail.get("error")
    # 两张子工单都成立并收尾
    titles = {t["title"] for t in detail["tickets"] if t["parent_ticket_id"]}
    assert titles == {"实现主题开关", "实现持久化"}
    assert all(t["state"] == "done" for t in detail["tickets"] if t["parent_ticket_id"])
    # 智能体步数（user 不计）：ceo派工 + cto拆单 + se1 + se2 + cto汇总 + ceo汇报 = 6
    assert detail["steps_used"] == 6, detail["steps_used"]
    # 用量聚合同样完整：6 步 × (10, 5)，一步都不能少（并行分支的账也要记全）
    assert detail["usage"]["input_tokens"] == 60 and detail["usage"]["output_tokens"] == 30
    # 序号不撞车：7 条时间线的 sequence 恰好是 1..7 的排列（并行取号也不重不漏）
    seqs = sorted(s["sequence"] for s in detail["steps"])
    assert seqs == list(range(1, len(seqs) + 1)), seqs


async def test_same_holder_tickets_never_run_in_parallel(
    client: httpx.AsyncClient, tmp_path: Path
):
    """两张子工单派给**同一个**岗位 → 仍然串行（一个岗位一个对话通道）。"""
    split_same_assignee = {
        "action": "delegate",
        "assignee": "se1",
        "mode": "split",
        "tickets": [
            {"title": "实现主题开关", "goal": "做开关", "acceptance": ["能切换"]},
            {"title": "实现持久化", "goal": "做持久化", "acceptance": ["刷新保持"]},
        ],
    }
    script = {
        "ceo": [
            {"action": "delegate", "assignee": "cto", "mode": "handoff", "ticket": {"title": "改造登录页"}},
            {"action": "done", "remark": "已交付：开关与持久化都完成了"},
        ],
        "cto": [split_same_assignee, {"action": "done", "remark": "已汇总"}],
        "se1": [
            {"action": "submit", "summary": "第一张完成"},
            {"action": "submit", "summary": "第二张完成"},
        ],
        "se2": [],
    }
    env = await make_fanout_env(script, tmp_path)
    budget = {
        "max_steps": 30,
        "max_tickets": 10,
        "max_rounds_per_ticket": 2,
        "max_tokens": 0,
        "timeout_seconds": 600,
        "max_parallel": 2,
    }
    team = await create_team(client, fanout_graph(env["agent_profiles"]), budget=budget)

    # 只卡 se1：如果引擎把同一岗位的两张工单并行派出，第二次执行会与第一次抢
    # 同一个 gate（都能过），但时间线上会出现两个 streaming 的 se1 步骤——
    # 串行则永远只有一个。
    (env["state_dir"] / "gate-se1").write_text("wait", encoding="utf-8")
    run_id, drive_task = await _start_in_background(client, team["id"], "把登录页改成暗色主题")
    try:
        assert await _wait_until(
            lambda: len(_prompts_of(_prompts(env["state_dir"]), "se1")) >= 1
        ), "se1 没有进入执行"
        await asyncio.sleep(0.3)  # 给并行调度留出犯错的时间窗口
        se1_prompts = _prompts_of(_prompts(env["state_dir"]), "se1")
        assert len(se1_prompts) == 1, f"同一岗位不该同时收到两个 prompt：{len(se1_prompts)}"
    finally:
        (env["state_dir"] / "gate-se1").unlink(missing_ok=True)
        await asyncio.wait_for(drive_task, timeout=30)

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "done", detail.get("error")


async def test_max_parallel_one_keeps_serial_behaviour(
    client: httpx.AsyncClient, tmp_path: Path
):
    """``max_parallel=1``（默认）：两张子工单严格排队，与过去的串行完全一致。"""
    script = {
        "ceo": [
            {"action": "delegate", "assignee": "cto", "mode": "handoff", "ticket": {"title": "改造登录页"}},
            {"action": "done", "remark": "已交付：开关与持久化都完成了"},
        ],
        "cto": [SPLIT_TWO, {"action": "done", "remark": "已汇总"}],
        "se1": [{"action": "submit", "summary": "开关完成"}],
        "se2": [{"action": "submit", "summary": "持久化完成"}],
    }
    env = await make_fanout_env(script, tmp_path)
    team = await create_team(client, fanout_graph(env["agent_profiles"]))

    # 卡住 se1：串行调度下 se2 必须还在等（prompt 没写入）
    (env["state_dir"] / "gate-se1").write_text("wait", encoding="utf-8")
    run_id, drive_task = await _start_in_background(client, team["id"], "把登录页改成暗色主题")
    try:
        assert await _wait_until(
            lambda: bool(_prompts_of(_prompts(env["state_dir"]), "se1"))
        ), "se1 没有进入执行"
        await asyncio.sleep(0.3)
        assert not _prompts_of(_prompts(env["state_dir"]), "se2"), (
            "max_parallel=1 时 se2 不该在 se1 完成前开始"
        )
    finally:
        (env["state_dir"] / "gate-se1").unlink(missing_ok=True)
        await asyncio.wait_for(drive_task, timeout=30)

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "done", detail.get("error")
    # 时间线顺序：se1 先于 se2（按就绪队列顺序）
    roles = [s["role_key"] for s in detail["steps"]]
    assert roles.index("se1") < roles.index("se2"), roles


async def test_budget_patch_can_tune_max_parallel_midway(
    client: httpx.AsyncClient, tmp_path: Path
):
    """运行中通过 ``/runs/{id}/budget`` 调整并行上限：挂起后调高、点继续即并行。

    场景：默认串行（max_parallel=1）跑到挂起（ask_user），把并行度调到 2 并回答提问，
    后续的兄弟工单波次按并行推进——「调预算 + 继续」是运行中唯一的并行开关入口。
    """
    ask = {
        "action": "ask_user",
        "question": "先并行做两张子工单吗？",
        "options": [
            {"label": "并行做", "value": "并行做"},
            {"label": "串行做", "value": "串行做"},
        ],
    }
    script = {
        "ceo": [
            {"action": "delegate", "assignee": "cto", "mode": "handoff", "ticket": {"title": "改造登录页"}},
            {"action": "done", "remark": "已交付"},
        ],
        "cto": [ask, SPLIT_TWO, {"action": "done", "remark": "已汇总"}],
        "se1": [{"action": "submit", "summary": "开关完成"}],
        "se2": [{"action": "submit", "summary": "持久化完成"}],
    }
    env = await make_fanout_env(script, tmp_path)
    team = await create_team(client, fanout_graph(env["agent_profiles"]))

    run = await client.post("/api/collab/runs", json={"team_id": team["id"]})
    assert run.status_code == 201, run.text
    run_id = run.json()["id"]
    first = await client.post(f"/api/collab/runs/{run_id}/messages", json={"content": "把登录页改成暗色主题"})
    assert first.status_code == 200, first.text
    await read_sse(first)
    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "waiting_user"  # CTO 在 ask_user 挂起
    assert detail["budget"]["max_parallel"] == 1  # 默认串行

    # 调高并行上限（运行挂起时才允许调预算）
    patched = await client.post(f"/api/collab/runs/{run_id}/budget", json={"max_parallel": 2})
    assert patched.status_code == 200, patched.text
    assert patched.json()["budget"]["max_parallel"] == 2

    # 两张 gate 都在：回答提问后 se1/se2 应当同波并行进入执行
    for key in ("se1", "se2"):
        (env["state_dir"] / f"gate-{key}").write_text("wait", encoding="utf-8")

    async def answer() -> None:
        async with client.stream(
            "POST", f"/api/collab/runs/{run_id}/messages", json={"content": "并行做"}
        ) as response:
            assert response.status_code == 200, response.text
            async for _ in response.aiter_lines():
                pass

    drive_task = asyncio.create_task(answer())
    try:
        assert await _wait_until(
            lambda: bool(_prompts_of(_prompts(env["state_dir"]), "se1"))
            and bool(_prompts_of(_prompts(env["state_dir"]), "se2"))
        ), "调高并行上限后 se1 与 se2 应当同时进入执行"
    finally:
        for key in ("se1", "se2"):
            (env["state_dir"] / f"gate-{key}").unlink(missing_ok=True)
        await asyncio.wait_for(drive_task, timeout=30)

    final = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert final["status"] == "done", final.get("error")
    titles = {t["title"] for t in final["tickets"] if t["parent_ticket_id"]}
    assert titles == {"实现主题开关", "实现持久化"}
