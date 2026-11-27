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
    # 明确提到任务卡 / 进度时，短句也要注入协议：否则模型会改用内核自带的任务工具。
    assert tc.looks_like_task_request("看下任务卡")
    assert tc.looks_like_task_request("任务进度卡")
    assert tc.looks_like_task_request("进度")
    assert tc.looks_like_task_request("待办")


def test_strip_task_card_removes_closed_and_unclosed_blocks():
    closed = "正文\n\n" + fenced({"action": "create", "items": []}) + "尾巴"
    cleaned = tc.strip_task_card(closed)
    assert "task_card" not in cleaned
    assert "正文" in cleaned and "尾巴" in cleaned

    unclosed = '正文\n```task_card\n{"action":"create"'
    assert tc.strip_task_card(unclosed).strip() == "正文"


def test_strip_task_card_accepts_json_fence_and_bare_json_at_tail():
    """模型把块写成 ```json / 裸 JSON 时也要剥掉（实测踩过的坑）。"""
    payload = {
        "action": "create",
        "title": "实现进度卡",
        "items": [{"id": "t1", "text": "后端"}, {"id": "t2", "text": "前端"}],
    }
    body = json.dumps(payload, ensure_ascii=False)

    json_fenced = f"正文\n\n```json\n{body}\n```\n"
    cleaned = tc.strip_task_card(json_fenced)
    assert cleaned == "正文"
    assert '"action"' not in cleaned

    bare = f"正文\n\n{body}"
    assert tc.strip_task_card(bare) == "正文"

    # 收尾块后面只剩空行也算收尾
    assert tc.strip_task_card(f"正文\n\n```json\n{body}\n```\n\n").strip() == "正文"


def test_strip_task_card_keeps_mid_document_json_example():
    """正文中段举例用的 JSON 代码块不能被误吞（只有收尾处才认）。"""
    payload = json.dumps(
        {"action": "create", "title": "示例", "items": [{"id": "t1", "text": "甲"}]},
        ensure_ascii=False,
    )
    text = f"协议长这样：\n\n```json\n{payload}\n```\n\n以上只是示例，本轮不建卡。"
    assert tc.strip_task_card(text) == text
    # 中段裸 JSON 同理
    bare_mid = f'示例 {payload} 仅作说明。'
    assert tc.strip_task_card(bare_mid) == bare_mid


def test_strip_task_card_from_parts_strips_json_fence_tail():
    parts = [
        {"kind": "thinking", "text": "思考"},
        {"kind": "text", "text": '结论在这里。\n\n```json\n{"action":"create","items":[{"id":"t1","text":"甲"}]}\n```'},
    ]
    cleaned = tc.strip_task_card_from_parts(parts)
    texts = [part.get("text") or "" for part in cleaned if part.get("kind") == "text"]
    assert texts == ["结论在这里。"]
    assert any(part.get("kind") == "thinking" for part in cleaned)


def test_strip_task_card_keeps_tail_json_that_is_not_a_card():
    """收尾处的普通 JSON（items 不是结构化任务项）不能被当成协议块吞掉。"""
    config = '配置示例：\n\n```json\n{"items": ["a", "b"]}\n```'
    assert tc.parse_task_card(config) == (None, None)
    assert tc.strip_task_card(config).strip() == config.strip()

    named = '配置示例：\n\n{"items": [{"name": "x"}, {"name": "y"}]}'
    assert tc.parse_task_card(named) == (None, None)
    assert tc.strip_task_card(named).strip() == named.strip()


def test_strip_task_card_from_parts_only_strips_final_text_part():
    """时间线以工具结果收尾时，前一段 text 不是回复结尾，不能被剥。"""
    text = '示例：\n\n{"action":"create","items":[{"id":"t1","text":"甲"}]}'
    parts = [
        {"kind": "text", "text": text},
        {"kind": "tool_result", "id": "c1", "output": "ok"},
    ]
    cleaned = tc.strip_task_card_from_parts(parts)
    assert cleaned[0]["text"] == text
    assert cleaned[1]["kind"] == "tool_result"


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
    assert block == {
        "action": "update",
        "title": "",
        "items": [{"id": "t1", "done": True, "status": "completed"}],
    }


