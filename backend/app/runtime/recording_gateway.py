"""记录网关：本机反向代理 + 同协议转发 + tee 响应 + 一手 usage 记账。

见 ``docs/usage-gateway.md`` 的「记录网关」章节。职责边界：

* **做**：同协议转发（Anthropic Messages / OpenAI Chat / OpenAI Responses）、透传必要请求头、
  按 ``auth_style`` 注入上游密钥、tee 响应解析 usage、按 ``request_id`` 去重写账本、
  失败 / 取消 / 客户端断开时写已测用量、一次性代理 token 的注册与回收。
* **不做**：跨协议翻译、请求体改写、工具执行、公网暴露、账本落正文、
  把上游密钥写进子进程环境或日志。
* **轨迹**：仅在用户开启调试且本 run 有采集器时，转发层把脱敏后的请求 / 响应写进
  ``trace_events``（``transport="gateway"``）；不进账本、不进消息表、不进日志。

安全模型：

* 只监听 ``127.0.0.1``（路由层再校验来源是回环地址，见 ``api/usage_gateway.py``）；
* 每个 run 一个 256-bit 一次性 token（``secrets.token_urlsafe(32)``），绑定 run / owner /
  conversation，TTL 到期即失效，run 结束由调用方吊销；
* ``inject`` 模式下上游密钥只存在服务端内存注册表，子进程 env 里只有代理 token。

**开关**（§8 P1）：``CODA_RECORDING_GATEWAY=off | auto | on``，默认 ``off``。

本模块**不 import FastAPI**：转发路由在 ``api/usage_gateway.py``，这里只提供可单测的纯机器。
"""
from __future__ import annotations

import codecs
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping

import httpx

from ..db import SessionLocal
from ..egress import wrap_client
from ..agents.metering import (
    PROTOCOL_UNKNOWN,
    RESPONSE_SHAPE_ANTHROPIC,
    RESPONSE_SHAPE_CHAT,
    RESPONSE_SHAPE_RESPONSES,
    resolve_protocol,
)
from ..models import ApiKey, utcnow
from ..settings import get_settings
from . import usage_ledger as ledger

logger = logging.getLogger("coda")

# ---------------------------------------------------------------------------
# 开关
# ---------------------------------------------------------------------------

MODE_OFF = "off"
MODE_AUTO = "auto"
MODE_ON = "on"
VALID_MODES = (MODE_OFF, MODE_AUTO, MODE_ON)


def recording_gateway_mode() -> str:
    """当前记录网关模式；非法值一律回落 ``off``（安全默认）。"""
    raw = (os.environ.get("CODA_RECORDING_GATEWAY") or "").strip().lower()
    return raw if raw in VALID_MODES else MODE_OFF


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ProxyTarget:
    """一次 run 的代理目标：上游地址 / 密钥 / 归属与 TTL。"""

    token: str
    run_id: str
    upstream_url: str
    protocol: str = "anthropic"
    auth_style: str = "bearer"
    #: 上游密钥：**不进 repr**，避免日志 / 调试打印意外泄露。
    api_key: str | None = field(default=None, repr=False)
    #: ``inject`` = 用服务端密钥替换子进程带来的代理 token；``passthrough`` = 原样转发。
    inject_key: bool = True
    owner_id: str | None = None
    conversation_id: str | None = None
    agent_type_id: str = ""
    agent_profile_id: str | None = None
    model: str = ""
    provider: str = ""
    expires_at: datetime = field(default_factory=utcnow)
    created_at: datetime = field(default_factory=utcnow)
    #: 无上游响应 id 时用来生成 ``<run_id>:<seq>`` 的请求序号。
    seq: int = 0
    #: 本 run 的消息创建时刻：用作 ``usage_ledger_started_at`` 的对齐点，避免边界轮
    #: 被「旧消息段 + 账本段」各算一次。
    run_started_at: datetime | None = None
    #: 是否把转发报文写进本 run 的轨迹。只给 CLI 内核置真：它们的真实 HTTP 请求发生在
    #: 子进程内部，网关是唯一可见点；API 内核（Harness）自己已采集完整请求，避免双记。
    capture_trace: bool = False
    #: 内核原始 usage 的协议（与请求协议可能不同：CodeBuddy 请求走 OpenAI Chat，
    #: 但响应 usage 是 Anthropic 形状）。账本归一化优先用它，而不是 ``protocol``。
    usage_protocol: str | None = None

    @property
    def base_url(self) -> str:
        """脱敏后的上游地址（不含密钥，可落账本 / 日志）。"""
        return self.upstream_url


