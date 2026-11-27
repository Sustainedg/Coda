"""外部模型网关（P3-2 / P3-4）：API Key 鉴权、上游密钥注入、外部记账、配额与非推理请求。

也包括上游错误体的密钥擦除。
"""
from __future__ import annotations

import json

import httpx
from sqlmodel import select

from app.db import SessionLocal
from app.models import Gateway, UsageRecord
from app.runtime import recording_gateway as gw
from app.runtime import usage_ledger
from app.security import decrypt
from helpers import make_gateway

_ANTHROPIC_SSE = (
    'event: message_start\n'
    'data: {"type":"message_start","message":{"id":"msg_ext","model":"claude-x",'
    '"usage":{"input_tokens":10,"cache_read_input_tokens":5,"output_tokens":1}}}\n\n'
    'event: message_delta\n'
    'data: {"type":"message_delta","usage":{"output_tokens":7}}\n\n'
    'event: message_stop\n'
    'data: {"type":"message_stop"}\n\n'
)


async def _issue_key(client: httpx.AsyncClient, **limits) -> tuple[dict, dict]:
    gateway = await make_gateway(
        client, protocol="anthropic", auth_style="bearer",
        base_url="https://upstream.example/v1",
    )
    created = (
        await client.post(
            "/api/api-keys",
            json={"name": "外部脚本", "gateway_id": gateway["id"], **limits},
        )
    ).json()
    return gateway, created


def _mock_upstream(monkeypatch, *, content: str = _ANTHROPIC_SSE, content_type="text/event-stream"):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["x-api-key"] = request.headers.get("x-api-key")
        seen["body"] = request.read().decode()
        return httpx.Response(
            200, content=content.encode(), headers={"content-type": content_type}
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(gw, "make_http_client", lambda: httpx.AsyncClient(transport=transport))
    return seen


async def test_invalid_or_missing_key_is_rejected(client: httpx.AsyncClient):
    missing = await client.post("/api/model-gateway/v1/messages", content=b"{}")
    assert missing.status_code == 401
    bogus = await client.post(
        "/api/model-gateway/v1/messages",
        content=b"{}",
        headers={"authorization": "Bearer cosk_not-a-real-key"},
    )
    assert bogus.status_code == 401


async def test_external_request_forwards_and_records(client: httpx.AsyncClient, monkeypatch):
    """外部请求：注入上游密钥、剔除平台 key 查询参数、记 origin=external + api_key_id。"""
    _gateway, created = await _issue_key(client)
    assert created["key"].startswith("cosk_")
    seen = _mock_upstream(monkeypatch)

    response = await client.post(
        f"/api/model-gateway/v1/messages?api_key={created['key']}",
        content=b'{"model":"claude-x"}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 200, response.text
    assert "message_start" in response.text

    # 上游拿到的是网关密钥，不是平台 API Key；URL 不重复 /v1、不带 api_key。
    assert seen["authorization"].startswith("Bearer ")
    assert created["key"] not in (seen["authorization"] or "")
    assert seen["url"] == "https://upstream.example/v1/messages"
    assert "api_key=" not in seen["url"]

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(UsageRecord).where(UsageRecord.origin == usage_ledger.ORIGIN_EXTERNAL)
            )
        ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.source == usage_ledger.SOURCE_GATEWAY
    assert row.api_key_id == created["id"]
    assert row.request_id == "msg_ext"
    assert row.input_tokens == 10
    assert row.cache_read_tokens == 5
    assert row.output_tokens == 7
    assert row.context_tokens == 15
    assert row.run_id.startswith("ext:")


async def test_quota_exceeded_returns_429(client: httpx.AsyncClient, monkeypatch):
    _gateway, created = await _issue_key(client, daily_request_limit=1)
    seen = _mock_upstream(monkeypatch)
    # 先记一笔外部用量，把日请求额度用满。
    async with SessionLocal() as session:
        await usage_ledger.record(
            session,
            usage_ledger.build_record(
                run_id="ext:seed", request_id="seed",
                source=usage_ledger.SOURCE_GATEWAY,
                origin=usage_ledger.ORIGIN_EXTERNAL,
                api_key_id=created["id"],
                counts=usage_ledger.normalize_usage({"input_tokens": 1}),
            ),
        )
        await session.commit()

    response = await client.post(
        "/api/model-gateway/v1/messages",
        content=b"{}",
        headers={"authorization": f"Bearer {created['key']}"},
    )
    assert response.status_code == 429
    assert "上限" in response.json()["detail"]
    assert "url" not in seen  # 被拒的请求不应打到上游


async def test_non_inference_path_forwards_without_recording(client: httpx.AsyncClient, monkeypatch):
    """`/models` 这类元请求：即使配额已满也放行，且不写账本、不占配额。"""
    _gateway, created = await _issue_key(client, daily_request_limit=1)
    seen = _mock_upstream(
        monkeypatch, content='{"data":[]}', content_type="application/json"
    )
    async with SessionLocal() as session:
        await usage_ledger.record(
            session,
            usage_ledger.build_record(
                run_id="ext:seed2", request_id="seed2",
                source=usage_ledger.SOURCE_GATEWAY,
                origin=usage_ledger.ORIGIN_EXTERNAL,
                api_key_id=created["id"],
                counts=usage_ledger.normalize_usage({"input_tokens": 1}),
            ),
        )
        await session.commit()

    response = await client.post(
        "/api/model-gateway/v1/models",
        headers={"x-api-key": created["key"]},
    )
    assert response.status_code == 200
    assert seen["url"].endswith("/v1/models")
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(UsageRecord).where(UsageRecord.api_key_id == created["id"])
            )
        ).scalars().all()
    assert len(rows) == 1  # 只有种子那一笔，/models 没有新增

async def test_upstream_error_body_is_scrubbed(client: httpx.AsyncClient, monkeypatch):
    """上游在错误体里回显密钥时，转发前要擦掉（非流式错误）。"""
    gateway, created = await _issue_key(client)
    async with SessionLocal() as session:
        row = await session.get(Gateway, gateway["id"])
        upstream_key = decrypt(row.api_key_encrypted)
    assert upstream_key

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.dumps({"error": f"invalid key {upstream_key}"})
        return httpx.Response(
            401, content=body.encode(), headers={"content-type": "application/json"}
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(gw, "make_http_client", lambda: httpx.AsyncClient(transport=transport))
    response = await client.post(
        "/api/model-gateway/v1/messages",
        content=b"{}",
        headers={"authorization": f"Bearer {created['key']}"},
    )
    assert response.status_code == 401
    assert upstream_key not in response.text
    assert "***" in response.text
