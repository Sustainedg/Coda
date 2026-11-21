"""编排状态机：把「一张图 + 一张工单」变成一条**波次推进**的流水线（默认串行）。

一次 :func:`consume_turn` 会一直推进到「需要用户介入」或「全部完成」为止：

```
用户消息 → 入口节点 → 工单被派发/拆解 → 实施者提交 → 审查者判定
                                             ↑                    │
                                             └──── rework 回边 ────┘
```

设计要点（对应 docs/collaboration-design.md §3 / §5）：

* **波次推进**：默认（``max_parallel=1``）同一时刻只有一个步骤在跑；调高后
  只有「同父兄弟工单 + 不同岗位」能同波并行（见 :func:`_pick_wave`），
  "谁在等谁"在波次边界仍然确定。
* **工单是唯一的交接物**：每个步骤的输入由 ``prompt.py`` 从工单字段拼出来，
  不携带任何智能体的对话原文。
* **每一步都落库**：``collab_steps`` 既用于展示时间线，也用于审计与重跑。
* **预算与轮次上限**：任何一项超限都挂起（``waiting_user``）并给出可读原因，绝不静默。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Sequence

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..agents.events import AgentEvent, EventType
from ..attachments import AttachmentError, attachment_relative_path, compose_with_attachments
from ..db import SessionLocal
from ..models import AgentProfile, utcnow
from . import execution, protocol
from .graph import (
    allowed_assignees,
    budget_from_storable,
    effective_max_rounds,
    effective_output_contract,
    entry_node,
    from_storable,
    node_map,
    reviewer_of,
    rework_edge,
    rework_targets,
    validate_graph,
)
from .prompt import (
    ENTRY_REPORT_ACTIONS,
    ChatHistoryEntry,
    ChatTicketView,
    HistoryEntry,
    TeamMember,
    TicketView,
    build_chat_prompt,
    build_step_prompt,
    ticket_code,
)
from .tickets import (
    extract_chat_body,
    ensure_ask_options,
    extract_body,
    merge_chat_update,
    parse_chat_advance,
    parse_chat_update,
    parse_work_order,
    resolve_order_targets,
    titles_look_duplicated,
    validate_order,
)
from .schemas import (
    DeliveryMode,
    GraphSpec,
    NodeSpec,
    RunStatus,
    TicketAction,
    TicketState,
    VERDICT_CHANGES,
    VERDICT_PASS,
)
from .tables import CollabMessage, CollabRun, CollabStep, CollabTeam, CollabTicket

#: 防御性上限：即使预算配错了，也不允许一个请求里无限循环
MAX_STEPS_PER_TURN = 200
#: 拆解树的最大层数：防止「拆子工单再拆子工单」无穷套娃
MAX_TICKET_DEPTH = 5
#: 同一个父工单下、同名子工单最多拆几次：超过就挂起让人判断，避免"复读式拆解"空转烧钱
MAX_DUPLICATE_CHILD_TITLES = 2
FINISHED_STATES = {TicketState.DONE.value, TicketState.REJECTED.value}
#: 「该有人动手」的状态：引擎就绪队列取的就是这些（波次调度也只从这里挑）
READY_STATES = {
    TicketState.OPEN.value,
    TicketState.WORKING.value,
    TicketState.REVIEW.value,
    TicketState.REWORK.value,
    # 上抛件归发起人处理：它必须能被立刻取起来，否则「上报」就变成了静默搁置
    TicketState.ESCALATED.value,
}
ACTIVE_STATES = {
    TicketState.OPEN.value,
    TicketState.WORKING.value,
    TicketState.WAITING_CHILDREN.value,
    TicketState.REVIEW.value,
    TicketState.REWORK.value,
    TicketState.ESCALATED.value,
    TicketState.BLOCKED.value,
}


class TurnError(Exception):
    """这次请求没法开始（配置缺失、图不合法、已经在跑等）。"""


class ChatError(Exception):
    """入口智能体聊天没法开始（没写内容、图不合法、上一条还在回复等）。"""


@dataclass
class PreparedTurn:
    run: CollabRun
    team: CollabTeam
    graph: GraphSpec
    entry: NodeSpec
    content: str | None = None


@dataclass
class PreparedChat:
    """一次入口智能体聊天：已经落库的用户消息 + 拼好的 prompt。"""

    run: CollabRun
    team: CollabTeam
    graph: GraphSpec
    entry: NodeSpec
    prompt: str
    user_message: CollabMessage


@dataclass
class TurnState:
    """异步生成器没法返回值，用这个可变载体把控制信号带回来。"""

    stop: bool = False
    reason: str | None = None
    status: str | None = None
    budget_exhausted: bool = False
    #: 这一步的用量（成功完成时才有）：主循环 / 波次汇总统一聚合到 run 行
    step_usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class WaveState:
    """一个并行波次的汇总载体（同 :class:`TurnState` 的用法）。"""

    #: 各成功步骤的用量清单
    usages: list[dict[str, Any]] = field(default_factory=list)
    #: 实际完成的步骤数
    finished: int = 0
    #: 波内要求整轮停止时给 (status, reason)；None 表示这一波顺利跑完
    stopped: tuple[str, str] | None = None


# --- 准备 -------------------------------------------------------------------


async def _tickets(session: AsyncSession, run_id: str) -> list[CollabTicket]:
    return list(
        (
            await session.execute(
                select(CollabTicket)
                .where(CollabTicket.run_id == run_id)
                .order_by(CollabTicket.created_at, CollabTicket.id)
                # 入口聊天可能在另一个数据库会话里刚补过 brief；每次都从库里取最新行，
                # 不能拿同一回合早期缓存在 identity map 里的旧对象拼下一步 prompt。
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


async def _pending_question_ticket(session: AsyncSession, run_id: str) -> CollabTicket | None:
    """最近一张在等用户回答的工单（``state=blocked`` 且 ``result`` 里有 question）。

    入口智能体提问时工单会挂起；但提问的**未必是根工单** —— 上抛件是在发起人手里转成
    ``ask_user`` 的。用户回答后要把它交回提问者，所以这里不写死 ``parent_ticket_id is None``。
    """
    rows = (
        (
            await session.execute(
                select(CollabTicket)
                .where(
                    CollabTicket.run_id == run_id,
                    CollabTicket.state == TicketState.BLOCKED.value,
                )
                .order_by(CollabTicket.updated_at.desc())
            )
        )
        .scalars()
        .all()
    )
    for ticket in rows:
        if (ticket.result or {}).get("question"):
            return ticket
    return None


async def _next_sequence(session: AsyncSession, run_id: str) -> int:
    """取下一个步骤序号：**对运行行原子自增**，多个并行步骤不会撞号。

    串行时代用「查最大值 + 1」就够；并行后两个步骤可能同时读到同一个最大值、
    各自 +1 落库，时间线顺序就毁了。SQLite 的 ``UPDATE ... RETURNING`` 是原子的，
    天然适合当计数器（WAL 模式下写锁只持有这一条语句的时间）。
    """
    seq = (
        await session.execute(
            update(CollabRun)
            .where(CollabRun.id == run_id)
            .values(step_seq=CollabRun.step_seq + 1)
            .returning(CollabRun.step_seq)
        )
    ).scalar_one()
    return int(seq or 1)


def _first_line(text: str, limit: int = 60) -> str:
    line = (text or "").strip().splitlines()[0] if text.strip() else "新协作"
    return line[:limit]


async def _consume_chat_advances(session: AsyncSession, run_id: str) -> None:
    """用户把指令发进了工单通道：聊天里已经给出的「推进任务」卡都算消费过了。

    前端刷新时会从最近一条 CEO 聊天的 ``meta.advance`` 恢复决策卡。指令都已经执行了，
    旧卡还留在 meta 里，就会每次打开/刷新都重新弹出来（点 × 也只是前端临时关掉）。
    这里在消息真正进入工单通道时清掉卡片数据，保留 ``advance_consumed`` 便于审计。
    """
    rows = (
        await session.execute(
            select(CollabMessage).where(
                CollabMessage.run_id == run_id,
                CollabMessage.sender == "leader",
            )
        )
    ).scalars().all()
    for message in rows:
        meta = dict(message.meta or {})
        if "advance" not in meta:
            continue
        meta.pop("advance", None)
        meta["advance_consumed"] = True
        message.meta = meta


#: 引用决策卡推进时，时间线上落的短标记（前端本地乐观显示用同一句，见 lib/collabAdvance.ts）。
ADVANCE_MARKER = "已按决策卡的建议推进"


async def prepare_turn(
    session: AsyncSession, run_id: str, content: str | None, use_advance: bool = False
) -> PreparedTurn:
    """校验图、把用户的这句话接到入口节点的工单上。

    ``use_advance=True`` 表示这条消息引用的是「推进任务」决策卡的建议：``content``
    仍是执行的完整指令，但时间线上不把长指令再显示一遍，只落一句短标记
    （指令原文在 CEO 的消息里已经写过，重发一遍既占屏幕又多花一遍 tokens）。
    """
    run = await session.get(CollabRun, run_id)
    if run is None:
        raise TurnError(f"找不到协作运行：{run_id}")
    if execution.is_running(run_id):
        raise TurnError("这次协作已经在运行中，请先等它结束或点「停止」")

    team = await session.get(CollabTeam, run.team_id)
    if team is None:
        raise TurnError("协作组已被删除")
    try:
        graph = from_storable(team.graph)
    except ValueError as exc:
        raise TurnError(f"编排图无法解析：{exc}") from None

    validation = validate_graph(graph, team.entry_key)
    if not validation.ok:
        detail = "；".join(issue.message for issue in validation.errors[:3])
        raise TurnError(f"编排图还不能跑：{detail}")

    entry = entry_node(graph, team.entry_key)
    assert entry is not None  # validate_graph 已经保证

    text = (content or "").strip()
    tickets = await _tickets(session, run_id)
    root = next((t for t in tickets if t.parent_ticket_id is None), None)
    # 用户可能在回答**某个智能体**的提问；上抛件会在发起人手里 ask_user，未必是根工单，
    # 所以先找出「最近一张在等用户回答的工单」，下面优先把它重新打开。
    asked = await _pending_question_ticket(session, run_id) if text else None

    if text:
        if run.goal == "":
            run.goal = text
            if run.title in ("", "新协作"):
                run.title = _first_line(text)
        # 用户说的话也要出现在时间线上。引用决策卡时只落短标记（完整指令在 CEO 的
        # 消息里已经写过一遍，重发一遍既占屏幕又多花一遍 tokens）。
        session.add(
            CollabStep(
                run_id=run.id,
                ticket_id=root.id if root else None,
                role_key="user",
                duty="user",
                sequence=await _next_sequence(session, run.id),
                action="",
                status="complete",
                content=ADVANCE_MARKER if use_advance else text,
                finished_at=utcnow(),
            )
        )
        # 这张指令是从聊天决策卡点进来的（或用户直接发了推进指令）：旧卡已经消费，
        # 别再让刷新后的前端从 meta 里把它恢复出来。
        await _consume_chat_advances(session, run.id)

    if root is None:
        if not text:
            raise TurnError("请先告诉入口智能体你想做什么")
        root = CollabTicket(
            run_id=run.id,
            parent_ticket_id=None,
            from_agent_profile_id=None,
            to_agent_profile_id=entry.agent_profile_id,
            owner_key=entry.key,
            holder_key=entry.key,
            title=_first_line(text),
            brief={"goal": text},
            state=TicketState.OPEN.value,
            max_rounds=effective_max_rounds(entry, budget_from_storable(run.budget)),
        )
        session.add(root)
        run.tickets_used = (run.tickets_used or 0) + 1
        await session.commit()
        await session.refresh(root)
    elif asked is not None:
        # 用户回答了提问 → 把提问的那张工单交回给提问者（当前持有者）继续推进
        asked.state = TicketState.OPEN.value
        # 提问已经答复，清掉卡片数据；否则下一次因别的原因挂起时会把旧卡片带出来
        result = dict(asked.result or {})
        result.pop("question", None)
        result.pop("options", None)
        asked.result = result
        asked.updated_at = utcnow()
        session.add(asked)
    elif text and root.state == TicketState.BLOCKED.value:
        # 入口智能体问过话，用户现在回答了 → 工单重新回到提问者（当前持有者）手里
        root.state = TicketState.OPEN.value
        # 上一轮的提问已经答复，清掉卡片数据；否则下一次因别的原因挂起时会把旧卡片带出来
        result = dict(root.result or {})
        result.pop("question", None)
        result.pop("options", None)
        root.result = result
    elif text and root.state == TicketState.DONE.value:
        # 运行完成后用户仍可继续找入口智能体：重新打开根工单，让领导者接手这次追问。
        # 历史工单与产出保留，后续派活会继续挂在同一棵工单树上。
        root.state = TicketState.OPEN.value
        root.holder_key = entry.key
        root.to_agent_profile_id = entry.agent_profile_id
        root.closed_at = None
        root.verdict = None
        # 新一轮收尾要重新汇报一次：清掉上一轮的汇报标记（用户追问 → 干完 → 再来一次汇报）
        root.report_at = None

    run.status = RunStatus.RUNNING.value
    run.error = None
    run.updated_at = utcnow()
    session.add(run)
    await session.commit()
    await session.refresh(run)
    # 旧库升级兜底：step_seq 是后加的计数列，历史运行的它停在 0，而步骤表里可能已有
    # 一大串 sequence。这里对齐一次（取 max+1），并行取号才不会与历史序号撞车。
    if (run.step_seq or 0) == 0:
        rows = (
            await session.execute(
                select(CollabStep.sequence).where(CollabStep.run_id == run.id)
            )
        ).all()
        top = max((row[0] for row in rows), default=0)
        if top:
            run.step_seq = top
            session.add(run)
            await session.commit()
    return PreparedTurn(run=run, team=team, graph=graph, entry=entry, content=text or None)


# --- 入口智能体常驻聊天 -------------------------------------------------------


async def _next_chat_sequence(session: AsyncSession, run_id: str) -> int:
    rows = (
        await session.execute(
            select(CollabMessage.sequence).where(CollabMessage.run_id == run_id)
        )
    ).all()
    return max((row[0] for row in rows), default=0) + 1


async def _chat_history(
    session: AsyncSession, run_id: str, limit: int = 20
) -> list[CollabMessage]:
    """最近若干条聊天，按时间正序返回（只给入口智能体本人回放）。"""
    rows = list(
        (
            await session.execute(
                select(CollabMessage)
                .where(CollabMessage.run_id == run_id)
                .order_by(CollabMessage.sequence.desc(), CollabMessage.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
    )
    rows.reverse()
    return rows


def _node_label(node: NodeSpec) -> str:
    return node.label or node.key


def _team_roster(graph: GraphSpec, *, entry_key: str | None) -> list[TeamMember]:
    """把编排图翻成一份名册：团队里有谁、谁负责什么、入口是谁。

    图是用户自己画的公开结构，名册只是把它念给智能体听 —— 不含任何人的对话原文或工单内容，
    因此不违反 C5（智能体之间不共享记忆）。此前一个节点只看得见自己的派发对象，
    于是 CEO 答不出「团队里有谁」，CTO 也会误判「本协作没有审查者」。
    """
    return [
        TeamMember(
            key=spec.key,
            label=spec.label,
            duty=spec.duty,
            is_entry=spec.key == entry_key,
            instructions=spec.instructions,
        )
        for spec in graph.nodes
    ]


async def _active_step_label(
    session: AsyncSession, run_id: str, graph: GraphSpec
) -> str | None:
    """当前正在跑的工单步骤，用一句结构化事实告诉 CEO「谁在做什么」。"""
    step = (
        await session.execute(
            select(CollabStep)
            .where(CollabStep.run_id == run_id, CollabStep.status == "streaming")
            .order_by(CollabStep.sequence.desc())
            .limit(1)
        )
    ).scalars().first()
    if step is None:
        return None
    node = node_map(graph).get(step.role_key)
    label = _node_label(node) if node else (step.role_key or "某个智能体")
    ticket = await session.get(CollabTicket, step.ticket_id) if step.ticket_id else None
    if ticket is not None:
        return f"{label}（{step.role_key}）正在处理《{ticket.title}》"
    return f"{label}（{step.role_key}）正在处理一项工作"


def _chat_ticket_view(ticket: CollabTicket, labels: dict[str, str]) -> ChatTicketView:
    brief = ticket.brief or {}
    result = ticket.result or {}
    holder = labels.get(ticket.holder_key, ticket.holder_key) or "未指定"
    return ChatTicketView(
        id=ticket.id,
        title=ticket.title,
        state=ticket.state,
        holder=f"{holder}（{ticket.holder_key}）" if ticket.holder_key else holder,
        goal=str(brief.get("goal") or ""),
        requirements=[str(item) for item in (brief.get("requirements") or [])],
        acceptance=[str(item) for item in (brief.get("acceptance") or [])],
        constraints=[str(item) for item in (brief.get("constraints") or [])],
        deliverables=[str(item) for item in (brief.get("deliverables") or [])],
        summary=str(result.get("summary") or result.get("remark") or ""),
        artifacts=_artifact_paths(result),
        review_comment=str(ticket.review_comment or ""),
    )


async def prepare_chat(
    session: AsyncSession, run_id: str, content: str, attachments: Sequence[str] | None = None
) -> PreparedChat:
    """校验并落库一条用户消息，拼出入口智能体这次的聊天 prompt。

    这里刻意**不碰工单状态**，也不检查主流程是否正在跑：CEO 的聊天通道与工单推进并行，
    是用户随时能找到负责人的唯一入口。

    ``attachments`` 是已经落在工作目录里的附件相对路径：只校验「确实在本工作目录内」，
    再把清单拼进提示词 —— 与对话的附件走同一套（见 :mod:`app.attachments`）。
    """
    run = await session.get(CollabRun, run_id)
    if run is None:
        raise ChatError(f"找不到协作运行：{run_id}")
    if execution.is_chat_running(run_id):
        raise ChatError("CEO 正在回复上一条消息，请稍等它说完")
    text = (content or "").strip()
    if not text and not attachments:
        raise ChatError("请先输入想对 CEO 说的话，或附上一个文件")
    try:
        files = [
            attachment_relative_path(item, execution.workspace_root(run))
            for item in attachments or []
        ]
    except AttachmentError as exc:
        raise ChatError(str(exc)) from None
    spoken = compose_with_attachments(text, files)

    team = await session.get(CollabTeam, run.team_id)
    if team is None:
        raise ChatError("协作组已被删除")
    try:
        graph = from_storable(team.graph)
    except ValueError as exc:
        raise ChatError(f"编排图无法解析：{exc}") from None
    validation = validate_graph(graph, team.entry_key)
    if not validation.ok:
        detail = "；".join(issue.message for issue in validation.errors[:3])
        raise ChatError(f"编排图还不能跑：{detail}")
    entry = entry_node(graph, team.entry_key)
    assert entry is not None

    tickets = await _tickets(session, run.id)
    history = await _chat_history(session, run.id)
    labels = {key: (spec.label or key) for key, spec in node_map(graph).items()}

    user_message = CollabMessage(
        run_id=run.id,
        sender="user",
        role_key=entry.key,
        sequence=await _next_chat_sequence(session, run.id),
        status="complete",
        content=spoken,
        finished_at=utcnow(),
    )
    session.add(user_message)
    run.updated_at = utcnow()
    session.add(run)
    await session.commit()
    await session.refresh(user_message)

    prompt = build_chat_prompt(
        team_name=team.name,
        run_title=run.title,
        run_status=run.status,
        node=entry,
        tickets=[_chat_ticket_view(ticket, labels) for ticket in tickets],
        history=[ChatHistoryEntry(sender=row.sender, content=row.content) for row in history],
        user_message=spoken,
        active_label=await _active_step_label(session, run.id, graph),
        node_labels=labels,
        roster=_team_roster(graph, entry_key=entry.key),
    )
    return PreparedChat(
        run=run,
        team=team,
        graph=graph,
        entry=entry,
        prompt=prompt,
        user_message=user_message,
    )


def _chat_parts(parts: list[dict[str, Any]], body: str) -> list[dict[str, Any]]:
    """持久化聊天消息时去掉 ``chat_update`` 块，避免刷新后把协议 JSON 当正文展示。"""
    cleaned: list[dict[str, Any]] = []
    for part in parts:
        item = dict(part)
        if item.get("kind") == "text":
            item["text"] = extract_chat_body(str(item.get("text") or ""))
            if not item["text"]:
                continue
        cleaned.append(item)
    if body and not any(item.get("kind") == "text" for item in cleaned):
        cleaned.append({"kind": "text", "text": body})
    return cleaned


async def _apply_chat_update(
    session: AsyncSession, run: CollabRun, update
) -> tuple[dict[str, Any], str | None]:
    """把聊天里的新增要求同步到根工单和所有未收尾工单。"""
    tickets = await _tickets(session, run.id)
    targets = [ticket for ticket in tickets if ticket.state in ACTIVE_STATES]
    if not targets:
        return {}, "当前没有仍在推进的工单，新要求没有可同步的对象；可以等任务继续后再补充"

    changed_any: list[str] = []
    applied_ids: list[str] = []
    for ticket in targets:
        merged, changed = merge_chat_update(ticket.brief or {}, update)
        if not changed:
            continue
        ticket.brief = merged
        ticket.updated_at = utcnow()
        session.add(ticket)
        applied_ids.append(ticket.id)
        for field_name in changed:
            if field_name not in changed_any:
                changed_any.append(field_name)
    if applied_ids:
        run.updated_at = utcnow()
        session.add(run)
    return (
        {
            "ticket_ids": applied_ids,
            "ticket_count": len(applied_ids),
            "fields": changed_any,
            "update": update.to_dict(),
        },
        None,
    )


async def consume_chat(
    session: AsyncSession, prepared: PreparedChat
) -> AsyncIterator[protocol.CollabEvent]:
    """执行入口智能体的一次聊天回复，并把可选的 chat_update 落进工单。"""
    run = prepared.run
    entry = prepared.entry
    assistant = CollabMessage(
        run_id=run.id,
        sender="leader",
        role_key=entry.key,
        sequence=await _next_chat_sequence(session, run.id),
        status="streaming",
    )
    session.add(assistant)
    await session.commit()
    await session.refresh(assistant)

    try:
        prepared_step = await execution.prepare_chat(
            session, run=run, node=entry, prompt=prepared.prompt
        )
    except execution.StepError as exc:
        assistant.status, assistant.error, assistant.finished_at = "error", str(exc), utcnow()
        session.add(assistant)
        await session.commit()
        yield protocol.chat_started(
            assistant.id, run.id, _node_label(entry), "", entry.key
        )
        yield protocol.chat_finished(assistant.id, "error", error=str(exc))
        return

    assistant.agent_profile_id = prepared_step.profile.id
    session.add(assistant)
    await session.commit()
    yield protocol.chat_started(
        assistant.id,
        run.id,
        prepared_step.profile.name,
        entry.key,
    )

    handle = execution.chat_registry.register(
        run.id, f"chat:{assistant.id}", prepared_step.profile.name, entry.key
    )
    text_parts: list[str] = []
    parts: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    error_text: str | None = None
    status = "complete"

    try:
        async for event in execution.stream_step(prepared_step, handle):
            _accumulate(event, text_parts, parts, usage)
            if event.type is EventType.ERROR:
                error_text = event.text or "智能体返回了错误"
            elif event.type is EventType.RUN_FINISHED:
                finished = (event.payload or {}).get("status")
                if finished in ("error", "cancelled"):
                    status = finished
            yield protocol.chat_event(assistant.id, event)
    except Exception as exc:  # noqa: BLE001 - 聊天失败也要显式落库并上报
        error_text, status = str(exc), "error"
        yield protocol.chat_event(assistant.id, AgentEvent(EventType.ERROR, text=str(exc)))
    finally:
        execution.chat_registry.finish(f"chat:{assistant.id}")

    raw_text = "".join(text_parts)
    body = extract_chat_body(raw_text)
    warning: str | None = None
    applied: dict[str, Any] = {}
    meta: dict[str, Any] = {}
    advance_payload: dict[str, Any] | None = None

    if status == "complete" and error_text is None:
        if not raw_text.strip():
            warning = "CEO 这次没有返回文字，只能先把空回复记下来；可以再问一次"
        update, parse_warning = parse_chat_update(raw_text)
        if parse_warning:
            warning = f"{warning}；{parse_warning}" if warning else parse_warning
        if update is not None:
            applied, apply_warning = await _apply_chat_update(session, run, update)
            if apply_warning:
                warning = f"{warning}；{apply_warning}" if warning else apply_warning
            if applied:
                meta["update"] = update.to_dict()
                meta["applied"] = applied
        advance, advance_warning = parse_chat_advance(raw_text)
        if advance_warning:
            warning = f"{warning}；{advance_warning}" if warning else advance_warning
        if advance is not None:
            advance_payload = advance.to_dict()
            meta["advance"] = advance_payload
        if warning:
            meta["warning"] = warning

    assistant.content = body
    assistant.parts = _chat_parts(parts, body)
    assistant.usage = usage
    assistant.status = (
        status
        if status in ("cancelled", "error")
        else ("error" if error_text else "complete")
    )
    assistant.error = error_text
    assistant.meta = meta
    assistant.finished_at = utcnow()
    run.updated_at = utcnow()
    session.add(assistant)
    session.add(run)
    await session.commit()

    yield protocol.chat_finished(
        assistant.id,
        assistant.status,
        usage=usage,
        error=error_text,
        warning=warning,
        applied=applied,
        advance=advance_payload,
    )


# --- 推进 -------------------------------------------------------------------


async def consume_turn(session: AsyncSession, prepared: PreparedTurn) -> AsyncIterator[protocol.CollabEvent]:
    """推进这次协作，直到需要用户介入或全部完成。

    调度是**波次并行**（``max_parallel > 1`` 时）：每一波从就绪工单里挑出
    「同一父工单拆出的兄弟工单、且持有者岗位互不相同」的一批同时推进，
    全部结束后（join）才做父工单汇总与下一波调度。护栏（见 docs/collaboration-design.md §3.4）：

    * **同一岗位永远串行**：一个岗位一个对话通道（与入口聊天通道的约束一致）；
    * **只有兄弟工单才并行**：它们由同一次 split 拆出、天然无先后依赖；其他工单（如
      返工环、审查链）仍按串行顺序推进；
    * ``max_parallel=1``（默认）时退化为完全串行，行为与过去一字不差。
    """
    run_id = prepared.run.id
    state = TurnState()
    steps_this_turn = 0
    first_step = True

    yield protocol.run_started(run_id, prepared.run.team_id, prepared.run.title, prepared.run.entry_key)

    while not state.stop:
        run = await session.get(CollabRun, run_id)
        if run is None:
            yield protocol.run_status(RunStatus.ERROR.value, "协作运行已被删除")
            return

        async for event in _rollup_parents(session, run):
            yield event

        reason = _budget_reason(run, prepared)
        if reason:
            state.stop, state.reason, state.budget_exhausted = True, reason, True
            break

        # 根工单收尾后，把最后一步交回入口智能体：让它把结论汇报给用户（主会话里唯一看得到的人）
        async for event in _hand_back_to_entry(session, prepared, run):
            yield event

        ready = [t for t in await _tickets(session, run_id) if t.state in READY_STATES]
        if not ready:
            if prepared.content and steps_this_turn == 0:
                # 用户发了一句话，但没有任何工单能自动推进 —— 最常见的两种原因：
                # 有工单被协议错误卡在 blocked，或这一轮的活已经收尾。
                # 过去这里直接 break，界面上只会多一条用户发言、永远等不到回话；
                # 现在显式说明原因，并提示可以去哪处理 / 改用 CEO 聊天。
                blocked = [
                    item
                    for item in await _tickets(session, run_id)
                    if item.state == TicketState.BLOCKED.value
                ]
                if blocked:
                    state.stop = True
                    state.status = RunStatus.WAITING_USER.value
                    state.reason = (
                        f"这条消息没有可推进的工单：有 {len(blocked)} 张工单卡在「等待处理」。"
                        "打开「工单看板」查看受阻原因，或点错误卡片上的「重试」；"
                        "想先了解进度，可以直接在下方和入口智能体聊天。"
                    )
            break

        wave = _pick_wave(prepared, ready)
        nodes = node_map(prepared.graph)

        if len(wave) == 1:
            # 串行路径（绝大多数轮次）：与过去完全一致，不开新会话、不多一层包装。
            ticket = wave[0]
            node = nodes.get(ticket.holder_key)
            if node is None:
                state.stop = True
                state.reason = (
                    f"工单 {ticket_code(ticket.id)} 的持有者「{ticket.holder_key}」不在图里"
                )
                state.status = RunStatus.ERROR.value
                break
            async for event in _run_step(
                session,
                prepared,
                run,
                ticket,
                node,
                state,
                user_message=prepared.content if first_step else None,
            ):
                yield event
            first_step = False
            steps_this_turn += 1
            if state.step_usage:
                await _accumulate_run_usage(session, run, [state.step_usage])
                state.step_usage = {}
        else:
            # 并行波次：后台任务推进（每步独立会话），事件从队列实时汇出，
            # 前端按 step_id 分流；波次全部结束后统一聚合用量、检查停止信号。
            queue: asyncio.Queue[protocol.CollabEvent | None] = asyncio.Queue()
            wave_task = asyncio.create_task(
                _run_wave(
                    prepared,
                    wave,
                    queue,
                    user_message=prepared.content if first_step else None,
                )
            )
            try:
                while True:
                    event = await queue.get()
                    if event is None:
                        break
                    yield event
            finally:
                if not wave_task.done():
                    await wave_task
            first_step = False
            result = wave_task.result()
            steps_this_turn += result.finished
            # 波次在独立会话里改过 run 行（steps_used 自增等）；主会话的 run 对象已过期，
            # 先刷新再聚合 usage，避免把旧值写回（见 _accumulate_run_usage 的读-改-写）。
            await session.refresh(run)
            await _accumulate_run_usage(session, run, result.usages)
            if result.stopped:
                # 波内某个步骤要求停下（失败挂起 / 取消）：带上它的理由收尾本轮
                state.stop, state.status, state.reason = True, *result.stopped

        if steps_this_turn >= MAX_STEPS_PER_TURN:
            state.stop = True
            state.reason = "单次请求内推进的步骤过多，已暂停（请稍后点「继续」）"
            break

    async for event in _finalize(session, prepared, state):
        yield event


def _pick_wave(prepared: PreparedTurn, ready: Sequence[CollabTicket]) -> list[CollabTicket]:
    """从就绪工单里挑出这一波并行推进的工单。

    规则（缺一不可，全部为了不引入新的不确定状态）：

    * 按既有顺序取第一张工单，它的**同父兄弟**才能进同一波——兄弟工单由同一次
      split 拆出，天然无先后依赖；其他关系的工单（返工环、跨部门的活）互相之间
      可能隐含依赖，一律不并；
    * **持有者岗位互不相同**：一个岗位一个对话通道，同一岗位同时跑两张工单
      会把两个 prompt 拼进同一段执行历史；
    * 数量不超过预算里的 ``max_parallel``；``max_parallel=1`` 时永远只有一张。
    """
    limit = budget_from_storable(prepared.run.budget).max_parallel
    if limit <= 1:
        return [ready[0]]
    head = ready[0]
    nodes = node_map(prepared.graph)
    wave: list[CollabTicket] = [head]
    seen_holders = {head.holder_key}
    for ticket in ready[1:]:
        if len(wave) >= limit:
            break
        if ticket.parent_ticket_id != head.parent_ticket_id:
            break
        if not ticket.holder_key or ticket.holder_key in seen_holders:
            continue
        if ticket.holder_key not in nodes:
            continue
        wave.append(ticket)
        seen_holders.add(ticket.holder_key)
    return wave


async def _run_wave(
    prepared: PreparedTurn,
    wave: Sequence[CollabTicket],
    queue: "asyncio.Queue[protocol.CollabEvent | None]",
    *,
    user_message: str | None,
) -> WaveState:
    """在后台任务里并行推进一波兄弟工单，事件实时塞进 ``queue``。

    每个步骤开**独立的数据库会话**（AsyncSession 不能并发共享）。事件按「谁先产出
    谁先出」入队，SSE 信封本身带 ``step_id``，前端按它分流到各岗位的会话线程，
    交织不会张冠李戴。

    任何一步要求停下（失败挂起 / 取消），**记下原因但让其余步骤跑完**：半途强杀
    已在执行的子进程会留下「工单还是 working、步骤还是 streaming」的脏状态，
    由启动恢复收尾反而更糟。波次结束后主循环按 stopped 决定整轮去留。
    """
    nodes = node_map(prepared.graph)
    result = WaveState()

    async def drive_one(index: int, ticket_id: str) -> None:
        try:
            async with SessionLocal() as step_session:
                ticket = await step_session.get(CollabTicket, ticket_id)
                if ticket is None or ticket.state not in READY_STATES:
                    return
                node = nodes.get(ticket.holder_key)
                if node is None:
                    result.stopped = (
                        RunStatus.ERROR.value,
                        f"工单 {ticket_code(ticket.id)} 的持有者「{ticket.holder_key}」不在图里",
                    )
                    return
                step_run = await step_session.get(CollabRun, prepared.run.id)
                if step_run is None:
                    return
                local_state = TurnState()
                async for event in _run_step(
                    step_session,
                    prepared,
                    step_run,
                    ticket,
                    node,
                    local_state,
                    user_message=user_message if index == 0 else None,
                ):
                    queue.put_nowait(event)
                result.finished += 1
                if local_state.step_usage:
                    result.usages.append(local_state.step_usage)
                if local_state.stop:
                    result.stopped = (
                        local_state.status or RunStatus.WAITING_USER.value,
                        local_state.reason or "并行步骤被挂起",
                    )
        except Exception as exc:  # noqa: BLE001 - 铁律 11：并行分支的异常也要显式上浮
            result.stopped = (RunStatus.ERROR.value, f"并行步骤执行失败：{exc}")

    tasks = [asyncio.create_task(drive_one(index, t.id)) for index, t in enumerate(wave)]
    try:
        await asyncio.gather(*tasks)
    finally:
        queue.put_nowait(None)
    return result


def _elapsed_seconds(started: datetime | None) -> float:
    """SQLite 取回的 datetime 不带时区，这里统一按 UTC 解释。"""
    if started is None:
        return 0.0
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (utcnow() - started).total_seconds()


def _budget_reason(run: CollabRun, prepared: PreparedTurn) -> str | None:
    budget = budget_from_storable(run.budget)
    if (run.steps_used or 0) >= budget.max_steps:
        return (
            f"已达步数上限（{budget.max_steps} 步）。"
            "可以在运行的「提高预算」里调高步数上限，然后点「继续」从中断处接着跑。"
        )
    if (run.tickets_used or 0) >= budget.max_tickets:
        return (
            f"已达工单上限（{budget.max_tickets} 张）。"
            "可以在运行的「提高预算」里调高工单上限，然后点「继续」从中断处接着跑。"
        )
    if budget.max_tokens:
        used = sum(v for v in (run.usage or {}).values() if isinstance(v, (int, float)))
        if used >= budget.max_tokens:
            return (
                f"已达 token 上限（{budget.max_tokens}）。"
                "可以在运行的「提高预算」里调高 token 上限（或设为 0 不限），然后点「继续」从中断处接着跑。"
            )
    # 交互式协作的时限按「多久没有进展」计算，而不是从创建时间起累计：
    # 用户隔一段时间回来继续、或运行在 waiting_user 停留很久，都不该让它永久失效。
    # prepare_turn 会刷新 updated_at；每一步成功落库也会刷新它。
    elapsed = _elapsed_seconds(run.updated_at)
    if elapsed > budget.timeout_seconds:
        return f"距上次进展已超过运行时限（{budget.timeout_seconds} 秒）"
    return None


# --- 单步 -------------------------------------------------------------------


async def _run_step(
    session: AsyncSession,
    prepared: PreparedTurn,
    run: CollabRun,
    ticket: CollabTicket,
    node: NodeSpec,
    state: TurnState,
    *,
    user_message: str | None,
) -> AsyncIterator[protocol.CollabEvent]:
    graph = prepared.graph
    assignees = allowed_assignees(graph, node.key)
    reviewer = reviewer_of(graph, node.key)
    rework = rework_targets(graph, node.key)
    labels = {key: (spec.label or key) for key, spec in node_map(graph).items()}
    label_counts: dict[str, int] = {}
    for label in labels.values():
        if label:
            label_counts[label] = label_counts.get(label, 0) + 1
    # 只接受唯一的显示名映射；同名节点必须由模型写 key，避免猜错接收人
    aliases = {
        label: key
        for key, label in labels.items()
        if label and label != key and label_counts[label] == 1
    }

    # 收尾汇报（见 _hand_back_to_entry）：根工单已完成、现在轮到入口智能体对用户交代结论。
    # 这一步只允许 done / ask_user —— 活已经干完了，绝不允许从这里重新派活。
    closing_report = (
        ticket.parent_ticket_id is None
        and ticket.report_at is not None
        and node.key == prepared.entry.key
    )
    digest = (
        _run_digest(await _tickets(session, run.id), reporting_id=ticket.id) if closing_report else ()
    )

    # 「我派出去的活交回了什么」：子工单的结构化摘要。上游据此判断能不能收尾，
    # 而不是像过去那样只能看见自己几次派单记录、于是反复重派。
    children = await _direct_children(session, ticket)
    downstream = _child_digest_lines(children)[-MAX_DOWNSTREAM_LINES:]
    downstream_ready = bool(children) and all(
        child.state in FINISHED_STATES for child in children
    )

    history = await _ticket_history(session, ticket.id)
    is_entry = node.key == prepared.entry.key
    prompt_text = build_step_prompt(
        team_name=prepared.team.name,
        workspace_path=str(execution.workspace_root(run)),
        node=node,
        ticket=TicketView(
            id=ticket.id,
            title=ticket.title,
            brief=ticket.brief or {},
            state=ticket.state,
            round=ticket.round,
            result=ticket.result or {},
            review_comment=ticket.review_comment,
            submitter_key=ticket.submitter_key or "",
            escalation=ticket.escalation or {},
        ),
        history=history,
        assignees=assignees,
        reviewer=reviewer,
        rework_targets=rework,
        user_message=user_message,
        node_labels=labels,
        roster=_team_roster(graph, entry_key=prepared.entry.key),
        closing_report=closing_report,
        run_digest=digest,
        downstream=downstream,
        downstream_ready=downstream_ready,
        is_entry=is_entry,
    )

    step = CollabStep(
        run_id=run.id,
        ticket_id=ticket.id,
        role_key=node.key,
        duty=node.duty.value,
        sequence=await _next_sequence(session, run.id),
        status="streaming",
    )
    session.add(step)
    # 只把「还没人碰过」的工单标成处理中；review / rework 的状态要原样带进 prompt
    if ticket.state == TicketState.OPEN.value:
        ticket.state = TicketState.WORKING.value
    session.add(ticket)
    await session.commit()
    await session.refresh(step)
    await session.refresh(ticket)

    try:
        prepared_step = await execution.prepare_step(
            session, run=run, node=node, step_id=step.id, prompt=prompt_text
        )
    except execution.StepError as exc:
        step.status, step.error, step.finished_at = "error", str(exc), utcnow()
        step.agent_profile_id = node.agent_profile_id
        session.add(step)
        await session.commit()
        yield protocol.step_finished(step.id, "error", error=str(exc))
        state.stop, state.reason, state.status = True, str(exc), RunStatus.ERROR.value
        return

    step.agent_profile_id = prepared_step.profile.id
    session.add(step)
    await session.commit()

    yield protocol.step_started(
        step.id, run.id, ticket.id, node.key, node.duty.value, prepared_step.profile.name
    )
    for warning in prepared_step.warnings:
        yield protocol.notice(warning, "warn", step_id=step.id)

    handle = execution.registry.register(run.id, step.id, prepared_step.profile.name, node.key)
    text_parts: list[str] = []
    parts: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    error_text: str | None = None
    step_status = "complete"

    try:
        async for event in execution.stream_step(prepared_step, handle):
            _accumulate(event, text_parts, parts, usage)
            if event.type is EventType.ERROR:
                error_text = event.text or "智能体返回了错误"
            elif event.type is EventType.RUN_FINISHED:
                status = (event.payload or {}).get("status")
                if status in ("error", "cancelled"):
                    step_status = status
            yield protocol.step_event(step.id, event)
    except Exception as exc:  # noqa: BLE001 - 执行失败也要落库并显式上报（铁律 11）
        error_text = str(exc)
        step_status = "error"
        yield protocol.step_event(step.id, AgentEvent(EventType.ERROR, text=str(exc)))
    finally:
        execution.registry.finish(step.id)

    audit = execution.finish_audit(prepared_step)
    raw_text = "".join(text_parts)
    body = ""
    order = None
    parse_warning: str | None = None

    if step_status == "complete" and error_text is None:
        parsed = parse_work_order(raw_text)
        if not parsed.ok or parsed.order is None:
            error_text = f"产出不符合工单协议：{parsed.error or '未给出 work_order 块'}"
            step_status = "error"
        else:
            body = extract_body(raw_text)
            parse_warning = parsed.warning
            resolve_order_targets(parsed.order, aliases)
            # ask_user 漏给 options 时不把用户堵在协议错误上：补成可点卡片继续推进。
            ensure_ask_options(parsed.order)
            semantic = validate_order_ctx(
                parsed.order,
                node,
                assignees,
                rework,
                is_entry=is_entry,
                escalated=ticket.state == TicketState.ESCALATED.value,
                allowed_actions=ENTRY_REPORT_ACTIONS if closing_report else None,
            )
            if semantic:
                error_text = semantic
                step_status = "error"
            else:
                order = parsed.order

    step.content = body or raw_text
    step.parts = parts
    step.usage = usage
    step.error = error_text
    step.status = step_status
    step.audit = audit
    step.action = order.action.value if order is not None else ""
    # 记下这一步把活交给了谁：岗位会话据此还原「谁对谁说了什么」的交接链
    step.target_key = (order.assignee or "") if order is not None and order.action is TicketAction.DELEGATE else ""
    step.gist = _order_gist(order) if order is not None else (error_text or "")[:160]
    step.finished_at = utcnow()

    session.add(step)
    # 步数走原子自增（并行步骤不会互相覆盖）；usage 是 JSON 列没法用 SQL 合并，
    # 由主循环 / 波次 join 后统一聚合（见 ``_accumulate_run_usage``）——每一步只落自己的行，
    # 聚合永远在单点串行做，读-改-写竞态就不存在了。
    await session.execute(
        update(CollabRun)
        .where(CollabRun.id == run.id)
        .values(steps_used=CollabRun.steps_used + 1, updated_at=utcnow())
    )
    await session.commit()
    # 成功完成的用量交给控制载体；串行路径同样受益（主循环统一聚合）
    if step_status == "complete" and usage:
        state.step_usage = usage

    if parse_warning:
        yield protocol.notice(parse_warning, "warn", step_id=step.id)

    if audit and not audit.get("unchanged", True):
        yield protocol.notice(
            f"越权提醒：「{prepared_step.profile.name}」本该只读，但共享工作目录有 "
            f"{audit.get('count')} 个文件发生变化（审计已记录）",
            "warn",
            step_id=step.id,
        )

    yield protocol.step_finished(
        step.id,
        step_status,
        action=step.action,
        usage=usage,
        error=error_text,
    )

    if step_status == "cancelled":
        state.stop, state.status, state.reason = True, RunStatus.CANCELLED.value, "用户停止了这次协作"
        return

    if step_status != "complete" or error_text:
        # 解析/执行失败 → 挂起等用户，绝不猜也不静默
        ticket.state = TicketState.BLOCKED.value
        session.add(ticket)
        run.error = error_text
        await session.commit()
        state.stop, state.status, state.reason = True, RunStatus.WAITING_USER.value, error_text
        return

    async for event in _safe_apply(session, prepared, run, ticket, node, order, reviewer, state):
        yield event


def validate_order_ctx(
    order,
    node: NodeSpec,
    assignees: Sequence[str],
    rework: Sequence[str],
    *,
    is_entry: bool,
    escalated: bool = False,
    allowed_actions: Sequence[TicketAction] | None = None,
) -> str | None:
    """把「节点允许的动作/对象」喂给纯函数做语义校验。

    ``allowed_actions`` 只在收尾汇报时收窄（done / ask_user），其余情况按节点契约来。
    """
    return validate_order(
        order,
        # 非入口节点不把 ask_user 当作「允许的动作」：配置里勾了它也会被忽略，
        # 校验层仍会给出「只有入口节点能向用户提问」这句明确原因。
        allowed_actions=allowed_actions
        or effective_output_contract(node, is_entry=is_entry, escalated=escalated),
        allowed_assignees=assignees,
        allowed_rework_targets=rework,
        is_entry=is_entry,
    )


def _order_gist(order: Any, limit: int = 160) -> str:
    """把一次动作压成一句话摘要，**只取结构化字段**，绝不引用原始正文。

    这是「只传工单、不传对话」的结构性保证：下游能看到的摘要全部由引擎派生。
    """
    text = ""
    if order.action is TicketAction.DELEGATE:
        target = order.assignee or "?"
        text = f"{order.mode.value} → {target}"
        if order.ticket is not None and order.ticket.title:
            text += f"：{order.ticket.title}"
    elif order.action is TicketAction.SUBMIT:
        text = order.summary or "已提交"
    elif order.action is TicketAction.REVIEW:
        text = order.verdict or ""
        if order.review_comment:
            text += f"：{order.review_comment}"
    elif order.action is TicketAction.ASK_USER:
        text = order.question or "向用户提问"
    elif order.action is TicketAction.ESCALATE:
        text = f"上报给发起人：{order.reason or '未说明原因'}"
    elif order.action is TicketAction.REJECT:
        text = order.reason or order.review_comment or "否决"
    else:
        text = order.remark or "确认完成"
    text = " ".join(text.split())
    return text[:limit]


def _accumulate(event: AgentEvent, texts: list[str], parts: list[dict[str, Any]], usage: dict[str, Any]) -> None:
    """与 ``runtime/service._merge_part`` 同构：把增量按真实发生顺序并进时间线。"""
    if event.type is EventType.MESSAGE_DELTA and event.text:
        texts.append(event.text)
        _merge_part(parts, "text", event.text)
    elif event.type is EventType.THINKING_DELTA and event.text:
        _merge_part(parts, "thinking", event.text)
    elif event.type is EventType.TOOL_CALL:
        parts.append({"kind": "tool_call", "id": event.call_id, "name": event.name, **(event.payload or {})})
    elif event.type is EventType.TOOL_RESULT:
        parts.append(
            {
                "kind": "tool_result",
                "id": event.call_id,
                "output": event.payload.get("output"),
                "is_error": event.payload.get("is_error", False),
            }
        )
    elif event.type is EventType.USAGE:
        usage.update({k: v for k, v in (event.payload or {}).items() if v is not None})


def _merge_part(parts: list[dict[str, Any]], kind: str, text: str) -> None:
    if parts and parts[-1].get("kind") == kind:
        parts[-1]["text"] = (parts[-1].get("text") or "") + text
    else:
        parts.append({"kind": kind, "text": text})


async def _accumulate_run_usage(
    session: AsyncSession, run: CollabRun, usages: Sequence[dict[str, Any]]
) -> None:
    """把一个波次里各步骤的用量聚合到运行行上（主循环串行调用，单点写）。

    并行步骤各自的 usage 已经落在 ``collab_steps`` 行里；run 行的聚合挪到这里统一做，
    避免多个会话同时读-改-写同一个 JSON 列（后提交的会覆盖先提交的）。
    """
    if not usages:
        return
    fresh = (
        (
            await session.execute(
                select(CollabRun)
                .where(CollabRun.id == run.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    if fresh is None:
        return
    merged = dict(fresh.usage or {})
    for usage in usages:
        for key, value in usage.items():
            if isinstance(value, (int, float)) and isinstance(merged.get(key), (int, float)):
                merged[key] = merged[key] + value
            else:
                merged[key] = value
    # 只写 usage 一列（Core UPDATE）：ORM 整对象写回会把并行会话刚自增的
    # steps_used 用本会话的旧值盖掉（expire_on_commit=False 时内存对象不会自动刷新）。
    await session.execute(
        update(CollabRun)
        .where(CollabRun.id == run.id)
        .values(usage=merged, updated_at=utcnow())
    )
    await session.commit()


async def _ticket_history(session: AsyncSession, ticket_id: str) -> list[HistoryEntry]:
    """只取**这一张工单**的历史摘要。

    摘要来自 ``step.gist``（引擎从工单字段派生），**不取 ``content``** ——
    这样"下游看不到上游对话原文"就不是靠自觉，而是结构上没有那条数据通路。
    """
    rows = (
        await session.execute(
            select(CollabStep)
            .where(CollabStep.ticket_id == ticket_id, CollabStep.role_key != "user")
            .order_by(CollabStep.sequence)
        )
    ).scalars().all()
    entries: list[HistoryEntry] = []
    for row in rows:
        gist = row.gist or row.error or ""
        entries.append(
            HistoryEntry(role_label=row.role_key, action=row.action or row.status, gist=gist or "（无摘要）")
        )
    return entries


# --- 收尾汇报 ---------------------------------------------------------------


async def _last_agent_step(session: AsyncSession, ticket_id: str) -> CollabStep | None:
    """这张工单上最后一次**智能体**发言（用户自己的发言不算）。"""
    rows = (
        await session.execute(
            select(CollabStep)
            .where(CollabStep.ticket_id == ticket_id, CollabStep.role_key != "user")
            .order_by(CollabStep.sequence.desc())
            .limit(1)
        )
    ).scalars().all()
    return rows[0] if rows else None


def _artifact_paths(result: dict[str, Any]) -> list[str]:
    """从工单 ``result`` 里取产出文件路径（兼容字符串与 ``{kind, path}`` 两种形态）。"""
    paths: list[str] = []
    for item in result.get("artifacts") or []:
        if isinstance(item, dict):
            path = item.get("path") or item.get("name")
            if path:
                paths.append(str(path))
        elif isinstance(item, str):
            paths.append(item)
    return paths


async def _direct_children(session: AsyncSession, ticket: CollabTicket) -> list[CollabTicket]:
    """这张工单的**直接**子工单（按拆出来的先后排序）。

    刻意只看直接子工单、不看整棵子树：子树的信息会沿着「子工单自己的 result」一层层带上来，
    没必要把整棵树的字段都塞进一次 prompt。
    """
    return list(
        (
            await session.execute(
                select(CollabTicket)
                .where(CollabTicket.parent_ticket_id == ticket.id)
                .order_by(CollabTicket.created_at, CollabTicket.id)
            )
        )
        .scalars()
        .all()
    )


#: 「下游交付摘要」最多带几行：工单多起来时必须截断，别把 prompt 顶爆
MAX_DOWNSTREAM_LINES = 12


def _child_digest_lines(children: Sequence[CollabTicket]) -> list[str]:
    """把子工单压成几行**结构化事实**，让上游看得见「我派出去的活交回了什么」。

    与 :func:`_run_digest` 同一套约束：只取工单字段（标题 / 状态 / 最后经手 / 交付摘要 /
    产出文件），不取任何智能体的对话原文 —— 与「只传工单」是同一套隔离。
    """
    lines: list[str] = []
    for child in children:
        result = child.result or {}
        parts = [f"{ticket_code(child.id)}《{child.title}》· {child.state}"]
        if child.holder_key:
            parts.append(f"最后经手：{child.holder_key}")
        summary = str(result.get("summary") or "").strip()
        if summary:
            parts.append(f"结论：{summary}")
        paths = _artifact_paths(result)
        if paths:
            shown = paths[:8]
            suffix = f"（共 {len(paths)} 个）" if len(paths) > len(shown) else ""
            parts.append(f"产出：{'、'.join(shown)}{suffix}")
        lines.append(" · ".join(parts))
    return lines


def _run_digest(tickets: Sequence[CollabTicket], reporting_id: str | None = None) -> list[str]:
    """把整次协作的结果压成几行**结构化事实**，供入口智能体向用户汇报。

    只取工单字段（标题 / 状态 / 交付摘要 / 产出文件），不取任何智能体的对话原文 ——
    与「只传工单」是同一套约束。``reporting_id`` 是本次汇报的那张工单：它为了汇报被重新
    打开了，清单里仍按「已收尾」表述，免得模型以为活还没干完。
    """
    lines: list[str] = []
    for ticket in tickets:
        result = ticket.result or {}
        state = "已收尾（本次汇报）" if ticket.id == reporting_id else ticket.state
        parts = [f"{ticket_code(ticket.id)}《{ticket.title}》· {state}"]
        if ticket.holder_key:
            parts.append(f"最后经手：{ticket.holder_key}")
        summary = str(result.get("summary") or "").strip()
        if summary:
            parts.append(summary)
        paths = _artifact_paths(result)
        if paths:
            shown = paths[:8]
            suffix = f"（共 {len(paths)} 个）" if len(paths) > len(shown) else ""
            parts.append(f"产出：{'、'.join(shown)}{suffix}")
        lines.append(" · ".join(parts))
    return lines


async def _hand_back_to_entry(
    session: AsyncSession, prepared: PreparedTurn, run: CollabRun
) -> AsyncIterator[protocol.CollabEvent]:
    """根工单收尾后，把最后一步交回**入口智能体**：由它把结论汇报给用户。

    工单沿着交接链一路往下走时，用户能看到的只有入口智能体那条主会话；活干完了如果没人回来说一声，
    用户在界面上就只能自己去翻工单树。所以这里在「根工单已完成」时把工单重新交回入口智能体，
    让它用一次 ``done`` 收尾（prompt 会要求它讲清结论，并禁止重新派活）。

    三条护栏：
    * 只有根工单收尾才汇报：子工单各归各的发起人（沿用「谁拆的谁汇总」）；
    * 最后拍板的就是入口智能体本人时不再汇报（它那一步已经把话说给用户听了）；
    * ``report_at`` 保证每轮收尾只汇报一次（用户追问重新开启后才清零）。
    """
    tickets = await _tickets(session, run.id)
    root = next((t for t in tickets if t.parent_ticket_id is None), None)
    if root is None or root.state != TicketState.DONE.value or root.report_at is not None:
        return

    entry = prepared.entry
    last = await _last_agent_step(session, root.id)
    if last is None or last.role_key == entry.key:
        return

    root.state = TicketState.OPEN.value
    root.holder_key = entry.key
    root.to_agent_profile_id = entry.agent_profile_id
    root.closed_at = None
    root.report_at = utcnow()
    root.updated_at = utcnow()
    session.add(root)
    await session.commit()
    await session.refresh(root)
    yield protocol.ticket_updated(_ticket_payload(root))


# --- 动作 -------------------------------------------------------------------


async def _ticket_depth(session: AsyncSession, ticket: CollabTicket) -> int:
    """工单在拆解树里的层数（根工单是 0）；只向上走 parent 链。"""
    depth = 0
    current = ticket
    while current.parent_ticket_id and depth <= MAX_TICKET_DEPTH + 1:
        parent = await session.get(CollabTicket, current.parent_ticket_id)
        if parent is None:
            break
        current = parent
        depth += 1
    return depth


async def _child_count_with_title(session: AsyncSession, parent_id: str, title: str) -> int:
    """同一父工单下、**同名或近似同名**的子工单已经拆过几次。

    含已完成/被否决的，用来发现"复读式拆解"。只比完全同名是不够的：模型最常靠
    「在标题尾巴上追加几个字」绕开它，所以这里按 ``titles_look_duplicated`` 判近似。
    """
    rows = (
        await session.execute(
            select(CollabTicket.title).where(CollabTicket.parent_ticket_id == parent_id)
        )
    ).all()
    return sum(1 for (other,) in rows if titles_look_duplicated(other or "", title))


async def _reject_action(
    session: AsyncSession, run: CollabRun, ticket: CollabTicket, state: TurnState, message: str
) -> None:
    """显式拒绝一次动作：工单挂起、运行置 ``waiting_user``，绝不静默（铁律 11）。"""
    ticket.state = TicketState.BLOCKED.value
    ticket.updated_at = utcnow()
    session.add(ticket)
    run.error = message
    run.updated_at = utcnow()
    session.add(run)
    await session.commit()
    await session.refresh(ticket)
    state.stop, state.status, state.reason = True, RunStatus.WAITING_USER.value, message


async def _escalation_target(
    session: AsyncSession, *, ticket: CollabTicket, node: NodeSpec, entry_key: str
) -> str | None:
    """上报给谁：这张工单的**发起人**（``owner_key``）。

    ``owner_key`` 是「谁派了这张活 / 谁负责汇总」——子工单上它就是派活的那个节点。
    顶层工单的负责人在把活交出去之后 ``owner_key`` 会变成它自己，这时沿父链往上走一层，
    直到找到一个不是我的节点。真的一路到顶（我就是发起人）→ 返回 ``None``，
    由调用方给出「请改用 ask_user」的明确原因，绝不静默搁置。
    """
    target = ticket.owner_key or ""
    current = ticket
    while (not target or target == node.key) and current.parent_ticket_id:
        parent = await session.get(CollabTicket, current.parent_ticket_id)
        if parent is None:
            break
        current = parent
        target = parent.owner_key or ""
    if not target or target == node.key:
        return entry_key if entry_key != node.key else None
    return target


async def _apply_action(
    session: AsyncSession,
    prepared: PreparedTurn,
    run: CollabRun,
    ticket: CollabTicket,
    node: NodeSpec,
    order,
    reviewer: str | None,
    state: TurnState,
) -> AsyncIterator[protocol.CollabEvent]:
    graph = prepared.graph
    nodes = node_map(graph)
    action = order.action

    if action is TicketAction.DELEGATE:
        target = nodes.get(order.assignee or "")
        if target is None:
            yield protocol.notice(f"派发目标「{order.assignee}」不在图里", "error")
            return
        if order.mode is DeliveryMode.SPLIT:
            # 一次 split 可拆多张（``tickets`` 数组）；单张时 ``order.ticket`` 就是它。
            # 先做全部护栏检查（深度 / 重名），任何一张不合规就整单拒绝——绝不留半截状态。
            drafts = order.tickets or ([order.ticket] if order.ticket else [])
            if not drafts:
                await _reject_action(
                    session,
                    run,
                    ticket,
                    state,
                    "mode=split 时必须带有 ticket 或 tickets（子工单字段）",
                )
                yield protocol.ticket_updated(_ticket_payload(ticket))
                return
            depth = await _ticket_depth(session, ticket)
            if depth >= MAX_TICKET_DEPTH:
                message = (
                    f"工单 {ticket_code(ticket.id)} 已经拆到第 {depth} 层（上限 {MAX_TICKET_DEPTH} 层），"
                    "再拆下去只会无穷套娃：请改用 handoff 直接交给实施者，或调整这次协作的拆解方式"
                )
                # 提示统一在本次推进收尾时发出（_finalize 会带上 state.reason），这里不重复
                await _reject_action(session, run, ticket, state, message)
                yield protocol.ticket_updated(_ticket_payload(ticket))
                return
            for draft in drafts:
                repeats = await _child_count_with_title(
                    session, ticket.id, draft.title or "子任务"
                )
                if repeats >= MAX_DUPLICATE_CHILD_TITLES:
                    message = (
                        f"同一个父工单下已经拆出过 {repeats} 张同名（或高度相似）的子工单，"
                        f"最近一张是「{draft.title or '子任务'}」。"
                        "再拆同一件事只会空转：要么先看看下游已经交回了什么、直接汇总收尾，"
                        "要么换一件真正不同的事、并给出更具体的标题；实在拿不准就 ask_user 让用户拍板"
                    )
                    await _reject_action(session, run, ticket, state, message)
                    yield protocol.ticket_updated(_ticket_payload(ticket))
                    return
            children: list[CollabTicket] = []
            for draft in drafts:
                # 每张可自带 assignee（分给不同岗位并行）；不带就用外层的目标
                draft_target = target
                if draft.assignee and draft.assignee != (order.assignee or ""):
                    override = nodes.get(draft.assignee)
                    if override is None:
                        await _reject_action(
                            session,
                            run,
                            ticket,
                            state,
                            f"子工单「{draft.title or '（无标题）'}」的派发对象「{draft.assignee}」不在图里",
                        )
                        yield protocol.ticket_updated(_ticket_payload(ticket))
                        return
                    draft_target = override
                children.append(
                    CollabTicket(
                        run_id=run.id,
                        parent_ticket_id=ticket.id,
                        from_agent_profile_id=node.agent_profile_id,
                        to_agent_profile_id=draft_target.agent_profile_id,
                        owner_key=node.key,
                        holder_key=draft_target.key,
                        title=(draft.title or "").strip() or "子任务",
                        brief=draft.to_dict(),
                        state=TicketState.OPEN.value,
                        max_rounds=effective_max_rounds(
                            draft_target, budget_from_storable(run.budget)
                        ),
                    )
                )
            # 谁拆的谁汇总：子工单全部完成后，父工单回到这个节点手里
            ticket.owner_key = node.key
            ticket.state = TicketState.WAITING_CHILDREN.value
            for child in children:
                session.add(child)
            session.add(ticket)
            run.tickets_used = (run.tickets_used or 0) + len(children)
            session.add(run)
            await session.commit()
            for child in children:
                await session.refresh(child)
                yield protocol.ticket_created(_ticket_payload(child))
            await session.refresh(ticket)
            yield protocol.ticket_updated(_ticket_payload(ticket))
            return

        # handoff：同一张工单换人做
        ticket.owner_key = ticket.owner_key or node.key
        ticket.holder_key = target.key
        ticket.to_agent_profile_id = target.agent_profile_id
        ticket.state = TicketState.WORKING.value
        if order.ticket and order.ticket.title:
            ticket.title = order.ticket.title
            ticket.brief = {**(ticket.brief or {}), **order.ticket.to_dict()}
        ticket.updated_at = utcnow()
        session.add(ticket)
        await session.commit()
        await session.refresh(ticket)
        yield protocol.ticket_updated(_ticket_payload(ticket))
        return

    if action is TicketAction.SUBMIT:
        ticket.result = {
            "summary": order.summary or "",
            "artifacts": order.artifacts,
        }
        ticket.submitter_key = node.key
        if reviewer:
            target = nodes.get(reviewer)
            ticket.state = TicketState.REVIEW.value
            ticket.holder_key = reviewer
            if target is not None:
                ticket.to_agent_profile_id = target.agent_profile_id
        else:
            ticket.state = TicketState.DONE.value
            ticket.closed_at = utcnow()
        ticket.updated_at = utcnow()
        session.add(ticket)
        await session.commit()
        await session.refresh(ticket)
        yield protocol.ticket_updated(_ticket_payload(ticket))
        return

    if action is TicketAction.REVIEW:
        if order.verdict == VERDICT_PASS:
            ticket.state = TicketState.DONE.value
            ticket.verdict = VERDICT_PASS
            ticket.closed_at = utcnow()
            ticket.updated_at = utcnow()
            session.add(ticket)
            await session.commit()
            await session.refresh(ticket)
            yield protocol.ticket_updated(_ticket_payload(ticket))
            return

        targets = rework_targets(graph, node.key)
        destination = order.rework_to or (
            ticket.submitter_key if ticket.submitter_key in targets else ""
        )
        if not destination:
            destination = targets[0] if len(targets) == 1 else ""
        target_node = nodes.get(destination)
        if target_node is None:
            message = (
                f"打回目标不明确：审查者「{node.key}」有多条退路"
                f"（{'、'.join(targets) or '无'}），需要在工单里用 rework_to 指明"
            )
            ticket.state = TicketState.BLOCKED.value
            run.error = message
            session.add(ticket)
            session.add(run)
            await session.commit()
            await session.refresh(ticket)
            yield protocol.notice(message, "error")
            yield protocol.ticket_updated(_ticket_payload(ticket))
            state.stop, state.status, state.reason = True, RunStatus.WAITING_USER.value, message
            return
        edge = rework_edge(graph, node.key, destination)
        cap = ticket.max_rounds or 0
        if edge is not None and edge.max_rounds:
            cap = min(cap, edge.max_rounds) if cap else edge.max_rounds
        if cap != (ticket.max_rounds or 0):
            # 边级上限更紧时，落到工单上（界面显示的「返工 x/y」就是它）
            ticket.max_rounds = cap
        ticket.round = (ticket.round or 0) + 1
        if cap and ticket.round > cap:
            message = (
                f"工单 {ticket_code(ticket.id)} 已返工 {ticket.round - 1} 轮仍未通过（上限 {cap} 轮），"
                "需要你介入：要么放宽这条边的返工上限，要么自己接手改完"
            )
            await _reject_action(session, run, ticket, state, message)
            yield protocol.ticket_updated(_ticket_payload(ticket))
            return
        ticket.state = TicketState.REWORK.value
        ticket.verdict = VERDICT_CHANGES
        ticket.review_comment = order.review_comment
        ticket.holder_key = target_node.key
        ticket.to_agent_profile_id = target_node.agent_profile_id
        ticket.updated_at = utcnow()
        session.add(ticket)
        await session.commit()
        await session.refresh(ticket)
        yield protocol.ticket_updated(_ticket_payload(ticket))
        return

    if action is TicketAction.DONE:
        ticket.state = TicketState.DONE.value
        ticket.closed_at = utcnow()
        # 收尾也要留下结论：done 的 remark 就是这张工单对外的交付摘要。
        # 少了它，父工单汇总时只看到一张"完成了"的空壳 —— 那正是「活干完了上游却看不见」的根因之一。
        summary = (order.summary or order.remark or "").strip()
        if summary or order.artifacts:
            result = dict(ticket.result or {})
            # 最新的结论为准：done 是这张工单最后一次表态，父工单汇总与收尾汇报都只看它
            if summary:
                result["summary"] = summary
            if order.artifacts:
                result["artifacts"] = order.artifacts
            ticket.result = result
        ticket.updated_at = utcnow()
        session.add(ticket)
        await session.commit()
        await session.refresh(ticket)
        yield protocol.ticket_updated(_ticket_payload(ticket))
        return

    if action is TicketAction.ASK_USER:
        question = order.question or "入口智能体需要你补充信息"
        options = [option.to_dict() for option in order.options]
        ticket.state = TicketState.BLOCKED.value
        ticket.result = {**(ticket.result or {}), "question": question, "options": options}
        ticket.updated_at = utcnow()
        session.add(ticket)
        await session.commit()
        await session.refresh(ticket)
        yield protocol.ticket_updated(_ticket_payload(ticket))
        yield protocol.notice(question, "ask", question=question, options=options)
        return

    if action is TicketAction.ESCALATE:
        target_key = await _escalation_target(
            session, ticket=ticket, node=node, entry_key=prepared.entry.key
        )
        if target_key is None or target_key not in nodes:
            await _reject_action(
                session,
                run,
                ticket,
                state,
                "这张工单没有可接手的上一级（发起人已不在图里，或已经到顶）："
                "需要用户拍板的事请让入口智能体用 ask_user 交给用户",
            )
            yield protocol.ticket_updated(_ticket_payload(ticket))
            return
        # 同一张工单被连续上报时累计「已经过谁」，提示词据此展示整条上报链
        was_escalated = ticket.state == TicketState.ESCALATED.value
        hops = list((ticket.escalation or {}).get("hops") or []) if was_escalated else []
        if node.key not in hops:
            hops.append(node.key)
        target = nodes[target_key]
        ticket.state = TicketState.ESCALATED.value
        ticket.holder_key = target_key
        ticket.to_agent_profile_id = target.agent_profile_id
        ticket.escalation = {
            "from": node.key,
            "reason": order.reason or "",
            "need": order.remark or "",
            "hops": hops,
        }
        ticket.updated_at = utcnow()
        session.add(ticket)
        await session.commit()
        await session.refresh(ticket)
        yield protocol.notice(
            f"工单 {ticket_code(ticket.id)} 被「{node.key}」上报给「{target_key}」："
            f"{ticket.escalation['reason'] or '未说明原因'}",
            "warn",
        )
        yield protocol.ticket_updated(_ticket_payload(ticket))
        return

    if action is TicketAction.REJECT:
        ticket.state = TicketState.REJECTED.value
        ticket.review_comment = order.reason or order.review_comment
        ticket.closed_at = utcnow()
        ticket.updated_at = utcnow()
        session.add(ticket)
        await session.commit()
        await session.refresh(ticket)
        yield protocol.notice(
            f"工单 {ticket_code(ticket.id)} 被「{node.key}」否决：{ticket.review_comment or '未说明原因'}",
            "error",
        )
        yield protocol.ticket_updated(_ticket_payload(ticket))
        return


async def _safe_apply(
    session: AsyncSession,
    prepared: PreparedTurn,
    run: CollabRun,
    ticket: CollabTicket,
    node: NodeSpec,
    order,
    reviewer: str | None,
    state: TurnState,
) -> AsyncIterator[protocol.CollabEvent]:
    """执行动作；任何意外都转成一条显式错误并挂起（绝不让事件流中途断掉）。"""
    try:
        async for event in _apply_action(session, prepared, run, ticket, node, order, reviewer, state):
            yield event
    except Exception as exc:  # noqa: BLE001
        message = f"处理工单动作时出错：{exc}"
        ticket.state = TicketState.BLOCKED.value
        run.error = message
        session.add(ticket)
        session.add(run)
        await session.commit()
        yield protocol.notice(message, "error")
        state.stop, state.status, state.reason = True, RunStatus.WAITING_USER.value, message


async def _rollup_parents(session: AsyncSession, run: CollabRun) -> AsyncIterator[protocol.CollabEvent]:
    """子工单全部收尾后，把父工单交回它的发起人。"""
    tickets = await _tickets(session, run.id)
    by_parent: dict[str, list[CollabTicket]] = {}
    for ticket in tickets:
        if ticket.parent_ticket_id:
            by_parent.setdefault(ticket.parent_ticket_id, []).append(ticket)
    for ticket in tickets:
        if ticket.state != TicketState.WAITING_CHILDREN.value:
            continue
        children = by_parent.get(ticket.id, [])
        if not children or any(child.state not in FINISHED_STATES for child in children):
            continue
        ticket.state = TicketState.OPEN.value
        ticket.holder_key = ticket.owner_key or ticket.holder_key
        ticket.updated_at = utcnow()
        session.add(ticket)
        await session.commit()
        await session.refresh(ticket)
        yield protocol.ticket_updated(_ticket_payload(ticket))


async def _finalize(session: AsyncSession, prepared: PreparedTurn, state: TurnState) -> AsyncIterator[protocol.CollabEvent]:
    # 并行波次在**别的会话**里原子自增过 steps_used / usage；本会话 identity map 里的
    # run 对象还是旧值（expire_on_commit=False），直接 add+commit 会用旧值把新值盖掉。
    # 这里强制重读一次，保证收尾写的是数据库里的最新状态。
    run = (
        (
            await session.execute(
                select(CollabRun)
                .where(CollabRun.id == prepared.run.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    if run is None:
        return
    tickets = await _tickets(session, run.id)
    root = next((t for t in tickets if t.parent_ticket_id is None), None)

    if state.status:
        status = state.status
    elif any(t.state == TicketState.BLOCKED.value for t in tickets):
        status = RunStatus.WAITING_USER.value
    elif root is not None and root.state == TicketState.DONE.value:
        status = RunStatus.DONE.value
    elif any(t.state in ACTIVE_STATES for t in tickets):
        status = RunStatus.WAITING_USER.value
    elif any(t.state == TicketState.DONE.value for t in tickets):
        status = RunStatus.DONE.value
    else:
        status = RunStatus.ERROR.value

    run.status = status
    run.error = state.reason if status in (RunStatus.WAITING_USER.value, RunStatus.ERROR.value) else None
    run.updated_at = utcnow()
    session.add(run)
    await session.commit()

    if state.reason:
        yield protocol.notice(state.reason, "warn" if state.budget_exhausted else "error")
    yield protocol.run_status(status, state.reason)


def _ticket_payload(ticket: CollabTicket) -> dict[str, Any]:
    return {
        "id": ticket.id,
        "run_id": ticket.run_id,
        "parent_ticket_id": ticket.parent_ticket_id,
        "from_agent_profile_id": ticket.from_agent_profile_id,
        "to_agent_profile_id": ticket.to_agent_profile_id,
        "owner_key": ticket.owner_key,
        "holder_key": ticket.holder_key,
        "submitter_key": ticket.submitter_key,
        "title": ticket.title,
        "brief": ticket.brief or {},
        "state": ticket.state,
        "round": ticket.round,
        "max_rounds": ticket.max_rounds,
        "verdict": ticket.verdict,
        "review_comment": ticket.review_comment,
        "result": ticket.result or {},
        "escalation": ticket.escalation or {},
        "created_at": ticket.created_at.isoformat(),
        "updated_at": ticket.updated_at.isoformat(),
        "closed_at": ticket.closed_at.isoformat() if ticket.closed_at else None,
    }
