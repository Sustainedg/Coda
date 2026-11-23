from __future__ import annotations

import asyncio
import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import update

from app import __version__
from app.db import SessionLocal, engine, init_db
from app.main import app
from app.models import AgentProfile, Conversation, Message, SQLModel
from app.runtime.manager import registry
from app.runtime.service import prepare_run, start_run
from app.schemas import SendMessage
from helpers import cli_options, make_gateway, make_profile



@pytest_asyncio.fixture
async def client():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def make_agent(client: httpx.AsyncClient, **overrides) -> dict:
    """建一个「网关 + 智能体」，返回智能体（网关 id 在 gateway_id 上）。"""
    gateway = await make_gateway(client)
    return await make_profile(client, gateway_id=gateway["id"], **overrides)


async def read_sse(response: httpx.Response) -> list[dict]:
    events: list[dict] = []
    async for line in response.aiter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


async def test_gateway_crud_masks_secrets(client: httpx.AsyncClient):
    gateway = await make_gateway(client)
    assert gateway["api_key_set"] is True
    assert gateway["api_key_preview"] == "sk-s…alue"
    assert "sk-super-secret-value" not in json.dumps(gateway)

    listed = (await client.get("/api/gateways")).json()["items"]
    assert len(listed) == 1
    assert listed[0]["selected_models"] == ["fake-model", "other-model"]

    patched = await client.patch(f"/api/gateways/{gateway['id']}", json={"name": "Renamed"})
    assert patched.json()["name"] == "Renamed"
    # 省略 api_key 表示保留已存密钥
    assert patched.json()["api_key_set"] is True

    cleared = await client.patch(f"/api/gateways/{gateway['id']}", json={"api_key": ""})
    assert cleared.json()["api_key_set"] is False

    assert (await client.delete(f"/api/gateways/{gateway['id']}")).status_code == 204
    assert (await client.get(f"/api/gateways/{gateway['id']}")).status_code == 404


async def test_agent_crud_keeps_gateway_and_model(client: httpx.AsyncClient):
    agent = await make_agent(client)
    assert agent["gateway_kind"] == "custom"
    assert agent["gateway_api_key_set"] is True
    assert agent["model"] == "fake-model"
    assert agent["type_name"] == "自定义 CLI"

    listed = (await client.get("/api/agents")).json()["items"]
    assert listed[0]["id"] == agent["id"]

    patched = await client.patch(f"/api/agents/{agent['id']}", json={"model": "other-model"})
    assert patched.json()["model"] == "other-model"

    assert (await client.delete(f"/api/agents/{agent['id']}")).status_code == 204
    assert (await client.get(f"/api/agents/{agent['id']}")).status_code == 404


async def test_agent_avatar_preset_roundtrip(client: httpx.AsyncClient):
    """内置头像包编号：能存能读能清空，形状不对要拒（字符串会落库，不能放任任意串）。"""
    agent = await make_agent(client, avatar_preset="pack-007")
    assert agent["avatar_preset"] == "pack-007"
    # 换 emoji 头像时预设要能一起清掉（前端传 null）
    cleared = await client.patch(f"/api/agents/{agent['id']}", json={"avatar_preset": None})
    assert cleared.json()["avatar_preset"] is None
    # 重新挑一个头像包
    picked = await client.patch(f"/api/agents/{agent['id']}", json={"avatar_preset": "pack-042"})
    assert picked.json()["avatar_preset"] == "pack-042"
    # 形状不合法的编号在写入前就挡掉
    bad = await client.post(
        "/api/agents",
        json={"name": "x", "type_id": "custom-cli", "avatar_preset": "javascript:alert(1)"},
    )
    assert bad.status_code == 422
    # 列表接口同样带出该字段：气泡 / 卡片据此画真头像
    listed = (await client.get("/api/agents")).json()["items"]
    assert next(item for item in listed if item["id"] == agent["id"])["avatar_preset"] == "pack-042"


async def test_gateway_delete_blocked_while_an_agent_uses_it(client: httpx.AsyncClient):
    agent = await make_agent(client)
    response = await client.delete(f"/api/gateways/{agent['gateway_id']}")
    assert response.status_code == 409
    assert "正被以下智能体使用" in response.json()["detail"]


async def test_agent_types_expose_config_schema_and_availability(client: httpx.AsyncClient):
    types = (await client.get("/api/agent-types")).json()["items"]
    by_id = {t["id"]: t for t in types}
    assert {"claude-code", "codex", "gemini-cli", "openai-harness", "custom-cli"} <= set(by_id)
    claude = by_id["claude-code"]
    assert claude["availability"]["installed"] is True
    assert claude["runtime"] == "cli"
    assert any(f["key"] == "permission_mode" for f in claude["config_fields"])
    assert claude["capabilities"]["sessions"] is True
    assert "system_defaults" in claude
    assert by_id["openai-harness"]["runtime"] == "api"


async def test_agent_types_never_leak_plaintext_secret(client: httpx.AsyncClient, monkeypatch, tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "sk-very-secret-token"}}), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    types = (await client.get("/api/agent-types")).json()["items"]
    claude = next(t for t in types if t["id"] == "claude-code")
    assert claude["system_defaults"]["api_key_set"] is True
    # 凭据只能以掩码形式出现在接口响应里（AGENTS.md 铁律 6）。
    assert "sk-very-secret-token" not in json.dumps(types)
    assert claude["system_defaults"]["api_key_preview"].endswith("oken")


