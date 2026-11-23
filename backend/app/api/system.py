from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from pydantic import BaseModel
from sqlmodel import select

from datetime import datetime, timezone
from typing import Annotated

from .. import VERSION_HISTORY, __version__
from ..agents import all_agents
from ..db import SessionLocal
from ..models import AgentProfile, Conversation, PlatformSetting, User
from ..runtime import availability as availability_service
from ..runtime import context as context_service
from ..runtime import registry
from ..runtime import titles
from ..runtime.service import pick_default_profile
from ..settings import get_settings
from .auth import current_user, owner_id_for, require_admin, require_owned_strict

router = APIRouter(tags=["system"])


class ProxySettings(BaseModel):
    """网络代理设置（注入到安装 / 更新等子进程的环境变量里）。"""

    proxy_url: str | None = None


async def get_proxy_url() -> str | None:
    """从数据库读取代理地址；不存在或为空则返回 None。"""
    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(PlatformSetting).where(PlatformSetting.key == "proxy_url")
            )
        ).scalar_one_or_none()
        return row.value if row and row.value else None


@router.get("/system/proxy")
async def get_proxy(
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """获取当前代理设置。"""
    url = await get_proxy_url()
    return {"proxy_url": url}


@router.put("/system/proxy")
async def set_proxy(
    payload: ProxySettings,
    admin: Annotated[User, Depends(require_admin)] = None,
) -> dict:
    """保存代理设置。空字符串视为清除。"""
    value = (payload.proxy_url or "").strip() or None
    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(PlatformSetting).where(PlatformSetting.key == "proxy_url")
            )
        ).scalar_one_or_none()
        if row is None:
            row = PlatformSetting(key="proxy_url", value=value)
            session.add(row)
        else:
            row.value = value
            row.updated_at = datetime.now(timezone.utc)
        await session.commit()
    return {"proxy_url": value}


class TitleSettings(BaseModel):
    """自动起标题：是否开启、用哪个智能体、哪个模型（都留空 = 跟随「默认智能体」的默认模型）。"""

    enabled: bool = True
    agent_profile_id: str | None = None
    model: str | None = None


async def _title_settings_view(session, user: User | None = None) -> dict:
    """把标题设置连同解析后的名字一起回给界面（界面只回名字，不必自己再查一遍）。

    另附上一次自动起标题的失败原因：起标题是后台增强，失败只在日志里，用户看不到；
    给它一个界面上能自查的出口（成功一次就清掉）。
    """
    settings = await titles.read_settings(session)
    diagnostics = await titles.read_diagnostics(session)
    owner_id = None if user is None or user.role == "admin" else user.id
    chosen = None
    if settings.agent_profile_id:
        candidate = await session.get(AgentProfile, settings.agent_profile_id)
        if (
            candidate is not None
            and candidate.deleted_at is None
            and (owner_id is None or candidate.owner_id == owner_id)
        ):
            chosen = candidate
    fallback = await pick_default_profile(session, owner_id)
    effective = chosen or fallback
    return {
        "enabled": settings.enabled,
        "agent_profile_id": settings.agent_profile_id,
        "model": settings.model,
        "default_agent_id": fallback.id if fallback else None,
        "default_agent_name": fallback.name if fallback else None,
        "effective_agent_id": effective.id if effective else None,
        "effective_agent_name": effective.name if effective else None,
        "last_error": diagnostics.last_error,
        "last_error_at": diagnostics.last_error_at,
    }


