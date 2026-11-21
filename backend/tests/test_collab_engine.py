"""协作引擎端到端：用替身智能体跑通「派发 → 拆解 → 实施 → 审查 → 打回 → 复审 → 完成」。

这些用例同时守住两件最要紧的承诺：

* 智能体之间**只传工单**（下游的 prompt 里不会出现任何智能体的对话原文）；
* 任何异常/不符合协议都**显式挂起**（``waiting_user``），并且能重试续跑。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import re
import sys
from datetime import timedelta
from pathlib import Path

import httpx
import pytest_asyncio
from sqlmodel import select

from app.collab.bus import bus
from app.collab.schemas import Duty, EdgeKind, EdgeSpec, GraphSpec, NodeSpec
from app.collab import execution as execution_service
from app.collab import graph as graph_service
from app.collab.store import recover_interrupted
from app.collab.tables import CollabRun, CollabStep, CollabTeam, CollabTicket
from app.db import SessionLocal
from app.models import AgentProfile, Gateway, utcnow
from helpers import collab_options


#: 每个智能体都会说的一句口头禅；它出现在别人的输入里就说明「共享了对话记忆」
CHITCHAT = "我来处理"

HANDOFF = {
    "action": "delegate",
    "assignee": "cto",
    "mode": "handoff",
    "ticket": {
        "title": "登录页改造为暗色主题",
        "goal": "在不改动后端的前提下支持暗色主题",
        "requirements": ["新增主题开关", "持久化用户选择"],
        "acceptance": ["刷新后主题保持", "对比度符合 WCAG AA"],
        "constraints": ["只改 frontend/src/**", "不得新增依赖"],
    },
}

#: 收尾汇报：根工单完成后交回入口智能体（CEO），它只汇报结论并 done 关闭工单。
CLOSING = {"action": "done", "remark": "已交付：暗色主题已上线，改动集中在 frontend/src/theme.ts"}

FULL_SCRIPT = {
    "ceo": [HANDOFF, CLOSING],
    "cto": [
        {
            "action": "delegate",
            "assignee": "se",
            "mode": "split",
            "ticket": {
                "title": "实现主题开关与持久化",
                "goal": "按验收标准实现暗色主题",
                "requirements": ["主题开关", "localStorage 持久化"],
                "acceptance": ["刷新后主题保持"],
            },
        },
        {"action": "done", "remark": "子任务已全部通过，汇总完成"},
    ],
    "se": [
        {"action": "submit", "summary": "已完成主题开关与持久化", "artifacts": ["frontend/src/theme.ts"]},
        {
            "action": "submit",
            "summary": "已按审查意见补上对比度",
            "artifacts": ["frontend/src/theme.ts", "frontend/src/index.css"],
        },
    ],
    "qa": [
        {
            "action": "review",
            "verdict": "changes_requested",
            "review_comment": "第 2 条验收未满足：刷新后主题没有保持",
            "rework_to": "se",
        },
        {"action": "review", "verdict": "pass"},
    ],
}


async def read_sse(response: httpx.Response) -> list[dict]:
    events: list[dict] = []
    async for line in response.aiter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


async def make_scenario(script: dict, tmp_path: Path) -> dict:
    """造好「一个假智能体 + 四个智能体 + 剧本文件」。"""
    script_path = tmp_path / "script.json"
    script_path.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    async with SessionLocal() as session:
        gateway = Gateway(
            name="协作测试网关", kind="custom", base_url="https://gw.example/v1", selected_models=[]
        )
        session.add(gateway)
        await session.commit()
        await session.refresh(gateway)

        roles = {}
        for key, name in (("ceo", "CEO"), ("cto", "CTO"), ("se", "SE"), ("qa", "审查员")):
            role = AgentProfile(
                name=name,
                avatar="🧑",
                system_prompt=f"你是 {name}。",
                type_id="custom-cli",
                gateway_id=gateway.id,
                options=collab_options(script_path, state_dir),
            )
            session.add(role)
            await session.commit()
            await session.refresh(role)
            roles[key] = role.id

    return {
        "agent_profiles": roles,
        "state_dir": state_dir,
        "script_path": script_path,
    }


def team_body(roles: dict[str, str], workspace_dir: str | None = None) -> dict:
    """CEO → CTO → SE，SE 提交给审查员，审查员可直接打回 SE（也可交回 CTO）。"""
    spec = GraphSpec(
        nodes=[
            NodeSpec(key="ceo", agent_profile_id=roles["ceo"], label="CEO", duty=Duty.COORDINATOR,
                     instructions="只负责理解需求与派活，不写代码",
                     capability_scope="不得写代码、不得访问工作目录"),
            NodeSpec(key="cto", agent_profile_id=roles["cto"], label="CTO", duty=Duty.COORDINATOR,
                     instructions="负责拆解任务与汇总"),
            NodeSpec(key="se", agent_profile_id=roles["se"], label="SE", duty=Duty.ENGINEER),
            NodeSpec(key="qa", agent_profile_id=roles["qa"], label="审查员", duty=Duty.REVIEWER),
        ],
        edges=[
            EdgeSpec(from_key="ceo", to_key="cto", kind=EdgeKind.ASSIGN),
            EdgeSpec(from_key="cto", to_key="se", kind=EdgeKind.ASSIGN),
            EdgeSpec(from_key="se", to_key="qa", kind=EdgeKind.REVIEW),
            EdgeSpec(from_key="qa", to_key="se", kind=EdgeKind.REWORK),
        ],
    )
    body = {
        "name": "客户端研发组",
        "entry_key": "ceo",
        "graph": graph_service.to_storable(spec),
    }
    if workspace_dir:
        body["workspace_dir"] = workspace_dir
    return body


async def start_run(
    client: httpx.AsyncClient,
    roles: dict[str, str],
    goal: str,
    *,
    team_workspace_dir: str | None = None,
    run_workspace_dir: str | None = None,
) -> tuple[str, list[dict]]:
    created = await client.post("/api/collab/teams", json=team_body(roles, team_workspace_dir))
    assert created.status_code == 201, created.text
    assert created.json()["validation"]["ok"], created.json()["validation"]
    run_body: dict = {"team_id": created.json()["id"]}
    if run_workspace_dir:
        run_body["workspace_dir"] = run_workspace_dir
    run = await client.post("/api/collab/runs", json=run_body)
    assert run.status_code == 201, run.text
    run_id = run.json()["id"]
    response = await client.post(f"/api/collab/runs/{run_id}/messages", json={"content": goal})
    assert response.status_code == 200, response.text
    return run_id, await read_sse(response)


def prompts(state_dir: Path) -> list[str]:
    path = state_dir / "prompts.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)["prompt"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


#: 岗位头行（如「[协作任务] … 你的岗位：实施者（se）」）；节点 key 也会作为别人的历史摘要出现，
#: 所以只认这一行，不认正文里的 key。
POST_LINE = re.compile(r"\[协作任务\][^\n]*你的岗位：[^（\n]*（(?P<key>[^）\n]+)）")
#: prompt 里的团队工作路径行；所有岗位必须拿到同一条路径。
WORKSPACE_LINE = re.compile(r"工作路径（本次协作唯一允许访问的目录）：(.+)")


def prompts_of(all_prompts: list[str], key: str) -> list[str]:
    """挑出**这一轮就是发给该岗位**的 prompt（按岗位头判定，不看正文里的 key）。"""
    picked = []
    for text in all_prompts:
        match = POST_LINE.search(text)
        if match and match.group("key") == key:
            picked.append(text)
    return picked


@pytest_asyncio.fixture
async def full(client: httpx.AsyncClient, tmp_path: Path) -> dict:
    return await make_scenario(FULL_SCRIPT, tmp_path)


async def test_full_chain_with_one_rework_round(client: httpx.AsyncClient, full):
    run_id, events = await start_run(client, full["agent_profiles"], "把登录页改成暗色主题")

    kinds = [event["type"] for event in events]
    # 串行推进：每一步都有 started / finished，最后给出运行状态
    assert kinds[0] == "collab.run.started"
    assert kinds.count("collab.step.started") == 8, kinds
    assert kinds.count("collab.step.finished") == 8
    # 流以框架自带的 stream.closed 收尾；运行态看最后一个 collab.run.status
    assert kinds[-1] == "stream.closed"
    statuses = [event["status"] for event in events if event["type"] == "collab.run.status"]
    assert statuses and statuses[-1] == "done"
    assert "collab.ticket.created" in kinds  # CTO 拆出的子工单

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "done"
    assert detail["tickets_used"] == 2
    assert detail["tickets"][0]["state"] == "done"
    # 审查打回后重走了一轮：子工单的 round 记到了 1
    child = next(t for t in detail["tickets"] if t["parent_ticket_id"])
    assert child["state"] == "done" and child["round"] == 1 and child["verdict"] == "pass"

    # 时间线：用户 1 条 + 智能体 8 条，且顺序与岗位一致；最后一条是入口智能体的收尾汇报
    assert [s["role_key"] for s in detail["steps"]] == [
        "user", "ceo", "cto", "se", "qa", "se", "qa", "cto", "ceo",
    ]
    assert detail["steps"][0]["content"] == "把登录页改成暗色主题"
    assert detail["steps"][-1]["action"] == "done"
    # 用量从每一步累加到运行上
    assert detail["usage"]["input_tokens"] == 80 and detail["usage"]["output_tokens"] == 40


async def test_root_ticket_closure_reports_back_to_entry(client: httpx.AsyncClient, full):
    """根工单收尾后必须交回入口智能体：由它把结论汇报给用户。

    否则工单在链路里转一圈就结束了，用户那边（主会话）永远等不到一句回话。
    """
    run_id, _ = await start_run(client, full["agent_profiles"], "把登录页改成暗色主题")

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "done"
    assert sum(1 for step in detail["steps"] if step["role_key"] == "ceo") == 2  # 派工 + 汇报
    last = detail["steps"][-1]
    assert last["role_key"] == "ceo" and last["action"] == "done"
    # 汇报挂在根工单上，工单树不变、也不额外占一张工单
    assert last["ticket_id"] == detail["tickets"][0]["id"]
    assert detail["tickets_used"] == 2

    report = prompts_of(prompts(full["state_dir"]), "ceo")[-1]
    assert "全流程结果" in report
    # 收尾只允许汇报 / 提问，绝不允许从这里重新派活
    assert "允许的动作：done、ask_user" in report
    assert "（你没有派发对象）" in report
    assert "本轮要求：所有工单都已收尾" in report
    # 子工单的结论也在汇总里：入口智能体据此汇报，不必回头问下游
    assert "《实现主题开关与持久化》" in report
    assert "已按审查意见补上对比度" in report

    # 已完成后再点「继续」也不会反复汇报
    resumed = await client.post(f"/api/collab/runs/{run_id}/resume")
    assert resumed.status_code == 200
    await read_sse(resumed)
    again = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert again["status"] == "done"
    assert len(again["steps"]) == len(detail["steps"])

    # 只汇报一次：标记落库，下一次推进不会再来一轮
    async with SessionLocal() as session:
        root = (
            await session.execute(select(CollabTicket).where(CollabTicket.run_id == run_id))
        ).scalars().first()
        assert root is not None and root.report_at is not None


async def test_entry_closing_the_ticket_itself_does_not_report_twice(client: httpx.AsyncClient, tmp_path: Path):
    """入口智能体自己收尾时（它那一步就是给用户的答复）不再补一轮汇报。"""
    script = {"ceo": [{"action": "done", "remark": "已答复用户：这次不用改代码"}]}
    scenario = await make_scenario(script, tmp_path)
    run_id, _ = await start_run(client, scenario["agent_profiles"], "解释一下这个报错")

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "done"
    assert [step["role_key"] for step in detail["steps"]] == ["user", "ceo"]
    assert len(prompts(scenario["state_dir"])) == 1


async def test_user_follow_up_opens_a_new_report_round(client: httpx.AsyncClient, tmp_path: Path):
    """用户追问 → 重新干活 → 再汇报一轮：汇报标记在重新开启根工单时清零。"""
    script = {"ceo": [HANDOFF, CLOSING, HANDOFF, CLOSING], "cto": [{"action": "done", "remark": "完成"}]}
    scenario = await make_scenario(script, tmp_path)
    run_id, _ = await start_run(client, scenario["agent_profiles"], "把登录页改成暗色主题")

    first = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert first["status"] == "done"
    assert [step["role_key"] for step in first["steps"]] == ["user", "ceo", "cto", "ceo"]

    response = await client.post(f"/api/collab/runs/{run_id}/messages", json={"content": "再加一个导出功能"})
    assert response.status_code == 200
    await read_sse(response)

    final = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert final["status"] == "done"
    assert [step["role_key"] for step in final["steps"]] == [
        "user", "ceo", "cto", "ceo", "user", "ceo", "cto", "ceo",
    ]
    assert final["steps"][-1]["role_key"] == "ceo" and final["steps"][-1]["action"] == "done"


async def test_single_rework_to_cto_does_not_need_rework_to(client: httpx.AsyncClient, tmp_path: Path):
    """审查者只有一条 QA → CTO 退路时，工单不写 rework_to 也应自动交回 CTO。"""
    script = {
        "ceo": [HANDOFF, CLOSING],
        "cto": [
            {
                "action": "delegate",
                "assignee": "se",
                "mode": "split",
                "ticket": {
                    "title": "实现主题开关与持久化",
                    "goal": "按验收标准实现暗色主题",
                    "requirements": ["主题开关", "localStorage 持久化"],
                    "acceptance": ["刷新后主题保持"],
                },
            },
            {
                "action": "delegate",
                "assignee": "se",
                "mode": "handoff",
                "ticket": {"title": "按审查意见返工"},
            },
            {"action": "done", "remark": "子任务已全部通过，汇总完成"},
        ],
        "se": [
            {"action": "submit", "summary": "已完成主题开关与持久化", "artifacts": ["frontend/src/theme.ts"]},
            {
                "action": "submit",
                "summary": "已按审查意见补上对比度",
                "artifacts": ["frontend/src/theme.ts", "frontend/src/index.css"],
            },
        ],
        "qa": [
            {
                "action": "review",
                "verdict": "changes_requested",
                "review_comment": "第 2 条验收未满足：刷新后主题没有保持",
            },
            {"action": "review", "verdict": "pass"},
        ],
    }
    scenario = await make_scenario(script, tmp_path)
    spec = GraphSpec(
        nodes=[
            NodeSpec(key="ceo", agent_profile_id=scenario["agent_profiles"]["ceo"], label="CEO", duty=Duty.COORDINATOR),
            NodeSpec(key="cto", agent_profile_id=scenario["agent_profiles"]["cto"], label="CTO", duty=Duty.COORDINATOR),
            NodeSpec(key="se", agent_profile_id=scenario["agent_profiles"]["se"], label="SE", duty=Duty.ENGINEER),
            NodeSpec(key="qa", agent_profile_id=scenario["agent_profiles"]["qa"], label="审查员", duty=Duty.REVIEWER),
        ],
        edges=[
            EdgeSpec(from_key="ceo", to_key="cto", kind=EdgeKind.ASSIGN),
            EdgeSpec(from_key="cto", to_key="se", kind=EdgeKind.ASSIGN),
            EdgeSpec(from_key="se", to_key="qa", kind=EdgeKind.REVIEW),
            EdgeSpec(from_key="qa", to_key="cto", kind=EdgeKind.REWORK),
        ],
    )
    created = await client.post(
        "/api/collab/teams",
        json={"name": "交回 CTO 的研发组", "entry_key": "ceo", "graph": graph_service.to_storable(spec)},
    )
    assert created.status_code == 201, created.text
    validation = created.json()["validation"]
    assert validation["ok"] and not validation["warnings"], validation

    run = await client.post("/api/collab/runs", json={"team_id": created.json()["id"]})
    assert run.status_code == 201, run.text
    response = await client.post(
        f"/api/collab/runs/{run.json()['id']}/messages", json={"content": "把登录页改成暗色主题"}
    )
    assert response.status_code == 200, response.text
    await read_sse(response)

    detail = (await client.get(f"/api/collab/runs/{run.json()['id']}")).json()
    assert detail["status"] == "done"
    assert [step["role_key"] for step in detail["steps"]] == [
        "user", "ceo", "cto", "se", "qa", "cto", "se", "qa", "cto", "ceo",
    ]
    child = next(ticket for ticket in detail["tickets"] if ticket["parent_ticket_id"])
    assert child["state"] == "done" and child["round"] == 1 and child["verdict"] == "pass"


async def test_downstream_only_sees_the_ticket_never_the_conversation(client: httpx.AsyncClient, full):
    """C5：下游只拿到工单字段与摘要，拿不到任何智能体的对话原文。"""
    await start_run(client, full["agent_profiles"], "把登录页改成暗色主题")
    all_prompts = prompts(full["state_dir"])
    assert len(all_prompts) == 8

    for text in all_prompts:
        # 每个智能体都会说「（<key>）我来处理。」；它出现在别人的输入里就等于泄露了对话
        assert CHITCHAT not in text, "下游 prompt 里出现了上游智能体的对话原文"
        assert "—— 工单 T-" in text  # 交接物一定是工单

    se_prompts = prompts_of(all_prompts, "se")
    assert len(se_prompts) == 2
    # 返工那一轮带的是审查意见（工单字段），不是审查者的整段对话
    assert "审查意见：第 2 条验收未满足：刷新后主题没有保持" in se_prompts[1]
    # 实施者的输入里没有 CEO 那份需求的完整原文，只有 CTO 拆出来的子工单字段
    assert "不得新增依赖" not in se_prompts[0]
    assert "实现主题开关与持久化" in se_prompts[0]

    qa_prompts = prompts_of(all_prompts, "qa")
    assert qa_prompts and all("刷新后主题保持" in text for text in qa_prompts)

    # 协调者也会收到团队工作路径；但按岗位职责没有该路径的读写权限
    ceo_prompts = prompts_of(all_prompts, "ceo")
    assert len(ceo_prompts) == 2  # 一次派工 + 一次收尾汇报
    dispatch_prompt, report_prompt = ceo_prompts
    assert "工作路径（本次协作唯一允许访问的目录）：" in dispatch_prompt
    assert "没有该路径的读写权限" in dispatch_prompt
    assert "不得写代码" in dispatch_prompt

    # 收尾汇报只看「结构化结果」，仍然看不到任何智能体的对话原文
    assert "全流程结果" in report_prompt
    assert "已按审查意见补上对比度" in report_prompt


async def test_every_role_sees_the_whole_team_roster(client: httpx.AsyncClient, full):
    """每个岗位的 prompt 里都有全组名册：团队里有谁、谁负责什么、入口是谁。

    真实反馈：入口智能体（CEO）答不出「团队里有哪些人」。此前 prompt 只列出**自己的派发对象**，
    组里配了审查者也无从得知 —— 同一张图里 CTO 因此说过「本协作没有安排审查者」。
    """
    await start_run(client, full["agent_profiles"], "把登录页改成暗色主题")
    all_prompts = prompts(full["state_dir"])
    assert all_prompts
    for text in all_prompts:
        assert "—— 协作组名册（系统提供的团队结构，只读）——" in text
        assert "CEO（ceo） · 协调者 · 入口，唯一和用户对话的智能体 · 只负责理解需求与派活，不写代码" in text
        assert "CTO（cto） · 协调者 · 负责拆解任务与汇总" in text
        assert "SE（se） · 实施者" in text
        assert "审查员（qa） · 审查者" in text
        # 名册正好 4 行（组里就这 4 个岗位），不是「自己 + 派发对象」那种半份名单
        roster_part = text.split("—— 协作组名册（系统提供的团队结构，只读）——")[1].split("名册只说明")[0]
        assert len([line for line in roster_part.splitlines() if line.strip()]) == 4


async def test_run_workspace_path_is_passed_to_every_role(client: httpx.AsyncClient, full, tmp_path: Path):
    """新建协作任务时指定的工作路径必须原样传给每个岗位，并落在运行记录里。"""
    shared = (tmp_path / "shared-project").resolve()
    shared.mkdir()

    run_id, _ = await start_run(client, full["agent_profiles"], "统一工作路径", run_workspace_dir=str(shared))
    all_prompts = prompts(full["state_dir"])
    assert len(all_prompts) == 8

    matched = [WORKSPACE_LINE.search(text) for text in all_prompts]
    assert all(match is not None for match in matched)
    assert {match.group(1).strip() for match in matched if match} == {str(shared)}

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["workspace_dir"] == str(shared)


async def test_protocol_violation_hangs_then_recovers_by_retry(client: httpx.AsyncClient, tmp_path: Path):
    script = {
        "ceo": ["我这就去办，稍等。", HANDOFF, CLOSING],
        "cto": [{"action": "done", "remark": "完成"}],
    }
    scenario = await make_scenario(script, tmp_path)
    run_id, events = await start_run(client, scenario["agent_profiles"], "把登录页改成暗色主题")

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "waiting_user"
    assert "工单协议" in (detail["error"] or "")
    assert any(event["type"] == "collab.notice" and event.get("level") == "error" for event in events)

    failed = detail["steps"][-1]
    assert failed["status"] == "error" and failed["role_key"] == "ceo"

    # 修好之后「重试 + 继续」应当能接着跑完
    retried = await client.post(f"/api/collab/steps/{failed['id']}/retry")
    assert retried.status_code == 200
    assert retried.json()["status"] == "waiting_user"
    resumed = await client.post(f"/api/collab/runs/{run_id}/resume")
    assert resumed.status_code == 200
    resumed_events = await read_sse(resumed)
    resumed_status = [e["status"] for e in resumed_events if e["type"] == "collab.run.status"]
    assert resumed_status and resumed_status[-1] == "done"
    final = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert final["status"] == "done" and final["tickets"][0]["state"] == "done"


async def test_round_with_two_work_order_blocks_uses_the_last_one(client: httpx.AsyncClient, tmp_path: Path):
    """工具型内核会把草稿和结论写进同一轮：取最后一个并提示，而不是把整轮判死。"""
    draft = (
        "先写个草稿工单：\n\n"
        '```work_order\n{"action": "ask_user", "question": "先问一句", '
        '"options": [{"label": "好", "value": "好"}, {"label": "不好", "value": "不好"}]}\n```\n'
    )
    final = "\n想清楚了，最终决定如下：\n\n```work_order\n" + json.dumps(HANDOFF, ensure_ascii=False) + "\n```\n"
    script = {"ceo": [draft + final, CLOSING], "cto": [{"action": "done", "remark": "完成"}]}
    scenario = await make_scenario(script, tmp_path)
    run_id, events = await start_run(client, scenario["agent_profiles"], "把登录页改成暗色主题")

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "done"
    ceo_step = detail["steps"][1]
    assert ceo_step["role_key"] == "ceo" and ceo_step["status"] == "complete"
    assert ceo_step["action"] == "delegate" and ceo_step["target_key"] == "cto"
    warnings = [e for e in events if e["type"] == "collab.notice" and e.get("level") == "warn"]
    assert any("2 个 work_order" in (e.get("text") or "") for e in warnings)
    # 草稿块不进正文：时间线上不该出现「假装发出去」的工单
    assert "work_order" not in ceo_step["content"]


async def test_entry_can_ask_the_user_and_then_continue(client: httpx.AsyncClient, tmp_path: Path):
    script = {
        "ceo": [
            {
                "action": "ask_user",
                "question": "暗色主题要跟随系统设置吗？",
                "options": [
                    {"label": "跟随系统", "value": "要，跟随系统", "description": "系统切换时同步"},
                    {"label": "固定暗色", "value": "不要，固定暗色"},
                ],
            },
            HANDOFF,
            CLOSING,
        ],
        "cto": [{"action": "done", "remark": "完成"}],
    }
    scenario = await make_scenario(script, tmp_path)
    run_id, events = await start_run(client, scenario["agent_profiles"], "把登录页改成暗色主题")

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "waiting_user"
    ask_events = [event for event in events if event["type"] == "collab.notice" and event.get("level") == "ask"]
    assert ask_events
    assert ask_events[0]["question"] == "暗色主题要跟随系统设置吗？"
    assert [item["label"] for item in ask_events[0]["options"]] == ["跟随系统", "固定暗色"]
    root = detail["tickets"][0]
    assert root["state"] == "blocked" and "跟随系统" in root["result"]["question"]
    assert root["result"]["options"][0] == {
        "label": "跟随系统",
        "value": "要，跟随系统",
        "description": "系统切换时同步",
        "freeform": False,
    }

    # 用户回答 → 工单回到入口智能体手里，继续推进到完成
    response = await client.post(f"/api/collab/runs/{run_id}/messages", json={"content": "要，跟随系统"})
    assert response.status_code == 200
    await read_sse(response)

    final = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert final["status"] == "done"
    assert [s["role_key"] for s in final["steps"]][:3] == ["user", "ceo", "user"]
    assert final["steps"][2]["content"] == "要，跟随系统"
    # 答复之后旧卡片数据被清掉，后续因别的原因挂起时不会误显示上一轮的选择题
    assert "question" not in final["tickets"][0]["result"]
    assert "options" not in final["tickets"][0]["result"]


async def test_non_entry_ask_user_is_explained_and_never_ends_in_silence(
    client: httpx.AsyncClient, tmp_path: Path
):
    """非入口节点误配 ask_user 时：提示词不再教它这么做，报错说清原因，且用户发言不会石沉大海。

    真实事故：编辑器把 ask_user 勾给了实施者，模型照做，整轮被判中止；用户随后在输入框里问
    「进度怎么样了」，界面只多了一条用户发言、没有任何回话。
    """
    split_to_cto = {
        "action": "delegate",
        "assignee": "cto",
        "mode": "split",
        "ticket": {
            "title": "查一下工作目录",
            "goal": "列出工作目录下的文件",
            "acceptance": ["给出文件清单，或明确说明目录为空"],
        },
    }
    script = {
        "ceo": [split_to_cto],
        "cto": [
            {
                "action": "ask_user",
                "question": "要暗色还是亮色？",
                "options": [{"label": "暗色", "value": "暗色"}, {"label": "亮色", "value": "亮色"}],
            }
        ],
    }
    scenario = await make_scenario(script, tmp_path)
    body = team_body(scenario["agent_profiles"])
    for item in body["graph"]["nodes"]:
        if item["key"] == "cto":
            item["output_contract"] = ["done", "ask_user"]
    created = await client.post("/api/collab/teams", json=body)
    assert created.status_code == 201, created.text

    run = await client.post("/api/collab/runs", json={"team_id": created.json()["id"]})
    run_id = run.json()["id"]
    response = await client.post(
        f"/api/collab/runs/{run_id}/messages", json={"content": "做一个记账系统"}
    )
    assert response.status_code == 200, response.text
    await read_sse(response)

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "waiting_user"
    assert "只有入口节点能向用户提问" in (detail["error"] or "")
    # 提示词里不再出现 ask_user：配置错了，引擎也不该教模型越权
    cto_prompt = prompts_of(prompts(scenario["state_dir"]), "cto")[-1]
    assert "ask_user" not in cto_prompt

    # 用户以为在问进度：即使这次轮不到任何工单，也必须有一条明确回话，而不是静默。
    again = await client.post(
        f"/api/collab/runs/{run_id}/messages", json={"content": "进度怎么样了"}
    )
    assert again.status_code == 200, again.text
    events = await read_sse(again)
    notices = [event for event in events if event["type"] == "collab.notice"]
    assert any("没有可推进的工单" in (event.get("text") or "") for event in notices), notices
    after = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert "没有可推进的工单" in (after["error"] or "")


async def test_ask_user_without_options_is_salvaged_into_card(client: httpx.AsyncClient, tmp_path: Path):
    """模型漏给 options 时仍应给出可点卡片，而不是只报协议错误。"""
    script = {"ceo": [{"action": "ask_user", "question": "暗色还是亮色？"}]}
    scenario = await make_scenario(script, tmp_path)
    run_id, events = await start_run(client, scenario["agent_profiles"], "把登录页换一套配色")

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "waiting_user"
    root = detail["tickets"][0]
    assert root["state"] == "blocked"
    assert root["result"]["question"] == "暗色还是亮色？"
    labels = [item["label"] for item in root["result"]["options"]]
    assert labels[:2] == ["暗色", "亮色"]

    ask_events = [event for event in events if event["type"] == "collab.notice" and event.get("level") == "ask"]
    assert ask_events and ask_events[0]["options"] == root["result"]["options"]


async def test_resume_after_created_at_exceeds_timeout(client: httpx.AsyncClient, tmp_path: Path):
    """运行创建超过时限后，用户回来继续仍应获得一个新的推进窗口。"""
    ask_once = {
        "action": "ask_user",
        "question": "先做哪一部分？",
        "options": [
            {"label": "先做核心流程", "value": "先做核心流程"},
            {"label": "我来说明", "value": "", "freeform": True},
        ],
    }
    ask_twice = {
        "action": "ask_user",
        "question": "核心流程先覆盖哪个入口？",
        "options": [
            {"label": "移动端", "value": "先覆盖移动端入口"},
            {"label": "桌面端", "value": "先覆盖桌面端入口"},
        ],
    }
    script = {"ceo": [ask_once, ask_twice, HANDOFF, CLOSING], "cto": [{"action": "done", "remark": "完成"}]}
    scenario = await make_scenario(script, tmp_path)
    run_id, _ = await start_run(client, scenario["agent_profiles"], "推进首期版本")

    # 模拟隔了两小时才回来：创建时间已远超标称时限，但最近一次进展仍发生在刚才。
    async with SessionLocal() as session:
        run = await session.get(CollabRun, run_id)
        assert run is not None
        run.created_at = utcnow() - timedelta(hours=2)
        session.add(run)
        await session.commit()

    first = await client.post(f"/api/collab/runs/{run_id}/messages", json={"content": "先做核心流程"})
    assert first.status_code == 200
    first_events = await read_sse(first)
    first_status = [e["status"] for e in first_events if e["type"] == "collab.run.status"]
    assert first_status and first_status[-1] == "waiting_user"
    assert not any("运行时限" in str(e.get("reason") or e.get("text") or "") for e in first_events)

    second = await client.post(f"/api/collab/runs/{run_id}/messages", json={"content": "先覆盖移动端入口"})
    assert second.status_code == 200
    second_events = await read_sse(second)
    second_status = [e["status"] for e in second_events if e["type"] == "collab.run.status"]
    assert second_status and second_status[-1] == "done"

    final = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert final["status"] == "done"
    assert [s["role_key"] for s in final["steps"]][:5] == ["user", "ceo", "user", "ceo", "user"]


async def test_zero_step_timeout_reaches_runner_as_unlimited(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch
):
    """预算单步限时 0 = 不限：执行层必须转成负数交给 runner，而不是落回全局默认。

    ``BudgetSpec.step_timeout_seconds`` 只允许非负数，用户能表达「不限」的唯一写法就是 0；
    而 runner 的哨兵是「0 = 这次没指定、负数 = 不限」。两者之间必须显式翻译，
    否则一旦部署方给对话域配了 ``settings.run_timeout_seconds``，界面写着「不限」的那一步
    也会在全局值到点时被掐断。
    """
    scenario = await make_scenario({"ceo": [{"action": "done", "remark": "完成"}]}, tmp_path)
    body = team_body(scenario["agent_profiles"])
    body["budget"] = {
        "max_steps": 10,
        "max_tickets": 5,
        "max_rounds_per_ticket": 2,
        "max_tokens": 0,
        "timeout_seconds": 600,
        "step_timeout_seconds": 0,
    }
    created = await client.post("/api/collab/teams", json=body)
    assert created.status_code == 201, created.text
    run = await client.post("/api/collab/runs", json={"team_id": created.json()["id"]})
    assert run.status_code == 201, run.text

    captured: list[int] = []

    async def fake_stream_run(definition, ctx, handle):
        captured.append(handle.timeout_seconds)
        for _ in ():  # 只关心传下去的时限，不必真的产出事件
            yield

    monkeypatch.setattr(execution_service, "stream_run", fake_stream_run)

    async with SessionLocal() as session:
        run_row = await session.get(CollabRun, run.json()["id"])
        assert run_row is not None
        team = await session.get(CollabTeam, run_row.team_id)
        assert team is not None
        node = graph_service.entry_node(graph_service.from_storable(team.graph), team.entry_key)
        assert node is not None

        prepared = await execution_service.prepare_chat(
            session, run=run_row, node=node, prompt="先聊两句"
        )
        assert prepared.step_timeout_seconds == -1
        handle = execution_service.registry.register(run_row.id, "step-unlimited", "CEO")
        try:
            _ = [event async for event in execution_service.stream_step(prepared, handle)]
            assert handle.timeout_seconds == -1
        finally:
            # 句柄表是进程级的：不清理会泄漏进后面的用例（「正在运行的智能体」会
            # 把这条幽灵岗位也列出来，别的用例断言就挂了）。
            execution_service.registry.finish("step-unlimited")

    assert captured == [-1]


async def test_done_run_can_be_reopened_by_user_follow_up(client: httpx.AsyncClient, tmp_path: Path):
    """运行完成后，用户继续找入口智能体时应重新打开根工单，而不是只落一条无人响应的消息。"""
    script = {
        "ceo": [HANDOFF, {"action": "done", "remark": "已按补充需求确认"}],
        "cto": [{"action": "done", "remark": "完成"}],
    }
    scenario = await make_scenario(script, tmp_path)
    run_id, _ = await start_run(client, scenario["agent_profiles"], "先把登录页改成暗色主题")

    first = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert first["status"] == "done" and first["tickets"][0]["state"] == "done"

    response = await client.post(
        f"/api/collab/runs/{run_id}/messages", json={"content": "再加一个导出功能"}
    )
    assert response.status_code == 200
    await read_sse(response)

    final = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert final["status"] == "done"
    assert final["tickets"][0]["state"] == "done"
    assert final["steps"][-2]["role_key"] == "user"
    assert final["steps"][-2]["content"] == "再加一个导出功能"
    assert final["steps"][-1]["role_key"] == "ceo"


async def test_cannot_start_twice_and_resume_needs_something_to_do(client: httpx.AsyncClient, full):
    created = await client.post("/api/collab/teams", json=team_body(full["agent_profiles"]))
    team_id = created.json()["id"]
    run = await client.post("/api/collab/runs", json={"team_id": team_id})
    run_id = run.json()["id"]

    # 还没有目标就继续 → 明确拒绝
    blocked = await client.post(f"/api/collab/runs/{run_id}/resume")
    assert blocked.status_code == 409
    assert "请先告诉入口智能体" in blocked.json()["detail"]

    # 图不合法时不允许开跑
    broken = team_body(full["agent_profiles"])
    broken["entry_key"] = "se"
    await client.put(f"/api/collab/teams/{team_id}", json={"entry_key": "se"})
    response = await client.post(f"/api/collab/runs/{run_id}/messages", json={"content": "开始"})
    assert response.status_code == 409
    assert "编排图还不能跑" in response.json()["detail"]
    assert broken["entry_key"] == "se"


async def test_recover_interrupted_also_settles_streaming_steps(client: httpx.AsyncClient, full):
    """服务重启后残留的 streaming 步骤必须收尾。

    否则界面会一直显示「处理中」，点开该岗位却看不到任何输出（它永远不会再产出）。
    """
    run_id, _ = await start_run(client, full["agent_profiles"], "把登录页改成暗色主题")

    async with SessionLocal() as session:
        run = await session.get(CollabRun, run_id)
        run.status = "running"
        session.add(run)
        session.add(
            CollabStep(run_id=run_id, role_key="se", duty="engineer", sequence=99, status="streaming")
        )
        await session.commit()

        await recover_interrupted(session)

        refreshed = await session.get(CollabRun, run_id)
        assert refreshed.status == "interrupted"
        lingering = (
            await session.execute(select(CollabStep).where(CollabStep.status == "streaming"))
        ).scalars().all()
        assert lingering == []
        step = (
            await session.execute(select(CollabStep).where(CollabStep.sequence == 99))
        ).scalars().one()
        assert step.status == "interrupted"
        assert step.finished_at is not None


async def test_client_disconnect_does_not_stop_the_run(client: httpx.AsyncClient, full):
    """客户端断开（手机锁屏、切后台、网络抖动）不该打断推进。

    旧实现把推进绑在 HTTP 请求上：连接一断，正在跑的那一步就被取消、运行停在 ``running``，
    界面显示「处理中」却再也不动。现在推进跑在独立的后台任务里，断开只退订。
    """
    created = await client.post("/api/collab/teams", json=team_body(full["agent_profiles"]))
    assert created.status_code == 201, created.text
    run_id = (await client.post("/api/collab/runs", json={"team_id": created.json()["id"]})).json()["id"]

    received = 0
    async with client.stream(
        "POST", f"/api/collab/runs/{run_id}/messages", json={"content": "把登录页改成暗色主题"}
    ) as response:
        assert response.status_code == 200, await response.aread()
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                received += 1
                if received >= 2:  # 模拟客户端中途断开
                    break
    assert received >= 2

    detail = None
    for _ in range(400):
        detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
        if detail["status"] != "running":
            break
        await asyncio.sleep(0.05)

    assert detail is not None
    assert detail["status"] in {"waiting_user", "done", "error"}, detail["status"]
    # 断开之后仍然推进了多步：证明它没有跟着连接一起死掉
    assert detail["steps_used"] > 0
    assert not any(step["status"] == "streaming" for step in detail["steps"])


async def _wait_until(predicate, *, timeout: float = 10.0) -> bool:
    """轮询直到条件成立或超时（避免用固定 sleep 引入竞态）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if predicate():
            return True
        if loop.time() >= deadline:
            return predicate()
        await asyncio.sleep(0.01)


