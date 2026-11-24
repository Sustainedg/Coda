"""协作域的数据表。

刻意与对话域（``Conversation`` / ``Message``）分开：协作里的「一次发言」是**工单交付物**，
不是聊天记录 —— 生命周期、状态机、查询方式都不同。两者只共享 `agent_profiles` 这一个既有的领域对象。

外键一律用字符串表名声明，因此本模块不 import ``app.models`` 的实体类，
避免与 ``db.init_db`` 的导入顺序互相纠缠。

注册方式：``db.init_db`` 会 import 本模块，``SQLModel.metadata.create_all`` 才能看到这些表
（有架构守卫测试盯着，见 ``tests/test_collab_architecture.py``）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Column, ForeignKey, JSON, String, Text
from sqlmodel import Field, SQLModel

from ..models import UTCDateTime, new_id, utcnow


#: 协作域**后来新增的可选列**：SQLite 的 ``create_all`` 只建新表、**不会给已存在的表补列**，
#: 而本地库（``backend/data/coda.db``）含真实数据，禁止删库重建 —— 所以在这里登记一次，
#: 由 ``db.init_db`` 幂等补齐（见 ``db._add_missing_columns``）。
#: 只允许追加「带默认值、旧行可以留空」的列；加新列时必须同时登记在这里，否则旧库启动后会报
#: "no such column"。
ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "collab_tickets": {
        "owner_key": "VARCHAR(64) NOT NULL DEFAULT ''",
        "holder_key": "VARCHAR(64) NOT NULL DEFAULT ''",
        "submitter_key": "VARCHAR(64) NOT NULL DEFAULT ''",
        # 上报记录：{from, reason, need, hops}；为空表示这张工单没被上报过
        "escalation": "JSON NOT NULL DEFAULT '{}'",
        # 收尾汇报的时间：根工单每次收尾只汇报一次（用户追问后会重新开启一轮）
        "report_at": "DATETIME",
    },
    "collab_steps": {
        "gist": "VARCHAR(300) NOT NULL DEFAULT ''",
        # 这一步把活派给了哪个节点（delegate 用）；用来在岗位会话里还原「谁把活交给了谁」
        "target_key": "VARCHAR(64) NOT NULL DEFAULT ''",
    },
    # ---- 多用户系统：协作组与协作运行按 owner_id 归属。工单和步骤经 run_id 间接归属，不加。
    "collab_teams": {"owner_id": "VARCHAR(64)"},
    "collab_runs": {
        "owner_id": "VARCHAR(64)",
        # 步骤序号计数器：并行推进时用「UPDATE ... RETURNING」原子取号，避免撞号
        "step_seq": "INTEGER NOT NULL DEFAULT 0",
        # 第一次推进时冻结的编排图快照：之后改协作组不再影响已下工单的运行
        "graph": "JSON",
    },
}


def empty_graph() -> dict[str, Any]:
    """空编排图；作为列默认值必须是可调用的，避免所有行共享同一个 dict。"""
    return {"nodes": [], "edges": []}


class CollabTeam(SQLModel, table=True):
    """协作组：一张可视化编排图（节点 = 智能体在协作中的岗位，边 = 允许的流转）。"""

    __tablename__ = "collab_teams"

    id: str = Field(default_factory=new_id, primary_key=True)
    name: str = Field(sa_column=Column(String(120), nullable=False))
    description: str | None = Field(default=None, sa_column=Column(Text))
    #: 唯一能与用户对话的节点 key（必须存在且 duty=coordinator）
    entry_key: str | None = Field(default=None, sa_column=Column(String(64)))
    #: 团队共享工作目录；为空时按运行 ID 在数据目录下分配
    workspace_dir: str | None = Field(default=None, sa_column=Column(String(500)))
    graph: dict[str, Any] = Field(
        default_factory=empty_graph, sa_column=Column(JSON, nullable=False, default=empty_graph)
    )
    budget: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime))
    #: 所属用户 id；老库由 bootstrap 回填给首任管理员。
    owner_id: str | None = Field(default=None, sa_column=Column(String(64), index=True))


class CollabRun(SQLModel, table=True):
    """一次协作运行（左侧列表里的一项，类比「对话」）。"""

    __tablename__ = "collab_runs"

    id: str = Field(default_factory=new_id, primary_key=True)
    team_id: str = Field(
        sa_column=Column(
            String(64), ForeignKey("collab_teams.id", ondelete="RESTRICT"), nullable=False, index=True
        )
    )
    title: str = Field(default="新协作", sa_column=Column(String(200), nullable=False))
    #: 用户下达的原始目标（入口智能体的第一份输入）
    goal: str = Field(default="", sa_column=Column(Text, nullable=False))
    #: running / waiting_user / done / error / cancelled / interrupted
    status: str = Field(default="running", sa_column=Column(String(24), nullable=False, index=True))
    entry_key: str | None = Field(default=None, sa_column=Column(String(64)))
    workspace_dir: str | None = Field(default=None, sa_column=Column(String(500)))
    budget: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    #: 第一次推进（下发第一张工单）时冻结的编排图快照（``collab_teams.graph`` 的副本）。
    #: 拍下快照后运行就只认自己的图：用户之后改协作组（改 key、删节点、换边）
    #: 都不会打断已有工单。为空表示还没开跑（跟着协作组现图）或升级前的老数据
    #: （引擎回退现图，并在下一次推进时补拍；见 ``engine.prepare_turn`` / ``store.backfill_run_graphs``）。
    graph: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    usage: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    steps_used: int = Field(default=0)
    tickets_used: int = Field(default=0)
    #: 步骤序号计数器（原子取号用，见 ``engine._next_sequence``）；老库由 ADDITIVE_COLUMNS 补 0
    step_seq: int = Field(default=0)
    error: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime))
    #: 所属用户 id；与所属协作组一致，冗余存一份是为了列表查询不必先 join team。
    owner_id: str | None = Field(default=None, sa_column=Column(String(64), index=True))


class CollabTicket(SQLModel, table=True):
    """工单：一次可交接的工作单元（可以有父工单，形成拆解树）。"""

    __tablename__ = "collab_tickets"

    id: str = Field(default_factory=new_id, primary_key=True)
    run_id: str = Field(
        sa_column=Column(
            String(64), ForeignKey("collab_runs.id", ondelete="CASCADE"), nullable=False, index=True
        )
    )
    parent_ticket_id: str | None = Field(
        default=None,
        sa_column=Column(
            String(64), ForeignKey("collab_tickets.id", ondelete="SET NULL"), index=True
        ),
    )
    #: 发起者智能体；由用户发起时为空
    from_agent_profile_id: str | None = Field(
        default=None, sa_column=Column(String(64), ForeignKey("agent_profiles.id", ondelete="RESTRICT"))
    )
    #: 当前承担者智能体
    to_agent_profile_id: str = Field(
        sa_column=Column(String(64), ForeignKey("agent_profiles.id", ondelete="RESTRICT"), nullable=False, index=True)
    )
    #: 发起/汇总该工单的节点 key（子工单完成后回到这里）
    owner_key: str = Field(default="", sa_column=Column(String(64), nullable=False))
    #: 承担者的节点 key（节点可能改绑智能体，这里保留当时的岗位）
    holder_key: str = Field(default="", sa_column=Column(String(64), nullable=False))
    #: 最近一次「提交待审」的节点 key（审查者打回时默认退给它）
    submitter_key: str = Field(default="", sa_column=Column(String(64), nullable=False))
    title: str = Field(sa_column=Column(String(240), nullable=False))
    #: 工单字段（goal/requirements/acceptance/constraints/deliverables）
    brief: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    #: open / working / waiting_children / review / rework / escalated / done / rejected / blocked
    state: str = Field(default="open", sa_column=Column(String(24), nullable=False, index=True))
    #: 已返工轮次
    round: int = Field(default=0)
    max_rounds: int = Field(default=0)
    verdict: str | None = Field(default=None, sa_column=Column(String(24)))
    review_comment: str | None = Field(default=None, sa_column=Column(Text))
    #: 最近一次交付摘要（给下游看的一小段文字 + 产物清单）
    result: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    #: 最近一次上报：{from, reason, need, hops}。非空表示这张工单正被逐级上抛。
    escalation: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime))
    closed_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime))

    #: 把「收尾汇报」交回入口智能体的时间；为空表示还没汇报过。
    #: 根工单每次收尾只汇报一次，用户追问重新开启后会清零（见 ``engine.prepare_turn``）。
    report_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime))


class CollabStep(SQLModel, table=True):
    """协作时间线上的一个条目。

    大多数条目是「某个智能体在一个工单上的一轮发言」；用户自己说的话也存在这里
    （``role_key="user"``、``agent_profile_id`` 为空），这样一次运行的时间线
    只有一张表、天然有序。结构刻意与 ``messages`` 表同构（``content`` + ``parts``），
    前端可以直接用 ``MessageView`` 渲染。
    """

    __tablename__ = "collab_steps"

    id: str = Field(default_factory=new_id, primary_key=True)
    run_id: str = Field(
        sa_column=Column(
            String(64), ForeignKey("collab_runs.id", ondelete="CASCADE"), nullable=False, index=True
        )
    )
    ticket_id: str | None = Field(
        default=None,
        sa_column=Column(
            String(64), ForeignKey("collab_tickets.id", ondelete="SET NULL"), index=True
        ),
    )
    #: 用户自己的发言没有智能体
    agent_profile_id: str | None = Field(
        default=None, sa_column=Column(String(64), ForeignKey("agent_profiles.id", ondelete="RESTRICT"))
    )
    role_key: str = Field(default="", sa_column=Column(String(64), nullable=False))
    duty: str = Field(default="", sa_column=Column(String(24), nullable=False))
    sequence: int = Field(default=0)
    #: 解析出的动作；解析失败时存空串，并由 error 字段说明原因
    action: str = Field(default="", sa_column=Column(String(24), nullable=False))
    #: delegate 时的目标节点 key（即这一步把活交给了谁）；其他动作为空串
    target_key: str = Field(default="", sa_column=Column(String(64), nullable=False, default=""))
    #: streaming / complete / error / cancelled / interrupted
    status: str = Field(default="streaming", sa_column=Column(String(16), nullable=False, index=True))
    content: str = Field(default="", sa_column=Column(Text, nullable=False))
    #: 由引擎从工单（结构化字段）派生出的一句话摘要 —— 交给下游时只看它，不看 content
    gist: str = Field(default="", sa_column=Column(String(300), nullable=False))
    parts: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False, default=list)
    )
    usage: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    error: str | None = Field(default=None, sa_column=Column(Text))
    #: 越权审计结果（只读节点运行前后共享目录的指纹对比）
    audit: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    started_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime))
    finished_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime))


class CollabMessage(SQLModel, table=True):
    """入口智能体与用户的一对一对话（CEO 常驻联络通道）。

    这是协作域里**唯一**允许携带历史原文的地方：它只面向入口智能体，用于细化要求、
    询问进度和日常沟通；工单步骤的 prompt 永远不读取这张表。单独建表而不是塞进
    ``collab_steps``，是为了不让聊天污染工单时间线、步骤计数与“谁把活交给了谁”的交接链。
    """

    __tablename__ = "collab_messages"

    id: str = Field(default_factory=new_id, primary_key=True)
    run_id: str = Field(
        sa_column=Column(
            String(64), ForeignKey("collab_runs.id", ondelete="CASCADE"), nullable=False, index=True
        )
    )
    #: user = 用户本人；leader = 入口智能体。没有其他发送者。
    sender: str = Field(default="user", sa_column=Column(String(16), nullable=False, index=True))
    #: 入口智能体岗位 key（用户发言也记下当时的入口，便于界面显示）
    role_key: str = Field(default="", sa_column=Column(String(64), nullable=False))
    agent_profile_id: str | None = Field(
        default=None, sa_column=Column(String(64), ForeignKey("agent_profiles.id", ondelete="RESTRICT"))
    )
    sequence: int = Field(default=0)
    #: streaming / complete / error / cancelled
    status: str = Field(default="complete", sa_column=Column(String(16), nullable=False, index=True))
    content: str = Field(default="", sa_column=Column(Text, nullable=False))
    parts: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False, default=list)
    )
    usage: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    error: str | None = Field(default=None, sa_column=Column(Text))
    #: 结构化伴随信息：本轮识别到的需求更新、同步到的工单、解析告警等
    meta: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime))
    finished_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime))
