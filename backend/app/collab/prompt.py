"""节点输入组装：把「岗位说明 + 工单载荷 + 本工单历史」拼成一次步骤的 prompt。

这是「不共享记忆」的落地处（需求 C5）：交给下游的**只有工单字段、一句话摘要与文件清单**，
不带任何智能体的对话原文，也不带其他工单的内容。纯函数模块，可以被直接断言。

人格（``profile.system_prompt``）不在这里拼 —— 它由 ``execution.py`` 按适配器的
``native_system_prompt`` 能力决定放进 system prompt 还是折进 prompt。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Sequence

from .graph import effective_output_contract
from .schemas import Duty, NodeSpec, TicketAction, TicketState, WorkspaceScope
from .tickets import FENCE, render_protocol_guide

DUTY_LABELS: dict[Duty, str] = {
    Duty.COORDINATOR: "协调者",
    Duty.ENGINEER: "实施者",
    Duty.REVIEWER: "审查者",
}

DUTY_DEFAULT_INSTRUCTIONS: dict[Duty, str] = {
    Duty.COORDINATOR: "你是这条链路的组织者：理解目标、拆解工作、把工单派给合适的下游，并对结果负责。",
    Duty.ENGINEER: "你是执行者：按工单的需求与验收标准动手完成，然后提交结果。",
    Duty.REVIEWER: "你是审查者：逐条对照验收标准检查产出，给出可执行的结论。",
}

DUTY_DEFAULT_SCOPE: dict[Duty, str] = {
    Duty.COORDINATOR: "不写代码、不改文件、不执行命令；只通过工单把任务交出去。",
    Duty.ENGINEER: "只在团队共享工作目录内改动；不得擅自扩大需求范围。",
    Duty.REVIEWER: "只读；不得修改任何文件，只产出审查意见。",
}

WORKSPACE_NOTES: dict[WorkspaceScope, str] = {
    WorkspaceScope.NONE: "没有该路径的读写权限；只依据工单信息工作，不要读取、写入或执行命令。",
    WorkspaceScope.READ: "只读；只能读取该路径内的文件，禁止创建、修改、删除，也禁止访问该路径之外的位置。",
    WorkspaceScope.WRITE: "可读写；所有读取与写入都必须在该路径内完成，禁止访问该路径之外的位置。",
}

#: 收尾汇报（根工单完成后交回入口智能体）允许的动作：只汇报或提问，绝不允许重新派活。
ENTRY_REPORT_ACTIONS: tuple[TicketAction, ...] = (TicketAction.DONE, TicketAction.ASK_USER)


@dataclass(slots=True)
class TicketView:
    """工单的只读快照（由 engine 从数据库读出后传入，prompt 层不碰 ORM）。"""

    id: str
    title: str
    brief: dict[str, Any] = field(default_factory=dict)
    state: str = TicketState.OPEN.value
    round: int = 0
    result: dict[str, Any] = field(default_factory=dict)
    review_comment: str | None = None
    submitter_key: str = ""
    #: 最近一次上报：{from, reason, need, hops}；非空表示这是下游上抛的阻塞件
    escalation: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HistoryEntry:
    """时间线上的一条**摘要**（不是原文）。"""

    role_label: str
    action: str
    gist: str


@dataclass(slots=True)
class ChatTicketView:
    """给入口智能体看的一张工单快照（只含结构化字段，不含其他智能体的对话原文）。"""

    id: str
    title: str
    state: str
    holder: str
    goal: str = ""
    requirements: Sequence[str] = ()
    acceptance: Sequence[str] = ()
    constraints: Sequence[str] = ()
    deliverables: Sequence[str] = ()
    summary: str = ""
    artifacts: Sequence[str] = ()
    review_comment: str = ""


@dataclass(slots=True)
class ChatHistoryEntry:
    """入口智能体与用户的一条历史发言。"""

    sender: str
    content: str


@dataclass(slots=True)
class TeamMember:
    """协作者名册里的一位成员（引擎从编排图导出，供每个节点知道「团队里有谁」）。

    名册是**图的公开结构**（用户自己画的），不是别的智能体的记忆，因此不违反 C5：
    它只回答「团队里有哪些人、谁负责什么、入口是谁」，不含任何人的对话原文或工单内容。
    它**不**声明谁能指挥谁 —— 派发 / 审查 / 打回的对象是逐轮变化的，那个权威来源是
    「输出协议」（见 ``ROSTER_CAVEAT``）。
    """

    key: str
    label: str = ""
    duty: Duty = Duty.ENGINEER
    #: 是不是入口智能体（唯一直接和用户对话的那个人）
    is_entry: bool = False
    #: 这位负责什么；留空就只显示岗位，不硬塞一句通用文案
    instructions: str = ""


def ticket_code(ticket_id: str) -> str:
    return f"T-{ticket_id[:8]}"


def _bullet_list(lines: Sequence[str], limit: int = 12) -> str:
    if not lines:
        return "（未指定）"
    items = [str(line).strip() for line in lines if str(line).strip()][:limit]
    if not items:
        return "（未指定）"
    return "\n".join(f"{index + 1}) {item}" for index, item in enumerate(items))


def _artifacts_text(result: dict[str, Any]) -> str:
    artifacts = result.get("artifacts") or []
    paths: list[str] = []
    for item in artifacts:
        if isinstance(item, dict):
            path = item.get("path") or item.get("name")
            if path:
                paths.append(str(path))
        elif isinstance(item, str):
            paths.append(item)
    if not paths:
        return ""
    shown = paths[:20]
    suffix = f"（共 {len(paths)} 个）" if len(paths) > len(shown) else ""
    return f"\n   - 变更文件：{'、'.join(shown)}{suffix}"


def _history_text(history: Sequence[HistoryEntry]) -> str:
    if not history:
        return "（本工单还没有历史）"
    lines = []
    for entry in history[-8:]:
        lines.append(f"  - {entry.role_label} · {entry.action}：{entry.gist}")
    return "\n".join(lines)


#: 名册末尾的收敛说明：看得见全组 ≠ 可以指挥全组（那件事由「输出协议」逐轮给出）。
ROSTER_CAVEAT = (
    "名册只说明团队里有哪些人、谁负责什么；你能把工单派给谁、谁审你、你能打回给谁，"
    "一律以「输出协议」里列出的对象为准。"
)

ROSTER_TITLE = "—— 协作组名册（系统提供的团队结构，只读）——"


def roster_lines(roster: Sequence[TeamMember]) -> str:
    """渲染团队成员名册（一行一人）。"""
    lines: list[str] = []
    for index, member in enumerate(roster, start=1):
        name = (
            f"{member.label}（{member.key}）"
            if member.label and member.label != member.key
            else member.key
        )
        tags = [DUTY_LABELS[member.duty]]
        if member.is_entry:
            tags.append("入口，唯一和用户对话的智能体")
        if member.instructions.strip():
            tags.append(_chat_text(member.instructions, 80))
        lines.append(f"   {index}) {name} · {' · '.join(tags)}")
    return "\n".join(lines)


def roster_block(roster: Sequence[TeamMember], *, caveat: str = ROSTER_CAVEAT) -> list[str]:
    """名册区块（题头 + 逐行 + 收敛说明）；空名册返回空列表。"""
    if not roster:
        return []
    return [ROSTER_TITLE, roster_lines(roster), f"   {caveat}"]


def build_step_prompt(
    *,
    team_name: str,
    workspace_path: str,
    node: NodeSpec,
    ticket: TicketView,
    history: Sequence[HistoryEntry],
    assignees: Sequence[str],
    reviewer: str | None,
    rework_targets: Sequence[str] = (),
    user_message: str | None = None,
    node_labels: dict[str, str] | None = None,
    roster: Sequence[TeamMember] = (),
    closing_report: bool = False,
    run_digest: Sequence[str] = (),
    downstream: Sequence[str] = (),
    downstream_ready: bool = False,
    is_entry: bool = True,
) -> str:
    """组装一次步骤的完整输入。"""
    labels = node_labels or {}
    duty_label = DUTY_LABELS[node.duty]
    scope = node.workspace_scope or {
        Duty.COORDINATOR: WorkspaceScope.NONE,
        Duty.ENGINEER: WorkspaceScope.WRITE,
        Duty.REVIEWER: WorkspaceScope.READ,
    }[node.duty]

    def named(key: str) -> str:
        label = labels.get(key, "")
        return f"{label}（{key}）" if label and label != key else key

    brief = ticket.brief or {}
    # 上抛件：这张工单正等发起人处理（state 为 escalated）。只有这一段「在办」期间才收窄动作、
    # 才显示上报表；一旦交到用户手里（ask_user）或被解决，它就该回到正常工单的规则。
    escalated = ticket.state == TicketState.ESCALATED.value
    # 收尾汇报：入口智能体不再派活，只看全流程结果，对用户说清结论。
    if closing_report:
        actions = ENTRY_REPORT_ACTIONS
    else:
        # 动作白名单只有一个来源（graph.effective_output_contract）：非入口自动剔除 ask_user，
        # 上抛件自动剔除 delegate。否则提示词会教模型做它运行时做不了的事
        # （真实事故：dev-3 被教着问用户，运行时判越权；CTO 被教着往下派，把阻塞原地转一圈）。
        actions = effective_output_contract(node, is_entry=is_entry, escalated=escalated)

    header = [
        f"[协作任务] 协作组「{team_name}」· 你的岗位：{duty_label}（{node.key}）",
        f"职责：{node.instructions.strip() or DUTY_DEFAULT_INSTRUCTIONS[node.duty]}",
        f"能力边界（必须遵守）：{node.capability_scope.strip() or DUTY_DEFAULT_SCOPE[node.duty]}",
        f"工作路径（本次协作唯一允许访问的目录）：{workspace_path}",
        f"路径权限（必须遵守）：{WORKSPACE_NOTES[scope]}",
    ]
    # 名册紧跟岗位信息、排在冗长的输出协议之前：它是「我在和谁一起干活」的背景，
    # 埋在一串协议规则与示例之后容易被忽略（用户反馈：入口智能体答不出团队里有谁）。
    if roster:
        header.extend(["", *roster_block(roster)])
    header.extend([
        "",
        "输出协议：",
        render_protocol_guide(
            duty_label=duty_label,
            actions=actions,
            assignees=[] if closing_report else [named(key) for key in assignees],
            reviewer=named(reviewer) if reviewer else None,
            rework_targets=[named(key) for key in rework_targets],
            duty=node.duty,
        ),
    ])

    body = [
        "",
        f"—— 工单 {ticket_code(ticket.id)} ——",
        f"标题：{ticket.title}",
        f"目标：{brief.get('goal') or '（未指定）'}",
        f"需求：\n{_bullet_list(brief.get('requirements') or [])}",
        f"验收标准：\n{_bullet_list(brief.get('acceptance') or [])}",
        f"约束：\n{_bullet_list(brief.get('constraints') or [])}",
    ]
    if brief.get("deliverables"):
        body.append(f"交付物：\n{_bullet_list(brief['deliverables'])}")

    if escalated:
        esc = ticket.escalation or {}
        body.append("—— 上报：下游卡住了，需要你处理（不要再原样往下派）——")
        if esc.get("from"):
            body.append(f"   - 上报人：{named(str(esc['from']))}")
        if esc.get("hops"):
            body.append(f"   - 已经过：{' → '.join(named(str(hop)) for hop in esc['hops'])}")
        if esc.get("reason"):
            body.append(f"   - 卡在哪：{esc['reason']}")
        if esc.get("need"):
            body.append(f"   - 需要你：{esc['need']}")

    result = ticket.result or {}
    if result.get("summary") or result.get("artifacts"):
        body.append("上游交付摘要：")
        if result.get("summary"):
            body.append(f"   - {result['summary']}")
        artifacts = _artifacts_text(result)
        if artifacts:
            body.append(artifacts.lstrip("\n"))

    if downstream:
        # 「我派出去的活交回了什么」：让上游看得见下游的结论，否则它只能反复重派（见 C5 的边界说明）
        body.append("下游交付（我派出去的子工单，引擎汇总，只有结构化字段）：")
        body.extend(f"   - {line}" for line in downstream)

    body.append(f"本工单历史（仅这一张工单）：\n{_history_text(history)}")
    if closing_report:
        body.append(f"—— 全流程结果（引擎汇总，只有结构化字段）——\n{_digest_text(run_digest)}")
    body.append(
        "本轮要求："
        + expected_instruction(
            node,
            ticket,
            user_message=user_message,
            closing_report=closing_report,
            downstream_ready=downstream_ready,
            allow_ask_user=is_entry,
            escalated=escalated,
            actions=actions,
        )
    )
    return "\n".join(header) + "\n".join(body) + "\n"


def _digest_text(digest: Sequence[str]) -> str:
    if not digest:
        return "（没有可汇总的工单）"
    return "\n".join(f"{index + 1}) {line}" for index, line in enumerate(digest))


def expected_instruction(
    node: NodeSpec,
    ticket: TicketView,
    *,
    user_message: str | None = None,
    closing_report: bool = False,
    downstream_ready: bool = False,
    escalated: bool = False,
    allow_ask_user: bool = True,
    actions: Sequence[TicketAction] | None = None,
) -> str:
    """按岗位与工单当前状态，告诉它这一轮应该干什么。

    ``actions`` 是本轮**生效**的动作白名单（已剔除非入口的 ask_user）。写指令时必须与它一致，
    否则会出现「协议说只能 done、本轮要求却催 submit」的自相矛盾 —— 自定义动作契约的错误来源之一。
    """
    can_submit = actions is None or TicketAction.SUBMIT in actions
    # ask_user 只有入口节点能做；非入口节点要用户拍板时，只能让上游/入口智能体转达。
    ask_note = (
        "需要用户拍板时用 ask_user（必须带 2-6 个可点 options）。"
        if allow_ask_user
        else "需要用户拍板时不要自己问：把问题写进正文和 remark，交给上层或入口智能体统一向用户确认。"
    )
    if closing_report:
        return (
            "所有工单都已收尾，现在轮到你向用户汇报：用几句话讲清「做完了什么、"
            "关键产出（文件或结论）、还需要用户注意什么」，用户看得懂就行，别复述过程。"
            "然后用 done 关闭这张工单（remark 写一句同样的结论）。"
            "不要重新派活、不要把已经做过的活再做一遍；确实还需要用户拍板时才用 ask_user。"
        )

    if escalated:
        # 上抛件：只能「自己解决 / 继续往上 / 交给用户」，绝不允许再往下派
        # （真实事故：CTO 把「环境跑不了」改写成「静态核对」又派给码农，白转一轮）。
        if allow_ask_user:
            return (
                "上面这张工单是下游**上报**的阻塞，全流程卡在这里，需要你定夺。"
                "如果你现在能定，就直接处理并收尾（该动手就动手，别把同一件事原样派回去）；"
                "确实需要用户拍板，就用 ask_user 把问题转达给用户 —— question 写清要决定什么，"
                "options 给 2-6 条可直接点选的选项。"
            )
        return (
            "上面这张工单是下游**上报**的阻塞。请不要再往下派："
            "能解决就解决并收尾（自己动手然后 submit/done，或按需交给真正能解决的岗位）；"
            "解决不了就继续 escalate 往上（reason 写清这一轮为什么没解决、你需要谁做什么），"
            "由发起人一路报到入口智能体那里去问用户。"
        )

    if ticket.state == TicketState.REVIEW.value and node.duty is Duty.REVIEWER:
        text = (
            "请审查上面这张工单的产出：逐条对照验收标准，能通过就给 "
            "review + pass；不通过就给 review + changes_requested，"
            "并在 review_comment 里写清不符合哪一条、为什么。"
        )
        return text

    if ticket.state == TicketState.REWORK.value:
        comment = (ticket.review_comment or "").strip()
        base = "这是一张被审查打回、需要返工的工单。"
        if comment:
            base += f"审查意见：{comment}"
        if node.duty is Duty.REVIEWER:
            return base
        if can_submit:
            tail = "改完后用 submit 重新提交。"
        else:
            tail = "改完后用 done 收尾（remark 写清改了什么）。"
        return (
            base + " 请针对每一条意见修改；" + tail
            + "如果你需要重新组织分工，也可以 delegate 给下游（用 split 拆子工单）。"
        )

    if downstream_ready and not user_message:
        # 「我派出去的活已经有回音了」：这是引擎给上游的收尾信号。
        # 没有它，上游只看得见自己几次派单记录，会误判成"活没干完"而反复重派（见 C5 的边界说明）。
        if node.duty is Duty.ENGINEER:
            submit_text = (
                "目标已达成就用 submit 提交（summary 写结论、artifacts 列出产出文件）；"
                if can_submit
                else "目标已达成就用 done 收尾（remark 写一句结论）；"
            )
            return (
                "你派出去的子工单已经全部收尾（见上面「下游交付」）。"
                "请把它们的结论汇总成一句话：" + submit_text
                + "确实还有没干的活再继续动手，别重复已经完成的部分。"
            )
        return (
            "你派出去的子工单已经全部收尾（见上面「下游交付」，那里就是它们交回的结论）。"
            "请先汇总：如果原始目标已经达成，直接 done 关闭这张工单（remark 写一句结论）；"
            "只有在确实还有没干的活时才 delegate，并说明这次要做的事与已交付的有何不同 —— "
            f"不要为了「再确认一下」重复派发同一件事；{ask_note}"
        )

    if node.duty is Duty.ENGINEER:
        if can_submit:
            return "请按需求动手完成，然后用 submit 提交结果（summary + artifacts）。"
        return "请按需求动手完成，然后用 done 收尾（remark 写一句结论）。"
    if node.duty is Duty.REVIEWER:
        return "请给出审查结论（review + verdict）。"
    if user_message:
        return (
            "用户刚刚说：" + user_message.strip() + "\n"
            "请据此更新工单：能派下去就 delegate（handoff 转交 / split 拆子工单）；"
            + ("信息不足就 ask_user 提问（必须带 2-6 个可点 options）；" if allow_ask_user else "")
            + "整件事都完成了用 done。"
        )
    return (
        "请推进这张工单：把任务派给合适的下游（delegate），"
        "或者确认它已经完成（done）；"
        + ask_note
    )


def _chat_text(text: str, limit: int = 2000) -> str:
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "…（已截断）"


def _chat_ticket_block(ticket: ChatTicketView) -> str:
    lines = [
        f"- {ticket_code(ticket.id)}《{ticket.title}》 · 状态：{ticket.state} · 当前持有：{ticket.holder or '未指定'}"
    ]
    if ticket.goal:
        lines.append(f"  目标：{_chat_text(ticket.goal, 500)}")
    if ticket.requirements:
        lines.append(f"  要求：{'；'.join(list(ticket.requirements)[:8])}")
    if ticket.acceptance:
        lines.append(f"  验收：{'；'.join(list(ticket.acceptance)[:8])}")
    if ticket.constraints:
        lines.append(f"  约束：{'；'.join(list(ticket.constraints)[:8])}")
    if ticket.deliverables:
        lines.append(f"  交付物：{'；'.join(list(ticket.deliverables)[:8])}")
    if ticket.summary:
        lines.append(f"  最近结论：{_chat_text(ticket.summary, 500)}")
    if ticket.artifacts:
        shown = list(ticket.artifacts)[:8]
        suffix = f"（共 {len(ticket.artifacts)} 个）" if len(ticket.artifacts) > len(shown) else ""
        lines.append(f"  产出文件：{'、'.join(shown)}{suffix}")
    if ticket.review_comment:
        lines.append(f"  最近审查意见：{_chat_text(ticket.review_comment, 500)}")
    return "\n".join(lines)


def build_chat_prompt(
    *,
    team_name: str,
    run_title: str,
    run_status: str,
    node: NodeSpec,
    tickets: Sequence[ChatTicketView],
    history: Sequence[ChatHistoryEntry],
    user_message: str,
    active_label: str | None = None,
    node_labels: dict[str, str] | None = None,
    roster: Sequence[TeamMember] = (),
) -> str:
    """组装入口智能体的「常驻聊天」输入。

    这是 C5（智能体之间不共享记忆）的**唯一例外**，但例外只发生在「用户 ↔ 入口智能体」这条
    线上：聊天历史只回放给入口智能体本人；工单步骤的 prompt 仍然只拿结构化字段。
    """
    labels = node_labels or {}
    role_label = node.label or labels.get(node.key) or node.key
    scope = DUTY_DEFAULT_SCOPE[node.duty]
    ticket_text = "\n".join(_chat_ticket_block(ticket) for ticket in tickets) or "（还没有工单）"
    roster_text = "\n".join(
        roster_block(roster, caveat="名册只说明团队里有哪些人、谁负责什么；聊天里不派活。")
    ) or f"{ROSTER_TITLE}\n（名册不可用）"
    if history:
        history_text = "\n".join(
            f"- {'用户' if turn.sender == 'user' else '你'}：{_chat_text(turn.content)}"
            for turn in list(history)[-20:]
        )
    else:
        history_text = "（这是你们的第一轮对话）"
    running = f"当前正在执行：{active_label}" if active_label else "当前没有智能体在动手（流程在等待或已收尾）"

    return f"""[协作对话] 你是这次协作的入口智能体，正在和用户一对一沟通。这不是工单步骤。