async def test_streaming_conversation_persists_a_full_turn(client: httpx.AsyncClient):
    # 关掉「自动起标题」：本用例验证的是首行兜底标题；自动起标题单独在 test_titles.py 覆盖。
    await client.put("/api/system/title", json={"enabled": False})

    agent = await make_agent(client)
    role = await make_profile(
        client,
        gateway_id=agent["gateway_id"],
        name="Tester",
        avatar="🧪",
        system_prompt="You are a tester.",
    )
    conversation = (await client.post("/api/conversations", json={"agent_profile_id": role["id"]})).json()

    async with client.stream(
        "POST", f"/api/conversations/{conversation['id']}/messages", json={"content": "hello world"}
    ) as response:
        assert response.status_code == 200
        events = await read_sse(response)

    kinds = [e["type"] for e in events]
    assert kinds[0] == "turn.started"
    assert kinds[-1] == "stream.closed"
    assert "session" in kinds and "tool.call" in kinds and "usage" in kinds
    deltas = "".join(e.get("text") or "" for e in events if e["type"] == "message.delta")
    assert deltas == "Echo: hello world... done"

    detail = (await client.get(f"/api/conversations/{conversation['id']}")).json()
    messages = detail["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "Echo: hello world... done"
    assert messages[1]["status"] == "complete"
    assert messages[1]["usage"]["output_tokens"] == 22
    # parts 保留真实时间线：正文 → 工具调用 → 工具结果 → 正文（AGENTS.md 前端渲染契约）。
    timeline = messages[1]["parts"]
    assert [p["kind"] for p in timeline] == ["text", "tool_call", "tool_result", "text"]
    assert timeline[0]["text"] == "Echo: hello world..."
    assert timeline[-1]["text"] == " done"
    assert timeline[1]["name"] == "run_shell" and timeline[2]["output"] == "a.txt"

    # 标题自动取自首条用户消息（智能体记录不受影响）
    assert detail["conversation"]["title"] == "hello world"
    assert detail["conversation"]["session_id"] == "sess-42"


async def test_unread_flag_lifecycle(client: httpx.AsyncClient):
    """列表的 unread：新对话已读 → AI 回完未读 → 标已读后消失，且标已读不改排序依据。"""
    agent = await make_agent(client)
    conversation = (await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})).json()

    listed = (await client.get("/api/conversations")).json()["items"][0]
    assert listed["unread"] is False

    async with client.stream(
        "POST", f"/api/conversations/{conversation['id']}/messages", json={"content": "hello"}
    ) as response:
        await read_sse(response)

    before = (await client.get("/api/conversations")).json()["items"][0]
    assert before["unread"] is True

    marked = await client.post(f"/api/conversations/{conversation['id']}/read")
    assert marked.status_code == 204

    after = (await client.get("/api/conversations")).json()["items"][0]
    assert after["unread"] is False
    # 标已读只动 last_read_at：updated_at 不变，列表排序不会把它顶到最前面。
    assert after["updated_at"] == before["updated_at"]

    # 旧对话（升级前没有 last_read_at）一律当已读，避免升级后满屏未读。
    async with SessionLocal() as session:
        row = await session.get(Conversation, conversation["id"])
        row.last_read_at = None
        session.add(row)
        await session.commit()
    assert (await client.get("/api/conversations")).json()["items"][0]["unread"] is False


async def test_streaming_assistant_message_is_not_unread(client: httpx.AsyncClient):
    """生成中的 assistant 占位不算未读 —— 那一行显示的是「正在回复」的动画。"""
    agent = await make_agent(client)
    conversation = (await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})).json()

    async with SessionLocal() as session:
        session.add(
            Message(
                conversation_id=conversation["id"],
                role="assistant",
                content="",
                status="streaming",
            )
        )
        await session.commit()

    listed = (await client.get("/api/conversations")).json()["items"][0]
    assert listed["unread"] is False


async def test_conversation_list_is_recent_first_and_shows_participants(client: httpx.AsyncClient):
    """列表按「最近活动」倒序；被 @ 召唤过的对话把参与者一起带出来（主场永远排第一）。

    排序口径是「对话 updated_at 与最后一条消息时间取较晚者」—— 这样即使历史数据里
    updated_at 没跟着消息走，最近有新消息的对话也不会被钉在下面。
    """
    home = await make_agent(client, name="主场智能体")
    guest = await make_agent(client, name="客座智能体")

    older = (await client.post("/api/conversations", json={"agent_profile_id": home["id"]})).json()
    newer = (await client.post("/api/conversations", json={"agent_profile_id": home["id"]})).json()
    t_old = datetime(2021, 1, 1, tzinfo=timezone.utc)
    t_new = datetime(2021, 1, 2, tzinfo=timezone.utc)

    async with SessionLocal() as session:
        # 两条对话的 updated_at 都停在旧时间：证明排序看的是真正的最新一条消息。
        for conversation_id in (older["id"], newer["id"]):
            row = await session.get(Conversation, conversation_id)
            row.updated_at = t_old
            session.add(row)
        session.add(
            Message(conversation_id=older["id"], role="user", content="旧消息", created_at=t_old)
        )
        # newer 由被 @ 召唤的客座智能体作答 —— 消息里记着它的 id。
        session.add(
            Message(
                conversation_id=newer["id"],
                role="assistant",
                content="客座的回答",
                agent_profile_id=guest["id"],
                created_at=t_new,
            )
        )
        await session.commit()

    items = (await client.get("/api/conversations")).json()["items"]
    assert [item["id"] for item in items] == [newer["id"], older["id"]]

    # 召唤过别的智能体：参与者 = 主场 + 客座（按首次出场排）。
    assert [p["id"] for p in items[0]["participants"]] == [home["id"], guest["id"]]
    assert [p["name"] for p in items[0]["participants"]] == ["主场智能体", "客座智能体"]
    # 没召唤过的对话只有主场一枚（前端据此画单聊头像）。
    assert [p["id"] for p in items[1]["participants"]] == [home["id"]]
    # 单条详情也带同一份参与者，聊天页与侧栏用的是同一口径。
    detail = (await client.get(f"/api/conversations/{newer['id']}")).json()
    assert [p["id"] for p in detail["conversation"]["participants"]] == [home["id"], guest["id"]]


