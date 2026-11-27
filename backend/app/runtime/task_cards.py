"""任务进度卡片：协议解析、合并语义与持久化。

设计见 ``docs/task-progress-card-design.md``。本模块是**框架层自包含**的：只依赖
标准库与 ``models`` / ``db``，不 import 协作域（协作域的工单解析只是算法参照，
见 ``collab/tickets.py``，两边不要互相依赖）。

协议：模型在正文末尾追加一个 ``task_card`` 围栏代码块，内容是一个 JSON 对象：

* 建卡 / 重规划：``{"action":"create","title":"…","items":[{"id":"t1","text":"…"}]}``
* 更新勾选：``{"action":"update","items":[{"id":"t1","done":true}]}``

解析在运行收尾时进行（见 ``service.consume_run``），失败一律「保持原状态」并记日志，
不猜测式修补；客户端没有任何写入口。

另有 MCP 通道（见 ``runtime/task_card_mcp.py``）与内核任务工具桥接（适配器把
``TaskCreate/TaskUpdate/TaskList`` 归一成同一形状的块）。三条输入最终都调用同一个
:func:`apply_block`，合并 / 三态 / 乐观锁语义完全共用，只有幂等策略不同（``source`` 参数）。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from ..db import SessionLocal
from ..models import Message, TaskCard

logger = logging.getLogger(__name__)

FENCE = "task_card"
MAX_SCAN_CHARS = 200_000
MAX_REPAIR_TRIES = 200
MIN_ITEMS, MAX_ITEMS = 2, 20
MAX_ITEM_CHARS, MAX_TITLE_CHARS = 120, 200
MAX_ID_CHARS = 32
MAX_RETRIES = 3

#: 任务项状态。``completed`` 是终态（不允许回退）；``pending`` / ``in_progress``
#: 之间允许来回切换。旧协议只有 ``done``：读旧数据时按 ``done`` 推导状态。
STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"
_STATUS_ALIASES = {
    "completed": STATUS_COMPLETED,
    "complete": STATUS_COMPLETED,
    "done": STATUS_COMPLETED,
    "已完成": STATUS_COMPLETED,
    "完成": STATUS_COMPLETED,
    "in_progress": STATUS_IN_PROGRESS,
    "in-progress": STATUS_IN_PROGRESS,
    "doing": STATUS_IN_PROGRESS,
    "进行中": STATUS_IN_PROGRESS,
    "pending": STATUS_PENDING,
    "todo": STATUS_PENDING,
    "待办": STATUS_PENDING,
    "未开始": STATUS_PENDING,
}

#: 匹配一个 task_card 围栏：``(?!\w)`` 防止把 ``task_cardx`` 之类误当成协议块；
#: 未闭合时一直吃到文本末尾（模型输出被截断也要能剥干净）。
_FENCE_RE = re.compile(
    r"```[ \t]*task_card(?![A-Za-z0-9_])[ \t]*\r?\n?([\s\S]*?)(?:```|\Z)",
    re.IGNORECASE,
)

#: 模型经常把协议块写成 ```json（而不是 ```task_card），甚至直接甩一段裸 JSON。
#: 这类「非 task_card 围栏」**只在正文收尾处**才认：回复中段举例用的 JSON 代码块
#: 不能当成协议块吞掉（见 `test_strip_task_card_keeps_mid_document_json_example`）。
#: 闭合围栏必须一直顶到正文末尾；未闭合（流式截断）另行判定，且其后不能再有 ```。
_JSON_FENCE_CLOSED_RE = re.compile(
    r"```[ \t]*json[ \t]*\r?\n?([\s\S]*?)\r?\n?```[ \t]*\Z",
    re.IGNORECASE,
)
_JSON_FENCE_OPEN_RE = re.compile(
    r"```[ \t]*json(?![A-Za-z0-9_])[ \t]*\r?\n",
    re.IGNORECASE,
)

#: 第 2 级容错：清理不可见字符、中文引号与尾随逗号
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SMART_QUOTES = {
    "“": '"',
    "”": '"',
    "„": '"',
    "‟": '"',
    "‘": "'",
    "’": "'",
    "，": ",",
    "：": ":",
}
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


# --- 判定：是不是「多步任务」------------------------------------------------


#: §7 的动作词表：命中任意一个才允许第一次建卡（阈值故意保守，闲聊不建卡）。
_TASK_WORDS = (
    "实现", "开发", "修复", "重构", "编写", "设计", "调研", "搭建", "部署", "优化",
    "迁移", "测试", "整理", "排查", "检查", "完成", "添加", "支持", "接入", "改造",
    "升级", "生成", "分析", "梳理", "写", "做", "改", "查",
)
#: 列表标记：行首的 ``1.`` / ``-`` / ``•`` / ``①`` 等
_LIST_MARKER = re.compile(r"(?m)^[ \t]*(?:\d+[.、)]|[-*•]|[①-⑳])\s*")

#: 用户明确在说「任务卡 / 进度卡 / 任务列表」时，**不受 10 字阈值限制**：
#: 这类短句通常就是「建一张卡」「看下进度」，不注入协议的话，模型会改用内核自带的
#: 任务工具（例如 TaskCreate/TaskList），那些数据平台看不到，界面自然画不出卡片。
_CARD_WORDS = ("任务卡", "进度卡", "任务列表", "待办列表", "待办", "进度")


def looks_like_task_request(text: str) -> bool:
    """用户正文像不像一件需要多步完成的任务（§7 的服务端硬闸门）。

    先看显式的卡片/进度词（短句也认，例如「看下任务卡」）；否则要求去空白后至少
    10 个字符，且（命中任务动作词 或 含清单结构）。短句「好的」「继续」「这是什么」
    不会命中，因此不建卡。
    """
    body = (text or "").strip()
    if any(word in body for word in _CARD_WORDS):
        return True
    if len(body) < 10:
        return False
    if any(word in body for word in _TASK_WORDS):
        return True
    if len(_LIST_MARKER.findall(body)) >= 2:
        return True
    # 两个以上以「；」分隔的祈使短句（至少一个分隔符）
    if body.count("；") + body.count(";") >= 1:
        return True
    return False


# --- 文本清理：剥离协议块（落库 / 实时流 / IM 三处共用同一套语义）-------------


def strip_task_card(text: str) -> str:
    """删掉协议块，保留其余正文。

    先按 ``task_card`` 围栏剥（含未闭合）；再补一刀：模型写成 ```json 或裸 JSON
    且位于正文收尾处的任务卡块同样剥掉（只认收尾位置，中段示例不动）。
    """
    if not text:
        return ""
    cleaned = _FENCE_RE.sub("", text).rstrip()
    tail = _find_tail_block(cleaned)
    if tail is not None:
        cleaned = cleaned[: tail[1]].rstrip()
    return cleaned


def _strip_stream(text: str, in_fence: bool) -> tuple[str, bool]:
    """按字符流剥离围栏：返回（清理后的文本，结尾是否仍在围栏内）。

    单独实现是因为 ``parts`` 里的围栏可能被思考 / 工具片段打断，不能只用一条正则。
    """
    out: list[str] = []
    pos = 0
    length = len(text)
    while pos < length:
        if in_fence:
            end = text.find("```", pos)
            if end < 0:
                return "".join(out), True
            pos = end + 3
            in_fence = False
            continue
        match = _OPEN_FENCE_RE.search(text, pos)
        if match is None:
            out.append(text[pos:])
            return "".join(out), False
        out.append(text[pos : match.start()])
        newline = text.find("\n", match.end())
        pos = length if newline < 0 else newline + 1
        in_fence = True
    return "".join(out), in_fence