团队：{team_name}
你的岗位：{role_label}（{node.key}）
任务：{run_title}
运行状态：{run_status}
你的职责边界：{scope}

{roster_text}

用户的所有消息都会先到这里：推进中、等待拍板时、收尾之后、任务还没开始时都会发进来。
由你判断每条消息是「聊一聊」还是「需要把任务推进起来」：前者正常回答，
后者在回复末尾附一个 chat_advance 块，系统会给用户一张决策卡，确认后才进入工单通道。
请像正常的项目负责人一样回答，简洁、具体、说人话。

—— 当前工单快照（系统提供的结构化事实，只读）——
{ticket_text}
{running}

—— 你们最近的对话（只属于你和用户，不会传给其他智能体）——
{history_text}

—— 用户刚刚说 ——
{user_message.strip()}

—— 回答规则 ——
1. 先判断这条消息属于哪一类，再决定怎么回：
   a. 问进度、聊方案、闲聊、需要澄清 → 正常回复用户，不要输出任何协议块；
   b. 明确补充/修改任务要求 → 正常回复，并在最后追加一个 chat_update 代码块（见规则 3）；
   c. 需要把活干起来（新任务、继续推进、答复待决策问题、明确要执行的事） →
      正常回复，并在最后追加一个 chat_advance 代码块（见规则 5）。
