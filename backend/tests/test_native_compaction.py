"""原生压缩配置：把平台上限 / 阈值翻译成 Claude Code 环境变量与 Codex -c 覆盖。"""
from __future__ import annotations

from app.agents import AgentContext, get_agent
from app.agents.builtin.util import (
    claude_compaction_env,
    codex_compaction_overrides,
    compaction_percent,
)
from app.models import Conversation
from app.runtime.context import ContextSettings
from app.runtime.service import apply_context_policy


def test_compaction_percent_clamps_to_integer_percent():
    assert compaction_percent(160_000, 200_000) == 80
    assert compaction_percent(1, 200_000) == 1
    assert compaction_percent(200_000, 200_000) == 100
    assert compaction_percent(0, 0) == 80  # 无效输入给保守默认


def test_claude_env_uses_native_window_clamp():
    env = claude_compaction_env(200_000, 160_000)
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "200000"
    assert env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "80"

    # 小于 100K：百分比按 100K 算（Claude 会把窗口夹到 100K）。
    small = claude_compaction_env(50_000, 40_000)
    assert small["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "50000"
    assert small["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "40"

    # 大于 1M：按 1M 算，百分比封顶 100。
    large = claude_compaction_env(2_000_000, 1_500_000)
    assert large["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "2000000"
    assert large["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "100"


def test_claude_env_empty_without_window():
    assert claude_compaction_env(None, None) == {}
    assert claude_compaction_env(0, 100) == {}


def test_codex_overrides():
    overrides = codex_compaction_overrides(200_000, 160_000)
    assert overrides == {
        "model_context_window": "200000",
        "model_auto_compact_token_limit": "160000",
    }
    assert codex_compaction_overrides(None, None) == {}


def test_claude_adapter_injects_env_unless_autocompact_is_explicit():
    ctx = AgentContext(
        agent_id="a", agent_name="n", model="sonnet", prompt="hi",
        context_limit_tokens=200_000, context_trigger_tokens=160_000,
    )
    invocation = get_agent("claude-code").build_invocation(ctx)
    assert invocation.env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "200000"
    assert invocation.env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "80"

    # 用户显式设了 --autocompact：不叠加环境变量，尊重显式配置。
    ctx.options["autocompact"] = 120_000
    explicit = get_agent("claude-code").build_invocation(ctx)
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in explicit.env
    assert "--autocompact" in explicit.argv


def test_codex_adapter_injects_compaction_overrides():
    ctx = AgentContext(
        agent_id="a", agent_name="n", model="gpt-5-codex",
        context_limit_tokens=200_000, context_trigger_tokens=160_000,
    )
    overrides = get_agent("codex")._config_overrides(ctx)
    assert overrides["model_context_window"] == "200000"
    assert overrides["model_auto_compact_token_limit"] == "160000"


def test_apply_context_policy_follows_auto_window_and_manual_override():
    conversation = Conversation(id="c1", title="t")
    conversation.context_kernel_window_tokens = 200_000

    auto = ContextSettings()  # 默认 auto
    ctx = AgentContext(agent_id="a", agent_name="n")
    apply_context_policy(ctx, auto, conversation)
    assert ctx.context_limit_tokens == 200_000
    assert ctx.context_trigger_tokens == 160_000  # 200k × 80%

    manual = ContextSettings(limit_tokens=100_000, threshold_ratio=0.5, limit_mode="manual")
    ctx2 = AgentContext(agent_id="a", agent_name="n")
    apply_context_policy(ctx2, manual, conversation)
    assert ctx2.context_limit_tokens == 100_000  # 用户显式上限优先
    assert ctx2.context_trigger_tokens == 50_000