def _stream_events(run_id: str) -> list[dict]:
    """总线里本轮的缓冲事件（测试用的内部视角，见 `app/collab/bus.py`）。"""
    stream = bus._streams.get(run_id)
    return list(stream.events) if stream else []


def _has_subscriber(run_id: str) -> bool:
    """这条运行的实时事件是否已经有订阅者挂上（同样是内部视角）。"""
    stream = bus._streams.get(run_id)
    return bool(stream and stream.subscribers)


async def _start_until_gate(
    client: httpx.AsyncClient,
    scenario: dict,
    goal: str,
    gate_key: str,
) -> tuple[str, asyncio.Task[None], Path]:
    """在后台启动协作，并等某个岗位真的进入执行（剧本已写入 prompt、卡在闸门）。"""
    gate = scenario["state_dir"] / f"gate-{gate_key}"
    gate.write_text("wait", encoding="utf-8")
    created = await client.post("/api/collab/teams", json=team_body(scenario["agent_profiles"]))
    assert created.status_code == 201, created.text
    run_id = (await client.post("/api/collab/runs", json={"team_id": created.json()["id"]})).json()["id"]

    async def drive() -> None:
        async with client.stream(
            "POST", f"/api/collab/runs/{run_id}/messages", json={"content": goal}
        ) as response:
            assert response.status_code == 200, response.text
            async for _ in response.aiter_lines():
                pass

    task = asyncio.create_task(drive())
    assert await _wait_until(lambda: bool(prompts_of(prompts(scenario["state_dir"]), gate_key))), (
        f"岗位 {gate_key} 没有进入执行"
    )
    return run_id, task, gate