2. 不要输出 work_order，也不要假装已经派活、改工单或启动新流程；真正执行一律走 chat_advance，
   由用户在决策卡上确认后才进入工单通道。
3. chat_update 必须是合法 JSON，字段全部可选，列表只写新追加的内容，不要重复工单里已有的条目：
```chat_update
{{
  "goal": "可选：只有任务目标确实变化时才写",
  "requirements_add": ["新增要求"],
  "acceptance_add": ["新增验收标准"],
  "constraints_add": ["新增约束"],
  "deliverables_add": ["新增交付物"]
}}
```
4. chat_update / chat_advance 块只能放在最后（两者都有时先 chat_update 再 chat_advance）；
   块里不要写注释，也不要把聊天正文塞进去。
5. chat_advance 是你对「是否推进任务」的判断：系统会把它渲染成一张决策卡给用户点选，
   点选后指令才进入工单通道。必须是合法 JSON：
```chat_advance
{{
  "reason": "一句话：为什么这需要推进任务（卡片上给用户看）",
  "order": "建议发出的完整指令：点选后原样作为推进消息发出",
  "options": [
    {{"label": "选项标题", "value": "点选后实际发出的指令", "description": "可选的一句说明"}},
    {{"label": "我来补充", "value": "", "freeform": true}}
  ]
}}
```
   order 写你建议的完整指令；options 可选（2-6 条，给用户备选或调整的余地），
   不给时系统会按 order 自动补一张「按建议推进 / 我再想想」的卡片。
   有待回答的问题（上面工单快照里 state=blocked 且带 question 的）时，order 必须能作为
   对那个问题的答复直接发出。