async def test_conversation_list_puts_running_conversations_first(client: httpx.AsyncClient):
    """AI 还在回复的对话整组置顶，组内与已完成组内都按最近活动倒序。

    一轮长回复期间用户可能又开了新对话；只按时间排会把还在出字的那条挤下去。
    判定「在跑」看运行表（``registry``），不看最后一条消息的 ``streaming`` 状态。
    """
    agent = await make_agent(client)
    running = (await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})).json()
    newer = (await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})).json()
    oldest = (await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})).json()
    t_oldest = datetime(2021, 1, 1, tzinfo=timezone.utc)
    t_running = datetime(2021, 1, 2, tzinfo=timezone.utc)
    t_newer = datetime(2021, 1, 3, tzinfo=timezone.utc)

    async with SessionLocal() as session:
        for conversation_id, moment in (
            (oldest["id"], t_oldest),
            (running["id"], t_running),
            (newer["id"], t_newer),
        ):
            row = await session.get(Conversation, conversation_id)
            row.updated_at = moment
            session.add(row)
            session.add(
                Message(conversation_id=conversation_id, role="user", content="hi", created_at=moment)
            )
        await session.commit()

    # 没有运行：纯按最近活动倒序。
    items = (await client.get("/api/conversations")).json()["items"]
    assert [item["id"] for item in items] == [newer["id"], running["id"], oldest["id"]]

    # running 虽然时间最早，但还在回复 —— 整组置顶；两个已完成组内仍按时间倒序。
    run = registry.register(running["id"], agent["name"])
    try:
        snapshot = (await client.get("/api/conversations")).json()
        assert [item["id"] for item in snapshot["items"]] == [running["id"], newer["id"], oldest["id"]]
        # 置顶只靠运行表，`active_runs` 照旧返回（前端画旋转小环 + 排兜底都用它）。
        assert [item["conversation_id"] for item in snapshot["active_runs"]] == [running["id"]]
    finally:
        registry.finish(run)

    # 回复结束：回到纯时间序，不会因为曾经跑过就赖在顶上。
    after = (await client.get("/api/conversations")).json()["items"]
    assert [item["id"] for item in after] == [newer["id"], running["id"], oldest["id"]]


async def test_run_continues_after_the_client_disconnects(client: httpx.AsyncClient):
    """断开 SSE 订阅只是「不看了」，后台那一轮运行必须继续跑完并落库。"""
    agent = await make_agent(client, options={**cli_options("sess-42", "0.12")})
    conversation = (await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})).json()

    async with SessionLocal() as session:
        prepared = await prepare_run(session, conversation["id"], SendMessage(content="slow hello"))

    start_run(prepared)
    subscription = prepared.run.subscribe()
    first = await subscription.__anext__()
    assert first["type"] == "turn.started"
    # 模拟浏览器刷新 / 关页面：只关掉订阅，不调用取消接口。
    await subscription.aclose()

    row = None
    for _ in range(80):
        await asyncio.sleep(0.15)
        async with SessionLocal() as check:
            row = await check.get(Message, prepared.assistant_message.id)
            if row is not None and row.status != "streaming":
                break
    assert row is not None
    assert row.status == "complete"
    assert row.content == "Echo: slow hello... done"
    assert registry.active_for(conversation["id"]) is None


async def test_active_run_stream_replays_buffer_and_live_events(client: httpx.AsyncClient):
    """另一台设备 / 刷新后订阅：先重放已有事件，再接着收实时事件。"""
    agent = await make_agent(client)
    conversation = (await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})).json()
    run = registry.register(conversation["id"], agent["name"])
    run.assistant_message_id = "assistant-1"
    run.publish({"type": "turn.started", "run_id": run.id, "conversation_id": conversation["id"]})
    run.publish({"type": "message.delta", "text": "hello", "payload": {}})

    async def finish() -> None:
        await asyncio.sleep(0.1)
        run.publish({"type": "message.delta", "text": " world", "payload": {}})
        run.publish({"type": "run.finished", "payload": {"status": "ok"}})
        registry.finish(run, "ok")
        run.close()

    task = asyncio.create_task(finish())
    try:
        async with client.stream("GET", f"/api/conversations/{conversation['id']}/stream") as response:
            assert response.status_code == 200
            events = await read_sse(response)
    finally:
        await task

    kinds = [event["type"] for event in events]
    assert kinds[0] == "turn.started"
    assert "run.finished" in kinds
    deltas = "".join(event.get("text") or "" for event in events if event["type"] == "message.delta")
    assert deltas == "hello world"

    # 没有运行任务时，订阅接口明确返回 404，前端据此退回普通同步。
    missing = await client.get(f"/api/conversations/{conversation['id']}/stream")
    assert missing.status_code == 404


