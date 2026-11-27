"""外部模型 API Key（P3-1）：签发只回一次明文、列表只给掩码、吊销即失效。"""
from __future__ import annotations

import httpx

from app.api_keys import API_KEY_PREFIX, find_active_key
from app.db import SessionLocal
from app.models import ApiKey, Gateway
from helpers import make_gateway


async def test_issue_key_returns_plaintext_once_and_lists_mask_only(client: httpx.AsyncClient):
    gateway = await make_gateway(client)
    response = await client.post(
        "/api/api-keys", json={"name": "脚本", "gateway_id": gateway["id"]}
    )
    assert response.status_code == 200, response.text
    data = response.json()
    raw = data["key"]
    assert raw.startswith(API_KEY_PREFIX)
    assert data["key_mask"].startswith(API_KEY_PREFIX)
    assert "key_hash" not in data

    listed = (await client.get("/api/api-keys")).json()["keys"]
    assert len(listed) == 1
    assert "key" not in listed[0] and "key_hash" not in listed[0]
    assert listed[0]["key_mask"] == data["key_mask"]

    # 库里只有哈希；按明文能查到。
    async with SessionLocal() as session:
        row = await find_active_key(session, raw)
        assert row is not None
        assert row.key_hash != raw
        assert len(row.key_hash) == 64


async def test_revoke_key_makes_it_unusable(client: httpx.AsyncClient):
    gateway = await make_gateway(client)
    created = (
        await client.post("/api/api-keys", json={"gateway_id": gateway["id"]})
    ).json()
    revoked = await client.delete(f"/api/api-keys/{created['id']}")
    assert revoked.status_code == 200, revoked.text
    async with SessionLocal() as session:
        assert await find_active_key(session, created["key"]) is None


async def test_official_gateway_cannot_back_an_api_key(client: httpx.AsyncClient):
    async with SessionLocal() as session:
        session.add(
            Gateway(
                id="official-gw", name="官方", kind="official", type_id="claude-code",
                protocol="anthropic", auth_style="x-api-key",
                base_url="https://api.anthropic.com",
            )
        )
        await session.commit()
    response = await client.post("/api/api-keys", json={"gateway_id": "official-gw"})
    assert response.status_code == 422


async def test_negative_quota_is_rejected(client: httpx.AsyncClient):
    gateway = await make_gateway(client)
    response = await client.post(
        "/api/api-keys",
        json={"gateway_id": gateway["id"], "daily_token_limit": -1},
    )
    assert response.status_code == 422


async def test_patch_updates_limits(client: httpx.AsyncClient):
    gateway = await make_gateway(client)
    created = (
        await client.post("/api/api-keys", json={"gateway_id": gateway["id"]})
    ).json()
    patched = await client.patch(
        f"/api/api-keys/{created['id']}",
        json={"daily_request_limit": 100, "name": "改名"},
    )
    assert patched.status_code == 200, patched.text
    data = patched.json()
    assert data["daily_request_limit"] == 100
    assert data["name"] == "改名"
    # 未提供的字段保持不变（月度限制仍为空）。
    assert data["monthly_token_limit"] is None
    async with SessionLocal() as session:
        row = await session.get(ApiKey, created["id"])
    assert row.daily_request_limit == 100