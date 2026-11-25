"""自动生成对话标题：净化规则、设置读写、以及真实跑一次。

设计见 ``app/runtime/titles.py``：用设置里指定的智能体 + 模型跑一次一次性运行，
把正文当标题写回。它是尽力而为的增强 —— 失败保留 ``prepare_run`` 写的首行兜底标题。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import httpx

from app.runtime import titles
from helpers import cli_options, make_gateway, make_profile

TITLE_FAKE = str(Path(__file__).parent / "fakes" / "fake_title_agent.py")


def title_options() -> dict:
    """指向「固定回一个标题」的替身内核。"""
    return {
        "command": sys.executable,
        "args": [TITLE_FAKE],
        "prompt_mode": "stdin",
        "output_format": "jsonl",
        "text_path": "delta",
        "delta_mode": True,
        "session_path": "session_id",
    }


def test_clean_normalizes_model_output():
    """标题净化：去包裹 / 去前缀 / 去句末标点 / 只留一行 / 超长截断。"""
    assert titles._clean('  "修一个 Python 的 bug"。  ') == "修一个 Python 的 bug"
    assert titles._clean("「登录页暗色主题」") == "登录页暗色主题"
    assert titles._clean("标题：固定打卡时间") == "固定打卡时间"
    assert titles._clean("## 帮我看看这个 bug\n第二行不要") == "帮我看看这个 bug"
    assert titles._clean("") == ""
    assert titles._clean("   ") == ""
    assert len(titles._clean("一" * 80)) == titles.MAX_TITLE_CHARS


async def test_probe_active_account_reads_the_active_slot():
    """探测当前登录账号：取 accounts 里 active 的那一个；没有 active 就当无法判断。"""

    class FakeDefinition:
        id = "fake-kernel"

        def auth_status(self):
            return SimpleNamespace(
                accounts=[
                    SimpleNamespace(id="slot-a", active=False),
                    SimpleNamespace(id="slot-b", active=True),
                ]
            )

    assert await titles._probe_active_account(FakeDefinition()) == "slot-b"


def test_pinned_account_skips_title_only_when_confirmed_different():
    """只有「确认不是当前登录账号」才跳过起标题；探测失败 / 自定义网关都照常跑。"""
    profile = SimpleNamespace(account_id="slot-b", type_id="codebuddy-cli")
    official = SimpleNamespace(kind="official")
    custom = SimpleNamespace(kind="custom")

    assert titles._pinned_account_is_inactive("slot-a", profile, official) is True
    assert titles._pinned_account_is_inactive("slot-b", profile, official) is False
    # 探测不到当前账号：无从判断，不跳过（仍沿用当前登录账号，不会改动机器登录态）。
    assert titles._pinned_account_is_inactive(None, profile, official) is False
    # 自定义网关不看内核登录态。
    assert titles._pinned_account_is_inactive("slot-a", profile, custom) is False
    # 没指定账号时什么都不用判断。
    assert titles._pinned_account_is_inactive(
        "slot-a", SimpleNamespace(account_id=None, type_id="codebuddy-cli"), official
    ) is False


async def test_title_settings_default_and_roundtrip(client: httpx.AsyncClient):
    """默认开、跟随默认智能体；保存后能读回；指定不存在的智能体报 422。"""
    agent = await make_profile(client)

    initial = (await client.get("/api/system/title")).json()
    assert initial["enabled"] is True
    assert initial["agent_profile_id"] is None
    # 没配置就用「默认智能体」；只有一个运行时它会被自动设成默认。
    assert initial["effective_agent_id"] == agent["id"]
    assert initial["effective_agent_name"] == agent["name"]

    saved = await client.put(
        "/api/system/title",
        json={"enabled": True, "agent_profile_id": agent["id"], "model": "fake-model"},
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["agent_profile_id"] == agent["id"] and body["model"] == "fake-model"

    reread = (await client.get("/api/system/title")).json()
    assert reread["agent_profile_id"] == agent["id"] and reread["model"] == "fake-model"

    off = await client.put("/api/system/title", json={"enabled": False, "model": ""})
    assert off.json()["enabled"] is False
    assert (await client.get("/api/system/title")).json()["enabled"] is False

    bad = await client.put("/api/system/title", json={"agent_profile_id": "not-a-real-agent"})
    assert bad.status_code == 422


async def test_generate_writes_a_title_from_the_configured_agent(client: httpx.AsyncClient):
    """端到端：跑一次替身内核，把它的正文净化后写成对话标题。"""
    gateway = await make_gateway(client)
    agent = await make_profile(client, gateway_id=gateway["id"], options=title_options())
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})
    ).json()
    assert conversation["title"] == "新对话"

    await titles.generate(conversation["id"], "新对话", "帮我修一下这个 Python 的 bug")

    detail = (await client.get(f"/api/conversations/{conversation['id']}")).json()
    assert detail["conversation"]["title"] == "修 Python 的 bug"


async def test_generate_keeps_a_renamed_title(client: httpx.AsyncClient):
    """用户/兜底已经把标题定下了（与生成时的基准不一致）就不覆盖。"""
    gateway = await make_gateway(client)
    agent = await make_profile(client, gateway_id=gateway["id"], options=title_options())
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})
    ).json()

    # 基准标题与库里当前的不同 —— 相当于用户在生成期间改过名。
    await titles.generate(conversation["id"], "用户改过的标题", "帮我修一下这个 bug")

    detail = (await client.get(f"/api/conversations/{conversation['id']}")).json()
    assert detail["conversation"]["title"] == "新对话"


async def test_generate_does_nothing_when_disabled(client: httpx.AsyncClient):
    """关掉开关后不跑任何内核，标题保持兜底值。"""
    gateway = await make_gateway(client)
    agent = await make_profile(client, gateway_id=gateway["id"], options=title_options())
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})
    ).json()
    await client.put("/api/system/title", json={"enabled": False})

    await titles.generate(conversation["id"], "新对话", "帮我修一下这个 bug")

    detail = (await client.get(f"/api/conversations/{conversation['id']}")).json()
    assert detail["conversation"]["title"] == "新对话"


async def test_agent_group_name_roundtrip(client: httpx.AsyncClient):
    """智能体分组名：创建可带、可读回、可改、可清空，复制时跟随。"""
    group = await make_profile(client, name="分组内", group_name=" 工作 ")
    assert group["group_name"] == "工作"  # 两端空格会去掉

    plain = await make_profile(client, name="未分组")
    assert plain["group_name"] == ""

    updated = await client.patch(f"/api/agents/{group['id']}", json={"group_name": "生活"})
    assert updated.status_code == 200 and updated.json()["group_name"] == "生活"

    cleared = await client.patch(f"/api/agents/{group['id']}", json={"group_name": ""})
    assert cleared.json()["group_name"] == ""

    clone = await client.post(f"/api/agents/{plain['id']}/duplicate")
    assert clone.status_code == 201
    assert clone.json()["group_name"] == plain["group_name"]

    listed = {item["name"]: item["group_name"] for item in (await client.get("/api/agents")).json()["items"]}
    assert listed["分组内"] == ""


async def test_cli_options_still_point_at_the_echo_fake():
    """护栏：本文件自建 options，别被 helpers 的默认值悄悄带偏（保持两端一致）。"""
    assert "fake_jsonl_agent.py" in " ".join(cli_options("sess-42")["args"])
    assert TITLE_FAKE.endswith("fake_title_agent.py")


async def test_failed_generation_is_visible_in_the_settings(client: httpx.AsyncClient):
    """起标题失败不打扰用户，但必须**留痕**：设置页能读到「上次失败原因」，成功一次就清掉。

    这是铁律 11 的落地方式：后台增强不抛异常给用户，但原因写进平台设置，
    界面（设置 → 对话）显示出来，用户可自查。
    """
    gateway = await make_gateway(client)
    agent = await make_profile(
        client,
        gateway_id=gateway["id"],
        options={**title_options(), "command": "definitely-not-a-real-binary-12345"},
    )
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})
    ).json()

    await titles.generate(conversation["id"], "新对话", "帮我修一下这个 bug")

    # 对话本身不受影响：标题仍是兜底值。
    detail = (await client.get(f"/api/conversations/{conversation['id']}")).json()
    assert detail["conversation"]["title"] == "新对话"
    # 失败原因落在设置里，界面能读到。
    settings = (await client.get("/api/system/title")).json()
    assert settings["last_error"], "起标题失败必须留下原因（铁律 11）"
    assert settings["last_error_at"]

    # 换回能跑的内核再生成一次：标题写回，过期的失败记录被清掉。
    patched = await client.patch(f"/api/agents/{agent['id']}", json={"options": title_options()})
    assert patched.status_code == 200, patched.text
    await titles.generate(conversation["id"], "新对话", "帮我修一下这个 bug")

    after = (await client.get("/api/system/title")).json()
    assert after["last_error"] is None and after["last_error_at"] is None
    retitled = (await client.get(f"/api/conversations/{conversation['id']}")).json()
    assert retitled["conversation"]["title"] == "修 Python 的 bug"
