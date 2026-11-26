"""数据库结构。

产品的核心概念：

* :class:`Gateway`      —— 网关：一个 OpenAI / Anthropic / Gemini 兼容的端点。
  分「自定义网关」与「官方链路」两类，**是 API 密钥的唯一来源**。
* :class:`Credential`   —— 凭据：工具/服务要用的第三方密钥（全局一份，可被多个智能体引用）。
* :class:`AgentProfile` —— 智能体：人格 + 内核 + 网关 + 默认模型。
  用户配置与使用的最小单位（原「角色 + 智能体实例」合并而来）。
* :class:`Conversation` —— 对话，绑定某个智能体并记住内核侧的会话 ID。
* :class:`Skill`        —— 从各智能体内核扫描出来的技能（统一编目）。
* :class:`AgentSkill`   —— 智能体与技能的分配关系（智能体能用的技能只能来自这里）。
* :class:`McpServer`    —— 统一编目的 MCP 服务器（平台新建的「通用」服务器 + 从各内核配置扫出来的）。
* :class:`AgentMcpServer` —— 智能体与 MCP 服务器的分配关系（智能体能挂的 MCP 只能来自这里）。
* :class:`RuntimeStatus` —— 各运行时的可用性检测结果（框架层缓存，不是领域实体）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.types import TypeDecorator
from sqlmodel import Field, SQLModel


#: **后来新增的可选列**：SQLite 的 ``create_all`` 只建新表、不会给已存在的表补列，
#: 所以在这里登记一次，由 ``db.init_db`` 幂等补齐（见 ``db._add_missing_columns``）。
#: 只允许追加「带默认值、旧行可以留空」的列。
ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    # 「默认智能体」这一列是后加的：老行一律按「不是默认」补上，由 bootstrap 在启动时补一颗星
    # 「分组名」也是后加的：老行一律按空串补上（智能体页把空串归到「未分组」）
    # owner_id 是多用户系统后加的：老行留空，由 bootstrap 回填给首任管理员
    "agent_profiles": {
        "is_default": "BOOLEAN NOT NULL DEFAULT 0",
        "group_name": "VARCHAR(60) NOT NULL DEFAULT ''",
        "owner_id": "VARCHAR(64)",
        # 「智能体自定义头像文件」是后加的：老行留空，界面回退到内置头像包。
        "avatar_file": "VARCHAR(120)",
        # 「内置头像包编号」是后加的：老行留空，界面按 id 稳定兜底一张头像包图。
        "avatar_preset": "VARCHAR(32)",
        # 「指定内核账号槽位」是后加的：老行留空 = 跟随内核当前登录的账号。
        "account_id": "VARCHAR(64)",
    },
    # 「技能市场页地址」这一列是后加的：老行一律按空串补上（来源仍复用已有的 source_kind）。
    # owner_id 是「按用户隔离技能库」后加的：老行留空 = 全平台共享（内核自带 / 宿主机技能）；
    # 平台导入、市场安装的技能都属于某个用户，按 owner_id 隔离。
    "skills": {
        "source_url": "VARCHAR(500) NOT NULL DEFAULT ''",
        "owner_id": "VARCHAR(64)",
    },
    # 「这条对话单独指定的模型」是后加的；为空表示跟随智能体的默认模型。
    # 老行留空即可，不需要回填。
    # 「最后已读时间」是后加的；为空表示升级前的旧对话，一律当已读（否则升级后满屏未读）。
    # owner_id 是多用户系统后加的：老行留空，由 bootstrap 回填给首任管理员
    # 上下文自动压缩是后加的：老行没有摘要、折叠条数为 0（= 整段历史都还在上下文里）。
    "conversations": {
        "model": "VARCHAR(160)",
        "last_read_at": "DATETIME",
        "owner_id": "VARCHAR(64)",
        "context_summary": "TEXT",
        "context_compacted_count": "INTEGER NOT NULL DEFAULT 0",
        "context_compacted_at": "DATETIME",
        # 「内核侧原生压缩的时间」是后加的（Claude Code auto-compact 等）：
        # 老行留空 = 内核从未压缩过。用于让压缩前的 context_tokens 快照失效。
        "context_kernel_compacted_at": "DATETIME",
        # 「内核上报的真实上下文窗口」是后加的：平台上限设为「自动」时跟随它。
        # 老行留空 = 内核没报过，回退到用户配置 / 默认值。
        "context_kernel_window_tokens": "INTEGER",
    },
    # 会话令牌版本：改口令 / 管理员重置口令时 +1，让旧令牌立即失效（无状态 JWT 的吊销机制）。
    # 头像文件名是后加的：老行留空，界面回退到「显示名首字母」的渐变圆。
    # 个人访问令牌（PAT）是后加的：哈希与签发时间，老行留空表示还没生成过。
    "users": {
        "token_version": "INTEGER NOT NULL DEFAULT 0",
        "avatar_file": "VARCHAR(120)",
        "pat_hash": "VARCHAR(64)",
        "pat_issued_at": "DATETIME",
        # 系统管理 MCP 专用的长效令牌：与用户自己在「账号」页生成的 PAT 分开，
        # 避免自动配置时轮换掉用户已有的令牌。只存哈希。
        "builtin_mcp_pat_hash": "VARCHAR(64)",
        "builtin_mcp_pat_issued_at": "DATETIME",
        # 轨迹调试开关：用户级、默认关闭；老行补列后一律 False（采集默认不开启）。
        "trace_debug": "BOOLEAN NOT NULL DEFAULT 0",
    },
    # ---- 多用户系统：每张「用户资产」表加 owner_id（可空，老库由 bootstrap 回填给首任管理员）。
    # 运行时状态（runtime_status）、平台设置（platform_settings）是平台级共享数据，不加 owner_id。
    # 网关含 API 密钥，按用户私有；管理员可看全部。
    "gateways": {"owner_id": "VARCHAR(64)"},
    "credentials": {"owner_id": "VARCHAR(64)"},
    # builtin_key 是「平台内置服务器」后加的稳定标识（例如 image-gen）；普通服务器为空。
    # 老行留空即可，不需要回填；内置服务器由启动播种逐用户补齐。
    "mcp_servers": {"owner_id": "VARCHAR(64)", "builtin_key": "VARCHAR(64)"},
    # 「这条回复由哪个智能体产出」是后加的：为空 = 对话归属的智能体（主场）；
    # 非空 = 用户 @ 召唤的智能体。老行留空即可，不需要回填。
    # 「最近一次请求的上下文 token 数」是后加的：会话型内核自己维护上下文，平台看不到
    # 其内部历史，只能由适配器上报。老行留空 = 没有快照，回退到按内容估算。
    # context_snapshot_at 是快照的**采集时刻**（不是消息创建时刻）：内核原生压缩可能发生在
    # 一轮中间，判断「快照是否早于压缩」必须比采集时刻，比 created_at 会把压缩后新上报的
    # 快照误判成压缩前的旧快照。老行留空 = 回退到 created_at（升级前的旧数据没有更好信息）。
    "messages": {
        "agent_profile_id": "VARCHAR(64)",
        "context_tokens": "INTEGER",
        "context_snapshot_at": "DATETIME",
    },
}


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """统一按 UTC 存取的 ``datetime`` 列。

    SQLite 没有原生时区类型：``DateTime(timezone=True)`` 在 SQLite 下**不会**保留
    ``tzinfo``，取出来是「裸 UTC 墙钟时间」。Pydantic 序列化裸时间时不带偏移量
    （``2026-11-18T15:45:41`` 而不是 ``...+00:00``），浏览器会按**本地时区**解析 ——
    东八区用户看到的时间整整少 8 小时。

    所以在这一层统一兜底：

    * **入库**：带时区的值先换算到 UTC 再抹掉 ``tzinfo``（库里的存储格式与历史数据完全一致，
      仍是 ``YYYY-MM-DD HH:MM:SS.ffffff`` 的裸 UTC，不需要迁移）；
    * **出库**：补回 ``tzinfo=UTC``，于是 ``isoformat()`` 一定带 ``+00:00``，
      前端 ``new Date(...)`` 能正确换算到用户本地时间。

    历史数据（本来就是裸 UTC）出库时同样被补上 UTC，无需回填。
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # 裸时间按「已经是 UTC」处理：调用方忘了带时区时也绝不能当成本地时间存进去。
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class Gateway(SQLModel, table=True):
    """一个模型服务端点（自定义网关或内核官方链路）。

    密钥只存在这里：智能体不再持有密钥，运行时从网关解密后注入 ``AgentContext``。
    ``kind == "official"`` 的网关由启动播种按适配器声明生成（``type_id`` 记录它属于哪个内核）；
    ``kind == "custom"`` 的网关由用户在设置页创建，``type_id`` 为空。
    """

    __tablename__ = "gateways"

    id: str = Field(default_factory=new_id, primary_key=True)
    name: str = Field(sa_column=Column(String(120), nullable=False))
    #: 取值：official（内核官方链路）/ custom（用户自建）
    kind: str = Field(default="custom", sa_column=Column(String(16), nullable=False))
    #: official 网关绑定的内核 id；custom 网关为空
    type_id: str | None = Field(default=None, sa_column=Column(String(64), index=True))
    #: 取值：openai / anthropic / gemini —— 决定探测模型时的请求形状
    protocol: str = Field(default="openai", sa_column=Column(String(32), nullable=False))
    #: 取值：bearer / x-api-key / query / none
    auth_style: str = Field(default="bearer", sa_column=Column(String(32), nullable=False))
    base_url: str = Field(sa_column=Column(String(500), nullable=False))
    api_key_encrypted: str | None = Field(default=None, sa_column=Column(Text))
    #: 用户勾选的模型白名单：智能体只能从这里选模型
    selected_models: list[str] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False, default=list)
    )
    #: 最近一次「获取模型」的完整结果（重新探测不会冲掉白名单）
    model_catalog: list[str] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False, default=list)
    )
    extra_headers: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    last_probed_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime))
    enabled: bool = Field(default=True)
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime))
    #: 软删除时间戳：仍被未删除的智能体引用时只打标记；历史对话照旧可解析到它。
    deleted_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime))
    #: 所属用户 id；老库由 bootstrap 回填给首任管理员。官方网关（kind=official）也归属到
    #: 创建它的那次播种的用户，但所有用户都能选用官方链路（见 api/gateways 的过滤）。
    owner_id: str | None = Field(default=None, sa_column=Column(String(64), index=True))