_OPEN_FENCE_RE = re.compile(r"```[ \t]*task_card(?![A-Za-z0-9_])[^\n]*", re.IGNORECASE)


def strip_task_card_from_parts(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """跨 ``parts`` 片段剥离任务卡协议块（状态机），非 text 片段原样保留。

    状态机只认 ``task_card`` 围栏；模型写成 ```json / 裸 JSON 时块一定在整条回复
    的收尾处，也就是最后一段 text 里 —— 再对最后一段补一刀收尾剥离。
    """
    result: list[dict[str, Any]] = []
    in_fence = False
    for part in parts or []:
        if not isinstance(part, dict) or part.get("kind") != "text":
            result.append(part)
            continue
        original = str(part.get("text") or "")
        cleaned, in_fence = _strip_stream(original, in_fence)
        if cleaned.strip():
            result.append({**part, "text": cleaned})
        elif original.strip():
            # 整段都在协议块里：保留一个空 text 片段会渲染出空气泡，直接丢掉。
            continue
        else:
            result.append(part)
    # 结尾仍在未闭合围栏里：后面已经没有可展示的正文，交给上面的状态机处理即可。
    if in_fence or not result:
        return result
    # 非标准块只认「整条回复的收尾处」。时间线最后一段不是 text（例如以工具结果收尾）时，
    # 前面那段 text 并不是回复结尾，不能剥，否则会吞掉中段的 JSON 示例。
    last = result[-1]
    if not isinstance(last, dict) or last.get("kind") != "text":
        return result
    original = str(last.get("text") or "")
    cleaned = strip_task_card(original)
    if cleaned == original:
        return result
    if cleaned.strip():
        result[-1] = {**last, "text": cleaned}
    elif original.strip():
        del result[-1]
    return result


# --- 解析：三级容错 ---------------------------------------------------------


def _sanitize_json_text(text: str) -> str:
    cleaned = text.replace("﻿", "")
    cleaned = _CTRL.sub("", cleaned)
    for src, dst in _SMART_QUOTES.items():
        cleaned = cleaned.replace(src, dst)
    for _ in range(3):
        new = _TRAILING_COMMA.sub(r"\1", cleaned)
        if new == cleaned:
            break
        cleaned = new
    return cleaned.strip()


def _scan_objects(text: str) -> list[str]:
    """扫描出正文里所有平衡的 ``{...}``（先内层后外层）。"""
    if len(text) > MAX_SCAN_CHARS:
        text = text[:MAX_SCAN_CHARS]
    found: list[str] = []
    stack: list[int] = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            stack.append(index)
        elif char == "}" and stack:
            found.append(text[stack.pop() : index + 1])
    return found


def _close_unbalanced(text: str) -> str | None:
    """补齐未闭合的 ``{`` / ``[``（必要时先补引号）；无法补救时返回 None。"""
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]" and stack:
            stack.pop()
    if not stack and not in_string:
        return None
    closers = "".join("}" if char == "{" else "]" for char in reversed(stack))
    if in_string:
        text += '"'
    return text + closers


