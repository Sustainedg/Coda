"""任务进度卡片 · MCP 通道：绑定、凭据、内部接口与 stdio server。

设计见 ``docs/计划任务/MCP任务进度卡方案.md``。覆盖：

* ``runtime/task_card_mcp.py``：每轮 context / 令牌 / 凭据文件权限 / 失效 / 限流计数；
* ``apply_block(source="mcp")``：一轮多次更新不被 ``last_run_id`` 吞掉，围栏仍幂等；
* ``api/task_cards_internal.py``：401 / 409 / 422 / 429 / 200，归属只认绑定；
* ``scripts/mcp/task_card_mcp_server.py``：协议分发、凭据查找、错误语义。
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.agents import AgentContext, get_agent
from app.db import SessionLocal
from app.models import Conversation, Message
from app.runtime import mcp as mcp_service
from app.runtime import service as runtime_service
from app.runtime import task_card_mcp
from app.runtime import task_cards as tc
from app.runtime.manager import registry
from app.settings import get_settings
import helpers

SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "mcp" / "task_card_mcp_server.py"
UPDATE_URL = "/api/internal/task-cards/update"


# --- 夹具 ---------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_bindings():
    """每个用例前后清空进程内绑定，避免令牌串用例。"""
    task_card_mcp._contexts.clear()
    task_card_mcp._by_token.clear()
    task_card_mcp._pending_tokens.clear()
    yield
    task_card_mcp._contexts.clear()
    task_card_mcp._by_token.clear()
    task_card_mcp._pending_tokens.clear()


@pytest.fixture(scope="module")
def server_module():
    spec = importlib.util.spec_from_file_location("task_card_mcp_server", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def make_conversation(client: httpx.AsyncClient) -> str:
    agent = await helpers.make_agent(client)
    response = await client.post(
        "/api/conversations", json={"agent_profile_id": agent["id"]}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _bind(client: httpx.AsyncClient, tmp_path: Path):
    """建对话 + 绑定 + 返回 (conversation_id, context_id, token, run)。"""
    conversation_id = await make_conversation(client)
    context_id, _root = task_card_mcp.begin(conversation_id, None, tmp_path)
    run = registry.register(conversation_id, "测试内核")
    task_card_mcp.bind(context_id, conversation_id, None, run.id, None)
    token = task_card_mcp.read_token(context_id)
    assert token
    return conversation_id, context_id, token, run


# --- 绑定与凭据 ---------------------------------------------------------------


def test_begin_writes_0600_credentials_and_authenticates(tmp_path):
    context_id, root = task_card_mcp.begin("conv-1", "user-1", tmp_path)
    assert root == tmp_path / "task-card"
    path = task_card_mcp.credential_rel_path(context_id)
    auth_file = tmp_path / path
    assert auth_file.is_file()
    # 文件 0600、目录 0700
    assert stat.S_IMODE(auth_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(auth_file.parent.stat().st_mode) == 0o700

    token = task_card_mcp.read_token(context_id)
    assert token and token.startswith("tc_")
    payload = json.loads(auth_file.read_text(encoding="utf-8"))
    assert payload["token"] == token
    assert payload["context_id"] == context_id

    binding = task_card_mcp.authenticate(f"Bearer {token}")
    assert binding is not None and binding.context_id == context_id
    # 方案大小写不敏感
    assert task_card_mcp.authenticate(f"bearer {token}") is not None
    assert task_card_mcp.authenticate(f"Basic {token}") is None
    assert task_card_mcp.authenticate("Bearer pat_xxx") is None
    assert task_card_mcp.authenticate(None) is None


def test_credentials_can_be_written_lazily(tmp_path):
    """家目录要等 MCP 物化后才知道：begin 先返回 None，随后补写。"""
    context_id, root = task_card_mcp.begin("conv-2", None)
    assert root is None
    written = task_card_mcp.write_credentials(context_id, tmp_path)
    assert written == tmp_path / "task-card"
    assert task_card_mcp.read_token(context_id)


def test_finish_invalidates_and_removes_credentials(tmp_path):
    context_id, root = task_card_mcp.begin("conv-3", None, tmp_path)
    token = task_card_mcp.read_token(context_id)
    assert root and root.exists()

    task_card_mcp.finish(context_id, "complete")
    assert not root.exists()
    assert task_card_mcp.authenticate(f"Bearer {token}") is None
    # 幂等：重复收口不抛错
    task_card_mcp.finish(context_id, "failed")


def test_finish_only_removes_its_own_credentials(tmp_path):
    """同一对话两轮重叠时，一轮收口不能删掉另一轮还在用的凭据。"""
    first, _root = task_card_mcp.begin("conv-overlap", None, tmp_path)
    second, _root2 = task_card_mcp.begin("conv-overlap", None, tmp_path)
    first_token = task_card_mcp.read_token(first)
    second_token = task_card_mcp.read_token(second)
    assert first_token and second_token

    task_card_mcp.finish(first, "complete")
    assert task_card_mcp.authenticate(f"Bearer {first_token}") is None
    assert task_card_mcp.read_token(second) == second_token
    assert (tmp_path / "task-card").exists()

    task_card_mcp.finish(second, "complete")
    assert not (tmp_path / "task-card").exists()


def test_task_card_slug_is_consistent_across_layers():
    """内置展示项、内部物化 spec、适配器权限工具名必须用同一个 slug。"""
    from app.agents.mcp import TASK_CARD_MCP_SLUG, TASK_CARD_MCP_TOOL
    from app.builtin_mcp import TASK_CARD
    from app.runtime.mcp import INTERNAL_TASK_CARD_SLUG

    assert TASK_CARD.slug == TASK_CARD_MCP_SLUG == INTERNAL_TASK_CARD_SLUG
    assert TASK_CARD_MCP_TOOL == f"mcp__{TASK_CARD_MCP_SLUG}__update_task_card"


def test_mark_used_and_rate_limit_counter(tmp_path):
    context_id, _root = task_card_mcp.begin("conv-4", None, tmp_path)
    assert task_card_mcp.was_used(context_id) is False
    task_card_mcp.mark_used(context_id)
    binding = task_card_mcp.authenticate(f"Bearer {task_card_mcp.read_token(context_id)}")
    assert binding is not None
    assert binding.calls == 1
    assert binding.mcp_used is True
    assert task_card_mcp.was_used(context_id) is True


def test_render_notice_contains_context_and_current_card():
    from app.models import TaskCard

    context_id, _root = task_card_mcp.begin("conv-5", None)
    card = TaskCard(
        conversation_id="conv-5",
        title="实现进度卡",
        items=[
            {"id": "t1", "text": "后端", "done": True},
            {"id": "t2", "text": "前端", "done": False},
        ],
    )
    notice = task_card_mcp.render_notice(context_id, card)
    assert context_id in notice
    assert "<task_card_context>" in notice
    assert "update_task_card" in notice
    assert "不要再输出 task_card 围栏" in notice
    assert "内核自带" in notice and "不要用它们代替" in notice
    assert "1/2 已完成" in notice
    assert "t1 [已完成] 后端" in notice
    assert "t2 [待办] 前端" in notice


# --- 合并：MCP 一轮多次更新 ----------------------------------------------------


async def test_mcp_allows_multiple_updates_in_one_run(client: httpx.AsyncClient):
    conversation_id = await make_conversation(client)
    create = {
        "action": "create",
        "title": "卡片",
        "items": [
            {"id": "t1", "text": "甲", "done": False},
            {"id": "t2", "text": "乙", "done": False},
        ],
    }
    first = await tc.apply_block(conversation_id, None, create, "run-1", source="mcp")
    assert first is not None and first.version == 1

    # 同一 run 的第二次更新：MCP 不能按 last_run_id 跳过
    second = await tc.apply_block(
        conversation_id,
        None,
        {"action": "update", "items": [{"id": "t1", "done": True}]},
        "run-1",
        source="mcp",
    )
    assert second is not None and second.version == 2
    assert second.items[0]["done"] is True

    third = await tc.apply_block(
        conversation_id,
        None,
        {"action": "update", "items": [{"id": "t2", "done": True}]},
        "run-1",
        source="mcp",
    )
    assert third is not None and third.version == 3
    assert all(item["done"] for item in third.items)

    # 重复提交同一内容：不 bump
    same = await tc.apply_block(
        conversation_id,
        None,
        {"action": "update", "items": [{"id": "t2", "done": True}]},
        "run-1",
        source="mcp",
    )
    assert same is not None and same.version == 3


async def test_fence_still_idempotent_by_run(client: httpx.AsyncClient):
    conversation_id = await make_conversation(client)
    create = {
        "action": "create",
        "title": "卡片",
        "items": [
            {"id": "t1", "text": "甲", "done": False},
            {"id": "t2", "text": "乙", "done": False},
        ],
    }
    first = await tc.apply_block(conversation_id, None, create, "run-f", source="fence")
    assert first is not None and first.version == 1
    replay = await tc.apply_block(conversation_id, None, create, "run-f", source="fence")
    assert replay is not None and replay.version == 1


# --- 物化注入 -----------------------------------------------------------------


async def test_prepare_mcp_home_appends_internal_server(tmp_path, monkeypatch):
    definition = get_agent("claude-code")

    async def fake_servers(_session, _profile, **_kwargs):
        return []

    monkeypatch.setattr(mcp_service, "mcp_servers_for_agent", fake_servers)
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    ctx = AgentContext(
        agent_id="a1",
        agent_name="测试",
        workspace_dir=str(workspace),
        home_dir=str(home),
    )
    context_id = "e" * 32
    await mcp_service.prepare_mcp_home(
        None,
        definition,
        None,
        None,
        ctx,
        scope_id="conv-1",
        task_card_context_id=context_id,
    )
    assert [spec.slug for spec in ctx.mcp_servers] == ["coda-task-card"]
    payload = json.loads((home / "mcp.json").read_text(encoding="utf-8"))
    entry = payload["mcpServers"]["coda-task-card"]
    assert entry["command"]
    assert entry["env"]["CODA_TASK_CARD_HOME"] == str(home)
    assert entry["env"]["CODA_TASK_CARD_API"].endswith("/api/internal/task-cards/update")
    # 令牌与凭据路径都不进配置：只有家目录与接口地址
    assert "tc_" not in json.dumps(entry)
    assert "auth.json" not in json.dumps(entry)


@pytest.mark.parametrize(
    "type_id",
    ["claude-code", "codex", "codebuddy-cli", "hermes-agent", "deepseek-harness"],
)
async def test_all_mcp_kernels_mount_internal_task_card(
    type_id: str, tmp_path, monkeypatch
):
    """声明了 Capabilities.mcp 的 5 个内核都要能把内部 server 写进自己的配置。"""
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    definition = get_agent(type_id)
    assert definition.capabilities.mcp is True

    async def fake_servers(_session, _profile, **_kwargs):
        return []

    monkeypatch.setattr(mcp_service, "mcp_servers_for_agent", fake_servers)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    ctx = AgentContext(
        agent_id="a1",
        agent_name="测试",
        workspace_dir=str(workspace),
        home_dir=str(home),
    )
    await mcp_service.prepare_mcp_home(
        None,
        definition,
        None,
        None,
        ctx,
        scope_id="conv-1",
        task_card_context_id="a" * 32,
    )
    assert any(spec.slug == "coda-task-card" for spec in ctx.mcp_servers)
    mount = definition.mcp_mount(ctx)
    assert mount is not None
    rendered = "\n".join([*mount.files.values(), *mount.workspace_files.values()])
    assert "coda-task-card" in rendered
    assert "CODA_TASK_CARD_HOME" in rendered


async def test_prepare_mcp_home_skips_internal_for_non_mcp_kernel(tmp_path, monkeypatch):
    definition = get_agent("custom-cli")

    async def fake_servers(_session, _profile, **_kwargs):
        return []

    monkeypatch.setattr(mcp_service, "mcp_servers_for_agent", fake_servers)
    ctx = AgentContext(
        agent_id="a1",
        agent_name="测试",
        workspace_dir=str(tmp_path / "ws"),
        home_dir=str(tmp_path / "home"),
    )
    await mcp_service.prepare_mcp_home(
        None, definition, None, None, ctx, scope_id="conv-2", task_card_context_id="f" * 32
    )
    assert all(spec.slug != "coda-task-card" for spec in ctx.mcp_servers)


# --- 内部接口 -----------------------------------------------------------------


async def test_internal_route_requires_valid_token(client: httpx.AsyncClient, tmp_path):
    conversation_id, context_id, token, run = await _bind(client, tmp_path)
    payload = {
        "context_id": context_id,
        "action": "create",
        "title": "卡片",
        "items": [
            {"id": "t1", "text": "甲", "done": False},
            {"id": "t2", "text": "乙", "done": False},
        ],
    }
    try:
        assert (
            await client.post(UPDATE_URL, json=payload)
        ).status_code == 401
        assert (
            await client.post(UPDATE_URL, json=payload, headers=_auth("tc_wrong"))
        ).status_code == 401
    finally:
        task_card_mcp.finish(context_id, "complete")
        registry.finish(run)


async def test_internal_route_creates_and_updates(client: httpx.AsyncClient, tmp_path):
    conversation_id, context_id, token, run = await _bind(client, tmp_path)
    try:
        create = {
            "context_id": context_id,
            "action": "create",
            "title": "实现进度卡",
            "items": [
                {"id": "t1", "text": "后端", "done": False},
                {"id": "t2", "text": "前端", "done": False},
            ],
        }
        response = await client.post(UPDATE_URL, json=create, headers=_auth(token))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        assert body["card"]["version"] == 1
        assert body["card"]["conversation_id"] == conversation_id
        assert [item["id"] for item in body["card"]["items"]] == ["t1", "t2"]

        update = {
            "context_id": context_id,
            "action": "update",
            "items": [{"id": "t1", "done": True}],
        }
        response = await client.post(UPDATE_URL, json=update, headers=_auth(token))
        assert response.status_code == 200, response.text
        assert response.json()["card"]["version"] == 2
        assert response.json()["card"]["items"][0]["done"] is True

        detail = (
            await client.get(f"/api/conversations/{conversation_id}")
        ).json()
        assert detail["task_card"]["version"] == 2
    finally:
        task_card_mcp.finish(context_id, "complete")
        registry.finish(run)


async def test_internal_route_rejects_bad_payloads(client: httpx.AsyncClient, tmp_path):
    conversation_id, context_id, token, run = await _bind(client, tmp_path)
    other_context, _root = task_card_mcp.begin("another-conversation", None)
    try:
        base = {
            "context_id": context_id,
            "action": "create",
            "title": "卡片",
            "items": [
                {"id": "t1", "text": "甲", "done": False},
                {"id": "t2", "text": "乙", "done": False},
            ],
        }
        # 字段白名单：出现 conversation_id 直接 422
        extra = {**base, "conversation_id": "somebody-else"}
        assert (
            await client.post(UPDATE_URL, json=extra, headers=_auth(token))
        ).status_code == 422

        # context 不匹配：409
        wrong_context = {**base, "context_id": other_context}
        assert (
            await client.post(UPDATE_URL, json=wrong_context, headers=_auth(token))
        ).status_code == 409

        # create 只有 1 项：服务端语义校验 422（schema 只管底线，不放宽服务端）
        too_few = {**base, "items": [{"id": "t1", "text": "甲", "done": False}]}
        assert (
            await client.post(UPDATE_URL, json=too_few, headers=_auth(token))
        ).status_code == 422

        # 没有卡就 update：409，且不写库
        update = {
            "context_id": context_id,
            "action": "update",
            "items": [{"id": "t1", "done": True}],
        }
        assert (
            await client.post(UPDATE_URL, json=update, headers=_auth(token))
        ).status_code == 409
    finally:
        task_card_mcp.finish(context_id, "complete")
        task_card_mcp.finish(other_context, "complete")
        registry.finish(run)


async def test_internal_route_rejects_finished_run_and_rate_limit(
    client: httpx.AsyncClient, tmp_path
):
    conversation_id, context_id, token, run = await _bind(client, tmp_path)
    payload = {
        "context_id": context_id,
        "action": "create",
        "title": "卡片",
        "items": [
            {"id": "t1", "text": "甲", "done": False},
            {"id": "t2", "text": "乙", "done": False},
        ],
    }
    try:
        registry.finish(run)
        assert (
            await client.post(UPDATE_URL, json=payload, headers=_auth(token))
        ).status_code == 409
    finally:
        task_card_mcp.finish(context_id, "complete")

    # 重新绑一轮测限流
    context_id, _root = task_card_mcp.begin(conversation_id, None, tmp_path)
    run2 = registry.register(conversation_id, "测试内核")
    task_card_mcp.bind(context_id, conversation_id, None, run2.id, None)
    token = task_card_mcp.read_token(context_id)
    binding = task_card_mcp.authenticate(f"Bearer {token}")
    assert binding is not None
    binding.calls = task_card_mcp.MAX_CALLS
    try:
        payload["context_id"] = context_id
        response = await client.post(UPDATE_URL, json=payload, headers=_auth(token))
        assert response.status_code == 429
    finally:
        task_card_mcp.finish(context_id, "complete")
        registry.finish(run2)


# --- 运行接线 -----------------------------------------------------------------


async def _make_mcp_conversation(client: httpx.AsyncClient, tmp_path: Path) -> str:
    gateway = await helpers.make_gateway(client)
    agent = await helpers.make_profile(
        client,
        gateway_id=gateway["id"],
        type_id="claude-code",
        options={},
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    response = await client.post(
        "/api/conversations",
        json={"agent_profile_id": agent["id"], "workspace_dir": str(workspace)},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _fake_skill_home(tmp_path: Path):
    async def fake(session, definition, profile, conversation, ctx, **_kwargs):
        ctx.home_dir = str(tmp_path / "home")
        return []

    return fake


async def test_prepare_run_mcp_kernel_binds_context_and_credentials(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(get_settings(), "task_card_mcp", True)
    conversation_id = await _make_mcp_conversation(client, tmp_path)
    monkeypatch.setattr(runtime_service, "prepare_skill_home", _fake_skill_home(tmp_path))
    payload = SimpleNamespace(
        content="帮我实现任务进度卡的后端与前端",
        attachments=None,
        workspace_dir=None,
        reset_session=False,
    )
    async with SessionLocal() as session:
        prepared = await runtime_service.prepare_run(session, conversation_id, payload)
    context_id = prepared.mcp_task_context_id
    try:
        assert context_id
        assert prepared.ctx.task_card_mcp is True
        assert "<task_card_context>" in prepared.ctx.prompt
        home = tmp_path / "home"
        assert (home / "mcp.json").is_file()
        config = json.loads((home / "mcp.json").read_text(encoding="utf-8"))
        assert "coda-task-card" in config["mcpServers"]
        assert config["mcpServers"]["coda-task-card"]["env"]["CODA_TASK_CARD_HOME"] == str(home)

        assert (home / task_card_mcp.credential_rel_path(context_id)).is_file()
        token = task_card_mcp.read_token(context_id)
        assert token
        binding = task_card_mcp.authenticate(f"Bearer {token}")
        assert binding is not None
        assert binding.conversation_id == conversation_id
        assert binding.run_id == prepared.run.id
        assert binding.anchor_message_id == prepared.assistant_message.id
    finally:
        if context_id:
            task_card_mcp.finish(context_id, "complete")
        registry.finish(prepared.run)


async def test_prepare_run_mcp_kernel_with_switch_off_keeps_fence(
    client: httpx.AsyncClient, tmp_path, monkeypatch
):
    """灰度开关关闭：MCP 内核也完全回到围栏通道（回滚安全）。"""
    # 显式关掉，避免用例受部署环境里 CODA_TASK_CARD_MCP 的影响。
    monkeypatch.setattr(get_settings(), "task_card_mcp", False)
    conversation_id = await _make_mcp_conversation(client, tmp_path)
    monkeypatch.setattr(runtime_service, "prepare_skill_home", _fake_skill_home(tmp_path))
    payload = SimpleNamespace(
        content="帮我实现任务进度卡的后端与前端",
        attachments=None,
        workspace_dir=None,
        reset_session=False,
    )
    async with SessionLocal() as session:
        prepared = await runtime_service.prepare_run(session, conversation_id, payload)
    try:
        assert prepared.mcp_task_context_id is None
        assert prepared.ctx.task_card_mcp is False
        assert "<task_card_context>" not in prepared.ctx.prompt
        assert "【任务进度卡片协议】" in prepared.ctx.prompt
        # 也没有内部 server 被写进配置
        config_path = tmp_path / "home" / "mcp.json"
        if config_path.is_file():
            config = json.loads(config_path.read_text(encoding="utf-8"))
            assert "coda-task-card" not in config.get("mcpServers", {})
    finally:
        registry.finish(prepared.run)


async def test_prepare_run_non_mcp_kernel_keeps_fence(client: httpx.AsyncClient, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "task_card_mcp", True)
    agent = await helpers.make_agent(client)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    conversation = await client.post(
        "/api/conversations",
        json={"agent_profile_id": agent["id"], "workspace_dir": str(workspace)},
    )
    conversation_id = conversation.json()["id"]
    monkeypatch.setattr(runtime_service, "prepare_skill_home", _fake_skill_home(tmp_path))
    payload = SimpleNamespace(
        content="帮我实现任务进度卡的后端与前端",
        attachments=None,
        workspace_dir=None,
        reset_session=False,
    )
    async with SessionLocal() as session:
        prepared = await runtime_service.prepare_run(session, conversation_id, payload)
    try:
        assert prepared.mcp_task_context_id is None
        assert prepared.ctx.task_card_mcp is False
        assert "<task_card_context>" not in prepared.ctx.prompt
        # 非 MCP 内核仍然走围栏协议
        assert "【任务进度卡片协议】" in prepared.ctx.prompt
    finally:
        registry.finish(prepared.run)


async def test_prepare_run_failure_reclaims_binding_and_credentials(
    client: httpx.AsyncClient, tmp_path, monkeypatch
):
    monkeypatch.setattr(get_settings(), "task_card_mcp", True)
    conversation_id = await _make_mcp_conversation(client, tmp_path)
    monkeypatch.setattr(runtime_service, "prepare_skill_home", _fake_skill_home(tmp_path))
    created: list[str] = []
    original_begin = task_card_mcp.begin

    def spy(*args, **kwargs):
        context_id, root = original_begin(*args, **kwargs)
        created.append(context_id)
        return context_id, root

    monkeypatch.setattr(task_card_mcp, "begin", spy)

    def boom(*_args, **_kwargs):
        raise RuntimeError("模拟运行注册失败")

    # 注册发生在凭据落盘之后：异常路径必须把凭据目录一起收掉。
    monkeypatch.setattr(runtime_service.registry, "register", boom)
    payload = SimpleNamespace(
        content="帮我实现任务进度卡的后端与前端",
        attachments=None,
        workspace_dir=None,
        reset_session=False,
    )
    async with SessionLocal() as session:
        with pytest.raises(RuntimeError, match="模拟运行注册失败"):
            await runtime_service.prepare_run(session, conversation_id, payload)
    assert created
    assert created[0] not in task_card_mcp._contexts
    assert not (tmp_path / "home" / "task-card").exists()


async def test_consume_run_ignores_fence_when_mcp_used(
    client: httpx.AsyncClient, tmp_path, monkeypatch
):
    """本轮已经通过 MCP 写过卡：正文围栏不重复落库，但协议块仍然从消息里剥掉。"""
    gateway = await helpers.make_gateway(client)
    fake = str(Path(__file__).parent / "fakes" / "fake_task_card_agent.py")
    options = {
        "command": sys.executable,
        "args": [fake],
        "prompt_mode": "stdin",
        "output_format": "jsonl",
        "text_path": "delta",
        "delta_mode": True,
        "session_path": "session_id",
    }
    agent = await helpers.make_profile(
        client, gateway_id=gateway["id"], type_id="custom-cli", options=options
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    conversation = await client.post(
        "/api/conversations",
        json={"agent_profile_id": agent["id"], "workspace_dir": str(workspace)},
    )
    conversation_id = conversation.json()["id"]
    monkeypatch.setattr(runtime_service, "prepare_skill_home", _fake_skill_home(tmp_path))
    payload = SimpleNamespace(
        content="帮我实现任务进度卡的后端与前端",
        attachments=None,
        workspace_dir=None,
        reset_session=False,
    )
    async with SessionLocal() as session:
        prepared = await runtime_service.prepare_run(session, conversation_id, payload)

    context_id, _root = task_card_mcp.begin(conversation_id, None)
    task_card_mcp.bind(
        context_id,
        conversation_id,
        None,
        prepared.run.id,
        prepared.assistant_message.id,
    )
    task_card_mcp.mark_used(context_id)
    prepared.mcp_task_context_id = context_id

    async with SessionLocal() as session:
        prepared.conversation = await session.get(Conversation, conversation_id)
        prepared.assistant_message = await session.get(
            Message, prepared.assistant_message.id
        )
        async for _event in runtime_service.consume_run(session, prepared):
            pass

    detail = (await client.get(f"/api/conversations/{conversation_id}")).json()
    # 围栏被忽略：卡片没有被重复创建
    assert detail["task_card"] is None
    # 但协议块仍然被剥离，正文里看不到 JSON
    assert "task_card" not in json.dumps(detail["messages"], ensure_ascii=False)
    # 轮末令牌已失效
    assert context_id not in task_card_mcp._contexts


async def test_consume_run_prefers_fence_over_kernel_block(
    client: httpx.AsyncClient, tmp_path, monkeypatch
):
    """围栏块合法时优先围栏；内核快照只在围栏没写 / 写坏时兜底。"""
    gateway = await helpers.make_gateway(client)
    fake = str(Path(__file__).parent / "fakes" / "fake_task_card_agent.py")
    options = {
        "command": sys.executable,
        "args": [fake],
        "prompt_mode": "stdin",
        "output_format": "jsonl",
        "text_path": "delta",
        "delta_mode": True,
        "session_path": "session_id",
    }
    agent = await helpers.make_profile(
        client, gateway_id=gateway["id"], type_id="custom-cli", options=options
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    conversation = await client.post(
        "/api/conversations",
        json={"agent_profile_id": agent["id"], "workspace_dir": str(workspace)},
    )
    conversation_id = conversation.json()["id"]
    monkeypatch.setattr(runtime_service, "prepare_skill_home", _fake_skill_home(tmp_path))
    payload = SimpleNamespace(
        content="帮我实现任务进度卡的后端与前端",
        attachments=None,
        workspace_dir=None,
        reset_session=False,
    )
    async with SessionLocal() as session:
        prepared = await runtime_service.prepare_run(session, conversation_id, payload)
    prepared.run.task_card_block = {
        "action": "create",
        "title": "内核任务",
        "items": [
            {"id": "k1", "text": "内核甲", "status": "in_progress"},
            {"id": "k2", "text": "内核乙", "status": "completed"},
        ],
    }
    async with SessionLocal() as session:
        prepared.conversation = await session.get(Conversation, conversation_id)
        prepared.assistant_message = await session.get(
            Message, prepared.assistant_message.id
        )
        async for _event in runtime_service.consume_run(session, prepared):
            pass

    detail = (await client.get(f"/api/conversations/{conversation_id}")).json()
    card = detail["task_card"]
    assert card is not None
    assert card["title"] == "实现进度卡"  # 围栏块赢了
    assert [item["id"] for item in card["items"]] == ["t1", "t2"]


async def test_consume_run_uses_kernel_block_as_fallback(
    client: httpx.AsyncClient, tmp_path, monkeypatch
):
    """围栏没写时，适配器归一的 Task* 快照作为兜底落库（source="kernel"）。"""
    agent = await helpers.make_agent(client)  # custom-cli + fake_jsonl_agent（只输出普通正文）
    workspace = tmp_path / "ws"
    workspace.mkdir()
    conversation = await client.post(
        "/api/conversations",
        json={"agent_profile_id": agent["id"], "workspace_dir": str(workspace)},
    )
    conversation_id = conversation.json()["id"]
    monkeypatch.setattr(runtime_service, "prepare_skill_home", _fake_skill_home(tmp_path))
    payload = SimpleNamespace(
        content="帮我实现任务进度卡的后端与前端",
        attachments=None,
        workspace_dir=None,
        reset_session=False,
    )
    async with SessionLocal() as session:
        prepared = await runtime_service.prepare_run(session, conversation_id, payload)
    prepared.run.task_card_block = {
        "action": "create",
        "title": "内核任务",
        "items": [
            {"id": "t1", "text": "甲", "status": "in_progress"},
            {"id": "t2", "text": "乙", "status": "completed"},
        ],
    }
    async with SessionLocal() as session:
        prepared.conversation = await session.get(Conversation, conversation_id)
        prepared.assistant_message = await session.get(
            Message, prepared.assistant_message.id
        )
        async for _event in runtime_service.consume_run(session, prepared):
            pass

    detail = (await client.get(f"/api/conversations/{conversation_id}")).json()
    card = detail["task_card"]
    assert card is not None
    assert card["title"] == "内核任务"
    by_id = {item["id"]: item for item in card["items"]}
    assert by_id["t1"]["status"] == "in_progress" and by_id["t1"]["done"] is False
    assert by_id["t2"]["status"] == "completed" and by_id["t2"]["done"] is True


# --- server：协议、凭据与错误 ---------------------------------------------------


def test_server_initialize_and_schema(server_module):
    reply = server_module.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert reply["result"]["serverInfo"]["name"] == "coda-task-card"
    assert reply["result"]["capabilities"] == {"tools": {}}

    reply = server_module.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tool = reply["result"]["tools"][0]
    assert tool["name"] == "update_task_card"
    schema = tool["inputSchema"]
    assert schema["required"] == ["context_id", "action", "items"]
    assert schema["additionalProperties"] is False
    # update 只报单项（只有 id + status）不能客户端本地校验拒掉
    item = schema["properties"]["items"]["items"]
    assert item["required"] == ["id"]
    assert "status" in item["properties"] and "done" in item["properties"]
    assert schema["properties"]["items"]["minItems"] == 1


def test_server_notifications_and_unknown_method(server_module):
    assert server_module.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    reply = server_module.handle_message({"jsonrpc": "2.0", "id": 9, "method": "resources/list"})
    assert reply["error"]["code"] == -32601


def test_server_credentials_lookup(server_module, tmp_path):
    context_id = "a" * 32
    target = tmp_path / task_card_mcp.credential_rel_path(context_id)
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps({"token": "tc_test-token", "context_id": context_id}),
        encoding="utf-8",
    )
    config = {"home": str(tmp_path), "api": "http://127.0.0.1:1/api", "timeout": 1.0}
    assert server_module.load_credentials(context_id, config) == "tc_test-token"

    with pytest.raises(server_module.ToolError, match="凭据不可用"):
        server_module.load_credentials("b" * 32, config)

    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(server_module.ToolError, match="无法读取"):
        server_module.load_credentials(context_id, config)

    target.write_text(
        json.dumps({"token": "tc_x", "context_id": "someone-else"}),
        encoding="utf-8",
    )
    with pytest.raises(server_module.ToolError, match="已失效"):
        server_module.load_credentials(context_id, config)


def test_server_call_update_payload_and_result(server_module, monkeypatch, tmp_path):
    context_id = "c" * 32
    target = tmp_path / task_card_mcp.credential_rel_path(context_id)
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps({"token": "tc_ok", "context_id": context_id}), encoding="utf-8"
    )
    captured: dict = {}

    def fake_post(url, payload, *, token, timeout):
        captured.update(url=url, payload=payload, token=token, timeout=timeout)
        return {"ok": True, "card": {"version": 2, "items": [{"id": "t1", "done": True}]}}

    monkeypatch.setattr(server_module, "_post_json", fake_post)
    config = {"home": str(tmp_path), "api": "http://x/api/internal/task-cards/update", "timeout": 3.0}
    text = server_module.call_update(
        {
            "context_id": context_id,
            "action": "update",
            "items": [{"id": "t1", "done": True}],
            # 服务端不认的字段不会进请求体
            "conversation_id": "somebody-else",
        },
        config,
    )
    assert captured["token"] == "tc_ok"
    assert captured["payload"] == {
        "context_id": context_id,
        "action": "update",
        "items": [{"id": "t1", "done": True}],
    }
    assert json.loads(text)["card"]["version"] == 2


def test_server_call_update_reports_tool_errors(server_module, monkeypatch, tmp_path):
    context_id = "d" * 32
    target = tmp_path / task_card_mcp.credential_rel_path(context_id)
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps({"token": "tc_ok", "context_id": context_id}), encoding="utf-8"
    )
    config = {"home": str(tmp_path), "api": "http://x/api/internal/task-cards/update", "timeout": 3.0}
    # handle_message 会自己读环境变量：这里指向有效地址与家目录，让错误来自 _post_json。
    monkeypatch.setenv(server_module.ENV_API, config["api"])
    monkeypatch.setenv(server_module.ENV_HOME, str(tmp_path))
    monkeypatch.setenv(server_module.ENV_TIMEOUT, "3")

    def boom(*args, **kwargs):
        raise server_module.ToolError("任务卡上下文已失效，请重新开始一轮")

    monkeypatch.setattr(server_module, "_post_json", boom)
    reply = server_module.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "update_task_card",
                "arguments": {"context_id": context_id, "action": "update", "items": []},
            },
        }
    )
    assert "error" not in reply
    assert reply["result"]["isError"] is True
    assert "已失效" in reply["result"]["content"][0]["text"]


class _FakeApi(BaseHTTPRequestHandler):
    """假的内部接口：按类属性回状态码，并记下 Authorization 与请求体。"""

    status = 200
    body: bytes = b'{"ok": true, "card": {"version": 1}}'
    seen: dict = {}

    def do_POST(self):  # noqa: N802 - http.server 的命名约定
        length = int(self.headers.get("Content-Length") or 0)
        _FakeApi.seen = {
            "auth": self.headers.get("Authorization"),
            "payload": json.loads(self.rfile.read(length) or b"{}"),
        }
        self.send_response(_FakeApi.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_FakeApi.body)))
        self.end_headers()
        self.wfile.write(_FakeApi.body)

    def log_message(self, *args):  # noqa: D401 - 静默测试输出
        return


@contextlib.contextmanager
def _fake_api():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeApi)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("status", "fragment"),
    [(401, "已失效"), (409, "进度未保存"), (429, "过于频繁"), (500, "HTTP 500")],
)
def test_server_post_maps_http_errors(server_module, status, fragment):
    _FakeApi.status = status
    _FakeApi.body = json.dumps({"detail": "上游说不行"}).encode()
    with _fake_api() as url:
        with pytest.raises(server_module.ToolError, match=fragment):
            server_module._post_json(
                f"{url}/api/internal/task-cards/update",
                {"context_id": "x"},
                token="tc_secret",
                timeout=3.0,
            )


def test_server_post_maps_network_errors(server_module, monkeypatch):
    def boom(*args, **kwargs):
        raise server_module.urllib.error.URLError("connection refused")

    monkeypatch.setattr(server_module.urllib.request, "urlopen", boom)
    with pytest.raises(server_module.ToolError, match="平台暂时不可达"):
        server_module._post_json("http://x", {}, token="tc_secret", timeout=1.0)


def test_stdio_server_subprocess_protocol():
    """真的起一次 server 进程：initialize + tools/list 必须走 stdout 的 JSON-RPC。"""
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    process = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input="\n".join(json.dumps(item) for item in messages) + "\n",
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert process.returncode == 0, process.stderr
    replies = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
    assert replies[0]["result"]["serverInfo"]["name"] == "coda-task-card"
    assert replies[1]["result"]["tools"][0]["name"] == "update_task_card"