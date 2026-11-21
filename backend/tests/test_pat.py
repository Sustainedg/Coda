"""个人访问令牌（PAT）的端到端测试：签发 / 状态 / 吊销 / 改口令联动 / 跨用户隔离。

PAT 是给 MCP 服务器等程序化调用用的长效凭据（见 ``app/auth.py`` 的 PAT 段与
``api/auth.py`` 的 ``/me/pat`` 三个接口）。这里在「鉴权开启」形态下跑（同 ``test_auth.py``）。
"""
from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from sqlmodel import select

from app.auth import PAT_PREFIX, pat_hash
from app.db import SessionLocal, engine, init_db
from app.main import app
from app.models import McpServer, SQLModel, User
from app.security import decrypt, encrypt
from app.settings import get_settings


async def _builtin_admin_token(session, user_id: str) -> str:
    """取某个用户「系统管理」内置 MCP 配置里的明文令牌（测试用）。

    令牌只以密文落库，明文只在加密配置里 —— 这里解出来是为了验证「平台换没换」。
    """
    row = (
        await session.execute(
            select(McpServer).where(
                McpServer.owner_id == user_id,
                McpServer.builtin_key == "coda-admin",
            )
        )
    ).scalars().first()
    assert row is not None, "系统管理 MCP 没有播种"
    plain = decrypt((row.env_encrypted or {}).get("CODA_TOKEN"))
    assert plain, "系统管理 MCP 没拿到令牌"
    return plain


@pytest_asyncio.fixture
async def auth_client(monkeypatch):
    """鉴权开启的客户端：每个测试自带干净库。"""
    monkeypatch.setattr(get_settings(), "auth_enabled", True)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _register(client: httpx.AsyncClient, username: str, password: str = "password123") -> dict:
    resp = await client.post("/api/auth/register", json={"username": username, "password": password})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_pat_roundtrip(auth_client):
    """生成 → 状态可见 → PAT 能调 API → 吊销 → PAT 失效。"""
    session = await _register(auth_client, "alice")
    token = session["access_token"]

    # 还没生成过：状态是「没有」
    status = await auth_client.get("/api/auth/me/pat", headers=_auth(token))
    assert status.status_code == 200
    assert status.json()["has_pat"] is False

    # 生成：明文只出现这一次
    issued = await auth_client.post("/api/auth/me/pat", headers=_auth(token))
    assert issued.status_code == 200
    data = issued.json()
    pat = data["pat"]
    assert pat.startswith("pat_") and len(pat) > 60
    assert data["issued_at"]

    # 状态变为「有」，但永不回令牌本体
    status = await auth_client.get("/api/auth/me/pat", headers=_auth(token))
    assert status.json()["has_pat"] is True
    assert "pat" not in status.json()

    # PAT 能像 JWT 一样调受保护接口
    resp = await auth_client.get("/api/agents", headers=_auth(pat))
    assert resp.status_code == 200

    # 吊销后再用 → 401
    revoked = await auth_client.delete("/api/auth/me/pat", headers=_auth(token))
    assert revoked.status_code == 200
    resp = await auth_client.get("/api/agents", headers=_auth(pat))
    assert resp.status_code == 401


async def test_pat_regeneration_revokes_old(auth_client):
    """重新生成会立即吊销旧令牌（旧哈希被覆盖）。"""
    session = await _register(auth_client, "alice")
    token = session["access_token"]
    first = (await auth_client.post("/api/auth/me/pat", headers=_auth(token))).json()["pat"]
    second = (await auth_client.post("/api/auth/me/pat", headers=_auth(token))).json()["pat"]
    assert first != second
    # 旧的失效，新的可用
    assert (await auth_client.get("/api/agents", headers=_auth(first))).status_code == 401
    assert (await auth_client.get("/api/agents", headers=_auth(second))).status_code == 200


