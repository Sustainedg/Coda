from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .. import attachments
from ..agents import get_agent
from ..db import get_session
from ..models import AgentProfile, Conversation, Gateway, Message, TraceEvent, User
from ..runtime import context as context_service
from ..runtime import registry
from ..runtime import skills as skill_service
from ..runtime import task_cards as task_card_service
from ..runtime import titles
from ..runtime.trace import MAX_PAGE_LIMIT
from ..runtime.conversations import (
    is_unread,
    ordered_participants,
    order_by_recent_activity,
    participant_brief,
    participants_by_conversation,
)
from ..runtime.service import (
    ConfigurationError,
    new_conversation,
    pick_default_profile,
    prepare_run,
    regenerate_last,
    resolve_workspace,
    start_run,
)
from ..schemas import (
    AttachmentUpload,
    ConversationCreate,
    ConversationRead,
    ConversationUpdate,
    MessageRead,
    SendMessage,
    TaskCardRead,
    TraceEventRead,
)
from ..transport.sse import sse_stream
from .auth import current_user, owner_id_for, require_owned, require_owned_strict

router = APIRouter(prefix="/conversations", tags=["conversations"])

#: 单个附件的上限：手机上多为截图与文档，8 MB 足够，也免得太大的 base64 撑爆请求体。
MAX_ATTACHMENT_BYTES = attachments.MAX_ATTACHMENT_BYTES


async def _related(session: AsyncSession, conversation: Conversation) -> AgentProfile | None:
    """取对话绑定的智能体（创建时的快照）。

    只给「单条响应」用；列表接口走批量查询，避免查询数随对话数线性增长（N+1）。
    """
    return await session.get(AgentProfile, conversation.agent_profile_id)


def _conversation_read(
    conversation: Conversation,
    profile: AgentProfile | None,
    count: int = 0,
    unread: bool = False,
    participants: list[AgentProfile] | None = None,
) -> ConversationRead:
    return ConversationRead(
        id=conversation.id,
        title=conversation.title,
        agent_profile_id=conversation.agent_profile_id,
        session_id=conversation.session_id,
        workspace_dir=conversation.workspace_dir,
        model=conversation.model,
        archived=conversation.archived,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        agent_name=profile.name if profile else None,
        agent_avatar=profile.avatar if profile else None,
        agent_avatar_preset=profile.avatar_preset if profile else None,
        agent_avatar_file=profile.avatar_file if profile else None,
        agent_type_id=profile.type_id if profile else None,
        agent_model=profile.model if profile else None,
        message_count=count,
        unread=unread,
        participants=[participant_brief(p) for p in (participants or [])],
    )


async def _owned_conversation(conversation_id: str, session: AsyncSession, user: User) -> Conversation:
    """取一条对话并校验归属；不存在或无权一律 404。

    对话内容是本人隐私：用**严格**归属检查，管理员也不能读别人的对话。
    """
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    require_owned_strict(conversation, user)
    return conversation


def _owned_query(stmt, user: User):
    """列表只看自己的对话（管理员也不看别人的：对话内容是隐私）。"""
    return stmt.where(Conversation.owner_id == owner_id_for(user))