async def test_stream_endpoint_replays_buffered_events_after_disconnect(client: httpx.AsyncClient, full):
    """断开后用 ``GET /runs/{id}/stream`` 重连：先补发本轮已产生的事件，再接着看实时增量。

    用闸门把 CEO 那一步稳稳卡在运行中，避免「刚好跑完」的竞态。推进请求放在后台任务里，
    取消它才是真断开 —— ``ASGITransport`` 会等应用跑完才返回响应，前台 ``break`` 测不出中途断开。
    """
    gate = full["state_dir"] / "gate-ceo"
    gate.write_text("wait", encoding="utf-8")

    created = await client.post("/api/collab/teams", json=team_body(full["agent_profiles"]))
    assert created.status_code == 201, created.text
    run_id = (await client.post("/api/collab/runs", json={"team_id": created.json()["id"]})).json()["id"]

    async def drive() -> None:
        async with client.stream(
            "POST", f"/api/collab/runs/{run_id}/messages", json={"content": "把登录页改成暗色主题"}
        ) as response:
            assert response.status_code == 200
            async for _ in response.aiter_lines():
                pass

    try:
        drive_task = asyncio.create_task(drive())
        assert await _wait_until(lambda: len(_stream_events(run_id)) >= 2), "推进没有开始"

        # 客户端断开（锁屏 / 切后台）：取消这条请求，运行本身不该受影响
        drive_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drive_task
        assert (await client.get(f"/api/collab/runs/{run_id}")).json()["status"] == "running"

        # 断线重连：订阅接口应当先补发此刻缓冲区里的事件，再接着推实时增量。
        # 注意 `ASGITransport` 会把响应读完才交回客户端，所以订阅也放在后台任务里，
        # 才能在前台「等它挂上 → 放行闸门」，观察真实的重连时序。
        buffered = [event["type"] for event in _stream_events(run_id)]
        assert buffered[0] == "collab.run.started", buffered
        assert "collab.step.started" in buffered, buffered

        received: list[dict] = []

        async def subscribe() -> None:
            async with client.stream("GET", f"/api/collab/runs/{run_id}/stream") as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        received.append(json.loads(line[6:]))

        subscribe_task = asyncio.create_task(subscribe())
        assert await _wait_until(lambda: _has_subscriber(run_id)), "订阅没有挂上"
        gate.unlink(missing_ok=True)  # 放行 CEO，后续步骤作为实时增量送到同一订阅
        await asyncio.wait_for(subscribe_task, timeout=20)
    finally:
        gate.unlink(missing_ok=True)

    kinds = [event["type"] for event in received]
    # 补发：重连后先看到的就是断开前已经发生的事件，顺序一致
    assert kinds[: len(buffered)] == buffered, kinds
    # 续看：同一订阅上继续收完这一轮，直到传输层收尾
    assert kinds.count("collab.step.finished") == 8, kinds
    assert kinds[-1] == "stream.closed", kinds
    assert received[0]["type"] == "collab.run.started"

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "done", detail
    assert detail["steps_used"] == 8