class AgentProfile(SQLModel, table=True):
    """一个配置好的智能体：人格 + 内核 + 网关 + 默认模型 + 适配器选项 + 凭据。"""

    __tablename__ = "agent_profiles"

    id: str = Field(default_factory=new_id, primary_key=True)
    name: str = Field(sa_column=Column(String(120), nullable=False))
    #: 老的 emoji 头像字段（**已下线**）：只为兼容历史数据保留，界面不再展示 / 可编辑。
    avatar: str = Field(default="", sa_column=Column(String(16), nullable=False))
    #: 内置头像包编号（``pack-001`` ~ ``pack-999``，实际清单见前端 `lib/avatarPack.ts`）：
    #: 随机生成 / 快选从这里选，前端渲染成 ``/avatars/pack-001.webp``。与 ``avatar_file``
    #: （自定义上传）并存，显示优先级：自定义上传 > 内置头像包 > 按 id 稳定兜底的头像包。
    avatar_preset: str | None = Field(default=None, sa_column=Column(String(32)))
    #: 自定义头像文件名（``<智能体id>-<内容摘要>.<png|jpg|webp>``，文件在 ``<数据目录>/avatars/``，
    #: 与用户头像同一个目录与校验管线）。有值时界面优先显示它，否则回退到内置头像包。
    avatar_file: str | None = Field(default=None, sa_column=Column(String(120)))
    #: 一句话简介（列表卡片用）
    tagline: str | None = Field(default=None, sa_column=Column(String(200)))
    #: 备注
    description: str | None = Field(default=None, sa_column=Column(Text))
    system_prompt: str = Field(default="", sa_column=Column(Text, nullable=False))
    #: 适配器 id（claude-code / codex / antigravity-cli / deepseek-harness / openai-harness / custom-cli …）
    type_id: str = Field(sa_column=Column(String(64), nullable=False, index=True))
    #: 使用的自定义网关；为空（或指向官方链路）表示「官方默认」——平台不接管路由，
    #: 运行时回落到适配器读到的本机配置（见 ``runtime/gateway.py:bind_gateway``）
    gateway_id: str | None = Field(
        default=None,
        sa_column=Column(String(64), ForeignKey("gateways.id", ondelete="RESTRICT"), index=True),
    )
    model: str | None = Field(default=None, sa_column=Column(String(160)))
    #: 多账号内核的账号槽位 id（见 ``AgentDefinition.activate_account``）。
    #: 为空 = 跟随内核当前登录的账号；「官方默认」链路上运行时按它切换一次登录态。
    #: 自定义网关不依赖内核登录态，这一列只在官方链路下取值。
    account_id: str | None = Field(default=None, sa_column=Column(String(64)))
    #: 适配器声明的可配置项（键值对，形状由 ConfigField 决定）
    options: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    enabled: bool = Field(default=True)
    #: 默认智能体：新建对话不再先问「选谁」，直接用这颗星标着的那个（全局最多一个）。
    #: 老库由 `bootstrap.ensure_default_agent` 在启动时补一次；换星只走 `api/agents`。
    is_default: bool = Field(default=False)
    #: 分组名：智能体页按它折叠分组展示（空串 = 未分组，永远排在最后）。
    group_name: str = Field(default="", sa_column=Column(String(60), nullable=False))
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime))
    #: 软删除时间戳：删除智能体时只打标记（历史对话仍引用它）。打了标记的智能体从所有
    #: 可选列表里消失，但历史对话仍能显示当时的名字与头像、也仍可继续。
    deleted_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime))
    #: 所属用户 id；老库由 bootstrap 回填给首任管理员。
    owner_id: str | None = Field(default=None, sa_column=Column(String(64), index=True))