def _looks_like_block(obj: Any) -> bool:
    """结构上像任务卡协议块：有 ``action``，或 ``items`` 是一组结构化任务项。

    只认 ``action`` 标注，或「items 里是对象且带 id / text / done」的块 —— 单纯一个
    ``{"items": [...]}`` 的配置示例不能被当成任务卡（否则 ``strip_task_card`` 会把
    用户可见正文剥掉）。判据与前端 ``lib/taskCardFence.ts`` 的 ``looksLikeTaskBlock``
    保持一致。
    """
    if not isinstance(obj, dict):
        return False
    if "action" in obj:
        return True
    items = obj.get("items")
    return isinstance(items, list) and any(
        isinstance(item, dict) and ("id" in item or "text" in item or "done" in item)
        for item in items
    )


def _repair_truncated(text: str) -> dict[str, Any] | None:
    """输出被截断时的兜底：先回退到最后一个完整 ``}``，再尝试补齐括号。"""
    start = text.find("{")
    if start < 0:
        return None
    body = text[start:]
    candidates: list[str] = []
    closes = [index for index, char in enumerate(body) if char == "}"]
    candidates.extend(body[: index + 1] for index in reversed(closes[-MAX_REPAIR_TRIES:]))
    closed = _close_unbalanced(body)
    if closed:
        candidates.append(closed)
        cut = body.rfind(",")
        if cut > 0:
            again = _close_unbalanced(body[:cut])
            if again:
                candidates.append(again)
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if _looks_like_block(obj):
            return obj
    return None