async def test_entry_chat_stays_available_while_the_workflow_is_running(
    client: httpx.AsyncClient, tmp_path: Path
):
    """工单推进期间仍能随时找入口智能体：不返回 409，也不会打断主流程。"""
    script = {
        **FULL_SCRIPT,
        "ceo:chat": ["现在是审查阶段：子工单已经提交，审查员正在看。可以继续补充要求。"],
    }
    scenario = await make_scenario(script, tmp_path)
    run_id, drive_task, gate = await _start_until_gate(
        client, scenario, "把登录页改成暗色主题", "qa"
    )
    try:
        response = await client.post(
            f"/api/collab/runs/{run_id}/chat", json={"content": "现在进度怎么样了？"}
        )
        assert response.status_code == 200, response.text
        events = await read_sse(response)
        kinds = [event["type"] for event in events]
        assert "collab.chat.started" in kinds
        assert "collab.chat.event" in kinds
        assert kinds[-2:] == ["collab.chat.finished", "stream.closed"], kinds
        finished = next(event for event in events if event["type"] == "collab.chat.finished")
        assert finished["status"] == "complete"

        # 主流程没有被聊天顶掉，仍然稳稳停在审查步骤。
        assert (await client.get(f"/api/collab/runs/{run_id}")).json()["status"] == "running"

        # 聊天历史单独落库，并且只给入口智能体回放结构化进度。
        detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
        chat_rows = detail["chat_messages"]
        assert [row["sender"] for row in chat_rows] == ["user", "leader"]
        assert chat_rows[0]["content"] == "现在进度怎么样了？"
        assert "审查阶段" in chat_rows[1]["content"]
        listed = (await client.get(f"/api/collab/runs/{run_id}/chat")).json()["items"]
        assert [row["id"] for row in listed] == [row["id"] for row in chat_rows]
        # 聊天是旁路，不消耗工单步数。
        assert detail["steps_used"] == 3

        chat_prompts = [text for text in prompts(scenario["state_dir"]) if "[协作对话]" in text]
        assert len(chat_prompts) == 1
        chat_prompt = chat_prompts[0]
        # 用户问「团队里都有谁」时 CEO 得答得出来：聊天通道也带全组名册。
        assert "—— 协作组名册（系统提供的团队结构，只读）——" in chat_prompt
        assert "审查员（qa） · 审查者" in chat_prompt
        assert "《实现主题开关与持久化》" in chat_prompt
        assert "状态：review" in chat_prompt
        assert "最近结论：已完成主题开关与持久化" in chat_prompt
        assert "frontend/src/theme.ts" in chat_prompt
        # 聊天能看进度，但看不到其他智能体的对话原文（C5 只对入口聊天开一个口子）。
        assert f"（se）{CHITCHAT}。" not in chat_prompt
    finally:
        gate.unlink(missing_ok=True)
        await asyncio.wait_for(drive_task, timeout=20)