@router.get("/system/title")
async def get_title_settings(
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """读取「自动生成对话标题」的设置。"""
    async with SessionLocal() as session:
        return await _title_settings_view(session, user)


@router.put("/system/title")
async def set_title_settings(
    payload: TitleSettings,
    admin: Annotated[User, Depends(require_admin)] = None,
) -> dict:
    """保存标题设置（平台级设置，仅管理员）。指定智能体必须存在且未删除；留空 = 跟随默认。"""
    agent_id = (payload.agent_profile_id or "").strip() or None
    async with SessionLocal() as session:
        if agent_id is not None:
            profile = await session.get(AgentProfile, agent_id)
            if profile is None or profile.deleted_at is not None:
                raise HTTPException(status_code=422, detail="指定的智能体不存在")
        await titles.write_settings(session, payload.enabled, agent_id, payload.model)
        return await _title_settings_view(session, admin)


class ContextSettingsBody(BaseModel):
    """上下文窗口配置：上限（tokens）与触发阈值（占上限的比例）。

    ``conversation_id`` 可选：带了就顺手校验「上限不得低于该对话当前已用量」，
    挡住那种一保存就把正在进行的对话压成负数的输入。
    """

    limit_tokens: int
    threshold_ratio: float = context_service.DEFAULT_THRESHOLD_RATIO
    conversation_id: str | None = None


def _context_settings_view(settings: context_service.ContextSettings) -> dict:
    return {
        "limit_tokens": settings.limit_tokens,
        "threshold_ratio": settings.threshold_ratio,
        "threshold_tokens": settings.threshold_tokens,
        "defaults": {
            "limit_tokens": context_service.DEFAULT_LIMIT_TOKENS,
            "threshold_ratio": context_service.DEFAULT_THRESHOLD_RATIO,
        },
        "min_limit_tokens": context_service.MIN_LIMIT_TOKENS,
    }


@router.get("/system/context")
async def get_context_settings(
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """读当前用户的上下文窗口配置（键不存在 = 默认 128k / 80%）。

    配置按「对话的归属」存：鉴权关闭时归属为空（单用户部署），与
    ``prepare_run`` 读取时用的 ``conversation.owner_id`` 是同一个键。
    """
    async with SessionLocal() as session:
        settings = await context_service.read_settings(session, owner_id_for(user))
        return _context_settings_view(settings)


@router.put("/system/context")
async def set_context_settings(
    payload: ContextSettingsBody,
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """保存上下文窗口配置：非法输入返回 422 且**不落盘**。"""
    try:
        settings = context_service.validate_settings(
            payload.limit_tokens, payload.threshold_ratio
        )
    except context_service.ContextSettingsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    async with SessionLocal() as session:
        if payload.conversation_id:
            conversation = await session.get(Conversation, payload.conversation_id)
            if conversation is None:
                raise HTTPException(status_code=404, detail="对话不存在")
            require_owned_strict(conversation, user)
            profile = await session.get(AgentProfile, conversation.agent_profile_id)
            used = await context_service.conversation_usage(
                session, conversation, profile.system_prompt if profile else None
            )
            if settings.limit_tokens < used:
                raise HTTPException(
                    status_code=422,
                    detail=f"上限不能低于当前上下文已用量（约 {used} tokens）",
                )
        saved = await context_service.write_settings(session, owner_id_for(user), settings)
        return _context_settings_view(saved)


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__}

@router.get("/system/overview")
async def overview(
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    settings = get_settings()
    agents = all_agents()
    # 可用性读缓存（缺行只查 PATH）：这里绝不能实时探测，否则每次打开页面都冻住几秒。
    statuses = await availability_service.statuses(agents)
    owner_id = None if user.role == "admin" else user.id
    # 绝对路径是服务器内部信息：非管理员只看「由管理员配置」，不暴露部署目录。
    return {
        "version": __version__,
        "version_history": [dict(item) for item in VERSION_HISTORY],
        "app_name": settings.app_name,
        "data_dir": str(settings.data_dir) if user.role == "admin" else "（仅管理员可见）",
        "workspaces_root": (
            str(settings.workspaces_root) if user.role == "admin" else "（仅管理员可见）"
        ),
        "agent_types": len(agents),
        "installed_types": sum(1 for a in agents if statuses[a.id].installed),
        "active_runs": registry.list(owner_id=owner_id),
    }


@router.get("/system/runs")
async def active_runs(
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    owner_id = None if user.role == "admin" else user.id
    return {"runs": registry.list(owner_id=owner_id)}