def _extract_json(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    """三级容错：严格 → 清理后 → 扫描平衡对象 / 截断修复。"""
    text = (raw or "").strip()
    if not text:
        return None, "task_card 块是空的"
    cleaned = _sanitize_json_text(text)
    last_error: str | None = None
    for candidate in (text, cleaned):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = last_error or f"task_card 块不是合法 JSON：{exc.msg}"
            continue
        if isinstance(obj, dict):
            return obj, None
        last_error = last_error or "task_card 块必须是一个 JSON 对象"
    # 扫描结果先内层后外层；倒过来优先取最外层的整块。
    for piece in reversed(_scan_objects(cleaned)):
        try:
            obj = json.loads(piece)
        except json.JSONDecodeError:
            continue
        if _looks_like_block(obj):
            return obj, None
    repaired = _repair_truncated(cleaned)
    if repaired is not None:
        return repaired, None
    return None, last_error or "没有找到可解析的 task_card 对象"


def _body_is_task_block(body: str) -> bool:
    """这段 JSON 像不像任务卡协议块（有 ``action`` 或结构化的 ``items``）。"""
    if not body.strip():
        return False
    obj, _reason = _extract_json(body)
    return obj is not None and _looks_like_block(obj)


def _tail_bare_block(text: str) -> tuple[str, int] | None:
    """正文以裸 JSON 收尾时，取出最外层的那个对象（返回正文与起点）。

    从后往前试 ``{`` 起点：先命中的往往是内层任务项（没有 ``items`` / ``action``），
    一路退到真正的外层对象；最多试 ``MAX_REPAIR_TRIES`` 个起点，避免长文本拖死。
    """
    stripped = text.rstrip()
    if not stripped.endswith("}"):
        return None
    starts = [index for index, char in enumerate(stripped) if char == "{"]
    for start in reversed(starts[-MAX_REPAIR_TRIES:]):
        body = stripped[start:]
        if _body_is_task_block(body):
            return body, start
    return None


def _find_tail_block(text: str) -> tuple[str, int, int] | None:
    """识别正文**收尾处**的 ```json 围栏 / 裸 JSON 任务卡块。

    返回 ``(块正文, 起点, 终点)``；找不到返回 ``None``。只认收尾位置是关键：
    回复中段讲解用的 JSON 示例（哪怕字段长得像卡片）必须原样保留。
    """
    stripped = text.rstrip()
    if not stripped:
        return None
    fenced = _JSON_FENCE_CLOSED_RE.search(stripped)
    if fenced is not None:
        body = fenced.group(1).strip()
        if _body_is_task_block(body):
            return body, fenced.start(), len(stripped)
        return None
    # 未闭合：最后一个 ```json 之后不能再出现 ```，否则那是「中段示例 + 后续正文」。
    last_open = None
    for match in _JSON_FENCE_OPEN_RE.finditer(stripped):
        last_open = match
    if last_open is not None:
        body = stripped[last_open.end() :].strip()
        if "```" not in body and _body_is_task_block(body):
            return body, last_open.start(), len(stripped)
        return None
    bare = _tail_bare_block(stripped)
    if bare is not None:
        body, start = bare
        return body.strip(), start, len(stripped)
    return None


def _as_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "done", "已完成")
    return False


def _as_status(value: Any) -> str | None:
    """把 ``status`` 规整成三种标准值；认不出来返回 ``None``。"""
    if value is None:
        return None
    return _STATUS_ALIASES.get(_as_text(value).lower())


def _item_status(item: dict[str, Any]) -> str:
    """读任务项状态：优先 ``status``，没有就按旧 ``done`` 推导。"""
    status = _as_status(item.get("status"))
    if status is not None:
        return status
    return STATUS_COMPLETED if _as_bool(item.get("done")) else STATUS_PENDING


def public_items(items: Any) -> list[dict[str, Any]]:
    """给接口 / 提示词的规整视图：每项都带 ``status`` 与派生 ``done``。

    旧卡片行里只有 ``done``；这里不为它们写库，只在读取时补齐，保证前端和
    模型看到的是同一套三态字段。
    """
    out: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        status = _item_status(item)
        out.append({**item, "status": status, "done": status == STATUS_COMPLETED})
    return out


def _clean_title(value: Any) -> str:
    return _as_text(value)[:MAX_TITLE_CHARS]


def _clean_item_text(value: Any) -> str:
    return _as_text(value)[:MAX_ITEM_CHARS]


def _clean_item_id(value: Any, index: int) -> str:
    item_id = _as_text(value)[:MAX_ID_CHARS]
    return item_id or f"t{index + 1}"


