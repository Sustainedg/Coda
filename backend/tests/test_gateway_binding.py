"""记录网关绑定（P1-6）：direct / record 模式、启用条件与上下文注入。"""
from __future__ import annotations

import pytest

from app.agents import AgentContext, get_agent
from app.models import AgentProfile, Conversation, Gateway
from app.runtime import recording_gateway as gw
from app.runtime import service as runtime_service
from app.runtime.gateway import bind_gateway, bind_recording
from app.security import encrypt


@pytest.fixture(autouse=True)
def _clean_registry():
    gw.registry.clear()
    yield
    gw.registry.clear()


def _custom_gateway() -> Gateway:
    return Gateway(
        id="g1",
        name="测试网关",
        kind="custom",
        protocol="anthropic",
        auth_style="x-api-key",
        base_url="https://up.example/v1",
        api_key_encrypted=encrypt("REAL-KEY"),
    )


def _official_gateway() -> Gateway:
    return Gateway(
        id="g2", name="官方", kind="official", type_id="claude-code",
        protocol="anthropic", auth_style="x-api-key",
        base_url="https://api.anthropic.com",
    )


def _run() -> object:
    from app.runtime.manager import ActiveRun

    return ActiveRun(id="run-1", conversation_id="c1", agent_name="智能体")


def _conversation() -> Conversation:
    return Conversation(id="c1", agent_profile_id="p1", owner_id="u1")


def _profile() -> AgentProfile:
    return AgentProfile(id="p1", name="智能体", type_id="claude-code")


def test_bind_recording_registers_token_and_points_at_proxy():
    binding = bind_recording(
        get_agent("claude-code"), _custom_gateway(), run_id="run-1",
        owner_id="u1", conversation_id="c1", agent_type_id="claude-code",
        agent_profile_id="p1", model="sonnet", provider="anthropic",
    )
    assert binding.mode == "record"
    assert binding.url and "/api/internal/usage-gateway/" in binding.url
    assert binding.api_key == binding.proxy_token
    assert binding.upstream_url == "https://up.example/v1"
    target = gw.registry.resolve(binding.proxy_token)
    assert target is not None
    assert target.api_key == "REAL-KEY"  # 上游密钥留在服务端注册表
    assert "REAL-KEY" not in repr(target)  # 密钥不进 repr，避免日志泄露
    assert target.run_id == "run-1"


def test_bind_gateway_official_stays_direct():
    binding = bind_gateway(get_agent("claude-code"), _official_gateway())
    assert binding.mode == "direct"
    assert binding.api_key is None


def test_recording_binding_respects_off(monkeypatch):
    monkeypatch.setenv("CODA_RECORDING_GATEWAY", "off")
    assert runtime_service._recording_gateway_binding(
        get_agent("claude-code"), _custom_gateway(), _profile(), _conversation(),
        _run(), "sonnet",
    ) is None


def test_recording_binding_auto_requires_capability(monkeypatch):
    monkeypatch.setenv("CODA_RECORDING_GATEWAY", "auto")
    definition = get_agent("claude-code")
    monkeypatch.setattr(definition.capabilities, "recording_gateway", False)
    assert runtime_service._recording_gateway_binding(
        definition, _custom_gateway(), _profile(), _conversation(), _run(), "sonnet",
    ) is None


def test_recording_binding_auto_enables_for_declared_custom(monkeypatch):
    monkeypatch.setenv("CODA_RECORDING_GATEWAY", "auto")
    binding = runtime_service._recording_gateway_binding(
        get_agent("claude-code"), _custom_gateway(), _profile(), _conversation(),
        _run(), "sonnet",
    )
    assert binding is not None and binding.mode == "record"


def test_recording_binding_on_enables_for_custom(monkeypatch):
    monkeypatch.setenv("CODA_RECORDING_GATEWAY", "on")
    binding = runtime_service._recording_gateway_binding(
        get_agent("claude-code"), _custom_gateway(), _profile(), _conversation(),
        _run(), "sonnet",
    )
    assert binding is not None and binding.mode == "record"


def test_recording_binding_skips_official_and_missing_env(monkeypatch):
    monkeypatch.setenv("CODA_RECORDING_GATEWAY", "on")
    assert runtime_service._recording_gateway_binding(
        get_agent("claude-code"), _official_gateway(), _profile(), _conversation(),
        _run(), "sonnet",
    ) is None
    # CodeBuddy 没有 base_url_env 注入缝 → 不启用。
    binding = runtime_service._recording_gateway_binding(
        get_agent("codebuddy-cli"), _custom_gateway(), _profile(), _conversation(),
        _run(), "sonnet",
    )
    assert binding is None


def test_apply_recording_gateway_rewrites_context():
    binding = bind_recording(
        get_agent("claude-code"), _custom_gateway(), run_id="run-1",
        agent_type_id="claude-code", model="sonnet",
    )
    ctx = AgentContext(
        agent_id="p1", agent_name="智能体",
        gateway_url="https://up.example/v1", api_key="REAL-KEY", auth_style="x-api-key",
    )
    runtime_service._apply_recording_gateway(ctx, binding)
    assert ctx.gateway_url == binding.url
    assert ctx.api_key == binding.proxy_token
    assert ctx.auth_style == "x-api-key"  # 保留网关声明的鉴权方式