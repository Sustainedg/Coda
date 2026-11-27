"""记录网关的内部转发路由（``/api/internal/usage-gateway/<token>/...``）。

**不进公开 OpenAPI**（``include_in_schema=False``），**不走用户鉴权**：调用方是 CLI 子进程，
凭据是 URL 里的**一次性代理 token** + **回环来源校验**。两道防线缺一不可——不能因为
「有 token 就行」就允许公网访问（见计划 §5.8）。

转发与记账复用 ``api/_forward.py``（与外部模型网关同一份流式逻辑）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from ..runtime import recording_gateway as gateway
from ._forward import forward_and_record

logger = logging.getLogger("coda")

router = APIRouter(tags=["internal"])

#: 回环主机名白名单（IPv4 / IPv6 / 字面量）。
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def is_loopback(host: str | None) -> bool:
    """只允许本机回环来源。``None`` 一律拒绝（拿不到来源就当不安全）。"""
    if not host:
        return False
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


@router.api_route(
    "/internal/usage-gateway/{token}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def usage_gateway(token: str, path: str, request: Request):
    """校验来源与 token，然后同协议转发到目标上游，并在响应终态记账。"""
    client_host = request.client.host if request.client else None
    if not is_loopback(client_host):
        raise HTTPException(status_code=403, detail="记录网关只接受本机回环请求")

    target = gateway.registry.resolve(token)
    if target is None:
        raise HTTPException(status_code=401, detail="记录网关令牌无效或已过期")

    return await forward_and_record(
        target=target,
        method=request.method,
        path=path,
        query=request.url.query,
        headers=request.headers,
        body=await request.body(),
    )


__all__ = ["router", "is_loopback"]