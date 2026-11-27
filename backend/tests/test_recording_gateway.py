"""记录网关（P1）：令牌生命周期、URL 拼接、转发头、SSE/JSON usage 解析、
去重记账与路由安全。
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import pytest_asyncio
from sqlmodel import select

from app.agents import AgentContext, get_agent
from app.agents.events import context_usage, text_delta, usage
from app.api import usage_gateway as gateway_route
from app.db import SessionLocal, engine, init_db
from app.models import (
    AgentProfile,
    Conversation,
    Gateway,
    Message,
    SQLModel,
    UsageRecord,
)
from app.runtime import context as context_service
from app.runtime import recording_gateway as gw
from app.runtime import service as runtime_service
from app.runtime import usage_ledger
from app.runtime.manager import ActiveRun
from app.security import encrypt


def _now():
    return datetime.now(timezone.utc)


@pytest_asyncio.fixture(autouse=True)
async def _schema_and_registry():
    """每个用例一份干净库 + 空注册表（注册表是进程级全局）。"""
    gw.registry.clear()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await init_db()
    yield
    gw.registry.clear()


# ---------------------------------------------------------------------------
# 令牌注册表
# ---------------------------------------------------------------------------


def test_registry_register_resolve_and_revoke():
    target = gw.registry.register(
        run_id="run-1", upstream_url="https://up.example/v1", api_key="secret",
        agent_type_id="claude-code", model="m",
    )
    assert len(target.token) >= 32
    resolved = gw.registry.resolve(target.token)
    assert resolved is target
    gw.registry.revoke(target.token)
    assert gw.registry.resolve(target.token) is None


def test_registry_expired_token_is_rejected():
    target = gw.registry.register(
        run_id="run-2", upstream_url="https://up.example", ttl_seconds=0
    )
    assert gw.registry.resolve(target.token) is None


def test_registry_revoke_run_removes_all_its_tokens():
    first = gw.registry.register(run_id="run-3", upstream_url="https://up.example")
    second = gw.registry.register(run_id="run-3", upstream_url="https://up.example")
    other = gw.registry.register(run_id="run-4", upstream_url="https://up.example")
    gw.registry.revoke_run("run-3")
    assert gw.registry.resolve(first.token) is None
    assert gw.registry.resolve(second.token) is None
    assert gw.registry.resolve(other.token) is not None


# ---------------------------------------------------------------------------
# URL 拼接
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "path", "query", "expected"),
    [
        ("https://api.anthropic.com", "v1/messages", "", "https://api.anthropic.com/v1/messages"),
        ("https://api.anthropic.com/", "/v1/messages", "", "https://api.anthropic.com/v1/messages"),
        ("https://gw.example/v1", "v1/messages", "", "https://gw.example/v1/messages"),
        ("https://gw.example/v1/", "v1/messages", "", "https://gw.example/v1/messages"),
        ("https://api.openai.com/v1", "chat/completions", "", "https://api.openai.com/v1/chat/completions"),
        ("https://api.openai.com/v1", "v1/responses", "", "https://api.openai.com/v1/responses"),
        ("https://gw.example/v1", "", "", "https://gw.example/v1"),
        ("https://gw.example", "v1/messages", "x=1&y=2", "https://gw.example/v1/messages?x=1&y=2"),
    ],
)
def test_join_upstream_url(base, path, query, expected):
    assert gw.join_upstream_url(base, path, query) == expected


# ---------------------------------------------------------------------------
# 转发头
# ---------------------------------------------------------------------------


def test_forward_headers_inject_replaces_client_auth():
    """inject：子进程带来的代理 token 不能透传，换成服务端上游密钥。"""
    target = gw.ProxyTarget(
        token="t", run_id="r", upstream_url="https://up", auth_style="x-api-key",
        api_key="REAL-KEY", inject_key=True,
    )
    incoming = {
        "host": "127.0.0.1:8010",
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "x-api-key": "PROXY-TOKEN",
        "authorization": "Bearer PROXY-TOKEN",
    }
    headers = gw.build_forward_headers(incoming, target, inject=True)
    assert headers["x-api-key"] == "REAL-KEY"
    assert "authorization" not in headers  # bearer 模式下不额外塞
    assert headers["anthropic-version"] == "2023-06-01"
    assert "host" not in headers


def test_forward_headers_bearer_inject():
    target = gw.ProxyTarget(
        token="t", run_id="r", upstream_url="https://up", auth_style="bearer",
        api_key="REAL-KEY", inject_key=True,
    )
    headers = gw.build_forward_headers({"authorization": "Bearer PROXY"}, target, inject=True)
    assert headers["authorization"] == "Bearer REAL-KEY"


def test_forward_headers_passthrough_keeps_client_auth():
    target = gw.ProxyTarget(
        token="t", run_id="r", upstream_url="https://up", auth_style="bearer",
        api_key="REAL-KEY", inject_key=False,
    )
    headers = gw.build_forward_headers({"authorization": "Bearer CLIENT"}, target, inject=False)
    assert headers["authorization"] == "Bearer CLIENT"


# ---------------------------------------------------------------------------
# usage 解析
# ---------------------------------------------------------------------------


def test_accumulator_anthropic_stream_start_zero_then_delta():
    """message_start 全 0、message_delta 才有值：以最终有效值覆盖，不丢缓存字段。"""
    accumulator = gw.UsageAccumulator()
    accumulator.feed(
        b'event: message_start\ndata: {"type":"message_start","message":'
        b'{"id":"msg_1","model":"claude-x","usage":{"input_tokens":0,"output_tokens":0}}}\n\n'
    )
    accumulator.feed(
        b'event: message_delta\ndata: {"type":"message_delta","usage":'
        b'{"input_tokens":75,"cache_read_input_tokens":14848,'
        b'"cache_creation_input_tokens":100,"output_tokens":12}}\n\n'
    )
    accumulator.feed(b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
    accumulator.finish()
    counts = accumulator.counts()
    assert accumulator.request_id == "msg_1"
    assert accumulator.model == "claude-x"
    assert counts["input_tokens"] == 75
    assert counts["cache_read_tokens"] == 14_848
    assert counts["cache_creation_tokens"] == 100
    assert counts["output_tokens"] == 12
    assert counts["context_tokens"] == 75 + 14_848 + 100


def test_accumulator_openai_chat_final_chunk():
    accumulator = gw.UsageAccumulator()
    accumulator.feed(
        b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","model":"gpt-x",'
        b'"choices":[{"delta":{"content":"hi"}}]}\n\n'
    )
    accumulator.feed(
        b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk",'
        b'"usage":{"prompt_tokens":100,"completion_tokens":7,'
        b'"prompt_tokens_details":{"cached_tokens":40}}}\n\n'
    )
    accumulator.feed(b"data: [DONE]\n\n")
    accumulator.finish()
    counts = accumulator.counts()
    assert accumulator.request_id == "chatcmpl-1"
    assert counts["input_tokens"] == 60
    assert counts["cache_read_tokens"] == 40
    assert counts["output_tokens"] == 7
    assert counts["context_tokens"] == 100


def test_accumulator_openai_responses_completed():
    accumulator = gw.UsageAccumulator()
    accumulator.feed(
        b'data: {"type":"response.completed","response":'
        b'{"id":"resp_1","model":"gpt-5",'
        b'"usage":{"input_tokens":800,"output_tokens":60,'
        b'"input_tokens_details":{"cached_tokens":200},'
        b'"output_tokens_details":{"reasoning_tokens":20}}}}\n\n'
    )
    accumulator.finish()
    counts = accumulator.counts()
    assert accumulator.request_id == "resp_1"
    assert counts["input_tokens"] == 600
    assert counts["cache_read_tokens"] == 200
    assert counts["reasoning_tokens"] == 20
    assert counts["context_tokens"] == 800


def test_accumulator_non_streaming_json_and_split_chunks():
    """非流式单体 JSON；且一行被拆成多块也要能拼回来。"""
    accumulator = gw.UsageAccumulator()
    payload = b'{"id":"msg_2","model":"claude-y","usage":{"input_tokens":11,"output_tokens":3}}'
    accumulator.feed(payload[:20])
    accumulator.feed(payload[20:])
    accumulator.finish()
    assert accumulator.request_id == "msg_2"
    assert accumulator.window_tokens() == 11


def test_accumulator_missing_usage_is_none_not_zero():
    accumulator = gw.UsageAccumulator()
    accumulator.feed(b'data: {"type":"message_stop"}\n\n')
    accumulator.finish()
    assert accumulator.window_tokens() is None
    assert all(value is None for value in accumulator.counts().values())


# ---------------------------------------------------------------------------
# 记账（去重 / unknown）
# ---------------------------------------------------------------------------


async def test_persist_usage_writes_one_row_and_dedups():
    target = gw.registry.register(
        run_id="persist-run", upstream_url="https://up.example/v1",
        protocol="anthropic", auth_style="x-api-key", api_key="k",
        agent_type_id="claude-code", model="claude-x", owner_id="u1", conversation_id="c1",
    )
    accumulator = gw.UsageAccumulator()
    accumulator.feed(
        b'data: {"type":"message_start","message":{"id":"msg_9","model":"claude-x",'
        b'"usage":{"input_tokens":10,"output_tokens":2}}}\n\n'
    )
    accumulator.finish()

    assert await gw.persist_usage(
        target, accumulator, status_code=200,
        started_at=_now(),
        duration_ms=12,
    )
    # 同一 request_id 再写：去重，不再新增。
    assert not await gw.persist_usage(
        target, accumulator, status_code=200,
        started_at=_now(),
        duration_ms=12,
    )

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(UsageRecord).where(UsageRecord.run_id == "persist-run")
            )
        ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.source == "gateway"
    assert row.request_id == "msg_9"
    assert row.input_tokens == 10
    assert row.output_tokens == 2
    assert row.context_tokens == 10


async def test_persist_usage_without_usage_records_unknown():
    target = gw.registry.register(run_id="unknown-run", upstream_url="https://up")
    accumulator = gw.UsageAccumulator()
    accumulator.finish()
    assert await gw.persist_usage(
        target, accumulator, status_code=500,
        started_at=_now(),
        duration_ms=5,
    )
    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(UsageRecord).where(UsageRecord.run_id == "unknown-run")
            )
        ).scalar_one()
    assert row.input_tokens is None
    assert row.output_tokens is None
    assert row.cost_source == "unknown"


# ---------------------------------------------------------------------------
# 路由（security + 端到端）
# ---------------------------------------------------------------------------


def test_is_loopback():
    assert gateway_route.is_loopback("127.0.0.1")
    assert gateway_route.is_loopback("127.0.0.42")
    assert gateway_route.is_loopback("::1")
    assert gateway_route.is_loopback("localhost")
    assert not gateway_route.is_loopback("10.0.0.1")
    assert not gateway_route.is_loopback(None)


async def test_route_rejects_unknown_token(client: httpx.AsyncClient):
    response = await client.post("/api/internal/usage-gateway/nope/v1/messages", content=b"{}")
    assert response.status_code == 401


async def test_route_rejects_non_loopback(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(gateway_route, "is_loopback", lambda host: False)
    target = gw.registry.register(run_id="nb", upstream_url="https://up")
    response = await client.post(
        f"/api/internal/usage-gateway/{target.token}/v1/messages", content=b"{}"
    )
    assert response.status_code == 403


async def test_route_forwards_and_records(client: httpx.AsyncClient, monkeypatch):
    target = gw.registry.register(
        run_id="route-run", upstream_url="https://upstream.example/v1",
        protocol="anthropic", auth_style="x-api-key", api_key="REAL-KEY",
        agent_type_id="claude-code", model="claude-x", owner_id="u1", conversation_id="c1",
    )
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-api-key")
        body = (
            'event: message_start\n'
            'data: {"type":"message_start","message":{"id":"msg_route","model":"claude-x",'
            '"usage":{"input_tokens":10,"cache_read_input_tokens":5,"output_tokens":1}}}\n\n'
            'event: message_delta\n'
            'data: {"type":"message_delta","usage":{"output_tokens":7}}\n\n'
            'event: message_stop\n'
            'data: {"type":"message_stop"}\n\n'
        )
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "text/event-stream"}
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(gw, "make_http_client", lambda: httpx.AsyncClient(transport=transport))

    response = await client.post(
        f"/api/internal/usage-gateway/{target.token}/v1/messages",
        content=b'{"model":"claude-x"}',
        headers={"x-api-key": "PROXY-TOKEN", "content-type": "application/json"},
    )
    assert response.status_code == 200
    assert "message_start" in response.text
    # URL 拼接：上游 base 已带 /v1，CLI 又追加 /v1 → 不重复。
    assert seen["url"] == "https://upstream.example/v1/messages"
    # 鉴权注入：上游拿到真实密钥，而不是代理 token。
    assert seen["key"] == "REAL-KEY"

    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(UsageRecord).where(UsageRecord.run_id == "route-run")
            )
        ).scalar_one()
    assert row.request_id == "msg_route"
    assert row.input_tokens == 10
    assert row.cache_read_tokens == 5
    assert row.output_tokens == 7
    assert row.context_tokens == 15

# ---------------------------------------------------------------------------
# 运行级联调：gateway 优先不双记 / 原生兜底
# ---------------------------------------------------------------------------


async def _flow_rows(run_id: str, *, proxy_url: str):
    """造一条对话 + 智能体 + 自定义网关 + 一对消息，返回可执行的 PreparedRun。"""
    async with SessionLocal() as session:
        conversation = Conversation(
            id="flow-conv", agent_profile_id="flow-profile", title="联调", owner_id="u1"
        )
        profile = AgentProfile(id="flow-profile", name="cl", type_id="claude-code")
        gateway = Gateway(
            id="flow-gw", name="gw", kind="custom", protocol="anthropic",
            auth_style="x-api-key", base_url="https://up.example/v1",
            api_key_encrypted=encrypt("REAL"),
        )
        session.add_all([conversation, profile, gateway])
        await session.commit()
        user = Message(conversation_id="flow-conv", role="user", content="hi")
        assistant = Message(conversation_id="flow-conv", role="assistant", content="")
        session.add_all([user, assistant])
        await session.commit()
        await session.refresh(user)
        await session.refresh(assistant)
        ctx = AgentContext(
            agent_id="flow-profile", agent_name="cl", gateway_url=proxy_url,
            api_key="proxy-token", auth_style="x-api-key", model="claude-x",
        )
        run = ActiveRun(id=run_id, conversation_id="flow-conv", agent_name="cl")
        prepared = runtime_service.PreparedRun(
            conversation=conversation, profile=profile, gateway=gateway,
            definition=get_agent("claude-code"), ctx=ctx, run=run,
            user_message=user, assistant_message=assistant,
        )
        return prepared, assistant.id


async def test_consume_run_prefers_gateway_and_does_not_double_count(monkeypatch):
    """已有 gateway 行时：窗口快照取账本最新值，原生 usage 不再入账。"""
    prepared, assistant_id = await _flow_rows(
        "flow-run-gw", proxy_url="http://127.0.0.1:8010/api/internal/usage-gateway/tok"
    )
    # 预写一手账本：context = 1000 + 234 = 1234。
    await usage_ledger.record_isolated(
        usage_ledger.build_record(
            run_id="flow-run-gw", request_id="g1", source=usage_ledger.SOURCE_GATEWAY,
            counts=usage_ledger.normalize_usage(
                {"input_tokens": 1000, "cache_read_input_tokens": 234, "output_tokens": 10}
            ),
            owner_id="u1", conversation_id="flow-conv",
        )
    )

    async def fake_stream(definition, ctx, active_run):
        yield usage(input_tokens=9999, output_tokens=9999)
        yield context_usage(777)
        yield text_delta("ok")

    monkeypatch.setattr(runtime_service, "stream_run", fake_stream)
    async with SessionLocal() as session:
        async for _ in runtime_service.consume_run(session, prepared):
            pass

    async with SessionLocal() as session:
        assistant = await session.get(Message, assistant_id)
        rows = (
            await session.execute(
                select(UsageRecord).where(UsageRecord.run_id == "flow-run-gw")
            )
        ).scalars().all()
    assert assistant.context_tokens == 1234  # 账本覆盖原生 777
    assert len(rows) == 1  # 原生 usage 不再入账，不双记
    assert rows[0].source == usage_ledger.SOURCE_GATEWAY


async def test_consume_run_native_fallback_writes_native_row(monkeypatch):
    """没有 gateway 行时：本轮原生累计作为一行 native 入账，窗口快照来自 context.usage。"""
    prepared, assistant_id = await _flow_rows(
        "flow-run-native", proxy_url="https://up.example/v1"  # 非代理地址 = 未走记录网关
    )

    async def fake_stream(definition, ctx, active_run):
        yield usage(input_tokens=500, output_tokens=20)
        yield context_usage(480)
        yield text_delta("ok")

    monkeypatch.setattr(runtime_service, "stream_run", fake_stream)
    async with SessionLocal() as session:
        async for _ in runtime_service.consume_run(session, prepared):
            pass

    async with SessionLocal() as session:
        assistant = await session.get(Message, assistant_id)
        rows = (
            await session.execute(
                select(UsageRecord).where(UsageRecord.run_id == "flow-run-native")
            )
        ).scalars().all()
    assert assistant.context_tokens == 480
    assert len(rows) == 1
    assert rows[0].source == usage_ledger.SOURCE_NATIVE
    assert rows[0].input_tokens == 500
    assert rows[0].context_tokens == 480


async def test_native_context_event_lands_in_ledger_mid_run(monkeypatch):
    """plan A：原生 context.usage 在**运行中**就落账，面板当场就能读到（不再等收尾）。"""
    prepared, assistant_id = await _flow_rows(
        "flow-run-live", proxy_url="https://up.example/v1"  # 非代理地址 = 原生
    )
    observed: list = []

    async def fake_stream(definition, ctx, active_run):
        yield context_usage(333)
        # 事件被 consume_run 处理完、生成器恢复到这里：此时账本应已有行。
        async with SessionLocal() as probe:
            rows = (
                await probe.execute(
                    select(UsageRecord).where(UsageRecord.run_id == "flow-run-live")
                )
            ).scalars().all()
            observed.append(
                [(row.source, row.request_id, row.context_tokens, row.input_tokens) for row in rows]
            )
            used, measured = await context_service.conversation_usage(
                probe, prepared.conversation, None, active_run_id="flow-run-live"
            )
            observed.append((used, measured))
        yield usage(input_tokens=1000, output_tokens=50)
        yield text_delta("ok")

    monkeypatch.setattr(runtime_service, "stream_run", fake_stream)
    async with SessionLocal() as session:
        async for _ in runtime_service.consume_run(session, prepared):
            pass

    # 运行中：已经有一行 native，context 是刚上报的 333。
    assert len(observed) == 2
    assert observed[0][0][0] == "native"
    assert observed[0][0][2] == 333
    # 面板运行中直接读到真实快照（不是估算）。
    assert observed[1] == (333, True)

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(UsageRecord).where(UsageRecord.run_id == "flow-run-live")
            )
        ).scalars().all()
    # 收尾更新同一行，不新增：计费计数补齐，窗口仍是最后一次请求的 333。
    assert len(rows) == 1
    row = rows[0]
    assert row.input_tokens == 1000
    assert row.output_tokens == 50
    assert row.context_tokens == 333
    assert row.source == "native"
