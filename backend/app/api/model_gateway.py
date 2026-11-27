"""外部模型网关：外部客户端带 API Key 调 ``/api/model-gateway/...``。

与内部记录网关的区别：

* **鉴权**：长期 API Key（``cosk_…``，按哈希查库），不是一次性代理 token；
* **目标**：key 绑定的自定义网关，平台用上游密钥 ``inject`` 转发；
* **记账**：``origin=external`` + ``api_key_id``，配额按 key 的日 / 月额度判（429）；
* **协议**：同协议直通（Anthropic Messages / OpenAI Chat / Responses），不做翻译。

非推理请求（``/models`` 等）只转发、不记账、不占配额，避免审计噪声。
"""
from __future__ import annotations

import logging
import uuid
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..api_keys import API_KEY_PREFIX, check_quota, find_active_key
from ..db import get_session
from ..models import Gateway, User
from ..runtime import recording_gateway as gateway
from ..runtime import usage_ledger
from ..security import decrypt
from ._forward import forward_and_record

logger = logging.getLogger("coda")

router = APIRouter(tags=["model-gateway"])
#: 推理请求路径标记：只有它们需要记账 / 占配额。
_INFERENCE_MARKERS = ("messages", "chat/completions", "responses", "completions")


def _is_inference(path: str) -> bool:
    lower = (path or "").lower()
    return any(marker in lower for marker in _INFERENCE_MARKERS)


def _extract_token(request: Request) -> str | None:
    """从 Authorization / x-api-key / 查询参数里取出外部 API Key。"""
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        candidate = auth[len("bearer ") :].strip()
        if candidate.startswith(API_KEY_PREFIX):
            return candidate
    for header in ("x-api-key", "anthropic-api-key"):
        candidate = request.headers.get(header)
        if candidate and candidate.startswith(API_KEY_PREFIX):
            return candidate
    candidate = request.query_params.get("api_key")
    if candidate and candidate.startswith(API_KEY_PREFIX):
        return candidate
    return None


def _forward_query(raw: str) -> str:
    """转发查询串时**剔除平台自己的 ``api_key``**，别把它泄露给上游。"""
    pairs = [
        (key, value)
        for key, value in parse_qsl(raw or "", keep_blank_values=True)
        if key.lower() != "api_key"
    ]
    return urlencode(pairs)


@router.api_route(
    "/model-gateway/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def model_gateway(
    path: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """校验 API Key 与配额，然后把请求转发到 key 绑定的上游网关并记账。"""
    key = await find_active_key(session, _extract_token(request))
    if key is None:
        raise HTTPException(status_code=401, detail="API Key 无效或已吊销")

    # 多用户安全：Key 的主人被禁用 / 删除时，这把 Key 立即失效（不只是吊销 Key 本身）。
    if key.owner_id is not None:
        owner = await session.get(User, key.owner_id)
        if owner is None or not owner.is_active:
            raise HTTPException(status_code=403, detail="API Key 的主人不可用")

    gateway_row = await session.get(Gateway, key.gateway_id)
    if (
        gateway_row is None
        or gateway_row.deleted_at is not None
        or not gateway_row.enabled
        or gateway_row.kind != "custom"
    ):
        raise HTTPException(status_code=403, detail="API Key 绑定的网关不可用")

    inference = _is_inference(path)
    if inference:
        reason = await check_quota(session, key)
        if reason:
            raise HTTPException(status_code=429, detail=reason)

    target = gateway.ProxyTarget(
        token=key.id,
        # 外部请求没有平台 run：用 key + 随机串做去重前缀（上游有响应 id 时优先用它）。
        run_id=f"ext:{key.id}:{uuid.uuid4().hex[:8]}",
        upstream_url=gateway_row.base_url,
        protocol=gateway_row.protocol,
        auth_style=gateway_row.auth_style,
        api_key=decrypt(gateway_row.api_key_encrypted),
        inject_key=True,
        owner_id=key.owner_id,
        conversation_id=None,
        agent_type_id="external",
        agent_profile_id=None,
        model="",
        provider=gateway_row.protocol,
    )
    return await forward_and_record(
        target=target,
        method=request.method,
        path=path,
        query=_forward_query(request.url.query),
        headers=request.headers,
        body=await request.body(),
        origin=usage_ledger.ORIGIN_EXTERNAL,
        api_key_id=key.id,
        record=inference,
    )


__all__ = ["router"]