class Skill(SQLModel, table=True):
    """统一编目的技能。

    * 内核自带 / 宿主机上已有的技能：``owner_id`` 为空，全平台共享、只读；
    * 用户自己的技能库（导入、市场安装、挂载到其它内核）**按 slug 只存一份**：
      ``<数据目录>/skill-libraries/<owner_id>/<slug>``，按 ``owner_id`` 隔离；被哪些内核
      引用登记在同级 ``.mounts.json``，一行对应一个引用它的内核，所有行 ``path`` 指向同一实体。

    ``id`` 是稳定标识：共享技能为 ``<type_id>:<slug>``，用户技能为
    ``<type_id>:lib:<owner_id 或 local>:<slug>``（同一 slug 在不同用户下互不冲突）。
    技能目录**确实消失**时重扫会解除 ``agent_skills`` 分配并删掉这一行；只有「目录还在、
    这一轮没扫到」才把 ``available`` 置为 False（保住分配），等下一轮恢复。技能根
    （父目录）本身也不在时按「这一轮无法判断」处理，同样只标失效、不动分配。
    """

    __tablename__ = "skills"

    id: str = Field(sa_column=Column(String(260), primary_key=True))
    type_id: str = Field(sa_column=Column(String(64), nullable=False, index=True))
    slug: str = Field(sa_column=Column(String(120), nullable=False))
    name: str = Field(sa_column=Column(String(160), nullable=False))
    description: str = Field(default="", sa_column=Column(Text, nullable=False))
    source_kind: str = Field(default="user", sa_column=Column(String(32), nullable=False))
    path: str = Field(default="", sa_column=Column(String(1000), nullable=False))
    fingerprint: str = Field(default="", sa_column=Column(String(64), nullable=False))
    version: str | None = Field(default=None, sa_column=Column(String(80)))
    author: str | None = Field(default=None, sa_column=Column(String(160)))
    when_to_use: str | None = Field(default=None, sa_column=Column(Text))
    allowed_tools: list[str] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False, default=list)
    )
    files: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False, default=list)
    )
    total_bytes: int = Field(default=0)
    meta: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    available: bool = Field(default=True)
    note: str | None = Field(default=None, sa_column=Column(String(400)))
    #: 来源是「技能市场」时，市场技能页地址；其余来源为空
    source_url: str = Field(default="", sa_column=Column(String(500), nullable=False))
    #: 所属用户 id：用户技能库里的技能按它隔离；为空表示内核自带 / 宿主机共享技能。
    owner_id: str | None = Field(default=None, sa_column=Column(String(64), index=True))
    discovered_at: datetime = Field(
        default_factory=utcnow, sa_column=Column(UTCDateTime)
    )
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime))


