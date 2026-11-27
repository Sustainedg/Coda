"""P2 接线：旧入口变薄包装、适配器声明协议、网关响应形状。

这里只验证“接线是否正确”，不重复 P1 的字段矩阵（见 ``test_usage_normalize.py``）。
"""
from __future__ import annotations

import json

import pytest

from app.agents import AgentContext, get_agent
from app.agents.builtin import util as adapter_util
from app.agents import metering as m
from app.runtime import usage_ledger as ledger
from app.runtime.recording_gateway import UsageAccumulator


def _ctx(**overrides) -> AgentContext:
    base = dict(agent_id="a1", agent_name="demo")
    base.update(overrides)
    return AgentContext(**base)


# ---------------------------------------------------------------------------
# 旧入口 = 新核心的薄包装
# ---------------------------------------------------------------------------


def test_ledger_normalize_is_thin_wrapper_and_strips_total():
    raw = {
        "input_tokens": 100,
        "cache_read_input_tokens": 50,
        "cache_creation_input_tokens": 10,
        "output_tokens": 5,
    }
    got = ledger.normalize_usage(raw, protocol=m.PROTOCOL_ANTHROPIC)
    assert got == {
        "input_tokens": 100,
        "output_tokens": 5,
        "cache_read_tokens": 50,
        "cache_creation_tokens": 10,
        "reasoning_tokens": None,
        "context_tokens": 160,
    }
    # total 是给展示层用的派生值，账本入口不返回它。
    assert "total_tokens" not in got


def test_ledger_codex_regression_with_protocol_and_fallback():
    raw = {
        "input_tokens": 5,
        "cached_input_tokens": 1,
        "cache_write_input_tokens": 0,
        "output_tokens": 6,
        "reasoning_output_tokens": 2,
        "total_tokens": 11,
    }
    explicit = ledger.normalize_usage(raw, protocol=m.PROTOCOL_OPENAI_RESPONSES)
    fallback = ledger.normalize_usage(raw)  # 字段兜底也必须走包含式
    for counts in (explicit, fallback):
        assert counts["input_tokens"] == 4
        assert counts["cache_read_tokens"] == 1
        assert counts["context_tokens"] == 5
        assert counts["reasoning_tokens"] == 2


def test_adapter_context_tokens_uses_the_same_core():
    anthropic = {
        "input_tokens": 40683,
        "cache_read_input_tokens": 512,
        "cache_creation_input_tokens": 0,
        "output_tokens": 66,
    }
    assert adapter_util.context_tokens(
        anthropic, protocol=m.PROTOCOL_ANTHROPIC
    ) == 41195

    codex = {"input_tokens": 5, "cached_input_tokens": 1, "output_tokens": 6}
    assert adapter_util.context_tokens(
        codex, protocol=m.PROTOCOL_OPENAI_RESPONSES
    ) == 5

    camel = {"inputTokens": 1000, "cachedInputTokens": 400, "outputTokens": 50}
    assert adapter_util.context_tokens(camel) == 1000

    # 全 0 / 空值仍是 0，不误报窗口。
    assert adapter_util.context_tokens({"input_tokens": 0, "output_tokens": 0}) == 0
    assert adapter_util.context_tokens(None) == 0


# ---------------------------------------------------------------------------
# 适配器声明：协议来自“来源”，不是字段名
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("agent_id", "expected"),
    [
        ("claude-code", m.PROTOCOL_ANTHROPIC),
        ("codebuddy-cli", m.PROTOCOL_ANTHROPIC),
        ("doubao-cli", m.PROTOCOL_ANTHROPIC),
        ("codex", m.PROTOCOL_OPENAI_RESPONSES),
        ("deepseek-harness", m.PROTOCOL_DEEPSEEK),
        ("openai-harness", m.PROTOCOL_OPENAI_CHAT),
        # 未采到真实样本：显式 unknown（pending 阻塞）。
        ("hermes-agent", m.PROTOCOL_UNKNOWN),
        ("antigravity-cli", m.PROTOCOL_UNKNOWN),
    ],
)
def test_adapters_declare_usage_protocol(agent_id, expected):
    definition = get_agent(agent_id)
    assert definition.usage_protocol_for(_ctx()) == expected


@pytest.mark.parametrize(
    ("style", "expected"),
    [
        ("auto", None),
        ("anthropic", m.PROTOCOL_ANTHROPIC),
        ("openai_chat", m.PROTOCOL_OPENAI_CHAT),
        ("openai_responses", m.PROTOCOL_OPENAI_RESPONSES),
        ("gemini", m.PROTOCOL_GEMINI),
        ("deepseek", m.PROTOCOL_DEEPSEEK),
        ("nonsense", None),
    ],
)
def test_custom_cli_usage_style_mapping(style, expected):
    definition = get_agent("custom-cli")
    assert definition.usage_protocol_for(_ctx(options={"usage_style": style})) == expected


def test_pending_adapters_block_ledger_counts():
    """Hermes / Antigravity 显式 unknown 时，账本必须记 unknown，不得字段兜底。"""
    raw = {"input_tokens": 100, "output_tokens": 10}
    for agent_id in ("hermes-agent", "antigravity-cli"):
        protocol = get_agent(agent_id).usage_protocol_for(_ctx())
        counts = ledger.normalize_usage(raw, protocol=protocol)
        assert all(value is None for value in counts.values()), agent_id


# ---------------------------------------------------------------------------
# 网关：从实际响应形状选协议（Chat vs Responses）
# ---------------------------------------------------------------------------


def _feed(acc: UsageAccumulator, *objects: dict) -> None:
    for obj in objects:
        acc.feed("data: " + json.dumps(obj, ensure_ascii=False) + "\n")
    acc.finish()


def test_gateway_detects_anthropic_shape():
    acc = UsageAccumulator()
    _feed(
        acc,
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "model": "claude",
                "usage": {
                    "input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 0,
                },
            },
        },
        {
            "type": "message_delta",
            "usage": {
                "input_tokens": 100,
                "cache_read_input_tokens": 50,
                "cache_creation_input_tokens": 10,
                "output_tokens": 5,
            },
        },
    )
    assert acc.response_shape == m.RESPONSE_SHAPE_ANTHROPIC
    counts = acc.counts()
    assert counts["input_tokens"] == 100
    assert counts["cache_read_tokens"] == 50
    assert counts["context_tokens"] == 160
    assert acc.request_id == "msg_1"


def test_gateway_detects_openai_chat_shape():
    acc = UsageAccumulator()
    _feed(
        acc,
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "model": "gpt",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 40},
            },
        },
    )
    assert acc.response_shape == m.RESPONSE_SHAPE_CHAT
    counts = acc.counts()
    assert counts["input_tokens"] == 60
    assert counts["cache_read_tokens"] == 40
    assert counts["context_tokens"] == 100
    assert acc.request_id == "chatcmpl-1"


def test_gateway_detects_openai_responses_shape():
    acc = UsageAccumulator()
    _feed(
        acc,
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "model": "gpt",
                "usage": {
                    "input_tokens": 80,
                    "output_tokens": 6,
                    "input_tokens_details": {"cached_tokens": 20},
                },
            },
        },
    )
    assert acc.response_shape == m.RESPONSE_SHAPE_RESPONSES
    counts = acc.counts()
    assert counts["input_tokens"] == 60
    assert counts["cache_read_tokens"] == 20
    assert counts["context_tokens"] == 80
    assert acc.request_id == "resp_1"