"""技能：统一编目与智能体分配。

内核自带 / 宿主机上的技能由适配器只读扫描，全平台共享；用户自己的技能库
（导入、市场安装、挂载到其它内核）存放在平台数据目录里，按 ``owner_id`` 隔离，
一份技能按 slug 只存一份、多个内核引用共用；接口只回当前用户可见的部分。
智能体能用的技能仍只能通过分配关系获得。
"""
from __future__ import annotations

import base64
import binascii
import re
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..agents import get_agent
from ..db import get_session
from ..models import Skill, User
from ..runtime import skills as skill_service
from ..schemas import (
    SkillCopyAllResult,
    SkillCopyIn,
    SkillDetail,
    SkillImportIn,
    SkillRead,
    SkillRescanResult,
)
from .auth import current_user, owner_id_for

router = APIRouter(prefix="/skills", tags=["skills"])


def _type_name(type_id: str) -> str | None:
    try:
        return get_agent(type_id).name
    except KeyError:
        return None


def _is_own(row: Skill, user: User) -> bool:
    """这一行是否是当前用户自己技能库里的技能（共享技能不算）。"""
    return skill_service.is_library_row(row) and row.owner_id == owner_id_for(user)


def _visible(row: Skill, user: User) -> bool:
    """共享技能所有人可见；用户技能只有主人与管理员可见。"""
    if user.role == "admin":
        return True
    return row.owner_id is None or row.owner_id == owner_id_for(user)


def _can_manage(row: Skill, user: User) -> bool:
    """能否删除 / 卸载：自己的技能或管理员；共享技能只有管理员能写。"""
    return user.role == "admin" or _is_own(row, user)


def _source_label(row: Skill) -> str:
    """来源标签：宿主机上扫描来的个人 / 市场技能要标明「共享」，避免和用户技能库混淆。"""
    label = skill_service.SOURCE_LABELS.get(row.source_kind, row.source_kind)
    shared_legacy = (
        not skill_service.is_library_row(row)
        and row.source_kind in {"user", skill_service.MARKET_SOURCE}
    )
    return f"{label}（共享）" if shared_legacy else label


def _describe(
    row: Skill,
    counts: dict[str, int],
    user: User,
    owner_names: dict[str, str],
    mounts_cache: dict[str | None, dict[str, list[str]]] | None = None,
) -> SkillRead:
    mounted: list[str] = []
    if skill_service.is_library_row(row):
        if mounts_cache is None:
            mounted = skill_service.mounts_for_row(row)
        else:
            # 列表里同一 owner 的多行共用一个缓存：`.mounts.json` 只读一次。
            if row.owner_id not in mounts_cache:
                mounts_cache[row.owner_id] = skill_service.read_library_mounts(
                    skill_service.library_root(row.owner_id)
                )
            mounted = skill_service.mounts_for_row(
                row, mounts=mounts_cache[row.owner_id]
            )
    return SkillRead(
        id=row.id,
        type_id=row.type_id,
        type_name=_type_name(row.type_id),
        slug=row.slug,
        name=row.name,
        description=row.description or "",
        source_kind=row.source_kind,
        source_label=_source_label(row),
        version=row.version,
        author=row.author,
        when_to_use=row.when_to_use,
        available=row.available,
        total_bytes=row.total_bytes or 0,
        file_count=len(row.files or []),
        note=row.note,
        used_by_agents=counts.get(row.id, 0),
        is_market=row.source_kind == skill_service.MARKET_SOURCE,
        deletable=skill_service.is_deletable(row) and _can_manage(row, user),
        source_url=(row.source_url or None),
        owner_id=row.owner_id,
        owner_name=owner_names.get(row.owner_id or ""),
        is_own=_is_own(row, user),
        mounted_kernels=mounted,
    )


async def _owner_names(session: AsyncSession, rows: list[Skill]) -> dict[str, str]:
    """管理员看全部技能时，把归属用户 id 翻成可读名字。"""
    ids = {row.owner_id for row in rows if row.owner_id}
    if not ids:
        return {}
    users = (await session.execute(select(User).where(User.id.in_(ids)))).scalars().all()
    return {item.id: (item.display_name or item.username) for item in users}


