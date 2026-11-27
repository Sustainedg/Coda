#!/usr/bin/env python3
"""任务进度卡 MCP 服务器（stdio 传输）。

平台内部的一个随包分发的 stdio MCP server：只暴露一个工具 ``update_task_card``，
让内核对「多步骤任务」通过结构化工具上报进度，而不是在正文里写 ``task_card``
围栏 JSON。它在「MCP」页只有一条 ``assignable=False`` 的展示项（不能分配 / 编辑）；
真实运行配置由运行层按每轮 ``context_id`` 自动挂到 5 个已声明
``Capabilities.mcp`` 的内核上。

环境变量（由 ``runtime/mcp.py`` 在内部 spec 里注入，不含令牌）：

* ``CODA_TASK_CARD_API``     平台内部接口地址（``POST /api/internal/task-cards/update``）
* ``CODA_TASK_CARD_TIMEOUT`` 单次请求超时秒数（默认 10）
* ``CODA_TASK_CARD_HOME``    本轮内核家目录（凭据就在其 ``task-card/`` 下）。
                              内核启动 MCP 子进程时的 cwd 是**对话工作目录**，不是家目录，
                              所以不能只按相对路径找；这个变量只暴露家目录路径，不含令牌。

凭据文件形状（由 ``runtime/task_card_mcp.py`` 写入，0600）：

    <home>/task-card/context-<sha256(context_id)[:16]>/auth.json
    {"token": "tc_…", "context_id": "…"}

工具参数里**只有** ``context_id`` 与卡片内容：``conversation_id`` / ``run_id`` /
``version`` 一律由服务端按令牌解析，模型传了也不会被采信。

设计纪律（与 ``image_mcp_server.py`` / ``coda_admin_mcp_server.py`` 一致）：

* 只用标准库：stdio 传输里 stdout 是协议端口，日志一律走 stderr；
* 任何失败都返回 ``isError: true`` 的工具结果，让模型看到可读原因并决定是否重试，
  不抛协议层错误、不向用户弹错误气泡；
* 绝不把令牌写进 stderr、返回值或异常文案。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SERVER_NAME = "coda-task-card"
SERVER_VERSION = "1.0.0"
#: MCP 协议版本；initialize 时优先回显客户端请求的版本
PROTOCOL_VERSION = "2024-11-05"

TOOL_NAME = "update_task_card"

ENV_API = "CODA_TASK_CARD_API"
ENV_TIMEOUT = "CODA_TASK_CARD_TIMEOUT"
ENV_HOME = "CODA_TASK_CARD_HOME"

DEFAULT_TIMEOUT = 10.0
CREDENTIALS_SUBDIR = "task-card"
AUTH_FILE = "auth.json"

TOOL_DESCRIPTION = (
    "更新当前对话的任务进度卡。多步骤任务使用；闲聊和单步任务不要调用。"
    "只使用本轮输入给出的 context_id，不要复用历史。"
    "每项用 status 表示 pending / in_progress / completed；兼容旧的 done:true/false。"
)

TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "context_id": {
            "type": "string",
            "description": "本轮输入末尾 <task_card_context> 里的 context_id",
        },
        "action": {"enum": ["create", "update"]},
        "title": {"type": "string", "maxLength": 200},
        "items": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "description": (
                "create：2~20 项，每项必带 text；"
                "update：只报变化项，每项只需 id 和 status（兼容 done）"
            ),
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "maxLength": 32},
                    "text": {
                        "type": "string",
                        "maxLength": 120,
                        "description": "create 必填；update 不需要（传了也会被服务端忽略）",
                    },
                    "status": {
                        "enum": ["pending", "in_progress", "completed"],
                        "description": "任务状态；completed 是终态，不能回退",
                    },
                    "done": {
                        "type": "boolean",
                        "description": "兼容旧协议：true=completed；false=不改状态",
                    },
                },
                # 两项都不是硬必填：create 由服务端要求 text，update 允许只报 status。
                "required": ["id"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["context_id", "action", "items"],
    "additionalProperties": False,
}


class ToolError(RuntimeError):
    """工具调用失败：消息面向模型，可直接展示；不得包含令牌。"""


# --- 配置与凭据 ---------------------------------------------------------------


def load_config() -> dict[str, Any]:
    """读取环境变量；缺地址不在这里报错，留到真正调用时给出可操作提示。"""
    timeout = DEFAULT_TIMEOUT
    raw_timeout = (os.environ.get(ENV_TIMEOUT) or "").strip()
    if raw_timeout:
        try:
            timeout = max(1.0, float(raw_timeout))
        except ValueError:
            print(
                f"{ENV_TIMEOUT} 配置不成秒数（{raw_timeout}），按默认 "
                f"{DEFAULT_TIMEOUT:.0f}s 处理",
                file=sys.stderr,
            )
    return {
        "api": (os.environ.get(ENV_API) or "").strip(),
        "timeout": timeout,
        "home": (os.environ.get(ENV_HOME) or "").strip(),
    }


def context_digest(context_id: str) -> str:
    """与 ``runtime/task_card_mcp.py`` 完全一致的目录摘要。"""
    return hashlib.sha256(context_id.encode("utf-8")).hexdigest()[:16]


def credential_candidates(context_id: str, config: dict[str, Any]) -> list[Path]:
    """凭据文件的候选位置：先家目录（由平台注入），再相对 cwd 兜底。"""
    rel = Path(CREDENTIALS_SUBDIR) / f"context-{context_digest(context_id)}" / AUTH_FILE
    candidates: list[Path] = []
    home = str(config.get("home") or "").strip()
    if home:
        candidates.append(Path(home) / rel)
    candidates.append(Path.cwd() / rel)
    return candidates


def load_credentials(context_id: str, config: dict[str, Any]) -> str:
    """按 context 找到并校验凭据文件，返回令牌。

    失败文案刻意区分「文件没了 / 上下文对不上」，但都不透露路径细节。
    """
    path = next((p for p in credential_candidates(context_id, config) if p.is_file()), None)
    if path is None:
        raise ToolError("当前轮的任务卡凭据不可用，请重新开始一轮")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolError(f"任务卡凭据文件无法读取：{exc.__class__.__name__}") from None
    if not isinstance(data, dict) or str(data.get("context_id") or "") != context_id:
        raise ToolError("任务卡上下文已失效，请重新开始一轮")
    token = str(data.get("token") or "")
    if not token.startswith("tc_"):
        raise ToolError("任务卡凭据已失效，请重新开始一轮")
    return token


# --- 客户端层 -----------------------------------------------------------------


def _post_json(
    url: str, payload: dict[str, Any], *, token: str, timeout: float
) -> dict[str, Any]:
    """POST 平台内部接口；HTTP 错误转成面向模型的中文 ToolError。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc)
        if exc.code == 401:
            raise ToolError("任务卡上下文已失效，请重新开始一轮") from None
        if exc.code == 404:
            raise ToolError("任务卡上下文已失效，请重新开始一轮") from None
        if exc.code == 409:
            raise ToolError(f"进度未保存：{detail or '上下文已失效'}") from None
        if exc.code == 429:
            raise ToolError("本轮任务卡更新过于频繁，请停止重复调用") from None
        raise ToolError(f"平台拒绝了这次更新（HTTP {exc.code}）：{detail or '无正文'}") from None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ToolError(f"平台暂时不可达：{exc}") from None
    try:
        result = json.loads(raw)
    except ValueError:
        raise ToolError("平台返回的不是 JSON，进度未保存") from None
    if not isinstance(result, dict):
        raise ToolError("平台返回的响应格式不正确，进度未保存")
    return result


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        raw = (exc.read() or b"").decode("utf-8", "replace")
    except OSError:
        return ""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw.strip().replace("\n", " ")[:200]
    if isinstance(data, dict):
        for key in ("detail", "message", "error"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:200]
        # FastAPI 的 422 详情是数组，取第一条可读消息
        detail = data.get("detail")
        if isinstance(detail, list) and detail:
            first = detail[0]
            if isinstance(first, dict):
                return str(first.get("msg") or first)[:200]
    return raw.strip().replace("\n", " ")[:200]