async def test_conversation_can_override_the_model(client: httpx.AsyncClient):
    """对话级模型：清单来自网关白名单，可设置、可清空回智能体默认。"""
    agent = await make_agent(client)
    conversation = (await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})).json()

    options = (await client.get(f"/api/conversations/{conversation['id']}/models")).json()
    assert options["models"] == ["fake-model", "other-model"]
    assert options["default"] == "fake-model"
    assert options["current"] is None

    patched = await client.patch(
        f"/api/conversations/{conversation['id']}", json={"model": "other-model"}
    )
    assert patched.status_code == 200
    assert patched.json()["model"] == "other-model"
    detail = (await client.get(f"/api/conversations/{conversation['id']}")).json()
    assert detail["conversation"]["model"] == "other-model"

    # 下一轮运行真的用这条对话的模型（而不是智能体默认）。
    async with SessionLocal() as session:
        prepared = await prepare_run(session, conversation["id"], SendMessage(content="hi"))
    try:
        assert prepared.ctx.model == "other-model"
    finally:
        registry.finish(prepared.run, "ok")

    # 自定义网关的模型白名单是服务端不变量，不能靠前端拦。
    rejected = await client.patch(
        f"/api/conversations/{conversation['id']}", json={"model": "not-in-whitelist"}
    )
    assert rejected.status_code == 422

    cleared = await client.patch(
        f"/api/conversations/{conversation['id']}", json={"model": None}
    )
    assert cleared.json()["model"] is None


async def test_multi_turn_uses_the_remembered_session(client: httpx.AsyncClient):
    agent = await make_agent(client)
    role = await make_profile(client, gateway_id=agent["gateway_id"], name="T")
    conversation = (await client.post("/api/conversations", json={"agent_profile_id": role["id"]})).json()

    for text in ("first", "second"):
        async with client.stream(
            "POST", f"/api/conversations/{conversation['id']}/messages", json={"content": text}
        ) as response:
            await read_sse(response)

    detail = (await client.get(f"/api/conversations/{conversation['id']}")).json()
    assert len(detail["messages"]) == 4
    assert detail["conversation"]["session_id"] == "sess-42"

    reset = await client.post(f"/api/conversations/{conversation['id']}/session/reset")
    assert reset.json()["session_id"] is None


async def test_configuration_error_returns_409(client: httpx.AsyncClient):
    agent = await make_agent(client, enabled=False)
    conversation = (await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})).json()
    response = await client.post(
        f"/api/conversations/{conversation['id']}/messages", json={"content": "hi"}
    )
    assert response.status_code == 409
    assert "已停用" in response.json()["detail"]


async def test_missing_executable_reports_a_clean_error(client: httpx.AsyncClient):
    options = {**cli_options("sess-42"), "command": "definitely-not-a-real-binary-12345"}
    agent = await make_agent(client, options=options)
    conversation = (await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})).json()
    async with client.stream(
        "POST", f"/api/conversations/{conversation['id']}/messages", json={"content": "hi"}
    ) as response:
        events = await read_sse(response)
    assert any(e["type"] == "error" and "未安装" in (e.get("text") or "") for e in events)
    assert events[-2]["type"] == "run.finished"
    assert events[-2]["payload"]["status"] == "error"

    # 落库的那条助手消息也要记住真正的错误原因（不能只剩一句「智能体返回了错误」）。
    detail = (await client.get(f"/api/conversations/{conversation['id']}")).json()
    assistant = detail["messages"][-1]
    assert assistant["status"] == "error"
    assert "未安装" in (assistant["error"] or "")


async def test_probe_endpoint_reports_missing_binary(client: httpx.AsyncClient):
    bad = await make_agent(
        client, options={**cli_options("sess-42"), "command": "nope-not-here-98765"}
    )
    result = (await client.post(f"/api/agents/{bad['id']}/test")).json()
    assert result["ok"] is False
    assert any("未在 PATH 中找到" in c["detail"] for c in result["checks"] if not c["ok"])


async def test_agent_and_conversation_guards(client: httpx.AsyncClient):
    agent = await make_agent(client)
    role = await make_profile(client, gateway_id=agent["gateway_id"], name="Solo")
    conversation = (await client.post("/api/conversations", json={"agent_profile_id": role["id"]})).json()

    assert (await client.delete(f"/api/conversations/{conversation['id']}")).status_code == 204
    assert (await client.delete(f"/api/agents/{role['id']}")).status_code == 204

    missing = await client.post(
        "/api/agents", json={"name": "x", "type_id": "custom-cli", "gateway_id": "ghost"}
    )
    assert missing.status_code == 422


async def test_agent_delete_keeps_its_conversations_readable(client: httpx.AsyncClient):
    """有历史对话不阻断删除智能体：软删除后它从列表消失，但历史仍完整可读。"""
    gateway = await make_gateway(client)
    role = await make_profile(client, gateway_id=gateway["id"], name="Solo")
    conversation = (await client.post("/api/conversations", json={"agent_profile_id": role["id"]})).json()

    assert (await client.delete(f"/api/agents/{role['id']}")).status_code == 204

    assert (await client.get("/api/agents")).json()["items"] == []
    assert (await client.get(f"/api/agents/{role['id']}")).status_code == 404
    # 历史对话仍显示当时的名字与头像（对话快照不因智能体删除而失效）
    detail = (await client.get(f"/api/conversations/{conversation['id']}")).json()["conversation"]
    assert detail["agent_name"] == "Solo"
    assert detail["agent_avatar"] == role["avatar"]
    # 已删除的智能体不能再用来开新对话
    rejected = await client.post("/api/conversations", json={"agent_profile_id": role["id"]})
    assert rejected.status_code == 422
    # 重复删除按不存在处理
    assert (await client.delete(f"/api/agents/{role['id']}")).status_code == 404


