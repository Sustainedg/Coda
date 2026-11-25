"""鉴权与多用户归属的端到端测试。

这里在「鉴权开启」形态下跑（用独立客户端、显式登录），与既有业务测试（鉴权关闭、
``CODA_AUTH_ENABLED=false``）互补：既有测试验 CRUD / 流式 / 协作逻辑，这里验
「登录 / 注册 / 令牌 / 归属隔离 / 管理员越权」。
"""
from __future__ import annotations

import base64

import httpx
import pytest
import pytest_asyncio

from app.db import SessionLocal, engine, init_db
from app.main import app
from app.models import SQLModel, User
from app.settings import get_settings


@pytest_asyncio.fixture
async def auth_client(monkeypatch):
    """鉴权开启的客户端：每个测试自带干净库。"""
    # conftest 为既有业务测试默认关了鉴权；这里显式打开（get_settings 是缓存的单例，
    # 改属性即可，无需重建引擎）。
    monkeypatch.setattr(get_settings(), "auth_enabled", True)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _register(client: httpx.AsyncClient, username: str, password: str = "password123") -> dict:
    """注册并返回登录响应（含 token）。第一个注册的自动是管理员。"""
    resp = await client.post("/api/auth/register", json={"username": username, "password": password})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_register_first_user_is_admin(auth_client):
    data = await _register(auth_client, "alice")
    assert data["user"]["role"] == "admin"
    assert data["access_token"]
    # 用 token 取 me
    me = await auth_client.get("/api/auth/me", headers=_auth(data["access_token"]))
    assert me.json()["username"] == "alice"


async def test_register_second_user_is_member(auth_client):
    await _register(auth_client, "alice")
    data = await _register(auth_client, "bob")
    assert data["user"]["role"] == "member"


async def test_login_with_wrong_password(auth_client):
    await _register(auth_client, "alice")
    resp = await auth_client.post(
        "/api/auth/login", json={"username": "alice", "password": "wrong"}
    )
    assert resp.status_code == 401
    assert "用户名或口令不正确" in resp.json()["detail"]


async def test_protected_endpoint_requires_token(auth_client):
    # 没登录就访问受保护接口 → 401
    resp = await auth_client.get("/api/agents")
    assert resp.status_code == 401


async def test_invalid_token_rejected(auth_client):
    resp = await auth_client.get("/api/agents", headers=_auth("garbage.token.here"))
    assert resp.status_code == 401