def call_update(arguments: dict[str, Any], config: dict[str, Any]) -> str:
    """执行一次任务卡更新，返回给模型的 JSON 文本。"""
    context_id = str(arguments.get("context_id") or "").strip()
    if not context_id:
        raise ToolError("缺少 context_id：请使用本轮输入末尾 <task_card_context> 里的值")
    api = str(config.get("api") or "").strip()
    if not api:
        raise ToolError("任务卡接口地址未配置，当前轮无法更新进度")
    token = load_credentials(context_id, config)
    payload: dict[str, Any] = {"context_id": context_id}
    for key in ("action", "title", "items"):
        if key in arguments:
            payload[key] = arguments[key]
    result = _post_json(
        api,
        payload,
        token=token,
        timeout=float(config.get("timeout") or DEFAULT_TIMEOUT),
    )
    if result.get("ok") is False:
        raise ToolError(str(result.get("error") or "进度未保存"))
    return json.dumps(result, ensure_ascii=False)


# --- 协议层 -------------------------------------------------------------------


def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _tool_response(msg_id: Any, text: str, *, is_error: bool = False) -> dict[str, Any]:
    """工具调用的标准回应：文本内容 + isError 标记。"""
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return _result(msg_id, result)


def handle_message(message: Any) -> dict[str, Any] | None:
    """分发一条 JSON-RPC 消息；通知（无 id）静默忽略。"""
    if not isinstance(message, dict):
        return _error(None, -32600, "请求必须是 JSON 对象")
    method = str(message.get("method") or "")
    msg_id = message.get("id")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    if method == "initialize":
        return _result(
            msg_id,
            {
                "protocolVersion": str(params.get("protocolVersion") or PROTOCOL_VERSION),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if msg_id is None:
        return None
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(
            msg_id,
            {
                "tools": [
                    {
                        "name": TOOL_NAME,
                        "description": TOOL_DESCRIPTION,
                        "inputSchema": TOOL_SCHEMA,
                    }
                ]
            },
        )
    if method == "tools/call":
        name = str(params.get("name") or "")
        if name != TOOL_NAME:
            return _error(msg_id, -32602, f"未知工具：{name}")
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        try:
            text = call_update(arguments, load_config())
        except ToolError as exc:
            return _tool_response(msg_id, f"更新任务卡失败：{exc}", is_error=True)
        except Exception as exc:  # noqa: BLE001 - 兜底：不让未知异常拖垮进程
            print(f"任务卡工具未预期的异常：{exc!r}", file=sys.stderr)
            return _tool_response(
                msg_id, f"更新任务卡时发生未预期的错误：{exc}", is_error=True
            )
        return _tool_response(msg_id, text)
    return _error(msg_id, -32601, f"未知方法：{method}")


def serve() -> int:
    """主循环：逐行读 stdin（每行一条 JSON-RPC 消息），回应写回 stdout。"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply = _error(None, -32700, "收到无法解析的 JSON 行")
        else:
            try:
                reply = handle_message(message)
            except Exception as exc:  # noqa: BLE001 - 协议层兜底，同上
                print(f"处理消息时未预期的异常：{exc!r}", file=sys.stderr)
                reply = _error(None, -32603, f"内部错误：{exc}")
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(serve())