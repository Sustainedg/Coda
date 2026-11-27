"""用量接口：token 归一化汇总、技能调用统计与时间范围过滤。"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from app.db import SessionLocal
from app.models import Conversation, Message, Skill
from app.runtime import usage_ledger
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
    # 统一口径后「输入」是普通输入（不含缓存）：
    #   Claude 分离式 = 100；OpenAI 包含式 = 200 - 100(缓存) = 100。
    assert tokens["input"] == 200
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


# ---------------------------------------------------------------------------
# P2：账本优先 + 旧消息分段
# ---------------------------------------------------------------------------


async def test_overview_segments_ledger_and_legacy_without_double_count(client: httpx.AsyncClient):
    """账本启用前读旧消息、之后读账本；启用后的消息即使带 usage 也不重复计。"""
    profile = await make_profile(client)
    conversation_id = await _seed_conversation(
        profile["id"],
        [
            {
                "usage": {"input_tokens": 100, "output_tokens": 10},
                "created_at": datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc),
            },
            # 账本启用后的消息：token 应由账本段负责，不能在这里再算一次。
            {
                "usage": {"input_tokens": 999, "output_tokens": 999},
                "created_at": datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc),
            },
        ],
    )
    async with SessionLocal() as session:
        await usage_ledger.mark_ledger_started(
            session, when=datetime(2026, 11, 20, tzinfo=timezone.utc)
        )
        await usage_ledger.record(
            session,
            usage_ledger.build_record(
                run_id="ledger-run", request_id="req-1",
                source=usage_ledger.SOURCE_GATEWAY,
                counts=usage_ledger.normalize_usage({"input_tokens": 40, "output_tokens": 5}),
                conversation_id=conversation_id,
                agent_profile_id=profile["id"],
                created_at=datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc),
            ),
        )
        await session.commit()

    data = (await client.get("/api/usage/overview")).json()
    # 旧消息 100+10 + 账本 40+5；999 那条被跳过。
    assert data["tokens"]["messages"] == 2
    assert data["tokens"]["input"] == 140
    assert data["tokens"]["output"] == 15
    assert data["tokens"]["total"] == 155
    assert data["by_source"]["legacy"]["input"] == 100
    assert data["by_source"]["gateway"]["input"] == 40


async def test_usage_records_endpoint_filters_and_validates(client: httpx.AsyncClient):
    profile = await make_profile(client)
    conversation_id = await _seed_conversation(profile["id"], [])
    async with SessionLocal() as session:
        await usage_ledger.mark_ledger_started(
            session, when=datetime(2026, 11, 1, tzinfo=timezone.utc)
        )
        for index, source in enumerate(("gateway", "native", "legacy")):
            await usage_ledger.record(
                session,
                usage_ledger.build_record(
                    run_id=f"run-{index}", request_id=f"req-{index}", source=source,
                    counts=usage_ledger.normalize_usage({"input_tokens": 10 * (index + 1)}),
                    conversation_id=conversation_id,
                    created_at=datetime(2026, 11, 10 + index, tzinfo=timezone.utc),
                ),
            )
        await session.commit()

    everything = (await client.get("/api/usage/records")).json()
    assert len(everything["records"]) == 3
    assert everything["has_more"] is False

    gateway = (await client.get("/api/usage/records", params={"source": "gateway"})).json()
    assert len(gateway["records"]) == 1
    assert gateway["records"][0]["source"] == "gateway"
    assert gateway["records"][0]["input_tokens"] == 10

    bad = await client.get("/api/usage/records", params={"source": "nope"})
    assert bad.status_code == 422


async def test_ledger_survives_conversation_deletion(client: httpx.AsyncClient):
    """账本是权威源：删对话 / 删消息后，已记的用量仍要能看到。"""
    profile = await make_profile(client)
    conversation_id = await _seed_conversation(profile["id"], [])
    async with SessionLocal() as session:
        await usage_ledger.mark_ledger_started(
            session, when=datetime(2026, 11, 1, tzinfo=timezone.utc)
        )
        await usage_ledger.record(
            session,
            usage_ledger.build_record(
                run_id="del-run", request_id="req-del",
                source=usage_ledger.SOURCE_GATEWAY,
                counts=usage_ledger.normalize_usage({"input_tokens": 77}),
                conversation_id=conversation_id,
                created_at=datetime(2026, 11, 10, tzinfo=timezone.utc),
            ),
        )
        await session.commit()

    async with SessionLocal() as session:
        await session.delete(await session.get(Conversation, conversation_id))
        await session.commit()

    data = (await client.get("/api/usage/overview")).json()
    assert data["tokens"]["input"] == 77
    # 对话已删除：标题不可见，但账本行仍按归属进入榜单。
    assert data["by_conversation"][0]["title"] == "（已删除的对话）"


async def test_pricing_crud_and_csv_export(client: httpx.AsyncClient):
    profile = await make_profile(client)
    conversation_id = await _seed_conversation(profile["id"], [])
    created = await client.post(
        "/api/usage/pricing",
        json={"model_pattern": "claude-x", "input_per_million": 3, "output_per_million": 15},
    )
    assert created.status_code == 200, created.text
    listed = (await client.get("/api/usage/pricing")).json()["pricing"]
    assert len(listed) == 1 and listed[0]["model_pattern"] == "claude-x"

    async with SessionLocal() as session:
        await usage_ledger.mark_ledger_started(
            session, when=datetime(2026, 11, 1, tzinfo=timezone.utc)
        )
        await usage_ledger.record(
            session,
            usage_ledger.build_record(
                run_id="csv-run", request_id="r1",
                source=usage_ledger.SOURCE_GATEWAY,
                counts=usage_ledger.normalize_usage({"input_tokens": 12, "output_tokens": 3}),
                conversation_id=conversation_id,
                model="claude-x",
                created_at=datetime(2026, 11, 10, tzinfo=timezone.utc),
            ),
        )
        await session.commit()

    export = await client.get("/api/usage/records/export.csv")
    assert export.status_code == 200
    assert "text/csv" in export.headers["content-type"]
    body = export.text.lstrip("\ufeff")
    assert body.startswith("created_at,source,origin")
    assert "csv-run" in body

    deleted = await client.delete(f"/api/usage/pricing/{created.json()['id']}")
    assert deleted.status_code == 200
    assert (await client.get("/api/usage/pricing")).json()["pricing"] == []


async def test_overview_reports_cost_and_coverage(client: httpx.AsyncClient):
    """成本只累加已定价行；覆盖率区分 estimated / unknown（历史消息算 unknown）。"""
    profile = await make_profile(client)
    conversation_id = await _seed_conversation(
        profile["id"],
        [
            {
                "usage": {"input_tokens": 100},
                "created_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
            }
        ],
    )
    async with SessionLocal() as session:
        await usage_ledger.mark_ledger_started(
            session, when=datetime(2026, 11, 1, tzinfo=timezone.utc)
        )
        await usage_ledger.record(
            session,
            usage_ledger.build_record(
                run_id="cost-a", request_id="a",
                source=usage_ledger.SOURCE_GATEWAY,
                counts=usage_ledger.normalize_usage({"input_tokens": 10}),
                conversation_id=conversation_id,
                cost_usd=0.25,
                cost_source=usage_ledger.COST_ESTIMATED,
                created_at=datetime(2026, 11, 10, tzinfo=timezone.utc),
            ),
        )
        await usage_ledger.record(
            session,
            usage_ledger.build_record(
                run_id="cost-b", request_id="b",
                source=usage_ledger.SOURCE_NATIVE,
                counts=usage_ledger.normalize_usage({"input_tokens": 20}),
                conversation_id=conversation_id,
                created_at=datetime(2026, 11, 11, tzinfo=timezone.utc),
            ),
        )
        await session.commit()

    data = (await client.get("/api/usage/overview")).json()
    assert data["cost_usd"] == 0.25
    assert data["cost_coverage"]["estimated"] == 1
    assert data["cost_coverage"]["unknown"] == 2  # 历史消息 1 + 未定价账本行 1
