"""事件构造器的入参约定。

适配器会把模型给出的工具入参 ``**arguments`` 原样展开进 :func:`tool_call`，
而工具入参里出现 ``name`` / ``call_id`` 这样的键很常见（``mcp_create`` 的
``name`` 就是必填项）。这里钉住这个约定，避免有人把形参改回可被关键字撞上的写法。
"""
from __future__ import annotations

from app.agents.events import EventType, tool_call, tool_result


def test_tool_call_accepts_name_in_tool_input():
    """工具入参里的 ``name`` 归入 payload，不能与事件名的形参冲突。"""
    event = tool_call(
        "mcp_create",
        "call-1",
        **{"name": "basic-memory", "transport": "stdio", "command": "/bin/bm"},
    )

    assert event.type is EventType.TOOL_CALL
    assert event.name == "mcp_create"
    assert event.call_id == "call-1"
    assert event.payload["input"] == {
        "name": "basic-memory",
        "transport": "stdio",
        "command": "/bin/bm",
    }


def test_tool_call_accepts_call_id_in_tool_input():
    """工具入参里的 ``call_id`` 同样归入 payload，不覆盖事件的调用 id。"""
    event = tool_call("shell", "call-2", **{"call_id": "model-supplied", "command": "ls"})

    assert event.call_id == "call-2"
    assert event.payload["input"] == {"call_id": "model-supplied", "command": "ls"}


def test_tool_call_without_call_id():
    """没有调用 id 时保持 None，payload 仍完整。"""
    event = tool_call("ping")

    assert event.call_id is None
    assert event.payload["input"] == {}


def test_tool_result_carries_error_flag():
    event = tool_result("call-3", "boom", is_error=True)

    assert event.type is EventType.TOOL_RESULT
    assert event.call_id == "call-3"
    assert event.payload == {"output": "boom", "is_error": True}
