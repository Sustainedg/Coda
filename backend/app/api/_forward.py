"""同协议转发 + tee usage 记账：内部记录网关与外部模型网关共用的唯一一份。

只做**直通转发**：不改请求体、不做跨协议翻译、不落正文；响应流边转发边解析 usage，
终态写账本（独立会话，写失败不影响本轮）。调用方负责鉴权 / 配额 / 目标解析。
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from fastapi import HTTPException
from fastapi.responses import Response, StreamingResponse

from ..models import utcnow
from ..runtime import recording_gateway as gateway
from ..runtime import usage_ledger

logger = logging.getLogger("coda")


def _scrub_secret(payload: bytes, secret: str | None) -> bytes:
    """把上游密钥从有限大小的错误体里擦掉（流式响应无法逐块可靠擦除）。"""
    if not secret:
        return payload
    try:
        needle = secret.encode("utf-8")
    except UnicodeError:  # pragma: no cover - 密钥是文本，正常不会触发
        return payload
    return payload.replace(needle, b"***") if needle else payload


async def forward_and_record(
    *,
    target: gateway.ProxyTarget,
    method: str,
    path: str,
    query: str,
    headers: Any,
    body: bytes,
    origin: str = usage_ledger.ORIGIN_INTERNAL,
    api_key_id: str | None = None,
    record: bool = True,
) -> Response:
    """把一次请求转发到目标上游，并在响应终态记账。

    ``record=False`` 用于``/models`` 这类没有 usage 的元请求：只转发，不写账本、不占配额。
    """
    upstream_url = gateway.with_query_key(
        gateway.join_upstream_url(target.upstream_url, path, query), target
    )
    forward_headers = gateway.build_forward_headers(headers, target, inject=target.inject_key)

    client = gateway.make_upstream_client(target, upstream_url)
    started = time.monotonic()
    started_at = utcnow()
    try:
        upstream = await client.send(
            client.build_request(method, upstream_url, headers=forward_headers, content=body),
            stream=True,
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        # 上游不可达也算一次已发生的请求尝试：记 unknown，不伪造 0。
        if record:
            await gateway.persist_usage(
                target,
                gateway.UsageAccumulator(),
                status_code=502,
                started_at=started_at,
                duration_ms=gateway.elapsed_ms(started),
                origin=origin,
                api_key_id=api_key_id,
            )
        logger.warning("网关转发失败：%s %s（%s）", method, path, type(exc).__name__)
        raise HTTPException(status_code=502, detail="上游网关不可达") from None

    status_code = upstream.status_code
    media_type = upstream.headers.get("content-type", "application/json")
    response_headers = gateway.filter_response_headers(upstream.headers)

    # 非流式错误体：读出来、擦掉上游密钥后再转发，避免上游在错误信息里回显密钥。
    # 流式错误片段无法逐块可靠擦除（已知边界，见 docs/usage-gateway.md）。
    if status_code >= 400 and "text/event-stream" not in media_type.lower():
        raw = await upstream.aread()
        await upstream.aclose()
        await client.aclose()
        accumulator = gateway.UsageAccumulator()
        accumulator.feed(raw)
        accumulator.finish()
        if record:
            await gateway.persist_usage(
                target,
                accumulator,
                status_code=status_code,
                started_at=started_at,
                duration_ms=gateway.elapsed_ms(started),
                origin=origin,
                api_key_id=api_key_id,
            )
        return Response(
            content=_scrub_secret(raw, target.api_key),
            status_code=status_code,
            media_type=media_type,
            headers=response_headers,
        )

    accumulator = gateway.UsageAccumulator()

    async def stream_body():
        try:
            # 用 aiter_bytes（已解压）：content-encoding / content-length 已丢弃。
            async for chunk in upstream.aiter_bytes():
                accumulator.feed(chunk)
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()
            # 正常收尾 / 客户端断开 / 上游异常都会走到这里：已测 usage 立即入账。
            if record:
                await gateway.persist_usage(
                    target,
                    accumulator,
                    status_code=status_code,
                    started_at=started_at,
                    duration_ms=gateway.elapsed_ms(started),
                    origin=origin,
                    api_key_id=api_key_id,
                )

    return StreamingResponse(
        stream_body(),
        status_code=status_code,
        media_type=media_type,
        headers=response_headers,
    )


__all__ = ["forward_and_record"]