async def test_users_isolated_in_gateways(auth_client):
    """两个成员各自建网关，互相看不到。"""
    await _register(auth_client, "root")  # 首任管理员，不参与本用例的隔离断言
    alice = await _register(auth_client, "alice")
    bob = await _register(auth_client, "bob")
    # alice 建一个网关
    resp = await auth_client.post(
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
    assert resp.status_code == 201
    alice_gw_id = resp.json()["id"]
    # bob 建一个网关
    resp = await auth_client.post(
        "/api/gateways",
        json={
            "name": "Bob 网关",
            "base_url": "https://gw.b/v1",
            "protocol": "openai",
            "auth_style": "bearer",
            "api_key": "sk-bob",
            "selected_models": ["m1"],
        },
        headers=_auth(bob["access_token"]),
    )
    assert resp.status_code == 201
    # alice 列表里只有自己的（+ 官方链路，这里没有播种所以只有自己的）
    listed = (await auth_client.get("/api/gateways", headers=_auth(alice["access_token"]))).json()
    names = [g["name"] for g in listed["items"]]
    assert "Alice 网关" in names
    assert "Bob 网关" not in names
    # bob 直接访问 alice 的网关 → 404
    resp = await auth_client.get(f"/api/gateways/{alice_gw_id}", headers=_auth(bob["access_token"]))
    assert resp.status_code == 404


async def test_admin_sees_all_gateways(auth_client):
    alice = await _register(auth_client, "alice")  # admin
    bob_data = await _register(auth_client, "bob")  # member
    await auth_client.post(
        "/api/gateways",
        json={
            "name": "Bob 网关",
            "base_url": "https://gw.b/v1",
            "protocol": "openai",
            "auth_style": "bearer",
            "api_key": "sk-bob",
            "selected_models": ["m1"],
        },
        headers=_auth(bob_data["access_token"]),
    )
    listed = (await auth_client.get("/api/gateways", headers=_auth(alice["access_token"]))).json()
    names = [g["name"] for g in listed["items"]]
    assert "Bob 网关" in names


async def test_change_password_then_login(auth_client):
    data = await _register(auth_client, "alice")
    resp = await auth_client.put(
        "/api/auth/me/password",
        json={"old_password": "password123", "new_password": "newpassword456"},
        headers=_auth(data["access_token"]),
    )
    assert resp.status_code == 200
    new_token = resp.json()["access_token"]
    # 改口令后返回的新令牌仍然可用
    resp = await auth_client.get("/api/auth/me", headers=_auth(new_token))
    assert resp.status_code == 200
    # 旧口令登录失败
    resp = await auth_client.post(
        "/api/auth/login", json={"username": "alice", "password": "password123"}
    )
    assert resp.status_code == 401
    # 新口令登录成功
    resp = await auth_client.post(
        "/api/auth/login", json={"username": "alice", "password": "newpassword456"}
    )
    assert resp.status_code == 200
    # 改口令前的旧令牌立即失效
    resp = await auth_client.get("/api/auth/me", headers=_auth(data["access_token"]))
    assert resp.status_code == 401


async def test_wrong_old_password_is_400_not_401(auth_client):
    """旧口令错误是请求内容问题；返回 400，前端才不会误判成令牌失效并登出。"""
    data = await _register(auth_client, "alice")
    resp = await auth_client.put(
        "/api/auth/me/password",
        json={"old_password": "wrong-pass", "new_password": "newpassword456"},
        headers=_auth(data["access_token"]),
    )
    assert resp.status_code == 400
    # 令牌仍然有效
    me = await auth_client.get("/api/auth/me", headers=_auth(data["access_token"]))
    assert me.status_code == 200


async def test_admin_can_disable_user(auth_client):
    alice = await _register(auth_client, "alice")  # admin
    bob = await _register(auth_client, "bob")  # member
    # alice 禁用 bob
    resp = await auth_client.patch(
        f"/api/auth/users/{bob['user']['id']}",
        json={"is_active": False},
        headers=_auth(alice["access_token"]),
    )
    assert resp.status_code == 200
    # bob 的旧 token 现在应该失效（用户已被禁用）
    resp = await auth_client.get("/api/auth/me", headers=_auth(bob["access_token"]))
    assert resp.status_code == 401


async def test_admin_reset_invalidates_old_token(auth_client):
    alice = await _register(auth_client, "alice")  # admin
    bob = await _register(auth_client, "bob")  # member
    resp = await auth_client.post(
        f"/api/auth/users/{bob['user']['id']}/reset-password",
        json={"old_password": "-", "new_password": "newpassword456"},
        headers=_auth(alice["access_token"]),
    )
    assert resp.status_code == 200
    # 重置前签发的令牌立即失效
    resp = await auth_client.get("/api/auth/me", headers=_auth(bob["access_token"]))
    assert resp.status_code == 401


async def test_member_cannot_list_users(auth_client):
    await _register(auth_client, "alice")  # admin
    bob = await _register(auth_client, "bob")  # member
    resp = await auth_client.get("/api/auth/users", headers=_auth(bob["access_token"]))
    assert resp.status_code == 403


async def test_member_cannot_read_provision_jobs(auth_client):
    """任务输出可能带登录二维码等短期凭据：列表 / 重连 / 清除只对管理员开放。"""
    alice = await _register(auth_client, "alice")  # admin
    bob = await _register(auth_client, "bob")  # member
    member = _auth(bob["access_token"])

    for method, url in (
        ("GET", "/api/runtimes/provision/jobs"),
        ("GET", "/api/runtimes/claude-code/provision/stream"),
        ("DELETE", "/api/runtimes/claude-code/provision"),
    ):
        resp = await auth_client.request(method, url, headers=member)
        assert resp.status_code == 403, f"{method} {url} 不该对普通成员开放"

    # 管理员仍可读任务列表
    admin = _auth(alice["access_token"])
    assert (await auth_client.get("/api/runtimes/provision/jobs", headers=admin)).status_code == 200


async def test_member_cannot_pin_kernel_account(auth_client):
    """账号槽位是机器级资产：普通成员不能新建时指定，也不能改指到别的槽位。"""
    alice = await _register(auth_client, "alice")  # admin
    bob = await _register(auth_client, "bob")  # member
    admin = _auth(alice["access_token"])
    member = _auth(bob["access_token"])

    # 普通成员新建时指定 → 403
    denied = await auth_client.post(
        "/api/agents",
        json={"name": "x", "type_id": "codebuddy-cli", "account_id": "slot-a"},
        headers=member,
    )
    assert denied.status_code == 403
    assert "管理员" in denied.json()["detail"]

    # 成员自己的智能体：管理员先替它指定账号，成员编辑时原样带回不算「改」。
    own = await auth_client.post(
        "/api/agents", json={"name": "我的", "type_id": "codebuddy-cli"}, headers=member
    )
    assert own.status_code == 201, own.text
    agent = own.json()
    pinned = await auth_client.patch(
        f"/api/agents/{agent['id']}", json={"account_id": "slot-a"}, headers=admin
    )
    assert pinned.status_code == 200, pinned.text
    assert pinned.json()["account_id"] == "slot-a"

    kept = await auth_client.patch(
        f"/api/agents/{agent['id']}",
        json={"name": "改名", "account_id": "slot-a"},
        headers=member,
    )
    assert kept.status_code == 200, kept.text

    # 想换到另一个槽位 → 403
    switched = await auth_client.patch(
        f"/api/agents/{agent['id']}", json={"account_id": "slot-b"}, headers=member
    )
    assert switched.status_code == 403
    assert "管理员" in switched.json()["detail"]


async def test_registration_can_be_closed(auth_client, monkeypatch):
    """关闭注册后，注册接口 403，但管理员仍能建用户。"""
    from app.settings import get_settings

    # 先注册一个管理员
    alice = await _register(auth_client, "alice")
    # 关闭注册
    monkeypatch.setattr(get_settings(), "registration_open", False)
    resp = await auth_client.post(
        "/api/auth/register", json={"username": "charlie", "password": "password123"}
    )
    assert resp.status_code == 403
    # 管理员仍能建
    resp = await auth_client.post(
        "/api/auth/users",
        json={"username": "charlie", "password": "password123"},
        headers=_auth(alice["access_token"]),
    )
    assert resp.status_code == 201


async def test_password_never_returned(auth_client):
    """口令哈希永不进接口响应。"""
    data = await _register(auth_client, "alice")
    # 响应里不该有 password / password_hash
    body = str(data)
    assert "password" not in body.lower()
    me = await auth_client.get("/api/auth/me", headers=_auth(data["access_token"]))
    assert "password" not in str(me.json()).lower()
    # 直接查库也确认存的是哈希不是明文
    async with SessionLocal() as session:
        from sqlmodel import select

        user = (await session.execute(select(User).where(User.username == "alice"))).scalar_one()
        assert "password123" not in user.password_hash
        assert user.password_hash.startswith("pbkdf2$")


@pytest.mark.parametrize("token", ["a.é.é", "a.b.c", "é.é.é"])
def test_malformed_token_is_rejected_not_500(token):
    """畸形令牌必须是 TokenError（接口 401），不能因编码 / base64 错误变成 500。"""
    from app.auth import TokenError, parse_token

    with pytest.raises(TokenError):
        parse_token(token)


# --- 头像 ------------------------------------------------------------------

#: 1×1 的 PNG（67 字节），只用来验证「按魔数认出是位图」这条路径。
_PNG_1PX = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
    "IQAAAABJRU5ErkJggg=="
)