def _create_items(raw_items: Any) -> tuple[list[dict[str, Any]] | None, str | None]:
    """把 create 的 items 规整成 ``{id,text,status,done}``：去空、去重、截断、2～20 项。"""
    if not isinstance(raw_items, list):
        return None, "create 的 items 必须是数组"
    seen: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            continue
        text = _clean_item_text(item.get("text"))
        if not text:
            continue
        item_id = _clean_item_id(item.get("id"), index)
        raw_status = item.get("status")
        if raw_status is None:
            status = STATUS_COMPLETED if _as_bool(item.get("done")) else STATUS_PENDING
        else:
            status = _as_status(raw_status)
            if status is None:
                return None, (
                    f"create 的任务项 status 不合法：{raw_status}"
                    "（只能是 pending / in_progress / completed）"
                )
        if item_id not in seen:
            order.append(item_id)
        seen[item_id] = {
            "id": item_id,
            "text": text,
            "status": status,
            "done": status == STATUS_COMPLETED,
        }
    items = [seen[item_id] for item_id in order]
    if len(items) < MIN_ITEMS:
        return None, f"create 的有效任务项不足 {MIN_ITEMS} 个，已忽略（不建空卡）"
    if len(items) > MAX_ITEMS:
        return None, f"create 的任务项超过 {MAX_ITEMS} 个，已忽略"
    return items, None


def _update_items(raw_items: Any) -> tuple[list[dict[str, Any]] | None, str | None]:
    """把 update 的 items 规整成 ``{id,status,done}``：同一 id 出现多次时按最后一次为准。

    * 带 ``status``：按三态更新；
    * 只有 ``done:true``：兼容旧协议，等价于 ``status=completed``；
    * 只有 ``done:false``：保持旧协议语义——不改状态（不能把已完成项改回去）。
    """
    if not isinstance(raw_items, list) or not raw_items:
        return None, "update 的 items 必须是非空数组"
    seen: dict[str, dict[str, Any]] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        item_id = _as_text(item.get("id"))[:MAX_ID_CHARS]
        if not item_id:
            continue
        raw_status = item.get("status")
        if raw_status is not None:
            status = _as_status(raw_status)
            if status is None:
                return None, (
                    f"update 的任务项 status 不合法：{raw_status}"
                    "（只能是 pending / in_progress / completed）"
                )
            seen[item_id] = {
                "id": item_id,
                "status": status,
                "done": status == STATUS_COMPLETED,
            }
            continue
        done = _as_bool(item.get("done"))
        if done:
            seen[item_id] = {"id": item_id, "status": STATUS_COMPLETED, "done": True}
        else:
            # 旧协议 done:false 是 no-op；显式回到 pending 必须用 status。
            seen[item_id] = {"id": item_id, "done": False}
    if not seen:
        return None, "update 没有任何有效任务项"
    return list(seen.values()), None