class TokenRegistry:
    """进程内的代理 token 表。单事件循环下无需加锁；测试可整体 ``clear``。"""

    def __init__(self, ttl_seconds: int = 2 * 60 * 60) -> None:
        self._targets: dict[str, ProxyTarget] = {}
        self._ttl = ttl_seconds

    def register(
        self,
        *,
        run_id: str,
        upstream_url: str,
        protocol: str = "anthropic",
        auth_style: str = "bearer",
        api_key: str | None = None,
        inject_key: bool = True,
        owner_id: str | None = None,
        conversation_id: str | None = None,
        agent_type_id: str = "",
        agent_profile_id: str | None = None,
        model: str = "",
        provider: str = "",
        run_started_at: datetime | None = None,
        ttl_seconds: int | None = None,
        capture_trace: bool = False,
        usage_protocol: str | None = None,
    ) -> ProxyTarget:
        token = secrets.token_urlsafe(32)
        target = ProxyTarget(
            token=token,
            run_id=run_id,
            upstream_url=upstream_url,
            protocol=protocol,
            auth_style=auth_style,
            api_key=api_key,
            inject_key=inject_key,
            owner_id=owner_id,
            conversation_id=conversation_id,
            agent_type_id=agent_type_id,
            agent_profile_id=agent_profile_id,
            model=model,
            provider=provider,
            run_started_at=run_started_at,
            capture_trace=capture_trace,
            usage_protocol=usage_protocol,
            # 注意 0 是合法 TTL（立即过期，用于测试 / 强制失效），不能被 ``or`` 吞掉。
            expires_at=utcnow()
            + timedelta(seconds=self._ttl if ttl_seconds is None else ttl_seconds),
        )
        self._targets[token] = target
        return target

    def resolve(self, token: str) -> ProxyTarget | None:
        """取有效目标；不存在 / 已过期返回 None（过期顺便清掉）。"""
        target = self._targets.get(token)
        if target is None:
            return None
        if target.expires_at <= utcnow():
            self._targets.pop(token, None)
            return None
        return target

    def revoke(self, token: str) -> None:
        self._targets.pop(token, None)

    def revoke_run(self, run_id: str) -> None:
        for token in [k for k, v in self._targets.items() if v.run_id == run_id]:
            self._targets.pop(token, None)

    def clear(self) -> None:
        self._targets.clear()

    def __len__(self) -> int:  # pragma: no cover - 仅调试 / 测试
        return len(self._targets)


#: 全局注册表（进程内）。
registry = TokenRegistry()


def proxy_base_url() -> str:
    """代理对外的本机地址。

    默认 ``http://127.0.0.1:<port>``；Docker / 宿主机分离等场景可用
    ``CODA_RECORDING_GATEWAY_BASE`` 覆盖。**只应指向回环 / 同 namespace 地址**。
    """
    override = (os.environ.get("CODA_RECORDING_GATEWAY_BASE") or "").strip()
    if override:
        return override.rstrip("/")
    return f"http://127.0.0.1:{get_settings().port}"


def proxy_url_for(token: str) -> str:
    return f"{proxy_base_url()}/api/internal/usage-gateway/{token}"


# ---------------------------------------------------------------------------
# URL 拼接 / 请求头
# ---------------------------------------------------------------------------


def join_upstream_url(base_url: str, path: str, query: str = "") -> str:
    """把上游 base 与 CLI 追加的路径拼起来，处理 ``/v1`` 重复、末尾斜杠与 query。

    CLI 拿到的 base_url 是代理地址；它会在后面追加自己协议路径（如 ``/v1/messages``、
    ``/chat/completions``、``/responses``）。若上游 base 本身已以 ``/v1`` 结尾，要避免
    拼出 ``/v1/v1/...``。
    """
    base = (base_url or "").strip().rstrip("/")
    clean_path = (path or "").strip().lstrip("/")
    if base.endswith("/v1"):
        if clean_path == "v1":
            clean_path = ""
        elif clean_path.startswith("v1/"):
            clean_path = clean_path[3:]
    url = f"{base}/{clean_path}" if clean_path else base
    q = (query or "").lstrip("?")
    if q:
        url = f"{url}?{q}"
    return url


#: 透传给上游的请求头（其余一律丢弃，尤其是 host / 鉴权 / 逐跳头）。
_FORWARD_HEADERS = {
    "content-type",
    "accept",
    "user-agent",
    "anthropic-version",
    "anthropic-beta",
    "anthropic-dangerous-direct-browser-access",
    "openai-beta",
    "openai-organization",
    "openai-project",
}
#: passthrough（官方 OAuth）额外透传的身份头：Codex 的 ChatGPT 登录会带
#: ``chatgpt-account-id`` / ``originator`` 等，丢了上游会拒；Claude Code 订阅登录
#: 也会带自己的 session 头。inject 模式下这些一律丢弃（属于子进程内部信息）。
_PASSTHROUGH_IDENTITY_HEADERS = {
    "chatgpt-account-id",
    "originator",
    "session_id",
    "conversation_id",
}

