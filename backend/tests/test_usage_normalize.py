"""用量归一化核心（P1）：协议优先、字段兜底、原始留痕与双轨预留。

fixture 与期望值的单一来源是 ``tests/fixtures/usage/manifest.json``：
- 真实样本（Claude / CodeBuddy）必须与期望逐字段一致；
- 未采到样本的协议标 ``pending``，只断言“被阻塞”，不猜结果；
- 任何改动先改 fixture / manifest，再改实现。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents import metering as m

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "usage"
MANIFEST = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
FIXTURES = MANIFEST["fixtures"]
EXPECTED = [entry for entry in FIXTURES if entry.get("expected")]
PENDING = [entry for entry in FIXTURES if not entry.get("expected")]

#: 已知“推理是输出子集”的协议；DeepSeek 样例 reasoning > output，不在其中。
REASONING_SUBSET_PROTOCOLS = {
    m.PROTOCOL_ANTHROPIC,
    m.PROTOCOL_OPENAI_CHAT,
    m.PROTOCOL_OPENAI_RESPONSES,
    m.PROTOCOL_OPENAI_COMPAT_CAMEL,
    m.PROTOCOL_GEMINI,
}


def _load(entry: dict) -> dict:
    raw = json.loads((FIXTURE_DIR / entry["file"]).read_text(encoding="utf-8"))
    extract = entry.get("extract")
    return raw[extract] if extract else raw


def _all_none(counts: dict) -> bool:
    return all(value is None for value in counts.values())


# ---------------------------------------------------------------------------
# fixture 主矩阵：T1–T13 / T21
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", EXPECTED, ids=[entry["file"] for entry in EXPECTED])
def test_fixture_matches_manifest(entry):
    """真实 / schema-derived fixture 的 7 个标准字段必须与 manifest 完全一致。"""
    got = m.normalize_usage(_load(entry), protocol=entry.get("protocol"))
    expected = entry["expected"]
    for field in m.STANDARD_FIELDS:
        assert got[field] == expected[field], f"{entry['file']} · {field}"


@pytest.mark.parametrize("entry", EXPECTED, ids=[entry["file"] for entry in EXPECTED])
def test_fixture_invariants(entry):
    """窗口/总量不变量；推理只在已知子集协议下断言 ≤ 输出。"""
    got = m.normalize_usage(_load(entry), protocol=entry.get("protocol"))
    context = got["context_tokens"]
    assert context == (
        got["input_tokens"] + got["cache_read_tokens"] + got["cache_creation_tokens"]
    ), entry["file"]
    assert got["total_tokens"] == context + got["output_tokens"], entry["file"]
    if entry.get("protocol") in REASONING_SUBSET_PROTOCOLS:
        reasoning = got["reasoning_tokens"]
        if reasoning is not None:
            assert reasoning <= got["output_tokens"], entry["file"]


@pytest.mark.parametrize("entry", PENDING, ids=[entry["file"] for entry in PENDING])
def test_pending_protocols_stay_blocked(entry):
    """未采到真实样本的协议必须保持阻塞：显式 unknown 不得被字段探测“救活”。"""
    got = m.normalize_usage(_load(entry), protocol=entry.get("protocol"))
    assert _all_none(got), f"{entry['file']} 不应在 pending 阶段产出数字"


# ---------------------------------------------------------------------------
# T14–T17：空值、布尔、冲突字段、兜底不得误判
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [None, {}, "", "nonsense", {"input_tokens": True}, {"input_tokens": None}],
    ids=["none", "empty", "string", "noise", "bool", "null-value"],
)
def test_no_usable_counts_returns_none_not_zero(raw):
    """没有可用计数时返回全 None（调用方记 unknown，不假装 0）。"""
    assert _all_none(m.normalize_usage(raw))


def test_message_start_all_zero_is_not_a_snapshot():
    """Claude 的 message_start 实测经常全 0：不能被当成“窗口占用 0”。"""
    raw = {
        "input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "output_tokens": 0,
    }
    assert _all_none(m.normalize_usage(raw, protocol=m.PROTOCOL_ANTHROPIC))


def test_conflicting_markers_prefer_prompt_tokens():
    """同时出现 prompt_tokens 与 cache_read_input_tokens 时，prompt 优先（OpenAI Chat）。"""
    raw = {
        "prompt_tokens": 1_000,
        "cached_tokens": 300,
        "cache_read_input_tokens": 50,
    }
    got = m.normalize_usage(raw)
    assert got["input_tokens"] == 700
    assert got["cache_read_tokens"] == 300
    assert got["context_tokens"] == 1_000


def test_fallback_never_treats_cache_write_as_exclusive_marker():
    """Codex 的 ``cache_write_input_tokens`` 只是数值别名，不能把缓存再加一遍。"""
    raw = {
        "input_tokens": 5,
        "cached_input_tokens": 1,
        "cache_write_input_tokens": 0,
        "output_tokens": 6,
        "reasoning_output_tokens": 2,
        "total_tokens": 11,
    }
    got = m.normalize_usage(raw)  # protocol=None → 兜底
    assert got["input_tokens"] == 4
    assert got["cache_read_tokens"] == 1
    assert got["context_tokens"] == 5
    assert got["total_tokens"] == 11


# ---------------------------------------------------------------------------
# T22–T23：协议优先，字段探测只兜底
# ---------------------------------------------------------------------------


def test_protocol_priority_beats_field_detection():
    """同一份 Codex 原始帧，显式给错协议也必须按调用方的协议算。"""
    raw = json.loads((FIXTURE_DIR / "codex_app_server_snake.json").read_text(encoding="utf-8"))
    correct = m.normalize_usage(raw, protocol=m.PROTOCOL_OPENAI_RESPONSES)
    wrong = m.normalize_usage(raw, protocol=m.PROTOCOL_ANTHROPIC)
    # 正确协议：缓存包含在输入里，普通输入 = 5 - 1。
    assert (correct["input_tokens"], correct["cache_read_tokens"]) == (4, 1)
    # 错误协议不会被字段名自动纠正：Anthropic 不认 cached_input_tokens。
    assert (wrong["input_tokens"], wrong["cache_read_tokens"]) == (5, 0)


@pytest.mark.parametrize(
    ("name", "raw", "expected"),
    [
        ("anthropic", {}, m.PROTOCOL_ANTHROPIC),
        ("chat", {}, m.PROTOCOL_OPENAI_CHAT),
        ("chat_completions", {}, m.PROTOCOL_OPENAI_CHAT),
        ("responses", {}, m.PROTOCOL_OPENAI_RESPONSES),
        ("deepseek", {}, m.PROTOCOL_DEEPSEEK),
        ("gemini", {}, m.PROTOCOL_GEMINI),
        ("openai", {"prompt_tokens": 1}, m.PROTOCOL_OPENAI_CHAT),
        ("openai", {"input_tokens": 1}, m.PROTOCOL_OPENAI_RESPONSES),
        ("unknown", {"prompt_tokens": 1}, None),
    ],
)
def test_protocol_aliases_and_hard_unknown(name, raw, expected):
    assert m.resolve_protocol(name, raw) == expected


def test_gateway_response_shapes_are_declared():
    assert set(m.RESPONSE_SHAPES) == {
        m.RESPONSE_SHAPE_ANTHROPIC,
        m.RESPONSE_SHAPE_CHAT,
        m.RESPONSE_SHAPE_RESPONSES,
        m.RESPONSE_SHAPE_GEMINI,
    }


# ---------------------------------------------------------------------------
# T18：禁止二次归一化
# ---------------------------------------------------------------------------


def test_already_normalized_does_not_subtract_cache_twice():
    normalized = {
        "input_tokens": 4,
        "output_tokens": 6,
        "cache_read_tokens": 1,
        "cache_creation_tokens": 0,
        "context_tokens": 5,
        "total_tokens": 11,
    }
    got = m.normalize_usage(normalized, already_normalized=True)
    assert got["input_tokens"] == 4
    assert got["cache_read_tokens"] == 1
    assert got["context_tokens"] == 5
    assert got["total_tokens"] == 11


def test_already_normalized_derives_missing_context_and_total():
    got = m.normalize_usage(
        {
            "input_tokens": 4,
            "output_tokens": 6,
            "cache_read_tokens": 1,
            "cache_creation_tokens": 0,
        },
        already_normalized=True,
    )
    assert got["context_tokens"] == 5
    assert got["total_tokens"] == 11


# ---------------------------------------------------------------------------
# T25：raw_usage 结构化截断
# ---------------------------------------------------------------------------


def test_truncate_raw_usage_keeps_small_payload_unchanged():
    raw = {"input_tokens": 1, "output_tokens": 2}
    assert m.truncate_raw_usage(raw) == raw


def test_truncate_raw_usage_writes_structured_placeholder():
    raw = {"input_tokens": 1, "output_tokens": 2, "noise": "x" * 20_000}
    out = m.truncate_raw_usage(raw)
    assert out["_truncated"] is True
    assert out["_original_bytes"] > m.RAW_USAGE_LIMIT_BYTES
    assert "noise" in out["_keys"]
    assert out["usage"]["input_tokens"] == 1
    # 占位对象自身必须是合法 JSON，不能被硬切字符串切坏。
    json.dumps(out, ensure_ascii=False)


def test_truncate_raw_usage_passthrough_already_truncated():
    raw = {"_truncated": True, "_original_bytes": 1, "_keys": [], "usage": {}}
    assert m.truncate_raw_usage(raw) == raw


@pytest.mark.parametrize("raw", [None, "nonsense", 123])
def test_truncate_raw_usage_rejects_non_mapping(raw):
    assert m.truncate_raw_usage(raw) == {}


# ---------------------------------------------------------------------------
# T26：推理计费开关
# ---------------------------------------------------------------------------


def test_reasoning_in_output_is_protocol_level_config():
    assert m.rules_for(m.PROTOCOL_ANTHROPIC).reasoning_in_output is True
    assert m.rules_for(m.PROTOCOL_OPENAI_CHAT).reasoning_in_output is True
    assert m.rules_for(m.PROTOCOL_OPENAI_RESPONSES).reasoning_in_output is True
    # DeepSeek 样例 reasoning > output：暂按独立处理，待真实样本确认。
    assert m.rules_for(m.PROTOCOL_DEEPSEEK).reasoning_in_output is False
    assert m.rules_for(None).reasoning_in_output is None
    assert m.rules_for("unknown").reasoning_in_output is None


# ---------------------------------------------------------------------------
# T27–T28：双轨遥测预留
# ---------------------------------------------------------------------------


def test_telemetry_sources_are_declared():
    assert m.TELEMETRY_SOURCES == (
        m.TELEMETRY_SOURCE_AGENT_REPORT,
        m.TELEMETRY_SOURCE_GATEWAY_PROXY,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" chatcmpl-abc ", "chatcmpl-abc"),
        ("req_123", "req_123"),
        ("x" * 200, "x" * m.UPSTREAM_REQUEST_ID_MAX_LEN),
        ("", None),
        ("   ", None),
        (None, None),
        (123, None),
    ],
)
def test_upstream_request_id_is_cleaned(value, expected):
    assert m.upstream_request_id(value) == expected


# ---------------------------------------------------------------------------
# 总量以派生为准，不被供应商 total 带偏
# ---------------------------------------------------------------------------


def test_provider_total_is_reference_only():
    raw = json.loads((FIXTURE_DIR / "deepseek_step_end.json").read_text(encoding="utf-8"))
    got = m.normalize_usage(raw, protocol=m.PROTOCOL_DEEPSEEK)
    assert raw["totalTokens"] == 7
    # 平台口径：context（含缓存 1+2）+ output 4 = 10，不跟着供应商的 7 走。
    assert got["total_tokens"] == 10