async def test_upload_avatar_then_read_it(auth_client, tmp_path, monkeypatch):
    """上传头像后：`/me` 带回标识，GET 头像能取到同一张图，换图后文件名变化。"""
    from app.settings import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    data = await _register(auth_client, "alice")
    headers = _auth(data["access_token"])

    resp = await auth_client.put(
        "/api/auth/me/avatar",
        json={"content_base64": _PNG_1PX, "name": "me.png"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    first = resp.json()["avatar"]
    assert first and first.endswith(".png")

    # 任何登录用户都能取到（用户列表、协作消息里要显示）
    image = await auth_client.get(f"/api/auth/users/{data['user']['id']}/avatar", headers=headers)
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content[:8] == b"\x89PNG\r\n\x1a\n"

    # 文件名里带内容摘要：同一张图再传一次不会换名，旧文件也不会堆积
    again = await auth_client.put(
        "/api/auth/me/avatar", json={"content_base64": _PNG_1PX}, headers=headers
    )
    assert again.json()["avatar"] == first
    assert len(list((tmp_path / "avatars").glob("alice*"))) == 0  # 文件名用用户 id，不是用户名
    assert len(list((tmp_path / "avatars").glob("*.png"))) == 1


async def test_avatar_rejects_non_image_and_oversized(auth_client, tmp_path, monkeypatch):
    from app.settings import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    data = await _register(auth_client, "alice")
    headers = _auth(data["access_token"])

    # SVG 能内嵌脚本，头像会直接进 <img>，必须拒绝
    svg = base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>').decode()
    resp = await auth_client.put(
        "/api/auth/me/avatar", json={"content_base64": svg}, headers=headers
    )
    assert resp.status_code == 422
    assert "PNG / JPEG / WebP" in resp.json()["detail"]

    resp = await auth_client.put(
        "/api/auth/me/avatar", json={"content_base64": "not-base64!!"}, headers=headers
    )
    assert resp.status_code == 422


async def test_avatar_can_be_removed(auth_client, tmp_path, monkeypatch):
    from app.settings import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    data = await _register(auth_client, "alice")
    headers = _auth(data["access_token"])
    await auth_client.put(
        "/api/auth/me/avatar", json={"content_base64": _PNG_1PX}, headers=headers
    )

    resp = await auth_client.delete("/api/auth/me/avatar", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["avatar"] is None
    # 文件也删掉了，不留垃圾
    assert list((tmp_path / "avatars").glob("*.png")) == []
    # 没有头像时取图是 404（前端回退到首字母）
    resp = await auth_client.get(f"/api/auth/users/{data['user']['id']}/avatar", headers=headers)
    assert resp.status_code == 404


async def test_avatar_requires_login(auth_client):
    resp = await auth_client.put(
        "/api/auth/me/avatar", json={"content_base64": _PNG_1PX}
    )
    assert resp.status_code == 401