async def test_chat_can_refine_requirements_and_next_step_sees_them(
    client: httpx.AsyncClient, tmp_path: Path
):
    """聊天里的细化要求会写进未收尾工单，并从下一轮步骤开始生效。"""
    chat_reply = (
        "这是只给 CEO 的闲聊暗号。好的，我再补一条要求：增加重置按钮。\n"
        "```chat_update\n"
        '{"requirements_add": ["增加一个重置按钮"], '
        '"acceptance_add": ["重置后恢复默认主题"]}\n'
        "```\n"
    )
    script = {**FULL_SCRIPT, "ceo:chat": [chat_reply]}
    scenario = await make_scenario(script, tmp_path)
    run_id, drive_task, gate = await _start_until_gate(
        client, scenario, "把登录页改成暗色主题", "qa"
    )
    try:
        response = await client.post(
            f"/api/collab/runs/{run_id}/chat", json={"content": "再加一个重置按钮"}
        )
        assert response.status_code == 200, response.text
        events = await read_sse(response)
        finished = next(event for event in events if event["type"] == "collab.chat.finished")
        assert finished["status"] == "complete"
        assert finished["applied"]["ticket_count"] == 2
        assert "requirements" in finished["applied"]["fields"]

        detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
        child = next(ticket for ticket in detail["tickets"] if ticket["parent_ticket_id"])
        root = next(ticket for ticket in detail["tickets"] if not ticket["parent_ticket_id"])
        assert "增加一个重置按钮" in child["brief"]["requirements"]
        assert "重置后恢复默认主题" in child["brief"]["acceptance"]
        assert "增加一个重置按钮" in root["brief"]["requirements"]
        # 聊天协议块不会作为正文展示给用户。
        leader = detail["chat_messages"][-1]
        assert "chat_update" not in leader["content"]
        assert "闲聊暗号" in leader["content"]
    finally:
        gate.unlink(missing_ok=True)
        await asyncio.wait_for(drive_task, timeout=20)

    # 聊天原文不会传给任何工单智能体；细化后的字段会。
    all_prompts = prompts(scenario["state_dir"])
    work_prompts = [text for text in all_prompts if "[协作对话]" not in text]
    assert all("闲聊暗号" not in text for text in work_prompts)
    qa_prompts = prompts_of(all_prompts, "qa")
    assert len(qa_prompts) == 2
    assert "增加一个重置按钮" in qa_prompts[1]
    assert "重置后恢复默认主题" in qa_prompts[1]


