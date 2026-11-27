"""多用户归属隔离：每个用户只能看到 / 引用自己的资产。

与 ``test_auth.py`` 的分工：那边验登录 / 注册 / 令牌 / 管理员权限，这里验「跨用户的
引用与统计不能串号」—— 尤其是网关、凭据、MCP、默认智能体与用量。
"""
from __future__ import annotations

import base64

import httpx
import pytest_asyncio
from sqlmodel import select

from app.db import SessionLocal, engine, init_db
from app.main import app
from app.models import (
    AgentMcpServer,
    Conversation,
    McpServer,
    Message,
    SQLModel,
    User,
    utcnow,
)
from app.auth import pat_hash
from app.settings import get_settings
from app.runtime import mcp as mcp_service


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setattr(get_settings(), "auth_enabled", True)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await init_db()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _register(c: httpx.AsyncClient, username: str) -> dict:
    resp = await c.post("/api/auth/register", json={"username": username, "password": "password123"})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _member_pair(c: httpx.AsyncClient):
    """一个管理员 + 两个普通成员（首任注册者自动是管理员）。"""
    await _register(c, "root")
    alice = await _register(c, "alice")
    bob = await _register(c, "bob")
    return alice, bob


async def _create_agent(c: httpx.AsyncClient, token: str, name: str, **extra) -> dict:
    payload = {"name": name, "type_id": "custom-cli", **extra}
    resp = await c.post("/api/agents", json=payload, headers=_auth(token))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_conversation(c: httpx.AsyncClient, token: str, agent_id: str | None = None) -> dict:
    payload = {"agent_profile_id": agent_id} if agent_id else {}
    resp = await c.post("/api/conversations", json=payload, headers=_auth(token))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_each_user_has_own_default_agent(client):
    alice, bob = await _member_pair(client)
    alice_agent = await _create_agent(client, alice["access_token"], "Alice 智能体")
    bob_agent = await _create_agent(client, bob["access_token"], "Bob 智能体")
    assert alice_agent["is_default"] is True
    assert bob_agent["is_default"] is True

    # 「不指定智能体」的新对话必须落到各自名下的默认智能体。
    alice_conv = await _create_conversation(client, alice["access_token"])
    bob_conv = await _create_conversation(client, bob["access_token"])
    assert alice_conv["agent_profile_id"] == alice_agent["id"]
    assert bob_conv["agent_profile_id"] == bob_agent["id"]


async def test_cannot_reference_other_users_gateway(client):
    alice, bob = await _member_pair(client)
    resp = await client.post(
        "/api/gateways",
        json={
            "name": "Alice 网关",
            "base_url": "https://gw.a/v1",
            "protocol": "openai",
            "auth_style": "bearer",
            "api_key": "sk-alice",
            "selected_models": ["m1"],
        },
        headers=_auth(alice["access_token"]),
    )
    assert resp.status_code == 201, resp.text
    gateway_id = resp.json()["id"]

    resp = await client.post(
        "/api/agents",
        json={"name": "偷网关", "type_id": "custom-cli", "gateway_id": gateway_id},
        headers=_auth(bob["access_token"]),
    )
    assert resp.status_code == 422, resp.text


async def test_cannot_reference_other_users_credential(client):
    alice, bob = await _member_pair(client)
    resp = await client.post(
        "/api/credentials",
        json={"name": "ALICE_TOKEN", "value": "secret-alice"},
        headers=_auth(alice["access_token"]),
    )
    assert resp.status_code == 201, resp.text
    credential_id = resp.json()["id"]

    resp = await client.post(
        "/api/agents",
        json={
            "name": "偷凭据",
            "type_id": "custom-cli",
            "credentials": [{"id": credential_id}],
        },
        headers=_auth(bob["access_token"]),
    )
    assert resp.status_code in (403, 422), resp.text


async def test_cannot_assign_other_users_platform_mcp(client):
    alice, bob = await _member_pair(client)
    resp = await client.post(
        "/api/mcp-servers",
        json={"name": "Alice MCP", "transport": "stdio", "command": "npx"},
        headers=_auth(alice["access_token"]),
    )
    assert resp.status_code == 201, resp.text
    mcp_id = resp.json()["id"]
    bob_agent = await _create_agent(client, bob["access_token"], "Bob 智能体")

    resp = await client.put(
        f"/api/agents/{bob_agent['id']}/mcp-servers",
        json={"mcp_ids": [mcp_id]},
        headers=_auth(bob["access_token"]),
    )
    assert resp.status_code == 422, resp.text


