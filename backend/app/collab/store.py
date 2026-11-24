"""协作域的持久化（只依赖 tables / graph / schemas 与 SQLModel）。

这一层不做编排决策：它负责「把图存下来、把运行与工单读出来」，
所有判断都在 ``engine.py``（M1）与 ``graph.py`` 里。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Sequence

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..models import AgentProfile, new_id, utcnow
from ..settings import get_settings
from . import graph as graph_service
from .schemas import (
    BudgetSpec,
    ChatMessageRead,
    GraphIssue,
    GraphSpec,
    GraphValidation,
    RunCreate,
    RunDetail,
    RunRead,
    RunStatus,
    StepRead,
    TeamCreate,
    TeamRead,
    TeamUpdate,
    TicketRead,
)
from .tables import CollabMessage, CollabRun, CollabStep, CollabTeam, CollabTicket


class TeamNotFound(Exception):
    """协作组不存在。"""


class RunNotFound(Exception):
    """协作运行不存在。"""


class TeamInUse(Exception):
    """协作组下还有运行记录，不能删除（沿用既有 409 语义）。"""


def to_read(team: CollabTeam, known_agent_profile_ids: set[str] | None = None) -> TeamRead:
    """把行转成响应模型，并**每次都重新校验**图（编辑器要能实时看到问题）。"""
    try:
        spec = graph_service.from_storable(team.graph)
    except (ValueError, ValidationError):
        spec = GraphSpec()
        validation = GraphValidation(
            ok=False,
            errors=[
                GraphIssue(
                    code="graph.malformed",
                    message="编排图数据结构损坏，无法解析；请重新保存一次这张图",
                )
            ],
        )
    else:
        validation = graph_service.validate_graph(spec, team.entry_key, known_agent_profile_ids)
    try:
        budget = graph_service.budget_from_storable(team.budget)
    except (ValueError, ValidationError):
        budget = BudgetSpec()
    return TeamRead(
        id=team.id,
        name=team.name,
        description=team.description,
        entry_key=team.entry_key,
        workspace_dir=team.workspace_dir,
        graph=spec,
        budget=budget,
        node_count=len(spec.nodes),
        created_at=team.created_at,
        updated_at=team.updated_at,
        validation=validation,
    )


async def list_teams(
    session: AsyncSession, owner_id: str | None = None
) -> list[CollabTeam]:
    stmt = select(CollabTeam).order_by(CollabTeam.updated_at.desc())
    if owner_id is not None:
        stmt = stmt.where(CollabTeam.owner_id == owner_id)
    return list((await session.execute(stmt)).scalars().all())


async def get_team(session: AsyncSession, team_id: str) -> CollabTeam:
    team = await session.get(CollabTeam, team_id)
    if team is None:
        raise TeamNotFound(f"找不到协作组：{team_id}")
    return team


async def create_team(
    session: AsyncSession, payload: TeamCreate, owner_id: str | None = None
) -> CollabTeam:
    team = CollabTeam(
        name=payload.name.strip(),
        description=payload.description,
        entry_key=payload.entry_key,
        workspace_dir=payload.workspace_dir,
        graph=graph_service.to_storable(payload.graph),
        budget=graph_service.budget_to_storable(payload.budget),
        owner_id=owner_id,
    )
    session.add(team)
    await session.commit()
    await session.refresh(team)
    return team


async def _copy_team(source: CollabTeam, name: str, owner_id: str | None = None) -> CollabTeam:
    return CollabTeam(
        name=name,
        description=source.description,
        entry_key=source.entry_key,
        workspace_dir=source.workspace_dir,
        graph=dict(source.graph or {}),
        budget=dict(source.budget or {}),
        owner_id=owner_id,
    )


async def _pin_unpinned_runs(session: AsyncSession, team_id: str, graph: Any) -> int:
    """把「已经下过工单、但还没有图快照」的老运行钉到 ``graph`` 上（不提交）。

    保存协作图前调用：先把老运行正在用的那张图拍下来，再写新图。
    这样改名 / 删节点不会打断任何已有运行 —— 它们继续按自己的快照跑完。
    还没下过工单的运行不钉：它们本来就该跟着协作组的最新图走。
    """
    if not graph_service.has_graph_snapshot(graph):
        return 0
    runs = (
        await session.execute(select(CollabRun).where(CollabRun.team_id == team_id))
    ).scalars().all()
    if not runs:
        return 0
    # 用子查询而不是把 run.id 列表塞进 IN：SQLite 的绑定参数有 999 个上限。
    team_run_ids = select(CollabRun.id).where(CollabRun.team_id == team_id)
    started = set(
        (
            await session.execute(
                select(CollabTicket.run_id).where(CollabTicket.run_id.in_(team_run_ids))
            )
        ).scalars()
    )
    pinned = 0
    for run in runs:
        if run.id not in started or graph_service.has_graph_snapshot(run.graph):
            continue
        run.graph = deepcopy(graph)
        session.add(run)
        pinned += 1
    return pinned


async def save_team(session: AsyncSession, team_id: str, payload: TeamUpdate) -> CollabTeam:
    """整图保存。语义错误不拦截（允许存草稿），但结构性错误已被 Pydantic 挡在路由层。

    改动协作组**不再**被「有运行记录」拦住：已经下过工单的运行冻结了自己的图，
    之后改 key / 删节点 / 换边只影响还没开跑 / 新发起的运行（老运行按自己的快照跑完）。
    """
    team = await get_team(session, team_id)
    data = payload.model_dump(exclude_unset=True)
    if data.get("name"):
        team.name = str(data["name"]).strip()
    if "description" in data:
        team.description = data["description"]
    if "entry_key" in data:
        team.entry_key = data["entry_key"]
    if "workspace_dir" in data:
        team.workspace_dir = data["workspace_dir"]
    if payload.graph is not None:
        new_graph = graph_service.to_storable(payload.graph)
        # 升级前创建的老运行还没有快照：先拍下「改之前」的图，再覆盖协作组。
        await _pin_unpinned_runs(session, team_id, team.graph)
        team.graph = new_graph
    if payload.budget is not None:
        team.budget = graph_service.budget_to_storable(payload.budget)
    team.updated_at = utcnow()
    session.add(team)
    await session.commit()
    await session.refresh(team)
    return team


async def duplicate_team(
    session: AsyncSession, team_id: str, owner_id: str | None = None
) -> CollabTeam:
    source = await get_team(session, team_id)
    copy = await _copy_team(source, f"{source.name}（副本）"[:120], owner_id=owner_id)
    session.add(copy)
    await session.commit()
    await session.refresh(copy)
    return copy


async def delete_team(session: AsyncSession, team_id: str) -> None:
    team = await get_team(session, team_id)
    runs = (await session.execute(select(CollabRun.id).where(CollabRun.team_id == team_id))).all()
    if runs:
        raise TeamInUse(f"协作组「{team.name}」下还有 {len(runs)} 次运行记录，请先删除这些运行")
    await session.delete(team)
    await session.commit()


async def count_runs(session: AsyncSession, team_id: str | None = None) -> int:
    query = select(CollabRun.id)
    if team_id:
        query = query.where(CollabRun.team_id == team_id)
    return len((await session.execute(query)).all())


async def recover_interrupted(session: AsyncSession) -> int:
    """启动时把残留的 ``running`` 运行与 ``streaming`` 步骤标记为 ``interrupted``。

    进程重启后内存里的运行句柄一定已经失效，因此启动时看到 ``running`` 就是残留；
    工单与步骤原样保留，用户可以在界面上「继续」（铁律 9：绝不删库重建）。
    残留的 ``streaming`` 步骤同样要收尾：否则界面会一直显示「处理中」，点开却看不到任何输出。
    """
    rows: Sequence[CollabRun] = (
        await session.execute(select(CollabRun).where(CollabRun.status == RunStatus.RUNNING.value))
    ).scalars().all()
    for row in rows:
        row.status = RunStatus.INTERRUPTED.value
        row.error = row.error or "服务重启，这次协作被中断；可以点「继续」从当前工单接着跑"
        row.updated_at = utcnow()
        session.add(row)
    steps: Sequence[CollabStep] = (
        await session.execute(select(CollabStep).where(CollabStep.status == "streaming"))
    ).scalars().all()
    for step in steps:
        step.status = "interrupted"
        step.error = step.error or "服务重启，这一步被中断，没有留下输出"
        step.finished_at = utcnow()
        session.add(step)
    if rows or steps:
        await session.commit()
    await mark_chat_interrupted(
        session, None, "服务重启，这条 CEO 回复被中断；可以直接再问一次"
    )
    return len(rows)


async def backfill_run_graphs(session: AsyncSession) -> int:
    """给升级前创建、还没有图快照的运行补一份快照（幂等）。

    老运行此前一直读协作组现图；升级后第一次启动时协作组还没有被改过，
    把现图拍下来就等于冻结了它们正在用的那张图。之后用户改协作组再也影响不到它们。
    """
    teams = {
        team.id: team for team in (await session.execute(select(CollabTeam))).scalars().all()
    }
    # 不用 SQL 的 ``IS NULL`` 过滤：SQLAlchemy 的 JSON 列默认把 Python ``None``
    # 存成 JSON 的 ``null`` 文本，``IS NULL`` 匹配不到；在 Python 侧判空更可靠。
    runs = (await session.execute(select(CollabRun))).scalars().all()
    # 只冻结已经下过工单的老运行；还没开跑的运行继续跟着协作组的最新图。
    started = set((await session.execute(select(CollabTicket.run_id).distinct())).scalars())
    pinned = 0
    for run in runs:
        if run.id not in started or graph_service.has_graph_snapshot(run.graph):
            continue
        team = teams.get(run.team_id)
        if team is None or not graph_service.has_graph_snapshot(team.graph):
            continue
        run.graph = deepcopy(team.graph)
        if not run.entry_key:
            run.entry_key = team.entry_key
        session.add(run)
        pinned += 1
    if pinned:
        await session.commit()
    return pinned


# --- 运行 -------------------------------------------------------------------


def _ticket_read(ticket: CollabTicket) -> TicketRead:
    return TicketRead.model_validate(ticket, from_attributes=True)


def _chat_read(message: CollabMessage) -> ChatMessageRead:
    return ChatMessageRead.model_validate(message, from_attributes=True)


async def list_chat_messages(session: AsyncSession, run_id: str) -> list[ChatMessageRead]:
    """按发生顺序读出入口智能体对话。"""
    rows = (
        await session.execute(
            select(CollabMessage)
            .where(CollabMessage.run_id == run_id)
            .order_by(CollabMessage.sequence, CollabMessage.created_at, CollabMessage.id)
        )
    ).scalars().all()
    return [_chat_read(row) for row in rows]


async def create_run(
    session: AsyncSession, payload: RunCreate, owner_id: str | None = None
) -> CollabRun:
    team = await get_team(session, payload.team_id)
    run_id = new_id()
    # 优先级：本次任务显式指定 > 协作组默认 > 按运行 ID 自动分配
    workspace = (
        payload.workspace_dir
        or team.workspace_dir
        or str(get_settings().workspaces_root / f"collab-{run_id}")
    )
    # 图快照不在这里拍：运行第一次真正推进（下第一张工单）时才冻结。
    # 这样「先建任务、再回去修图」仍然能救回来，只有已经下过工单的运行才受保护。
    run = CollabRun(
        id=run_id,
        team_id=team.id,
        title=(payload.title or "新协作").strip() or "新协作",
        goal="",
        # 建好之后先等用户开口（第一条消息就是这次协作的目标）
        status=RunStatus.WAITING_USER.value,
        entry_key=team.entry_key,
        workspace_dir=workspace,
        budget=dict(team.budget or {}),
        owner_id=owner_id,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


async def get_run(session: AsyncSession, run_id: str) -> CollabRun:
    run = await session.get(CollabRun, run_id)
    if run is None:
        raise RunNotFound(f"找不到协作运行：{run_id}")
    return run


async def list_runs(
    session: AsyncSession,
    team_id: str | None = None,
    limit: int = 100,
    owner_id: str | None = None,
) -> list[CollabRun]:
    query = select(CollabRun).order_by(CollabRun.updated_at.desc()).limit(limit)
    if team_id:
        query = query.where(CollabRun.team_id == team_id)
    if owner_id is not None:
        query = query.where(CollabRun.owner_id == owner_id)
    return list((await session.execute(query)).scalars().all())


async def delete_run(session: AsyncSession, run_id: str) -> None:
    run = await get_run(session, run_id)
    tickets = (
        await session.execute(select(CollabTicket).where(CollabTicket.run_id == run_id))
    ).scalars().all()
    # 先解开父子引用，再删工单（否则父工单被删时子工单会短暂悬空）
    for ticket in tickets:
        ticket.parent_ticket_id = None
    await session.commit()
    for ticket in tickets:
        steps = (
            await session.execute(select(CollabStep).where(CollabStep.ticket_id == ticket.id))
        ).scalars().all()
        for step in steps:
            step.ticket_id = None
        await session.delete(ticket)
    await session.commit()
    await session.delete(run)
    await session.commit()


async def mark_interrupted(session: AsyncSession, run_id: str, reason: str) -> None:
    """叫停或服务重启后，把这次运行连同残留步骤标记为中断（不删数据）。"""
    run = await session.get(CollabRun, run_id)
    if run is None:
        return
    if run.status == RunStatus.RUNNING.value:
        run.status = RunStatus.INTERRUPTED.value
    run.error = run.error or reason
    run.updated_at = utcnow()
    session.add(run)
    steps = (
        await session.execute(
            select(CollabStep).where(CollabStep.run_id == run_id, CollabStep.status == "streaming")
        )
    ).scalars().all()
    for step in steps:
        step.status = "interrupted"
        step.error = step.error or reason
        step.finished_at = utcnow()
        session.add(step)
    await session.commit()


async def mark_chat_interrupted(
    session: AsyncSession, run_id: str | None, reason: str
) -> int:
    """把残留的 ``streaming`` 聊天回复收尾，避免界面永远显示「CEO 正在回复」。"""
    query = select(CollabMessage).where(CollabMessage.status == "streaming")
    if run_id:
        query = query.where(CollabMessage.run_id == run_id)
    rows = (await session.execute(query)).scalars().all()
    for row in rows:
        row.status = "interrupted"
        row.error = row.error or reason
        row.finished_at = utcnow()
        session.add(row)
    if rows:
        await session.commit()
    return len(rows)


async def run_read(session: AsyncSession, run: CollabRun) -> RunRead:
    """运行列表项：只算计数，不把工单与步骤全捞出来。"""
    team = await session.get(CollabTeam, run.team_id)
    states = [
        row[0]
        for row in (
            await session.execute(select(CollabTicket.state).where(CollabTicket.run_id == run.id))
        ).all()
    ]
    # 有快照的运行按自己记的入口；还没开跑（没快照）的运行跟着协作组走。
    if graph_service.has_graph_snapshot(run.graph):
        entry_key = run.entry_key or (team.entry_key if team else None)
    else:
        entry_key = (team.entry_key if team else None) or run.entry_key
    return RunRead(
        id=run.id,
        team_id=run.team_id,
        team_name=team.name if team else None,
        title=run.title,
        goal=run.goal,
        budget=graph_service.budget_from_storable(run.budget),
        status=run.status,
        entry_key=entry_key,
        workspace_dir=run.workspace_dir,
        steps_used=run.steps_used,
        tickets_used=run.tickets_used,
        usage=run.usage or {},
        error=run.error,
        created_at=run.created_at,
        updated_at=run.updated_at,
        ticket_count=len(states),
        open_tickets=sum(
            1
            for state in states
            if state in ("open", "working", "review", "rework", "waiting_children", "blocked")
        ),
    )


def _run_graph_spec(run: CollabRun, team: CollabTeam | None) -> GraphSpec:
    """运行详情里展示的图：就是这次运行实际在用的那张（快照优先）。"""
    raw = graph_service.run_graph_source(run.graph, team.graph if team else {})
    try:
        return graph_service.from_storable(raw)
    except (ValueError, ValidationError):
        return GraphSpec()


async def run_detail(session: AsyncSession, run: CollabRun) -> RunDetail:
    """运行详情：工单、步骤与聊天记录。

    步骤只带**摘要**（``content`` / ``gist``）与 ``parts_count``，不带完整 ``parts``：
    一次长跑的 ``parts``（每次工具调用的入参出参、思考）能到几十 MB，全量返回会让
    「打开协作任务」慢到不可用。完整过程由 ``GET /runs/{id}/steps/{step_id}/parts``
    按需加载（见 :func:`step_parts`）。
    """
    team = await session.get(CollabTeam, run.team_id)
    tickets = (
        await session.execute(
            select(CollabTicket).where(CollabTicket.run_id == run.id).order_by(CollabTicket.created_at)
        )
    ).scalars().all()
    # 步骤摘要列先单独取；parts 仍然要再读一遍（省的是 HTTP 传输体，不是读库）：
    # parts_count / tool_names 必须从 parts 本体数出来，SQLite 里没有现成的统计列。
    step_columns = (
        CollabStep.id,
        CollabStep.run_id,
        CollabStep.ticket_id,
        CollabStep.agent_profile_id,
        CollabStep.role_key,
        CollabStep.duty,
        CollabStep.sequence,
        CollabStep.action,
        CollabStep.target_key,
        CollabStep.status,
        CollabStep.content,
        CollabStep.gist,
        CollabStep.usage,
        CollabStep.error,
        CollabStep.audit,
        CollabStep.started_at,
        CollabStep.finished_at,
    )
    steps = (
        await session.execute(
            select(*step_columns).where(CollabStep.run_id == run.id).order_by(CollabStep.sequence)
        )
    ).all()
    parts_rows = (
        await session.execute(
            select(CollabStep.id, CollabStep.parts).where(CollabStep.run_id == run.id)
        )
    ).all()
    parts_counts: dict[str, int] = {}
    tool_names: dict[str, list[str]] = {}
    for step_id, parts in parts_rows:
        parts_counts[step_id] = len(parts or [])
        names: list[str] = []
        for part in parts or []:
            name = part.get("name")
            if part.get("kind") == "tool_call" and name and name not in names:
                names.append(name)
                if len(names) >= 6:
                    break
        tool_names[step_id] = names
    profile_names = {
        profile.id: profile.name
        for profile in (await session.execute(select(AgentProfile))).scalars().all()
    }
    read = await run_read(session, run)
    return RunDetail(
        **read.model_dump(),
        graph=_run_graph_spec(run, team),
        tickets=[_ticket_read(t) for t in tickets],
        steps=[
            StepRead(
                id=step.id,
                run_id=step.run_id,
                ticket_id=step.ticket_id,
                agent_profile_id=step.agent_profile_id,
                profile_name="你" if step.role_key == "user" else profile_names.get(step.agent_profile_id or ""),
                role_key=step.role_key,
                duty=step.duty,
                sequence=step.sequence,
                action=step.action,
                target_key=step.target_key or "",
                status=step.status,
                content=step.content,
                gist=step.gist,
                parts=[],
                parts_count=parts_counts.get(step.id) or 0,
                tool_names=tool_names.get(step.id) or [],
                usage=step.usage or {},
                error=step.error,
                audit=step.audit or {},
                started_at=step.started_at,
                finished_at=step.finished_at,
            )
            for step in steps
        ],
        chat_messages=await list_chat_messages(session, run.id),
    )


async def step_parts(session: AsyncSession, run_id: str, step_id: str) -> list[dict[str, Any]]:
    """单个步骤的完整过程（懒加载）。步骤不存在或不属于这次运行时抛 :class:`RunNotFound`。"""
    row = (
        await session.execute(
            select(CollabStep.parts).where(CollabStep.id == step_id, CollabStep.run_id == run_id)
        )
    ).first()
    if row is None:
        raise RunNotFound(f"找不到协作步骤：{step_id}")
    return row[0] or []
