"""网关接口：自定义网关的增删改查、官方链路的读取，以及「获取模型」探测。

网关是 API 密钥的唯一来源（见 docs/架构/architecture.md）。官方链路（``kind == "official"``）
由启动播种按适配器声明生成，不能删除；自定义网关由用户在这里创建。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import func, select

from ..agents import get_agent
from ..db import get_session
from ..models import AgentProfile, Gateway, User
from ..runtime.gateway_probe import AUTH_STYLES, PROTOCOLS, ProbeError, probe_models
from ..schemas import (
    GatewayCreate,
    GatewayProbeRequest,
    GatewayProbeResult,
    GatewayRead,
    GatewayUpdate,
)
from ..security import decrypt, encrypt, mask
from .auth import current_user, owner_id_for, require_owned

router = APIRouter(prefix="/gateways", tags=["gateways"])


def to_read(gateway: Gateway, used_by: int = 0) -> GatewayRead:
    type_name = None
    if gateway.type_id:
        try:
            type_name = get_agent(gateway.type_id).name
        except KeyError:
            type_name = gateway.type_id
    secret = decrypt(gateway.api_key_encrypted)
    return GatewayRead(
        id=gateway.id,
        name=gateway.name,
        kind=gateway.kind,
        type_id=gateway.type_id,
        protocol=gateway.protocol,
        auth_style=gateway.auth_style,
        base_url=gateway.base_url,
        selected_models=list(gateway.selected_models or []),
        model_catalog=list(gateway.model_catalog or []),
        extra_headers=dict(gateway.extra_headers or {}),
        enabled=gateway.enabled,
        created_at=gateway.created_at,
        updated_at=gateway.updated_at,
        last_probed_at=gateway.last_probed_at,
        type_name=type_name,
        api_key_set=bool(gateway.api_key_encrypted),
        api_key_preview=mask(secret),
        used_by_profiles=used_by,
    )


async def _usage_counts(session: AsyncSession) -> dict[str, int]:
    rows = (
        await session.execute(
            select(AgentProfile.gateway_id, func.count(AgentProfile.id))
            .where(AgentProfile.deleted_at.is_(None), AgentProfile.gateway_id.is_not(None))
            .group_by(AgentProfile.gateway_id)
        )
    ).all()
    return {gateway_id: count for gateway_id, count in rows if gateway_id}


async def _active_gateway(gateway_id: str, session: AsyncSession) -> Gateway:
    gateway = await session.get(Gateway, gateway_id)
    if gateway is None or gateway.deleted_at is not None:
        raise HTTPException(status_code=404, detail="网关不存在")
    return gateway


def _owned_query(stmt, user: User):
    """列表只看自己的网关；管理员看全部。官方链路（kind=official）所有用户都能选用，
    因此对非管理员是「自己的 + 官方的」。"""
    if user.role == "admin":
        return stmt
    return stmt.where((Gateway.owner_id == user.id) | (Gateway.kind == "official"))


@router.get("")
async def list_gateways(
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    rows = (
        await session.execute(
            _owned_query(
                select(Gateway).where(Gateway.deleted_at.is_(None)).order_by(
                    Gateway.kind, Gateway.created_at
                ),
                user,
            )
        )
    ).scalars().all()
    counts = await _usage_counts(session)
    return {
        "items": [to_read(row, counts.get(row.id, 0)).model_dump() for row in rows],
        "protocols": [{"value": key, "label": label} for key, label in _protocol_options()],
        "auth_styles": [{"value": key, "label": label} for key, label in _auth_options()],
    }


def _protocol_options() -> list[tuple[str, str]]:
    from ..runtime.gateway_probe import PROTOCOL_LABELS

    return [(key, PROTOCOL_LABELS[key]) for key in PROTOCOLS]


def _auth_options() -> list[tuple[str, str]]:
    from ..runtime.gateway_probe import AUTH_LABELS

    return [(key, AUTH_LABELS[key]) for key in AUTH_STYLES]


@router.post("", status_code=201)
async def create_gateway(
    payload: GatewayCreate,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    if payload.protocol not in PROTOCOLS:
        raise HTTPException(status_code=422, detail=f"未知的网关协议：{payload.protocol}")
    if payload.auth_style not in AUTH_STYLES:
        raise HTTPException(status_code=422, detail=f"未知的鉴权方式：{payload.auth_style}")
    models = [item for item in payload.selected_models if str(item).strip()]
    if not models:
        raise HTTPException(status_code=422, detail="至少勾选一个模型：请先「获取模型」，或在旁路里手动录入")
    gateway = Gateway(
        name=payload.name,
        kind="custom",
        type_id=None,
        protocol=payload.protocol,
        auth_style=payload.auth_style,
        base_url=payload.base_url.strip(),
        api_key_encrypted=encrypt(payload.api_key),
        selected_models=models,
        model_catalog=models,
        extra_headers=dict(payload.extra_headers or {}),
        enabled=payload.enabled,
        owner_id=owner_id_for(user),
    )
    session.add(gateway)
    await session.commit()
    await session.refresh(gateway)
    return to_read(gateway).model_dump()


@router.get("/{gateway_id}")
async def get_gateway(
    gateway_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    gateway = await _active_gateway(gateway_id, session)
    # 官方链路对所有人可读（列表里也能看到）；自定义网关按归属隔离。
    if gateway.kind != "official":
        require_owned(gateway, user)
    counts = await _usage_counts(session)
    return to_read(gateway, counts.get(gateway.id, 0)).model_dump()


@router.patch("/{gateway_id}")
async def update_gateway(
    gateway_id: str,
    payload: GatewayUpdate,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    gateway = await _active_gateway(gateway_id, session)
    require_owned(gateway, user)
    data = payload.model_dump(exclude_unset=True)
    if "api_key" in data:
        secret = data.pop("api_key")
        gateway.api_key_encrypted = encrypt(secret) if secret else None
    if "protocol" in data and data["protocol"] not in PROTOCOLS:
        raise HTTPException(status_code=422, detail=f"未知的网关协议：{data['protocol']}")
    if "auth_style" in data and data["auth_style"] not in AUTH_STYLES:
        raise HTTPException(status_code=422, detail=f"未知的鉴权方式：{data['auth_style']}")
    if "selected_models" in data:
        models = [item for item in data.pop("selected_models") or [] if str(item).strip()]
        if not models:
            raise HTTPException(status_code=422, detail="至少保留一个模型")
        gateway.selected_models = models
    if "extra_headers" in data and data["extra_headers"] is not None:
        gateway.extra_headers = dict(data.pop("extra_headers") or {})
    for field, value in data.items():
        if value is not None:
            setattr(gateway, field, value)
    gateway.updated_at = datetime.now(timezone.utc)
    session.add(gateway)
    await session.commit()
    await session.refresh(gateway)
    counts = await _usage_counts(session)
    return to_read(gateway, counts.get(gateway.id, 0)).model_dump()


@router.delete("/{gateway_id}", status_code=204)
async def delete_gateway(
    gateway_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> Response:
    """删除网关：官方链路不可删；仍被**未删除**的智能体引用时返回 409。"""
    gateway = await _active_gateway(gateway_id, session)
    require_owned(gateway, user)
    if gateway.kind == "official":
        raise HTTPException(status_code=409, detail="官方链路由内核提供，不能删除")
    in_use = (
        await session.execute(
            select(AgentProfile.name).where(
                AgentProfile.gateway_id == gateway_id, AgentProfile.deleted_at.is_(None)
            )
        )
    ).scalars().all()
    if in_use:
        raise HTTPException(status_code=409, detail=f"该网关正被以下智能体使用：{'、'.join(in_use)}")
    gateway.deleted_at = datetime.now(timezone.utc)
    gateway.updated_at = gateway.deleted_at
    session.add(gateway)
    await session.commit()
    return Response(status_code=204)


@router.post("/probe")
async def probe(
    payload: GatewayProbeRequest,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """「获取模型」：探测一次并返回模型清单（不落库）。

    密钥留空且带 ``gateway_id`` 时复用已存密钥（重新探测已保存的网关）；复用的网关
    必须是当前用户有权访问的，否则任何人都能拿别人的密钥去探测。
    """
    api_key = payload.api_key
    if not api_key and payload.gateway_id:
        stored = await session.get(Gateway, payload.gateway_id)
        if stored is not None:
            require_owned(stored, user)
            api_key = decrypt(stored.api_key_encrypted)
    try:
        models = await probe_models(
            base_url=payload.base_url,
            protocol=payload.protocol,
            auth_style=payload.auth_style,
            api_key=api_key,
            extra_headers=payload.extra_headers,
        )
    except ProbeError as exc:
        return GatewayProbeResult(ok=False, message=str(exc)).model_dump()
    return GatewayProbeResult(
        ok=True, message=f"获取到 {len(models)} 个模型", models=models
    ).model_dump()


@router.post("/{gateway_id}/refresh-models")
async def refresh_models(
    gateway_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """用已存的配置重新拉一次模型，更新 ``model_catalog``（不动白名单）。"""
    gateway = await _active_gateway(gateway_id, session)
    require_owned(gateway, user)
    try:
        models = await probe_models(
            base_url=gateway.base_url,
            protocol=gateway.protocol,
            auth_style=gateway.auth_style,
            api_key=decrypt(gateway.api_key_encrypted),
            extra_headers=dict(gateway.extra_headers or {}),
        )
    except ProbeError as exc:
        return GatewayProbeResult(ok=False, message=str(exc)).model_dump()
    gateway.model_catalog = models
    gateway.last_probed_at = datetime.now(timezone.utc)
    gateway.updated_at = gateway.last_probed_at
    session.add(gateway)
    await session.commit()
    return GatewayProbeResult(ok=True, message=f"获取到 {len(models)} 个模型", models=models).model_dump()