class AgentSkill(SQLModel, table=True):
    """智能体 → 技能的分配关系。智能体实际可用的技能只能来自这张表。"""

    __tablename__ = "agent_skills"

    agent_profile_id: str = Field(
        sa_column=Column(
            String(64), ForeignKey("agent_profiles.id", ondelete="CASCADE"), primary_key=True
        )
    )
    skill_id: str = Field(
        sa_column=Column(
            String(260), ForeignKey("skills.id", ondelete="RESTRICT"), primary_key=True
        )
    )
    position: int = Field(default=0)


class McpServer(SQLModel, table=True):
    """统一编目的一个 MCP 服务器。

    两类来源：

    * ``type_id is None``（``source_kind == "manual"``）—— 用户在「MCP」页新建的**平台通用**
      服务器，可以分配给任意内核的智能体；
    * ``type_id`` 非空 —— 从某个内核自己的配置里只读扫描出来的（来源是那个内核），
      只能分配给同内核的智能体，且不可在平台上编辑 / 删除。

    ``env`` / ``headers`` 的值可能含密钥，只经 :mod:`app.security` 加密保存
    （明文键有且只有一层：接口永远只回键名与掩码）。
    """

    __tablename__ = "mcp_servers"

    id: str = Field(sa_column=Column(String(200), primary_key=True))
    name: str = Field(sa_column=Column(String(160), nullable=False))
    #: 稳定标识：写进内核配置的键名（扫描来的就是配置里的原始键名）
    slug: str = Field(sa_column=Column(String(120), nullable=False))
    description: str = Field(default="", sa_column=Column(Text, nullable=False))
    #: 来源内核 id；None 表示平台通用（可分配给任意内核）
    type_id: str | None = Field(default=None, sa_column=Column(String(64), index=True))
    #: 取值：manual（平台新建）/ user（内核配置扫描）
    source_kind: str = Field(default="manual", sa_column=Column(String(32), nullable=False))
    #: 取值：stdio / http / sse
    transport: str = Field(default="stdio", sa_column=Column(String(16), nullable=False))
    command: str | None = Field(default=None, sa_column=Column(String(500)))
    args: list[str] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False, default=list)
    )
    url: str | None = Field(default=None, sa_column=Column(String(1000)))
    #: 「环境变量名 → 密文」。键明文、值加密；运行时解密后按内核形状写出
    env_encrypted: dict[str, str] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    #: 「请求头名 → 密文」，同 env
    headers_encrypted: dict[str, str] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    enabled: bool = Field(default=True)
    #: 扫描来的服务器在配置里是否仍存在；平台新建的恒为 True
    available: bool = Field(default=True)
    #: 平台内置服务器的稳定标识（例如 image-gen / coda-admin）；普通服务器为空。
    #: 内置服务器的启动命令 / 脚本参数由 ``app.builtin_mcp`` 声明、启动时自动维护，
    #: 用户只需要填写参数（平台地址、令牌、网关地址、模型名……）。
    builtin_key: str | None = Field(default=None, sa_column=Column(String(64), index=True))
    note: str | None = Field(default=None, sa_column=Column(String(400)))
    discovered_at: datetime = Field(
        default_factory=utcnow, sa_column=Column(UTCDateTime)
    )
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime))
    #: 软删除时间戳：仅平台新建的服务器可删；历史对话与协作历史照旧可读
    deleted_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime))
    #: 所属用户 id；扫描来的（source_kind=user）也归属到那次扫描的用户。老库回填给首任管理员。
    owner_id: str | None = Field(default=None, sa_column=Column(String(64), index=True))


