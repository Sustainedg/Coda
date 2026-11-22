"""所有智能体适配器共用的规范化事件协议。

适配器输出的一切都用这些类型表达，因此传输层（SSE）、持久化层与界面
都不需要关心这次运行是来自 CLI 子进程（Claude Code、Codex、Gemini CLI…）
还是来自进程内的 API 内核（任意 OpenAI 兼容端点）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    RUN_STARTED = "run.started"
    MESSAGE_DELTA = "message.delta"
    THINKING_DELTA = "thinking.delta"
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    USAGE = "usage"
    SESSION = "session"
    LOG = "log"
    ERROR = "error"
    RUN_FINISHED = "run.finished"


@dataclass(slots=True)
class AgentEvent:
    type: EventType
    text: str | None = None
    name: str | None = None
    call_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["type"] = self.type.value
        data["payload"] = self.payload or {}
        return {k: v for k, v in data.items() if v is not None or k == "payload"}


# --- 便捷的事件构造器，让适配器代码更简短 -----------------------------


def text_delta(text: str) -> AgentEvent:
    return AgentEvent(EventType.MESSAGE_DELTA, text=text)


def thinking_delta(text: str) -> AgentEvent:
    return AgentEvent(EventType.THINKING_DELTA, text=text)


def tool_call(
    tool_name: str, call_id: str | None = None, /, **input_: Any
) -> AgentEvent:
    """构造一次工具调用事件。

    两个位置参数都用 positional-only（``/``）声明：适配器会把模型给的入参
    ``**arguments`` 原样展开，而工具入参里出现 ``name`` / ``call_id`` 这类键
    非常常见（例如 ``mcp_create`` 的 ``name``），普通形参会被它们撞上并抛
    ``got multiple values for argument``。
    """
    return AgentEvent(EventType.TOOL_CALL, name=tool_name, call_id=call_id, payload={"input": input_})


def tool_result(call_id: str | None, output: str, is_error: bool = False) -> AgentEvent:
    return AgentEvent(
        EventType.TOOL_RESULT,
        call_id=call_id,
        payload={"output": output, "is_error": is_error},
    )


def usage(**counters: Any) -> AgentEvent:
    return AgentEvent(EventType.USAGE, payload=counters)


def session(session_id: str) -> AgentEvent:
    return AgentEvent(EventType.SESSION, payload={"session_id": session_id})


def log(message: str, level: str = "info") -> AgentEvent:
    return AgentEvent(EventType.LOG, text=message, payload={"level": level})


def error(message: str, **extra: Any) -> AgentEvent:
    return AgentEvent(EventType.ERROR, text=message, payload=extra)


def run_started(run_id: str) -> AgentEvent:
    return AgentEvent(EventType.RUN_STARTED, payload={"run_id": run_id})


def run_finished(status: str = "ok", **extra: Any) -> AgentEvent:
    return AgentEvent(EventType.RUN_FINISHED, payload={"status": status, **extra})
