"""协作域的 HTTP 接口（薄层：只做参数校验、状态码与序列化）。

编排逻辑在 ``engine.py``（M1 落地），图的判断在 ``graph.py``。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .. import attachments
from ..auth_deps import current_user, owner_id_for, require_owned
from ..db import SessionLocal, get_session
from ..models import AgentProfile, McpServer, Skill, User, utcnow
from ..transport.sse import sse_stream
from .bus import bus, chat_bus
from . import engine, execution, graph as graph_service, store
from .schemas import (
    GraphSpec,
    RunAttachmentUpload,
    RunBudgetPatch,
    RunCreate,
    RunDetail,
    RunMessage,
    RunRead,
    StepPartsRead,
    TeamCreate,
    TeamRead,
    TeamUpdate,
    TeamValidateIn,
)
from .tables import CollabRun, CollabStep, CollabTicket

router = APIRouter(prefix="/collab", tags=["collab"])


def _conflict(exc: store.TeamInUse) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


async def _known_agent_profile_ids(session: AsyncSession, user: User | None = None) -> set[str]:
    """把「智能体是否还存在 / 是否归当前用户」这类事实注入纯函数。

    非管理员只返回自己的智能体：图里引用了别人的节点会被校验标成「不存在 / 无权」，
    保存与运行时同样会拦截（见 :func:`_assert_graph_agents_owned`）。
    """
    stmt = select(AgentProfile.id)
    if user is not None and user.role != "admin":
        stmt = stmt.where(AgentProfile.owner_id == user.id)
    rows = (await session.execute(stmt)).all()
    return {row[0] for row in rows}


async def _assert_graph_agents_owned(
    session: AsyncSession, user: User, graph: GraphSpec
) -> None:
    """拒绝让协作图引用别人的智能体：否则运行时会用到对方的网关密钥 / 凭据。"""
    if user.role == "admin":
        return
    owned = await _known_agent_profile_ids(session, user)
    for node in graph.nodes:
        if node.agent_profile_id not in owned:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"节点「{node.label or node.key}」引用了不属于你的智能体，"
                    "请重新选择自己的智能体"
                ),
            )


async def _assert_graph_refs_owned(
    session: AsyncSession, user: User, graph: GraphSpec
) -> None:
    """拒绝让协作图引用别人的技能 / MCP：节点级覆盖会把对方服务器的密钥带进一次运行。

    可见口径与列表一致（见 ``runtime/skills.py`` 与 ``runtime/mcp.py``）：共享技能、内核配置
    扫来的共享 MCP 人人可用；平台通用技能 / 通用 MCP 与内置 MCP 按用户隔离（内置项各用户各一份）。
    """
    if user.role == "admin":
        return
    owner_id = user.id
    skill_ids = {
        row[0]
        for row in (
            await session.execute(
                select(Skill.id).where(or_(Skill.owner_id.is_(None), Skill.owner_id == owner_id))
            )
        ).all()
    }
    mcp_ids = {
        row[0]
        for row in (
            await session.execute(
                select(McpServer.id).where(
                    or_(McpServer.owner_id == owner_id, McpServer.type_id.is_not(None))
                )
            )
        ).all()
    }
    for node in graph.nodes:
        for field, allowed, label in (
            ("skill_ids", skill_ids, "技能"),
            ("mcp_ids", mcp_ids, "MCP 服务器"),
        ):
            value = getattr(node, field, None)
            if value is None:
                continue
            for ref in value:
                if ref not in allowed:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"节点「{node.label or node.key}」引用了"
                            f"不属于你的{label}，请重新选择自己的"
                        ),
                    )


async def _owned_team(team_id: str, session: AsyncSession, user: User):
    try:
        team = await store.get_team(session, team_id)
    except store.TeamNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    require_owned(team, user)
    return team


async def _owned_run(run_id: str, session: AsyncSession, user: User):
    try:
        run = await store.get_run(session, run_id)
    except store.RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    require_owned(run, user)
    return run


@router.get("/teams")
async def list_teams(
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    owner_id = None if user.role == "admin" else user.id
    rows = await store.list_teams(session, owner_id=owner_id)
    known = await _known_agent_profile_ids(session, user)
    return {"items": [store.to_read(row, known) for row in rows]}


@router.post("/teams", status_code=201)
async def create_team(
    payload: TeamCreate,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> TeamRead:
    await _assert_graph_agents_owned(session, user, payload.graph)
    await _assert_graph_refs_owned(session, user, payload.graph)
    team = await store.create_team(session, payload, owner_id=owner_id_for(user))
    return store.to_read(team, await _known_agent_profile_ids(session, user))


@router.get("/teams/{team_id}")
async def get_team(
    team_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> TeamRead:
    team = await _owned_team(team_id, session, user)
    return store.to_read(team, await _known_agent_profile_ids(session, user))


@router.put("/teams/{team_id}")
async def save_team(
    team_id: str,
    payload: TeamUpdate,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> TeamRead:
    await _owned_team(team_id, session, user)
    if payload.graph is not None:
        await _assert_graph_agents_owned(session, user, payload.graph)
        await _assert_graph_refs_owned(session, user, payload.graph)
    try:
        team = await store.save_team(session, team_id, payload)
    except store.TeamNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return store.to_read(team, await _known_agent_profile_ids(session, user))


@router.post("/teams/{team_id}/validate")
async def validate_team(
    team_id: str,
    payload: TeamValidateIn | None = None,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """只校验不保存：编辑器每改一笔就调一次，用来就地提示问题。"""
    team = await _owned_team(team_id, session, user)

    body = payload or TeamValidateIn()
    if body.graph is not None:
        spec: GraphSpec = body.graph
    else:
        try:
            spec = graph_service.from_storable(team.graph)
        except ValueError:
            spec = GraphSpec()
    entry_key = body.entry_key if body.entry_key is not None else team.entry_key
    known = await _known_agent_profile_ids(session, user)
    return graph_service.validate_graph(spec, entry_key, known).model_dump()


@router.post("/teams/{team_id}/duplicate", status_code=201)
async def duplicate_team(
    team_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> TeamRead:
    await _owned_team(team_id, session, user)
    try:
        team = await store.duplicate_team(session, team_id, owner_id=owner_id_for(user))
    except store.TeamNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return store.to_read(team, await _known_agent_profile_ids(session, user))


@router.delete("/teams/{team_id}", status_code=204)
async def delete_team(
    team_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> Response:
    await _owned_team(team_id, session, user)
    try:
        await store.delete_team(session, team_id)
    except store.TeamNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except store.TeamInUse as exc:
        raise _conflict(exc) from None
    return Response(status_code=204)


# --- 运行 -------------------------------------------------------------------


async def _list_run_reads(
    session: AsyncSession, team_id: str | None, owner_id: str | None
) -> list[RunRead]:
    rows = await store.list_runs(session, team_id, owner_id=owner_id)
    return [await store.run_read(session, row) for row in rows]


@router.get("/runs")
async def list_runs(
    team_id: str | None = None,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    owner_id = None if user.role == "admin" else user.id
    return {"items": [item.model_dump() for item in await _list_run_reads(session, team_id, owner_id)]}


@router.get("/active-runs")
async def active_runs(
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """本进程里正在跑的协作智能体（工单步骤 + 入口智能体的对话回复）。

    设置页的「正在运行的智能体」要一并显示它们：协作的运行登记在协作域自己这里，
    对话域那份运行表（`GET /api/system/overview` 的 `active_runs`）看不到。
    汇总接口留在各自域的边界上，所以这里由协作域自己给一份，并补上运行标题（界面好认）。
    """
    items = execution.active_runs()
    if items:
        ids = {item["run_id"] for item in items}
        stmt = select(CollabRun).where(CollabRun.id.in_(ids))
        if user.role != "admin":
            stmt = stmt.where(CollabRun.owner_id == user.id)
        rows = (await session.execute(stmt)).scalars().all()
        allowed = {row.id for row in rows}
        if user.role != "admin":
            # 非管理员只看自己的运行；否则会把别人的岗位名 / 运行 id 也列出来。
            items = [item for item in items if item["run_id"] in allowed]
        titles = {row.id: row.title for row in rows}
        for item in items:
            item["title"] = titles.get(item["run_id"])
    return {"items": items}


@router.post("/runs", status_code=201)
async def create_run(
    payload: RunCreate,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> RunRead:
    # 校验协作组归属：不能用别人的协作组建运行
    team = await _owned_team(payload.team_id, session, user)
    try:
        graph = graph_service.from_storable(team.graph)
    except ValueError:
        graph = GraphSpec()
    # 老库 / 管理员建的图里也可能残留别人的智能体 / 技能 / MCP，运行前再拦一次。
    await _assert_graph_agents_owned(session, user, graph)
    await _assert_graph_refs_owned(session, user, graph)
    try:
        run = await store.create_run(session, payload, owner_id=owner_id_for(user))
    except store.TeamNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return await store.run_read(session, run)


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> RunDetail:
    run = await _owned_run(run_id, session, user)
    detail = await store.run_detail(session, run)
    detail.chat_active = chat_bus.is_running(run_id) or execution.is_chat_running(run_id)
    return detail


@router.get("/runs/{run_id}/steps/{step_id}/parts")
async def get_step_parts(
    run_id: str,
    step_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> StepPartsRead:
    """单个步骤的完整过程（工具调用 / 思考 / 结果）。

    详情接口只带 ``parts_count``，不携带 ``parts`` 本体——长跑一次能积累几十 MB 的
    工具流水，全量返回会让打开详情慢到不可用。前端在用户点开「过程」折叠块时
    才调这里，按需取这一步的 ``parts``。
    """
    await _owned_run(run_id, session, user)
    try:
        parts = await store.step_parts(session, run_id, step_id)
    except store.RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return StepPartsRead(step_id=step_id, run_id=run_id, parts=parts)


@router.delete("/runs/{run_id}", status_code=204)
async def delete_run(
    run_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> Response:
    await _owned_run(run_id, session, user)
    execution.cancel_run(run_id)
    bus.cancel(run_id)
    chat_bus.cancel(run_id)
    try:
        await store.delete_run(session, run_id)
    except store.RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return Response(status_code=204)


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    run = await _owned_run(run_id, session, user)
    execution.cancel_run(run_id)
    cancelled = bus.cancel(run_id)
    chat_cancelled = chat_bus.cancel(run_id)
    if run.status == "running":
        await store.mark_interrupted(session, run_id, "已停止")
    await store.mark_chat_interrupted(session, run_id, "你停止了这次对话")
    return {"ok": True, "cancelled": cancelled or chat_cancelled}


async def _drive_turn(prepared: engine.PreparedTurn) -> AsyncIterator[dict]:
    """后台推进一个 turn：自己开数据库会话，生命周期与 HTTP 请求无关。"""
    async with SessionLocal() as session:
        run = await session.get(CollabRun, prepared.run.id)
        if run is not None:
            prepared.run = run
        async for event in engine.consume_turn(session, prepared):
            yield event.to_dict()


def _sse(source: AsyncIterator[dict]) -> StreamingResponse:
    """把事件源包成 SSE 响应（心跳与 ``stream.closed`` 由传输层补）。"""
    async def guarded() -> AsyncIterator[str]:
        async for chunk in sse_stream(source):
            yield chunk

    return StreamingResponse(
        guarded(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


def _stream_turn(prepared: engine.PreparedTurn) -> StreamingResponse:
    """启动后台推进并返回 SSE。

    推进跑在独立任务里，客户端断开只会退订、**不会**打断它；重新打开页面用
    ``GET /runs/{id}/stream`` 订阅同一份实时事件即可续看。
    """
    run_id = prepared.run.id
    bus.start(run_id, lambda: _drive_turn(prepared))
    return _sse(bus.subscribe(run_id))


async def _drive_chat(prepared: engine.PreparedChat) -> AsyncIterator[dict]:
    """后台执行一次 CEO 回复：与主流程使用各自独立的数据库会话。"""
    async with SessionLocal() as session:
        run = await session.get(CollabRun, prepared.run.id)
        if run is not None:
            prepared.run = run
        async for event in engine.consume_chat(session, prepared):
            yield event.to_dict()


def _stream_chat(prepared: engine.PreparedChat) -> StreamingResponse:
    run_id = prepared.run.id
    chat_bus.start(run_id, lambda: _drive_chat(prepared))
    return _sse(chat_bus.subscribe(run_id))


@router.get("/runs/{run_id}/stream")
async def stream_run(
    run_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> StreamingResponse:
    """订阅一次运行的实时事件：用于断开后重连，或在另一台设备上实时围观。"""
    await _owned_run(run_id, session, user)
    return _sse(bus.subscribe(run_id))


@router.post("/runs/{run_id}/messages")
async def send_message(
    run_id: str,
    payload: RunMessage,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> StreamingResponse:
    await _owned_run(run_id, session, user)
    if bus.is_running(run_id):
        raise HTTPException(status_code=409, detail="这次协作正在推进中，请稍候（可直接围观实时输出）")
    if chat_bus.is_running(run_id) or execution.is_chat_running(run_id):
        raise HTTPException(status_code=409, detail="CEO 正在回复你的上一条消息，请稍等它说完")
    try:
        prepared = await engine.prepare_turn(session, run_id, payload.content, use_advance=payload.use_advance)
    except store.RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except engine.TurnError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _stream_turn(prepared)


@router.get("/runs/{run_id}/chat")
async def list_chat(
    run_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """入口智能体对话历史；运行推进期间也可以随时读取。"""
    await _owned_run(run_id, session, user)
    return {"items": [item.model_dump() for item in await store.list_chat_messages(session, run_id)]}


@router.get("/runs/{run_id}/chat/stream")
async def stream_chat(
    run_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> StreamingResponse:
    """订阅正在生成的 CEO 回复；断线重连时先补发本轮已产生的事件。"""
    await _owned_run(run_id, session, user)
    return _sse(chat_bus.subscribe(run_id))


@router.post("/runs/{run_id}/chat")
async def send_chat(
    run_id: str,
    payload: RunMessage,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> StreamingResponse:
    """和入口智能体聊天：**不受**主流程是否正在推进影响。"""
    await _owned_run(run_id, session, user)
    if not (payload.content or "").strip() and not payload.attachments:
        raise HTTPException(status_code=422, detail="请先输入想对 CEO 说的话，或附上一个文件")
    if chat_bus.is_running(run_id) or execution.is_chat_running(run_id):
        raise HTTPException(status_code=409, detail="CEO 正在回复上一条消息，请稍等它说完")
    try:
        prepared = await engine.prepare_chat(session, run_id, payload.content, payload.attachments)
    except engine.ChatError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _stream_chat(prepared)


@router.post("/runs/{run_id}/attachments", status_code=201)
async def upload_run_attachment(
    run_id: str,
    payload: RunAttachmentUpload,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """把附件存进这次协作的工作目录（`<工作目录>/attachments/`）。

    与对话附件同一套实现（:mod:`app.attachments`）：文件落到工作目录里，发消息时把路径
    写进提示词，因此任何能读工作目录的内核都自动支持。
    """
    run = await _owned_run(run_id, session, user)
    try:
        raw = attachments.decode_attachment(payload.content_base64)
        workspace = execution.ensure_workspace(run)
        stored = attachments.store_attachment(workspace, payload.name, raw)
    except attachments.AttachmentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    except execution.StepError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return stored.to_dict()


@router.get("/runs/{run_id}/files/{rel_path:path}")
async def get_run_file(
    run_id: str,
    rel_path: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> FileResponse:
    """把这次协作工作目录里的一个文件回给浏览器看（附件预览、岗位产出的图片等）。

    只读，且仅限工作目录内部（与对话域同一个 `app.attachments.resolve_workspace_file` 守卫）；
    路径不存在时不创建目录，直接 4xx。
    """
    run = await _owned_run(run_id, session, user)
    try:
        target = attachments.resolve_workspace_file(rel_path, execution.workspace_root(run))
    except attachments.AttachmentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    return FileResponse(
        target,
        media_type=attachments.guess_media_type(target.name),
        headers={"Cache-Control": "private, max-age=30"},
    )


@router.post("/runs/{run_id}/budget")
async def patch_run_budget(
    run_id: str,
    payload: RunBudgetPatch,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> RunRead:
    """运行中调高预算：步数/工单数/token 超限挂起后，调高上限再点「继续」就能救回来。

    只允许调大（新值必须大于当前已用量），不允许把预算压到已用之下——
    那样改完立刻又挂起，没有意义还容易误操作。
    """
    if bus.is_running(run_id) or execution.is_running(run_id):
        raise HTTPException(status_code=409, detail="这次协作正在推进中，请等它挂起或完成后再调预算")
    run = await _owned_run(run_id, session, user)

    budget = graph_service.budget_from_storable(run.budget)
    steps_used = run.steps_used or 0
    tickets_used = run.tickets_used or 0
    tokens_used = sum(
        v for v in (run.usage or {}).values() if isinstance(v, (int, float))
    )
    if payload.max_steps is not None:
        if payload.max_steps <= steps_used:
            raise HTTPException(
                status_code=409,
                detail=f"步数上限只能调到大于当前已用步数（已用 {steps_used} 步）",
            )
        budget.max_steps = payload.max_steps
    if payload.max_tickets is not None:
        if payload.max_tickets <= tickets_used:
            raise HTTPException(
                status_code=409,
                detail=f"工单上限只能调到大于当前已用工单数（已用 {tickets_used} 张）",
            )
        budget.max_tickets = payload.max_tickets
    if payload.max_tokens is not None:
        if payload.max_tokens and payload.max_tokens <= tokens_used:
            raise HTTPException(
                status_code=409,
                detail=f"token 上限只能调到大于当前已用量（已用 {int(tokens_used)}）或设为 0（不限）",
            )
        budget.max_tokens = payload.max_tokens
    if payload.timeout_seconds is not None:
        budget.timeout_seconds = payload.timeout_seconds
    if payload.step_timeout_seconds is not None:
        budget.step_timeout_seconds = payload.step_timeout_seconds
    if payload.max_parallel is not None:
        # 并行上限不涉及「不能压到已用量之下」：随时可调（含调回 1 = 串行）
        budget.max_parallel = payload.max_parallel

    run.budget = graph_service.budget_to_storable(budget)
    # 预算超限的挂起原因已经不再成立，清掉它让「继续」按钮语义干净。
    if run.status == "waiting_user" and run.error and (
        "上限" in run.error or "运行时限" in run.error
    ):
        run.error = None
    run.updated_at = utcnow()
    session.add(run)
    await session.commit()
    return await store.run_read(session, run)


@router.post("/runs/{run_id}/resume")
async def resume_run(
    run_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> StreamingResponse:
    """从中断处继续（不追加用户消息）。"""
    await _owned_run(run_id, session, user)
    if bus.is_running(run_id):
        raise HTTPException(status_code=409, detail="这次协作正在推进中，请稍候（可直接围观实时输出）")
    if chat_bus.is_running(run_id) or execution.is_chat_running(run_id):
        raise HTTPException(status_code=409, detail="CEO 正在回复你的上一条消息，请稍等它说完")
    try:
        prepared = await engine.prepare_turn(session, run_id, None)
    except store.RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except engine.TurnError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _stream_turn(prepared)


@router.post("/steps/{step_id}/retry")
async def retry_step(
    step_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> RunRead:
    """把这一步所在的工单放回队列，随后用 ``/runs/{id}/resume`` 重跑。

    典型场景：产出不符合工单协议导致运行挂起 —— 用户改好剧本/模型后点「重试」。
    """
    step = await session.get(CollabStep, step_id)
    if step is None:
        raise HTTPException(status_code=404, detail="找不到这一步")
    await _owned_run(step.run_id, session, user)
    try:
        run = await store.get_run(session, step.run_id)
    except store.RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    if step.ticket_id:
        ticket = await session.get(CollabTicket, step.ticket_id)
        if ticket is not None and ticket.state == "blocked":
            ticket.state = "open"
            ticket.updated_at = utcnow()
            session.add(ticket)
    run.status = "waiting_user"
    run.error = None
    run.updated_at = utcnow()
    session.add(run)
    await session.commit()
    return await store.run_read(session, run)