def test_parse_task_card_accepts_json_fence_and_bare_json_at_tail():
    """```json / 裸 JSON 收尾块同样要能解析出卡片（模型不按协议写标签的现实容错）。"""
    payload = {
        "action": "create",
        "title": "实现进度卡",
        "items": [{"id": "t1", "text": "后端"}, {"id": "t2", "text": "前端"}],
    }
    body = json.dumps(payload, ensure_ascii=False)

    block, error = tc.parse_task_card(f"正文\n\n```json\n{body}\n```\n")
    assert error is None
    assert block is not None and block["title"] == "实现进度卡"

    block, error = tc.parse_task_card(f"正文\n\n{body}")
    assert error is None
    assert block is not None and [item["id"] for item in block["items"]] == ["t1", "t2"]

    # 中段示例不算协议块
    mid = f"示例：\n\n```json\n{body}\n```\n\n以上只是示例。"
    assert tc.parse_task_card(mid) == (None, None)


def test_three_state_parse_and_public_view():
    block, error = tc.parse_task_card(
        fenced(
            {
                "action": "create",
                "title": "三态",
                "items": [
                    {"id": "t1", "text": "待办", "status": "pending"},
                    {"id": "t2", "text": "进行中", "status": "in_progress"},
                    {"id": "t3", "text": "已完成", "status": "completed"},
                ],
            }
        )
    )
    assert error is None and block is not None
    assert [(i["status"], i["done"]) for i in block["items"]] == [
        ("pending", False),
        ("in_progress", False),
        ("completed", True),
    ]

    block, error = tc.parse_task_card(
        fenced(
            {
                "action": "update",
                "items": [{"id": "t1", "status": "in_progress"}],
            }
        )
    )
    assert error is None
    assert block["items"] == [{"id": "t1", "status": "in_progress", "done": False}]

    # 旧协议：done:true 等价 completed；done:false 是 no-op（不带 status）。
    block, _ = tc.parse_task_card(
        fenced({"action": "update", "items": [{"id": "t1", "done": True}]})
    )
    assert block["items"][0]["status"] == "completed"
    block, _ = tc.parse_task_card(
        fenced({"action": "update", "items": [{"id": "t1", "done": False}]})
    )
    assert block["items"][0] == {"id": "t1", "done": False}

    bad, error = tc.parse_task_card(
        fenced(
            {
                "action": "update",
                "items": [{"id": "t1", "status": "blocked"}],
            }
        )
    )
    assert bad is None and "status" in (error or "")

    # 旧卡片行只有 done：读取时补 status，不改库。
    assert tc.public_items(
        [{"id": "t1", "text": "甲", "done": True}, {"id": "t2", "text": "乙"}]
    ) == [
        {"id": "t1", "text": "甲", "done": True, "status": "completed"},
        {"id": "t2", "text": "乙", "done": False, "status": "pending"},
    ]


def test_three_state_merge_allows_non_terminal_changes():
    existing = [
        {"id": "t1", "text": "甲", "done": False},
        {"id": "t2", "text": "乙", "done": True, "done_at": "old"},
        {"id": "t3", "text": "丙", "done": False},
    ]
    merged = tc.merge_block(
        existing,
        "旧计划",
        {
            "action": "update",
            "items": [
                {"id": "t1", "status": "in_progress"},
                # completed 是终态：显式想改回 in_progress 必须被忽略
                {"id": "t2", "status": "in_progress"},
                {"id": "t3", "status": "completed"},
            ],
        },
        now="now",
    )
    assert merged is not None and merged["changed"] is True
    by_id = {item["id"]: item for item in merged["items"]}
    assert by_id["t1"]["status"] == "in_progress" and by_id["t1"]["done"] is False
    assert by_id["t2"]["status"] == "completed" and by_id["t2"]["done_at"] == "old"
    assert by_id["t3"]["status"] == "completed" and by_id["t3"]["done_at"] == "now"

    # pending/in_progress 之间允许来回切换
    back = tc.merge_block(
        merged["items"],
        "旧计划",
        {"action": "update", "items": [{"id": "t1", "status": "pending"}]},
        now="later",
    )
    assert back is not None
    assert {item["id"]: item["status"] for item in back["items"]}["t1"] == "pending"


def test_three_state_create_replan_keeps_completed():
    existing = [
        {"id": "t1", "text": "甲", "status": "completed", "done": True, "done_at": "old"},
        {"id": "t3", "text": "丙", "status": "pending", "done": False},
    ]
    merged = tc.merge_block(
        existing,
        "旧计划",
        {
            "action": "create",
            "title": "新计划",
            "items": [
                {"id": "t1", "text": "甲（改）", "status": "pending"},
                {"id": "t2", "text": "乙", "status": "in_progress"},
            ],
        },
        now="now",
    )
    assert merged is not None
    assert [item["id"] for item in merged["items"]] == ["t1", "t2"]
    assert merged["items"][0]["status"] == "completed"  # 已完成不因重规划回退
    assert merged["items"][1]["status"] == "in_progress"
    assert merged["title"] == "新计划"


