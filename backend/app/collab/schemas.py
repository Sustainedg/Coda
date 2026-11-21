"""协作域的请求/响应契约与编排图结构。

这里是「画布上的那张图」的权威定义：节点（智能体在协作里的岗位）与有向边（允许的流转）。
图的问题分两类：

* **结构性错误**（不是合法的 JSON 形状、类型不对）→ Pydantic 直接 422；
* **语义错误**（少入口、缺回边、成环…）→ ``graph.validate_graph()`` 返回可读的
  ``errors`` / ``warnings``。保存时不拦截（允许存草稿），发起运行时才拒绝。

请求体一律 ``extra="allow"``：未知键原样保留，便于后续加字段时旧前端不崩。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Duty(str, Enum):
    """节点在协作里的岗位。"""

    COORDINATOR = "coordinator"  # 协调者：与用户对话、只派活
    ENGINEER = "engineer"        # 实施者：真正动手改代码
    REVIEWER = "reviewer"        # 审查者：只读 + 给审查意见


class EdgeKind(str, Enum):
    """有向边的类型，决定工单能沿哪个方向流转。"""

    ASSIGN = "assign"  # 派发 / 交付
    REVIEW = "review"  # 提交给审查者
    REWORK = "rework"  # 审查者打回给实施者（必须与 review 边成对出现）


class EdgePort(str, Enum):
    """连线在节点卡片上使用的端口；输入只允许左/上，输出只允许右/下。"""

    LEFT = "left"
    RIGHT = "right"
    TOP = "top"
    BOTTOM = "bottom"


class WorkspaceScope(str, Enum):
    """节点对团队共享工作目录的权限范围。"""

    NONE = "none"    # 不给工作目录（分配一次性空目录）
    READ = "read"    # 可读；越权写会被运行前后的指纹审计发现
    WRITE = "write"  # 可写


class TicketAction(str, Enum):
    """一个节点在一轮里能发起的动作。"""

    DELEGATE = "delegate"   # 派发 / 拆分子工单
    SUBMIT = "submit"       # 提交实施结果
    REVIEW = "review"       # 审查并给出结论
    DONE = "done"           # 确认完成
    ASK_USER = "ask_user"   # 向用户提问（仅入口节点）
    ESCALATE = "escalate"   # 上报：卡住了、需要发起人/上层拍板或协调
    REJECT = "reject"       # 否决，终止该分支


class DeliveryMode(str, Enum):
    """``delegate`` 的两种语义：把同一件活交出去，还是拆出别人独立负责的子工单。"""

    HANDOFF = "handoff"  # 转交：本工单换持有者，我继续等结果
    SPLIT = "split"      # 拆分：新建子工单（我是发起人，等它完成后汇总）


class TicketState(str, Enum):
    OPEN = "open"
    WORKING = "working"
    WAITING_CHILDREN = "waiting_children"
    REVIEW = "review"
    REWORK = "rework"
    ESCALATED = "escalated"
    DONE = "done"
    REJECTED = "rejected"
    BLOCKED = "blocked"


class RunStatus(str, Enum):
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


#: 审查结论的合法取值
VERDICT_PASS = "pass"
VERDICT_CHANGES = "changes_requested"
VERDICTS = (VERDICT_PASS, VERDICT_CHANGES)


def _edge_id() -> str:
    return uuid.uuid4().hex[:12]


class NodeSpec(BaseModel):
    """协作图上的一个节点 = 某个智能体在本次协作里担任的岗位。"""

    model_config = ConfigDict(extra="allow")

    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
    agent_profile_id: str = Field(min_length=1, max_length=64)
    label: str = Field(default="", max_length=120)
    duty: Duty = Duty.ENGINEER
    #: 职责说明，追加在智能体人格之后
    instructions: str = ""
    #: 能力边界（写给人看、也写进 prompt 的约束）
    capability_scope: str = ""
    #: 为空表示按 duty 取默认值（协调者 none / 实施者 write / 审查者 read）
    workspace_scope: WorkspaceScope | None = None
    #: 为空表示按 duty 取默认动作集合
    output_contract: list[TicketAction] = Field(default_factory=list)
    #: 同一工单在该节点上最多返工几轮；0 表示取团队默认
    max_rounds: int = Field(default=0, ge=0)
    #: 协作专属技能分配：None = 完全跟随智能体页面的分配；给了列表就以它为准
    #: （空列表 = 这个协作里不给任何技能）。只影响协作运行，不写回智能体配置。
    skill_ids: list[str] | None = None
    #: 协作专属 MCP 分配：语义同 skill_ids。
    mcp_ids: list[str] | None = None
    x: float = 0
    y: float = 0


class EdgeSpec(BaseModel):
    """一条有向边：``from_key`` 允许把工单沿 ``kind`` 交给 ``to_key``。"""

    model_config = ConfigDict(extra="allow")

    id: str = Field(default_factory=_edge_id, min_length=1, max_length=64)
    from_key: str = Field(min_length=1, max_length=64)
    to_key: str = Field(min_length=1, max_length=64)
    kind: EdgeKind = EdgeKind.ASSIGN
    label: str = Field(default="", max_length=120)
    #: 这条边额外的返工轮次上限（0 = 不额外限制）：实际上限取「边 / 节点 / 运行时预算」三者最小
    max_rounds: int = Field(default=0, ge=0)
    #: 画布端口方向（只影响编排图的外观，不参与运行语义）；旧图为空时由前端自动推断。
    from_side: EdgePort | None = None
    to_side: EdgePort | None = None


class GraphSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    nodes: list[NodeSpec] = Field(default_factory=list)
    edges: list[EdgeSpec] = Field(default_factory=list)


class BudgetSpec(BaseModel):
    """一次协作运行的预算上限。任何一项超限都会挂起并明确提示用户。"""

    # 一张子工单的最小生命周期（拆单→干活→评审→交回汇总）至少 4 步，有返工就是 6-8 步；
    # 60 步在「领导者 + split 拆子工单」的图里跑十来张工单就会在收尾前被掐断。
    max_steps: int = Field(default=200, ge=1)
    max_tickets: int = Field(default=30, ge=1)
    max_rounds_per_ticket: int = Field(default=3, ge=1)
    #: 0 表示不限制 token
    max_tokens: int = Field(default=0, ge=0)
    timeout_seconds: int = Field(default=3600, ge=30)
    #: 单步限时（秒）：一个工单步骤 / 聊天回复的执行上限；**0 表示不限**。
    #: 这是协作域**自己的**时限，与对话域的全局 ``run_timeout_seconds`` 互不影响——
    #: 重活（大仓库构建、长测试）经常跑过一小时；0 在这里是「不限」而不是「用默认」，
    #: 执行层会把它转成 runner 认识的 -1。
    step_timeout_seconds: int = Field(default=36000, ge=0)
    #: 同一波最多并行推进几个工单步骤；**1 = 完全串行**（一期默认，行为与过去完全一致）。
    #: 只有「同一父工单拆出的兄弟工单、且持有者岗位互不相同」才会并行（见 ``engine.consume_turn``）；
    #: 同一岗位永远串行（一个岗位一个对话通道），返工 / 审查环也只在波次边界推进。
    max_parallel: int = Field(default=1, ge=1, le=8)


class GraphIssue(BaseModel):
    code: str
    message: str
    node_key: str | None = None
    edge_id: str | None = None


class GraphValidation(BaseModel):
    ok: bool = True
    errors: list[GraphIssue] = Field(default_factory=list)
    warnings: list[GraphIssue] = Field(default_factory=list)

    @classmethod
    def build(cls, errors: list[GraphIssue], warnings: list[GraphIssue]) -> GraphValidation:
        return cls(ok=not errors, errors=errors, warnings=warnings)


def _clean_workspace_dir(value: str | None) -> str | None:
    """规整协作组的工作路径：留空 = 默认目录，填写则必须是绝对路径。"""
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        path = Path(raw).expanduser()
    except RuntimeError as exc:
        raise ValueError("工作路径里的 `~` 无法展开，请直接填写绝对路径") from exc
    if not path.is_absolute():
        raise ValueError("工作路径必须是绝对路径，例如 /vol1/projects/my-app")
    return str(path)


class TeamCreate(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    entry_key: str | None = None
    workspace_dir: str | None = None
    graph: GraphSpec = Field(default_factory=GraphSpec)
    budget: BudgetSpec = Field(default_factory=BudgetSpec)

    _clean_create_workspace = field_validator("workspace_dir", mode="before")(_clean_workspace_dir)


class TeamUpdate(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    entry_key: str | None = None
    workspace_dir: str | None = None
    #: 整图替换：编辑器保存的就是完整的一张图
    graph: GraphSpec | None = None
    budget: BudgetSpec | None = None

    _clean_update_workspace = field_validator("workspace_dir", mode="before")(_clean_workspace_dir)


class TeamRead(BaseModel):
    id: str
    name: str
    description: str | None = None
    entry_key: str | None = None
    workspace_dir: str | None = None
    graph: GraphSpec
    budget: BudgetSpec
    node_count: int = 0
    created_at: datetime
    updated_at: datetime
    #: 每次都重新算：编辑器要能在保存前就看到问题
    validation: GraphValidation = Field(default_factory=GraphValidation)


class TeamValidateIn(BaseModel):
    """只校验不保存；不给图就校验已存的那张。"""

    model_config = ConfigDict(extra="allow")

    entry_key: str | None = None
    graph: GraphSpec | None = None


class RunCreate(BaseModel):
    """发起一次协作运行；目标本身走 ``/runs/{id}/messages`` 的第一条消息。"""

    model_config = ConfigDict(extra="allow")

    team_id: str = Field(min_length=1, max_length=64)
    title: str | None = None
    #: 本次任务的工作路径；为空时回落到团队的默认路径，再为空则按运行 ID 分配
    workspace_dir: str | None = None

    _clean_run_workspace = field_validator("workspace_dir", mode="before")(_clean_workspace_dir)


class RunMessage(BaseModel):
    content: str = ""
    #: 已上传附件的相对路径（相对这次协作的工作目录，由 `/runs/{id}/attachments` 返回）
    attachments: list[str] = Field(default_factory=list)
    #: 这条消息引用的是「推进任务」决策卡的建议：``content`` 仍是执行的完整指令，
    #: 但时间线上不把长指令再显示一遍，只落一句短标记（指令原文在 CEO 的消息里
    #: 已经写过，重发一遍既占屏幕又多花一遍 tokens）。
    use_advance: bool = False


class RunBudgetPatch(BaseModel):
    """运行中调整预算：只允许调大或补时限，不允许把预算调小到当前用量以下。

    典型场景：步数/工单数超限挂起后，用户提高上限再点「继续」——
    过去预算在创建运行时快照、超限后 resume 会立刻再挂起，运行等于救不回来。
    """

    model_config = ConfigDict(extra="forbid")

    #: 新的步数上限；为空表示不改。必须大于当前已用步数。
    max_steps: int | None = Field(default=None, ge=1)
    #: 新的工单数上限；为空表示不改。必须大于当前已用工单数。
    max_tickets: int | None = Field(default=None, ge=1)
    #: 新的 token 上限（0 = 不限）；为空表示不改。0 以外的值必须大于当前已用 token。
    max_tokens: int | None = Field(default=None, ge=0)
    #: 新的运行时限（秒）；为空表示不改。
    timeout_seconds: int | None = Field(default=None, ge=30)
    #: 新的单步限时（秒，0 = 不限）；为空表示不改。对正在跑的那一步不生效，从下一步起算。
    step_timeout_seconds: int | None = Field(default=None, ge=0)
    #: 新的并行上限（1 = 串行）；为空表示不改。对正在跑的这一波不生效，从下一波起算。
    max_parallel: int | None = Field(default=None, ge=1, le=8)


class RunAttachmentUpload(BaseModel):
    """上传一个协作附件：内容走 base64，省掉 multipart 依赖（同对话附件）。"""

    name: str = Field(min_length=1, max_length=200)
    content_base64: str = Field(min_length=1)


class TicketRead(BaseModel):
    id: str
    run_id: str
    parent_ticket_id: str | None = None
    from_agent_profile_id: str | None = None
    to_agent_profile_id: str
    owner_key: str = ""
    holder_key: str = ""
    submitter_key: str = ""
    title: str
    brief: dict[str, Any] = Field(default_factory=dict)
    state: str
    round: int = 0
    max_rounds: int = 0
    verdict: str | None = None
    review_comment: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    #: 最近一次上报记录 {from, reason, need, hops}；为空表示没被上报过
    escalation: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None


class StepRead(BaseModel):
    id: str
    run_id: str
    ticket_id: str | None = None
    #: 用户自己的发言没有智能体
    agent_profile_id: str | None = None
    profile_name: str | None = None
    role_key: str = ""
    duty: str = ""
    sequence: int = 0
    action: str = ""
    #: delegate 把活交给了哪个节点；空串表示不是派发或未知
    target_key: str = ""
    status: str
    content: str = ""
    #: 引擎派生的结构化摘要（交接给下游的就是它，而不是 content）
    gist: str = ""
    #: 详情接口**不再**携带完整过程（工具调用/思考可能上 MB，一次运行几十步就是几十 MB）；
    #: 这里只给「有没有、有多少」的元信息，正文气泡照常渲染，点开「过程」折叠块时
    #: 再按 ``GET /runs/{run_id}/steps/{step_id}/parts`` 懒加载。
    parts: list[dict[str, Any]] = Field(default_factory=list)
    parts_count: int = 0
    #: 这一步调用过的工具名（去重、限量）：看板/进度页要展示「用过哪些工具」，
    #: 但不需要完整流水——从 parts 里提取一次放在这里，前端不用为它拉懒加载接口。
    tool_names: list[str] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    audit: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime
    finished_at: datetime | None = None


class StepPartsRead(BaseModel):
    """单个步骤的完整过程（``parts``）——懒加载接口的响应体。"""

    step_id: str
    run_id: str
    parts: list[dict[str, Any]] = Field(default_factory=list)


class ChatMessageRead(BaseModel):
    """入口智能体对话的一条消息（``collab_messages``）。"""

    id: str
    run_id: str
    #: user / leader
    sender: str
    role_key: str = ""
    agent_profile_id: str | None = None
    sequence: int = 0
    status: str = "complete"
    content: str = ""
    parts: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    #: 需求更新、同步工单与解析告警等结构化信息
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    finished_at: datetime | None = None


class RunRead(BaseModel):
    id: str
    team_id: str
    team_name: str | None = None
    title: str
    goal: str = ""
    status: str
    entry_key: str | None = None
    workspace_dir: str | None = None
    #: 这次运行的预算（界面用来显示「已用 / 上限」）
    budget: BudgetSpec = Field(default_factory=BudgetSpec)
    steps_used: int = 0
    tickets_used: int = 0
    usage: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    ticket_count: int = 0
    open_tickets: int = 0


class RunDetail(RunRead):
    tickets: list[TicketRead] = Field(default_factory=list)
    steps: list[StepRead] = Field(default_factory=list)
    #: 入口智能体与用户的一对一对话；与工单步骤分开保存、在界面上按时间合并展示
    chat_messages: list[ChatMessageRead] = Field(default_factory=list)
    #: 当前是否有一条 CEO 回复正在生成（前端刷新后可据此重新订阅聊天流）
    chat_active: bool = False