6. 其他智能体看不到聊天原文，所以真正需要执行的新要求必须写进 chat_update 或 chat_advance，
   光在正文里说“按这个做”是不会传下去的。
7. 用户问「团队里有哪些人 / 谁负责什么」时，照上面的名册回答（岗位 + 职责，入口是谁）；
   名册之外的人不存在，不要编造，也不要凭印象说某个岗位空缺。
"""


def build_repair_prompt(
    *,
    order: Any,
    error_text: str,
    assignees: Sequence[str],
    rework: Sequence[str],
    duty_label: str = "",
    node_key: str = "",
) -> str:
    """路由拼写错误的一次性修复 prompt：**只含**上轮工单、错误原因与合法枚举。

    刻意不带工单历史、名册、长对话 —— 修复轮是同一步内的格式纠正，不是重新决策；
    上下文越短，模型越只能做一件事：把错误的 assignee/rework_to 换成合法 key。
    岗位头保留一行（与正常步骤同格式）：内核（与测试替身）都靠它识别「我是谁」。
    """
    order_json = json.dumps(order, ensure_ascii=False, indent=2, default=str)
    assignee_keys = "、".join(sorted(assignees)) or "（无）"
    rework_keys = "、".join(sorted(rework)) or "（无）"
    header = f"[协议修复] 协作任务 · 你的岗位：{duty_label}（{node_key}）\n" if node_key else ""
    return f"""{header}[协议修复] 你刚输出的一份工单指令里，路由目标填错了。这不是新任务，也不要重做任何工作。

上一轮你输出的 work_order（仅供对照，**不要原样重发**）：
```
{order_json}
```

系统校验给出的错误：
{error_text}

合法取值（只能从中选）：
- assignee：{assignee_keys}
- rework_to：{rework_keys}

请输出**修正后的完整 work_order**：保持原来的 action / ticket 内容不变，只把错误的路由目标
（assignee 或 rework_to，包括 tickets 里每张子工单的 assignee）换成上面列表里的一个 key。
不要解释，不要输出别的内容，只给一个 {FENCE} 代码块，内容是修正后的 JSON 对象。
"""