@router.get("")
async def list_conversations(
    include_archived: bool = False,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    query = _owned_query(select(Conversation), user)
    if not include_archived:
        query = query.where(Conversation.archived == False)  # noqa: E712
    rows = (await session.execute(query)).scalars().all()

    counts = dict(
        (await session.execute(select(Message.conversation_id, func.count(Message.id)).group_by(Message.conversation_id))).all()
    )
    # 每条对话的最后一条消息：一次批量取回，别为未读判断逐条查（N+1）。
    # 先按对话求 max(created_at)，再把时间相等的那几行取出来。
    latest = {}
    if rows:
        latest_at = (
            select(Message.conversation_id, func.max(Message.created_at).label("latest_at"))
            .where(Message.conversation_id.in_([c.id for c in rows]))
            .group_by(Message.conversation_id)
            .subquery()
        )
        latest_rows = (
            await session.execute(
                select(Message).join(
                    latest_at,
                    (Message.conversation_id == latest_at.c.conversation_id)
                    & (Message.created_at == latest_at.c.latest_at),
                )
            )
        ).scalars().all()
        latest = {message.conversation_id: message for message in latest_rows}
    # 每条对话出现过的「客场」智能体（@ 召唤者）：侧栏据此把头像叠成群聊样式。
    summoned = await participants_by_conversation(session, [c.id for c in rows])
    # 智能体批量取一次：逐个 session.get 会让一次列表请求打出 N 条查询。
    # 召唤者的头像也要一起取回来，所以 id 集合要把他们并进去。
    profile_ids = {c.agent_profile_id for c in rows}
    profile_ids.update(pid for ids in summoned.values() for pid in ids)
    profiles = {
        row.id: row
        for row in (
            await session.execute(select(AgentProfile).where(AgentProfile.id.in_(profile_ids)))
        ).scalars()
    }

    # 排序口径与参与者组装是业务口径，放在 runtime/conversations.py；这里只负责序列化。
    # 「正在回复」的对话整组置顶：一轮长回复期间新开的对话不能把它挤下去，所以排序要
    # 先按运行表分组，组内才比最近活动时间。
    active_runs = registry.list(owner_id=None if user.role == "admin" else user.id)
    running_ids = {run["conversation_id"] for run in active_runs}
    ordered = order_by_recent_activity(rows, latest, running_ids)

    items = []
    for conversation in ordered:
        home = profiles.get(conversation.agent_profile_id)
        participants = ordered_participants(
            conversation, home, summoned.get(conversation.id, []), profiles
        )
        items.append(
            _conversation_read(
                conversation,
                home,
                counts.get(conversation.id, 0),
                is_unread(conversation, latest.get(conversation.id)),
                participants,
            ).model_dump()
        )
    return {"items": items, "active_runs": active_runs}


@router.post("/{conversation_id}/read", status_code=204)
async def mark_conversation_read(
    conversation_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> Response:
    """把这条对话标记为已读（打开 / 正在看时由前端调用）。

    只改 ``last_read_at``，**不动** ``updated_at``：标已读不该让对话跳到列表最前面。
    """
    conversation = await _owned_conversation(conversation_id, session, user)
    conversation.last_read_at = datetime.now(timezone.utc)
    session.add(conversation)
    await session.commit()
    return Response(status_code=204)


@router.post("", status_code=201)
async def create_conversation(
    payload: ConversationCreate,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """新建对话：不指定智能体就用「默认智能体」（卡片右上角那颗五角星）。

    工作目录同理可以留空（按对话自动分配独立沙箱），进去之后在聊天页输入框上方
    那两个小图标里随时改 —— 所以「新建」这一步不再需要先做任何选择。
    """
    if payload.agent_profile_id:
        profile = await session.get(AgentProfile, payload.agent_profile_id)
        if profile is None or profile.deleted_at is not None:
            raise HTTPException(status_code=422, detail="指定的智能体不存在")
        # 不能用别人的智能体建对话
        require_owned(profile, user)
    else:
        profile = await pick_default_profile(
            session, None if user.role == "admin" else user.id
        )
        if profile is None:
            raise HTTPException(
                status_code=422,
                detail="还没有可用的智能体：先到「智能体」页创建，并点卡片右上角的五角星设为默认",
            )
        # 非管理员只能用自己设为默认的智能体；若默认智能体不归他，报错引导去创建
        require_owned(profile, user)
    conversation = new_conversation(profile, payload.title, payload.workspace_dir)
    conversation.owner_id = owner_id_for(user)
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    return _conversation_read(conversation, profile, 0).model_dump()


@router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    conversation = await _owned_conversation(conversation_id, session, user)
    messages = (
        await session.execute(
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at)
        )
    ).scalars().all()
    profile = await _related(session, conversation)
    # 单条响应也要带参与者：聊天页顶栏与侧栏共用同一份「这条对话里有谁」的视图。
    summoned_ids = (await participants_by_conversation(session, [conversation.id])).get(
        conversation.id, []
    )
    extra_ids = [pid for pid in summoned_ids if pid != conversation.agent_profile_id]
    extra_profiles: dict[str, AgentProfile] = {}
    if extra_ids:
        extra_profiles = {
            row.id: row
            for row in (
                await session.execute(select(AgentProfile).where(AgentProfile.id.in_(extra_ids)))
            ).scalars()
        }
    participants = ordered_participants(conversation, profile, summoned_ids, extra_profiles)
    card = await task_card_service.load_card(session, conversation.id)
    return {
        "conversation": _conversation_read(
            conversation, profile, len(messages), participants=participants
        ).model_dump(),
        "messages": [MessageRead.model_validate(m).model_dump() for m in messages],
        "active_run": registry.active_for(conversation_id).snapshot() if registry.active_for(conversation_id) else None,
        # 任务进度卡片：没有卡时为 null。只读，没有任何写接口。
        "task_card": TaskCardRead.model_validate(card).model_dump() if card is not None else None,
    }


@router.get("/{conversation_id}/context")
async def get_conversation_context(
    conversation_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """输入框上方「上下文用量」按钮的数据来源。

    估算口径与自动压缩决策共用 ``runtime.context``：界面只展示后端算好的数，
    不在前端另算一套。压缩状态是进程内记录（重启后回到「尚未压缩」），
    已折叠的摘要与条数则持久化在对话上。
    """
    conversation = await _owned_conversation(conversation_id, session, user)
    profile = await session.get(AgentProfile, conversation.agent_profile_id)
    settings = await context_service.read_settings(session, conversation.owner_id)
    used, measured = await context_service.conversation_usage(
        session, conversation, profile.system_prompt if profile else None
    )
    return context_service.usage_view(used, settings, conversation, measured=measured)


@router.get("/{conversation_id}/trace")
async def list_trace_events(
    conversation_id: str,
    after_seq: int = 0,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """只读分页读取一条对话的轨迹事件（不触发任何运行、不读 ActiveRun 缓冲）。

    排序键是会话内严格递增的 ``seq``；``next_after_seq`` 为空表示没有下一页。
    """
    await _owned_conversation(conversation_id, session, user)
    limit = max(1, min(limit, MAX_PAGE_LIMIT))
    rows = (
        await session.execute(
            select(TraceEvent)
            .where(TraceEvent.conversation_id == conversation_id, TraceEvent.seq > after_seq)
            .order_by(TraceEvent.seq)
            # 多取一条用来判断是否还有下一页，不额外打一次 count。
            .limit(limit + 1)
        )
    ).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        TraceEventRead(
            id=row.id,
            session_id=row.conversation_id,
            owner_id=row.owner_id,
            run_id=row.run_id,
            turn_index=row.turn_index,
            seq=row.seq,
            timestamp=row.created_at,
            type=row.type,
            payload=row.payload or {},
            duration_ms=row.duration_ms,
            error=row.error,
            truncated=row.truncated,
            bytes=row.bytes,
        ).model_dump()
        for row in rows
    ]
    return {
        "items": items,
        "next_after_seq": rows[-1].seq if has_more and rows else None,
    }


@router.patch("/{conversation_id}")
async def update_conversation(
    conversation_id: str,
    payload: ConversationUpdate,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    conversation = await _owned_conversation(conversation_id, session, user)
    data = payload.model_dump(exclude_unset=True)

    # 换智能体 / 换工作目录都要让会话记忆重来：内核的 session 是绑在「智能体 + 工作目录」
    # 上的，留着上一个内核的会话 id 只会把新的一轮带偏（CLI 甚至直接报 resume 失败）。
    scope_changed = False
    agent_changed = False
    if data.get("agent_profile_id") and data["agent_profile_id"] != conversation.agent_profile_id:
        profile = await session.get(AgentProfile, data.pop("agent_profile_id"))
        if profile is None or profile.deleted_at is not None:
            raise HTTPException(status_code=422, detail="指定的智能体不存在")
        require_owned(profile, user)
        conversation.agent_profile_id = profile.id
        scope_changed = True
        agent_changed = True
    if "workspace_dir" in data:
        workspace = (data.pop("workspace_dir") or "").strip()
        if workspace != (conversation.workspace_dir or ""):
            scope_changed = True
        conversation.workspace_dir = workspace or None
    if "model" in data:
        # 换模型不清会话记忆（同一个内核会话可以换模型继续跑），只是下一轮用它。
        # 自定义网关必须守着白名单这条服务端不变量；官方默认链路允许直接填模型名。
        model = (data.pop("model") or "").strip()
        if model:
            profile = await session.get(AgentProfile, conversation.agent_profile_id)
            gateway = (
                await session.get(Gateway, profile.gateway_id)
                if profile is not None and profile.gateway_id
                else None
            )
            if gateway is not None and model not in (gateway.selected_models or []):
                raise HTTPException(status_code=422, detail="模型不在该网关的白名单里")
        conversation.model = model or None
    if scope_changed and registry.active_for(conversation_id):
        raise HTTPException(status_code=409, detail="这条对话正在运行，先停止或等它结束再改")
    if scope_changed:
        conversation.session_id = None
    if agent_changed:
        # 换智能体后模型要回到新智能体的默认（旧模型可能属于另一个网关的白名单）。
        conversation.model = None

    for field, value in data.items():
        if value is not None:
            setattr(conversation, field, value)
    conversation.updated_at = datetime.now(timezone.utc)
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    profile = await _related(session, conversation)
    return _conversation_read(conversation, profile).model_dump()


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> Response:
    conversation = await _owned_conversation(conversation_id, session, user)
    registry.cancel_for_conversation(conversation_id)
    # 用单条 DELETE 显式清空消息（保留「删对话必须清消息」的不变量），比逐行
    # session.delete 少一层往返，也避开 SQLite 驱动对 executemany 行数统计为 0 的误导性告警。
    await session.execute(delete(Message).where(Message.conversation_id == conversation_id))
    await session.delete(conversation)
    await session.commit()
    # 该对话的内核隔离配置目录（含分配技能的副本）与压缩状态一并清掉。
    skill_service.cleanup_conversation(conversation_id)
    context_service.reset_state(conversation_id)
    return Response(status_code=204)


@router.post("/{conversation_id}/session/reset")
async def reset_session(
    conversation_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    conversation = await _owned_conversation(conversation_id, session, user)
    conversation.session_id = None
    # 「清除记忆」= 下一轮从零开始：自动压缩留下的摘要与折叠进度也一并清掉，
    # 否则摘要会像一段幽灵记忆一样继续注进上下文。
    conversation.context_summary = None
    conversation.context_compacted_count = 0
    conversation.context_compacted_at = None
    session.add(conversation)
    await session.commit()
    context_service.reset_state(conversation.id)
    return {"ok": True, "session_id": None}


@router.post("/{conversation_id}/attachments", status_code=201)
async def upload_attachment(
    conversation_id: str,
    payload: AttachmentUpload,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """把附件存进对话的工作目录（`<工作目录>/attachments/`）。

    附件不经过适配器：智能体本来就能读工作目录里的文件，发消息时把相对路径写进提示词即可
    （见 `runtime/service.compose_user_content`），因此每个内核都自动支持。
    """
    conversation = await _owned_conversation(conversation_id, session, user)
    profile = await session.get(AgentProfile, conversation.agent_profile_id)
    if profile is None:
        raise HTTPException(status_code=409, detail="对话绑定的智能体已不存在，无法保存附件")

    try:
        raw = attachments.decode_attachment(payload.content_base64, limit=MAX_ATTACHMENT_BYTES)
        workspace = Path(resolve_workspace(conversation))
        return attachments.store_attachment(workspace, payload.name, raw).to_dict()
    except attachments.AttachmentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    except ConfigurationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.get("/{conversation_id}/files/{rel_path:path}")
async def get_conversation_file(
    conversation_id: str,
    rel_path: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> FileResponse:
    """把工作目录里的一个文件回给浏览器看（附件点开预览、智能体产出的图片等）。

    只读，且**仅限该对话工作目录内部**（`../` 穿越、目录、目录外软链一律拒绝）。附件本就是
    工作目录里的普通文件，所以「预览我上传的图」与「看智能体生成的图」共用这一个出口 ——
    这也是聊天里嵌图能真正显示出来的前提（浏览器取不到工作目录里的本地路径）。
    """
    conversation = await _owned_conversation(conversation_id, session, user)
    try:
        workspace = Path(resolve_workspace(conversation))
        target = attachments.resolve_workspace_file(rel_path, workspace)
    except attachments.AttachmentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    except ConfigurationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return FileResponse(
        target,
        media_type=attachments.guess_media_type(target.name),
        # 内容会随对话进展变化（同名路径可能被重写），只让浏览器短暂缓存，避免看到旧图。
        headers={"Cache-Control": "private, max-age=30"},
    )


def _sse_response(source: AsyncIterator[dict]) -> StreamingResponse:
    return StreamingResponse(
        sse_stream(source),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


def _stream_prepared(prepared) -> StreamingResponse:
    """只负责订阅后台运行的事件。

    运行的执行与 HTTP 连接解耦（``service.start_run``）：客户端刷新或关页面时
    只是这个订阅者离开，运行继续跑完并落库；重新打开对话会走
    ``GET /conversations/{id}/stream`` 从缓冲重放，再接着实时输出。
    """

    async def event_source() -> AsyncIterator[dict]:
        async for event in prepared.run.subscribe():
            yield event

    return _sse_response(event_source())


@router.get("/{conversation_id}/stream")
async def stream_active_run(
    conversation_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> StreamingResponse:
    """订阅一条对话当前正在运行的那一轮（刷新 / 另一台设备实时围观用）。"""
    await _owned_conversation(conversation_id, session, user)
    run = registry.active_for(conversation_id)
    if run is None:
        raise HTTPException(status_code=404, detail="这条对话当前没有正在运行的任务")

    async def event_source() -> AsyncIterator[dict]:
        async for event in run.subscribe():
            yield event

    return _sse_response(event_source())


@router.get("/{conversation_id}/models")
async def conversation_models(
    conversation_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """这条对话可选的模型清单。

    有自定义网关时只能用网关白名单里的模型；官方默认时返回空清单，由前端
    允许直接填写模型名（与「智能体」页的默认模型输入一致）。
    """
    conversation = await _owned_conversation(conversation_id, session, user)
    profile = await session.get(AgentProfile, conversation.agent_profile_id)
    gateway = (
        await session.get(Gateway, profile.gateway_id)
        if profile is not None and profile.gateway_id
        else None
    )
    default_model = profile.model if profile is not None else None
    if not default_model and profile is not None:
        try:
            default_model = get_agent(profile.type_id).resolve_model(None)
        except KeyError:
            default_model = None
    return {
        "current": conversation.model,
        "default": default_model,
        "gateway": gateway.name if gateway is not None else None,
        "models": list(gateway.selected_models) if gateway is not None else [],
    }


@router.post("/{conversation_id}/messages")
async def send_message(
    conversation_id: str,
    payload: SendMessage,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> StreamingResponse:
    await _owned_conversation(conversation_id, session, user)
    # @ 召唤的智能体也是用户资产：启动运行前先过归属校验（与换智能体同一口径）。
    if payload.summon_profile_id:
        summon = await session.get(AgentProfile, payload.summon_profile_id)
        if summon is None or summon.deleted_at is not None:
            raise HTTPException(status_code=422, detail="指定的智能体不存在")
        if not summon.enabled:
            raise HTTPException(status_code=422, detail="指定的智能体已停用")
        require_owned(summon, user)
    try:
        # 采集开关只认后端权威值（当前登录用户的设置），不读请求体任何字段。
        prepared = await prepare_run(
            session, conversation_id, payload, trace_enabled=bool(user.trace_debug)
        )
    except ConfigurationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    start_run(prepared)
    # 这是这条对话的第一句话：丢一个后台任务用「设置」里指定的智能体把标题概括得更准
    # （临时标题已经在 prepare_run 里按首行写好，真跑不出来也不影响使用）。
    if prepared.title_pending:
        titles.schedule(prepared.conversation.id, prepared.conversation.title, prepared.user_message.content)
    return _stream_prepared(prepared)


@router.post("/{conversation_id}/regenerate")
async def regenerate(
    conversation_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> StreamingResponse:
    await _owned_conversation(conversation_id, session, user)
    try:
        prepared = await regenerate_last(
            session, conversation_id, trace_enabled=bool(user.trace_debug)
        )
    except ConfigurationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    start_run(prepared)
    return _stream_prepared(prepared)


@router.post("/{conversation_id}/cancel")
async def cancel_run(
    conversation_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    await _owned_conversation(conversation_id, session, user)
    cancelled = registry.cancel_for_conversation(conversation_id)
    return {"ok": cancelled}