def normalize_block(raw: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """校验并规整一个已解析的 JSON 对象。

    返回的块里 ``action`` 可能是 ``"auto"``（模型没写 action）：由 ``apply_block``
    按「会话是否已有卡」决定 create 还是 update。
    """
    if not isinstance(raw, dict):
        return None, "task_card 块必须是一个 JSON 对象"
    action = _as_text(raw.get("action")).lower()
    title = _clean_title(raw.get("title"))
    if not action:
        # 没写 action：按 items 的形状先做一次通用规整，落到库时再按有无卡判定。
        items, error = _create_items(raw.get("items"))
        if items is None:
            # 也可能是只报了 id/done 的 update
            update_items, update_error = _update_items(raw.get("items"))
            if update_items is None:
                return None, error or update_error
            return {"action": "auto", "title": title, "items": update_items}, None
        return {"action": "auto", "title": title, "items": items}, None
    if action == "create":
        items, error = _create_items(raw.get("items"))
        if items is None:
            return None, error
        return {"action": "create", "title": title, "items": items}, None
    if action == "update":
        items, error = _update_items(raw.get("items"))
        if items is None:
            return None, error
        return {"action": "update", "title": title, "items": items}, None
    return None, f"不认识的 action：{action}（只允许 create / update）"


def parse_task_card(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """从模型输出里取出最后一个任务卡片块并规整。

    一轮出现多个块时按最后一个生效并记告警（与工单解析的容错策略一致）。
    兼容模型把块写成 ```json 或裸 JSON 的情形，但只认正文收尾处，避免误解析中段示例。
    没有可解析的块时返回 ``(None, None)``：这是正常情况，不算解析失败。
    """
    if not text:
        return None, None
    bodies = [match.group(1) for match in _FENCE_RE.finditer(text)]
    if bodies:
        if len(bodies) > 1:
            logger.warning("检测到 %d 个 task_card 代码块，已按最后一个解析", len(bodies))
        body = bodies[-1]
    else:
        tail = _find_tail_block(text)
        if tail is None:
            return None, None
        body = tail[0]
    obj, reason = _extract_json(body)
    if obj is None:
        return None, reason
    return normalize_block(obj)


# --- 合并语义（纯函数，便于单测）--------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_action(block: dict[str, Any], has_card: bool) -> dict[str, Any] | None:
    """把 ``auto`` 按会话有无卡落成 create / update。"""
    if block.get("action") in ("create", "update"):
        return block
    items = block.get("items") or []
    if has_card:
        update_items = [
            {
                "id": item["id"],
                "done": bool(item.get("done")),
                **({"status": item["status"]} if item.get("status") else {}),
            }
            for item in items
            if isinstance(item, dict) and item.get("id")
        ]
        if not update_items:
            return None
        return {"action": "update", "title": "", "items": update_items}
    create_items = [
        {
            "id": item["id"],
            "text": item.get("text") or "",
            "done": bool(item.get("done")),
            **({"status": item["status"]} if item.get("status") else {}),
        }
        for item in items
        if isinstance(item, dict) and item.get("id")
    ]
    cleaned, _error = _create_items(create_items)
    if cleaned is None:
        return None
    return {"action": "create", "title": block.get("title") or "", "items": cleaned}


def merge_block(
    existing_items: list[dict[str, Any]],
    existing_title: str,
    block: dict[str, Any],
    *,
    now: str,
) -> dict[str, Any] | None:
    """按 §6.5 的合并语义算出新的任务项。

    返回 ``{"title", "items", "changed"}``；``None`` 表示整块忽略（不写库）。
    ``changed == False`` 时不 bump 版本号。
    """
    action = block.get("action")
    if action == "update":
        return _merge_update(existing_items, existing_title, block, now=now)
    if action == "create":
        return _merge_create(existing_items, existing_title, block, now=now)
    return None


def _merge_create(
    existing_items: list[dict[str, Any]],
    existing_title: str,
    block: dict[str, Any],
    *,
    now: str,
) -> dict[str, Any]:
    old_map = {
        str(item.get("id")): item for item in existing_items if isinstance(item, dict) and item.get("id")
    }
    new_items: list[dict[str, Any]] = []
    changed = len(existing_items) != len(block["items"])
    for incoming in block["items"]:
        item_id = incoming["id"]
        text = incoming.get("text") or ""
        incoming_status = _item_status(incoming)
        old = old_map.get(item_id)
        if old is None:
            new_items.append(
                {
                    "id": item_id,
                    "text": text,
                    "status": incoming_status,
                    "done": incoming_status == STATUS_COMPLETED,
                    "done_at": now if incoming_status == STATUS_COMPLETED else None,
                    "updated_at": now,
                }
            )
            continue
        old_status = _item_status(old)
        # 已完成的项永远不因重规划回退；其余状态按新计划来（允许 in_progress ↔ pending）。
        merged_status = (
            STATUS_COMPLETED
            if old_status == STATUS_COMPLETED or incoming_status == STATUS_COMPLETED
            else incoming_status
        )
        item_changed = merged_status != old_status or text != (old.get("text") or "")
        if merged_status == STATUS_COMPLETED and old_status != STATUS_COMPLETED:
            done_at = now
        else:
            done_at = old.get("done_at")
        new_items.append(
            {
                "id": item_id,
                "text": text,
                "status": merged_status,
                "done": merged_status == STATUS_COMPLETED,
                "done_at": done_at,
                "updated_at": now if item_changed else (old.get("updated_at") or now),
            }
        )
        changed = changed or item_changed
    title = block.get("title") or existing_title
    changed = changed or title != existing_title
    return {"title": title, "items": new_items, "changed": changed}


def _merge_update(
    existing_items: list[dict[str, Any]],
    existing_title: str,
    block: dict[str, Any],
    *,
    now: str,
) -> dict[str, Any] | None:
    if not existing_items:
        # update 永远不能凭空造卡：从根上杜绝空卡片
        logger.warning("任务进度卡片：收到 update 但会话没有卡片，已忽略")
        return None
    items = [dict(item) for item in existing_items if isinstance(item, dict)]
    # 旧行只有 done：这里统一补一次 status，写回后就是完整三态。
    for item in items:
        item["status"] = _item_status(item)
        item["done"] = item["status"] == STATUS_COMPLETED
    by_id = {str(item.get("id")): item for item in items if item.get("id")}
    changed = False
    for incoming in block["items"]:
        item_id = incoming["id"]
        target = by_id.get(item_id)
        if target is None:
            logger.warning("任务进度卡片：忽略未知任务项 id=%s", item_id)
            continue
        old_status = _item_status(target)
        if old_status == STATUS_COMPLETED:
            # completed 是终态：显式想改回去、或旧协议 done:false，都忽略。
            if incoming.get("status") is not None or incoming.get("done") is False:
                logger.warning("任务进度卡片：忽略把已完成项改回未完成 id=%s", item_id)
            continue
        incoming_status = _as_status(incoming.get("status"))
        if incoming_status is None:
            # 旧协议：done:true 等价 completed；done:false 是 no-op（上面已排除已完成项）。
            if not _as_bool(incoming.get("done")):
                continue
            incoming_status = STATUS_COMPLETED
        if incoming_status == old_status:
            continue
        target["status"] = incoming_status
        target["done"] = incoming_status == STATUS_COMPLETED
        if incoming_status == STATUS_COMPLETED:
            target["done_at"] = now
        target["updated_at"] = now
        changed = True
    return {"title": existing_title, "items": items, "changed": changed}


# --- 协议说明（注入提示词）--------------------------------------------------


def render_notice(card: TaskCard | None, allow_create: bool) -> str:
    """生成追加到 ``ctx.prompt`` 末尾的协议说明。"""
    if card is None and not allow_create:
        return ""
    lines = [
        "【任务进度卡片协议】",
        "当本轮确实在推进一件需要多步完成的工作时，在正文最后追加一个 task_card 围栏代码块"
        "（围栏的语言标签必须原样写 task_card，不要写成 json 或别的；整轮只能有一个）：",
        '- 首次给出计划：{"action":"create","title":"一句话任务名","items":[{"id":"t1","text":"步骤一","status":"pending"},{"id":"t2","text":"步骤二","status":"pending"}]}',
        '- 后续轮次只报告状态变化：{"action":"update","items":[{"id":"t1","status":"in_progress"}]}',
        "规则：items 数量 2~20；id 短且稳定；status 只能是 pending / in_progress / completed；"
        "completed 是终态，不能回退；仍兼容 done:true（=completed）与 done:false（=不改状态）；"
        "闲聊、问答、单步指令不要输出这个块。",
        "平台进度卡只认这个 task_card 围栏块；内核自带的任务 / 待办工具不会显示在界面上，不要用它们代替。",
    ]
    if card is None:
        lines.append("当前卡片：无")
    else:
        items = public_items(card.items or [])
        done = sum(1 for item in items if item["status"] == STATUS_COMPLETED)
        doing = sum(1 for item in items if item["status"] == STATUS_IN_PROGRESS)
        lines.append(
            f"当前卡片：{card.title or '任务进度'}"
            f"（{done}/{len(items)} 已完成，{doing} 进行中）"
        )
        labels = {
            STATUS_COMPLETED: "已完成",
            STATUS_IN_PROGRESS: "进行中",
            STATUS_PENDING: "待办",
        }
        for item in items:
            lines.append(f"- {item.get('id')} [{labels[item['status']]}] {item.get('text')}")
    return "\n".join(lines)


# --- 持久化 -----------------------------------------------------------------


async def load_card(session: Any, conversation_id: str) -> TaskCard | None:
    """按会话 id 读卡片（唯一索引保证最多一行）。"""
    result = await session.execute(
        select(TaskCard).where(TaskCard.conversation_id == conversation_id)
    )
    return result.scalars().first()


async def _valid_anchor(session: Any, conversation_id: str, anchor_message_id: str | None) -> str | None:
    """锚点只能是同一对话的助手消息；不合法时返回 None（界面回退到末尾）。"""
    if not anchor_message_id:
        return None
    row = await session.get(Message, anchor_message_id)
    if row is not None and row.conversation_id == conversation_id and row.role == "assistant":
        return anchor_message_id
    return None


async def apply_block(
    conversation_id: str,
    anchor_message_id: str | None,
    block: dict[str, Any],
    run_id: str,
    *,
    source: str = "fence",
) -> TaskCard | None:
    """把一轮解析出的块落到 ``task_cards``。

    两条输入通道共用这一个函数，只有幂等策略不同：

    * ``source="fence"``（正文围栏）：``last_run_id`` 保证同一轮重复应用幂等；
    * ``source="kernel"``（适配器桥接的内核任务快照）：同样一轮只应用一次；
    * ``source="mcp"``（工具通道）：一轮允许多次更新（建计划、完成第一步、完成第二步），
      **不能**按 ``run_id`` 跳过，否则第二次以后的更新会被整块吞掉；
      重复提交同一内容时由 ``merge_block`` 的 ``changed=False`` 兜底。

    其余行为完全一致：

    * 使用独立 ``SessionLocal``：卡片写入失败绝不影响消息落库；
    * 唯一约束防并发建卡（撞约束后重读转更新）；
    * 乐观锁（``WHERE version = :v``）防并发覆盖，冲突最多重试 3 次；
    * 成功写入时更新 ``last_run_id``（它是「最后一次成功写卡的 run」，与幂等判定解耦）；
    * 任何异常只记日志并返回 None（后台增强失败不打扰用户）。
    """
    try:
        async with SessionLocal() as session:
            for _attempt in range(MAX_RETRIES):
                card = await load_card(session, conversation_id)
                resolved = _resolve_action(block, has_card=card is not None)
                if resolved is None:
                    return card
                if card is None:
                    if resolved["action"] != "create":
                        return None
                    anchor = await _valid_anchor(session, conversation_id, anchor_message_id)
                    merged = merge_block([], "", resolved, now=_now_iso())
                    if merged is None:
                        return None
                    row = TaskCard(
                        conversation_id=conversation_id,
                        anchor_message_id=anchor,
                        title=merged["title"],
                        items=merged["items"],
                        version=1,
                        last_run_id=run_id,
                    )
                    session.add(row)
                    try:
                        await session.commit()
                    except IntegrityError:
                        # 并发建卡：另一个运行先插入成功；回到循环重读后按 update 合并。
                        await session.rollback()
                        continue
                    return row

                if source in ("fence", "kernel") and card.last_run_id == run_id:
                    # 围栏 / 内核快照通道：同一轮重复应用（重试 / 重放）幂等跳过。
                    # MCP 通道一轮内允许多次更新，不能按 run 跳过（设计 §6.3）。
                    return card

                merged = merge_block(card.items or [], card.title, resolved, now=_now_iso())
                if merged is None:
                    return card
                if not merged["changed"]:
                    # 没有任何状态变化：不写库、不 bump version
                    return card

                # 锚点只在「重新规划（create）」时随最新计划移动；普通 update 保持原位置，
                # 否则每轮报一次进度都会把卡片往对话末尾拖（设计 §6.5）。
                # 注意：卡片当前已固定在输入框上方渲染，anchor 仅为接口兼容/历史数据保留。
                anchor = card.anchor_message_id
                if resolved["action"] == "create":
                    anchor = (
                        await _valid_anchor(session, conversation_id, anchor_message_id)
                        or card.anchor_message_id
                    )
                version = int(card.version or 1)
                result = await session.execute(
                    sa_update(TaskCard)
                    .where(
                        TaskCard.id == card.id,
                        TaskCard.conversation_id == conversation_id,
                        TaskCard.version == version,
                    )
                    .values(
                        title=merged["title"],
                        items=merged["items"],
                        anchor_message_id=anchor,
                        version=version + 1,
                        last_run_id=run_id,
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                if result.rowcount == 0:
                    # 并发写入冲突：重读后重新合并
                    await session.rollback()
                    continue
                await session.commit()
                return await load_card(session, conversation_id)
            logger.warning("任务进度卡片写入冲突，放弃本次更新：conversation=%s", conversation_id)
            return None
    except Exception:  # noqa: BLE001 - 后台增强失败只留日志，不打扰用户回复
        logger.warning("任务进度卡片写入失败，已忽略：conversation=%s", conversation_id, exc_info=True)
        return None
