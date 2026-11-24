"""智能体运行时（各 CLI 内核）的安装 / 更新 / 卸载接口。

运行时 = 某个 CLI 适配器依赖的本机可执行文件。这里只做三件事：列出本机有哪些运行时、
汇报安装状态与当前版本、代用户执行安装 / 更新 / 卸载。具体命令由适配器声明
（``AgentDefinition.provisioner``），本层不认识任何具体内核（铁律 1）。
"""
from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..agents import AgentDefinition, all_agents, get_agent
from ..agents.base import Availability
from ..db import get_session
from ..models import AgentProfile, User
from ..runtime import availability as availability_service
from ..runtime import provisioning
from ..schemas import RuntimeAction
from ..transport.sse import sse_stream
from .auth import current_user, require_admin

#: 整个路由器都要求登录：运行时是平台级操作，任何登录用户都可查看 / 安装。
router = APIRouter(prefix="/runtimes", tags=["runtimes"], dependencies=[Depends(current_user)])


def _describe(definition: AgentDefinition, agents: int, availability: Availability) -> dict:
    """一张运行时卡片的全部数据；安装状态来自框架缓存的检测结果（不实时探测）。"""
    return {
        "id": definition.id,
        "name": definition.name,
        "vendor": definition.vendor,
        "description": definition.description,
        "runtime": definition.runtime.value,
        "docs_url": definition.docs_url,
        "logo": definition.logo,
        "logo_style": definition.logo_style,
        "executable": definition.executable,
        #: 有多少个未删除的智能体在用这个内核（卸载前提示影响面）
        "agents": agents,
        #: 是否实现了登录态探测。与 `provisioning.commands.login` 分开：有些内核
        #: 只能交互式登录（平台代跑不了），但依然值得把登录状态展示出来。
        "auth_supported": definition.supports_auth(),
        "provisioning": definition.provisioning(availability),
    }


@router.get("")
async def list_runtimes(session: AsyncSession = Depends(get_session)) -> dict:
    rows = (
        await session.execute(
            select(AgentProfile.type_id, func.count(AgentProfile.id))
            .where(AgentProfile.deleted_at.is_(None))
            .group_by(AgentProfile.type_id)
        )
    ).all()
    counts = {type_id: count for type_id, count in rows}
    definitions = all_agents()
    statuses = await availability_service.statuses(definitions)
    return {
        "items": [
            _describe(definition, counts.get(definition.id, 0), statuses[definition.id])
            for definition in definitions
        ]
    }


@router.post("/{type_id}/refresh", dependencies=[Depends(require_admin)])
async def refresh_runtime(type_id: str) -> dict:
    """强制重测一次安装状态与版本（卡片上的「重新检测」按钮）。

    这是唯一会同步等待探测的读接口 —— 但探测在后台线程里跑，且这是用户主动触发的，
    因此不会像「每次打开页面都探测」那样拖慢整站。
    """
    try:
        return (await availability_service.refresh_type(type_id)).to_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