class AgentMcpServer(SQLModel, table=True):
    """智能体 → MCP 服务器的分配关系。智能体实际能挂的 MCP 只能来自这张表。"""

    __tablename__ = "agent_mcp_servers"

    agent_profile_id: str = Field(
        sa_column=Column(
            String(64), ForeignKey("agent_profiles.id", ondelete="CASCADE"), primary_key=True
        )
    )
    mcp_server_id: str = Field(
        sa_column=Column(
            String(200), ForeignKey("mcp_servers.id", ondelete="RESTRICT"), primary_key=True
        )
    )
    position: int = Field(default=0)


class Credential(SQLModel, table=True):
    """一条凭据：智能体干活要用的第三方密钥（GitHub token、数据库口令…）。

    与 :class:`Gateway` 的密钥不是一回事：网关密钥属于「模型提供方」，凭据属于
    「工具 / 服务」，是**全局一份**、可被多个智能体引用（``agent_credentials``）。
    值只经 :mod:`app.security` 加密保存，运行时解密后按环境变量注入（见 ``app.credentials``），
    接口永远只回掩码。
    """

    __tablename__ = "credentials"

    id: str = Field(default_factory=new_id, primary_key=True)
    #: 同时就是要注入的环境变量名（``^[A-Za-z_][A-Za-z0-9_]{0,63}$``）
    name: str = Field(sa_column=Column(String(64), nullable=False, index=True))
    #: 用途说明（给人和模型看的「名字清单」用）
    description: str | None = Field(default=None, sa_column=Column(String(200)))
    value_encrypted: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime))
    #: 软删除：仍被未删除的智能体引用时只打标记（历史运行照旧可解析）
    deleted_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime))
    #: 所属用户 id；老库由 bootstrap 回填给首任管理员。
    owner_id: str | None = Field(default=None, sa_column=Column(String(64), index=True))