async def test_pat_revoked_on_password_change(auth_client):
    """改口令会一并吊销 PAT（凭据可能已泄露的保守处理）。"""
    session = await _register(auth_client, "alice")
    token = session["access_token"]
    pat = (await auth_client.post("/api/auth/me/pat", headers=_auth(token))).json()["pat"]

    resp = await auth_client.put(
        "/api/auth/me/password",
        json={"old_password": "password123", "new_password": "newpassword456"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    # 旧 PAT 失效
    assert (await auth_client.get("/api/agents", headers=_auth(pat))).status_code == 401
    # JWT 状态也清了（换新令牌登录）
    assert (await auth_client.get("/api/agents", headers=_auth(token))).status_code == 401


async def test_pat_isolated_per_user(auth_client):
    """A 的 PAT 拿不到 B 的资产（PAT 身份 = 属主身份，权限体系原样生效）。"""
    alice = await _register(auth_client, "alice")  # 首任管理员，资产属主
    bob = await _register(auth_client, "bob")
    bob_pat = (await auth_client.post("/api/auth/me/pat", headers=_auth(bob["access_token"]))).json()[
        "pat"
    ]
    # alice 建一个自定义网关；bob 的 PAT 列表里看不到它（非管理员列表只回自己的行）
    resp = await auth_client.post(
        "/api/gateways",
        json={
            "name": "alice 的网关",
            "protocol": "openai",
            "auth_style": "bearer",
            "base_url": "https://api.example.com",
            "api_key": "sk-test-not-a-real-key",
            "selected_models": ["gpt-4o"],
        },
        headers=_auth(alice["access_token"]),
    )
    assert resp.status_code == 201, resp.text
    listed = await auth_client.get("/api/gateways", headers=_auth(bob_pat))
    assert listed.status_code == 200
    items = [item for item in listed.json()["items"] if item["name"] == "alice 的网关"]
    assert items == []
    # 按按 id 读 alice 的网关也是 404（不说 403，避免泄露「存在但无权」）
    gateway_id = resp.json()["id"]
    resp = await auth_client.get(f"/api/gateways/{gateway_id}", headers=_auth(bob_pat))
    assert resp.status_code == 404


async def test_pat_requires_login(auth_client):
    """未登录不能生成 / 查看 / 吊销 PAT。"""
    assert (await auth_client.post("/api/auth/me/pat")).status_code == 401
    assert (await auth_client.get("/api/auth/me/pat")).status_code == 401
    assert (await auth_client.delete("/api/auth/me/pat")).status_code == 401


async def test_pat_hash_only_in_db(auth_client):
    """库里只存哈希：令牌本体绝不出现在任何响应里。"""
    session = await _register(auth_client, "alice")
    token = session["access_token"]
    pat = (await auth_client.post("/api/auth/me/pat", headers=_auth(token))).json()["pat"]

    async with SessionLocal() as db:
        row = await db.execute(select(User).where(User.username == "alice"))
        user = row.scalars().first()
        assert user.pat_hash == pat_hash(pat)
        assert user.pat_hash != pat
        assert pat.startswith(PAT_PREFIX)


async def test_pat_revoked_on_admin_password_reset(auth_client):
    """管理员重置口令同样吊销 PAT：用户 PAT 与内置 MCP 专用令牌都清空。

    PAT 不随 ``token_version`` 校验，重置口令只 +1 不会让它失效；这里确保应急入口
    也把长效凭据一并吊销。内置 MCP 的专用令牌**当场补发**一份新的（旧的那份立即失效），
    系统管理 MCP 不用等平台重启就能继续用。
    """
    admin = await _register(auth_client, "alice")
    bob = await _register(auth_client, "bob")
    pat = (await auth_client.post("/api/auth/me/pat", headers=_auth(bob["access_token"]))).json()[
        "pat"
    ]

    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "bob"))).scalars().first()
        assert user is not None and user.builtin_mcp_pat_hash
        builtin_digest = user.builtin_mcp_pat_hash
        builtin_token = await _builtin_admin_token(db, bob["user"]["id"])

    resp = await auth_client.post(
        f"/api/auth/users/{bob['user']['id']}/reset-password",
        json={"old_password": "-", "new_password": "newpassword456"},
        headers=_auth(admin["access_token"]),
    )
    assert resp.status_code == 200, resp.text

    # 旧 PAT 立即失效。
    assert (await auth_client.get("/api/agents", headers=_auth(pat))).status_code == 401
    # 旧的内置 MCP 专用令牌也立即失效（它的哈希被清空了）。
    assert (
        await auth_client.get("/api/auth/me", headers=_auth(builtin_token))
    ).status_code == 401
    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "bob"))).scalars().first()
        assert user is not None
        assert user.pat_hash is None
        # 专用令牌已换成新的一份（不是清空 —— 那是旧行为，要等重启才补上）。
        assert user.builtin_mcp_pat_hash is not None
        assert user.builtin_mcp_pat_hash != builtin_digest
        new_token = await _builtin_admin_token(db, bob["user"]["id"])
    # 新令牌立刻可用：系统管理 MCP 不用等平台重启。
    assert new_token != builtin_token
    assert (await auth_client.get("/api/auth/me", headers=_auth(new_token))).status_code == 200