async def test_soft_deleted_agent_frees_its_gateway(client: httpx.AsyncClient):
    """智能体软删除后不再占用网关（否则网关会被历史引用永久锁死）。"""
    agent = await make_agent(client)
    assert (await client.delete(f"/api/gateways/{agent['gateway_id']}")).status_code == 409

    assert (await client.delete(f"/api/agents/{agent['id']}")).status_code == 204
    assert (await client.delete(f"/api/gateways/{agent['gateway_id']}")).status_code == 204


async def test_system_overview_counts_builtin_types(client: httpx.AsyncClient):
    overview = (await client.get("/api/system/overview")).json()
    assert overview["agent_types"] == 9  # 豆包 CLI 之后共 9 个内置适配器
    assert overview["installed_types"] >= 1  # 当前环境中已安装 claude-code
    assert overview["version"] == __version__
    assert overview["version_history"][0]["version"] == overview["version"]
    assert overview["version_history"][0]["highlights"]


async def _conversation_with_workspace(
    client: httpx.AsyncClient, tmp_path: Path
) -> tuple[dict, Path]:
    """建一个工作目录可控的对话，附件测试不必往真实数据目录里写东西。"""
    agent = await make_agent(client)
    role = await make_profile(client, gateway_id=agent["gateway_id"], name="Tester")
    workspace = tmp_path / "ws"
    conversation = (
        await client.post(
            "/api/conversations", json={"agent_profile_id": role["id"], "workspace_dir": str(workspace)}
        )
    ).json()
    return conversation, workspace


async def test_attachment_is_saved_in_the_workspace_and_reaches_the_prompt(
    client: httpx.AsyncClient, tmp_path: Path
):
    conversation, workspace = await _conversation_with_workspace(client, tmp_path)
    raw = "季度报表内容".encode()

    uploaded = await client.post(
        f"/api/conversations/{conversation['id']}/attachments",
        json={"name": "季度报表.txt", "content_base64": base64.b64encode(raw).decode()},
    )
    assert uploaded.status_code == 201, uploaded.text
    info = uploaded.json()
    assert info["name"] == "季度报表.txt"
    assert info["rel_path"].startswith("attachments/")
    assert info["size"] == len(raw)
    saved = workspace / info["rel_path"]
    assert saved.is_file() and saved.read_bytes() == raw

    async with client.stream(
        "POST",
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": "看看这个", "attachments": [info["rel_path"]]},
    ) as response:
        assert response.status_code == 200
        events = await read_sse(response)

    # 附件行真的进了智能体看到的提示词（替身智能体会回显提示词开头）。
    echoed = "".join(e.get("text") or "" for e in events if e["type"] == "message.delta")
    assert "📎 附件" in echoed

    detail = (await client.get(f"/api/conversations/{conversation['id']}")).json()
    assert detail["messages"][1]["status"] == "complete"
    user = detail["messages"][0]
    assert user["role"] == "user"
    assert user["content"].startswith("看看这个")
    assert info["rel_path"] in user["content"]


async def test_attachment_only_message_is_allowed(client: httpx.AsyncClient, tmp_path: Path):
    conversation, _ = await _conversation_with_workspace(client, tmp_path)
    info = (
        await client.post(
            f"/api/conversations/{conversation['id']}/attachments",
            json={"name": "note.md", "content_base64": base64.b64encode(b"# hi").decode()},
        )
    ).json()

    async with client.stream(
        "POST",
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": "   ", "attachments": [info["rel_path"]]},
    ) as response:
        assert response.status_code == 200
        await read_sse(response)

    detail = (await client.get(f"/api/conversations/{conversation['id']}")).json()
    assert detail["messages"][0]["content"].startswith("📎 附件")

    # 既没有正文也没有附件 → 422（与旧行为一致）
    empty = await client.post(
        f"/api/conversations/{conversation['id']}/messages", json={"content": "  "}
    )
    assert empty.status_code == 422


async def test_attachment_upload_guards(client: httpx.AsyncClient, tmp_path: Path, monkeypatch):
    conversation, workspace = await _conversation_with_workspace(client, tmp_path)
    base = f"/api/conversations/{conversation['id']}/attachments"

    invalid = await client.post(base, json={"name": "x.txt", "content_base64": "不是 base64!!"})
    assert invalid.status_code == 422
    assert "base64" in invalid.json()["detail"]

    # 文件名里的目录成分被剥掉：跑不出工作目录
    sneaky = await client.post(
        base, json={"name": "../../逃逸.txt", "content_base64": base64.b64encode(b"x").decode()}
    )
    assert sneaky.status_code == 201, sneaky.text
    assert sneaky.json()["name"] == "逃逸.txt"
    assert ".." not in sneaky.json()["rel_path"]

    # 超过上限的附件被拒（把上限压到 4 字节，不必真造一个 8 MB 的请求）
    from app.api import conversations as api_conversations

    monkeypatch.setattr(api_conversations, "MAX_ATTACHMENT_BYTES", 4)
    oversized = await client.post(
        base, json={"name": "big.bin", "content_base64": base64.b64encode(b"0123456789").decode()}
    )
    assert oversized.status_code == 413
    assert "附件过大" in oversized.json()["detail"]

    # 指向工作目录之外的附件一律拒绝，别让模型去读工作目录以外的文件
    async with client.stream(
        "POST",
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": "hi", "attachments": ["../../etc/hosts"]},
    ) as response:
        assert response.status_code == 409


