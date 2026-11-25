"""智能体接口：人格 + 内核 + 网关 + 模型 + 技能分配。

智能体是用户配置与使用的最小单位（原「角色」与「智能体实例」合并而来），
它引用一个网关（密钥的唯一来源）与一个内核适配器（``type_id``）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .. import avatars
from ..agents import AgentContext, AgentDefinition, get_agent as get_definition
from ..agents.base import Availability
from ..credentials import CredentialError
from ..credentials import reads as credential_reads
from ..db import SessionLocal, get_session
from ..settings import get_settings
from ..models import (
    AgentCredential,
    AgentMcpServer,
    AgentProfile,
    AgentSkill,
    Conversation,
    Credential,
    Gateway,
    McpServer,
    Skill,
    User,
)
from ..runtime import availability as availability_service
from ..runtime import credentials as credential_service
from ..runtime import mcp as mcp_service
from ..runtime import skills as skill_service
from ..runtime.gateway import bind_gateway
from ..schemas import (
    AgentCredentialBrief,
    AgentMcpBrief,
    AgentMcpUpdate,
    AgentProfileCreate,
    AgentProfileRead,
    AgentProfileUpdate,
    AgentSkillBrief,
    AgentSkillsUpdate,
    AgentTestResult,
    AttachmentUpload as AvatarUpload,
)
from .auth import current_user, owner_id_for, require_owned

router = APIRouter(prefix="/agents", tags=["agents"])


def _availability(profile: AgentProfile, statuses: dict[str, Availability]) -> Availability | None:
    try:
        get_definition(profile.type_id)
    except KeyError:
        return None
    return statuses.get(profile.type_id)


async def _gateway_for(profile: AgentProfile, session: AsyncSession) -> Gateway | None:
    if not profile.gateway_id:
        return None
    return await session.get(Gateway, profile.gateway_id)


async def _skill_briefs(
    profile_id: str, session: AsyncSession, owner_id: str | None = None
) -> list[AgentSkillBrief]:
    links = (
        await session.execute(
            select(AgentSkill).where(AgentSkill.agent_profile_id == profile_id).order_by(AgentSkill.position)
        )
    ).scalars().all()
    if not links:
        return []
    ids = [link.skill_id for link in links]
    rows = {
        row.id: row
        for row in (await session.execute(select(Skill).where(Skill.id.in_(ids)))).scalars().all()
    }
    briefs: list[AgentSkillBrief] = []
    for skill_id in ids:
        row = rows.get(skill_id)
        if row is None:
            continue
        # 用户技能库按用户隔离：旧分配关系里指向别人技能的条目不再展示。
        if row.owner_id is not None and row.owner_id != owner_id:
            continue
        briefs.append(
            AgentSkillBrief(
                id=row.id,
                name=row.name,
                type_id=row.type_id,
                source_kind=row.source_kind,
                available=row.available,
            )
        )
    return briefs


async def _mcp_briefs(profile_id: str, session: AsyncSession) -> list[AgentMcpBrief]:
    """智能体挂着的 MCP 服务器摘要（只有名字、标识与开关，没有密钥）。"""
    links = (
        await session.execute(
            select(AgentMcpServer)
            .where(AgentMcpServer.agent_profile_id == profile_id)
            .order_by(AgentMcpServer.position)
        )
    ).scalars().all()
    if not links:
        return []
    ids = [link.mcp_server_id for link in links]
    rows = {
        row.id: row
        for row in (await session.execute(select(McpServer).where(McpServer.id.in_(ids)))).scalars().all()
    }
    briefs: list[AgentMcpBrief] = []
    for mcp_id in ids:
        row = rows.get(mcp_id)
        if row is None:
            continue
        briefs.append(
            AgentMcpBrief(
                id=row.id,
                name=row.name,
                slug=row.slug,
                transport=row.transport,
                type_id=row.type_id,
                available=row.available,
                enabled=row.enabled,
            )
        )
    return briefs


async def _credential_briefs(profile_id: str, session: AsyncSession) -> list[AgentCredentialBrief]:
    """智能体引用到的凭据摘要（只有名字、说明与掩码）。"""
    rows = await credential_service.linked_rows(session, profile_id)
    return [AgentCredentialBrief(**item) for item in credential_reads(rows)]


async def _to_read(
    profile: AgentProfile,
    session: AsyncSession,
    availability: Availability | None = None,
) -> AgentProfileRead:
    type_name = runtime = None
    try:
        definition = get_definition(profile.type_id)
        type_name, runtime = definition.name, definition.runtime.value
    except KeyError:
        availability = None
    gateway = await _gateway_for(profile, session)
    briefs = await _skill_briefs(profile.id, session, profile.owner_id)
    mcp_briefs = await _mcp_briefs(profile.id, session)
    credentials = await _credential_briefs(profile.id, session)
    return AgentProfileRead(
        id=profile.id,
        name=profile.name,
        avatar=profile.avatar,
        avatar_preset=profile.avatar_preset,
        avatar_file=profile.avatar_file,
        tagline=profile.tagline,
        description=profile.description,
        system_prompt=profile.system_prompt,
        type_id=profile.type_id,
        gateway_id=profile.gateway_id,
        account_id=profile.account_id,
        model=profile.model,
        options=profile.options or {},
        enabled=profile.enabled,
        is_default=profile.is_default,
        group_name=profile.group_name or "",
        created_at=profile.created_at,
        updated_at=profile.updated_at,
        type_name=type_name,
        runtime=runtime,
        availability=availability.to_dict() if availability else None,
        gateway_name=gateway.name if gateway else None,
        gateway_base_url=gateway.base_url if gateway else None,
        gateway_kind=gateway.kind if gateway else None,
        gateway_api_key_set=bool(gateway and gateway.api_key_encrypted),
        credentials=credentials,
        skill_ids=[brief.id for brief in briefs],
        skills=briefs,
        mcp_ids=[brief.id for brief in mcp_briefs],
        mcp_servers=mcp_briefs,
    )


async def _has_default(session: AsyncSession, owner_id: str | None) -> bool:
    stmt = select(AgentProfile.id).where(
        AgentProfile.is_default == True,  # noqa: E712
        AgentProfile.deleted_at.is_(None),
    )
    if owner_id is not None:
        stmt = stmt.where(AgentProfile.owner_id == owner_id)
    return (await session.execute(stmt.limit(1))).first() is not None


async def _clear_other_defaults(keep_id: str, session: AsyncSession, owner_id: str | None) -> None:
    """每个用户各自只有一颗五角星：点亮自己的同时把同一用户其它智能体的星摘掉。"""
    stmt = update(AgentProfile).where(AgentProfile.id != keep_id)
    if owner_id is not None:
        stmt = stmt.where(AgentProfile.owner_id == owner_id)
    await session.execute(stmt.values(is_default=False))


async def _promote_default(session: AsyncSession, owner_id: str | None) -> None:
    """默认智能体被删掉后补一颗星，免得「新建对话」突然没得用（按用户分别补）。"""
    if await _has_default(session, owner_id):
        return
    stmt = select(AgentProfile).where(AgentProfile.deleted_at.is_(None))
    if owner_id is not None:
        stmt = stmt.where(AgentProfile.owner_id == owner_id)
    nxt = (
        await session.execute(
            stmt.order_by(AgentProfile.enabled.desc(), AgentProfile.created_at).limit(1)
        )
    ).scalars().first()
    if nxt is None:
        return
    nxt.is_default = True
    session.add(nxt)


async def _active_profile(profile_id: str, session: AsyncSession) -> AgentProfile:
    """取一个**未删除**的智能体；软删除过的一律按不存在处理。"""
    profile = await session.get(AgentProfile, profile_id)
    if profile is None or profile.deleted_at is not None:
        raise HTTPException(status_code=404, detail="智能体不存在")
    return profile


async def _require_gateway(
    gateway_id: str | None, session: AsyncSession, user: User
) -> Gateway | None:
    """智能体引用的网关必须可用：官方链路所有人可选，自定义网关只能是自己的（管理员不限）。"""
    if not gateway_id:
        return None
    gateway = await session.get(Gateway, gateway_id)
    if gateway is None or gateway.deleted_at is not None:
        raise HTTPException(status_code=422, detail="指定的网关不存在")
    if user.role != "admin" and gateway.kind != "official" and gateway.owner_id != user.id:
        raise HTTPException(status_code=422, detail="指定的网关不存在")
    return gateway


def _validate_account(definition: AgentDefinition, account_id: str | None) -> str | None:
    """账号槽位是可选项：只有实现了多账号切换的内核才允许指定。"""
    account = (account_id or "").strip() or None
    if account and not definition.supports_accounts():
        raise HTTPException(
            status_code=422, detail=f"{definition.name} 不支持指定内核账号"
        )
    return account


def _owned_query(stmt, user: User):
    """列表只看自己的智能体；管理员看全部。"""
    if user.role == "admin":
        return stmt
    return stmt.where(AgentProfile.owner_id == user.id)


@router.get("")
async def list_agents(
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    rows = (
        await session.execute(
            _owned_query(
                select(AgentProfile)
                .where(AgentProfile.deleted_at.is_(None))
                .order_by(AgentProfile.created_at),
                user,
            )
        )
    ).scalars().all()
    # 可用性按 type_id 批量取一次，避免「每行都探测一遍」。
    statuses = await availability_service.for_types(row.type_id for row in rows)
    # 已有分组名（去重、排序）：编辑表单的「分组」输入框点开时给这些快捷选项。
    groups = sorted({row.group_name for row in rows if row.group_name})
    return {
        "items": [(await _to_read(row, session, statuses.get(row.type_id))).model_dump() for row in rows],
        "groups": groups,
    }


@router.post("", status_code=201)
async def create_agent(
    payload: AgentProfileCreate,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    try:
        definition = get_definition(payload.type_id)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    gateway = await _require_gateway(payload.gateway_id, session, user)
    # 账号槽位只对「官方默认」链路有意义：自定义网关自带地址与密钥，不看内核登录态。
    account_id = (
        None
        if gateway is not None and gateway.kind != "official"
        else _validate_account(definition, payload.account_id)
    )
    # 账号槽位是机器级资产（清单只对管理员可见），指定它等于替全机选登录态：只允许管理员。
    if account_id and user.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员能指定内核账号")
    target_owner = owner_id_for(user)
    # 不传 = 不用内置头像包（界面按 id 稳定兜底一张），只有真的带了编号才校验形状。
    if payload.avatar_preset is not None and not avatars.is_valid_preset(payload.avatar_preset):
        raise HTTPException(status_code=422, detail="头像包编号不合法")
    profile = AgentProfile(
        name=payload.name,
        type_id=payload.type_id,
        avatar=payload.avatar or "",
        avatar_preset=payload.avatar_preset,
        tagline=payload.tagline,
        description=payload.description,
        system_prompt=payload.system_prompt or "",
        gateway_id=payload.gateway_id,
        account_id=account_id,
        model=payload.model,
        options=definition.coerce_options(payload.options or {}),
        enabled=payload.enabled,
        # 每个用户的第一个智能体自动戴上五角星（新建对话就能直接聊）；显式声明时以显式声明为准
        is_default=bool(payload.is_default) or not await _has_default(session, target_owner),
        group_name=(payload.group_name or "").strip()[:60],
        owner_id=target_owner,
    )
    session.add(profile)
    if profile.is_default:
        await _clear_other_defaults(profile.id, session, target_owner)
    await session.commit()
    await session.refresh(profile)
    if payload.credentials:
        try:
            await credential_service.set_agent_credentials(
                session, profile, payload.credentials, owner_id=owner_id_for(user)
            )
        except CredentialError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    statuses = await availability_service.for_types([profile.type_id])
    return (await _to_read(profile, session, statuses.get(profile.type_id))).model_dump()


@router.get("/{profile_id}")
async def get_agent(
    profile_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    profile = await _active_profile(profile_id, session)
    require_owned(profile, user)
    statuses = await availability_service.for_types([profile.type_id])
    return (await _to_read(profile, session, statuses.get(profile.type_id))).model_dump()


@router.patch("/{profile_id}")
async def update_agent(
    profile_id: str,
    payload: AgentProfileUpdate,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    profile = await _active_profile(profile_id, session)
    require_owned(profile, user)
    data = payload.model_dump(exclude_unset=True)
    type_changed = bool(data.get("type_id")) and data["type_id"] != profile.type_id
    if data.get("type_id"):
        try:
            get_definition(data["type_id"])
        except KeyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    # 最终生效的网关：请求里换了就用新的，没换就沿用当前绑定的那个。
    gateway = (
        await _require_gateway(data["gateway_id"], session, user)
        if "gateway_id" in data
        else await _gateway_for(profile, session)
    )
    official_route = gateway is None or gateway.kind == "official"

    # 换内核时旧账号槽位不再适用：没有显式指定就回到「跟随当前登录」。
    if type_changed and "account_id" not in data:
        data["account_id"] = None
    if not official_route:
        # 自定义网关不看内核登录态：清掉账号槽位，不往库里写残留值
        # （否则下次切回官方链路会意外用上一个没人再选的账号）。
        data["account_id"] = None
    elif "account_id" in data:
        try:
            account_definition = get_definition(data.get("type_id") or profile.type_id)
        except KeyError:
            if data["account_id"]:
                raise HTTPException(
                    status_code=422, detail="该内核已下线，无法指定内核账号"
                ) from None
        else:
            data["account_id"] = _validate_account(account_definition, data["account_id"])
    # 指到别的账号 = 替全机换登录态，只有管理员可以；保持原值 / 清空不拦（普通成员编辑时
    # 前端会把已有的 account_id 原样带回）。
    if (
        data.get("account_id")
        and data["account_id"] != profile.account_id
        and user.role != "admin"
    ):
        raise HTTPException(status_code=403, detail="只有管理员能指定内核账号")
    if data.get("avatar_preset") is not None and not avatars.is_valid_preset(data["avatar_preset"]):
        raise HTTPException(status_code=422, detail="头像包编号不合法")
    credentials: list = data.pop("credentials", None) or []
    if data.get("is_default") is not None:
        # 点星 / 取消星：点亮时把别人的摘掉（全局唯一）
        flag = bool(data.pop("is_default"))
        profile.is_default = flag
        if flag:
            await _clear_other_defaults(profile.id, session, profile.owner_id)
    if "options" in data and data["options"] is not None:
        type_id = data.get("type_id") or profile.type_id
        try:
            data["options"] = get_definition(type_id).coerce_options(data["options"])
        except KeyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    for field, value in data.items():
        if value is not None or field in {
            "gateway_id",
            "account_id",
            "model",
            "tagline",
            "description",
            "avatar_preset",
        }:
            setattr(profile, field, value)
    if data.get("type_id"):
        # 换内核后，原来分配的技能 / 内核专属 MCP 可能不属于新型号，直接丢弃并在响应里体现。
        await _drop_incompatible_skills(profile, session)
        await _drop_incompatible_mcp_servers(profile, session)
    profile.updated_at = datetime.now(timezone.utc)
    session.add(profile)
    await session.commit()
    if "credentials" in payload.model_fields_set:
        try:
            await credential_service.set_agent_credentials(
                session, profile, credentials, owner_id=profile.owner_id or user.id
            )
        except CredentialError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    await session.refresh(profile)
    statuses = await availability_service.for_types([profile.type_id])
    return (await _to_read(profile, session, statuses.get(profile.type_id))).model_dump()


@router.put("/{profile_id}/avatar")
async def upload_agent_avatar(
    profile_id: str,
    payload: AvatarUpload,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """上传 / 替换智能体的自定义头像（优先于 emoji 头像显示）。

    与用户头像同一条管线（``app/avatars``）：只收位图（PNG / JPEG / WebP，≤2 MB），
    文件名 = 智能体 id + 内容摘要，落在 ``<数据目录>/avatars/`` —— 智能体 id 与用户 id
    一样是 32 位 hex，同一套文件名规则直接复用。编辑表单「先建后传」：新建的智能体
    保存后前端立刻补一次上传。
    """
    profile = await _active_profile(profile_id, session)
    require_owned(profile, user)
    try:
        raw, extension = avatars.decode_avatar(payload.content_base64)
    except avatars.AvatarError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    data_dir = get_settings().data_dir
    try:
        stored = avatars.store_avatar(data_dir, profile.id, raw, extension)
    except avatars.AvatarError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    profile.avatar_file = stored.filename
    profile.updated_at = datetime.now(timezone.utc)
    session.add(profile)
    await session.commit()
    statuses = await availability_service.for_types([profile.type_id])
    return (await _to_read(profile, session, statuses.get(profile.type_id))).model_dump()


@router.delete("/{profile_id}/avatar")
async def delete_agent_avatar(
    profile_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """清掉智能体的自定义头像，界面回退到 emoji 头像。"""
    profile = await _active_profile(profile_id, session)
    require_owned(profile, user)
    previous = profile.avatar_file
    profile.avatar_file = None
    profile.updated_at = datetime.now(timezone.utc)
    session.add(profile)
    await session.commit()
    avatars.remove_avatar(get_settings().data_dir, previous)
    statuses = await availability_service.for_types([profile.type_id])
    return (await _to_read(profile, session, statuses.get(profile.type_id))).model_dump()


@router.get("/{profile_id}/avatar")
async def get_agent_avatar(
    profile_id: str,
    user: Annotated[User, Depends(current_user)] = None,
) -> FileResponse:
    """取智能体的自定义头像（任何登录用户都能看 —— 对话气泡里也要显示）。"""
    # 只读头像文件本身，不要求归属：别的用户建的智能体发来的消息也要能显示头像。
    async with SessionLocal() as session:
        profile = await session.get(AgentProfile, profile_id)
    if profile is None or not profile.avatar_file:
        raise HTTPException(status_code=404, detail="该智能体没有自定义头像")
    target = avatars.resolve_avatar(get_settings().data_dir, profile.avatar_file)
    if target is None:
        raise HTTPException(status_code=404, detail="该智能体没有自定义头像")
    return FileResponse(target, media_type=avatars.guess_media_type(target.name))


@router.delete("/{profile_id}", status_code=204)
async def delete_agent(
    profile_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> Response:
    """删除智能体（软删除）。

    智能体常被历史引用（对话外键是 ``RESTRICT``），硬删会连历史一起打断。
    因此统一打 ``deleted_at``：它从所有可选列表里消失，而历史对话仍能显示当时的
    名字与头像、也仍可继续对话。
    """
    profile = await _active_profile(profile_id, session)
    require_owned(profile, user)
    profile.deleted_at = datetime.now(timezone.utc)
    profile.updated_at = profile.deleted_at
    profile.is_default = False
    session.add(profile)
    # 删掉的正是那颗星时，从该用户剩下的智能体里补一颗（还有智能体就永远有一颗）
    await _promote_default(session, profile.owner_id)
    await session.commit()
    return Response(status_code=204)


async def _drop_incompatible_skills(profile: AgentProfile, session: AsyncSession) -> None:
    for link in (
        await session.execute(
            select(AgentSkill).where(AgentSkill.agent_profile_id == profile.id)
        )
    ).scalars().all():
        row = await session.get(Skill, link.skill_id)
        if row is None or row.type_id != profile.type_id:
            await session.delete(link)
    await session.flush()


async def _drop_incompatible_mcp_servers(profile: AgentProfile, session: AsyncSession) -> None:
    """平台通用的 MCP 服务器与内核无关，保留；从某个内核配置扫来的要跟着内核一起换掉。"""
    for link in (
        await session.execute(
            select(AgentMcpServer).where(AgentMcpServer.agent_profile_id == profile.id)
        )
    ).scalars().all():
        row = await session.get(McpServer, link.mcp_server_id)
        if row is None or (row.type_id is not None and row.type_id != profile.type_id):
            await session.delete(link)
    await session.flush()


@router.put("/{profile_id}/mcp-servers")
async def set_agent_mcp_servers(
    profile_id: str,
    payload: AgentMcpUpdate,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """整表替换智能体挂着的 MCP 服务器（界面已禁止选错内核，这里是服务端兜底）。"""
    profile = await _active_profile(profile_id, session)
    require_owned(profile, user)
    try:
        await mcp_service.set_agent_mcp_servers(
            session,
            profile,
            payload.mcp_ids,
            owner_id=None if user.role == "admin" else (profile.owner_id or user.id),
            builtin_owner_id=profile.owner_id or owner_id_for(user),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    statuses = await availability_service.for_types([profile.type_id])
    return (await _to_read(profile, session, statuses.get(profile.type_id))).model_dump()


@router.put("/{profile_id}/skills")
async def set_agent_skills(
    profile_id: str,
    payload: AgentSkillsUpdate,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """整表替换智能体分配到的技能（界面已禁止选错类型，这里是服务端兜底）。"""
    profile = await _active_profile(profile_id, session)
    require_owned(profile, user)
    try:
        await skill_service.set_agent_skills(session, profile, payload.skill_ids)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    statuses = await availability_service.for_types([profile.type_id])
    return (await _to_read(profile, session, statuses.get(profile.type_id))).model_dump()


@router.post("/{profile_id}/duplicate", status_code=201)
async def duplicate_agent(
    profile_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    profile = await _active_profile(profile_id, session)
    require_owned(profile, user)
    # 复制出的智能体归当前用户：引用的私有网关 / 凭据若不属于他，就不要带过去。
    gateway_id = profile.gateway_id
    if gateway_id:
        gateway = await session.get(Gateway, gateway_id)
        if (
            gateway is None
            or gateway.deleted_at is not None
            or (gateway.kind != "official" and gateway.owner_id != user.id)
        ):
            gateway_id = None
    # 刻意不复制 account_id：账号槽位是机器级资产，普通成员复制管理员的智能体时不能
    # 顺带继承「替全机选登录态」的能力（否则管理员设的绑定就被复制绕过了）。
    clone = AgentProfile(
        name=f"{profile.name} 副本",
        type_id=profile.type_id,
        avatar=profile.avatar,
        avatar_preset=profile.avatar_preset,
        tagline=profile.tagline,
        description=profile.description,
        system_prompt=profile.system_prompt,
        gateway_id=gateway_id,
        model=profile.model,
        options=dict(profile.options or {}),
        enabled=profile.enabled,
        group_name=profile.group_name or "",
        owner_id=owner_id_for(user),
    )
    session.add(clone)
    await session.commit()
    await session.refresh(clone)
    links = (
        await session.execute(
            select(AgentSkill)
            .where(AgentSkill.agent_profile_id == profile.id)
            .order_by(AgentSkill.position)
        )
    ).scalars().all()
    for link in links:
        skill = await session.get(Skill, link.skill_id)
        # 用户技能库按用户隔离：复制别人的智能体时，不能把他库里的私有技能带过来。
        if skill is None or (skill.owner_id is not None and skill.owner_id != clone.owner_id):
            continue
        session.add(
            AgentSkill(agent_profile_id=clone.id, skill_id=link.skill_id, position=link.position)
        )
    for link in (
        await session.execute(
            select(AgentMcpServer)
            .where(AgentMcpServer.agent_profile_id == profile.id)
            .order_by(AgentMcpServer.position)
        )
    ).scalars().all():
        server = await session.get(McpServer, link.mcp_server_id)
        if server is None or server.deleted_at is not None:
            continue
        if server.type_id is None and server.owner_id != owner_id_for(user):
            continue
        session.add(
            AgentMcpServer(
                agent_profile_id=clone.id, mcp_server_id=link.mcp_server_id, position=link.position
            )
        )
    for link in (
        await session.execute(
            select(AgentCredential)
            .where(AgentCredential.agent_profile_id == profile.id)
            .order_by(AgentCredential.position)
        )
    ).scalars().all():
        credential = await session.get(Credential, link.credential_id)
        if (
            credential is None
            or credential.deleted_at is not None
            or credential.owner_id != owner_id_for(user)
        ):
            continue
        session.add(
            AgentCredential(
                agent_profile_id=clone.id, credential_id=link.credential_id, position=link.position
            )
        )
    await session.commit()
    statuses = await availability_service.for_types([clone.type_id])
    return (await _to_read(clone, session, statuses.get(clone.type_id))).model_dump()


@router.post("/{profile_id}/test")
async def test_agent(
    profile_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """连通性测试：实时探测运行环境与网关（用户主动触发，走线程池，不冻住事件循环）。

    注意：连通性探测按内核**当前登录的账号**发起，不会切换到智能体指定的账号 ——
    探测本身不该改动机器登录态；指定账号是否可用请到「设置 → 内核管理」查看。
    """
    profile = await _active_profile(profile_id, session)
    require_owned(profile, user)
    try:
        definition = get_definition(profile.type_id)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    gateway = await _gateway_for(profile, session)

    probed = await availability_service.probe(definition)
    checks = [
        {"name": "适配器", "ok": True, "detail": f"{definition.name}（{definition.runtime.value}）"},
        {
            "name": "运行环境",
            "ok": bool(probed and probed.installed),
            "detail": (probed or availability_service.cheap_availability(definition)).detail,
        },
        {
            "name": "网关",
            "ok": bool(gateway),
            "detail": f"{gateway.name}（{gateway.base_url}）" if gateway else "未选择网关",
        },
    ]
    binding = bind_gateway(definition, gateway)
    ctx = AgentContext(
        agent_id=profile.id,
        agent_name=profile.name,
        options=definition.coerce_options(profile.options or {}),
        gateway_url=binding.url,
        api_key=binding.api_key,
        auth_style=binding.auth_style,
        model=profile.model or definition.default_model,
        system_prompt=None,
        prompt="ping",
    )
    ok, message = await definition.probe(ctx)
    checks.append({"name": "连通性", "ok": ok, "detail": message})
    return AgentTestResult(ok=all(c["ok"] for c in checks), message=message, checks=checks).model_dump()

__all__ = ["router"]