async def _provision_stream(type_id: str, action: provisioning.Action) -> StreamingResponse:
    try:
        definition = get_agent(type_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    label = provisioning.ACTION_LABELS[action]
    if definition.provisioner is None:
        raise HTTPException(status_code=409, detail=f"{definition.name} 不由平台管理，无法{label}")
    if not definition.provisioner.supports(action):
        raise HTTPException(status_code=409, detail=f"{definition.name} 未声明「{label}」命令")
    #: 已有进行中的任务就直接订阅它，避免重复启动
    existing = provisioning.job_registry.get(type_id)
    if existing and existing.status == "running":
        job = existing
    else:
        job = await provisioning.run_provision_background(definition, action)
    return StreamingResponse(
        sse_stream(provisioning.stream_job(job)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.post("/{type_id}/install", dependencies=[Depends(require_admin)])
async def install_runtime(type_id: str) -> StreamingResponse:
    return await _provision_stream(type_id, "install")


@router.post("/{type_id}/update", dependencies=[Depends(require_admin)])
async def update_runtime(type_id: str) -> StreamingResponse:
    return await _provision_stream(type_id, "update")


@router.post("/{type_id}/uninstall", dependencies=[Depends(require_admin)])
async def uninstall_runtime(type_id: str, payload: RuntimeAction) -> StreamingResponse:
    if not payload.confirm:
        raise HTTPException(status_code=422, detail="卸载属于破坏性操作，需要显式确认（confirm=true）")
    return await _provision_stream(type_id, "uninstall")


async def _auth_payload(type_id: str, *, include_accounts: bool) -> dict:
    """登录状态响应体；``include_accounts`` 控制是否带出已保存的账号清单。"""
    try:
        definition = get_agent(type_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    status = await asyncio.to_thread(definition.auth_status)
    if status is None:
        return {"supported": False, "required": False, "logged_in": None, "detail": None, "account": None}
    payload = {"supported": True, **status.to_dict()}
    # 已保存账号的昵称 / 企业属于机器级（管理员）资产：普通成员只看到登录态，
    # 看不到这台机器登录过哪些账号（切换 / 删除本来就只对管理员开放）。
    if not include_accounts:
        payload["accounts"] = None
    return payload


@router.get("/{type_id}/auth")
async def runtime_auth(type_id: str, user: Annotated[User, Depends(current_user)]) -> dict:
    """账号型内核的登录状态（二维码登录 / OAuth）。

    这是用户主动打开详情弹层时才调的接口，探测在后台线程里跑，最多等一个内部超时，
    不影响内核列表的缓存探测策略。账号清单只回给管理员。
    """
    return await _auth_payload(type_id, include_accounts=user.role == "admin")


@router.post("/{type_id}/login", dependencies=[Depends(require_admin)])
async def login_runtime(type_id: str) -> StreamingResponse:
    """启动一次内核登录（二维码走 SSE 的 ``provision.qr`` 事件推给前端）。"""
    return await _provision_stream(type_id, "login")


@router.post("/{type_id}/accounts/{account_id}/activate", dependencies=[Depends(require_admin)])
async def activate_runtime_account(type_id: str, account_id: str) -> dict:
    """切换到一个已保存的账号（多账号内核），返回切换后的登录状态。

    账号槽位是适配器自己的存储（例如 CodeBuddy 的 session 快照），框架只调用
    ``activate_account()``；切换失败时把可读原因回给界面。
    """
    try:
        definition = get_agent(type_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    if not definition.supports_accounts():
        raise HTTPException(status_code=409, detail=f"{definition.name} 不支持多账号切换")
    try:
        await asyncio.to_thread(definition.activate_account, account_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="账号不存在或槽位已损坏") from None
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return await _auth_payload(type_id, include_accounts=True)


@router.delete("/{type_id}/accounts/{account_id}", dependencies=[Depends(require_admin)])
async def remove_runtime_account(type_id: str, account_id: str) -> dict:
    """删除一个已保存的账号槽位；当前正在用的登录态不受影响。"""
    try:
        definition = get_agent(type_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    if not definition.supports_accounts():
        raise HTTPException(status_code=409, detail=f"{definition.name} 不支持多账号切换")
    try:
        await asyncio.to_thread(definition.remove_account, account_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="账号不存在") from None
    except RuntimeError as exc:
        # 槽位文件删不掉（权限等）：显式告诉用户，别让界面以为已经删干净了。
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return await _auth_payload(type_id, include_accounts=True)


@router.post("/{type_id}/check", dependencies=[Depends(require_admin)])
async def check_runtime(type_id: str) -> dict:
    """查询该运行时的最新可用版本（给界面「检查更新」按钮用）。"""
    try:
        definition = get_agent(type_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return await provisioning.latest_version(definition)


@router.get("/provision/jobs", dependencies=[Depends(require_admin)])
async def list_provision_jobs() -> dict:
    """列出所有进行中 / 刚完成的安装任务（给前端刷新后恢复状态用）。

    任务的输出里可能带登录二维码这类短期凭据，只允许管理员查看/订阅（AGENTS.md §7）。
    """
    return {"items": provisioning.job_registry.list()}


@router.get("/{type_id}/provision/stream", dependencies=[Depends(require_admin)])
async def stream_provision_job(type_id: str) -> StreamingResponse:
    """重连到一个进行中任务的输出流（刷新页面后查看实时进度）。

    重连会重放日志，其中登录动作的二维码是短期凭据，因此只对管理员开放。
    """
    job = provisioning.job_registry.get(type_id)
    if job is None:
        raise HTTPException(status_code=404, detail="没有进行中的任务")
    return StreamingResponse(
        sse_stream(provisioning.stream_job(job)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.delete("/{type_id}/provision", dependencies=[Depends(require_admin)])
async def clear_provision_job(type_id: str) -> dict:
    """清除一个已完成的任务记录（用户关掉结果弹窗时调用）。"""
    job = provisioning.job_registry.get(type_id)
    if job and job.status == "running":
        raise HTTPException(status_code=409, detail="任务进行中，无法清除")
    provisioning.job_registry.clear(type_id)
    return {"ok": True}
