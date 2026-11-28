"""任务进度卡片 · MCP 通道的每轮能力绑定。

设计见 ``docs/功能文档/任务进度卡.md``。围栏方案（模型在正文末尾输出
``task_card`` 代码块）继续服务其余内核；本模块只负责「模型通过工具把进度送进来」
这条通道的身份、令牌与生命周期 —— 卡片语义、合并规则与存储**全部复用**
:mod:`app.runtime.task_cards`。

一次运行的完整链路：

1. ``prepare_run`` 生成每轮 :func:`begin`，把 ``context_id`` 拼进本轮提示词；
2. MCP 物化（``prepare_mcp_home``）之后，:func:`write_credentials` 把令牌写进
   该对话的内核隔离目录（``task-card/context-<hash>/auth.json``，0600）；
3. 内置 stdio server 带着 ``context_id`` 调平台内部接口，服务端用 Bearer 令牌
   :func:`authenticate` 解出绑定，归属（conversation / run）只来自绑定；
4. 运行收尾 :func:`finish` 吊销令牌并删除凭据目录（成功、失败、取消都要调）。

安全要点：

* ``context_id`` 每轮随机、不入库、轮末失效，不能拿它反查别的对话；
* 令牌只存哈希在内存，明文只在凭据文件里；日志只记 ``context_id[:8]``；
* 凭据目录放在**该对话的内核家目录**内，开了「仅工作目录」的内核物理读不到别的对话；
* 多进程部署时内存绑定不共享。当前服务按单进程运行；启用多 worker 必须把绑定与
  令牌哈希迁到数据库 / Redis，或在网关层做粘性会话，否则请求可能落到另一个 worker
  而返回 401。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from ..models import TaskCard
from . import task_cards as task_card_service

logger = logging.getLogger(__name__)

#: 令牌前缀：与用户 PAT（``pat_``）刻意区分，避免混用。
TOKEN_PREFIX = "tc_"
#: 每个 context 允许的调用次数上限（防模型死循环刷写库）。
MAX_CALLS = 120
#: 凭据目录（内核家目录下的相对路径）
CREDENTIALS_SUBDIR = "task-card"
AUTH_FILE = "auth.json"


@dataclass
class TaskCardContext:
    """一轮运行的任务卡能力绑定（只在进程内存里）。"""

    context_id: str
    conversation_id: str
    owner_id: str | None
    run_id: str = ""
    anchor_message_id: str | None = None
    token_hash: str = ""
    active: bool = True
    #: 本轮是否已经通过 MCP 成功写过卡片；收尾据此决定要不要再解析围栏。
    mcp_used: bool = False
    calls: int = 0
    created_at: float = 0.0
    last_call_at: float = 0.0
    #: 凭据根目录（``<home>/task-card``），收尾时整目录删除。
    credentials_root: Path | None = None


_contexts: dict[str, TaskCardContext] = {}
#: token 哈希 → context_id（轮末随绑定一起删除）
_by_token: dict[str, str] = {}
#: 尚未写进凭据文件的明文令牌：``begin`` 先生成，家目录物化后再落盘。
#: 落盘成功即删除；进程内存里不长期保留明文。
_pending_tokens: dict[str, str] = {}


# --- 路径与摘要 ---------------------------------------------------------------


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def context_digest(context_id: str) -> str:
    """凭据目录名只由 ``sha256(context_id)`` 拼成，绝不用原始字符串做路径。"""
    return hashlib.sha256(context_id.encode("utf-8")).hexdigest()[:16]


def credential_rel_path(context_id: str) -> Path:
    """凭据文件相对内核家目录的路径（MCP server 也按这个形状查找）。"""
    return Path(CREDENTIALS_SUBDIR) / f"context-{context_digest(context_id)}" / AUTH_FILE


# --- 生命周期 -----------------------------------------------------------------


def begin(
    conversation_id: str,
    owner_id: str | None,
    home_dir: str | Path | None = None,
) -> tuple[str, Path | None]:
    """为新一轮生成 ``context_id`` 与令牌，登记绑定。

    ``home_dir`` 已知时立即写凭据文件（返回凭据根目录）；未知时先返回 ``None``，
    等 ``prepare_mcp_home`` 物化出内核家目录后再调 :func:`write_credentials`。
    """
    context_id = secrets.token_hex(16)
    token = f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    now = time.time()
    binding = TaskCardContext(
        context_id=context_id,
        conversation_id=conversation_id,
        owner_id=owner_id,
        token_hash=_token_hash(token),
        created_at=now,
    )
    _contexts[context_id] = binding
    _by_token[binding.token_hash] = context_id
    _pending_tokens[context_id] = token
    root: Path | None = None
    if home_dir is not None:
        root = write_credentials(context_id, home_dir)
    logger.debug(
        "任务卡 MCP 上下文已创建：context=%s conversation=%s",
        context_id[:8],
        conversation_id,
    )
    return context_id, root


def write_credentials(context_id: str, home_dir: str | Path) -> Path | None:
    """把令牌写进内核家目录的 ``task-card/context-<hash>/auth.json``（0600）。

    目录与文件权限都收紧；写入失败只记日志并返回 ``None``（本轮工具拿不到凭据，
    但绝不能因此拖垮整个运行）。幂等：已落过盘时直接返回原目录，不重复写。
    """
    binding = _contexts.get(context_id)
    if binding is None or not binding.active:
        return None
    token = _pending_tokens.get(context_id)
    if not token:
        # 已经落过盘：返回原目录即可，不重复写。
        return binding.credentials_root
    root = Path(home_dir) / CREDENTIALS_SUBDIR
    # 先登记根目录：即使后续写文件失败，finish() 也能把残留目录一并清掉。
    binding.credentials_root = root
    target_dir = root / f"context-{context_digest(context_id)}"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        os.chmod(target_dir, 0o700)
        target = target_dir / AUTH_FILE
        target.write_text(
            json.dumps({"token": token, "context_id": context_id}) + "\n",
            encoding="utf-8",
        )
        os.chmod(target, 0o600)
    except OSError as exc:
        logger.warning(
            "任务卡凭据写入失败：context=%s home=%s 原因=%s",
            context_id[:8],
            home_dir,
            exc,
        )
        return None
    _pending_tokens.pop(context_id, None)
    return root


def bind(
    context_id: str,
    conversation_id: str,
    owner_id: str | None,
    run_id: str,
    anchor_message_id: str | None,
) -> None:
    """运行注册之后补齐归属；``conversation_id`` 以这里的绑定为准。"""
    binding = _contexts.get(context_id)
    if binding is None:
        return
    binding.conversation_id = conversation_id
    binding.owner_id = owner_id
    binding.run_id = run_id
    binding.anchor_message_id = anchor_message_id


def authenticate(authorization: str | None) -> TaskCardContext | None:
    """校验 ``Authorization: Bearer tc_...``；返回活跃绑定，失败返回 ``None``。"""
    raw = (authorization or "").strip()
    if not raw:
        return None
    parts = raw.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    if not token.startswith(TOKEN_PREFIX):
        return None
    context_id = _by_token.get(_token_hash(token))
    binding = _contexts.get(context_id or "")
    if binding is None or not binding.active:
        return None
    return binding


def read_token(context_id: str) -> str | None:
    """仅供测试 / 排障：从凭据文件读回明文令牌（不写日志、不回接口）。

    生产路径不会用它：服务端只认请求头里的令牌。
    """
    binding = _contexts.get(context_id)
    root = binding.credentials_root if binding is not None else None
    if root is None:
        return None
    path = root / f"context-{context_digest(context_id)}" / AUTH_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    token = str(data.get("token") or "")
    return token if token.startswith(TOKEN_PREFIX) else None


def mark_used(context_id: str) -> None:
    """记录一次成功的 MCP 写卡；同时累计调用次数（限流用）。"""
    binding = _contexts.get(context_id)
    if binding is None:
        return
    binding.mcp_used = True
    binding.calls += 1
    binding.last_call_at = time.time()


def was_used(context_id: str | None) -> bool:
    """本轮是否已经通过 MCP 成功写过卡片（收尾判断双写）。"""
    if not context_id:
        return False
    binding = _contexts.get(context_id)
    return bool(binding is not None and binding.mcp_used)


def finish(context_id: str | None, status: str) -> None:
    """轮末收口：失效绑定、删令牌索引、删本轮凭据目录。幂等。

    成功 / 失败 / 取消都调它；进程被杀时绑定随内存消失，令牌自然也失效。

    只删本轮自己的 ``context-<hash>/``，不删整个 ``task-card/``：同一对话理论上
    可能有两轮重叠（API 没有强制串行），整目录删除会误伤另一轮仍在使用的令牌。
    删完顺手 ``rmdir`` 空的 ``task-card/``；异常残留的旧目录里的令牌已随绑定失效，
    下一次同对话运行也不会复用。
    """
    if not context_id:
        return
    binding = _contexts.pop(context_id, None)
    _pending_tokens.pop(context_id, None)
    if binding is not None:
        binding.active = False
        _by_token.pop(binding.token_hash, None)
        root = binding.credentials_root
        if root is not None:
            shutil.rmtree(root / f"context-{context_digest(context_id)}", ignore_errors=True)
            try:
                root.rmdir()  # 只有空目录才删得掉；别的运行还在用就保留
            except OSError:
                pass
        logger.info(
            "任务卡 MCP 上下文已失效：context=%s status=%s calls=%s",
            context_id[:8],
            status,
            binding.calls,
        )


# --- 提示词 -------------------------------------------------------------------


def render_notice(context_id: str, card: TaskCard | None) -> str:
    """生成 MCP 版协议说明（注入到本轮 ``ctx.prompt`` 末尾）。

    与围栏版文案保持同一套规则，只是把「输出代码块」换成「调用工具」。
    当前卡片逐项列出，让模型知道哪些已经完成、不要重复报。
    """
    lines = [
        "【任务进度卡片协议 · MCP】",
        "当本轮确实在推进一件需要多步完成的工作时，调用 coda-task-card 的 "
        "update_task_card 工具：",
        '- 首次给出计划：{"context_id":"<本轮 context_id>","action":"create",'
        '"title":"一句话任务名","items":[{"id":"t1","text":"步骤一","status":"pending"},'
        '{"id":"t2","text":"步骤二","status":"pending"}]}',
        '- 后续轮次只报告状态变化：{"context_id":"<本轮 context_id>",'
        '"action":"update","items":[{"id":"t1","status":"in_progress"}]}',
        "规则：只使用本轮输入给出的 context_id，不要复用历史；create 时 items 数量 "
        "2~20、每项带 text；update 时只报变化项（≥1 项，只需 id 和 status；"
        "旧协议 done:true 仍等价于 completed，done:false 不改状态）；id 短且稳定；"
        "status 只能是 pending / in_progress / completed，completed 是终态不能回退；"
        "闲聊、问答、单步指令不要调用。"
        "平台进度卡只认这个 update_task_card 工具；内核自带的任务 / 待办工具不会显示在界面上，"
        "不要用它们代替。不要再输出 task_card 围栏。",
        f"<task_card_context>{context_id}</task_card_context>",
    ]
    if card is None:
        lines.append("当前卡片：无")
    else:
        items = task_card_service.public_items(card.items or [])
        done = sum(1 for item in items if item["status"] == task_card_service.STATUS_COMPLETED)
        doing = sum(1 for item in items if item["status"] == task_card_service.STATUS_IN_PROGRESS)
        lines.append(
            f"当前卡片：{card.title or '任务进度'}"
            f"（{done}/{len(items)} 已完成，{doing} 进行中）"
        )
        labels = {
            task_card_service.STATUS_COMPLETED: "已完成",
            task_card_service.STATUS_IN_PROGRESS: "进行中",
            task_card_service.STATUS_PENDING: "待办",
        }
        for item in items:
            lines.append(f"- {item.get('id')} [{labels[item['status']]}] {item.get('text')}")
    return "\n".join(lines)


__all__ = [
    "AUTH_FILE",
    "CREDENTIALS_SUBDIR",
    "MAX_CALLS",
    "TOKEN_PREFIX",
    "TaskCardContext",
    "authenticate",
    "begin",
    "bind",
    "context_digest",
    "credential_rel_path",
    "finish",
    "mark_used",
    "read_token",
    "render_notice",
    "was_used",
    "write_credentials",
]