async def test_builtin_mcp_servers_are_scoped_to_their_owner(client):
    """内置服务器按用户各一份：列表只显示自己的 2 条，管理员也不看别人的副本。

    系统管理 MCP 的令牌属于配置它的用户；管理员若能把别人的那份挂到自己的智能体上，
    就会串用对方的 PAT。列表、详情与分配三处都必须按 ``owner_id`` 收敛。
    """
    root = await _register(client, "root")
    alice = await _register(client, "alice")
    bob = await _register(client, "bob")

    async def builtin_items(token: str) -> list[dict]:
        resp = await client.get("/api/mcp-servers", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        return [item for item in resp.json()["items"] if item["builtin_key"]]

    root_items = await builtin_items(root["access_token"])
    alice_items = await builtin_items(alice["access_token"])
    bob_items = await builtin_items(bob["access_token"])
    for items in (root_items, alice_items, bob_items):
        assert len(items) == 3
        assert {item["builtin_key"] for item in items} == {
            "image-gen",
            "coda-admin",
            "task-card",
        }

    root_ids = {item["id"] for item in root_items}
    alice_ids = {item["id"] for item in alice_items}
    bob_ids = {item["id"] for item in bob_items}
    assert root_ids.isdisjoint(alice_ids)
    assert root_ids.isdisjoint(bob_ids)
    assert alice_ids.isdisjoint(bob_ids)

    # 管理员也不能打开或分配别人的内置服务器（用可分配的图片生成，避开内部展示项）。
    bob_builtin = next(item for item in bob_items if item["builtin_key"] == "image-gen")
    assert (
        await client.get(
            f"/api/mcp-servers/{bob_builtin['id']}", headers=_auth(root["access_token"])
        )
    ).status_code == 404

    root_agent = await _create_agent(client, root["access_token"], "Root 智能体")
    denied = await client.put(
        f"/api/agents/{root_agent['id']}/mcp-servers",
        json={"mcp_ids": [bob_builtin["id"]]},
        headers=_auth(root["access_token"]),
    )
    assert denied.status_code == 422, denied.text

    root_assignable = next(
        item for item in root_items if item["builtin_key"] == "image-gen"
    )
    allowed = await client.put(
        f"/api/agents/{root_agent['id']}/mcp-servers",
        json={"mcp_ids": [root_assignable["id"]]},
        headers=_auth(root["access_token"]),
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["mcp_ids"] == [root_assignable["id"]]


async def test_admin_mcp_is_zero_config_and_uses_dedicated_token(client):
    """系统管理 MCP 开箱即用：地址与专用令牌自动填好，且不会轮换用户自己的 PAT。"""
    root = await _register(client, "root")
    alice = await _register(client, "alice")

    # 用户自己生成的 PAT 必须原样保留。
    own_pat = (
        await client.post("/api/auth/me/pat", headers=_auth(alice["access_token"]))
    ).json()["pat"]

    async def admin_item(token: str) -> dict:
        items = (await client.get("/api/mcp-servers", headers=_auth(token))).json()["items"]
        return next(item for item in items if item["builtin_key"] == "coda-admin")

    item = await admin_item(alice["access_token"])
    # 地址与令牌都自动填好：界面不再显示「待填参数」。
    assert item["env_preview"]["CODA_BASE_URL"]
    assert item["env_preview"]["CODA_TOKEN"]
    assert all(
        not param["required"] or param["managed"] or item["env_preview"].get(param["key"])
        for param in item["builtin_params"]
    )

    async with SessionLocal() as session:
        user = await session.get(User, alice["user"]["id"])
        assert user is not None and user.builtin_mcp_pat_hash
        # 自动令牌与用户自己的 PAT 是两份，不互相覆盖。
        assert user.pat_hash == pat_hash(own_pat)
        assert user.builtin_mcp_pat_hash != user.pat_hash

    # 自动令牌能直接调平台 API，身份就是这个用户。
    async with SessionLocal() as session:
        admin_mcp = next(
            row
            for row in (
                await session.execute(
                    select(McpServer).where(
                        McpServer.owner_id == alice["user"]["id"],
                        McpServer.builtin_key == "coda-admin",
                    )
                )
            ).scalars().all()
        )
        from app.security import decrypt

        auto_token = decrypt(admin_mcp.env_encrypted["CODA_TOKEN"])
    assert auto_token and auto_token.startswith("pat_")
    me = await client.get("/api/auth/me", headers=_auth(auto_token))
    assert me.status_code == 200 and me.json()["username"] == "alice"

    # 再次播种是幂等的：不会把已经可用的令牌轮换掉。
    async with SessionLocal() as session:
        await mcp_service.ensure_builtin_servers(session, alice["user"]["id"])
    assert (await client.get("/api/auth/me", headers=_auth(auto_token))).status_code == 200
    assert (await client.get("/api/auth/me", headers=_auth(own_pat))).status_code == 200

    # 管理员看自己的内置项同样自动填好，且与 alice 的不是同一份。
    root_item = await admin_item(root["access_token"])
    assert root_item["id"] != item["id"]
    assert root_item["env_preview"]["CODA_TOKEN"]


async def test_mcp_usage_count_is_owner_scoped(client):
    """共享 MCP 的「被几个智能体使用」按当前用户统计，不泄露别人的用量。"""
    alice, bob = await _member_pair(client)
    alice_agent = await _create_agent(client, alice["access_token"], "Alice 智能体")
    bob_agent = await _create_agent(client, bob["access_token"], "Bob 智能体")

    mcp_id = "shared-usage-test"
    async with SessionLocal() as session:
        session.add(
            McpServer(
                id=mcp_id,
                name="共享 MCP",
                slug="shared-usage-test",
                type_id="custom-cli",
                source_kind="user",
                transport="stdio",
                command="npx",
                available=True,
            )
        )
        await session.flush()
        session.add(AgentMcpServer(agent_profile_id=alice_agent["id"], mcp_server_id=mcp_id))
        session.add(AgentMcpServer(agent_profile_id=bob_agent["id"], mcp_server_id=mcp_id))
        await session.commit()

    async def used_by(token: str) -> int:
        items = (
            await client.get("/api/mcp-servers", headers=_auth(token))
        ).json()["items"]
        row = next(item for item in items if item["id"] == mcp_id)
        return row["used_by_agents"]

    assert await used_by(alice["access_token"]) == 1
    assert await used_by(bob["access_token"]) == 1


async def test_usage_only_counts_own_conversations(client):
    alice, bob = await _member_pair(client)
    alice_agent = await _create_agent(client, alice["access_token"], "Alice 智能体")
    bob_agent = await _create_agent(client, bob["access_token"], "Bob 智能体")
    alice_conv = await _create_conversation(client, alice["access_token"], alice_agent["id"])
    bob_conv = await _create_conversation(client, bob["access_token"], bob_agent["id"])

    async with SessionLocal() as session:
        for conv_id, tokens in ((alice_conv["id"], 100), (bob_conv["id"], 7)):
            session.add(
                Message(
                    conversation_id=conv_id,
                    role="assistant",
                    content="ok",
                    usage={"input_tokens": tokens},
                    created_at=utcnow(),
                )
            )
        await session.commit()

    alice_usage = (
        await client.get("/api/usage/overview", headers=_auth(alice["access_token"]))
    ).json()
    bob_usage = (
        await client.get("/api/usage/overview", headers=_auth(bob["access_token"]))
    ).json()
    assert alice_usage["tokens"]["input"] == 100
    assert bob_usage["tokens"]["input"] == 7
    assert {row["id"] for row in bob_usage["by_conversation"]} == {bob_conv["id"]}


async def test_admin_cannot_read_other_users_conversations(client):
    """对话内容是本人隐私：管理员也不能列 / 读 / 写别人的对话（列表、详情、消息、附件…）。"""
    root = await _register(client, "root")  # 首任注册者自动是管理员
    alice = await _register(client, "alice")
    alice_agent = await _create_agent(client, alice["access_token"], "Alice 智能体")
    alice_conv = await _create_conversation(client, alice["access_token"], alice_agent["id"])

    root_agent = await _create_agent(client, root["access_token"], "Root 智能体")
    root_conv = await _create_conversation(client, root["access_token"], root_agent["id"])
    root_headers = _auth(root["access_token"])

    items = (await client.get("/api/conversations", headers=root_headers)).json()["items"]
    assert {item["id"] for item in items} == {root_conv["id"]}, "管理员列表里不该出现别人的对话"

    for path in (
        f"/api/conversations/{alice_conv['id']}",
        f"/api/conversations/{alice_conv['id']}/context",
        f"/api/conversations/{alice_conv['id']}/trace",
        f"/api/conversations/{alice_conv['id']}/models",
        f"/api/conversations/{alice_conv['id']}/stream",
    ):
        resp = await client.get(path, headers=root_headers)
        assert resp.status_code == 404, f"{path} 不该对管理员开放：{resp.text}"

    # 写操作同样拒绝：发消息 / 传附件 / 重置记忆 / 删除
    assert (
        await client.post(
            f"/api/conversations/{alice_conv['id']}/messages",
            json={"content": "偷看"},
            headers=root_headers,
        )
    ).status_code == 404
    assert (
        await client.post(
            f"/api/conversations/{alice_conv['id']}/attachments",
            json={"name": "a.txt", "content_base64": "aGVsbG8="},
            headers=root_headers,
        )
    ).status_code == 404
    assert (
        await client.post(
            f"/api/conversations/{alice_conv['id']}/session/reset", headers=root_headers
        )
    ).status_code == 404
    assert (
        await client.delete(f"/api/conversations/{alice_conv['id']}", headers=root_headers)
    ).status_code == 404

    # 自己的对话照旧能开（不是把管理员整个禁掉）
    assert (
        await client.get(f"/api/conversations/{root_conv['id']}", headers=root_headers)
    ).status_code == 200


async def test_admin_usage_keeps_totals_but_not_other_conversation_titles(client):
    """管理员仍看平台总量，但「按对话」榜单不暴露别人的对话标题（标题常取自用户正文）。"""
    root = await _register(client, "root")
    alice = await _register(client, "alice")
    alice_agent = await _create_agent(client, alice["access_token"], "Alice 智能体")
    alice_conv = await _create_conversation(client, alice["access_token"], alice_agent["id"])

    async with SessionLocal() as session:
        session.add(
            Message(
                conversation_id=alice_conv["id"],
                role="assistant",
                content="ok",
                usage={"input_tokens": 42},
                created_at=utcnow(),
            )
        )
        await session.commit()

    usage = (
        await client.get("/api/usage/overview", headers=_auth(root["access_token"]))
    ).json()
    assert usage["tokens"]["input"] == 42, "平台总量对管理员仍应汇总"
    assert usage["by_conversation"] == [], "不该把别人的对话标题列给管理员"


async def test_file_url_accepts_token_query_for_img_tags(client):
    alice, _bob = await _member_pair(client)
    agent = await _create_agent(client, alice["access_token"], "Alice 智能体")
    conversation = await _create_conversation(client, alice["access_token"], agent["id"])

    raw = b"hello"
    resp = await client.post(
        f"/api/conversations/{conversation['id']}/attachments",
        json={"name": "a.txt", "content_base64": base64.b64encode(raw).decode()},
        headers=_auth(alice["access_token"]),
    )
    assert resp.status_code == 201, resp.text
    rel_path = resp.json()["rel_path"]

    # 没有令牌：401；带 Authorization 头：200；带 access_token 查询参数（<img> 只能这样）：200。
    path = f"/api/conversations/{conversation['id']}/files/{rel_path}"
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers=_auth(alice["access_token"]))).status_code == 200
    assert (
        await client.get(path, params={"access_token": alice["access_token"]})
    ).status_code == 200


