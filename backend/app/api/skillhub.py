"""技能市场（SkillHub）：浏览、安装、卸载。

把 https://skillhub.cn 的技能接进「技能」页：浏览 / 搜索 / 安装到**当前用户的技能中心**
（按用户隔离，一份实体全部支持技能的内核共用），并卸载由平台装入的（``source_kind=market``）
技能。**装完仍需在智能体配置里分配**，运行时才会生效（与内核自带技能同一套规则）。

本层保持薄：只做参数校验、调用 :mod:`app.skillhub` 与 :mod:`app.runtime.skills`、
并把结果翻成 HTTP 状态码。
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..agents import all_agents
from ..db import get_session
from ..models import User
from ..runtime import skills as skill_service
from ..schemas import SkillHubInstallIn, SkillHubUninstallIn
from ..skillhub import SkillHubError
from ..skillhub import download_skill as fetch_skill_archive
from ..skillhub import get_skill as fetch_skill_detail
from ..skillhub import list_skills as fetch_skill_list
from .auth import current_user, owner_id_for

logger = logging.getLogger("coda")

#: 整个路由器都要求登录：浏览市场、安装到自己的技能中心都需要当前用户。
router = APIRouter(prefix="/skillhub", tags=["skillhub"], dependencies=[Depends(current_user)])


@router.get("/skills")
async def market_skills(
    page: int = 1,
    page_size: int = 24,
    sort_by: str = "score",
    order: str | None = None,
    keyword: str | None = None,
    category: str | None = None,
    source: str | None = None,
) -> dict:
    """从技能市场拉一页技能（按评分 / 下载量 / 更新时间排序，可搜索）。"""
    try:
        return await fetch_skill_list(
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            order=order,
            keyword=keyword,
            category=category,
            source=source,
        )
    except SkillHubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None


@router.get("/kernels")
async def market_kernels(
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """哪些内核可以装技能（声明了技能目录的适配器），供导入 / 复制时的目标选择。

    市场**安装**不再需要指定内核（技能中心全内核共用）；但这个列表仍服务于
    「导入技能」与「复制技能到其它内核」——它们的目标是单个内核的编目行。
    ``path`` 是**当前用户**的技能库目录 —— 技能按 slug 只存一份，所有内核共用。
    """
    items: list[dict[str, str | None]] = []
    for definition in all_agents():
        try:
            if not definition.skill_roots():
                continue
        except Exception as exc:  # noqa: BLE001 - 单个适配器异常不影响选择列表
            logger.warning("读取内核 %s 的技能根失败：%s", definition.id, exc)
            continue
        items.append(
            {
                "type_id": definition.id,
                "name": definition.name,
                "path": str(skill_service.library_root(owner_id_for(user))),
            }
        )
    return {"items": items}


@router.get("/skills/{slug}")
async def market_skill_detail(slug: str) -> dict:
    """技能市场里一个技能的详情。"""
    try:
        return await fetch_skill_detail(slug)
    except SkillHubError as exc:
        status = 404 if "不存在" in str(exc) else 502
        raise HTTPException(status_code=status, detail=str(exc)) from None


@router.post("/install")
async def install_market_skill(
    payload: SkillHubInstallIn,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """把一个市场技能下载并安装到**当前用户的技能中心**（全部支持技能的内核共用）。"""
    slug = (payload.slug or "").strip()
    if not slug:
        raise HTTPException(status_code=422, detail="缺少技能标识（slug）")

    version = payload.version
    requires_api_key = False
    try:
        detail = await fetch_skill_detail(slug)
        requires_api_key = bool(detail.get("requires_api_key"))
        if not version:
            version = detail.get("version") or None
    except SkillHubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None

    try:
        archive = await fetch_skill_archive(slug, version=version)
    except SkillHubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None

    try:
        result = await skill_service.install_market_skill(
            session,
            slug=slug,
            version=version,
            archive=archive,
            owner_id=owner_id_for(user),
            source_url=f"https://skillhub.cn/skills/{slug}",
            display_slug=slug,
        )
    except skill_service.SkillError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    return {
        **result,
        "slug": slug,
        "version": version,
        "requires_api_key": requires_api_key,
    }


@router.post("/uninstall")
async def uninstall_market_skill(
    payload: SkillHubUninstallIn,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """从技能中心整体卸载一个由市场装入的技能（移除唯一实体与全部编目行）。"""
    row = await skill_service.get_row(session, payload.skill_id)
    mine = bool(
        row
        and skill_service.is_library_row(row)
        and row.owner_id == owner_id_for(user)
    )
    if row is None or (user.role != "admin" and not mine):
        # 非管理员看不到别人的技能；共享的老市场技能也只有管理员能卸载。
        raise HTTPException(status_code=404, detail=f"技能不存在：{payload.skill_id}")
    try:
        result = await skill_service.uninstall_market_skill(session, payload.skill_id)
    except skill_service.SkillNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except skill_service.SkillError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"ok": True, **result}