class AgentCredential(SQLModel, table=True):
    """智能体 → 凭据的引用关系。智能体运行时能拿到的凭据只能来自这张表。"""

    __tablename__ = "agent_credentials"

    agent_profile_id: str = Field(
        sa_column=Column(
            String(64), ForeignKey("agent_profiles.id", ondelete="CASCADE"), primary_key=True
        )
    )
    credential_id: str = Field(
        sa_column=Column(
            String(64), ForeignKey("credentials.id", ondelete="RESTRICT"), primary_key=True
        )
    )
    position: int = Field(default=0)


class Conversation(SQLModel, table=True):
    __tablename__ = "conversations"

    id: str = Field(default_factory=new_id, primary_key=True)
    title: str = Field(default="新对话", sa_column=Column(String(200), nullable=False))
    #: 创建时对智能体做的快照；历史对话必须可复现
    agent_profile_id: str = Field(
        sa_column=Column(
            String(64), ForeignKey("agent_profiles.id", ondelete="RESTRICT"), nullable=False, index=True
        )
    )
    session_id: str | None = Field(default=None, sa_column=Column(String(160)))
    workspace_dir: str | None = Field(default=None, sa_column=Column(String(500)))
    #: 这条对话单独指定的模型；为空表示跟随智能体的默认模型（换模型不影响会话记忆）
    model: str | None = Field(default=None, sa_column=Column(String(160)))
    archived: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime))
    #: 用户最后一次看这条对话的时间；列表据此判断「AI 回复完了但还没读」。
    #: 为空表示升级前的旧对话 —— 一律当已读，避免升级后所有历史对话都亮未读。
    last_read_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime))
    #: 上下文自动压缩：更早消息折叠成的摘要（原文仍完整保留在 messages 表里）。
    context_summary: str | None = Field(default=None, sa_column=Column(Text))
    #: 已折叠进摘要的消息条数（按 user / assistant 有正文的消息计数）。
    #: 0 = 还没有压缩过；历史消息一条都不删，靠这个偏移量跳过它们。
    context_compacted_count: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, default=0)
    )
    #: 最近一次压缩的时间（界面展示用）。
    context_compacted_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime))
    #: 内核侧原生压缩（如 Claude Code auto-compact）最近一次发生的时间。
    #: 与 :attr:`context_compacted_at` 分开记：平台压缩会重开会话（快照随 session_id
    #: 失效），内核压缩不动会话，必须靠这个时间戳让压缩前的 context_tokens 快照失效。
    context_kernel_compacted_at: datetime | None = Field(
        default=None, sa_column=Column(UTCDateTime)
    )
    #: 内核上报的**真实上下文窗口**（tokens）：Claude Code 的
    #: ``autocompact_state.effective_window``、Codex 的 ``tokenUsage.modelContextWindow``。
    #: 平台上限设为「自动」时跟随它（见 ``runtime.context.ContextSettings.for_window``）；
    #: 为空 = 该内核没报过，回退到用户配置 / 默认值。
    context_kernel_window_tokens: int | None = Field(
        default=None, sa_column=Column(Integer)
    )
    #: 所属用户 id；老库由 bootstrap 回填给首任管理员。
    owner_id: str | None = Field(default=None, sa_column=Column(String(64), index=True))


class Message(SQLModel, table=True):
    __tablename__ = "messages"

    id: str = Field(default_factory=new_id, primary_key=True)
    conversation_id: str = Field(sa_column=Column(String(64), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True))
    role: str = Field(sa_column=Column(String(16), nullable=False))  # 取值：user / assistant / system
    content: str = Field(default="", sa_column=Column(Text, nullable=False))
    parts: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON, nullable=False, default=list))
    usage: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict))
    status: str = Field(default="complete", sa_column=Column(String(16), nullable=False))
    error: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime, index=True))
    #: 这条回复实际由哪个智能体产出。为空 = 对话归属的智能体（主场）；
    #: 非空 = 用户在对话里 @ 召唤的智能体（只回答这一轮，不改变对话归属）。
    agent_profile_id: str | None = Field(default=None, sa_column=Column(String(64)))
    #: 本轮**最后一次模型请求**的输入 token 数（上下文窗口真实占用）。
    #: 由适配器通过 ``context.usage`` 事件上报；为空 = 没有快照，按内容估算。
    context_tokens: int | None = Field(default=None, sa_column=Column(Integer))
    #: 上面那个快照的**采集时刻**（UTC）。``created_at`` 是消息开始的时间，而快照产生于
    #: 本轮中间某次 API 调用——内核原生压缩后上报的 ``post_tokens`` 就发生在 ``created_at``
    #: 之后。失效判定必须比这个时间，否则压缩后的新快照会被自己的失效线误杀。
    #: 为空 = 升级前的旧行，判定时回退到 ``created_at``。
    context_snapshot_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime))