async def test_workspace_file_endpoint_serves_attachments_and_blocks_traversal(
    client: httpx.AsyncClient, tmp_path: Path
):
    """聊天里嵌图的前提：工作目录里的文件能被浏览器取到，且**只能**取到目录内部的。

    附件的 rel_path 是工作目录里的相对路径，浏览器拿不到本地文件；没有这个只读出口，
    上传/生成的图片在页面上永远是一个裂图或一串路径。
    """
    conversation, workspace = await _conversation_with_workspace(client, tmp_path)
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+P+/HgAFhAJ/"
        "wlseKgAAAABJRU5ErkJggg=="
    )
    info = (
        await client.post(
            f"/api/conversations/{conversation['id']}/attachments",
            json={"name": "截图.png", "content_base64": base64.b64encode(png).decode()},
        )
    ).json()

    got = await client.get(f"/api/conversations/{conversation['id']}/files/{info['rel_path']}")
    assert got.status_code == 200, got.text
    assert got.headers["content-type"].startswith("image/png")
    assert got.content == png

    # 目录穿越被拒（不能读到工作目录以外的文件）；用编码过的 `../` 绕过客户端归一化。
    (workspace.parent / "secret.txt").write_text("top secret", encoding="utf-8")
    escape = await client.get(f"/api/conversations/{conversation['id']}/files/..%2Fsecret.txt")
    assert escape.status_code == 422

    # 智能体常直接回工作目录的**绝对路径**（容器内是 `/app/data/workspaces/<id>/a.png`）：
    # 只要它确实在当前工作目录内，就要能取到；不做跨工作区重映射。
    (workspace / "generated.png").write_bytes(png)
    absolute = str(workspace / "generated.png")
    got_absolute = await client.get(
        f"/api/conversations/{conversation['id']}/files/{quote(absolute, safe='')}"
    )
    assert got_absolute.status_code == 200, got_absolute.text
    assert got_absolute.content == png

    # 工作目录之外的绝对路径仍被拒绝。
    outside = str(workspace.parent / "secret.txt")
    crossed = await client.get(
        f"/api/conversations/{conversation['id']}/files/{quote(outside, safe='')}"
    )
    assert crossed.status_code == 422

    # 不存在的文件给明确的 4xx，而不是裸 500
    missing = await client.get(
        f"/api/conversations/{conversation['id']}/files/attachments/nope.png"
    )
    assert missing.status_code == 422


async def test_attachment_upload_reports_an_unusable_workspace(
    client: httpx.AsyncClient, tmp_path: Path
):
    """工作目录不可用时要给出明确提示，而不是让建目录的异常冒成裸 500。"""
    agent = await make_agent(client)
    role = await make_profile(client, gateway_id=agent["gateway_id"], name="Tester")
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("x", encoding="utf-8")
    conversation = (
        await client.post(
            "/api/conversations", json={"agent_profile_id": role["id"], "workspace_dir": str(blocked)}
        )
    ).json()

    response = await client.post(
        f"/api/conversations/{conversation['id']}/attachments",
        json={"name": "x.txt", "content_base64": base64.b64encode(b"x").decode()},
    )
    assert response.status_code == 409
    assert "工作目录不可用" in response.json()["detail"]


async def test_removed_kernel_gives_a_readable_error(client: httpx.AsyncClient):
    """适配器下架后，历史实例仍留在库里：聊天与重新生成都要给中文提示，而不是裸 500。

    这是「取消某个内核」的兼容性兜底：数据一行不删，但使用到它时要明确说清楚哪里不对。
    """
    agent = await make_agent(client)
    role = await make_profile(client, gateway_id=agent["gateway_id"], name="Legacy")
    conversation = (await client.post("/api/conversations", json={"agent_profile_id": role["id"]})).json()

    async with client.stream(
        "POST", f"/api/conversations/{conversation['id']}/messages", json={"content": "hi"}
    ) as response:
        assert response.status_code == 200
        await read_sse(response)

    # 模拟库里残留的、已经下架的内核（接口不允许这么建，只能直接改库）
    async with SessionLocal() as session:
        await session.execute(
            update(AgentProfile)
            .where(AgentProfile.id == role["id"])
            .values(type_id="removed-kernel")
        )
        await session.commit()

    response = await client.post(
        f"/api/conversations/{conversation['id']}/messages", json={"content": "再来一轮"}
    )
    assert response.status_code == 409
    assert "未知的智能体类型" in response.json()["detail"]

    regen = await client.post(f"/api/conversations/{conversation['id']}/regenerate")
    assert regen.status_code == 409
    assert "未知的智能体类型" in regen.json()["detail"]

    # 列表接口仍然要把这条智能体展示出来（历史对话照旧可读），只是标成「未知类型」
    items = (await client.get("/api/agents")).json()["items"]
    assert [row["id"] for row in items] == [agent["id"], role["id"]]
    assert items[1]["type_id"] == "removed-kernel"


async def test_default_agent_star_is_unique_and_drives_new_conversations(client: httpx.AsyncClient):
    """五角星全局唯一：不指定智能体时，新建对话直接用星标着的那个。"""
    first = await make_agent(client)
    # 第一个智能体自动戴上星：装好就能点「新建对话」
    assert first["is_default"] is True
    second = await make_profile(client, gateway_id=first["gateway_id"], name="第二个")
    assert second["is_default"] is False

    starred = await client.patch(f"/api/agents/{second['id']}", json={"is_default": True})
    assert starred.json()["is_default"] is True
    listed = (await client.get("/api/agents")).json()["items"]
    assert [item["is_default"] for item in listed] == [False, True]

    created = await client.post("/api/conversations", json={})
    assert created.status_code == 201, created.text
    assert created.json()["agent_profile_id"] == second["id"]
    assert created.json()["workspace_dir"] is None

    # 摘掉星、而且有不止一个可选智能体 → 报错要能告诉用户去哪儿点星
    assert (
        await client.patch(f"/api/agents/{second['id']}", json={"is_default": False})
    ).json()["is_default"] is False
    denied = await client.post("/api/conversations", json={})
    assert denied.status_code == 422
    assert "五角星" in denied.json()["detail"]


