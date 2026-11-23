"""用户管理接口（登录 / 注册 / 用户 CRUD）。

鉴权依赖项（``current_user`` / ``require_admin`` / ``require_owned``）在顶层
:mod:`app.auth_deps`，因为协作域有架构守卫禁止依赖 ``app.api.*``，但它的路由同样
需要鉴权 —— 依赖项必须住在协作域允许依赖的层里。本模块从那里重新导出，方便 ``api``
层内的路由 ``from .auth import current_user`` 使用。

设计要点：

* **无状态会话**：令牌是 JWT（见 :mod:`app.auth`），服务端不存 session，水平扩展无需共享会话存储。
* **鉴权开关**：默认开启；由 :func:`current_user` 校验令牌，未登录返回 401。
  ``CODA_AUTH_ENABLED=false`` 时返回虚拟管理员，所有接口照旧可用 —— 给单用户部署
  保留开箱即用的形态。
* **归属过滤**：资产接口拿到 ``current_user`` 后，在查询里加 ``owner_id == user.id``（对话内容
  严格只对本人可见、管理员也不豁免；其余资产管理员可看全部）。过滤逻辑在各路由里写，
  不在这里藏魔法。
* **首任管理员**：由 ``bootstrap.seed_initial_admin`` 在启动时按环境变量创建，不在这里做。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlmodel import select
from starlette.concurrency import run_in_threadpool

from .. import avatars
from ..schemas import AvatarUpload
from ..auth import hash_password, issue_token, verify_password
from ..auth import issue_pat as auth_issue_pat
from ..auth_deps import (
    _SYSTEM_USER,
    current_user,
    owner_id_for,
    require_admin,
    require_owned,
    require_owned_strict,
)
from ..db import SessionLocal
from ..models import User
from ..settings import get_settings

router = APIRouter(prefix="/auth", tags=["auth"])

__all__ = [
    "router",
    "current_user",
    "require_admin",
    "require_owned",
    "require_owned_strict",
]


# --- 请求 / 响应模型 --------------------------------------------------------


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=1, max_length=200)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=2, max_length=60)
    password: str = Field(min_length=8, max_length=200)
    display_name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=200)


class UserRead(BaseModel):
    id: str
    username: str
    display_name: str | None = None
    email: str | None = None
    role: str
    is_active: bool
    created_at: datetime
    #: 头像的对外标识：有值时前端拼 ``/api/auth/users/{id}/avatar?v=<它>``；
    #: 为空表示没上传过，界面回退到「显示名首字母」。
    #: 它同时是缓存键 —— 换头像就换值，浏览器不必猜什么时候该重取。
    avatar: str | None = None
    #: 轨迹调试开关（用户级，默认关闭）：只有它为 True 时运行层才采集轨迹。
    trace_debug: bool = False


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserRead


class AuthConfig(BaseModel):
    """登录页需要的公开信息：是否开鉴权 / 是否允许注册 / 是否已有用户。"""

    auth_enabled: bool
    registration_open: bool
    has_users: bool


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=8, max_length=200)


class UpdateUserRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=200)
    role: str | None = None
    is_active: bool | None = None
    #: 轨迹调试开关；只有用户自己（``PUT /me``）会写，管理员改用户时不碰它。
    trace_debug: bool | None = None


def _to_read(user: User) -> UserRead:
    return UserRead(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        email=user.email,
        role=user.role,
        is_active=user.is_active,
        created_at=user.created_at,
        avatar=user.avatar_file,
        trace_debug=bool(user.trace_debug),
    )


async def _refresh_builtin_tokens(session, user_id: str) -> int:
    """凭据变了：立刻给该用户的内置 MCP 补发平台令牌。

    用户重新生成 / 吊销 PAT、改口令、管理员重置口令之后，配置里那份
    ``CODA_TOKEN`` 可能已经失效 —— 不补发的话，系统管理 MCP 会一直收到 401，
    只能等下次启动播种才恢复。这里当场补齐（见
    :func:`app.runtime.mcp.refresh_builtin_tokens`）。
    """
    from ..runtime import mcp as mcp_service

    return await mcp_service.refresh_builtin_tokens(session, user_id)


# --- 接口 ------------------------------------------------------------------


@router.get("/config")
async def auth_config() -> AuthConfig:
    """公开的鉴权配置（不需要令牌）：登录页据此决定显示登录还是注册。"""
    settings = get_settings()
    async with SessionLocal() as session:
        has_users = (await session.execute(select(User.id).limit(1))).first() is not None
    return AuthConfig(
        auth_enabled=settings.auth_enabled,
        registration_open=settings.registration_open,
        has_users=has_users,
    )


@router.post("/login")
async def login(payload: LoginRequest) -> LoginResponse:
    """登录：校验口令，签发会话令牌。"""
    username = payload.username.strip()
    async with SessionLocal() as session:
        user = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="用户名或口令不正确")
        # PBKDF2 是 CPU 密集操作，放线程池里跑，避免阻塞事件循环。
        if not await run_in_threadpool(
            verify_password, payload.password, user.password_hash
        ):
            raise HTTPException(status_code=401, detail="用户名或口令不正确")
        # 登录时顺手确认内置 MCP 的平台令牌还有效（失效就当场补发一份新的）。
        await _refresh_builtin_tokens(session, user.id)
    token = issue_token(user.id, user.role, user.token_version or 0)
    return LoginResponse(access_token=token, user=_to_read(user))


@router.post("/register", status_code=201)
async def register(payload: RegisterRequest) -> LoginResponse:
    """注册：创建普通用户并直接登录。

    可由 ``CODA_REGISTRATION_OPEN=false`` 关闭；关闭后只能由管理员建用户。
    """
    settings = get_settings()
    if not settings.registration_open:
        raise HTTPException(status_code=403, detail="注册已关闭，请联系管理员创建账号")
    username = payload.username.strip()
    if len(username) < 2:
        raise HTTPException(status_code=422, detail="用户名至少 2 个字符")
    async with SessionLocal() as session:
        existing = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail="用户名已被占用")
        # 第一个用户自动是管理员：保证开箱即用时第一个注册的人能管理后续用户。
        has_any = (await session.execute(select(User.id).limit(1))).first() is not None
        user = User(
            username=username,
            display_name=(payload.display_name or "").strip() or None,
            email=(payload.email or "").strip() or None,
            password_hash=await run_in_threadpool(hash_password, payload.password),
            role="admin" if not has_any else "member",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        if not has_any:
            # 老库升级上来、又没配 CODA_ADMIN_* 时：第一个注册的人就是管理员，
            # 把原本无归属的存量数据交给他（幂等，已有标记则直接返回）。
            from ..bootstrap import backfill_ownership

            await backfill_ownership(session, user)
        # 新用户开箱即用：把平台内置的 MCP 服务器播种到他的名下（幂等）。
        from ..runtime import mcp as mcp_service

        await mcp_service.ensure_builtin_servers(session, user.id)
    token = issue_token(user.id, user.role, user.token_version or 0)
    return LoginResponse(access_token=token, user=_to_read(user))


@router.get("/me")
async def me(user: Annotated[User, Depends(current_user)]) -> UserRead:
    """取当前登录用户。"""
    return _to_read(user)


@router.put("/me/password")
async def change_password(
    payload: ChangePasswordRequest, user: Annotated[User, Depends(current_user)]
) -> dict:
    """修改自己的口令：要校验旧口令。"""
    if user.id == _SYSTEM_USER.id:
        raise HTTPException(status_code=400, detail="鉴权已关闭，无需修改口令")
    async with SessionLocal() as session:
        row = await session.get(User, user.id)
        if row is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        if not await run_in_threadpool(verify_password, payload.old_password, row.password_hash):
            # 旧口令错误是请求内容问题，不是会话失效；返回 400，
            # 避免前端把 401 当成令牌过期而把用户踢回登录页。
            raise HTTPException(status_code=400, detail="旧口令不正确")
        row.password_hash = await run_in_threadpool(hash_password, payload.new_password)
        # 旧令牌立即失效；给当前设备签发一份新令牌，改完口令不必重新登录。
        # PAT 是长效凭据，口令改了说明「凭据可能已泄露」，一并吊销（清空哈希即失效）。
        row.token_version = (row.token_version or 0) + 1
        row.pat_hash = None
        row.pat_issued_at = None
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        # 凭据刚被吊销：内置 MCP 里那份平台令牌可能已失效，当场补发一份新的。
        await _refresh_builtin_tokens(session, row.id)
        new_token = issue_token(row.id, row.role, row.token_version or 0)
    return {"ok": True, "access_token": new_token}


@router.put("/me")
async def update_me(
    payload: UpdateUserRequest, user: Annotated[User, Depends(current_user)]
) -> UserRead:
    """修改自己的显示名 / 邮箱（不能改角色与启用状态，那是管理员的事）。"""
    if user.id == _SYSTEM_USER.id:
        raise HTTPException(status_code=400, detail="鉴权已关闭，无个人资料可改")
    async with SessionLocal() as session:
        row = await session.get(User, user.id)
        if row is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        if payload.display_name is not None:
            row.display_name = payload.display_name.strip() or None
        if payload.email is not None:
            row.email = payload.email.strip() or None
        if payload.trace_debug is not None:
            row.trace_debug = payload.trace_debug
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return _to_read(row)


# --- 个人访问令牌（PAT）------------------------------------------------------


@router.post("/me/pat")
async def issue_my_pat(user: Annotated[User, Depends(current_user)]) -> dict:
    """生成（或重新生成）自己的个人访问令牌。

    每个用户同时只有一份 PAT：重新生成会立即吊销旧的（旧哈希被覆盖）。
    明文令牌**只在本次响应里出现一次** —— 平台只存 sha256 哈希，关掉弹窗就再也看不到。
    用途：MCP 服务器等脚本以你的身份调用平台 REST API（带 ``Authorization: Bearer pat_…``）。
    """
    if user.id == _SYSTEM_USER.id:
        raise HTTPException(status_code=400, detail="鉴权已关闭，无需个人访问令牌")
    raw, token_hash = auth_issue_pat()
    async with SessionLocal() as session:
        row = await session.get(User, user.id)
        if row is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        row.pat_hash = token_hash
        row.pat_issued_at = datetime.now(timezone.utc)
        row.updated_at = row.pat_issued_at
        session.add(row)
        await session.commit()
        # commit 会把对象过期掉，值要在会话还开着时取出来。
        issued_at = row.pat_issued_at.isoformat()
        # 旧令牌已作废：内置 MCP 若还在用旧的那份，当场换成新的（不必等下次启动）。
        await _refresh_builtin_tokens(session, row.id)
    return {
        "ok": True,
        "pat": raw,
        "issued_at": issued_at,
        "hint": "请立即复制保存：令牌只显示这一次，关闭后无法再查看。",
    }


@router.get("/me/pat")
async def my_pat_status(user: Annotated[User, Depends(current_user)]) -> dict:
    """看自己的 PAT 状态（只回「有没有 / 何时生成」，永不回令牌本体或哈希）。"""
    if user.id == _SYSTEM_USER.id:
        raise HTTPException(status_code=400, detail="鉴权已关闭，没有个人访问令牌")
    async with SessionLocal() as session:
        row = await session.get(User, user.id)
        if row is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        return {
            "has_pat": bool(row.pat_hash),
            "issued_at": row.pat_issued_at.isoformat() if row.pat_issued_at else None,
        }


@router.delete("/me/pat")
async def revoke_my_pat(user: Annotated[User, Depends(current_user)]) -> dict:
    """吊销自己的个人访问令牌（清空哈希即失效，正在用它的 MCP 服务器会开始收到 401）。"""
    if user.id == _SYSTEM_USER.id:
        raise HTTPException(status_code=400, detail="鉴权已关闭，没有个人访问令牌可吊销")
    async with SessionLocal() as session:
        row = await session.get(User, user.id)
        if row is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        row.pat_hash = None
        row.pat_issued_at = None
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)
        await session.commit()
        # 令牌没了：内置 MCP 里那份可能已失效，当场补发（不必等下次启动）。
        await _refresh_builtin_tokens(session, row.id)
    return {"ok": True}


@router.put("/me/avatar")
async def upload_my_avatar(
    payload: AvatarUpload, user: Annotated[User, Depends(current_user)]
) -> UserRead:
    """上传 / 替换自己的头像。

    只收位图（PNG / JPEG / WebP，≤2 MB），文件名由服务端按「用户 id + 内容摘要」生成 ——
    既挡掉目录穿越，也让「换头像 = 换文件名」，浏览器缓存自动失效。
    """
    if user.id == _SYSTEM_USER.id:
        raise HTTPException(status_code=400, detail="鉴权已关闭，没有个人头像可设置")
    try:
        raw, extension = avatars.decode_avatar(payload.content_base64)
    except avatars.AvatarError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    data_dir = get_settings().data_dir
    try:
        stored = avatars.store_avatar(data_dir, user.id, raw, extension)
    except avatars.AvatarError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    async with SessionLocal() as session:
        row = await session.get(User, user.id)
        if row is None:
            avatars.remove_avatar(data_dir, stored.filename)
            raise HTTPException(status_code=404, detail="用户不存在")
        row.avatar_file = stored.filename
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return _to_read(row)


@router.delete("/me/avatar")
async def delete_my_avatar(user: Annotated[User, Depends(current_user)]) -> UserRead:
    """清掉自己的头像，界面回退到「显示名首字母」。"""
    if user.id == _SYSTEM_USER.id:
        raise HTTPException(status_code=400, detail="鉴权已关闭，没有个人头像可清除")
    data_dir = get_settings().data_dir
    async with SessionLocal() as session:
        row = await session.get(User, user.id)
        if row is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        previous = row.avatar_file
        row.avatar_file = None
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)
        await session.commit()
        await session.refresh(row)
    avatars.remove_avatar(data_dir, previous)
    return _to_read(row)


@router.get("/users/{user_id}/avatar")
async def get_user_avatar(
    user_id: str, user: Annotated[User, Depends(current_user)]
) -> Response:
    """取某个用户的头像（任何登录用户都能看 —— 用户列表、协作消息里都要显示）。"""
    async with SessionLocal() as session:
        row = await session.get(User, user_id)
    if row is None or not row.avatar_file:
        raise HTTPException(status_code=404, detail="该用户没有头像")
    target = avatars.resolve_avatar(get_settings().data_dir, row.avatar_file)
    if target is None:
        raise HTTPException(status_code=404, detail="头像文件已丢失")
    # 文件名带内容摘要，可以长缓存；用户换头像后 URL 里的 `avatar` 值也变了。
    return Response(
        content=target.read_bytes(),
        media_type=avatars.guess_media_type(target.name),
        headers={"Cache-Control": "private, max-age=604800"},
    )


# --- 管理员接口 ------------------------------------------------------------


@router.get("/users")
async def list_users(admin: Annotated[User, Depends(require_admin)]) -> dict:
    """列出全部用户（管理员）。"""
    async with SessionLocal() as session:
        rows = (
            await session.execute(select(User).order_by(User.created_at))
        ).scalars().all()
    return {"items": [_to_read(row).model_dump() for row in rows]}


@router.post("/users", status_code=201)
async def admin_create_user(
    payload: RegisterRequest, admin: Annotated[User, Depends(require_admin)]
) -> UserRead:
    """管理员新建用户（不受注册开关限制）。"""
    username = payload.username.strip()
    if len(username) < 2:
        raise HTTPException(status_code=422, detail="用户名至少 2 个字符")
    async with SessionLocal() as session:
        existing = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail="用户名已被占用")
        user = User(
            username=username,
            display_name=(payload.display_name or "").strip() or None,
            email=(payload.email or "").strip() or None,
            password_hash=await run_in_threadpool(hash_password, payload.password),
            role="member",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        # 管理员建的用户同样开箱即用：播种内置 MCP 服务器（幂等）。
        from ..runtime import mcp as mcp_service

        await mcp_service.ensure_builtin_servers(session, user.id)
    return _to_read(user)


@router.patch("/users/{user_id}")
async def admin_update_user(
    user_id: str,
    payload: UpdateUserRequest,
    admin: Annotated[User, Depends(require_admin)],
) -> UserRead:
    """管理员修改用户：显示名 / 邮箱 / 角色 / 启用状态。"""
    async with SessionLocal() as session:
        row = await session.get(User, user_id)
        if row is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        # 不能把自己降级 / 禁用：避免管理员手滑把自己锁在系统外。
        demoting = payload.role is not None and payload.role != "admin"
        disabling = payload.is_active is False
        if row.id == admin.id and (demoting or disabling):
            raise HTTPException(status_code=409, detail="不能降级或禁用当前登录的管理员自己")
        # 最后一个管理员也不能被降级 / 禁用：否则再没人能管理用户。
        losing_admin = row.role == "admin" and (demoting or disabling)
        if losing_admin:
            admins = (
                await session.execute(
                    select(User.id).where(User.role == "admin", User.is_active == True)  # noqa: E712
                )
            ).all()
            if len(admins) <= 1:
                raise HTTPException(status_code=409, detail="系统至少需要一名启用中的管理员")
        if payload.display_name is not None:
            row.display_name = payload.display_name.strip() or None
        if payload.email is not None:
            row.email = payload.email.strip() or None
        if payload.role is not None:
            if payload.role not in ("admin", "member"):
                raise HTTPException(status_code=422, detail="角色只能是 admin 或 member")
            row.role = payload.role
        if payload.is_active is not None:
            row.is_active = payload.is_active
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return _to_read(row)


@router.post("/users/{user_id}/reset-password")
async def admin_reset_password(
    user_id: str,
    payload: ChangePasswordRequest,
    admin: Annotated[User, Depends(require_admin)],
) -> dict:
    """管理员重置用户口令：不校验旧口令（``old_password`` 字段保留但忽略）。"""
    async with SessionLocal() as session:
        row = await session.get(User, user_id)
        if row is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        row.password_hash = await run_in_threadpool(hash_password, payload.new_password)
        # 旧令牌立即失效，必须用新口令重新登录。
        row.token_version = (row.token_version or 0) + 1
        # PAT 不校验 token_version（长效凭据），所以必须显式清空哈希；
        # 管理员重置口令通常意味着账号可能已泄露，这里连同内置 MCP 专用令牌一起吊销。
        # 内置令牌清空后，下次启动播种会自动补发一份新的，不会弄挂系统管理 MCP。
        row.pat_hash = None
        row.pat_issued_at = None
        row.builtin_mcp_pat_hash = None
        row.builtin_mcp_pat_issued_at = None
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        # 专用令牌刚被吊销：内置 MCP 里那份已经失效，当场补发一份新的，
        # 系统管理 MCP 不用等平台重启就能继续用。
        await _refresh_builtin_tokens(session, row.id)
    result: dict = {"ok": True}
    if row.id == admin.id:
        # 管理员重置的是自己的口令：给当前设备换一份新令牌，避免下一次请求被登出。
        result["access_token"] = issue_token(row.id, row.role, row.token_version or 0)
    return result