async def test_auth_config_is_public(client):
    resp = await client.get("/api/auth/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["auth_enabled"] is True
    assert body["registration_open"] is True


async def test_cannot_create_team_with_other_users_agent(client):
    """协作图不能引用别人的智能体：否则运行时会用到对方的网关密钥 / 凭据。"""
    alice, bob = await _member_pair(client)
    alice_agent = await _create_agent(client, alice["access_token"], "Alice 智能体")

    resp = await client.post(
        "/api/collab/teams",
        json={
            "name": "越权协作组",
            "graph": {
                "nodes": [{"key": "boss", "agent_profile_id": alice_agent["id"]}],
                "edges": [],
            },
        },
        headers=_auth(bob["access_token"]),
    )
    assert resp.status_code == 422, resp.text

    # 引用自己的智能体仍然可以正常建组
    bob_agent = await _create_agent(client, bob["access_token"], "Bob 智能体")
    resp = await client.post(
        "/api/collab/teams",
        json={
            "name": "正常协作组",
            "graph": {
                "nodes": [{"key": "boss", "agent_profile_id": bob_agent["id"]}],
                "edges": [],
            },
        },
        headers=_auth(bob["access_token"]),
    )
    assert resp.status_code == 201, resp.text


async def test_im_connections_are_owner_scoped(client):
    """IM 连接按 owner_id 隔离：别人的连接在列表里看不到，改 / 删 / 重试 / 读聊天都是 404。"""
    from app.im.tables import ImConnection

    alice, bob = await _member_pair(client)
    alice_agent = await _create_agent(client, alice["access_token"], "Alice 智能体")

    # 绕过「先向平台核对」直接落库：本用例只验归属，不连任何平台。
    async with SessionLocal() as session:
        row = ImConnection(
            platform="telegram",
            agent_profile_id=alice_agent["id"],
            owner_id=alice["user"]["id"],
            fields={},
            secrets={"token": "fake"},
        )
        session.add(row)
        await session.commit()
        connection_id = row.id

    alice_list = (await client.get("/api/im/connections", headers=_auth(alice["access_token"]))).json()
    assert [item["id"] for item in alice_list["items"]] == [connection_id]
    bob_list = (await client.get("/api/im/connections", headers=_auth(bob["access_token"]))).json()
    assert bob_list["items"] == []

    # 不能把别人的连接绑到自己的智能体上
    bob_agent = await _create_agent(client, bob["access_token"], "Bob 智能体")
    for method, path, payload in (
        ("patch", f"/api/im/connections/{connection_id}", {"agent_profile_id": bob_agent["id"]}),
        ("delete", f"/api/im/connections/{connection_id}", None),
        ("post", f"/api/im/connections/{connection_id}/retry", None),
        ("get", f"/api/im/connections/{connection_id}/chats", None),
    ):
        kwargs = {"headers": _auth(bob["access_token"])}
        if payload is not None:
            kwargs["json"] = payload
        resp = await getattr(client, method)(path, **kwargs)
        assert resp.status_code == 404, f"{method} {path} 应当 404，实际 {resp.status_code}"

    # 配对请求的列表同样只回自己名下的连接
    assert (await client.get("/api/im/peers", headers=_auth(bob["access_token"]))).json()["items"] == []


async def test_backfill_covers_im_connections(client):
    """IM 连接也带 owner_id：鉴权关闭期间建的连接，打开鉴权后要能回填给管理员。

    ``backfill_ownership`` 漏掉某张表时，那些行会永远停在 ``owner_id IS NULL``，
    管理员在列表里看不到、也没法接管 —— 这是 AGENTS.md §9 矩阵明确要求的同步项。
    """
    from app.bootstrap import backfill_ownership
    from app.im.tables import ImConnection

    alice, _bob = await _member_pair(client)
    async with SessionLocal() as session:
        admin = await session.get(User, alice["user"]["id"])
        assert admin is not None
        # 模拟「鉴权关闭期间创建」的行：owner_id 为空
        session.add(ImConnection(platform="telegram", fields={}, secrets={"token": "x"}))
        await session.commit()

    settings = get_settings()
    marker = settings.data_dir / ".ownership_backfilled"
    had_marker = marker.exists()
    if had_marker:
        marker.unlink()
    try:
        async with SessionLocal() as session:
            admin = await session.get(User, alice["user"]["id"])
            assert admin is not None
            await backfill_ownership(session, admin)
        async with SessionLocal() as session:
            rows = (
                await session.execute(__import__("sqlmodel").select(ImConnection))
            ).scalars().all()
        assert rows and all(row.owner_id == alice["user"]["id"] for row in rows)
    finally:
        if not had_marker and marker.exists():
            marker.unlink()