async def test_lonely_agent_is_used_without_a_star(client: httpx.AsyncClient):
    """只有一启用智能体时（还没点过星）也直接用它，别卡住新建。"""
    agent = await make_agent(client)
    await client.patch(f"/api/agents/{agent['id']}", json={"is_default": False})
    created = await client.post("/api/conversations", json={})
    assert created.status_code == 201, created.text
    assert created.json()["agent_profile_id"] == agent["id"]


async def test_deleting_the_starred_agent_promotes_another(client: httpx.AsyncClient):
    first = await make_agent(client)
    second = await make_profile(client, gateway_id=first["gateway_id"], name="第二个")
    assert first["is_default"] is True

    assert (await client.delete(f"/api/agents/{first['id']}")).status_code == 204
    listed = (await client.get("/api/agents")).json()["items"]
    assert [item["id"] for item in listed] == [second["id"]]
    assert listed[0]["is_default"] is True


async def test_conversation_scope_change_resets_the_session(client: httpx.AsyncClient, tmp_path: Path):
    """聊天页输入框上方那两个小图标：换智能体 / 换工作目录都要清掉会话记忆。"""
    agent = await make_agent(client)
    other = await make_profile(client, gateway_id=agent["gateway_id"], name="另一个")
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})
    ).json()

    async def set_session(value: str) -> None:
        async with SessionLocal() as db:
            await db.execute(
                update(Conversation).where(Conversation.id == conversation["id"]).values(session_id=value)
            )
            await db.commit()

    # 只改标题：记忆不动
    await set_session("sess-1")
    renamed = await client.patch(f"/api/conversations/{conversation['id']}", json={"title": "改个名"})
    assert renamed.json()["session_id"] == "sess-1"

    # 换工作目录：记忆清空（换了目录就是另一份上下文）
    moved = await client.patch(
        f"/api/conversations/{conversation['id']}", json={"workspace_dir": str(tmp_path)}
    )
    assert moved.json()["workspace_dir"] == str(tmp_path)
    assert moved.json()["session_id"] is None

    # 换智能体：同样清空
    await set_session("sess-2")
    switched = await client.patch(
        f"/api/conversations/{conversation['id']}", json={"agent_profile_id": other["id"]}
    )
    assert switched.json()["agent_profile_id"] == other["id"]
    assert switched.json()["session_id"] is None

    # 工作目录可以留空 = 回到「按对话自动分配」
    cleared = await client.patch(f"/api/conversations/{conversation['id']}", json={"workspace_dir": ""})
    assert cleared.json()["workspace_dir"] is None

    # 不存在的智能体：422，且这条对话不该被改动
    ghost = await client.patch(
        f"/api/conversations/{conversation['id']}", json={"agent_profile_id": "ghost"}
    )
    assert ghost.status_code == 422
    assert (await client.get(f"/api/conversations/{conversation['id']}")).json()["conversation"][
        "agent_profile_id"
    ] == other["id"]


async def _send_and_read(client: httpx.AsyncClient, conversation_id: str, body: dict) -> list[dict]:
    """发一条消息并把 SSE 事件读完（fake 内核秒回，读完即整轮结束）。"""
    async with client.stream(
        "POST", f"/api/conversations/{conversation_id}/messages", json=body
    ) as response:
        assert response.status_code == 200, (await response.aread()).decode()
        return await read_sse(response)


async def test_switching_agent_keeps_history_in_prompt(client: httpx.AsyncClient):
    """换智能体不再失忆：session 被清掉的那一轮，prompt 里带着此前几轮的最终回复。

    fake 内核会回显 prompt 的前 40 个字符，据此断言历史真的进了上下文。
    """
    agent = await make_agent(client)
    other = await make_profile(client, gateway_id=agent["gateway_id"], name="另一个")
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})
    ).json()

    await _send_and_read(client, conversation["id"], {"content": "第一条重要事实"})
    # 换智能体（session_id 被有意清空）
    await client.patch(f"/api/conversations/{conversation['id']}", json={"agent_profile_id": other["id"]})

    async with client.stream(
        "POST", f"/api/conversations/{conversation['id']}/messages", json={"content": "接着说"}
    ) as response:
        events = await read_sse(response)

    # 回显的 prompt 里能看到前置上下文的提示语与上一轮最终回复的开头
    # （fake 内核只回显前 40 字符，暗号全文落不进窗口，断言窗口内的片段）。
    text = "".join(
        e.get("text", "") for e in events if e.get("type") == "message.delta"
    )
    assert "交流记录" in text
    assert "第一条重要" in text


