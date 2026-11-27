"""外部模型 API Key 的签发 / 列表 / 吊销 / 调参。

**明文只返回一次**（``POST`` 的响应里）；之后列表只给掩码。配额字段为空 = 不限制。
非管理员只能管理自己的 key（管理员可看全部）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..api_keys import issue_api_key
from ..auth_deps import current_user, owner_id_for, require_owned
from ..db import get_session
from ..models import ApiKey, Gateway, User

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


class ApiKeyCreate(BaseModel):
    name: str = ""
    gateway_id: str
    daily_request_limit: int | None = None
    daily_token_limit: int | None = None
    monthly_request_limit: int | None = None
    monthly_token_limit: int | None = None


class ApiKeyUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    daily_request_limit: int | None = None
    daily_token_limit: int | None = None
    monthly_request_limit: int | None = None
    monthly_token_limit: int | None = None


_LIMIT_FIELDS = (
    "daily_request_limit",
    "daily_token_limit",
    "monthly_request_limit",
    "monthly_token_limit",
)


def _validate_limits(values: dict[str, Any]) -> None:
    for field in _LIMIT_FIELDS:
        value = values.get(field)
        if value is not None and (isinstance(value, bool) or value < 0):
            raise HTTPException(status_code=422, detail=f"{field} 必须是非负整数或留空")


def _key_view(row: ApiKey, gateway_name: str | None = None) -> dict[str, Any]:
    """对外结构：**绝不包含 key_hash**。"""
    return {
        "id": row.id,
        "name": row.name,
        "gateway_id": row.gateway_id,
        "gateway_name": gateway_name,
        "key_mask": row.key_mask,
        "enabled": row.enabled,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "daily_request_limit": row.daily_request_limit,
        "daily_token_limit": row.daily_token_limit,
        "monthly_request_limit": row.monthly_request_limit,
        "monthly_token_limit": row.monthly_token_limit,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def _load_owned_key(session: AsyncSession, key_id: str, user: User) -> ApiKey:
    row = await session.get(ApiKey, key_id)
    if row is None:
        raise HTTPException(status_code=404, detail="API Key 不存在")
    require_owned(row, user)
    return row


@router.post("")
async def create_api_key(
    body: ApiKeyCreate,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """签发一把新 key。**明文只在这次响应里出现**，请立刻保存。"""
    gateway = await session.get(Gateway, body.gateway_id)
    if gateway is None or gateway.deleted_at is not None:
        raise HTTPException(status_code=404, detail="指定的网关不存在")
    require_owned(gateway, user)
    if gateway.kind != "custom":
        raise HTTPException(status_code=422, detail="外部 API 只能绑定自定义网关")

    values = {"name": body.name, **{field: getattr(body, field) for field in _LIMIT_FIELDS}}
    _validate_limits(values)
    raw, key_hash, key_mask = issue_api_key()
    row = ApiKey(
        owner_id=owner_id_for(user),
        gateway_id=gateway.id,
        key_hash=key_hash,
        key_mask=key_mask,
        **values,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return {**_key_view(row, gateway.name), "key": raw, "key_warning": "明文只显示这一次，请立即保存"}


@router.get("")
async def list_api_keys(
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """列出 key（只有掩码）。非管理员只看自己的，管理员看全部。"""
    stmt = select(ApiKey)
    if user.role != "admin":
        stmt = stmt.where(ApiKey.owner_id == owner_id_for(user))
    rows = (
        await session.execute(stmt.order_by(ApiKey.created_at.desc()))
    ).scalars().all()
    gateways = {
        row.id: row.name for row in (await session.execute(select(Gateway))).scalars().all()
    }
    return {"keys": [_key_view(row, gateways.get(row.gateway_id)) for row in rows]}


@router.patch("/{key_id}")
async def update_api_key(
    key_id: str,
    body: ApiKeyUpdate,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """改名字 / 开关 / 配额。只应用请求里**显式出现**的字段（传 null = 清空限制）。"""
    row = await _load_owned_key(session, key_id, user)
    provided = body.model_fields_set
    values = {field: getattr(body, field) for field in _LIMIT_FIELDS if field in provided}
    _validate_limits(values)
    for field in ("name", "enabled", *_LIMIT_FIELDS):
        if field in provided:
            setattr(row, field, getattr(body, field))
    row.updated_at = datetime.now(timezone.utc)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    gateway = await session.get(Gateway, row.gateway_id)
    return _key_view(row, gateway.name if gateway else None)


@router.delete("/{key_id}")
async def revoke_api_key(
    key_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """吊销（保留审计记录，不物理删除）。"""
    row = await _load_owned_key(session, key_id, user)
    row.enabled = False
    row.revoked_at = datetime.now(timezone.utc)
    row.updated_at = row.revoked_at
    session.add(row)
    await session.commit()
    return {"ok": True, "id": row.id, "revoked_at": row.revoked_at.isoformat()}


__all__ = ["router"]