#: 轨迹里允许保留的请求头：转发白名单 + 鉴权 / 身份头。鉴权头的**值**会被
#: ``trace.sanitize`` 掩码；自定义头（可能夹带密钥）一律不入轨迹，避免明文密钥入库。
_TRACE_HEADERS = (
    _FORWARD_HEADERS
    | {"authorization", "x-api-key", "cookie", "set-cookie"}
    | _PASSTHROUGH_IDENTITY_HEADERS
)


def trace_safe_headers(incoming: Mapping[str, str] | Any) -> dict[str, str]:
    """按白名单挑出可写入轨迹的请求头（键名保留，值仍由 ``sanitize`` 脱敏）。"""
    return {
        str(key): str(value)
        for key, value in incoming.items()
        if str(key).lower() in _TRACE_HEADERS
    }


#: 响应侧不透传的头（由本服务 / httpx 重新生成）。
_DROP_RESPONSE_HEADERS = {
    "content-length",
    "content-encoding",
    "transfer-encoding",
    "connection",
    "keep-alive",
}


def build_forward_headers(
    incoming: Mapping[str, str] | Any,
    target: ProxyTarget,
    *,
    inject: bool = True,
) -> dict[str, str]:
    """过滤并补齐转发头。``inject`` 时用服务端上游密钥替换客户端鉴权头。

    ``passthrough``（官方登录 / 内核自带鉴权）时保留客户端自己的 ``authorization`` /
    ``x-api-key``；``inject`` 时把它们丢掉，避免代理 token 泄露到上游。
    """
    allowed = set(_FORWARD_HEADERS)
    if not inject:
        allowed |= {"authorization", "x-api-key"} | _PASSTHROUGH_IDENTITY_HEADERS
    headers: dict[str, str] = {}
    for key, value in incoming.items():
        lower = key.lower()
        if lower not in allowed:
            continue
        headers[key] = value
    if inject and target.api_key:
        style = target.auth_style or "bearer"
        if style == "x-api-key":
            headers["x-api-key"] = target.api_key
        elif style == "bearer":
            headers["authorization"] = f"Bearer {target.api_key}"
        # query / none：query 在 URL 上补，none 不带头。
    return headers


def filter_response_headers(headers: Any) -> dict[str, str]:
    """过滤上游响应头：丢掉逐跳 / 长度 / 编码头（body 已经解码，长度会变）。"""
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in _DROP_RESPONSE_HEADERS
    }


def with_query_key(url: str, target: ProxyTarget) -> str:
    """``auth_style=query`` 时把密钥拼进查询串（只对 inject 模式生效）。"""
    if not (target.inject_key and target.api_key and target.auth_style == "query"):
        return url
    from urllib.parse import quote

    sep = "&" if "?" in url else "?"
    return f"{url}{sep}key={quote(target.api_key, safe='')}"


# ---------------------------------------------------------------------------
# usage 解析
# ---------------------------------------------------------------------------