async def test_summon_answers_one_turn_and_keeps_ownership(client: httpx.AsyncClient):
    """@ 召唤：被召唤的智能体回答这一轮，对话归属与主场会话记忆都不动。"""
    agent = await make_agent(client)
    other = await make_profile(client, gateway_id=agent["gateway_id"], name="被召唤者")
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})
    ).json()

    # 先用主场智能体跑一轮，确立 session 记忆
    await _send_and_read(client, conversation["id"], {"content": "主场第一轮"})

    # @ 召唤另一名智能体回答这一轮
    await _send_and_read(
        client,
        conversation["id"],
        {"content": "轮到你了", "summon_profile_id": other["id"]},
    )

    detail = (await client.get(f"/api/conversations/{conversation['id']}")).json()
    # 对话归属不变
    assert detail["conversation"]["agent_profile_id"] == agent["id"]
    # 主场的 session 记忆不被召唤轮回报的 session_id 顶掉（fake 内核会回报 sess-42）
    assert detail["conversation"]["session_id"] == "sess-42"
    # 召唤轮的回复标记了产出者
    summon_reply = next(
        m for m in detail["messages"] if m["role"] == "assistant" and m["agent_profile_id"] == other["id"]
    )
    assert summon_reply["status"] == "complete"
    # 主场轮的回复没有归属标记
    home_reply = next(
        m for m in detail["messages"] if m["role"] == "assistant" and not m["agent_profile_id"]
    )
    assert home_reply is not None


async def test_summon_run_snapshot_carries_the_answerer(client: httpx.AsyncClient):
    """运行快照必须带**本轮作答者**：另一台设备重连时靠它显示正确的名字与头像。

    若这里错记成对话归属的智能体，被召唤者就会顶着主场智能体的头像说话。
    """
    agent = await make_agent(client)
    other = await make_profile(client, gateway_id=agent["gateway_id"], name="被召唤者")
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})
    ).json()

    async with SessionLocal() as session:
        prepared = await prepare_run(
            session,
            conversation["id"],
            SendMessage(content="召唤", summon_profile_id=other["id"]),
        )
    try:
        snapshot = prepared.run.snapshot()
        assert snapshot["agent_name"] == "被召唤者"
        assert snapshot["agent_profile_id"] == other["id"]
        assert snapshot["agent_avatar"] == other["avatar"]
    finally:
        registry.finish(prepared.run, "ok")


async def test_summon_shares_history_but_not_session(client: httpx.AsyncClient):
    """召唤轮共享对话历史（前置上下文里能看到主场的回复），但不续接主场会话。"""
    agent = await make_agent(client)
    other = await make_profile(client, gateway_id=agent["gateway_id"], name="被召唤者")
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})
    ).json()

    await _send_and_read(client, conversation["id"], {"content": "主场说了个暗号：企鹅爱吃鱼"})

    events = await _send_and_read(
        client,
        conversation["id"],
        {"content": "暗号是什么", "summon_profile_id": other["id"]},
    )
    text = "".join(e.get("text", "") for e in events if e.get("type") == "message.delta")
    # fake 内核回显 prompt 前 40 字符：前置上下文的提示语或暗号本身都应落在窗口里
    assert "交流记录" in text or "企鹅爱吃鱼" in text


async def test_summon_rejects_invalid_targets(client: httpx.AsyncClient):
    """召唤不存在的 / 对话本身的智能体：422 / 409，拒绝得干净。"""
    agent = await make_agent(client)
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})
    ).json()

    # 不存在的智能体
    response = await client.post(
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": "召唤", "summon_profile_id": "ghost"},
    )
    assert response.status_code == 422

    # 召唤对话自己的智能体：没有意义，409
    response = await client.post(
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": "召唤自己", "summon_profile_id": agent["id"]},
    )
    assert response.status_code == 409


async def test_summon_uses_isolated_home_and_leaves_main_home_alone(
    client: httpx.AsyncClient,
):
    """召唤轮的隔离目录是一次性作用域：即使与主场智能体同内核，也不碰主场的家。

    用 claude-code 内核（声明 skills，物化必然建 ``claude-home``）做同内核对照：
    主场的家先建好，召唤同内核的智能体跑一轮 —— 主场的家必须保持原样，
    召唤轮的家另立 ``summon-`` 目录。
    """
    from app.settings import get_settings

    agent = await make_agent(client, type_id="claude-code", name="主场")
    other = await make_profile(
        client,
        gateway_id=agent["gateway_id"],
        type_id="claude-code",
        name="同内核被召唤者",
    )
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})
    ).json()

    # 主场先跑一轮：主场的隔离目录（claude-home）就此建立
    await _send_and_read(client, conversation["id"], {"content": "主场第一轮"})
    homes_root = get_settings().agent_homes_root
    main_home = homes_root / conversation["id"]
    assert main_home.is_dir(), "主场先跑一轮应该建立自己的隔离目录"
    main_before = sorted(str(p.relative_to(homes_root)) for p in main_home.rglob("*"))

    # 同内核召唤一轮
    await _send_and_read(
        client,
        conversation["id"],
        {"content": "召唤轮", "summon_profile_id": other["id"]},
    )

    # 主场的家一个字节都没动
    main_after = sorted(str(p.relative_to(homes_root)) for p in main_home.rglob("*"))
    assert main_after == main_before
    # 召唤轮的家另立门户（summon- 前缀的作用域），不落在对话 id 下
    summon_homes = [p for p in homes_root.iterdir() if p.name.startswith("summon-")]
    assert summon_homes, "召唤轮应该有自己的隔离目录"


async def test_running_conversation_refuses_scope_change(client: httpx.AsyncClient):
    """正在跑的对话不让中途换智能体 / 换目录（换了会把这一轮带偏）。"""
    agent = await make_agent(client)
    other = await make_profile(client, gateway_id=agent["gateway_id"], name="另一个")
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})
    ).json()
    registry.register(conversation["id"], "假智能体")
    try:
        blocked = await client.patch(
            f"/api/conversations/{conversation['id']}", json={"agent_profile_id": other["id"]}
        )
        assert blocked.status_code == 409
        assert "正在运行" in blocked.json()["detail"]
    finally:
        registry.cancel_for_conversation(conversation["id"])
