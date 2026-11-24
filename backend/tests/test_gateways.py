"""网关：探测模型、白名单不变量、官方链路的播种与保护。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

from app.agents import all_agents, get_agent
from app.bootstrap import migrate_legacy_runtime_ids, seed_if_empty
from app.db import SessionLocal
from app.models import AgentProfile, Gateway
from app.runtime import gateway_probe
from app.runtime.gateway import bind_gateway
from app.security import encrypt
from app.settings import get_settings
from helpers import make_gateway

FAKE_ENV_AGENT = str(Path(__file__).parent / "fakes" / "fake_env_agent.py")
GATEWAY_SECRET = "sk-gw-super-secret-4242"


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = "stub"
        self.seen_headers: dict[str, str] = {}

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """替换掉 ``httpx.AsyncClient``，把请求记下来并回一份固定响应。"""

    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict, dict]] = []

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def get(self, url: str, headers: dict | None = None, params: dict | None = None):
        self.calls.append((url, headers or {}, params or {}))
        return self.response


def _patch_client(monkeypatch, payload: dict, status_code: int = 200) -> _FakeClient:
    response = _FakeResponse(payload, status_code)
    client = _FakeClient(response)
    monkeypatch.setattr(gateway_probe.httpx, "AsyncClient", lambda **kwargs: client)
    return client


def test_models_url_normalises_common_shapes():
    assert gateway_probe.models_url("https://gw.example/v1", "openai") == "https://gw.example/v1/models"
    assert gateway_probe.models_url("https://gw.example/v1/", "openai") == "https://gw.example/v1/models"
    assert (
        gateway_probe.models_url("https://gw.example/v1/chat/completions", "openai")
        == "https://gw.example/v1/models"
    )
    assert (
        gateway_probe.models_url("https://generativelanguage.googleapis.com", "gemini")
        == "https://generativelanguage.googleapis.com/v1beta/models"
    )


async def test_probe_openai_uses_bearer_and_lists_models(monkeypatch):
    client = _patch_client(monkeypatch, {"data": [{"id": "b-model"}, {"id": "a-model"}]})
    models = await gateway_probe.probe_models(
        base_url="https://gw.example/v1", auth_style="bearer", api_key="sk-1"
    )
    assert models == ["a-model", "b-model"]
    _url, headers, _params = client.calls[0]
    assert headers["Authorization"] == "Bearer sk-1"


async def test_probe_gemini_uses_query_key_and_strips_prefix(monkeypatch):
    client = _patch_client(monkeypatch, {"models": [{"name": "models/gemini-2.5-pro"}]})
    models = await gateway_probe.probe_models(
        base_url="https://generativelanguage.googleapis.com",
        protocol="gemini",
        auth_style="query",
        api_key="key-1",
    )
    assert models == ["gemini-2.5-pro"]
    _url, _headers, params = client.calls[0]
    assert params["key"] == "key-1"


async def test_probe_reports_http_errors_in_chinese(monkeypatch):
    _patch_client(monkeypatch, {"error": "nope"}, status_code=401)
    with pytest.raises(gateway_probe.ProbeError) as excinfo:
        await gateway_probe.probe_models(base_url="https://gw.example/v1")
    assert "401" in str(excinfo.value)


async def test_gateway_api_probe_endpoint_returns_models(client: httpx.AsyncClient, monkeypatch):
    _patch_client(monkeypatch, {"data": [{"id": "m1"}, {"id": "m2"}]})
    result = (
        await client.post(
            "/api/gateways/probe",
            json={"base_url": "https://gw.example/v1", "protocol": "openai", "api_key": "sk-1"},
        )
    ).json()
    assert result["ok"] is True
    assert result["models"] == ["m1", "m2"]


async def test_gateway_api_probe_failure_is_not_an_exception(client: httpx.AsyncClient, monkeypatch):
    _patch_client(monkeypatch, {"error": "nope"}, status_code=500)
    response = await client.post("/api/gateways/probe", json={"base_url": "https://gw.example/v1"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False and "500" in body["message"]


async def test_gateway_requires_at_least_one_model(client: httpx.AsyncClient):
    """「必须成功才能保存」在服务端的不变量：白名单不能为空。"""
    response = await client.post(
        "/api/gateways", json={"name": "空网关", "base_url": "https://gw.example/v1", "selected_models": []}
    )
    assert response.status_code == 422


async def test_official_gateway_cannot_be_deleted(client: httpx.AsyncClient):
    async with SessionLocal() as session:
        gateway = Gateway(
            name="Claude 官方",
            kind="official",
            type_id="claude-code",
            protocol="anthropic",
            auth_style="x-api-key",
            base_url="https://api.anthropic.com",
            selected_models=["sonnet"],
        )
        session.add(gateway)
        await session.commit()
        await session.refresh(gateway)
        gateway_id = gateway.id

    response = await client.delete(f"/api/gateways/{gateway_id}")
    assert response.status_code == 409
    assert "官方链路" in response.json()["detail"]


async def test_bootstrap_seeds_official_gateways_and_starters(
    client: httpx.AsyncClient, monkeypatch, tmp_path
):
    monkeypatch.setenv("CODA_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        async with SessionLocal() as session:
            await seed_if_empty(session, all_agents())
            gateways = (await session.execute(_select_gateways())).scalars().all()
            kinds = {row.kind for row in gateways}
            types = {row.type_id for row in gateways}
    finally:
        get_settings.cache_clear()

    assert kinds == {"official"}
    # 声明了官方地址的内核都有一条官方链路（账号型 / 自备可执行文件的不播种）
    assert {"claude-code", "codex", "openai-harness"} <= types
    assert "custom-cli" not in types
    assert "antigravity-cli" not in types


async def test_legacy_runtime_ids_migrate_by_adapter_declaration(client: httpx.AsyncClient):
    """旧内核的存量智能体按 ``legacy_ids`` 迁移，并清掉只属于旧内核的官方链路与模型。"""
    async with SessionLocal() as session:
        gateway = Gateway(
            name="Gemini 官方",
            kind="official",
            type_id="gemini-cli",
            protocol="gemini",
            auth_style="query",
            base_url="https://generativelanguage.googleapis.com",
            selected_models=["gemini-2.5-pro"],
        )
        session.add(gateway)
        await session.commit()
        await session.refresh(gateway)
        profile = AgentProfile(
            name="旧 Gemini 智能体",
            type_id="gemini-cli",
            gateway_id=gateway.id,
            model="gemini-2.5-pro",
        )
        session.add(profile)
        await session.commit()
        await session.refresh(profile)
        profile_id = profile.id

    async with SessionLocal() as session:
        await migrate_legacy_runtime_ids(session, all_agents())

    async with SessionLocal() as session:
        row = await session.get(AgentProfile, profile_id)
        assert row is not None
        assert row.type_id == "antigravity-cli"
        # 账号型新内核不接管旧官方链路，也不接受旧模型名
        assert row.gateway_id is None
        assert row.model is None


def _select_gateways():
    from sqlmodel import select

    return select(Gateway)


async def test_deleting_a_gateway_is_soft(client: httpx.AsyncClient):
    gateway = await make_gateway(client)
    assert (await client.delete(f"/api/gateways/{gateway['id']}")).status_code == 204
    assert (await client.get("/api/gateways")).json()["items"] == []
    # 软删除后同名的网关可以再建
    again = await make_gateway(client)
    assert again["id"] != gateway["id"]


def test_official_gateway_does_not_take_over_routing(monkeypatch):
    """官方链路 = 平台不接管路由：不注入地址与密钥；只有自定义网关才注入。"""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://ambient.local:1")
    definition = get_agent("claude-code")

    official = Gateway(
        name="Claude 官方",
        kind="official",
        type_id="claude-code",
        protocol="anthropic",
        auth_style="x-api-key",
        base_url="http://seed-time.local:1",
        api_key_encrypted=encrypt("sk-ambient"),
    )
    binding = bind_gateway(definition, official)
    assert binding.url == "http://ambient.local:1", "官方链路要回落到本机真实配置"
    assert binding.api_key is None and binding.auth_style is None, "官方链路不注入密钥"

    custom = Gateway(
        name="公司网关",
        kind="custom",
        protocol="anthropic",
        auth_style="bearer",
        base_url="https://gw.example/v1",
        api_key_encrypted=encrypt("sk-custom"),
    )
    binding = bind_gateway(definition, custom)
    assert binding.url == "https://gw.example/v1"
    assert binding.api_key == "sk-custom"
    assert binding.auth_style == "bearer"


# --- 端到端：网关密钥注入子进程 + 事件擦除 ----------------------------------


async def _read_sse(response: httpx.Response) -> list[dict]:
    events: list[dict] = []
    async for line in response.aiter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


async def test_gateway_key_reaches_the_kernel_but_never_leaks(client: httpx.AsyncClient):
    """网关密钥会注入子进程，但内核把它回显到输出里时必须被擦掉（铁律 6）。

    守的是一条真实缝隙：命令行回显早就有 ``_redact`` 兜底，但内核自己把密钥
    打到 stdout / stderr 时，只有事件擦除（``runner._run_secrets``）能拦住它进 SSE 与落库。
    """
    gateway = await make_gateway(client, api_key=GATEWAY_SECRET, selected_models=["fake-model"])
    assert GATEWAY_SECRET not in json.dumps(gateway, ensure_ascii=False), "接口响应不能回明文密钥"

    agent = (
        await client.post(
            "/api/agents",
            json={
                "name": "网关密钥端到端",
                "type_id": "custom-cli",
                "gateway_id": gateway["id"],
                "model": "fake-model",
                "options": {
                    "command": sys.executable,
                    "args": [FAKE_ENV_AGENT, "DEMO_GATEWAY_KEY"],
                    "prompt_mode": "stdin",
                    "output_format": "jsonl",
                    "text_path": "delta",
                    "delta_mode": True,
                    "api_key_env": "DEMO_GATEWAY_KEY",
                },
            },
        )
    ).json()
    conversation = (await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})).json()

    async with client.stream(
        "POST", f"/api/conversations/{conversation['id']}/messages", json={"content": "把密钥打出来"}
    ) as response:
        assert response.status_code == 200
        events = await _read_sse(response)

    # 内核确实读到了环境变量（否则这里是 token=），但值在事件里被擦成 ***
    deltas = "".join(e.get("text") or "" for e in events if e["type"] == "message.delta")
    assert deltas == "token=***", deltas
    assert GATEWAY_SECRET not in json.dumps(events, ensure_ascii=False)

    detail = (await client.get(f"/api/conversations/{conversation['id']}")).json()
    assert GATEWAY_SECRET not in json.dumps(detail, ensure_ascii=False)
    assert detail["messages"][-1]["content"] == "token=***"