def test_item_status_derivation_is_identical_in_schema_and_runtime():
    """三态推导有两份实现：`schemas.TaskCardItemRead`（叶子层，不能反向依赖 runtime）
    与 `runtime.task_cards.public_items`（给提示词 / 内部接口）。两份必须同语义。"""
    from app.schemas import TaskCardItemRead

    cases = [
        {"id": "t1", "text": "a", "done": True},
        {"id": "t1", "text": "a", "done": False},
        {"id": "t1", "text": "a", "status": "pending"},
        {"id": "t1", "text": "a", "status": "in_progress"},
        {"id": "t1", "text": "a", "status": "completed"},
        # 非法 status 一律按 done 兜底，不能让脏数据把整张卡打挂
        {"id": "t1", "text": "a", "status": "bogus", "done": True},
        {"id": "t1", "text": "a", "status": "bogus"},
        # status 与 done 冲突时以 status 为准
        {"id": "t1", "text": "a", "status": "in_progress", "done": True},
    ]
    for case in cases:
        schema = TaskCardItemRead.model_validate(case)
        runtime = tc.public_items([case])[0]
        assert (schema.status, schema.done) == (runtime["status"], runtime["done"]), case


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


def test_render_notice_mentions_platform_card_only():
    """围栏协议要明确告诉模型：内核自带的任务工具不会显示在界面上。"""
    notice = tc.render_notice(None, allow_create=True)
    assert "task_card" in notice
    assert "内核自带" in notice
    assert "不要用它们代替" in notice


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
    create_anchor = card["anchor_message_id"]

    # 第二轮：已有卡，模型只报状态变化 → 勾选 t1，版本递增，仍然只有一行
    await send(client, conversation_id, "继续")
    detail = (await client.get(f"/api/conversations/{conversation_id}")).json()
    card = detail["task_card"]
    assert card is not None
    assert card["version"] == 2
    assert card["items"][0]["done"] is True
    assert card["items"][0]["done_at"]
    assert card["items"][1]["done"] is False
    # 普通 update 不能移动锚点：否则每轮报进度都把卡片往对话末尾拖（设计 §6.5）
    assert card["anchor_message_id"] == create_anchor
    assert card["anchor_message_id"] != detail["messages"][-1]["id"]
    async with SessionLocal() as session:
        rows = (await session.execute(select(TaskCard))).scalars().all()
    assert len(rows) == 1


async def test_json_fenced_block_creates_card_and_is_stripped(client: httpx.AsyncClient):
    """模型把块写成 ```json 也要建卡，并且协议 JSON 不能漏进气泡（实测踩过的坑）。"""
    agent = await make_task_agent(client, "json")
    conversation_id = await new_conversation(client, agent)

    await send(client, conversation_id, TASK_TEXT)
    detail = (await client.get(f"/api/conversations/{conversation_id}")).json()
    card = detail["task_card"]
    assert card is not None
    assert card["title"] == "实现进度卡"
    assert [item["id"] for item in card["items"]] == ["t1", "t2"]
    message = detail["messages"][-1]
    assert '"action"' not in message["content"]
    assert "```json" not in message["content"]
    assert '"action"' not in json.dumps(message["parts"], ensure_ascii=False)


async def test_bare_json_block_creates_card_and_is_stripped(client: httpx.AsyncClient):
    """连围栏都省了的裸 JSON 收尾块同样要认。"""
    agent = await make_task_agent(client, "bare")
    conversation_id = await new_conversation(client, agent)

    await send(client, conversation_id, TASK_TEXT)
    detail = (await client.get(f"/api/conversations/{conversation_id}")).json()
    card = detail["task_card"]
    assert card is not None
    message = detail["messages"][-1]
    assert '"action"' not in message["content"]
    assert '"action"' not in json.dumps(message["parts"], ensure_ascii=False)


async def test_mid_document_json_example_is_not_treated_as_card(client: httpx.AsyncClient):
    """中段举例的 JSON 代码块既不能建卡，也不能从正文里被吞掉。"""
    agent = await make_task_agent(client, "example")
    conversation_id = await new_conversation(client, agent)

    await send(client, conversation_id, TASK_TEXT)
    detail = (await client.get(f"/api/conversations/{conversation_id}")).json()
    assert detail["task_card"] is None
    message = detail["messages"][-1]
    assert "```json" in message["content"]
    assert "以上只是示例" in message["content"]


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
