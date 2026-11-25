"""上下文窗口：近似估算、上限配置持久化与校验、接近上限自动压缩。

口径的唯一来源是 ``app.runtime.context.estimate_tokens``（展示 / 阈值 / 压缩决策共用）；
这里既验算它，也验接口与运行层的落盘行为。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlmodel import select

from app.agents.builtin.util import context_tokens as request_context_tokens
from app.agents.events import context_compacted, context_usage, log, text_delta
from app.db import SessionLocal
from app.models import Conversation, Message
from app.runtime import context as context_service
from app.runtime import service as runtime_service
from app.runtime.manager import ActiveRun

from helpers import make_gateway, make_profile


async def make_agent(client: httpx.AsyncClient, **overrides) -> dict:
    gateway = await make_gateway(client)
    return await make_profile(client, gateway_id=gateway["id"], **overrides)


async def read_sse(response: httpx.Response) -> list[dict]:
    events: list[dict] = []
    async for line in response.aiter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def test_estimate_tokens_is_the_shared_metric():
    """中日韩按字、其余按 4 字符；空串为 0。"""
    assert context_service.estimate_tokens(None) == 0
    assert context_service.estimate_tokens("") == 0
    assert context_service.estimate_tokens("hello world") == 3  # ceil(11 / 4)
    assert context_service.estimate_tokens("你好世界") == 4
    assert context_service.estimate_tokens("你好 hello") == 4  # 2 字 + ceil(6 / 4)


def test_estimate_message_tokens_counts_tool_parts():
    """工具调用入参、工具结果与推理内容都要计入；text 片段与 content 只算一次。"""
    message = Message(
        conversation_id="c",
        role="assistant",
        content="完成",
        parts=[
            {"kind": "text", "text": "完成"},
            {"kind": "tool_call", "id": "c1", "name": "Read", "input": {"path": "a" * 400}},
            {"kind": "tool_result", "id": "c1", "output": "b" * 4000},
            {"kind": "thinking", "text": "c" * 800},
        ],
    )
    base = context_service.estimate_message_tokens(message)
    with_parts = context_service.estimate_message_tokens(message, include_parts=True)
    assert base == context_service.estimate_tokens("完成") + context_service.MESSAGE_OVERHEAD_TOKENS
    # 4000 个 ASCII ≈ 1000 tokens，工具/思考的量级必须体现出来
    assert with_parts - base > 1000


def test_context_tokens_helper_normalizes_both_notations():
    """Anthropic 风格缓存与 input 分离；OpenAI 风格 cached 是 prompt_tokens 的子集。"""
    assert request_context_tokens(
        {"input_tokens": 100, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 50}
    ) == 1050
    assert request_context_tokens({"prompt_tokens": 1000, "cached_tokens": 900}) == 1000
    assert request_context_tokens({}) == 0
    assert request_context_tokens(None) == 0


async def test_context_usage_counts_tool_parts_for_session_kernels(
    client: httpx.AsyncClient,
):
    """会话型内核的转录里装着工具调用与结果，面板不能只数最终正文。"""
    agent = await make_agent(client)
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})
    ).json()
    async with SessionLocal() as session:
        row = await session.get(Conversation, conversation["id"])
        row.session_id = "sess-parts"  # 会话型内核：平台看不到内部历史，按全量 + 片段估算
        session.add(row)
        session.add(
            Message(
                conversation_id=conversation["id"],
                role="assistant",
                content="完成",
                parts=[
                    {"kind": "tool_call", "id": "c1", "name": "Read", "input": {"path": "a" * 400}},
                    {"kind": "tool_result", "id": "c1", "output": "b" * 4000},
                    {"kind": "thinking", "text": "c" * 800},
                ],
            )
        )
        await session.commit()

    view = (await client.get(f"/api/conversations/{conversation['id']}/context")).json()
    assert view["measured"] is False
    assert view["used_tokens"] > 1000, "工具结果 / 推理没有计入上下文"


async def test_context_usage_uses_kernel_snapshot_when_available(
    client: httpx.AsyncClient,
):
    """有内核上报的单次请求 token 时，以它为基准，不再按正文重新估。"""
    agent = await make_agent(client)
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})
    ).json()
    async with SessionLocal() as session:
        row = await session.get(Conversation, conversation["id"])
        row.session_id = "sess-snapshot"
        session.add(row)
        session.add(
            Message(
                conversation_id=conversation["id"],
                role="assistant",
                content="好的",
                context_tokens=5000,
            )
        )
        await session.commit()

    view = (await client.get(f"/api/conversations/{conversation['id']}/context")).json()
    assert view["measured"] is True
    # 快照是最后一次请求的输入；它自己的回复正文还没进去，要补 2 字 + 4 开销。
    assert view["used_tokens"] == 5000 + context_service.estimate_tokens("好的") + 4
    assert "内核最近一次上报" in view["estimation"]


async def test_context_usage_ignores_stale_snapshot_after_compaction(
    client: httpx.AsyncClient,
):
    """压缩会清掉 session_id、旧快照随之失效，否则面板会一直停在压缩前的大数。"""
    agent = await make_agent(client)
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})
    ).json()
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        row = await session.get(Conversation, conversation["id"])
        row.session_id = "sess-old"
        row.context_compacted_at = now
        session.add(row)
        session.add(
            Message(
                conversation_id=conversation["id"],
                role="assistant",
                content="好的",
                context_tokens=50_000,
                created_at=now - timedelta(hours=1),
            )
        )
        await session.commit()

    view = (await client.get(f"/api/conversations/{conversation['id']}/context")).json()
    assert view["measured"] is False
    assert view["used_tokens"] < 1000


async def test_context_usage_ignores_tool_parts_when_history_is_replayed(
    client: httpx.AsyncClient,
):
    """非会话型内核只回放最终回复：工具片段根本没发送，不能算进已用。"""
    agent = await make_agent(client)
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})
    ).json()
    async with SessionLocal() as session:
        session.add(
            Message(
                conversation_id=conversation["id"],
                role="assistant",
                content="好的",
                parts=[
                    {"kind": "tool_result", "id": "c1", "output": "b" * 40_000},
                ],
            )
        )
        await session.commit()

    view = (await client.get(f"/api/conversations/{conversation['id']}/context")).json()
    assert view["measured"] is False
    assert view["used_tokens"] < 1000


async def test_context_usage_event_persists_snapshot(client: httpx.AsyncClient, monkeypatch):
    """运行层必须把 ``context.usage`` 落到消息上，面板才能读到真实快照。"""
    agent = await make_agent(client)
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})
    ).json()

    async with SessionLocal() as session:
        conv = await session.get(Conversation, conversation["id"])
        user = Message(conversation_id=conv.id, role="user", content="你好")
        assistant = Message(conversation_id=conv.id, role="assistant", content="")
        session.add_all([user, assistant])
        await session.commit()
        await session.refresh(assistant)

        run = ActiveRun(id="run-ctx", conversation_id=conv.id, agent_name="假智能体")
        prepared = runtime_service.PreparedRun(
            conversation=conv,
            profile=None,  # type: ignore[arg-type] - 本用例只验证事件落库
            gateway=None,
            definition=None,  # type: ignore[arg-type]
            ctx=None,  # type: ignore[arg-type]
            run=run,
            user_message=user,
            assistant_message=assistant,
        )

        async def fake_stream(definition, ctx, active_run):
            yield context_usage(4321)
            yield text_delta("好")

        monkeypatch.setattr(runtime_service, "stream_run", fake_stream)
        async for _ in runtime_service.consume_run(session, prepared):
            pass

        await session.refresh(assistant)
        assert assistant.context_tokens == 4321
        assert assistant.content == "好"


async def test_context_settings_defaults_and_roundtrip(client: httpx.AsyncClient):
    # 没有配置过 = 仓库默认值（128k / 80%）
    defaults = (await client.get("/api/system/context")).json()
    assert defaults["limit_tokens"] == context_service.DEFAULT_LIMIT_TOKENS
    assert defaults["threshold_ratio"] == pytest.approx(context_service.DEFAULT_THRESHOLD_RATIO)
    assert defaults["threshold_tokens"] == int(
        context_service.DEFAULT_LIMIT_TOKENS * context_service.DEFAULT_THRESHOLD_RATIO
    )

    saved = await client.put(
        "/api/system/context", json={"limit_tokens": 32000, "threshold_ratio": 0.75}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["limit_tokens"] == 32000
    assert saved.json()["threshold_tokens"] == 24000

    # 重新打开页面（重新 GET）仍然生效 = 已落库，不是进程内状态
    again = (await client.get("/api/system/context")).json()
    assert again["limit_tokens"] == 32000
    assert again["threshold_ratio"] == pytest.approx(0.75)


@pytest.mark.parametrize(
    "payload",
    [
        {"limit_tokens": 0, "threshold_ratio": 0.8},
        {"limit_tokens": -100, "threshold_ratio": 0.8},
        {"limit_tokens": 500, "threshold_ratio": 0.8},  # 低于合理下限
        {"limit_tokens": 32000, "threshold_ratio": 0},
        {"limit_tokens": 32000, "threshold_ratio": -0.1},
        {"limit_tokens": 32000, "threshold_ratio": 1.2},
        {"limit_tokens": "abc", "threshold_ratio": 0.8},
        {"limit_tokens": 32000, "threshold_ratio": "abc"},
    ],
)
async def test_context_settings_reject_invalid_input_without_saving(
    client: httpx.AsyncClient, payload: dict
):
    before = (await client.get("/api/system/context")).json()
    response = await client.put("/api/system/context", json=payload)
    assert response.status_code == 422, response.text
    after = (await client.get("/api/system/context")).json()
    assert after == before, "非法输入不得写入配置"


async def test_context_settings_reject_limit_below_current_usage(client: httpx.AsyncClient):
    agent = await make_agent(client)
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})
    ).json()

    async with SessionLocal() as session:
        session.add(
            Message(
                conversation_id=conversation["id"],
                role="user",
                content="字" * 4000,  # ≈ 4000 tokens
            )
        )
        await session.commit()

    too_small = await client.put(
        "/api/system/context",
        json={"limit_tokens": 1000, "threshold_ratio": 0.8, "conversation_id": conversation["id"]},
    )
    assert too_small.status_code == 422
    assert "不能低于当前上下文已用量" in too_small.json()["detail"]
    assert (await client.get("/api/system/context")).json()["limit_tokens"] == (
        context_service.DEFAULT_LIMIT_TOKENS
    )

    ok = await client.put(
        "/api/system/context",
        json={"limit_tokens": 8000, "threshold_ratio": 0.8, "conversation_id": conversation["id"]},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["limit_tokens"] == 8000


async def test_conversation_context_endpoint_reports_usage(client: httpx.AsyncClient):
    agent = await make_agent(client)
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})
    ).json()
    async with SessionLocal() as session:
        session.add_all(
            [
                Message(conversation_id=conversation["id"], role="user", content="你好"),
                Message(
                    conversation_id=conversation["id"],
                    role="assistant",
                    content="你好，有什么可以帮你？",
                ),
            ]
        )
        await session.commit()

    view = (await client.get(f"/api/conversations/{conversation['id']}/context")).json()
    assert view["used_tokens"] > 0
    assert view["limit_tokens"] == context_service.DEFAULT_LIMIT_TOKENS
    assert 0 < view["percent"] < 100
    assert view["strategy"] and "最近" in view["strategy"]
    assert view["compaction"] is None
    assert view["compacted_count"] == 0
    assert "estimation" in view


async def test_auto_compaction_keeps_recent_messages_and_persists_summary(
    client: httpx.AsyncClient,
):
    """达到阈值时自动压缩：摘要落库、最近若干条保留、本轮照常完成。"""
    await client.put("/api/system/context", json={"limit_tokens": 1500, "threshold_ratio": 0.8})
    agent = await make_agent(client, system_prompt="You are a tester.")
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})
    ).json()

    # 12 条较早消息：估算远高于 1200 的阈值，且条数多于「保留最近 8 条」。
    async with SessionLocal() as session:
        session.add_all(
            [
                Message(
                    conversation_id=conversation["id"],
                    role="user" if index % 2 == 0 else "assistant",
                    content=f"较早的第 {index} 条消息：" + "a" * 1000,
                )
                for index in range(12)
            ]
        )
        await session.commit()

    async with client.stream(
        "POST",
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": "现在继续"},
    ) as response:
        assert response.status_code == 200
        events = await read_sse(response)

    texts = [e.get("text") or "" for e in events if e["type"] == "log"]
    assert any("上下文已自动压缩" in text for text in texts), texts
    # 压缩后本轮照常完成
    assert events[-1]["type"] == "stream.closed"

    async with SessionLocal() as session:
        row = await session.get(Conversation, conversation["id"])
        assert row is not None
        # 按「上限的一半」从新往旧收：12 条里折叠 10 条，只留最近 1 轮
        assert row.context_compacted_count == 10
        assert row.context_summary and "较早的第" in row.context_summary
        assert row.context_compacted_at is not None

    # 原始消息一条不少（对话记录 / 导出不受影响）
    detail = (await client.get(f"/api/conversations/{conversation['id']}")).json()
    assert len(detail["messages"]) == 14
    # 压缩后的用量回到阈值以下（展示与决策同一口径）
    view = (await client.get(f"/api/conversations/{conversation['id']}/context")).json()
    assert view["compacted_count"] == 10
    assert view["compaction"]["status"] == "ok"
    assert view["compaction"]["dropped"] == 10
    assert view["used_tokens"] < 1500

    # 还能继续对话（压缩没有把对话弄坏）
    async with client.stream(
        "POST", f"/api/conversations/{conversation['id']}/messages", json={"content": "再来一句"}
    ) as response:
        assert response.status_code == 200
        await read_sse(response)


async def test_compaction_failure_keeps_original_context(
    client: httpx.AsyncClient, monkeypatch
):
    """压缩失败不丢原上下文、不阻塞本轮，并在界面状态里留一条失败原因。"""
    await client.put("/api/system/context", json={"limit_tokens": 1500, "threshold_ratio": 0.8})
    agent = await make_agent(client)
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})
    ).json()
    async with SessionLocal() as session:
        session.add_all(
            [
                Message(
                    conversation_id=conversation["id"],
                    role="user" if index % 2 == 0 else "assistant",
                    content=f"历史 {index} " + "b" * 1000,
                )
                for index in range(12)
            ]
        )
        await session.commit()

    def boom(*args, **kwargs):
        raise RuntimeError("模拟压缩失败")

    monkeypatch.setattr(context_service, "compact_context", boom)

    async with client.stream(
        "POST", f"/api/conversations/{conversation['id']}/messages", json={"content": "下一句"}
    ) as response:
        assert response.status_code == 200
        events = await read_sse(response)

    texts = [e.get("text") or "" for e in events if e["type"] == "log"]
    assert any("压缩失败，已保留原上下文" in text for text in texts), texts
    assert events[-1]["type"] == "stream.closed"

    async with SessionLocal() as session:
        row = await session.get(Conversation, conversation["id"])
        assert row is not None
        assert row.context_compacted_count == 0
        assert row.context_summary is None

    view = (await client.get(f"/api/conversations/{conversation['id']}/context")).json()
    assert view["compaction"]["status"] == "error"
    assert "模拟压缩失败" in view["compaction"]["error"]

    # 12 条历史 + 本轮 2 条全部还在（失败不丢任何上下文）
    detail = (await client.get(f"/api/conversations/{conversation['id']}")).json()
    assert len(detail["messages"]) == 14


async def test_clear_session_also_clears_compaction_memory(client: httpx.AsyncClient):
    """`/clear` 清记忆时，自动压缩留下的摘要与折叠进度要一起清掉。"""
    agent = await make_agent(client)
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})
    ).json()
    async with SessionLocal() as session:
        row = await session.get(Conversation, conversation["id"])
        row.context_summary = "旧摘要"
        row.context_compacted_count = 6
        session.add(row)
        await session.commit()

    reset = await client.post(f"/api/conversations/{conversation['id']}/session/reset")
    assert reset.status_code == 200
    async with SessionLocal() as session:
        row = await session.get(Conversation, conversation["id"])
        assert row is not None
        assert row.context_summary is None
        assert row.context_compacted_count == 0


# —— 内核原生压缩（Claude Code auto-compact）的协同 ————————————————


async def test_kernel_compact_event_logs_and_invalidates_snapshot(
    client: httpx.AsyncClient, monkeypatch
):
    """内核压缩事件：日志提醒可见、时间戳落库、压缩前的大快照失效。"""
    agent = await make_agent(client)
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})
    ).json()
    async with SessionLocal() as session:
        session.add(
            Message(
                conversation_id=conversation["id"],
                role="user",
                content="你好",
            )
        )
        # 压缩前的旧快照：5 万 token（内核已折叠，这个数不再可信）。
        session.add(
            Message(
                conversation_id=conversation["id"],
                role="assistant",
                content="旧回复",
                context_tokens=50_000,
            )
        )
        await session.commit()

    async with SessionLocal() as session:
        conv = await session.get(Conversation, conversation["id"])
        rows = (
            await session.execute(
                select(Message).where(Message.conversation_id == conv.id)
            )
        ).scalars().all()
        user = next(r for r in rows if r.role == "user")
        assistant = next(r for r in rows if r.role == "assistant" and r.id != user.id)
        run = ActiveRun(id="run-kc", conversation_id=conv.id, agent_name="假智能体")
        prepared = runtime_service.PreparedRun(
            conversation=conv,
            profile=None,  # type: ignore[arg-type] - 本用例只验证事件落库
            gateway=None,
            definition=None,  # type: ignore[arg-type]
            ctx=None,  # type: ignore[arg-type]
            run=run,
            user_message=user,
            assistant_message=assistant,
        )

        async def fake_stream(definition, ctx, active_run):
            # 先压缩（窗口降到 2k），再上报压缩后的新快照。
            yield context_compacted(
                trigger="auto", pre_tokens=50_000, post_tokens=2_000, messages_summarized=18
            )
            yield context_usage(2_100)
            yield text_delta("好")

        monkeypatch.setattr(runtime_service, "stream_run", fake_stream)
        notices: list[str] = []
        async for event in runtime_service.consume_run(session, prepared):
            if event.type.value == "log":
                notices.append(event.text or "")
        assert any("内核自动压缩" in text for text in notices), notices

        await session.refresh(assistant)
        await session.refresh(conv)
        # post_tokens 成为最新快照；随后 context.usage 覆盖为 2100。
        assert assistant.context_tokens == 2_100
        assert conv.context_kernel_compacted_at is not None
        # session_id 没被清掉：内核压缩在会话内完成，与会话续接不冲突。
        assert conv.context_summary is None

    # 压缩时间戳让更早的旧快照失效：估算回落到「按内容估算」档。
    view = (await client.get(f"/api/conversations/{conversation['id']}/context")).json()
    assert view["measured"] is False
    assert view["used_tokens"] < 1000


async def test_kernel_compact_event_without_post_tokens_falls_back(
    client: httpx.AsyncClient, monkeypatch
):
    """内核没报 post_tokens 时：不落快照，只记时间戳与提醒。"""
    agent = await make_agent(client)
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})
    ).json()

    async with SessionLocal() as session:
        conv = await session.get(Conversation, conversation["id"])
        user = Message(conversation_id=conv.id, role="user", content="你好")
        assistant = Message(conversation_id=conv.id, role="assistant", content="")
        session.add_all([user, assistant])
        await session.commit()
        await session.refresh(assistant)

        run = ActiveRun(id="run-kc2", conversation_id=conv.id, agent_name="假智能体")
        prepared = runtime_service.PreparedRun(
            conversation=conv,
            profile=None,  # type: ignore[arg-type]
            gateway=None,
            definition=None,  # type: ignore[arg-type]
            ctx=None,  # type: ignore[arg-type]
            run=run,
            user_message=user,
            assistant_message=assistant,
        )

        async def fake_stream(definition, ctx, active_run):
            yield context_compacted(trigger="manual")
            yield text_delta("好")

        monkeypatch.setattr(runtime_service, "stream_run", fake_stream)
        async for _ in runtime_service.consume_run(session, prepared):
            pass

        await session.refresh(assistant)
        await session.refresh(conv)
        assert assistant.context_tokens is None
        assert conv.context_kernel_compacted_at is not None


def test_claude_code_adapter_parses_compact_boundary():
    """适配器把 Claude Code 的 system/compact_boundary 翻译成 context.compacted 事件。

    字段白名单生效：trigger / pre / post / messages_summarized 透传，
    内部遥测字段（precomputed、pre_compact_*）不进事件流。
    """
    from app.agents.builtin.claude_code import ClaudeCodeAgent
    from app.agents.base import AgentContext, AgentRun

    definition = ClaudeCodeAgent()
    run = AgentRun(definition, AgentContext(agent_id="claude-code", agent_name="Claude Code"))

    def events_for(line: str) -> list[AgentEvent]:
        return list(definition.parse_line(line, run))

    # 带 compact_metadata 的完整事件
    line = json.dumps(
        {
            "type": "system",
            "subtype": "compact_boundary",
            "compact_metadata": {
                "trigger": "auto",
                "pre_tokens": 150_000,
                "post_tokens": 20_000,
                "messages_summarized": 42,
                "duration_ms": 3_500,
                "precomputed": True,
                "pre_compact_discovered_tools": ["Bash", "Read"],
            },
        }
    )
    events = events_for(line)
    assert [e.type.value for e in events] == ["context.compacted"]
    payload = events[0].payload
    assert payload["trigger"] == "auto"
    assert payload["pre_tokens"] == 150_000
    assert payload["post_tokens"] == 20_000
    assert payload["messages_summarized"] == 42
    assert "precomputed" not in payload, "内部遥测字段不得透传"
    assert "pre_compact_discovered_tools" not in payload, "内部遥测字段不得透传"

    # 缺 compact_metadata 也能上報（各内核字段参差，事件不依赖任何字段必在）。
    bare = json.dumps({"type": "system", "subtype": "compact_boundary"})
    events = events_for(bare)
    assert [e.type.value for e in events] == ["context.compacted"]
    assert events[0].payload == {}


def test_claude_code_autocompact_flag_passthrough():
    """配置了 autocompact 才透传 --autocompact；未配置保持命令行不变。"""
    from app.agents.builtin.claude_code import ClaudeCodeAgent
    from app.agents.base import AgentContext

    definition = ClaudeCodeAgent()

    def argv_with(options: dict) -> list[str]:
        ctx = AgentContext(agent_id="claude-code", agent_name="Claude Code", options=options)
        return definition.build_invocation(ctx).argv

    argv = argv_with({"autocompact": "auto"})
    assert argv[argv.index("--autocompact") + 1] == "auto"

    argv = argv_with({"autocompact": "200k"})
    assert argv[argv.index("--autocompact") + 1] == "200k"

    argv = argv_with({})
    assert "--autocompact" not in argv

    # coerce_options 按字段声明规整：数字 200000 也能透传（CLI 认裸数字）。
    coerced = definition.coerce_options({"autocompact": 200000})
    argv = argv_with(coerced)
    assert argv[argv.index("--autocompact") + 1] == "200000"