async def test_chat_stays_available_after_the_run_is_done(
    client: httpx.AsyncClient, tmp_path: Path
):
    """一轮收尾后仍可只找入口智能体聊天：不重开工单、不增加步数。"""
    script = {
        **FULL_SCRIPT,
        "ceo:chat": ["这轮已经收尾了。想闲聊或问结论都可以，我不会重新派活。"],
    }
    scenario = await make_scenario(script, tmp_path)
    run_id, _ = await start_run(client, scenario["agent_profiles"], "把登录页改成暗色主题")
    before = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert before["status"] == "done"

    response = await client.post(
        f"/api/collab/runs/{run_id}/chat", json={"content": "辛苦了，聊两句"}
    )
    assert response.status_code == 200, response.text
    events = await read_sse(response)
    finished = next(event for event in events if event["type"] == "collab.chat.finished")
    assert finished["status"] == "complete"

    after = (await client.get(f"/api/collab/runs/{run_id}")).json()
    # 聊天是旁路：运行状态、工单时间线与步数都不变。
    assert after["status"] == "done"
    assert len(after["steps"]) == len(before["steps"])
    assert after["steps_used"] == before["steps_used"]
    assert [row["sender"] for row in after["chat_messages"]] == ["user", "leader"]

    chat_prompts = [text for text in prompts(scenario["state_dir"]) if "[协作对话]" in text]
    assert chat_prompts and "运行状态：done" in chat_prompts[0]


async def test_chat_advance_renders_decision_card_then_user_confirms(
    client: httpx.AsyncClient, tmp_path: Path
):
    """单一聊天通道：CEO 判断「需要推进」时给出 chat_advance，用户确认后才进工单通道。

    流程：任务已收尾 → 用户说「再加一个导出按钮」→ CEO 判断要推进并附 chat_advance →
    chat.finished 事件带 advance、落库 meta 带 advance（刷新后卡片可恢复）→
    用户点选后把 order 发进 /messages → 根工单重新打开、推进发生。
    """
    chat_reply = (
        "这个需要真正改代码，我建议直接派下去。\n"
        "```chat_advance\n"
        "{\n"
        '  "reason": "新功能要动手实现，聊天里做不了",\n'
        '  "order": "再加一个导出按钮：登录页右上角，点击导出当前配置",\n'
        '  "options": [\n'
        '    {"label": "按建议推进", "value": "再加一个导出按钮：登录页右上角，点击导出当前配置"},\n'
        '    {"label": "我来补充", "value": "", "freeform": true}\n'
        "  ]\n"
        "}\n"
        "```\n"
    )
    reopen = {"action": "done", "remark": "已按新要求交付"}
    script = {**FULL_SCRIPT, "ceo:chat": [chat_reply], "ceo": [HANDOFF, CLOSING, reopen]}
    scenario = await make_scenario(script, tmp_path)
    run_id, _ = await start_run(client, scenario["agent_profiles"], "把登录页改成暗色主题")
    before = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert before["status"] == "done"
    steps_before = before["steps_used"]

    response = await client.post(
        f"/api/collab/runs/{run_id}/chat", json={"content": "再加一个导出按钮"}
    )
    assert response.status_code == 200, response.text
    events = await read_sse(response)
    finished = next(event for event in events if event["type"] == "collab.chat.finished")
    assert finished["status"] == "complete"
    # 事件里带决策卡数据：前端据此渲染「推进任务」卡片。
    advance = finished["advance"]
    assert advance and advance["reason"] == "新功能要动手实现，聊天里做不了"
    assert "导出按钮" in advance["order"]
    assert [item["label"] for item in advance["options"]] == ["按建议推进", "我来补充"]
    assert advance["options"][1]["freeform"] is True
    # 聊天本身仍是旁路：卡片给出来了，但还没有推进任何工单。
    idle = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert idle["status"] == "done"
    assert idle["steps_used"] == steps_before
    # 落库 meta 里有同样的 advance：刷新后前端能恢复这张卡。
    leader = idle["chat_messages"][-1]
    assert leader["meta"]["advance"] == advance
    # 协议块不进正文：用户看到的只是 CEO 的人话。
    assert "chat_advance" not in leader["content"]
    assert "我建议直接派下去" in leader["content"]

    # 用户点「按建议推进」→ order 原样发进工单通道 → 根工单重新打开并推进。
    confirm = await client.post(
        f"/api/collab/runs/{run_id}/messages", json={"content": advance["order"]}
    )
    assert confirm.status_code == 200, confirm.text
    await read_sse(confirm)

    final = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert final["status"] == "done"
    assert final["steps_used"] > steps_before
    user_steps = [step for step in final["steps"] if step["role_key"] == "user"]
    assert any("导出按钮" in step["content"] for step in user_steps)

    # 卡片已经消费：指令进入工单通道后，落库 meta 里不再留 advance，
    # 否则每次刷新都会把这张早就执行过的卡重新弹出来（前端点 × 也挡不住刷新）。
    consumed = final["chat_messages"][-1]["meta"]
    assert "advance" not in consumed
    assert consumed.get("advance_consumed") is True

    # 聊天 prompt 教过 CEO 这套判断规则。
    chat_prompts = [text for text in prompts(scenario["state_dir"]) if "[协作对话]" in text]
    assert chat_prompts and "chat_advance" in chat_prompts[0]
    # 聊天里只属于 CEO 的回复原文不会传给任何工单智能体（用户的推进指令是工单的
    # 合法输入，当然会出现；CEO 的人话不该出现）。
    work_prompts = [text for text in prompts(scenario["state_dir"]) if "[协作对话]" not in text]
    assert all("我建议直接派下去" not in text for text in work_prompts)