async def test_builtin_mcp_token_refreshed_when_user_pat_rotates(auth_client):
    """用户重新生成 PAT 后，内置 MCP 里那份令牌自动更新（不必重启平台）。

    内置 MCP 的配置里可能存着用户自己的 PAT（平台允许手工填自己的令牌），
    用户一重新生成，那份就失效了 —— 平台必须立刻换一份新的写回去，
    否则系统管理 MCP 会一直 401，直到平台重启。
    """
    session = await _register(auth_client, "alice")
    token = session["access_token"]
    user_id = session["user"]["id"]
    pat = (await auth_client.post("/api/auth/me/pat", headers=_auth(token))).json()["pat"]

    # 模拟「配置里存的是用户自己的 PAT」：直接改库里的加密配置。
    async with SessionLocal() as db:
        row = (
            await db.execute(
                select(McpServer).where(
                    McpServer.owner_id == user_id,
                    McpServer.builtin_key == "coda-admin",
                )
            )
        ).scalars().first()
        assert row is not None
        row.env_encrypted = {
            **(row.env_encrypted or {}),
            "CODA_TOKEN": encrypt(pat) or "",
        }
        db.add(row)
        await db.commit()

    # 用户重新生成 PAT：旧的那份立刻失效。
    new_pat = (await auth_client.post("/api/auth/me/pat", headers=_auth(token))).json()["pat"]
    assert new_pat != pat
    assert (await auth_client.get("/api/auth/me", headers=_auth(pat))).status_code == 401

    # 内置 MCP 里的令牌被自动换掉了：不再是那份已失效的旧 PAT，且立刻可用。
    async with SessionLocal() as db:
        stored = await _builtin_admin_token(db, user_id)
    assert stored != pat
    assert (await auth_client.get("/api/auth/me", headers=_auth(stored))).status_code == 200


async def test_builtin_mcp_token_kept_when_still_valid(auth_client):
    """凭据变了但内置 MCP 那份令牌还有效时，不多此一举地轮换它。

    平台托管的令牌是独立的一份（与用户自己的 PAT 分开）：用户重新生成 PAT
    不该把系统管理 MCP 也一起掀了 —— 它还在有效期内，就让它继续用。
    """
    session = await _register(auth_client, "alice")
    token = session["access_token"]
    user_id = session["user"]["id"]

    async with SessionLocal() as db:
        before = await _builtin_admin_token(db, user_id)
    assert before

    await auth_client.post("/api/auth/me/pat", headers=_auth(token))

    async with SessionLocal() as db:
        after = await _builtin_admin_token(db, user_id)
    assert after == before
    assert (await auth_client.get("/api/auth/me", headers=_auth(after))).status_code == 200


async def test_builtin_mcp_token_reissued_on_edit_without_restart(auth_client):
    """删掉内置服务器的令牌值后保存：平台当场补发一份新的（不必重启平台）。

    以前这条路径只能靠「删掉令牌值 → 重启平台」恢复，中间系统管理 MCP 一直 401。
    """
    session = await _register(auth_client, "alice")
    token = session["access_token"]
    user_id = session["user"]["id"]

    async with SessionLocal() as db:
        before = await _builtin_admin_token(db, user_id)
        row = (
            await db.execute(
                select(McpServer).where(
                    McpServer.owner_id == user_id,
                    McpServer.builtin_key == "coda-admin",
                )
            )
        ).scalars().first()
        assert row is not None
        # 模拟「用户把令牌值删了」。
        raw_env = dict(row.env_encrypted or {})
        raw_env["CODA_TOKEN"] = encrypt("") or ""
        row.env_encrypted = raw_env
        db.add(row)
        await db.commit()
        mcp_id = row.id

    patched = await auth_client.patch(
        f"/api/mcp-servers/{mcp_id}", json={"env": {}}, headers=_auth(token)
    )
    assert patched.status_code == 200, patched.text

    async with SessionLocal() as db:
        after = await _builtin_admin_token(db, user_id)
    # 令牌被补发了：不是空值，也不是原来那份，且立刻可用。
    assert after
    assert after != before
    assert (await auth_client.get("/api/auth/me", headers=_auth(after))).status_code == 200