@router.get("")
async def list_skills(
    type_id: str | None = None,
    source_kind: str | None = None,
    available: bool | None = None,
    q: str | None = None,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """列出对当前用户可见的技能（共享技能 + 自己的用户技能库）。

    管理员可以看到全部用户的技能库，并附带归属用户名，便于排查。
    """
    is_admin = user.role == "admin"
    rows = await skill_service.list_rows(
        session,
        type_id=type_id,
        source_kind=source_kind,
        available=available,
        query=q,
        owner_id=None if is_admin else owner_id_for(user),
        all_owners=is_admin,
    )
    counts = await skill_service.usage_counts(
        session, None if is_admin else owner_id_for(user)
    )
    names = await _owner_names(session, rows)
    mounts_cache: dict[str | None, dict[str, list[str]]] = {}
    return {
        "items": [
            _describe(row, counts, user, names, mounts_cache).model_dump()
            for row in rows
        ],
        "source_labels": skill_service.SOURCE_LABELS,
    }


@router.post("/rescan")
async def rescan_skills(
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """重新扫描内核技能目录与各用户的技能库，并同步到统一目录。"""
    result = await skill_service.rescan(session)
    return SkillRescanResult(**result).model_dump()


@router.post("/import")
async def import_skill(
    payload: SkillImportIn,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """把技能压缩包（base64）导入当前用户的技能库。

    导入后仍需在智能体里分配才生效；同名技能不会覆盖。每个用户只能看到 / 管理
    自己导入的技能（管理员可看全部）。
    """
    try:
        archive = base64.b64decode(payload.content_base64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail="压缩包内容不是合法的 base64") from None
    limit = skill_service.MAX_TRANSFER_BYTES
    if len(archive) > limit:
        raise HTTPException(
            status_code=422,
            detail=f"压缩包超过 {limit // 1024 // 1024} MB，已拒绝导入",
        )
    try:
        result = await skill_service.import_skill_archive(
            session,
            type_id=payload.type_id,
            archive=archive,
            owner_id=owner_id_for(user),
            filename=payload.name,
        )
    except skill_service.SkillError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return result


@router.get("/{skill_id}")
async def get_skill(
    skill_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    row = await skill_service.get_row(session, skill_id)
    if row is None or not _visible(row, user):
        raise HTTPException(status_code=404, detail=f"技能不存在：{skill_id}")
    counts = await skill_service.usage_counts(
        session, None if user.role == "admin" else owner_id_for(user)
    )
    names = await _owner_names(session, [row])
    body, truncated = skill_service.read_body(row.path)
    detail = SkillDetail(
        **_describe(row, counts, user, names).model_dump(),
        path=row.path,
        fingerprint=row.fingerprint or "",
        allowed_tools=list(row.allowed_tools or []),
        files=list(row.files or []),
        meta=dict(row.meta or {}),
        body=body,
        body_truncated=truncated,
        agents=[
            {"id": profile.id, "name": profile.name, "avatar": profile.avatar, "avatar_preset": profile.avatar_preset}
            for profile in await skill_service.agents_using(
                session, skill_id, None if user.role == "admin" else owner_id_for(user)
            )
        ],
    )
    return detail.model_dump()


@router.get("/{skill_id}/export")
async def export_skill(
    skill_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> Response:
    """把一个技能导出成 zip（共享技能与自己的技能都可以导出）。"""
    row = await skill_service.get_row(session, skill_id)
    if row is None or not _visible(row, user):
        raise HTTPException(status_code=404, detail=f"技能不存在：{skill_id}")
    try:
        archive = skill_service.export_skill_archive(row)
    except skill_service.SkillError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    # 技能目录名可能是中文：filename 只放 ASCII 兜底名，完整名字走 RFC 5987 的 filename*。
    fallback = re.sub(r"[^A-Za-z0-9._-]+", "_", row.slug).strip("._") or "skill"
    filename = f"{row.slug}.zip"
    disposition = (
        f"attachment; filename=\"{fallback}.zip\"; "
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )
    return Response(
        content=archive,
        media_type="application/zip",
        headers={"Content-Disposition": disposition},
    )


@router.post("/{skill_id}/copy")
async def copy_skill(
    skill_id: str,
    payload: SkillCopyIn,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """把一份技能挂载到当前用户的另一个内核（技能库按 slug 只存一份，不复制文件）。

    源可以是共享技能（第一次挂载时收进自己的技能库），也可以是自己库里的技能；
    目标内核里已有同 slug 的共享技能时拒绝（物化会撞名）。
    """
    row = await skill_service.get_row(session, skill_id)
    if row is None or not _visible(row, user):
        raise HTTPException(status_code=404, detail=f"技能不存在：{skill_id}")
    try:
        result = await skill_service.copy_skill_to_kernel(
            session,
            skill_id=skill_id,
            type_id=payload.type_id,
            owner_id=owner_id_for(user),
        )
    except skill_service.SkillNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except skill_service.SkillError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return result


@router.post("/{skill_id}/copy-all")
async def copy_skill_to_all(
    skill_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """把一份技能挂载到全部支持技能的内核（尽力而为，冲突的内核只跳过并汇报）。"""
    row = await skill_service.get_row(session, skill_id)
    if row is None or not _visible(row, user):
        raise HTTPException(status_code=404, detail=f"技能不存在：{skill_id}")
    try:
        result = await skill_service.copy_skill_to_all_kernels(
            session, skill_id=skill_id, owner_id=owner_id_for(user)
        )
    except skill_service.SkillNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return SkillCopyAllResult(**result).model_dump()


@router.delete("/{skill_id}")
async def delete_skill(
    skill_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """删除技能：只能删自己技能库里的技能；管理员可删共享 / 他人的技能。

    内核自带（system）与已安装插件（plugin）里的技能只读。用户技能库里的一份实体
    可能被多个内核引用：删除先摘本内核引用并断开本内核分配，还有别的内核在用时保留实体；
    最后一个引用消失才删目录与全部编目行（响应里的 ``remaining_kernels`` 说明保留情况）。
    """
    row = await skill_service.get_row(session, skill_id)
    if row is None or not _visible(row, user):
        raise HTTPException(status_code=404, detail=f"技能不存在：{skill_id}")
    if not _can_manage(row, user):
        raise HTTPException(status_code=403, detail="只能删除自己技能库里的技能")
    try:
        result = await skill_service.delete_skill(session, skill_id)
    except skill_service.SkillNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except skill_service.SkillError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"ok": True, **result}