async def test_chat_advance_without_options_is_salvaged(
    client: httpx.AsyncClient, tmp_path: Path
):
    """CEO 只给 order 没给 options 时自动补兜底卡；纯聊天不产出 advance。"""
    reply = (
        "这活我来安排。\n"
        "```chat_advance\n"
        '{"reason": "需要改代码", "order": "把导出按钮加到登录页右上角"}\n'
        "```\n"
    )
    script = {**FULL_SCRIPT, "ceo:chat": [reply, "只是聊两句，不用动工单。"]}
    scenario = await make_scenario(script, tmp_path)
    run_id, _ = await start_run(client, scenario["agent_profiles"], "把登录页改成暗色主题")

    first = await client.post(
        f"/api/collab/runs/{run_id}/chat", json={"content": "加个导出按钮"}
    )
    assert first.status_code == 200, first.text
    events = await read_sse(first)
    finished = next(event for event in events if event["type"] == "collab.chat.finished")
    advance = finished["advance"]
    assert advance and advance["order"] == "把导出按钮加到登录页右上角"
    # 漏给 options：自动补出「按建议推进 / 我再想想」，卡片永远可点。
    labels = [item["label"] for item in advance["options"]]
    assert labels == ["按建议推进", "我再想想"]
    assert advance["options"][1]["freeform"] is True

    # 纯聊天（问进度 / 闲聊）：不带 advance，什么都不渲染。
    second = await client.post(
        f"/api/collab/runs/{run_id}/chat", json={"content": "辛苦了，聊两句"}
    )
    assert second.status_code == 200, second.text
    events = await read_sse(second)
    finished = next(event for event in events if event["type"] == "collab.chat.finished")
    assert finished["advance"] is None
    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert "advance" not in detail["chat_messages"][-1]["meta"]


async def test_chat_attachment_lands_in_workspace_and_reaches_the_prompt(
    client: httpx.AsyncClient, tmp_path: Path
):
    """协作聊天也能带附件：文件落到工作目录，提示词里点名让入口智能体去读。"""
    script = {**FULL_SCRIPT, "ceo:chat": ["我看到了，按附件里的要求办。"]}
    scenario = await make_scenario(script, tmp_path)
    workspace = tmp_path / "ws"
    run_id, _ = await start_run(
        client, scenario["agent_profiles"], "把登录页改成暗色主题", run_workspace_dir=str(workspace)
    )

    raw = "补充要求：登录页要支持扫码".encode()
    uploaded = await client.post(
        f"/api/collab/runs/{run_id}/attachments",
        json={"name": "补充说明.txt", "content_base64": base64.b64encode(raw).decode()},
    )
    assert uploaded.status_code == 201, uploaded.text
    info = uploaded.json()
    assert info["rel_path"].startswith("attachments/")
    assert info["size"] == len(raw)
    saved = workspace / info["rel_path"]
    assert saved.is_file() and saved.read_bytes() == raw

    response = await client.post(
        f"/api/collab/runs/{run_id}/chat",
        json={"content": "看下这个", "attachments": [info["rel_path"]]},
    )
    assert response.status_code == 200, response.text
    events = await read_sse(response)
    assert any(event["type"] == "collab.chat.finished" for event in events)

    chat_prompts = [text for text in prompts(scenario["state_dir"]) if "[协作对话]" in text]
    assert chat_prompts, "入口智能体的聊天 prompt 应当被记录下来"
    assert "📎 附件" in chat_prompts[-1]
    assert info["rel_path"] in chat_prompts[-1]

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    user_row = next(row for row in detail["chat_messages"] if row["sender"] == "user")
    assert user_row["content"].startswith("看下这个")
    assert info["rel_path"] in user_row["content"]


async def test_chat_attachment_guards(client: httpx.AsyncClient, tmp_path: Path):
    """附件与正文至少要有一样；附件必须真的在工作目录里、内容必须是 base64。"""
    script = {**FULL_SCRIPT, "ceo:chat": ["收到。"]}
    scenario = await make_scenario(script, tmp_path)
    workspace = tmp_path / "ws"
    run_id, _ = await start_run(
        client, scenario["agent_profiles"], "把登录页改成暗色主题", run_workspace_dir=str(workspace)
    )

    empty = await client.post(f"/api/collab/runs/{run_id}/chat", json={"content": "   "})
    assert empty.status_code == 422

    bad_base64 = await client.post(
        f"/api/collab/runs/{run_id}/attachments",
        json={"name": "x.txt", "content_base64": "不是 base64!!"},
    )
    assert bad_base64.status_code == 422
    assert "base64" in bad_base64.json()["detail"]

    # 文件名里的目录成分被剥掉：跑不出工作目录
    sneaky = await client.post(
        f"/api/collab/runs/{run_id}/attachments",
        json={"name": "../../逃逸.txt", "content_base64": base64.b64encode(b"x").decode()},
    )
    assert sneaky.status_code == 201, sneaky.text
    assert sneaky.json()["name"] == "逃逸.txt"
    assert ".." not in sneaky.json()["rel_path"]

    # 指向工作目录之外的附件一律拒绝
    outside = await client.post(
        f"/api/collab/runs/{run_id}/chat",
        json={"content": "hi", "attachments": ["../../etc/hosts"]},
    )
    assert outside.status_code == 409

    # 未知运行：附件上传给 404，而不是悄悄新建目录
    missing = await client.post(
        "/api/collab/runs/nope/attachments",
        json={"name": "x.txt", "content_base64": base64.b64encode(b"x").decode()},
    )
    assert missing.status_code == 404


async def test_delegate_step_records_target_for_handoff_chain(client: httpx.AsyncClient, full):
    """派发步骤要记住「把活交给了谁」。

    岗位会话靠这个字段还原交接链（CTO 把活交给码农，码农的抽屉里就能看到 CTO 的那句话）。
    """
    run_id, _ = await start_run(client, full["agent_profiles"], "把登录页改成暗色主题")
    for _ in range(400):
        detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
        if detail["status"] != "running":
            break
        await asyncio.sleep(0.05)

    delegates = [step for step in detail["steps"] if step["action"] == "delegate"]
    assert delegates, "这次协作里应当有派发步骤"
    known_keys = {"ceo", "cto", "se", "qa"}
    for step in delegates:
        assert step["target_key"] in known_keys, f"派发目标应当是节点 key：{step['target_key']}"
    # 至少有一条「上游交给下游」的记录（ceo→cto 或 cto→se）
    assert any(step["target_key"] != step["role_key"] for step in delegates)


# --- 「活干完了，上游要看得见」 --------------------------------------------------