def _merge_counts(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    """把一份 usage 合并进累计值：非 0 覆盖，0 只在缺失时补（兼容 message_start 全 0）。"""
    for key, value in source.items():
        if isinstance(value, dict):
            child = target.get(key)
            if not isinstance(child, dict):
                child = {}
                target[key] = child
            _merge_counts(child, value)
        elif isinstance(value, bool):
            continue
        elif isinstance(value, (int, float)):
            if value != 0 or key not in target:
                target[key] = int(value)


class UsageAccumulator:
    """边转发边解析 usage：SSE 逐行喂，或整包 JSON。

    支持 Anthropic Messages（``message_start`` / ``message_delta``）、OpenAI Chat
    （末尾带 ``usage`` 的 chunk）、OpenAI Responses（``response.completed`` 的
    ``response.usage``），以及它们的非流式单体 JSON。

    同一响应多次上报 usage 时**合并到同一份**（最终有效值覆盖早期 0），对应账本里
    「同一 request_id 只一行、以最终值覆盖」。
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buffer = ""
        self._usage: dict[str, Any] = {}
        self._request_id: str | None = None
        self._model: str | None = None
        #: 实际观察到的响应形状（``anthropic_messages`` / ``chat`` / ``responses``）。
        #: 比网关配置里的粗粒度 ``protocol`` 更准：OpenAI 的 Chat 与 Responses
        #: 字段语义不同，必须分开归一化。
        self._shape: str | None = None

    # -- 解析入口 ------------------------------------------------------------

    def feed(self, chunk: bytes | str) -> None:
        text = self._decoder.decode(chunk) if isinstance(chunk, (bytes, bytearray)) else chunk
        if not text:
            return
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._consume_line(line)

    def feed_json(self, payload: Any) -> None:
        """直接喂一个已解析的 JSON 对象（非流式响应用）。"""
        self._consume_obj(payload)

    def finish(self) -> None:
        """收尾：把最后一截没有换行的缓冲也解析掉。"""
        tail = self._decoder.decode(b"", final=True) if self._buffer else ""
        self._buffer += tail
        if self._buffer.strip():
            self._consume_line(self._buffer)
            self._buffer = ""

    def _consume_line(self, line: str) -> None:
        line = line.rstrip("\r").strip()
        if not line or line.startswith(":") or line.startswith("event:"):
            return
        if line.startswith("data:"):
            payload = line[len("data:") :].strip()
        else:
            payload = line
        if not payload or payload == "[DONE]":
            return
        try:
            obj = json.loads(payload)
        except (ValueError, TypeError):
            return
        self._consume_obj(obj)

    def _consume_obj(self, obj: Any) -> None:
        if not isinstance(obj, dict):
            return
        kind = obj.get("type")
        if kind == "message_start":
            message = obj.get("message") or {}
            self._set_id(message.get("id"))
            self._set_model(message.get("model"))
            self._merge_usage(message.get("usage"))
            self._shape = RESPONSE_SHAPE_ANTHROPIC
            return
        if kind == "message_delta":
            self._merge_usage(obj.get("usage"))
            self._shape = RESPONSE_SHAPE_ANTHROPIC
            return
        if kind in ("response.completed", "response.incomplete", "response.failed"):
            response = obj.get("response") or {}
            self._set_id(response.get("id"))
            self._set_model(response.get("model"))
            self._merge_usage(response.get("usage"))
            self._shape = RESPONSE_SHAPE_RESPONSES
            return
        if kind in ("content_block_delta", "content_block_start", "content_block_stop",
                    "message_stop", "ping", "response.output_text.delta",
                    "response.created", "response.in_progress"):
            return
        # 非流式单体 JSON：Anthropic message / OpenAI chat.completion / OpenAI response。
        if isinstance(obj.get("usage"), dict):
            self._set_id(obj.get("id"))
            self._set_model(obj.get("model"))
            self._merge_usage(obj.get("usage"))
            self._shape = self._shape_for_non_stream(obj)

    # -- 累计 ----------------------------------------------------------------

    def _set_id(self, value: Any) -> None:
        if self._request_id is None and isinstance(value, str) and value:
            self._request_id = value[:120]

    def _set_model(self, value: Any) -> None:
        if self._model is None and isinstance(value, str) and value:
            self._model = value[:160]

    def _merge_usage(self, usage: Any) -> None:
        if isinstance(usage, dict):
            _merge_counts(self._usage, usage)

    @staticmethod
    def _shape_for_non_stream(obj: Mapping[str, Any]) -> str:
        """非流式单体 JSON 的形状判定（Chat vs Responses vs Anthropic）。"""
        obj_type = str(obj.get("type") or "")
        object_name = str(obj.get("object") or "")
        if obj_type == "message":
            return RESPONSE_SHAPE_ANTHROPIC
        if "choices" in obj or object_name.startswith("chat.completion"):
            return RESPONSE_SHAPE_CHAT
        if "output" in obj or object_name.startswith("response"):
            return RESPONSE_SHAPE_RESPONSES
        if "content" in obj and "choices" not in obj:
            return RESPONSE_SHAPE_ANTHROPIC
        # 兜底按 OpenAI Chat（兼容端点最常见）；不猜 Anthropic，避免把缓存加两遍。
        return RESPONSE_SHAPE_CHAT

    @property
    def request_id(self) -> str | None:
        return self._request_id

    @property
    def model(self) -> str | None:
        return self._model

    @property
    def raw_usage(self) -> dict[str, Any]:
        return self._usage

    @property
    def response_shape(self) -> str | None:
        """实际观察到的响应形状；一次请求内一旦确定就不再变。"""
        return self._shape

    def counts(self, protocol: str | None = None) -> dict[str, int | None]:
        return ledger.normalize_usage(
            self._usage, protocol=protocol or self._shape
        )

    def window_tokens(self, protocol: str | None = None) -> int | None:
        return ledger.window_tokens(self._usage, protocol=protocol or self._shape)


# ---------------------------------------------------------------------------
# 记账（独立会话：失败不影响转发 / 本轮对话）
# ---------------------------------------------------------------------------


def request_id_for(target: ProxyTarget, accumulator: UsageAccumulator) -> str:
    """优先上游响应 id，没有就按 ``<run_id>:<seq>`` 生成一个稳定去重键。"""
    if accumulator.request_id:
        return accumulator.request_id
    target.seq += 1
    return f"{target.run_id}:{target.seq}"


async def persist_usage(
    target: ProxyTarget,
    accumulator: UsageAccumulator,
    *,
    status_code: int,
    started_at: datetime,
    duration_ms: int,
    origin: str = ledger.ORIGIN_INTERNAL,
    api_key_id: str | None = None,
) -> bool:
    """把一次请求的已测用量写进账本；任何异常只告警，绝不向调用方抛出。

    返回是否**新增**一行（去重后可能为 False）。
    """
    try:
        # 归一化优先看「实际响应形状」→「内核声明的 usage 协议」→「请求协议」。
        # 三者不能混：CodeBuddy 请求是 OpenAI Chat，但 usage 是 Anthropic 形状，
        # 用请求协议归一化会把缓存 token 算重。
        # ``unknown`` 是「未验证、别猜」的硬阻塞值，但它不该把更粗的请求协议也挡住：
        # 这里把 unknown 视为「没有提示」，继续回落到请求协议 / 字段探测。
        hint = target.usage_protocol
        if not hint or hint == PROTOCOL_UNKNOWN:
            hint = target.protocol
        protocol = resolve_protocol(
            accumulator.response_shape or hint,
            accumulator.raw_usage,
        )
        counts = accumulator.counts(protocol=protocol)
        request_id = request_id_for(target, accumulator)
        record = ledger.build_record(
            run_id=target.run_id,
            request_id=request_id,
            source=ledger.SOURCE_GATEWAY,
            counts=counts,
            raw_usage=accumulator.raw_usage,
            usage_protocol=protocol,
            response_shape=accumulator.response_shape,
            upstream_request_id=accumulator.request_id,
            owner_id=target.owner_id,
            conversation_id=target.conversation_id,
            agent_type_id=target.agent_type_id,
            agent_profile_id=target.agent_profile_id,
            model=accumulator.model or target.model,
            provider=target.provider or target.protocol,
            base_url=target.base_url,
            api_duration_ms=duration_ms,
            request_started_at=started_at,
            origin=origin,
            api_key_id=api_key_id,
        )
        async with SessionLocal() as session:
            # 用「本 run 的消息创建时刻」作为账本启用边界：边界轮的消息不会被旧消息段
            # 重复统计（它 >= started_at，旧消息段会跳过），由账本段独占。
            await ledger.mark_ledger_started(session, when=target.run_started_at or started_at)
            await ledger.apply_cost(session, record)
            added = await ledger.record(session, record)
            if api_key_id:
                # 外部 API：顺手更新「最近使用时间」（审计用）。
                key_row = await session.get(ApiKey, api_key_id)
                if key_row is not None:
                    key_row.last_used_at = utcnow()
                    session.add(key_row)
            await session.commit()
        if not counts["input_tokens"] and not counts["output_tokens"]:
            logger.info(
                "记录网关：请求 %s 未取到 usage（status=%s），已记 unknown，原生事件可兜底",
                request_id, status_code,
            )
        return added
    except Exception:  # noqa: BLE001 - 账本写入失败不能影响本轮对话
        logger.warning("记录网关写账本失败（不影响本轮对话）", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# HTTP 客户端（测试可替换）
# ---------------------------------------------------------------------------


def make_http_client() -> httpx.AsyncClient:
    """转发用的 httpx 客户端。测试 monkeypatch 它注入 ``MockTransport``。"""
    return httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=20.0))


def make_upstream_client(target: ProxyTarget, url: str) -> httpx.AsyncClient:
    """建好并套上出口过滤的客户端（与 ``openai_harness`` 同一套 egress 策略）。"""
    return wrap_client(make_http_client(), extra_hosts=[url])


def elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


__all__ = [
    "MODE_OFF",
    "MODE_AUTO",
    "MODE_ON",
    "VALID_MODES",
    "recording_gateway_mode",
    "ProxyTarget",
    "TokenRegistry",
    "registry",
    "proxy_base_url",
    "proxy_url_for",
    "join_upstream_url",
    "build_forward_headers",
    "trace_safe_headers",
    "filter_response_headers",
    "with_query_key",
    "UsageAccumulator",
    "request_id_for",
    "persist_usage",
    "make_http_client",
    "make_upstream_client",
    "elapsed_ms",
]