"""轨迹调试视图（阶段 2）：开关、采集、脱敏、持久化、裁剪与分页。

本文件用**开启鉴权**的客户端跑接口，确保 ``users.trace_debug`` 是唯一的采集开关
（与 ``test_ownership.py`` 同一套 fixture 形态）。
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest_asyncio
from sqlmodel import select

from app.agents import AgentContext, get_agent
from app.db import SessionLocal, engine, init_db
from app.main import app
from app.models import SQLModel, TraceEvent
from app.runtime import trace as trace_mod
from app.runtime.trace import (
    MAX_EVENT_BYTES,
    RawCollector,
    TraceRecorder,
    build_context_snapshot,
    sanitize,
)
from app.settings import get_settings

from helpers import FAKE_AGENT, cli_options

TRACE_SECRET = "sk-super-secret-value"
SYSTEM_PROMPT = "TOP-SECRET-SYSTEM-PROMPT"


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setattr(get_settings(), "auth_enabled", True)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await init_db()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register(client: httpx.AsyncClient, username: str = "trace-user") -> dict:
    response = await client.post(
        "/api/auth/register",
        json={"username": username, "password": "password123"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _setup(client: httpx.AsyncClient, *, system_prompt: str = SYSTEM_PROMPT) -> dict:
    """建一个「管理员 + 网关 + custom-cli 智能体 + 对话」。"""
    user = await _register(client)
    token = user["access_token"]
    headers = _auth(token)
    # 关掉自动起标题：本文件验的是轨迹，不想让后台标题任务去起真实内核。
    await client.put("/api/system/title", json={"enabled": False}, headers=headers)

    gateway = await client.post(
        "/api/gateways",
        json={
            "name": "轨迹测试网关",
            "base_url": "https://gw.example/v1",
            "protocol": "openai",
            "auth_style": "bearer",
            "api_key": TRACE_SECRET,
            "selected_models": ["fake-model"],
        },
        headers=headers,
    )
    assert gateway.status_code == 201, gateway.text
    options = cli_options("trace-sess")
    # 让密钥也进 argv，验证运行层与采集层的双重脱敏；第三个位置参数是替身的延迟（0 秒）。
    options["args"] = [FAKE_AGENT, "trace-sess", "0", f"--key={TRACE_SECRET}"]
    agent = await client.post(
        "/api/agents",
        json={
            "name": "轨迹智能体",
            "type_id": "custom-cli",
            "system_prompt": system_prompt,
            "gateway_id": gateway.json()["id"],
            "model": "fake-model",
            "options": options,
        },
        headers=headers,
    )
    assert agent.status_code == 201, agent.text
    conversation = await client.post(
        "/api/conversations",
        json={"agent_profile_id": agent.json()["id"]},
        headers=headers,
    )
    assert conversation.status_code == 201, conversation.text
    return {
        "token": token,
        "headers": headers,
        "user": user["user"],
        "gateway": gateway.json(),
        "agent": agent.json(),
        "conversation": conversation.json(),
    }


async def _set_trace_debug(client: httpx.AsyncClient, ctx: dict, enabled: bool) -> dict:
    response = await client.put(
        "/api/auth/me", json={"trace_debug": enabled}, headers=ctx["headers"]
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _send(client: httpx.AsyncClient, ctx: dict, content: str) -> None:
    async with client.stream(
        "POST",
        f"/api/conversations/{ctx['conversation']['id']}/messages",
        json={"content": content},
        headers=ctx["headers"],
    ) as response:
        assert response.status_code == 200, await response.aread()
        async for _ in response.aiter_lines():
            pass


async def _trace_rows(conversation_id: str) -> list[TraceEvent]:
    async with SessionLocal() as session:
        return list(
            (
                await session.execute(
                    select(TraceEvent)
                    .where(TraceEvent.conversation_id == conversation_id)
                    .order_by(TraceEvent.seq)
                )
            ).scalars()
        )


async def test_trace_switch_defaults_off_then_collects_when_enabled(client: httpx.AsyncClient):
    ctx = await _setup(client)

    me = (await client.get("/api/auth/me", headers=ctx["headers"])).json()
    assert me["trace_debug"] is False

    # 默认关闭：发一条消息后一条轨迹都不该有。
    await _send(client, ctx, "first message")
    assert await _trace_rows(ctx["conversation"]["id"]) == []

    updated = await _set_trace_debug(client, ctx, True)
    assert updated["trace_debug"] is True

    await _send(client, ctx, "second message")
    rows = await _trace_rows(ctx["conversation"]["id"])
    assert rows, "打开开关后必须产生轨迹"

    types = [row.type for row in rows]
    assert types.count("context_snapshot") == 1
    assert "request" in types and "response" in types
    assert "tool_call" in types and "tool_result" in types
    # seq 在会话内严格递增，时间线与分页都依赖它。
    assert all(left.seq < right.seq for left, right in zip(rows, rows[1:]))

    snapshot = next(row for row in rows if row.type == "context_snapshot")
    assert snapshot.turn_index == 1  # 第二条 user 消息（0 起）
    assert snapshot.owner_id == ctx["user"]["id"]
    assert snapshot.payload["user_message_id"]
    assert snapshot.payload["prompt"].find("second message") >= 0
    # 系统提示词只留长度 + 哈希，正文绝不入库。
    assert snapshot.payload["system_prompt"]["chars"] == len(SYSTEM_PROMPT)
    assert snapshot.payload["system_prompt"]["sha256_prefix"] == hashlib.sha256(
        SYSTEM_PROMPT.encode()
    ).hexdigest()[:16]
    assert SYSTEM_PROMPT not in json.dumps(snapshot.payload, ensure_ascii=False)

    request = next(row for row in rows if row.type == "request")
    assert request.payload["transport"] == "cli"
    assert request.payload["invocation"]["argv"]
    assert TRACE_SECRET not in json.dumps(request.payload, ensure_ascii=False)

    response = next(row for row in rows if row.type == "response")
    assert response.payload["granularity"] == "run"
    assert response.payload["exit_code"] == 0
    assert response.payload["raw_lines"]

    tool_call = next(row for row in rows if row.type == "tool_call")
    assert tool_call.payload["name"] == "run_shell"
    assert tool_call.payload["call_id"] == "t1"
    assert "command" in tool_call.payload["input"]
    tool_result = next(row for row in rows if row.type == "tool_result")
    assert tool_result.payload["call_id"] == "t1"
    assert tool_result.payload["output"] == "a.txt"
    assert tool_result.payload["is_error"] is False


async def test_regenerate_with_trace_reuses_the_rerun_turn_index(client: httpx.AsyncClient):
    ctx = await _setup(client)
    await _set_trace_debug(client, ctx, True)
    await _send(client, ctx, "only message")
    first_run_ids = {row.run_id for row in await _trace_rows(ctx["conversation"]["id"]) if row.run_id}

    async with client.stream(
        "POST",
        f"/api/conversations/{ctx['conversation']['id']}/regenerate",
        headers=ctx["headers"],
    ) as response:
        assert response.status_code == 200, await response.aread()
        async for _ in response.aiter_lines():
            pass

    rows = await _trace_rows(ctx["conversation"]["id"])
    snapshots = [row for row in rows if row.type == "context_snapshot"]
    assert len(snapshots) == 2
    # 重生成复用被重跑那条 user 消息的序号（这里只有第 0 条）。
    assert {snapshot.turn_index for snapshot in snapshots} == {0}
    regenerated_run = snapshots[-1].run_id
    assert regenerated_run not in first_run_ids
    assert any(row.type == "response" and row.run_id == regenerated_run for row in rows)


async def test_trace_endpoint_paginates_with_after_seq(client: httpx.AsyncClient):
    ctx = await _setup(client)
    conversation_id = ctx["conversation"]["id"]
    async with SessionLocal() as session:
        for index in range(1, 521):
            session.add(
                TraceEvent(
                    conversation_id=conversation_id,
                    owner_id=ctx["user"]["id"],
                    run_id="seed-run",
                    turn_index=0,
                    seq=index,
                    type="request",
                    payload={"index": index},
                    bytes=10,
                )
            )
        await session.commit()

    async def page(**params) -> dict:
        response = await client.get(
            f"/api/conversations/{conversation_id}/trace",
            params=params,
            headers=ctx["headers"],
        )
        assert response.status_code == 200, response.text
        return response.json()

    first = await page(after_seq=0, limit=100)
    assert len(first["items"]) == 100
    assert first["items"][0]["seq"] == 1 and first["items"][-1]["seq"] == 100
    assert first["next_after_seq"] == 100

    second = await page(after_seq=first["next_after_seq"], limit=100)
    assert second["items"][0]["seq"] == 101 and second["items"][-1]["seq"] == 200
    assert second["next_after_seq"] == 200

    tail = await page(after_seq=500, limit=100)
    assert len(tail["items"]) == 20
    assert tail["next_after_seq"] is None

    # limit 上限生效：再大也只回 MAX_PAGE_LIMIT 条。
    clamped = await page(after_seq=0, limit=99999)
    assert len(clamped["items"]) == trace_mod.MAX_PAGE_LIMIT

    # 只读接口返回 §4.1 字段名（session_id / timestamp）。
    item = first["items"][0]
    assert item["session_id"] == conversation_id
    assert item["timestamp"]
    assert item["payload"] == {"index": 1}


async def test_trace_rows_follow_conversation_cascade_delete(client: httpx.AsyncClient):
    ctx = await _setup(client)
    await _set_trace_debug(client, ctx, True)
    await _send(client, ctx, "cascade message")
    conversation_id = ctx["conversation"]["id"]
    assert await _trace_rows(conversation_id)

    response = await client.delete(
        f"/api/conversations/{conversation_id}", headers=ctx["headers"]
    )
    assert response.status_code == 204, response.text
    assert await _trace_rows(conversation_id) == []


async def test_sanitize_masks_sensitive_keys_and_binary():
    safe, redacted = sanitize(
        {
            "headers": {
                "Authorization": f"Bearer {TRACE_SECRET}",
                "Cookie": "sid=abc",
                "Content-Type": "application/json",
            },
            "api_key": TRACE_SECRET,
            "nested": {"token": "abc", "safe": "visible"},
            "inline": "data:image/png;base64,AAAA",
            "raw": b"\x00\x01",
        },
        secrets=[TRACE_SECRET],
    )
    assert safe["headers"]["Authorization"] == "***"
    assert safe["headers"]["Cookie"] == "***"
    assert safe["headers"]["Content-Type"] == "application/json"
    assert safe["api_key"] == "***"
    assert safe["nested"]["token"] == "***"
    assert safe["nested"]["safe"] == "visible"
    assert safe["inline"] == {"__omitted__": "binary", "mime": "image/png", "bytes": len("data:image/png;base64,AAAA")}
    assert safe["raw"]["__omitted__"] == "binary"
    assert "headers.Authorization" in redacted
    assert "api_key" in redacted
    # 键名没命中但值里含凭据时也要擦掉。
    safe_text, _ = sanitize({"note": f"prefix {TRACE_SECRET} suffix"}, secrets=[TRACE_SECRET])
    assert safe_text["note"] == "prefix *** suffix"


async def test_recorder_never_writes_the_plaintext_secret(client: httpx.AsyncClient):
    ctx = await _setup(client)
    conversation_id = ctx["conversation"]["id"]
    recorder = TraceRecorder(
        conversation_id=conversation_id,
        owner_id=ctx["user"]["id"],
        run_id="secret-run",
        turn_index=0,
        secrets=[TRACE_SECRET],
    )
    await recorder.request(
        {
            "headers": {"Authorization": f"Bearer {TRACE_SECRET}"},
            "body": {"api_key": TRACE_SECRET, "note": f"before {TRACE_SECRET} after"},
        }
    )
    await recorder.flush()

    rows = await _trace_rows(conversation_id)
    assert len(rows) == 1
    assert TRACE_SECRET not in json.dumps(rows[0].payload, ensure_ascii=False)
    assert rows[0].payload["redacted_fields"]


async def test_payload_truncation_and_per_conversation_pruning(client: httpx.AsyncClient, monkeypatch):
    ctx = await _setup(client)
    conversation_id = ctx["conversation"]["id"]
    monkeypatch.setattr(trace_mod, "MAX_EVENTS_PER_CONV", 3)

    recorder = TraceRecorder(conversation_id=conversation_id, run_id="small-run", start_seq=0)
    for index in range(5):
        await recorder.response({"index": index, "blob": "x" * 1000})
    await recorder.flush()

    rows = await _trace_rows(conversation_id)
    assert len(rows) == 3
    # 按 created_at/seq 从旧到新裁剪，留下的必须是最新的三条。
    assert [row.payload["index"] for row in rows] == [2, 3, 4]

    big = TraceRecorder(conversation_id=conversation_id, run_id="big-run", start_seq=10)
    await big.response({"raw": "y!" * (MAX_EVENT_BYTES // 2 + 512)})
    await big.flush()

    rows = await _trace_rows(conversation_id)
    latest = next(row for row in rows if row.run_id == "big-run")
    assert latest.truncated is True
    assert latest.payload["__truncated__"] is True
    assert latest.payload["head"] and latest.payload["tail"]
    assert latest.bytes > MAX_EVENT_BYTES
    # 裁剪仍把最新一条留下（最新一轮始终可查）。
    assert len(rows) == 3


def test_raw_collector_keeps_head_and_tail():
    collector = RawCollector(head_bytes=200, tail_bytes=100)
    for index in range(200):
        collector.add(f"line-{index:03d}-" + "x" * 20)
    assert collector.truncated is True
    lines = collector.lines()
    assert lines[0].startswith("line-000")
    assert lines[-1].startswith("line-199")
    assert sum(len(line.encode("utf-8")) for line in lines) <= 300 + 21


def test_context_snapshot_hides_system_prompt_and_clips_history():
    ctx = SimpleNamespace(
        system_prompt="S" * 100,
        prompt="本轮用户消息",
        history=[{"role": "user", "content": "a" * 10_000}]
        + [{"role": "assistant", "content": "b" * 4_000} for _ in range(12)],
        model="fake-model",
        workspace_dir="/tmp/ws",
        session_id="kernel-sess",
        agent_id="profile-1",
        skills=[SimpleNamespace(name="skill-a")],
        mcp_servers=[SimpleNamespace(name="mcp-a")],
    )
    snapshot = build_context_snapshot(ctx, "user-message-1")
    assert snapshot["system_prompt"] == {
        "chars": 100,
        "sha256_prefix": hashlib.sha256(("S" * 100).encode()).hexdigest()[:16],
    }
    assert "S" * 100 not in json.dumps(snapshot, ensure_ascii=False)
    assert snapshot["truncated"] is True
    total = sum(len(entry.get("content", "").encode("utf-8")) for entry in snapshot["history"])
    assert total <= 32 * 1024
    assert all(
        len(entry.get("content", "").encode("utf-8")) <= 8 * 1024
        for entry in snapshot["history"]
    )
    assert snapshot["history_count"] == 13
    assert snapshot["skills"] == ["skill-a"]
    assert snapshot["mcp_servers"] == ["mcp-a"]
    assert snapshot["user_message_id"] == "user-message-1"


# --- API 内核（openai-harness）的请求 / 响应采集 -----------------------------


def _sse(*chunks: dict) -> bytes:
    return b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"


TOOL_CHUNK = {
    "choices": [
        {
            "delta": {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "run_shell", "arguments": '{"command": "echo hi"}'},
                    }
                ]
            }
        }
    ]
}
FINAL_CHUNKS = [
    {"choices": [{"delta": {"content": "all "}}]},
    {"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]},
    {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}},
]


class _RecordingTrace:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict, dict]] = []

    async def request(self, payload: dict, **kwargs) -> None:
        self.events.append(("request", payload, kwargs))

    async def response(self, payload: dict, **kwargs) -> None:
        self.events.append(("response", payload, kwargs))

    async def on_event(self, event) -> None:  # pragma: no cover - 适配器不直接调它
        pass

    async def flush(self) -> None:  # pragma: no cover - 适配器不负责落盘
        pass


async def test_openai_harness_traces_each_step_and_reads_finish_reason(monkeypatch, tmp_path):
    definition = get_agent("openai-harness")
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        chunks = [TOOL_CHUNK] if len(seen) == 1 else FINAL_CHUNKS
        return httpx.Response(
            200, content=_sse(*chunks), headers={"content-type": "text/event-stream"}
        )

    monkeypatch.setattr(
        definition,
        "create_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        raising=False,
    )
    trace = _RecordingTrace()
    ctx = AgentContext(
        agent_id="a1",
        agent_name="Harness",
        gateway_url="https://gateway.example/v1",
        api_key=TRACE_SECRET,
        model="gpt-4o-mini",
        workspace_dir=str(tmp_path),
        prompt="check the workspace",
        trace=trace,
    )

    events = [event async for event in definition.stream(ctx)]
    assert events and events[-1].type.value == "usage"

    requests = [payload for kind, payload, _ in trace.events if kind == "request"]
    responses = [payload for kind, payload, _ in trace.events if kind == "response"]
    assert len(requests) == 2 and len(responses) == 2
    assert [item["step"] for item in requests] == [0, 1]
    assert requests[0]["transport"] == "http"
    assert requests[0]["method"] == "POST"
    assert requests[0]["body"]["model"] == "gpt-4o-mini"
    # 白名单脱敏：Authorization 不会原样入库。
    assert requests[0]["headers"]["Authorization"] == "***"
    assert responses[0]["status_code"] == 200
    assert responses[0]["chunk_count"] >= 1
    assert responses[0]["raw_sse"]
    assert responses[-1]["finish_reason"] == "stop"
    assert responses[-1]["usage"] == {"prompt_tokens": 7, "completion_tokens": 3}
