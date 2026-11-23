"""上下文窗口：近似估算、上限配置持久化与校验、接近上限自动压缩。

口径的唯一来源是 ``app.runtime.context.estimate_tokens``（展示 / 阈值 / 压缩决策共用）；
这里既验算它，也验接口与运行层的落盘行为。
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.db import SessionLocal
from app.models import Conversation, Message
from app.runtime import context as context_service

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