class TaskCard(SQLModel, table=True):
    """一条对话的「任务进度卡片」（一个对话最多一张）。

    卡片是**对话级对象**，不是消息的一部分：消息是不可变的历史原文，进度会反复变化，
    写回旧消息既语义错误、也会和消息落库路径抢同一行。任务项存 JSON 列，
    ``conversation_id`` 唯一约束从根上杜绝同一对话出现多张卡。

    卡片没有任何客户端写入口：唯一写入方是运行收尾时对模型输出的解析（见
    ``runtime/task_cards.py``）。界面只读，勾选状态只能由服务端按模型声明变更。
    """

    __tablename__ = "task_cards"

    id: str = Field(default_factory=new_id, primary_key=True)
    #: 所属对话：唯一 + 级联删除（删对话时卡片随之消失，不留孤儿）
    conversation_id: str = Field(
        sa_column=Column(
            String(64),
            ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
            index=True,
        )
    )
    #: 建卡那一轮的助手消息 id：决定卡片在对话流中的位置（重规划时更新到最新一轮）
    anchor_message_id: str | None = Field(default=None, sa_column=Column(String(64), index=True))
    #: 卡片标题；为空时界面显示「任务进度」
    title: str = Field(default="", sa_column=Column(String(200), nullable=False, default=""))
    #: 任务项数组：``{id, text, done, done_at, updated_at}``
    items: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False, default=list)
    )
    #: 乐观锁版本号：每次成功写入 +1，前端据此判断要不要重渲染
    version: int = Field(default=1, sa_column=Column(Integer, nullable=False, default=1))
    #: 最近一次写入它的运行 id：同一运行重复应用时幂等跳过
    last_run_id: str | None = Field(default=None, sa_column=Column(String(64)))
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime))


class TraceEvent(SQLModel, table=True):
    """一轮对话的调试轨迹（旁路采集，不参与消息表与业务逻辑）。

    ``conversation_id`` 指向平台对话（``Conversation.id``），**不是**内核侧的
    ``Conversation.session_id``；内核会话 id 记录在 ``context_snapshot.payload`` 的
    ``kernel_session_id`` 里。轨迹写入来自独立会话，任何采集失败都不得影响正常回复。
    """

    __tablename__ = "trace_events"
    __table_args__ = (
        # 时间线读取按 (conversation_id, created_at)；单轮查看按 (conversation_id, run_id)。
        Index("ix_trace_events_conv_created", "conversation_id", "created_at"),
        Index("ix_trace_events_conv_run", "conversation_id", "run_id"),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    conversation_id: str = Field(
        sa_column=Column(
            String(64), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
        )
    )
    owner_id: str | None = Field(default=None, sa_column=Column(String(64), index=True))
    run_id: str | None = Field(default=None, sa_column=Column(String(64), index=True))
    #: 该对话第几轮用户消息（0 起），不写进 Message 表
    turn_index: int = Field(default=0, sa_column=Column(Integer, nullable=False, default=0))
    #: 同一会话内严格递增（跨轮也不回退），时间线排序与 after_seq 分页的稳定键
    seq: int = Field(default=0, sa_column=Column(Integer, nullable=False, default=0))
    type: str = Field(sa_column=Column(String(24), nullable=False, index=True))
    payload: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    duration_ms: int | None = Field(default=None, sa_column=Column(Integer))
    error: str | None = Field(default=None, sa_column=Column(Text))
    truncated: bool = Field(default=False, sa_column=Column(Boolean, nullable=False, default=False))
    #: payload 序列化后的 UTF-8 字节数（截断前），用于单会话体积裁剪
    bytes: int = Field(default=0, sa_column=Column(Integer, nullable=False, default=0))
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime, index=True))


class RuntimeStatus(SQLModel, table=True):
    """某个运行时（CLI 内核）在本机的一次可用性检测结果。

    这是框架层的**缓存**，不是领域实体：内核装没装、是什么版本，只有跑一次
    ``内核 --version`` 才知道，而 Node 系 CLI 的冷启动接近 2 秒 —— 这种同步子进程
    放在请求路径上会冻住整个事件循环（详见 ``runtime/availability.py``）。
    因此由框架定时检测后写在这里，接口只读这张表。``type_id`` 就是适配器的 id。
    """

    __tablename__ = "runtime_status"

    type_id: str = Field(sa_column=Column(String(64), primary_key=True))
    installed: bool = Field(default=False, sa_column=Column(Boolean, nullable=False, default=False))
    version: str | None = Field(default=None, sa_column=Column(String(160)))
    #: 已安装时是可执行文件路径，未安装时是原因（与 ``Availability.detail`` 同义）。
    detail: str | None = Field(default=None, sa_column=Column(Text))
    #: 本次检测的时间（UTC）。超过 ``runtime.availability.CHECK_INTERVAL`` 即视为过期。
    checked_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime))