#: 一次拆解：根工单拆出一张子工单交给 CTO；子工单完成后必须回到 CEO 手里汇总。
SPLIT_TO_CTO = {
    "action": "delegate",
    "assignee": "cto",
    "mode": "split",
    "ticket": {
        "title": "查一下工作目录",
        "goal": "列出工作目录下的文件",
        "acceptance": ["给出文件清单，或明确说明目录为空"],
    },
}


async def test_child_conclusion_is_carried_back_to_the_upstream_ticket(
    client: httpx.AsyncClient, tmp_path: Path
):
    """子工单收尾后，上游那张工单的 prompt 里必须出现「下游交付」。

    这是线上「反复流转」事故的正面回归：上游过去只看得到自己几次派单记录，
    于是把「活已经干完」误判成「还没收到回报」，一遍遍重派同一件事。
    """
    script = {
        "ceo": [SPLIT_TO_CTO, {"action": "done", "remark": "已汇总：目录为空，没有任何文件"}],
        "cto": [{"action": "done", "remark": "目录为空，没有任何文件（已用 ls -la 与 find 双重确认）"}],
    }
    scenario = await make_scenario(script, tmp_path)
    run_id, _ = await start_run(client, scenario["agent_profiles"], "看看工作目录下有哪些文件")

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "done"

    # done 的 remark 必须落进工单 result —— 否则父工单汇总时只看到一张空壳
    child = next(ticket for ticket in detail["tickets"] if ticket["parent_ticket_id"])
    assert child["state"] == "done"
    assert child["result"]["summary"] == "目录为空，没有任何文件（已用 ls -la 与 find 双重确认）"

    ceo_prompts = prompts_of(prompts(scenario["state_dir"]), "ceo")
    assert len(ceo_prompts) == 2
    assert "下游交付" not in ceo_prompts[0]  # 第一次只是派活
    rollback = ceo_prompts[1]  # 子工单已收尾、工单回到 CEO 手里
    assert "下游交付（我派出去的子工单" in rollback
    assert "《查一下工作目录》" in rollback
    assert "目录为空，没有任何文件（已用 ls -la 与 find 双重确认）" in rollback
    assert "· done" in rollback
    assert "重复派发同一件事" in rollback


async def test_rollback_prompt_shows_downstream_delivery_in_full_flow(
    client: httpx.AsyncClient, full
):
    """完整链路（拆子工单 → 实施 → 审查 → 返工 → 通过）里，汇总方同样拿得到下游交付摘要。"""
    run_id, _ = await start_run(client, full["agent_profiles"], "把登录页改成暗色主题")

    cto_prompts = prompts_of(prompts(full["state_dir"]), "cto")
    assert len(cto_prompts) == 2
    assert "下游交付" not in cto_prompts[0]
    second = cto_prompts[1]
    assert "下游交付" in second
    assert "《实现主题开关与持久化》" in second
    assert "已按审查意见补上对比度" in second  # 下游 submit 的结论

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    child = next(ticket for ticket in detail["tickets"] if ticket["parent_ticket_id"])
    assert child["result"]["summary"] == "已按审查意见补上对比度"


ESCALATION_BLOCKER = {
    "action": "escalate",
    "reason": "本环境禁止执行命令，跑不了运行时验收",
    "remark": "需要用户决定是否授予执行权限",
}

BLOCKED_TITLE = "跑运行时验收"


def allowed_actions_line(prompt: str) -> str:
    """取 prompt 里「允许的动作：…」那一行，用来断言这一轮生效的动作白名单。"""
    match = re.search(r"允许的动作：([^\n]+)", prompt)
    return match.group(1) if match else ""


async def test_escalation_walks_up_to_entry_then_asks_user_and_resumes(
    client: httpx.AsyncClient, tmp_path: Path
):
    """卡住了要能逐级上报：se → cto → ceo，最后由入口智能体 ask_user 交给用户。

    真实事故：UI 卡在「环境禁止执行」后只能把失败写进 done；CTO 手里没有上报动作，
    只好把同一件事改写成「静态核对」又派给码农 —— 阻塞原地转了一圈，用户什么都没收到。
    """
    split_to_cto = {
        "action": "delegate",
        "assignee": "cto",
        "mode": "split",
        "ticket": {"title": "落地首期", "goal": "交付记账系统首期", "acceptance": ["可启动"]},
    }
    split_to_se = {
        "action": "delegate",
        "assignee": "se",
        "mode": "split",
        "ticket": {
            "title": BLOCKED_TITLE,
            "goal": "启动服务并跑通验收 1-12",
            "acceptance": ["有真实命令输出"],
        },
    }
    ask = {
        "action": "ask_user",
        "question": "本环境禁止执行命令，无法跑运行时验收，怎么处理？",
        "options": [
            {"label": "授权后补测", "value": "授权执行权限后补测"},
            {"label": "按现状收尾", "value": "按现状收尾"},
        ],
    }
    script = {
        "ceo": [split_to_cto, ask, {"action": "done", "remark": "已按用户答复收尾"}],
        "cto": [
            split_to_se,
            ESCALATION_BLOCKER,
            {"action": "done", "remark": "子工单已收尾，汇总完成"},
        ],
        "se": [ESCALATION_BLOCKER],
    }
    scenario = await make_scenario(script, tmp_path)
    run_id, events = await start_run(client, scenario["agent_profiles"], "做一个记账系统")

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "waiting_user"
    blocked = {ticket["title"]: ticket for ticket in detail["tickets"]}[BLOCKED_TITLE]
    assert blocked["state"] == "blocked"
    # 上报链整条留在工单上：谁报的、卡在哪、已经过谁
    assert blocked["escalation"]["from"] == "cto"
    assert blocked["escalation"]["hops"] == ["se", "cto"]
    assert "禁止执行命令" in blocked["escalation"]["reason"]
    assert blocked["result"]["question"]
    # 上报本身也是时间线上的一条提示：「谁把什么上报给了谁」用户看得见
    assert any(
        event["type"] == "collab.notice" and "上报给" in (event.get("text") or "")
        for event in events
    )

    # 提示词一路收窄：CTO 那一步不许再往下派，到入口智能体则只剩「转给用户」
    cto_prompts = prompts_of(prompts(scenario["state_dir"]), "cto")
    assert "delegate" not in allowed_actions_line(cto_prompts[-1])
    assert "escalate" in allowed_actions_line(cto_prompts[-1])
    assert "不要再往下派" in cto_prompts[-1]
    ceo_prompts = prompts_of(prompts(scenario["state_dir"]), "ceo")
    assert "escalate" not in allowed_actions_line(ceo_prompts[-1])
    assert "ask_user" in allowed_actions_line(ceo_prompts[-1])

    # 用户回答后，重新打开的必须是**提问的那张工单**（不是根工单），否则回答会石沉大海
    reply = await client.post(
        f"/api/collab/runs/{run_id}/messages", json={"content": "授权执行，重跑验收"}
    )
    assert reply.status_code == 200, reply.text
    await read_sse(reply)
    after = (await client.get(f"/api/collab/runs/{run_id}")).json()
    answered = {ticket["title"]: ticket for ticket in after["tickets"]}[BLOCKED_TITLE]
    assert answered["state"] != "blocked"
    # 用户的话与工单一起交回入口智能体：认的是这张工单的 prompt，不是根工单那条
    resumed = [
        text for text in prompts_of(prompts(scenario["state_dir"]), "ceo") if BLOCKED_TITLE in text
    ][-1]
    assert "授权执行" in resumed
    assert after["status"] == "done"


async def test_escalated_ticket_cannot_be_delegated_back_down(
    client: httpx.AsyncClient, tmp_path: Path
):
    """上抛件不许再往下压：换个说法派回去，运行时也必须明确拦下。"""
    script = {
        "ceo": [
            {
                "action": "delegate",
                "assignee": "cto",
                "mode": "split",
                "ticket": {"title": "落地首期", "goal": "交付首期", "acceptance": ["可启动"]},
            }
        ],
        "cto": [
            {
                "action": "delegate",
                "assignee": "se",
                "mode": "split",
                "ticket": {
                    "title": BLOCKED_TITLE,
                    "goal": "跑运行时验收",
                    "acceptance": ["有真实输出"],
                },
            },
            {"action": "delegate", "assignee": "se", "mode": "handoff"},
        ],
        "se": [ESCALATION_BLOCKER],
    }
    scenario = await make_scenario(script, tmp_path)
    run_id, _ = await start_run(client, scenario["agent_profiles"], "做一个记账系统")

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "waiting_user"
    assert "本轮不允许的动作：delegate" in (detail["error"] or "")
    # 提示词里压根没教它这么做，运行时再兜一道
    cto_prompts = prompts_of(prompts(scenario["state_dir"]), "cto")
    assert "delegate" not in allowed_actions_line(cto_prompts[-1])
