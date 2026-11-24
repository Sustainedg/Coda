"""任务进度卡片：协议解析、合并语义、持久化与失败降级。

设计见 ``docs/task-progress-card-design.md``。测试覆盖：

* 纯函数：任务判定阈值、三级容错解析、跨片段剥离、create/update 合并；
* 存储：一个对话一行、并发/幂等由 ``apply_block`` 内部保证、删对话级联删卡；
* 接口：详情返回 ``task_card``、正文不含协议块、没有客户端写路由。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
from sqlmodel import select

from app.db import SessionLocal, engine
from app.models import TaskCard
from app.runtime import task_cards as tc
from helpers import make_gateway, make_profile

FAKE = str(Path(__file__).parent / "fakes" / "fake_task_card_agent.py")
TASK_TEXT = "帮我实现任务进度卡片的后端与前端"


def tc_options(*args: str) -> dict:
    """指向任务卡替身内核的运行参数。"""
    return {
        "command": sys.executable,
        "args": [FAKE, *args],
        "prompt_mode": "stdin",
        "output_format": "jsonl",
        "text_path": "delta",
        "delta_mode": True,
        "session_path": "session_id",
    }


async def read_sse(response: httpx.Response) -> list[dict]:
    events: list[dict] = []
    async for line in response.aiter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


async def send(client: httpx.AsyncClient, conversation_id: str, content: str) -> list[dict]:
    response = await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": content}
    )
    assert response.status_code == 200, response.text
    return await read_sse(response)


async def make_task_agent(client: httpx.AsyncClient, *args: str) -> dict:
    gateway = await make_gateway(client)
    return await make_profile(client, gateway_id=gateway["id"], options=tc_options(*args))


async def new_conversation(client: httpx.AsyncClient, agent: dict) -> str:
    response = await client.post(
        "/api/conversations", json={"agent_profile_id": agent["id"]}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def fenced(payload: dict | str) -> str:
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"正文\n\n```task_card\n{body}\n```\n"


# --- 纯函数 -----------------------------------------------------------------


def test_looks_like_task_request_thresholds():
    assert tc.looks_like_task_request(TASK_TEXT)
    assert tc.looks_like_task_request("先排查这个 bug；再补测试")
    assert tc.looks_like_task_request("1. 第一步\n2. 第二步")
    # 不足 10 个字符：不建卡（「继续」「你好」这类短句）
    assert not tc.looks_like_task_request("实现" + "a" * 7)
    assert tc.looks_like_task_request("实现" + "a" * 8)
    assert not tc.looks_like_task_request("你好")
    assert not tc.looks_like_task_request("")


def test_strip_task_card_removes_closed_and_unclosed_blocks():
    closed = "正文\n\n" + fenced({"action": "create", "items": []}) + "尾巴"
    cleaned = tc.strip_task_card(closed)
    assert "task_card" not in cleaned
    assert "正文" in cleaned and "尾巴" in cleaned

    unclosed = '正文\n```task_card\n{"action":"create"'
    assert tc.strip_task_card(unclosed).strip() == "正文"


def test_strip_task_card_from_parts_handles_cross_part_fence():
    parts = [
        {"kind": "text", "text": 'hello\n```task_card\n{"action":'},
        {"kind": "thinking", "text": "思考"},
        {"kind": "text", "text": '"create"}\n```\nafter'},
    ]
    cleaned = tc.strip_task_card_from_parts(parts)
    texts = [part.get("text") or "" for part in cleaned if part.get("kind") == "text"]
    assert any("hello" in text for text in texts)
    assert any("after" in text for text in texts)
    assert all("task_card" not in text for text in texts)
    # 非 text 片段原样保留
    assert any(part.get("kind") == "thinking" and part.get("text") == "思考" for part in cleaned)


def test_parse_task_card_valid_create_and_update():
    block, error = tc.parse_task_card(
        fenced(
            {
                "action": "create",
                "title": "实现进度卡",
                "items": [
                    {"id": "t1", "text": "后端"},
                    {"id": "t2", "text": "前端"},
                ],
            }
        )
    )
    assert error is None
    assert block is not None and block["action"] == "create"
    assert block["title"] == "实现进度卡"
    assert [item["id"] for item in block["items"]] == ["t1", "t2"]

    block, error = tc.parse_task_card(
        fenced({"action": "update", "items": [{"id": "t1", "done": True}]})
    )
    assert error is None
    assert block == {"action": "update", "title": "", "items": [{"id": "t1", "done": True}]}


def test_parse_task_card_without_block_is_not_an_error():
    assert tc.parse_task_card("这只是一段普通回答。") == (None, None)


def test_parse_task_card_bad_json_returns_error():
    block, error = tc.parse_task_card("```task_card\n{not json\n```")
    assert block is None
    assert error


def test_parse_task_card_repairs_truncated_output():
    truncated = (
        "```task_card\n"
        '{"action":"create","title":"x","items":['
        '{"id":"t1","text":"a"},{"id":"t2","text":"b"}'
    )
    block, error = tc.parse_task_card(truncated)
    assert error is None
    assert block is not None and len(block["items"]) == 2


def test_parse_task_card_uses_last_block_and_dedupes():
    first = {"action": "create", "items": [{"id": "a", "text": "旧"}, {"id": "b", "text": "旧"}]}
    second = {
        "action": "create",
        "items": [
            {"id": "t1", "text": " 甲 "},
            {"id": "t1", "text": "乙"},
            {"id": "t2", "text": ""},
            {"id": "t3", "text": "很长的任务" * 40},
        ],
    }
    text = fenced(first) + "\n中间\n" + fenced(second)
    block, error = tc.parse_task_card(text)
    assert error is None
    assert block is not None
    # 重复 id 按最后一次为准；空文本被丢弃；超长截断
    assert block["items"][0]["text"] == "乙"
    assert len(block["items"]) == 2
    assert len(block["items"][1]["text"]) == tc.MAX_ITEM_CHARS


def test_parse_task_card_item_count_boundaries():
    one, error = tc.parse_task_card(
        fenced({"action": "create", "items": [{"id": "t1", "text": "唯一"}]})
    )
    assert one is None and "不足" in (error or "")

    two, error = tc.parse_task_card(
        fenced(
            {
                "action": "create",
                "items": [{"id": "t1", "text": "a"}, {"id": "t2", "text": "b"}],
            }
        )
    )
    assert two is not None and error is None

    twenty = {"action": "create", "items": [{"id": f"t{i}", "text": str(i)} for i in range(20)]}
    block, error = tc.parse_task_card(fenced(twenty))
    assert block is not None and len(block["items"]) == 20 and error is None

    twentyone = {
        "action": "create",
        "items": [{"id": f"t{i}", "text": str(i)} for i in range(21)],
    }
    block, error = tc.parse_task_card(fenced(twentyone))
    assert block is None and error


def test_merge_create_replan_keeps_done_and_update_is_monotonic():
    existing = [
        {"id": "t1", "text": "后端", "done": True, "done_at": "2026-01-01T00:00:00+00:00", "updated_at": "x"},
        {"id": "t2", "text": "前端", "done": False, "done_at": None, "updated_at": "x"},
    ]
    block = {
        "action": "create",
        "title": "新计划",
        "items": [
            {"id": "t1", "text": "后端（改）", "done": False},
            {"id": "t3", "text": "联调", "done": False},
        ],
    }
    merged = tc.merge_block(existing, "旧计划", block, now="2026-02-02T00:00:00+00:00")
    assert merged is not None and merged["changed"] is True
    assert merged["title"] == "新计划"
    # 已完成的 t1 不会因重规划被抹掉；t2 被移除；t3 新增
    assert [item["id"] for item in merged["items"]] == ["t1", "t3"]
    assert merged["items"][0]["done"] is True

    # update：false → true 允许；true → false 忽略；未知 id 忽略
    block = {
        "action": "update",
        "items": [
            {"id": "t2", "done": True},
            {"id": "t1", "done": False},
            {"id": "nope", "done": True},
        ],
    }
    merged = tc.merge_block(existing, "旧计划", block, now="2026-03-03T00:00:00+00:00")
    assert merged is not None and merged["changed"] is True
    assert merged["items"][0]["done"] is True  # 不会被改回未完成
    assert merged["items"][1]["done"] is True

    # 没有任何变化：不写库（changed == False）
    merged = tc.merge_block(existing, "旧计划", {"action": "update", "items": []}, now="t")
    assert merged is not None and merged["changed"] is False

    # update 不能凭空造卡
    assert tc.merge_block([], "", {"action": "update", "items": [{"id": "t1", "done": True}]}, now="t") is None


# --- 存储与接口 --------------------------------------------------------------


async def test_task_cards_table_has_expected_columns(client: httpx.AsyncClient):
    async with engine.begin() as conn:
        columns = {
            row[1] for row in (await conn.exec_driver_sql("PRAGMA table_info(task_cards)")).all()
        }
    assert {
        "id",
        "conversation_id",
        "anchor_message_id",
        "title",
        "items",
        "version",
        "last_run_id",
        "created_at",
        "updated_at",
    } <= columns


async def test_create_then_update_and_protocol_is_stripped(client: httpx.AsyncClient):
    agent = await make_task_agent(client)
    conversation_id = await new_conversation(client, agent)

    await send(client, conversation_id, TASK_TEXT)
    detail = (await client.get(f"/api/conversations/{conversation_id}")).json()
    card = detail["task_card"]
    assert card is not None
    assert card["version"] == 1
    assert card["title"] == "实现进度卡"
    assert [item["text"] for item in card["items"]] == ["后端存储与接口", "前端卡片组件"]
    assert all(item["done"] is False for item in card["items"])
    # 卡片锚在创建它那一轮的助手消息上
    assert card["anchor_message_id"] == detail["messages"][-1]["id"]
    # 协议块不能出现在落库的消息正文 / parts 里
    assert "task_card" not in detail["messages"][-1]["content"]
    assert "task_card" not in json.dumps(detail["messages"][-1]["parts"], ensure_ascii=False)

    # 第二轮：已有卡，模型只报状态变化 → 勾选 t1，版本递增，仍然只有一行
    await send(client, conversation_id, "继续")
    detail = (await client.get(f"/api/conversations/{conversation_id}")).json()
    card = detail["task_card"]
    assert card is not None
    assert card["version"] == 2
    assert card["items"][0]["done"] is True
    assert card["items"][0]["done_at"]
    assert card["items"][1]["done"] is False
    async with SessionLocal() as session:
        rows = (await session.execute(select(TaskCard))).scalars().all()
    assert len(rows) == 1


async def test_apply_block_is_idempotent_and_keeps_one_row(client: httpx.AsyncClient):
    """同一运行重复应用不重复推进；没有语义变化不 bump version；update 只改勾选。"""
    agent = await make_task_agent(client)
    conversation_id = await new_conversation(client, agent)

    create = {
        "action": "create",
        "title": "卡片",
        "items": [
            {"id": "t1", "text": "甲", "done": False},
            {"id": "t2", "text": "乙", "done": False},
        ],
    }
    first = await tc.apply_block(conversation_id, None, create, "run-1")
    assert first is not None and first.version == 1

    # 同一 run 重放：幂等跳过
    replay = await tc.apply_block(conversation_id, None, create, "run-1")
    assert replay is not None and replay.version == 1

    # 不同 run 但内容完全相同：没有语义变化，不写库、不 bump
    same = await tc.apply_block(conversation_id, None, create, "run-2")
    assert same is not None and same.version == 1

    # 只报变化的 update：done 单向推进，版本 +1
    updated = await tc.apply_block(
        conversation_id,
        None,
        {"action": "update", "items": [{"id": "t1", "done": True}]},
        "run-3",
    )
    assert updated is not None and updated.version == 2
    assert updated.items[0]["done"] is True
    assert updated.items[1]["done"] is False

    # update 不能凭空造卡
    assert await tc.apply_block("not-a-conversation", None, {"action": "update", "items": [{"id": "x", "done": True}]}, "run-x") is None


async def test_chit_chat_does_not_create_card(client: httpx.AsyncClient):
    agent = await make_task_agent(client)
    conversation_id = await new_conversation(client, agent)

    await send(client, conversation_id, "你好呀")
    detail = (await client.get(f"/api/conversations/{conversation_id}")).json()
    assert detail["task_card"] is None


async def test_bad_block_degrades_to_no_card_and_no_leak(client: httpx.AsyncClient):
    agent = await make_task_agent(client, "bad")
    conversation_id = await new_conversation(client, agent)

    await send(client, conversation_id, TASK_TEXT)
    detail = (await client.get(f"/api/conversations/{conversation_id}")).json()
    assert detail["task_card"] is None
    # 坏块仍然被剥掉，用户看不到协议 JSON
    assert "task_card" not in json.dumps(detail["messages"], ensure_ascii=False)


async def test_delete_conversation_cascades_card(client: httpx.AsyncClient):
    agent = await make_task_agent(client)
    conversation_id = await new_conversation(client, agent)
    await send(client, conversation_id, TASK_TEXT)

    assert (await client.delete(f"/api/conversations/{conversation_id}")).status_code == 204
    async with SessionLocal() as session:
        rows = (await session.execute(select(TaskCard))).scalars().all()
    assert rows == []


async def test_no_client_write_route(client: httpx.AsyncClient):
    agent = await make_task_agent(client)
    conversation_id = await new_conversation(client, agent)

    for method in ("POST", "PATCH", "DELETE"):
        response = await client.request(
            method, f"/api/conversations/{conversation_id}/task-card", json={"items": []}
        )
        assert response.status_code in (404, 405), response.text
