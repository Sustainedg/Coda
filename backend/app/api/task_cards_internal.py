"""任务进度卡的内部写入口（``POST /api/internal/task-cards/update``）。

**不进公开 OpenAPI**（``include_in_schema=False``），**不走用户鉴权**：调用方是
本轮运行的 stdio MCP 子进程，凭据是运行级一次性令牌 ``Authorization: Bearer tc_…``。
与 ``usage_gateway.py`` 的内部路由同一先例：令牌即鉴权。

服务端权威：请求体只允许 ``context_id/action/title/items`` 四个字段；对话、运行、
版本全部由令牌解出的绑定决定。模型就算把别的 ``conversation_id`` 塞进请求体也会被
Pydantic 直接 422 拒掉。

请求体结构（字段白名单 / 类型）由 FastAPI 在进入处理器前校验（多余字段直接 422）；
处理器内按以下顺序校验，任何一步失败都不写库：

1. Bearer 令牌格式与哈希命中绑定；
2. 绑定仍活跃；
3. 请求体 ``context_id`` 与绑定一致；
4. 绑定的 run 仍在注册表里且未结束；
5. 每 context 最多 :data:`MAX_CALLS` 次（防模型死循环）；
6. ``normalize_block`` 校验项数与长度；
7. ``apply_block(source="mcp")`` 落库，返回最新快照。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict

from ..models import TaskCard
from ..runtime import task_card_mcp
from ..runtime import task_cards as task_card_service
from ..runtime.manager import registry

logger = logging.getLogger("coda")

router = APIRouter(tags=["internal"])


class TaskCardUpdateRequest(BaseModel):
    """内部写卡请求：字段白名单，多一个字段直接 422。"""

    model_config = ConfigDict(extra="forbid")

    context_id: str
    action: str
    title: str = ""
    items: list[dict[str, Any]]


def _card_view(card: TaskCard) -> dict[str, Any]:
    """返回给工具的最新快照（与详情接口的卡片字段同口径）。"""
    return {
        "id": card.id,
        "conversation_id": card.conversation_id,
        "anchor_message_id": card.anchor_message_id,
        "title": card.title,
        "items": task_card_service.public_items(card.items or []),
        "version": int(card.version or 1),
        "updated_at": card.updated_at.isoformat() if card.updated_at else None,
    }


@router.post("/internal/task-cards/update", include_in_schema=False)
async def update_task_card(
    payload: TaskCardUpdateRequest,
    authorization: str | None = Header(default=None),
):
    """按运行级令牌更新当前对话的任务卡。"""
    binding = task_card_mcp.authenticate(authorization)
    if binding is None or not binding.active:
        raise HTTPException(status_code=401, detail="任务卡令牌无效或已过期")

    if payload.context_id != binding.context_id:
        logger.warning(
            "任务卡写入被拒绝（context 不匹配）：context=%s conversation=%s",
            binding.context_id[:8],
            binding.conversation_id,
        )
        raise HTTPException(status_code=409, detail="任务卡上下文已失效")

    run = registry.get(binding.run_id)
    if run is None or run.finished:
        logger.warning(
            "任务卡写入被拒绝（运行已结束）：context=%s run=%s",
            binding.context_id[:8],
            binding.run_id,
        )
        raise HTTPException(status_code=409, detail="任务卡上下文已失效")

    if binding.calls >= task_card_mcp.MAX_CALLS:
        logger.warning(
            "任务卡写入被限流：context=%s calls=%s",
            binding.context_id[:8],
            binding.calls,
        )
        raise HTTPException(status_code=429, detail="本轮任务卡更新过于频繁")

    block, error = task_card_service.normalize_block(
        {
            "action": payload.action,
            "title": payload.title,
            "items": payload.items,
        }
    )
    if block is None:
        raise HTTPException(status_code=422, detail=error or "任务卡内容不合法")

    card = await task_card_service.apply_block(
        binding.conversation_id,
        binding.anchor_message_id,
        block,
        binding.run_id,
        source="mcp",
    )
    if card is None:
        # 两种可能：还没有卡却收到 update；或并发写冲突重试后仍失败。
        # 不区分具体原因，给模型一条既覆盖「先 create」又覆盖「可重试」的提示。
        raise HTTPException(
            status_code=409,
            detail="进度未保存：请确认已用 action=create 建立计划；若是并发冲突可稍后重试",
        )
    task_card_mcp.mark_used(binding.context_id)
    return {"ok": True, "card": _card_view(card)}


__all__ = ["router", "TaskCardUpdateRequest"]