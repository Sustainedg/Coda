"""同协议转发 + tee usage 记账：内部记录网关与外部模型网关共用的唯一一份。

只做**直通转发**：不改请求体、不做跨协议翻译；**只在本轮开启轨迹调试时**把脱敏后的
请求 / 响应写进轨迹（内容只进 ``trace_events``，不进消息表、不进日志）。响应流边转发边
解析 usage，终态写账本（独立会话，写失败不影响本轮）。调用方负责鉴权 / 配额 / 目标解析。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Sequence

import httpx
from fastapi import HTTPException
from fastapi.responses import Response, StreamingResponse

from ..credentials import scrub
from ..models import utcnow
from ..runtime import recording_gateway as gateway
from ..runtime import trace as trace_mod
from ..runtime import usage_ledger
from ..runtime.manager import registry as run_registry

logger = logging.getLogger("coda")

#: 可能在上游错误体里回显的客户端凭据头（passthrough 模式下平台没有密钥，只能从请求里取）。
_CLIENT_CREDENTIAL_HEADERS = (
    "authorization",
    "x-api-key",
    "api-key",
    "proxy-authorization",
    "x-goog-api-key",
)


def _header_value(headers: Any, name: str) -> str | None:
    """大小写不敏感地取一个请求头的值（Starlette / httpx / dict 都适用）。"""
    for key, value in headers.items():
        if str(key).lower() == name:
            return str(value)
    return None


def _client_secrets(target: gateway.ProxyTarget, headers: Any) -> list[str]:
    """本轮要擦除的凭据值：上游密钥 + 客户端带来的鉴权头（含 Bearer 裸 token）。

    passthrough（官方 OAuth）时 ``target.api_key`` 是 ``None``，上游错误体可能把用户自己的
    OAuth 令牌回显给前端——所以必须从请求头里把凭据值也收集进来。
    """
    values: list[str] = []
    if target.api_key:
        values.append(target.api_key)
    for name in _CLIENT_CREDENTIAL_HEADERS:
        value = _header_value(headers, name)
        if not value:
            continue
        values.append(value)
        if value.lower().startswith("bearer "):
            token = value[7:].strip()
            if token:
                values.append(token)
    # 去重，长的先擦，避免先擦到短串导致长串残缺。
    return sorted({value for value in values if value}, key=len, reverse=True)


def _scrub_secrets(payload: bytes, secrets: list[str]) -> bytes:
    """把凭据值从有限大小的错误体里擦掉（流式响应无法逐块可靠擦除）。"""
    for secret in secrets:
        try:
            needle = secret.encode("utf-8")
        except UnicodeError:  # pragma: no cover - 密钥是文本，正常不会触发
            continue
        if needle:
            payload = payload.replace(needle, b"***")
    return payload


def _recorder_for(target: gateway.ProxyTarget):
    """按 ``run_id`` 反查本轮轨迹采集器；关调试 / 非 CLI 内核 / 已结束一律返回 None。"""
    if not target.capture_trace:
        return None
    run = run_registry.get(target.run_id)
    recorder = getattr(run, "trace", None) if run is not None else None
    if recorder is None or not getattr(recorder, "enabled", False):
        return None
    return recorder


def _json_or_text(raw: bytes) -> Any:
    """请求 / 响应正文：能解析成 JSON 就存结构，否则存文本。

    采集是旁路：畸形 / 超深嵌套的 JSON 解析失败时退回文本，绝不影响转发。
    """
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
        return raw.decode("utf-8", "ignore")


def _sse_events(text: str) -> list[Any]:
    """把 SSE 文本拆成事件列表，方便在轨迹里逐条查看；解析失败的行原样保留。"""
    events: list[Any] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("data:"):
            continue
        payload = stripped[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            events.append(json.loads(payload))
        except (ValueError, TypeError, RecursionError):
            events.append(payload)
    return events


async def _trace_event(
    recorder: Any,
    event_type: str,
    payload: dict[str, Any],
    *,
    secrets: Sequence[str] = (),
    duration_ms: int | None = None,
    error: str | None = None,
    truncated: bool = False,
) -> None:
    """写一条网关轨迹；任何失败只告警，绝不打断转发。

    ``error`` 单独成列（不进 payload），所以**必须在这里先脱敏**：httpx 的异常文本
    可能带上完整 URL，而 ``auth_style=query`` 时 URL 查询串里有上游密钥。
    """
    if recorder is None:
        return
    try:
        safe, _ = trace_mod.sanitize(payload, secrets=secrets)
        if not isinstance(safe, dict):
            safe = {"value": safe}
        safe_error = scrub(error, secrets) if error else error
        if event_type == "request":
            await recorder.request(
                safe, duration_ms=duration_ms, error=safe_error, truncated=truncated
            )
        else:
            await recorder.response(
                safe, duration_ms=duration_ms, error=safe_error, truncated=truncated
            )
    except Exception:  # noqa: BLE001 - 旁路采集不能影响转发
        logger.warning("记录网关写轨迹失败（不影响转发）", exc_info=True)


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

    ``record=False`` 用于 ``/models`` 这类没有 usage 的元请求：只转发，不写账本、不占配额。
    本轮开启轨迹调试且 ``target.capture_trace`` 时，额外写脱敏的请求 / 响应轨迹；
    采集失败只告警，绝不影响转发。
    """
    upstream_url = gateway.with_query_key(
        gateway.join_upstream_url(target.upstream_url, path, query), target
    )
    forward_headers = gateway.build_forward_headers(headers, target, inject=target.inject_key)

    # 轨迹采集器与要擦除的凭据值：关调试时 recorder 为 None，后续全是空操作。
    recorder = _recorder_for(target)
    secrets = _client_secrets(target, headers)
    if recorder is not None:
        await _trace_event(
            recorder,
            "request",
            {
                "transport": "gateway",
                "method": method,
                "path": path,
                "upstream": target.base_url,
                # 只留白名单头：自定义头可能夹带密钥，不入轨迹（方案 §8.2）。
                "headers": gateway.trace_safe_headers(headers),
                "body": _json_or_text(body),
            },
            secrets=secrets,
        )

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
        await _trace_event(
            recorder,
            "response",
            {"transport": "gateway", "status_code": 502, "error": type(exc).__name__},
            secrets=secrets,
            duration_ms=gateway.elapsed_ms(started),
            error=str(exc)[:500],
        )
        logger.warning("网关转发失败：%s %s（%s）", method, path, type(exc).__name__)
        raise HTTPException(status_code=502, detail="上游网关不可达") from None

    status_code = upstream.status_code
    media_type = upstream.headers.get("content-type", "application/json")
    response_headers = gateway.filter_response_headers(upstream.headers)

    # 非流式错误体：读出来、擦掉凭据后再转发，避免上游在错误信息里回显密钥。
    # passthrough 模式下平台没有密钥，凭据值从客户端请求头收集（见 _client_secrets）。
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
        if recorder is not None:
            await _trace_event(
                recorder,
                "response",
                {
                    "transport": "gateway",
                    "status_code": status_code,
                    "content_type": media_type,
                    "bytes": len(raw),
                    "body": _json_or_text(raw),
                },
                secrets=secrets,
                duration_ms=gateway.elapsed_ms(started),
            )
        return Response(
            content=_scrub_secrets(raw, secrets),
            status_code=status_code,
            media_type=media_type,
            headers=response_headers,
        )

    accumulator = gateway.UsageAccumulator()
    # 响应正文只为轨迹保留一份有上限的副本：超过单事件上限就停采并标 truncated。
    capture_limit = trace_mod.MAX_EVENT_BYTES
    captured = bytearray()
    captured_truncated = False
    total_bytes = 0

    async def stream_body():
        nonlocal captured_truncated, total_bytes
        try:
            # 用 aiter_bytes（已解压）：content-encoding / content-length 已丢弃。
            async for chunk in upstream.aiter_bytes():
                accumulator.feed(chunk)
                total_bytes += len(chunk)
                if recorder is not None and len(captured) < capture_limit:
                    captured.extend(chunk[: capture_limit - len(captured)])
                if recorder is not None and total_bytes > capture_limit:
                    captured_truncated = True
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
            if recorder is not None:
                text = bytes(captured).decode("utf-8", "ignore")
                body: Any = (
                    {"sse_events": _sse_events(text)}
                    if "text/event-stream" in media_type.lower()
                    else _json_or_text(bytes(captured))
                )
                await _trace_event(
                    recorder,
                    "response",
                    {
                        "transport": "gateway",
                        "status_code": status_code,
                        "content_type": media_type,
                        "bytes": total_bytes,
                        "body": body,
                    },
                    secrets=secrets,
                    duration_ms=gateway.elapsed_ms(started),
                    truncated=captured_truncated,
                )

    return StreamingResponse(
        stream_body(),
        status_code=status_code,
        media_type=media_type,
        headers=response_headers,
    )


__all__ = ["forward_and_record"]