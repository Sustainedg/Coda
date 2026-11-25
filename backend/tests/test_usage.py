"""用量接口：token 归一化汇总、技能调用统计与时间范围过滤。"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from app.db import SessionLocal
from app.models import Conversation, Message, Skill
from helpers import make_gateway, make_profile


async def _seed_conversation(profile_id: str, entries: list[dict]) -> str:
    """直接落一条对话与若干条助手消息（不跑智能体，只造统计原料）。"""
    async with SessionLocal() as session:
        conversation = Conversation(agent_profile_id=profile_id, title="用量测试")
        session.add(conversation)
        await session.flush()
        for index, entry in enumerate(entries):
            session.add(
                Message(
                    conversation_id=conversation.id,
                    role="assistant",
                    content=entry.get("content", f"回复 {index}"),
                    usage=entry.get("usage", {}),
                    parts=entry.get("parts", []),
                    created_at=entry.get(
                        "created_at", datetime(2026, 11, 10 + index, 12, 0, tzinfo=timezone.utc)
                    ),
                )
            )
        await session.commit()
        return conversation.id


def _skill_part(slug: str) -> dict:
    """一条「调用技能」的工具调用 part（Claude Code 的 ``Skill`` 工具）。"""
    return {"kind": "tool_call", "id": f"call-{slug}", "name": "Skill", "input": {"skill": slug}}


async def test_usage_overview_normalises_tokens_across_field_names(client: httpx.AsyncClient):
    """Claude Code 的分离式记法与 OpenAI 的包含式记法要汇到同一组字段。"""
    profile = await make_profile(client)
    await _seed_conversation(
        profile["id"],
        [
            # Claude Code：缓存与 input_tokens 分离
            {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 40,
                    "cache_read_input_tokens": 900,
                    "cache_creation_input_tokens": 100,
                }
            },
            # OpenAI 兼容：prompt/completion + cached_tokens
            {
                "usage": {
                    "prompt_tokens": 200,
                    "completion_tokens": 50,
                    "total_tokens": 250,
                    "cached_tokens": 100,
                }
            },
        ],
    )

    data = (await client.get("/api/usage/overview")).json()
    tokens = data["tokens"]

    assert tokens["messages"] == 2
    assert tokens["input"] == 300
    assert tokens["output"] == 90
    assert tokens["cache_read"] == 1000
    assert tokens["cache_creation"] == 100
    assert tokens["total"] == 1140 + 250  # 分离式那条＝100+40+900+100
    # 分母：分离式 = 100+900+100 = 1100，包含式 = 200，合计 1300
    assert tokens["cache_ratio"] == 1000 / 1300

    assert data["conversations"] == 1
    assert len(data["by_agent"]) == 1
    assert data["by_agent"][0]["name"] == profile["name"]
    assert data["by_conversation"][0]["tokens"]["total"] == 1390
    assert [day["date"] for day in data["daily"]] == ["2026-11-10", "2026-11-11"]


async def test_usage_overview_ignores_messages_without_usage(client: httpx.AsyncClient):
    """没有上报计数的助手消息（空 usage）不参与统计，也不虚增对话数。"""
    profile = await make_profile(client)
    await _seed_conversation(profile["id"], [{}])

    data = (await client.get("/api/usage/overview")).json()
    assert data["tokens"]["messages"] == 0
    assert data["tokens"]["cache_ratio"] is None
    assert data["conversations"] == 0
    assert data["by_agent"] == []


async def test_usage_overview_counts_skill_calls(client: httpx.AsyncClient):
    """技能用量＝调用次数：从消息 parts 里的 ``Skill`` 工具调用数出来（不是分配次数）。"""
    profile = await make_profile(client, type_id="claude-code", options={})
    async with SessionLocal() as session:
        session.add(Skill(id="claude-code:dev-god", type_id="claude-code", slug="dev-god", name="编程大神"))
        await session.commit()

    await _seed_conversation(
        profile["id"],
        [
            {"usage": {"input_tokens": 10, "output_tokens": 5},
             "parts": [_skill_part("dev-god"), _skill_part("dev-god")]},
            {"usage": {"input_tokens": 10, "output_tokens": 5},
             "parts": [{"kind": "tool_call", "id": "c", "name": "Bash", "input": {"command": "ls"}}]},
        ],
    )

    skills = (await client.get("/api/usage/overview")).json()["skills"]
    assert skills["calls"] == 2
    assert skills["skills_used"] == 1
    assert skills["top"][0]["id"] == "claude-code:dev-god"
    assert skills["top"][0]["name"] == "编程大神"
    assert skills["top"][0]["calls"] == 2


async def test_usage_overview_only_counts_declared_skill_invocations(client: httpx.AsyncClient):
    """未声明「技能调用」识别方式的内核不会被计入（默认 ``skill_invocation`` 返回 None）。"""
    profile = await make_profile(client)  # custom-cli：不识别技能调用
    await _seed_conversation(
        profile["id"],
        [{"usage": {"prompt_tokens": 1, "completion_tokens": 1}, "parts": [_skill_part("dev-god")]}],
    )

    skills = (await client.get("/api/usage/overview")).json()["skills"]
    assert skills["calls"] == 0
    assert skills["top"] == []


async def test_usage_overview_filters_by_time_range(client: httpx.AsyncClient):
    """start / end 只保留窗口内的消息（end 为开区间），token 与技能调用一起被过滤。"""
    profile = await make_profile(client, type_id="claude-code", options={})
    await _seed_conversation(
        profile["id"],
        [
            {"created_at": datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc),
             "usage": {"input_tokens": 100, "output_tokens": 0}, "parts": [_skill_part("dev-god")]},
            {"created_at": datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc),
             "usage": {"input_tokens": 200, "output_tokens": 0}, "parts": [_skill_part("dev-god")]},
            {"created_at": datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc),
             "usage": {"input_tokens": 400, "output_tokens": 0}},
        ],
    )

    query = "start=2026-11-11T00:00:00Z&end=2026-11-12T00:00:00Z"
    data = (await client.get(f"/api/usage/overview?{query}")).json()
    assert data["tokens"]["messages"] == 1
    assert data["tokens"]["input"] == 200
    assert data["skills"]["calls"] == 1
    assert data["daily"] == [{"date": "2026-11-11", "input": 200, "output": 0, "total": 200}]


async def test_usage_overview_rejects_a_bad_range(client: httpx.AsyncClient):
    """时间点解析不了就明确报 400，不静默当成「全部时间」。"""
    response = await client.get("/api/usage/overview?start=昨天")
    assert response.status_code == 400
    assert "昨天" in response.json()["detail"]


async def test_usage_overview_keeps_name_of_a_soft_deleted_agent(client: httpx.AsyncClient):
    """智能体被软删除后，历史对话的用量仍归属它、并继续显示当时的名字。"""
    gateway = await make_gateway(client)
    profile = await make_profile(client, gateway_id=gateway["id"])
    await _seed_conversation(
        profile["id"], [{"usage": {"prompt_tokens": 10, "completion_tokens": 5}}]
    )
    assert (await client.delete(f"/api/agents/{profile['id']}")).status_code == 204

    data = (await client.get("/api/usage/overview")).json()
    assert data["tokens"]["total"] == 15
    assert data["by_agent"][0]["name"] == profile["name"]


async def test_usage_overview_filters_by_agent(client: httpx.AsyncClient):
    """agent_id 只统计这一个智能体：智能体设定页「统计」标签的数据源。"""
    profile_a = await make_profile(client, name="智能体A")
    profile_b = await make_profile(client, name="智能体B")
    await _seed_conversation(
        profile_a["id"], [{"usage": {"input_tokens": 30, "output_tokens": 10}}]
    )
    await _seed_conversation(
        profile_b["id"], [{"usage": {"input_tokens": 70, "output_tokens": 20}}]
    )

    data = (
        await client.get(f"/api/usage/overview?agent_id={profile_a['id']}")
    ).json()
    assert data["tokens"]["input"] == 30
    assert data["tokens"]["total"] == 40
    assert [item["id"] for item in data["by_agent"]] == [profile_a["id"]]
    assert data["conversations"] == 1

    # 不传 agent_id 仍是平台 / 用户全量口径（回归）。
    total = (await client.get("/api/usage/overview")).json()
    assert total["tokens"]["input"] == 100


async def test_usage_overview_agent_filter_rejects_unknown_agent(client: httpx.AsyncClient):
    """agent_id 不存在（或已软删除）时明确 422，不静默当成「没有用量」。"""
    response = await client.get("/api/usage/overview?agent_id=不存在的智能体")
    assert response.status_code == 422
    assert "不存在" in response.json()["detail"]
