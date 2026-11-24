"""IM 域的测试：适配器契约、文本切分、接口、配对与「消息进对话、回复回聊天」。

不连任何真实平台：适配器在这一层被替换成一个假的（``FakeAdapter``），
验证的是框架侧的行为 —— 凭据只回掩码、没配对的账号只回一次请求、
配对后消息进绑定智能体的对话、回复发回来源聊天。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.im import service as im_service
from app.im.base import (
    Account,
    Adapter,
    ChatRef,
    ConnectionBase,
    Handlers,
    InboundMessage,
    OutboundMessage,
    Sender,
    int_or,
    json_body,
    platform_dicts,
    reason_of,
    retry_after_of,
    scrub,
)
from app.im.format import chunk_markdown, chunk_telegram, to_plain
from app.im.platforms import ADAPTERS
from helpers import make_agent


# --- 文本 -------------------------------------------------------------------


def test_platforms_declare_the_expected_shape():
    items = {item["id"]: item for item in platform_dicts()}
    assert {"telegram", "feishu", "dingtalk", "wecom", "qq", "discord", "slack"} <= set(items)
    telegram = items["telegram"]
    assert telegram["fields"][0]["key"] == "token" and telegram["fields"][0]["secret"] is True
    assert telegram["buttons"] is True
    # 微信（个人号）暂不支持：元数据里要写明原因，添加按钮不能出现
    assert items["weixin"]["available"] is False
    assert items["weixin"]["note"]


def test_every_available_platform_has_an_adapter():
    available = {item["id"] for item in platform_dicts() if item["available"]}
    assert available <= set(ADAPTERS)


def test_telegram_html_and_plain_render_the_same_blocks():
    markdown = "**粗体** 与 `code`\n\n```py\nprint(1)\n```\n\n[链接](https://example.com)"
    html = "\n".join(chunk[0] for chunk in chunk_telegram(markdown, 4096))
    assert "<b>粗体</b>" in html and "<code>code</code>" in html
    assert '<pre><code class="language-py">print(1)</code></pre>' in html
    assert '<a href="https://example.com">链接</a>' in html

    plain = to_plain(markdown)
    assert "粗体" in plain and "<b>" not in plain
    assert "链接 (https://example.com)" in plain


def test_chunking_respects_the_limit_and_keeps_code_fences():
    text = "\n\n".join(f"第 {index} 段" * 30 for index in range(40))
    chunks = chunk_markdown(text, 200)
    assert len(chunks) > 1
    assert all(len(item) <= 200 for item in chunks)

    html_chunks = chunk_telegram("```py\n" + "x = 1\n" * 200 + "```", 300)
    assert html_chunks
    for html, _plain in html_chunks:
        assert "<pre>" in html and "</pre>" in html


# --- 假适配器 ---------------------------------------------------------------


class FakeHandle(ConnectionBase):
    def __init__(self, fields: dict[str, Any], handlers: Handlers) -> None:
        super().__init__(handlers)
        self.fields = fields
        self.sent: list[tuple[ChatRef, OutboundMessage]] = []
        self.typing_count = 0

    async def open(self, generation: int) -> None:
        self.status("ready", account=Account(id="fake-1", name="@fake"))

    async def send(self, chat: ChatRef, message: OutboundMessage) -> None:
        self.sent.append((chat, message))

    async def typing(self, chat: ChatRef) -> None:
        self.typing_count += 1


class FakeAdapter(Adapter):
    id = "telegram"

    def __init__(self) -> None:
        self.handles: list[FakeHandle] = []
        self.checked: list[dict[str, Any]] = []

    async def check(self, fields: dict[str, Any]) -> Account:
        self.checked.append(dict(fields))
        if not str(fields.get("token") or "").strip():
            raise im_service.AdapterError("请先填写 Bot token")
        return Account(id="fake-1", name="@fake")

    async def connect(self, fields, handlers, runtime=None):  # type: ignore[override]
        handle = FakeHandle(fields, handlers)
        self.handles.append(handle)
        await handle.open(0)
        return handle


@pytest.fixture(autouse=True)
async def _reset_im_manager():
    """每个用例收尾时把连接管理器复位。

    连接与后台任务跨用例存活会占着事件循环，下一个用例建库 / 删表时就会撞上
    「Event loop is closed」。这里把活着的连接停掉、状态清干净。
    """
    yield
    await im_service.manager().stop_all()
    im_service.manager().resume()


@pytest.fixture()
def fake_adapter(monkeypatch: pytest.MonkeyPatch) -> FakeAdapter:
    adapter = FakeAdapter()
    monkeypatch.setitem(ADAPTERS, "telegram", adapter)
    yield adapter


# --- 接口 -------------------------------------------------------------------


async def test_connection_crud_masks_secrets_and_checks_the_platform(
    client: httpx.AsyncClient, fake_adapter: FakeAdapter
):
    agent = await make_agent(client)
    created = await client.post(
        "/api/im/connections",
        json={
            "platform": "telegram",
            "agent_profile_id": agent["id"],
            "fields": {"token": "123456:secret"},
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["platform_name"] == "Telegram"
    assert body["secret_fields"] == ["token"]
    assert body["agent_name"] == agent["name"]
    # 明文绝不出现在响应里
    assert "123456:secret" not in json.dumps(body)
    # 平台核对拿到的确实是解密后的明文
    assert fake_adapter.checked[-1]["token"] == "123456:secret"

    listed = (await client.get("/api/im/connections")).json()["items"]
    assert [item["id"] for item in listed] == [body["id"]]

    # 留空密钥 = 保留旧值，仍然能通过核对
    patched = await client.patch(
        f"/api/im/connections/{body['id']}",
        json={"fields": {"token": ""}, "enabled": False},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["enabled"] is False
    assert fake_adapter.checked[-1]["token"] == "123456:secret"

    assert (await client.delete(f"/api/im/connections/{body['id']}")).status_code == 204
    assert (await client.get("/api/im/connections")).json()["items"] == []


async def test_connection_requires_a_working_platform_check(
    client: httpx.AsyncClient, fake_adapter: FakeAdapter
):
    agent = await make_agent(client)
    response = await client.post(
        "/api/im/connections",
        json={"platform": "telegram", "agent_profile_id": agent["id"], "fields": {"token": ""}},
    )
    assert response.status_code == 422
    assert "token" in response.json()["detail"] or "Bot" in response.json()["detail"]

    missing_agent = await client.post(
        "/api/im/connections",
        json={"platform": "telegram", "fields": {"token": "abc"}},
    )
    assert missing_agent.status_code == 422


async def test_unavailable_platform_cannot_be_added(client: httpx.AsyncClient):
    agent = await make_agent(client)
    response = await client.post(
        "/api/im/connections",
        json={"platform": "weixin", "agent_profile_id": agent["id"], "fields": {}},
    )
    assert response.status_code == 422


# --- 配对与消息 -------------------------------------------------------------


async def _add_connection(client: httpx.AsyncClient, agent_id: str) -> dict:
    response = await client.post(
        "/api/im/connections",
        json={"platform": "telegram", "agent_profile_id": agent_id, "fields": {"token": "t0ken"}},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_unpaired_sender_gets_one_request_then_messages_flow(
    client: httpx.AsyncClient, fake_adapter: FakeAdapter
):
    # 关掉自动起标题：本用例只关心 IM 这一路
    await client.put("/api/system/title", json={"enabled": False})
    agent = await make_agent(client)
    connection = await _add_connection(client, agent["id"])
    handle = fake_adapter.handles[-1]

    stranger = InboundMessage(
        chat=ChatRef(key="chat-1", kind="direct", title="张三"),
        sender=Sender(id="u1", name="张三"),
        text="你好",
    )
    await im_service.manager().handle_message(connection["id"], stranger)
    await asyncio.sleep(0)
    assert handle.sent, "没配对的账号应当收到一句「已发出连接请求」"
    assert "连接请求" in handle.sent[-1][1].text

    # 再说一句：不重复回
    before = len(handle.sent)
    await im_service.manager().handle_message(connection["id"], stranger)
    await asyncio.sleep(0)
    assert len(handle.sent) == before

    peers = (await client.get(f"/api/im/peers?connection_id={connection['id']}")).json()["items"]
    assert len(peers) == 1 and peers[0]["state"] == "pending"
    assert peers[0]["sender_name"] == "张三"

    allowed = await client.post(
        f"/api/im/connections/{connection['id']}/peers/{peers[0]['id']}", json={"allow": True}
    )
    assert allowed.status_code == 200 and allowed.json()["state"] == "allowed"
    chats = (await client.get(f"/api/im/connections/{connection['id']}/chats")).json()["items"]
    assert [item["chat_key"] for item in chats] == ["chat-1"]

    sent_before = len(handle.sent)
    await im_service.manager().handle_message(connection["id"], stranger)
    assert len(handle.sent) == sent_before + 1
    assert handle.sent[-1][1].text == "Echo: 你好... done"

    # 消息进了绑定的智能体：对话与消息都落库了（聊天行在这一轮里建出了对应对话）
    chats = (await client.get(f"/api/im/connections/{connection['id']}/chats")).json()["items"]
    assert chats[0]["conversation_id"]
    detail = (await client.get(f"/api/conversations/{chats[0]['conversation_id']}")).json()
    assert [item["role"] for item in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][0]["content"] == "你好"
    assert detail["messages"][1]["content"] == "Echo: 你好... done"


async def test_denied_sender_is_not_forwarded(client: httpx.AsyncClient, fake_adapter: FakeAdapter):
    agent = await make_agent(client)
    connection = await _add_connection(client, agent["id"])
    handle = fake_adapter.handles[-1]
    stranger = InboundMessage(
        chat=ChatRef(key="chat-2", kind="direct"),
        sender=Sender(id="u2", name="李四"),
        text="在吗",
    )
    await im_service.manager().handle_message(connection["id"], stranger)
    peers = (await client.get(f"/api/im/peers?connection_id={connection['id']}")).json()["items"]
    denied = await client.post(
        f"/api/im/connections/{connection['id']}/peers/{peers[0]['id']}", json={"allow": False}
    )
    assert denied.json()["state"] == "denied"

    before = len(handle.sent)
    await im_service.manager().handle_message(connection["id"], stranger)
    assert len(handle.sent) == before + 1
    assert "拒绝" in handle.sent[-1][1].text
    assert (await client.get("/api/conversations")).json()["items"] == []


async def test_group_message_without_mention_is_ignored(
    client: httpx.AsyncClient, fake_adapter: FakeAdapter
):
    agent = await make_agent(client)
    connection = await _add_connection(client, agent["id"])
    handle = fake_adapter.handles[-1]
    group = InboundMessage(
        chat=ChatRef(key="g1", kind="group", title="工作群"),
        sender=Sender(id="u3", name="王五"),
        text="大家早",
        mentioned=False,
    )
    await im_service.manager().handle_message(connection["id"], group)
    assert handle.sent == []
    assert (await client.get("/api/im/peers")).json()["items"] == []


async def test_failed_run_reports_the_reason_instead_of_silence(
    client: httpx.AsyncClient, fake_adapter: FakeAdapter, monkeypatch: pytest.MonkeyPatch
):
    """智能体跑挂了：聊天软件里要收到原因，而不是「没反应」。"""
    from app.im import bridge as im_bridge

    await client.put("/api/system/title", json={"enabled": False})
    agent = await make_agent(client)
    connection = await _add_connection(client, agent["id"])
    handle = fake_adapter.handles[-1]
    peer = InboundMessage(
        chat=ChatRef(key="chat-err", kind="direct"),
        sender=Sender(id="u9", name="赵六"),
        text="跑一下",
    )
    await im_service.manager().handle_message(connection["id"], peer)
    peers = (await client.get(f"/api/im/peers?connection_id={connection['id']}")).json()["items"]
    await client.post(
        f"/api/im/connections/{connection['id']}/peers/{peers[0]['id']}",
        json={"allow": True},
    )

    async def boom(connection_row, message):
        raise im_bridge.ImReplyError("智能体已停用，请先在「智能体」页启用它")

    monkeypatch.setattr(im_bridge, "dispatch_message", boom)
    before = len(handle.sent)
    await im_service.manager().handle_message(connection["id"], peer)
    assert len(handle.sent) == before + 1
    assert "智能体已停用" in handle.sent[-1][1].text


async def test_stop_command_cancels_the_running_turn(
    client: httpx.AsyncClient, fake_adapter: FakeAdapter
):
    """`/stop` 停的是这个聊天正在跑的那一轮。"""
    agent = await make_agent(client)
    connection = await _add_connection(client, agent["id"])
    handle = fake_adapter.handles[-1]
    peer = InboundMessage(
        chat=ChatRef(key="chat-stop", kind="direct"),
        sender=Sender(id="u10", name="钱七"),
        text="开始",
    )
    await im_service.manager().handle_message(connection["id"], peer)
    peers = (await client.get(f"/api/im/peers?connection_id={connection['id']}")).json()["items"]
    await client.post(
        f"/api/im/connections/{connection['id']}/peers/{peers[0]['id']}",
        json={"allow": True},
    )

    before = len(handle.sent)
    stop = InboundMessage(
        chat=ChatRef(key="chat-stop", kind="direct"),
        sender=Sender(id="u10", name="钱七"),
        text="/stop",
    )
    await im_service.manager().handle_message(connection["id"], stop)
    assert len(handle.sent) == before + 1
    assert "没有在做的活" in handle.sent[-1][1].text or "已停下" in handle.sent[-1][1].text


async def test_group_without_mention_is_ignored_even_when_paired(
    client: httpx.AsyncClient, fake_adapter: FakeAdapter
):
    """配对过的账号在群里没 @ 机器人：不打扰、也不进智能体。"""
    agent = await make_agent(client)
    connection = await _add_connection(client, agent["id"])
    handle = fake_adapter.handles[-1]
    group = InboundMessage(
        chat=ChatRef(key="g9", kind="group", title="工作群"),
        sender=Sender(id="u11", name="孙八"),
        text="大家好",
        mentioned=False,
    )
    await im_service.manager().handle_message(connection["id"], group)
    peers = (await client.get(f"/api/im/peers?connection_id={connection['id']}")).json()["items"]
    assert peers == []
    assert handle.sent == []


async def test_rebinding_the_agent_starts_a_fresh_conversation(
    client: httpx.AsyncClient, fake_adapter: FakeAdapter
):
    """换绑智能体后另开一条对话：Conversation 是智能体快照，旧对话不能继续用。"""
    from helpers import make_profile

    await client.put("/api/system/title", json={"enabled": False})
    first = await make_agent(client, name="第一个")
    second = await make_profile(
        client, gateway_id=first["gateway_id"], name="第二个", model=first["model"]
    )
    connection = await _add_connection(client, first["id"])
    peer = InboundMessage(
        chat=ChatRef(key="chat-rebind", kind="direct"),
        sender=Sender(id="u12", name="周九"),
        text="一",
    )
    await im_service.manager().handle_message(connection["id"], peer)
    peers = (await client.get(f"/api/im/peers?connection_id={connection['id']}")).json()["items"]
    await client.post(
        f"/api/im/connections/{connection['id']}/peers/{peers[0]['id']}",
        json={"allow": True},
    )
    await im_service.manager().handle_message(connection["id"], peer)
    chats = (await client.get(f"/api/im/connections/{connection['id']}/chats")).json()["items"]
    old_conversation = chats[0]["conversation_id"]
    assert old_conversation

    rebound = await client.patch(
        f"/api/im/connections/{connection['id']}", json={"agent_profile_id": second["id"]}
    )
    assert rebound.status_code == 200, rebound.text

    await im_service.manager().handle_message(
        connection["id"],
        InboundMessage(
            chat=ChatRef(key="chat-rebind", kind="direct"),
            sender=Sender(id="u12", name="周九"),
            text="二",
        ),
    )
    chats = (await client.get(f"/api/im/connections/{connection['id']}/chats")).json()["items"]
    assert chats[0]["conversation_id"] != old_conversation
    new_detail = (await client.get(f"/api/conversations/{chats[0]['conversation_id']}")).json()
    assert new_detail["conversation"]["agent_profile_id"] == second["id"]


# --- 过程内容开关（思考 / 工具 / 日志）-------------------------------------


class _FakeRun:
    """假的 ActiveRun：直接回放一串事件，不跑真实内核。"""

    def __init__(self, events: list[dict]) -> None:
        self._events = events

    async def subscribe(self):
        for event in self._events:
            yield event


async def test_traces_are_off_by_default_and_follow_the_per_connection_switch():
    """默认只发正文；打开开关后思考 / 工具 / 日志以引用块附在正文之后。"""
    from app.im.bridge import ReplySink, _collect

    events = [
        {"type": "thinking.delta", "text": "先看看目录"},
        {"type": "message.delta", "text": "这是正文。"},
        {"type": "tool.call", "name": "run_shell"},
        {"type": "tool.result", "payload": {"output": "a.txt"}},
        {"type": "log", "text": "技能：已物化", "payload": {"level": "info"}},
        {"type": "run.finished", "payload": {"status": "ok"}},
    ]

    quiet = ReplySink(chat=ChatRef(key="k"), options={})
    await _collect(_FakeRun(events), quiet)
    assert quiet.text == "这是正文。"

    loud = ReplySink(
        chat=ChatRef(key="k"),
        options={"show_thinking": True, "show_tools": True, "show_logs": True},
    )
    await _collect(_FakeRun(events), loud)
    assert loud.text.startswith("这是正文。")
    assert "💭 先看看目录" in loud.text
    assert "🔧 调用 run_shell" in loud.text
    assert "↩️ a.txt" in loud.text
    assert "技能：已物化" in loud.text
    # 过程内容与正文之间用空行隔开，不会和回答混在一起
    assert "这是正文。\n\n> 💭" in loud.text


async def test_trace_output_is_flattened_and_capped():
    """工具输出压成一行并截断：一个超长输出不能把整条消息撑爆。"""
    from app.im.bridge import TRACE_LINE_LIMIT, ReplySink, _collect

    sink = ReplySink(chat=ChatRef(key="k"), options={"show_tools": True})
    await _collect(
        _FakeRun(
            [
                {"type": "tool.result", "payload": {"output": "行一\n行二\n" + "x" * 5000}},
                {"type": "run.finished", "payload": {"status": "ok"}},
            ]
        ),
        sink,
    )
    # 没有正文时，过程内容就是全部；换行被压平，超长被截断
    assert sink.text.count("\n") == 0
    assert "行二" in sink.text
    assert len(sink.text) <= TRACE_LINE_LIMIT + 8
    assert sink.text.endswith("…")


async def test_task_card_protocol_is_not_forwarded_to_chat_apps():
    """任务卡协议块只属于控制台卡片：IM 汇总正文时必须剥掉，聊天软件里看不到 JSON。"""
    from app.im.bridge import ReplySink, _collect

    sink = ReplySink(chat=ChatRef(key="k"), options={})
    await _collect(
        _FakeRun(
            [
                {
                    "type": "message.delta",
                    "text": (
                        "已经拆好计划。\n\n```task_card\n"
                        '{"action":"create","items":[{"id":"t1","text":"a"},'
                        '{"id":"t2","text":"b"}]}\n```'
                    ),
                },
                {"type": "run.finished", "payload": {"status": "ok"}},
            ]
        ),
        sink,
    )
    assert sink.text.startswith("已经拆好计划。")
    assert "task_card" not in sink.text
    assert '"action"' not in sink.text


async def test_options_can_be_set_per_connection_and_partially_updated(
    client: httpx.AsyncClient, fake_adapter: FakeAdapter
):
    agent = await make_agent(client)
    created = await client.post(
        "/api/im/connections",
        json={
            "platform": "telegram",
            "agent_profile_id": agent["id"],
            "fields": {"token": "t0ken"},
            "options": {"show_tools": True},
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["options"] == {
        "show_thinking": False,
        "show_tools": True,
        "show_logs": False,
    }

    # 只改一个键：其它两个保持原值
    patched = await client.patch(
        f"/api/im/connections/{created.json()['id']}", json={"options": {"show_logs": True}}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["options"] == {
        "show_thinking": False,
        "show_tools": True,
        "show_logs": True,
    }

    # 新建时不传 = 三个都关（老行的语义）
    plain = await client.post(
        "/api/im/connections",
        json={"platform": "telegram", "agent_profile_id": agent["id"], "fields": {"token": "t2"}},
    )
    assert plain.json()["options"] == {
        "show_thinking": False,
        "show_tools": False,
        "show_logs": False,
    }


# --- 凭据安全与共享小工具 -----------------------------------------------------


def test_scrub_removes_credentials_from_exception_text():
    """异常文本里夹带的 ticket / token 必须先擦掉，才能落库并回给前端。"""
    # websockets.InvalidURI 会把完整 URI 回显出来（实测行为）
    raw = "wss://example.invalid/connect?ticket=SECRET123#frag isn't a valid URI"
    assert "SECRET123" not in scrub(raw)
    assert "ticket=***" in scrub(raw)
    # Telegram 把 token 放在路径里
    bot_url = "POST https://api.telegram.org/bot123456:ABC/getMe failed"
    assert "123456:ABC" not in scrub(bot_url)
    # 钉钉 / Slack 的 query 凭据
    query = scrub("GET /gettoken?appkey=k&appsecret=v")
    assert "appsecret=***" in query
    # 正常中文报错不受影响
    assert scrub("钉钉要求重连") == "钉钉要求重连"


def test_reason_of_never_leaks_a_token():
    class Boom(Exception):
        pass

    message = reason_of(Boom("connect wss://h/ws?ticket=LEAK#x failed"))
    assert "LEAK" not in message
    # 空文本回落到异常类名
    assert reason_of(Boom()) == "Boom"


def test_shared_http_helpers_tolerate_dirty_input():
    """共享小工具要能吃脏数据：不抛错，给出可用的默认值。"""
    assert int_or("12", 0) == 12
    assert int_or("abc", 7) == 7
    assert int_or(None, 7) == 7
    assert json_body(httpx.Response(200, text="not json")) == {}
    assert json_body(httpx.Response(200, json=[1, 2])) == {}
    assert json_body(httpx.Response(200, json={"ok": True})) == {"ok": True}
    assert retry_after_of(httpx.Response(200, headers={"retry-after": "3"})) == 3.0
    assert retry_after_of(httpx.Response(200, headers={"retry-after": "x"})) is None
    limited = httpx.Response(200, headers={"retry-after": "3"})
    assert retry_after_of(limited, only_on_429=True) is None


def test_connection_status_is_scrubbed_before_reporting():
    """适配器把错误交给框架时，ConnectionBase.status 会先脱敏。"""
    seen: list[dict[str, Any]] = []

    class Handle(ConnectionBase):
        async def send(self, chat: ChatRef, message: OutboundMessage) -> None:
            return None

        async def open(self, generation: int) -> None:
            return None

    handle = Handle(
        Handlers(
            on_message=lambda message: asyncio.sleep(0),
            on_action=lambda action: asyncio.sleep(0),
            on_status=lambda **kwargs: seen.append(kwargs),
        )
    )
    handle.status("error", "无法连接 wss://h/ws?ticket=TOPSECRET")
    assert seen and "TOPSECRET" not in seen[0]["error"]


# --- 发送队列：停机时不能让调用方悬空 -----------------------------------------


async def test_sender_close_settles_the_inflight_future():
    """``close()`` 必须结束正在发送的那条消息，否则调用方永久挂在 await 上。"""
    class Slow(ConnectionBase):
        async def send(self, chat: ChatRef, message: OutboundMessage) -> None:
            await asyncio.sleep(10)

        async def open(self, generation: int) -> None:
            return None

    sender = im_service._Sender("c1", Slow())
    pending = sender.submit(ChatRef(key="k"), OutboundMessage(text="hi"))
    await asyncio.sleep(0.05)  # 让 _drain 取到并进入 send()
    await sender.close()
    with pytest.raises(im_service.ImError):
        await asyncio.wait_for(pending, timeout=0.5)
    # 停机之后再提交，立刻失败而不是排队等一个不会再来的连接
    with pytest.raises(im_service.ImError):
        await asyncio.wait_for(
            sender.submit(ChatRef(key="k"), OutboundMessage(text="again")), timeout=0.5
        )


async def test_stop_does_not_report_a_spurious_disconnect():
    """主动停机不该被读循环的 finally 误报成「掉线 / 重试」。"""
    events: list[dict[str, Any]] = []

    class Handle(ConnectionBase):
        def __init__(self) -> None:
            super().__init__(
                Handlers(
                    on_message=lambda message: asyncio.sleep(0),
                    on_action=lambda action: asyncio.sleep(0),
                    on_status=lambda **kwargs: events.append(kwargs),
                )
            )
            self.closed: asyncio.Event | None = None

        async def send(self, chat: ChatRef, message: OutboundMessage) -> None:
            return None

        async def open(self, generation: int) -> None:
            self.closed = asyncio.Event()
            self.spawn(self._read(generation))

        async def _read(self, generation: int) -> None:
            try:
                assert self.closed is not None
                await self.closed.wait()
            finally:
                if self.current(generation):
                    self.status("retrying", "连接已断开")

        async def stop(self) -> None:
            # 正确顺序：先置位 stopped，再关 socket
            await super().stop()
            if self.closed is not None:
                self.closed.set()

    handle = Handle()
    handle.begin()
    await asyncio.sleep(0.05)
    events.clear()
    await handle.stop()
    await asyncio.sleep(0.2)
    assert not [event for event in events if event.get("state") == "retrying"]
