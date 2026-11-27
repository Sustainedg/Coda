"""协议优先的 token 用量归一化（唯一实现）。

**为什么需要它**：各模型厂商 / 中转层的小票写法不同，最核心的分歧是
「缓存 token 是否已经包含在输入里」：

* **分离派**（Anthropic / Claude Code）：``input_tokens`` 不含缓存，
  ``cache_read_input_tokens`` / ``cache_creation_input_tokens`` 单列；
  窗口占用 = 三者相加。
* **包含派**（OpenAI Chat / Responses、Gemini）：``prompt_tokens`` /
  ``input_tokens`` / ``promptTokenCount`` 已含缓存；窗口占用 = 输入侧总量，
  普通输入 = 输入侧总量 - 缓存读。

本模块只做一件事：把任意协议的小票翻成固定的 7 个标准字段。
**协议由调用方传进来（协议优先）；只有 ``protocol=None`` 时才按字段名兜底。**
字段名探测永远不负责判断「包含还是分离」——那是协议的知识。

标准字段：

``input_tokens``
    普通输入（不含缓存）。
``output_tokens``
    输出。
``cache_read_tokens``
    缓存读。
``cache_creation_tokens``
    缓存写。
``reasoning_tokens``
    推理 / 思考 token。**只做明细，不重复计入总量**；是否属于输出子集由
    :func:`rules_for` 的 ``reasoning_in_output`` 声明。
``context_tokens``
    窗口占用（输入侧含缓存）。
``total_tokens``
    派生展示值 = ``context_tokens + output_tokens``。
    供应商自己给的 ``total_tokens`` **不参与判定**，只作为一致性校验的参考。

没有可用计数时返回全 ``None``（调用方记 ``unknown``，不假装是 0）。
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# 协议
# ---------------------------------------------------------------------------

PROTOCOL_ANTHROPIC = "anthropic_messages"
PROTOCOL_OPENAI_CHAT = "openai_chat"
PROTOCOL_OPENAI_RESPONSES = "openai_responses"
PROTOCOL_OPENAI_COMPAT_CAMEL = "openai_compat_camel"
PROTOCOL_GEMINI = "gemini"
PROTOCOL_DEEPSEEK = "deepseek_camel"
#: 哨兵：协议未知 / 未采到真实样本。显式传入时**硬阻塞**，不得被字段探测“救活”。
PROTOCOL_UNKNOWN = "unknown"

PROTOCOLS = (
    PROTOCOL_ANTHROPIC,
    PROTOCOL_OPENAI_CHAT,
    PROTOCOL_OPENAI_RESPONSES,
    PROTOCOL_OPENAI_COMPAT_CAMEL,
    PROTOCOL_GEMINI,
    PROTOCOL_DEEPSEEK,
)

#: 旧调用点 / 网关配置里的粗粒度名字 → 本模块协议。
#: ``openai`` 不直接映射：Chat 与 Responses 的输入语义都是“包含式”，
#: 但字段名不同，交给 :func:`detect_protocol` 看实际形状更稳。
_PROTOCOL_ALIASES: dict[str, str | None] = {
    PROTOCOL_ANTHROPIC: PROTOCOL_ANTHROPIC,
    "anthropic": PROTOCOL_ANTHROPIC,
    PROTOCOL_OPENAI_CHAT: PROTOCOL_OPENAI_CHAT,
    "chat": PROTOCOL_OPENAI_CHAT,
    "chat_completions": PROTOCOL_OPENAI_CHAT,
    PROTOCOL_OPENAI_RESPONSES: PROTOCOL_OPENAI_RESPONSES,
    "responses": PROTOCOL_OPENAI_RESPONSES,
    PROTOCOL_OPENAI_COMPAT_CAMEL: PROTOCOL_OPENAI_COMPAT_CAMEL,
    "openai_camel": PROTOCOL_OPENAI_COMPAT_CAMEL,
    PROTOCOL_GEMINI: PROTOCOL_GEMINI,
    PROTOCOL_DEEPSEEK: PROTOCOL_DEEPSEEK,
    "deepseek": PROTOCOL_DEEPSEEK,
    "openai": None,
}

#: 账本里区分新老归一化规则：老行为 / 历史行记 ``v0``，本模块首次落地记 ``v1``。
NORMALIZER_VERSION = "v1"

# ---------------------------------------------------------------------------
# 双轨采集（P3 落表，这里先定义常量与清洗函数）
# ---------------------------------------------------------------------------

#: Agent 自报：适配器解析内核原生事件（直连模式的 Claude Code / CodeBuddy 等）。
TELEMETRY_SOURCE_AGENT_REPORT = "agent_report"
#: 网关旁路：记录网关从上游响应 tee 出 usage（无自报能力的 Agent）。
TELEMETRY_SOURCE_GATEWAY_PROXY = "gateway_proxy"
TELEMETRY_SOURCES = (TELEMETRY_SOURCE_AGENT_REPORT, TELEMETRY_SOURCE_GATEWAY_PROXY)

#: 上游响应形状：OpenAI Chat Completions 与 Responses API 的字段不一样。
RESPONSE_SHAPE_ANTHROPIC = "anthropic_messages"
RESPONSE_SHAPE_CHAT = "chat"
RESPONSE_SHAPE_RESPONSES = "responses"
RESPONSE_SHAPE_GEMINI = "gemini"
RESPONSE_SHAPES = (
    RESPONSE_SHAPE_ANTHROPIC,
    RESPONSE_SHAPE_CHAT,
    RESPONSE_SHAPE_RESPONSES,
    RESPONSE_SHAPE_GEMINI,
)

#: 上游响应 ID 列宽（与 ``UsageRecord.request_id`` 一致）。
UPSTREAM_REQUEST_ID_MAX_LEN = 120

#: 原始 usage 落库上限（字节）。超过后写结构化占位对象，不硬切字符串。
RAW_USAGE_LIMIT_BYTES = 16 * 1024
#: 截断时优先保留的顶层键（覆盖各家协议的计费/窗口字段）。
RAW_USAGE_KEY_FIELDS = (
    "input_tokens",
    "output_tokens",
    "prompt_tokens",
    "completion_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "cached_tokens",
    "cachedInputTokens",
    "cacheReadTokens",
    "cacheWriteTokens",
    "reasoning_tokens",
    "reasoning_output_tokens",
    "reasoningTokens",
    "reasoningOutputTokens",
    "thinking_tokens",
    "total_tokens",
    "totalTokens",
    "input_tokens_details",
    "output_tokens_details",
    "prompt_tokens_details",
    "completion_tokens_details",
    "cache_creation",
    "usageMetadata",
    "modelContextWindow",
)

#: 标准字段顺序（返回字典与前端展示共用）。
STANDARD_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "reasoning_tokens",
    "context_tokens",
    "total_tokens",
)


@dataclass(frozen=True, slots=True)
class ProtocolRules:
    """一个协议的口径声明。

    ``cache_in_input``
        ``True`` = 输入侧已含缓存；``False`` = 输入与缓存分离；``None`` = 未知。
    ``reasoning_in_output``
        ``True`` = 推理是输出的子集（不能重复计费）；``False`` = 推理独立；
        ``None`` = 未知。计费层据此决定是否另算推理。
    ``verified``
        ``True`` = 已用真实线上样本验证；``False`` = 仍待补采。
    """

    protocol: str
    cache_in_input: bool | None
    reasoning_in_output: bool | None
    verified: bool = False


_UNKNOWN_RULES = ProtocolRules(protocol="unknown", cache_in_input=None, reasoning_in_output=None)

_RULES: dict[str, ProtocolRules] = {
    # 真实样本已验证：input 与缓存分离；thinking_tokens 是输出子集。
    PROTOCOL_ANTHROPIC: ProtocolRules(
        protocol=PROTOCOL_ANTHROPIC,
        cache_in_input=False,
        reasoning_in_output=True,
        verified=True,
    ),
    # 官方协议语义（包含式）；尚未用本机真实响应验证。
    PROTOCOL_OPENAI_CHAT: ProtocolRules(
        protocol=PROTOCOL_OPENAI_CHAT,
        cache_in_input=True,
        reasoning_in_output=True,
        verified=False,
    ),
    PROTOCOL_OPENAI_RESPONSES: ProtocolRules(
        protocol=PROTOCOL_OPENAI_RESPONSES,
        cache_in_input=True,
        reasoning_in_output=True,
        verified=False,
    ),
    # 自定义 CLI / 任意 OpenAI 兼容驼峰端点：默认包含式。
    PROTOCOL_OPENAI_COMPAT_CAMEL: ProtocolRules(
        protocol=PROTOCOL_OPENAI_COMPAT_CAMEL,
        cache_in_input=True,
        reasoning_in_output=True,
        verified=False,
    ),
    # Gemini usageMetadata：promptTokenCount 含 cachedContentTokenCount。
    PROTOCOL_GEMINI: ProtocolRules(
        protocol=PROTOCOL_GEMINI,
        cache_in_input=True,
        reasoning_in_output=True,
        verified=False,
    ),
    # DeepSeek 样例出现 reasoningTokens > outputTokens，暂按“推理独立”处理；
    # 真实样本补采前不得放开为主链路。
    PROTOCOL_DEEPSEEK: ProtocolRules(
        protocol=PROTOCOL_DEEPSEEK,
        cache_in_input=False,
        reasoning_in_output=False,
        verified=False,
    ),
}


def rules_for(protocol: str | None) -> ProtocolRules:
    """返回协议口径；未知协议返回“全部未知”，调用方不得据此做计费推断。"""
    resolved = resolve_protocol(protocol, raw=None)
    if resolved is None:
        return _UNKNOWN_RULES
    return _RULES.get(resolved, _UNKNOWN_RULES)


#: 协议 → 上游响应形状（落库审计用；网关会传实际观察到的形状）。
_SHAPE_BY_PROTOCOL = {
    PROTOCOL_ANTHROPIC: RESPONSE_SHAPE_ANTHROPIC,
    PROTOCOL_OPENAI_CHAT: RESPONSE_SHAPE_CHAT,
    PROTOCOL_OPENAI_RESPONSES: RESPONSE_SHAPE_RESPONSES,
    PROTOCOL_OPENAI_COMPAT_CAMEL: RESPONSE_SHAPE_CHAT,
    PROTOCOL_GEMINI: RESPONSE_SHAPE_GEMINI,
}


def response_shape_for(protocol: str | None) -> str | None:
    """把协议名翻成响应形状；未知协议返回 ``None``。"""
    resolved = resolve_protocol(protocol, raw=None)
    if resolved is None:
        return None
    return _SHAPE_BY_PROTOCOL.get(resolved)


def resolve_protocol(protocol: str | None, raw: Any = None) -> str | None:
    """把调用方给的协议名归一到本模块的枚举。

    * 已知协议 → 原样（或别名归一）。
    * ``openai`` → ``None``（交给字段 / 响应形状探测，避免把 Chat 与 Responses 混为一谈）。
    * 显式 ``unknown`` → ``None``（**硬阻塞**：未验证协议不得被字段自动“救活”）。
    * 未给 → 若传了 ``raw``，尝试 :func:`detect_protocol`。
    """
    if protocol:
        name = str(protocol).strip()
        if name in _RULES:
            return name
        if name == "unknown":
            return None
        if name in _PROTOCOL_ALIASES:
            mapped = _PROTOCOL_ALIASES[name]
            if mapped is not None:
                return mapped
            return detect_protocol(raw)
        # 未知名字：不猜，交给字段探测（如果有 raw）。
        return detect_protocol(raw)
    return detect_protocol(raw)


# ---------------------------------------------------------------------------
# 字段探测（只在 protocol=None 时兜底）
# ---------------------------------------------------------------------------


def detect_protocol(raw: Any) -> str | None:
    """按字段形状猜协议；**只做兜底，不负责判断包含/分离**。

    顺序：

    1. ``prompt_tokens`` → OpenAI Chat（Anthropic 不会发这个键）。
    2. ``cache_read_input_tokens`` / ``cache_creation_input_tokens`` → Anthropic。
    3. ``usageMetadata`` / ``promptTokenCount`` → Gemini。
    4. ``input_tokens`` → OpenAI Responses（裸 ``input_tokens`` 默认包含式）。
    5. 驼峰 ``inputTokens`` / ``outputTokens`` → OpenAI 兼容驼峰。
    6. 认不出 → ``None``。
    """
    if not isinstance(raw, Mapping) or not raw:
        return None
    if "prompt_tokens" in raw:
        return PROTOCOL_OPENAI_CHAT
    if (
        "cache_read_input_tokens" in raw
        or "cache_creation_input_tokens" in raw
        or isinstance(raw.get("cache_creation"), Mapping)
    ):
        return PROTOCOL_ANTHROPIC
    if "usageMetadata" in raw or "promptTokenCount" in raw:
        return PROTOCOL_GEMINI
    if "input_tokens" in raw:
        return PROTOCOL_OPENAI_RESPONSES
    if any(
        key in raw
        for key in (
            "inputTokens",
            "outputTokens",
            "cachedInputTokens",
            "cachedTokens",
            "cacheReadTokens",
            "cacheWriteTokens",
            "reasoningOutputTokens",
        )
    ):
        return PROTOCOL_OPENAI_COMPAT_CAMEL
    return None


# ---------------------------------------------------------------------------
# 取数小工具
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and not (isinstance(value, float) and not math.isfinite(value))
    )


def _num(raw: Mapping[str, Any], *keys: str) -> int | None:
    """按顺序取第一个出现的数值；缺失 / ``None`` / 布尔 / 字符串一律跳过。"""
    for key in keys:
        if key not in raw:
            continue
        value = raw[key]
        if _is_number(value):
            return int(value)
    return None


def _num0(raw: Mapping[str, Any], *keys: str) -> int:
    """同 :func:`_num`，但缺失按 0（用于“这个协议一定没有此桶”的字段）。"""
    value = _num(raw, *keys)
    return 0 if value is None else value


def _detail_num(
    raw: Mapping[str, Any], detail_key: str, *keys: str
) -> int | None:
    """从嵌套 details 里取数；也接受顶层同名键。"""
    detail = raw.get(detail_key)
    if isinstance(detail, Mapping):
        value = _num(detail, *keys)
        if value is not None:
            return value
    return _num(raw, *keys)


def _sum_nested_cache_creation(raw: Mapping[str, Any]) -> int:
    """Anthropic 新格式：``cache_creation`` 是 1h / 5m 的嵌套字典。"""
    nested = raw.get("cache_creation")
    if not isinstance(nested, Mapping):
        return 0
    return _num0(nested, "ephemeral_1h_input_tokens") + _num0(
        nested, "ephemeral_5m_input_tokens"
    )


# ---------------------------------------------------------------------------
# 各协议归一化
# ---------------------------------------------------------------------------


def _normalize_anthropic(raw: Mapping[str, Any]) -> dict[str, int | None]:
    input_tokens = _num0(raw, "input_tokens")
    cache_read = _num0(raw, "cache_read_input_tokens", "cache_read_tokens")
    cache_creation = _num(
        raw,
        "cache_creation_input_tokens",
        "cache_creation_tokens",
        # 缓存写只是“数值别名”，不是“分离式”的判定标记。
        "cache_write_input_tokens",
        "cacheWriteInputTokens",
    )
    if not cache_creation:
        cache_creation = _sum_nested_cache_creation(raw)
    output = _num0(raw, "output_tokens")
    reasoning = _detail_num(raw, "output_tokens_details", "thinking_tokens", "reasoning_tokens")
    if reasoning is None:
        reasoning = _num(raw, "thinking_tokens")
    return _finish(
        input_tokens=input_tokens,
        output_tokens=output,
        cache_read=cache_read,
        cache_creation=cache_creation,
        reasoning=reasoning,
        context=input_tokens + cache_read + cache_creation,
    )


def _normalize_openai_chat(raw: Mapping[str, Any]) -> dict[str, int | None]:
    # prompt_tokens / input_tokens 都是“输入侧总量”，缓存是它的子集。
    prompt = _num0(raw, "prompt_tokens", "input_tokens")
    cache_read = _detail_num(raw, "prompt_tokens_details", "cached_tokens")
    if cache_read is None:
        cache_read = _num0(
            raw, "cached_tokens", "cached_input_tokens", "cachedInputTokens"
        )
    input_tokens = max(0, prompt - cache_read)
    output = _num0(raw, "completion_tokens", "output_tokens")
    reasoning = _detail_num(raw, "completion_tokens_details", "reasoning_tokens")
    if reasoning is None:
        reasoning = _num(raw, "reasoning_tokens")
    return _finish(
        input_tokens=input_tokens,
        output_tokens=output,
        cache_read=cache_read,
        cache_creation=0,
        reasoning=reasoning,
        context=prompt,
    )


def _normalize_openai_responses(raw: Mapping[str, Any]) -> dict[str, int | None]:
    prompt = _num0(raw, "input_tokens", "inputTokens")
    cache_read = _detail_num(raw, "input_tokens_details", "cached_tokens")
    if cache_read is None:
        cache_read = _num0(
            raw,
            "cached_input_tokens",
            "cachedInputTokens",
            "cached_tokens",
            # 兼容“已经翻译成蛇形”的上游字段（如 Antigravity stats）。
            "cache_read_tokens",
        )
    # 包含式协议里缓存写不是窗口占用的组成部分，但仍可作为明细保留（不加入 context）。
    cache_creation = _num0(raw, "cache_creation_tokens", "cache_creation_input_tokens")
    input_tokens = max(0, prompt - cache_read)
    output = _num0(raw, "output_tokens", "outputTokens", "completion_tokens")
    reasoning = _detail_num(raw, "output_tokens_details", "reasoning_tokens")
    if reasoning is None:
        reasoning = _num(
            raw, "reasoning_output_tokens", "reasoningOutputTokens", "reasoning_tokens"
        )
    return _finish(
        input_tokens=input_tokens,
        output_tokens=output,
        cache_read=cache_read,
        cache_creation=cache_creation,
        reasoning=reasoning,
        context=prompt,
    )


def _normalize_openai_compat_camel(raw: Mapping[str, Any]) -> dict[str, int | None]:
    prompt = _num0(raw, "inputTokens", "input_tokens", "prompt_tokens")
    cache_read = _detail_num(raw, "input_tokens_details", "cached_tokens")
    if cache_read is None:
        cache_read = _num0(
            raw,
            "cachedInputTokens",
            "cached_input_tokens",
            "cachedTokens",
            "cached_tokens",
            "cache_read_tokens",
            "cacheReadTokens",
        )
    cache_creation = _num0(
        raw, "cache_creation_tokens", "cache_creation_input_tokens", "cacheWriteTokens"
    )
    input_tokens = max(0, prompt - cache_read)
    output = _num0(raw, "outputTokens", "output_tokens", "completion_tokens")
    reasoning = _num(
        raw, "reasoningOutputTokens", "reasoning_output_tokens", "reasoningTokens", "reasoning_tokens"
    )
    return _finish(
        input_tokens=input_tokens,
        output_tokens=output,
        cache_read=cache_read,
        cache_creation=cache_creation,
        reasoning=reasoning,
        context=prompt,
    )


def _normalize_gemini(raw: Mapping[str, Any]) -> dict[str, int | None]:
    meta = raw.get("usageMetadata")
    src = meta if isinstance(meta, Mapping) else raw
    prompt = _num0(src, "promptTokenCount", "prompt_token_count")
    cache_read = _num0(src, "cachedContentTokenCount", "cached_content_token_count")
    input_tokens = max(0, prompt - cache_read)
    output = _num0(src, "candidatesTokenCount", "candidates_token_count")
    reasoning = _num(src, "thoughtsTokenCount", "thoughts_token_count")
    return _finish(
        input_tokens=input_tokens,
        output_tokens=output,
        cache_read=cache_read,
        cache_creation=0,
        reasoning=reasoning,
        context=prompt,
    )


def _normalize_deepseek(raw: Mapping[str, Any]) -> dict[str, int | None]:
    input_tokens = _num0(raw, "inputTokens", "input_tokens")
    # dsh 适配器可能已经先把驼峰翻译成蛇形（cache_read_input_tokens 等），
    # 两种形态都要认，否则显式协议会把缓存丢掉。
    cache_read = _num0(
        raw, "cacheReadTokens", "cache_read_tokens", "cache_read_input_tokens"
    )
    cache_creation = _num0(
        raw, "cacheWriteTokens", "cache_creation_tokens", "cache_creation_input_tokens"
    )
    output = _num0(raw, "outputTokens", "output_tokens")
    reasoning = _num(raw, "reasoningTokens", "reasoning_tokens")
    return _finish(
        input_tokens=input_tokens,
        output_tokens=output,
        cache_read=cache_read,
        cache_creation=cache_creation,
        reasoning=reasoning,
        context=input_tokens + cache_read + cache_creation,
    )


_NORMALIZERS = {
    PROTOCOL_ANTHROPIC: _normalize_anthropic,
    PROTOCOL_OPENAI_CHAT: _normalize_openai_chat,
    PROTOCOL_OPENAI_RESPONSES: _normalize_openai_responses,
    PROTOCOL_OPENAI_COMPAT_CAMEL: _normalize_openai_compat_camel,
    PROTOCOL_GEMINI: _normalize_gemini,
    PROTOCOL_DEEPSEEK: _normalize_deepseek,
}


def _empty() -> dict[str, int | None]:
    return {field: None for field in STANDARD_FIELDS}


def _finish(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_creation: int,
    reasoning: int | None,
    context: int,
) -> dict[str, int | None]:
    """收口：没有任何有效计数时返回全 None（记 unknown），否则补齐派生值。"""
    if not any((input_tokens, output_tokens, cache_read, cache_creation, reasoning)):
        return _empty()
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
        "reasoning_tokens": reasoning,
        "context_tokens": context,
        "total_tokens": context + output_tokens,
    }


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------


def normalize_usage(
    raw: Any,
    *,
    protocol: str | None = None,
    already_normalized: bool = False,
) -> dict[str, int | None]:
    """把一份 usage 归一化成 :data:`STANDARD_FIELDS` 的 7 个字段。

    参数
    ----
    raw
        原始 usage 对象。**只接受原始帧**；已经归一化过的字典请传
        ``already_normalized=True``，避免二次扣缓存。
    protocol
        协议名（见模块常量）。``None`` 时按 :func:`detect_protocol` 兜底。
        支持旧名字：``anthropic`` / ``gemini`` / ``chat`` / ``responses`` /
        ``deepseek``；``openai`` 不直接映射，交给形状探测。
    already_normalized
        为 ``True`` 时按“已经归一化”读取：``input_tokens`` 不再扣缓存，
        缺失的 ``context_tokens`` / ``total_tokens`` 按标准口径派生。
    """
    if already_normalized:
        return _normalized_input(raw)

    if not isinstance(raw, Mapping) or not raw:
        return _empty()

    resolved = resolve_protocol(protocol, raw)
    normalizer = _NORMALIZERS.get(resolved or "")
    if normalizer is None:
        return _empty()
    return normalizer(raw)


def _normalized_input(raw: Any) -> dict[str, int | None]:
    """读取已归一化字典；不做任何包含/分离推断。"""
    if not isinstance(raw, Mapping) or not raw:
        return _empty()
    input_tokens = _num0(raw, "input_tokens", "inputTokens")
    output = _num0(raw, "output_tokens", "outputTokens")
    cache_read = _num0(raw, "cache_read_tokens", "cached_input_tokens")
    cache_creation = _num0(raw, "cache_creation_tokens", "cache_write_input_tokens")
    reasoning = _num(raw, "reasoning_tokens", "reasoning_output_tokens")
    context = _num(raw, "context_tokens")
    if context is None:
        context = input_tokens + cache_read + cache_creation
    total = _num(raw, "total_tokens")
    if total is None:
        total = context + output
    if not any((input_tokens, output, cache_read, cache_creation, reasoning, context)):
        return _empty()
    return {
        "input_tokens": input_tokens,
        "output_tokens": output,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
        "reasoning_tokens": reasoning,
        "context_tokens": context,
        "total_tokens": total,
    }


# ---------------------------------------------------------------------------
# 原始留痕与双轨去重辅助
# ---------------------------------------------------------------------------


def upstream_request_id(value: Any) -> str | None:
    """清洗上游响应 ID（Anthropic ``message.id`` / OpenAI ``resp_...`` / ``chatcmpl-...``）。

    只做裁剪与去空白；不校验前缀（不同中转层会自造 ID）。空值返回 ``None``。
    """
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned[:UPSTREAM_REQUEST_ID_MAX_LEN]


def truncate_raw_usage(
    raw: Any, *, limit: int = RAW_USAGE_LIMIT_BYTES
) -> dict[str, Any]:
    """把原始 usage 收到落库上限内。

    未超限：原样返回（浅拷贝）。
    超限：**不硬切字符串**，返回结构化占位对象，保证仍是合法 JSON：

    ``{"_truncated": True, "_original_bytes": N, "_keys": [...], "usage": {...}}``
    """
    if not isinstance(raw, Mapping):
        return {}
    if raw.get("_truncated") is True:
        return dict(raw)

    try:
        encoded = json.dumps(
            dict(raw), ensure_ascii=False, separators=(",", ":"), default=str
        )
    except (TypeError, ValueError):
        encoded = ""

    original_bytes = len(encoded.encode("utf-8"))
    if original_bytes <= limit:
        return dict(raw)

    kept: dict[str, Any] = {
        key: raw[key] for key in RAW_USAGE_KEY_FIELDS if key in raw
    }
    # 保证占位对象自身可序列化（嵌套里混入非 JSON 值时统一转字符串）。
    safe_kept = json.loads(json.dumps(kept, ensure_ascii=False, default=str))
    return {
        "_truncated": True,
        "_original_bytes": original_bytes,
        "_keys": sorted(str(key) for key in raw.keys()),
        "usage": safe_kept,
    }


__all__ = [
    "NORMALIZER_VERSION",
    "PROTOCOL_ANTHROPIC",
    "PROTOCOL_DEEPSEEK",
    "PROTOCOL_GEMINI",
    "PROTOCOL_OPENAI_CHAT",
    "PROTOCOL_OPENAI_COMPAT_CAMEL",
    "PROTOCOL_OPENAI_RESPONSES",
    "PROTOCOL_UNKNOWN",
    "PROTOCOLS",
    "RAW_USAGE_LIMIT_BYTES",
    "RESPONSE_SHAPES",
    "STANDARD_FIELDS",
    "TELEMETRY_SOURCES",
    "ProtocolRules",
    "detect_protocol",
    "normalize_usage",
    "resolve_protocol",
    "response_shape_for",
    "rules_for",
    "truncate_raw_usage",
    "upstream_request_id",
]