class PlatformSetting(SQLModel, table=True):
    """平台级设置的键值存储（代理地址等）。

    与网关 / 凭据不同：这里存的是平台自身的行为参数，不绑定某个智能体。
    代理地址要注入子进程环境变量，必须以明文取出，因此不加密——
    代理地址不是 API 密钥，泄露风险低。
    """

    __tablename__ = "platform_settings"

    key: str = Field(sa_column=Column(String(64), primary_key=True))
    value: str | None = Field(default=None, sa_column=Column(Text))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime))


class User(SQLModel, table=True):
    """一个注册用户。

    多用户系统的核心实体：智能体、对话、网关、凭据、MCP 服务器、协作组都按 ``owner_id``
    归属到某个用户。口令只存哈希（见 :mod:`app.auth`），永不存明文。

    首任管理员由 ``bootstrap.seed_initial_admin`` 在启动时按环境变量
    ``CODA_ADMIN_USERNAME`` / ``CODA_ADMIN_PASSWORD`` 创建；老库（原本单用户、
    所有数据无归属）升级时，存量数据会被回填给这个首任管理员。
    """

    __tablename__ = "users"

    id: str = Field(default_factory=new_id, primary_key=True)
    #: 登录名，唯一（大小写敏感，注册时校验唯一性）
    username: str = Field(sa_column=Column(String(60), nullable=False, unique=True, index=True))
    #: 显示名（给界面用）；为空时回退到 username
    display_name: str | None = Field(default=None, sa_column=Column(String(120)))
    #: 邮箱（可选，预留：将来发通知 / 找回口令用）
    email: str | None = Field(default=None, sa_column=Column(String(200)))
    #: 口令哈希（``pbkdf2$<迭代>$<盐>$<哈希>``，见 :mod:`app.auth`）；永不存明文
    password_hash: str = Field(sa_column=Column(String(200), nullable=False))
    #: 会话令牌版本：改口令 / 重置口令时 +1，旧 JWT 立即失效（无状态会话的吊销机制）
    token_version: int = Field(default=0, sa_column=Column(Integer, nullable=False, default=0))
    #: 角色：admin（可管理用户、可看全部用户的网关）/ member（只能看自己的）
    role: str = Field(default="member", sa_column=Column(String(16), nullable=False))
    #: 是否启用；禁用的用户无法登录、其历史数据照旧可读
    is_active: bool = Field(default=True, sa_column=Column(Boolean, nullable=False, default=True))
    #: 头像文件名（``<用户id>-<内容摘要>.<png|jpg|webp>``，文件在 ``<数据目录>/avatars/``）。
    #: 为空表示没上传过，界面回退到「显示名首字母」的渐变圆。
    avatar_file: str | None = Field(default=None, sa_column=Column(String(120)))
    #: 个人访问令牌（PAT）的哈希：给 MCP 服务器等程序化调用用的长效凭据。
    #: 只存 sha256 哈希（明文仅在签发时返回一次，平台不再能看到）；为空表示还没生成过。
    pat_hash: str | None = Field(default=None, sa_column=Column(String(64)))
    #: PAT 的签发时间（给界面展示「生成于 …」用）；为空表示还没生成过。
    pat_issued_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime))
    #: 系统管理 MCP 专用的 PAT 哈希：平台自动生成并写进该用户的内置 MCP 配置，
    #: 与用户手动生成的 ``pat_hash`` 互不影响。只存哈希，明文只在配置里加密保存。
    builtin_mcp_pat_hash: str | None = Field(default=None, sa_column=Column(String(64)))
    #: 系统管理 MCP 专用 PAT 的签发时间。
    builtin_mcp_pat_issued_at: datetime | None = Field(
        default=None, sa_column=Column(UTCDateTime)
    )
    #: 轨迹调试开关（用户级，默认关闭）。只有它为 True 时运行层才采集轨迹；
    #: 判定点唯一在 ``runtime.service.prepare_run`` / ``regenerate_last``。
    trace_debug: bool = Field(default=False, sa_column=Column(Boolean, nullable=False, default=False))
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime))
