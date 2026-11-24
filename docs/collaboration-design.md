# 协作（多智能体工单协作）设计方案

> 状态：**方案已评审通过，正在按 §11 里程碑落地**（进度见 §13）。本文档描述的是完整设计，
> M0（契约冻结）、M1（引擎 MVP：后端跑通含返工的闭环）、M2（画布编排 + 协作页）已落地，
> 未实现的部分在表格里标了里程碑。
> 相关约束：`AGENTS.md`（铁律与分层）、`docs/architecture.md`（现有运行时序）。

---

## 0. 一句话结论

新增一个**物理独立**的协作域 `backend/app/collab/`：用户在画布上把「智能体」拖成节点、连成有向图，
一次运行由引擎**波次推进**（默认串行，`max_parallel > 1` 时同父兄弟工单可同波并行，见 §3.4），智能体之间**只传工单**（结构化交付物）而不传对话历史；
每个节点的运行仍然走现有的 `runtime.runner.stream_run()`，因此取消、超时、脱敏、
SSE、前端 Markdown 渲染全部直接复用，**不新增事件类型、不新增依赖、不改适配器**。

评审决策已全部确认（§12），M0「契约冻结」已落地（§13）。

---

## 1. 需求拆解

### 1.1 用户故事（来自需求原文）

```
用户 ──对话──▶ CEO（只沟通、只派活，不能写代码）
                 │  创建工单
                 ▼
                CTO（分析需求 + 编码规范，拆成子工单）
                 │  派发
                 ▼
                SE（实施，写代码）
                 │  提交
                 ▼
              审查员（审查）── 有问题 ──签署审查意见──▶ CTO ──▶ SE 返工 ──▶ 再审
                 │
                 └── 标注「完成」──▶ 工单关闭
```

### 1.2 硬性约束（逐条对应设计）

| # | 需求 | 设计落点 |
| --- | --- | --- |
| C1 | 左侧菜单新增「协作」，与「对话」同级 | §7.1 导航 + `/collab` 路由 |
| C2 | 可视化自定义智能体设定 | §7.3 画布节点属性面板（智能体下拉 + 协作语义字段；参数由智能体自己决定） |
| C3 | 可视化连线串接智能体 | §7.3 拖拽连线 + 有向边 + 边类型 |
| C4 | 智能体之间用「工单」分配任务 | §4 工单协议（结构化交接载荷） |
| C5 | **不共享记忆**，只把任务内容传给下一个智能体 | §4.2 交付载荷；强制 `ctx.history=[]`、`ctx.session_id=None` |
| C6 | 智能体能力范围受限（如 CEO 不能写代码） | §4.3 三层约束 + 越权审计 |
| C7 | 审查不过 → 带意见退回 → 返工 → 再审，直到完成 | §3.3 工单状态机（`rework` 回边） |
| C8 | 功能独立，不污染现有代码 | §2 物理分层 + §2.4 宿主改造点清单（后端共 4 处、每条 1~2 行） |

### 1.3 设计目标 / 非目标

**目标**：可理解（每一步都能在 UI 上讲清"谁把什么交给了谁"）、可终止（预算与防死循环）、
可复现（工单与产出全落库）、可审计（越权可发现）。

**非目标**：共享黑板/记忆、跨机分布式、节点内子图嵌套、
非结构化自由流转（必须走工单协议）。

> 并行执行**一期未做**，已在 M6 落地为「波次并行、默认串行」（`max_parallel=1` 时行为不变），
> 设计基线见 §3.4，专题见 `docs/collab-parallel.md`。

---

## 2. 物理分层：协作域自成一体

### 2.1 模块树

```
backend/app/collab/            ← 新目录，协作域的全部逻辑都在这里
  __init__.py        只导出 router 与 self-check 入口
  tables.py          4 张表（SQLModel），FK 用字符串表名，不 import app.models
  schemas.py         请求/响应 + 编排图（nodes/edges）的 Pydantic 校验
  protocol.py        协作信封事件（collab.*）——独立于 agents/events.py
  tickets.py         ★ 工单协议：解析智能体输出 / 渲染交付载荷（纯函数）
  graph.py           ★ 编排图校验与查询（纯函数）
  prompt.py          ★ 节点上下文 → prompt 组装（纯函数）
  engine.py          ★ 编排状态机（无 HTTP、无 SSE、无 SQL 细节之外的东西）
  store.py           持久化（只依赖 tables.py + sqlmodel）
  execution.py       ★★ 唯一与 runtime/agents 交互的边界
  api.py             APIRouter(prefix="/api/collab")，薄

frontend/src/
  pages/CollabPage.tsx             协作页：只保留协作运行（设置已移到智能体页，M2 ✅）
  components/CollabRunsPanel.tsx   协作运行页签：运行列表 + 时间线 + 工单条 + 输入框（M2 ✅）
  components/CollabSettingsPanel.tsx 协作组编排面板：智能体页「协作组」标签内使用（M2 ✅）
  components/CollabCanvas.tsx      画布：节点拖动 + 四向端口 + 正交避障连线 + 拖放添加节点（M2 ✅）
  lib/collabRouting.ts            连线端口推断与正交避障路由（纯函数）
  components/NodeInspector.tsx     节点 / 边 / 团队属性面板（M2 ✅）
  lib/agentStream.ts               从 ChatPage 抽出的智能体事件纯归约器（两页共用，M2 ✅）
  lib/collabStream.ts              协作信封事件归约（时间线 + 工单，M2 ✅）
  components/TicketBoard.tsx       工单看板抽屉：左工单列表 / 中流转时间线 / 右静态规则（M4 → M14 三栏）
  components/TicketBoardKit.tsx    看板卡片的共享零件（动作徽章 / 流转卡片 / 规则手风琴，两端共用，M14）
  lib/ticketBoard.ts               看板纯视图模型：工单 + 步骤 → 三栏要的一切（含越权判定，M14）
  mobile/pages/MobileTicketBoardPage.tsx  移动端工单看板（下拉选工单 + 流转 / 规则分段，M14）
  （样式按 AGENTS.md §4.7 写进 index.css，不新增 collab.css）
```

### 2.2 协作域内部的依赖方向（单向、无环）

```
api.py ──▶ engine.py ──▶ store.py ──▶ tables.py
                │
                ├──▶ execution.py ──▶ app.runtime.* / app.agents.*   ← 唯一的对外依赖
                ├──▶ tickets.py / graph.py / prompt.py               ← 纯函数叶子，零依赖
                └──▶ protocol.py
```

`tickets.py` / `graph.py` / `prompt.py` 是**纯函数叶子**（只依赖标准库与 dataclass）：
工单解析、图校验、载荷渲染这三块最容易出错的核心逻辑，可以**完全不启动数据库/HTTP** 单测。

### 2.3 复用 vs 隔离

| 复用（直接调用） | 方式与收益 |
| --- | --- |
| 智能体执行 | `execution.py` 调用 `runtime.runner.stream_run(definition, ctx, handle)`；取消、超时、命令回显脱敏、stdin/文件物化全部白得 |
| 运行句柄 | 复用 `runtime.manager.ActiveRun` 这个数据类（`cancel_event` / `process`），协作层自己持句柄表 |
| 智能体 | 直接外键引用 `agent_profiles`（自带 `gateway_id`），不复制、不派生 |
| 技能 | 智能体的技能分配（`agent_skills`）→ `runtime.skills.prepare_skill_home()`（§2.4 第 4 条） |
| 前端渲染 | `ConversationThread`（内部 `MessageView`）/ `MarkdownView` / `DynamicForm` / `Modal` |
| 前端流式 | `lib/api.ts:streamPost()` + 从 `ChatPage` 提取出的 `applyEvent` 归约器（§7.4） |
| SSE 传输 | `transport/sse.py:sse_stream()`（M0 已下沉为通用设施，`api/` 与 `collab/` 共用） |

| 隔离（不碰） | 原因 |
| --- | --- |
| `agents/events.py` 的事件枚举 | **不新增事件类型**：适配器协议原样保留，`test_event_protocol_matches_the_documented_contract` 依旧全绿 |
| `agents/base.py` 契约 | 不新增钩子，协作节点就是一个普通的 `AgentContext` |
| `runtime/service.py` | 它是 Conversation 中心的编排，与工单编排无关；强行复用会把它污染成两用模块 |
| `Conversation` / `Message` 表 | 协作的"发言"语义是交付物而非聊天；用协作域自己的表 |
| `models.py` / `schemas.py` / `security.py` | 零改动 |
| 全部现有测试 | 零改动（协作只新增测试文件） |

> **跨层问题已按评审意见解决**：`api/sse.py` 已平移到 `app/transport/sse.py`，成为与业务无关的
> 通用传输设施（只依赖 `asyncio/json`）。因此协作层不再需要任何"允许 import 上层"的例外，
> 架构守卫里也就没有白名单，只有硬规则（§2.5）。

### 2.4 对宿主的全部改造点（这是"防污染"的核心承诺）

| # | 文件 | 改动 | 状态 |
| --- | --- | --- | --- |
| 1 | `backend/app/api/__init__.py` | +2 行：挂载 `/api/collab` 路由 | ✅ M0 |
| 2 | `backend/app/db.py::init_db` | import `app.collab.tables` 注册新表；再调 `_add_missing_columns()` 幂等补齐后加的可选列（SQLite 的 `create_all` 不会 ALTER 旧表，见 §8） | ✅ M0/M1 |
| 3 | `backend/app/main.py::lifespan` | +3 行：启动时把残留的 `running` 运行标记为 `interrupted` | ✅ M0 |
| 4 | `backend/app/transport/sse.py` | `api/sse.py` 平移到通用传输设施；`api/conversations.py` 改 1 行 import | ✅ M0 |
| 5 | `backend/app/runtime/skills.py` | `prepare_skill_home(..., scope_id=None)`：隔离目录改用调用方给的作用域（协作运行传「运行 × 智能体 × 通道」；`prepare_mcp_home` 同一形态） | ✅ M5 |
| 6 | `backend/app/collab/*` | 协作域全部实现（见 §2.1 模块树） | M0 起 |
| 7 | `frontend/src/App.tsx` | `NAV` 加 1 项 + `Routes` 加 4 条 | ✅ M2 |
| 8 | `frontend/src/lib/api.ts`、`lib/types.ts` | 追加协作类型与接口（不改现有导出；`streamPost` 加一个泛型参数） | ✅ M2 |
| 9 | `frontend/src/index.css` | 追加协作样式（按 AGENTS.md §4.7，不新增 `collab.css`） | ✅ M2 |
| 10 | `frontend/src/pages/ChatPage.tsx` | 把 `applyEvent` 提到 `lib/agentStream.ts` 复用（协作页也要用它，所以提前到 M2 做） | ✅ M2 |
| 11 | `frontend/src/components/MessageView.tsx` | 加可选 `label` 属性（时间线上显示岗位名，默认仍是「智能体」） | ✅ M2 |
| 11 | `docs/architecture.md` | §7 扩展点表加一行指向本文档 | ✅ M0 |
| 12 | `backend/app/collab/schemas.py` `store.py` `execution.py` `prompt.py` | 新建协作任务（run）时可指定工作路径：留空 → 团队默认 → 自动分配；所有岗位统一 cwd，`workspace_scope` 只约束读写权限与越权审计 | ✅ |
| 13 | `frontend/src/components/ConversationThread.tsx` `CollabAgentThread.tsx` `AgentConversationPanel.tsx`、`src/mobile/components/MobileAgentConversationSheet.tsx` | 对话只有一份展示模块：单聊 / 协作主对话 / 协作看板岗位实时对话（含移动端岗位弹层）共用同一线程与气泡样式 | ✅ |

改动总量：后端 4 处（M0 约 7 行；M1 又在 `db.py` 追加约 20 行的"补列"工具）、前端约 6 行，
全部是"挂载"性质，不含任何业务分支注入。
（注意：**没有**在 `runtime/`、`api/`、前端里写 `if type_id == ...`，符合铁律 1。）

### 2.5 架构守卫（`backend/tests/test_collab_architecture.py`，已落地）

1. `collab/` 的任何模块都不得 import `app.api.*`、`app.runtime.*` 与上层的 `agents.*`
   （`agents.events` 这个事件数据契约除外）。
2. `collab/execution.py` 是**唯一**允许访问执行层的文件（M1 落地；守卫已就位）。
3. `api/`（除 `__init__.py`）、`runtime/`、`agents/` 不得反向依赖 `app.collab`。
4. `tickets.py` / `graph.py` / `prompt.py` 必须是纯函数模块：不得依赖 `sqlalchemy` / `sqlmodel`
   / `fastapi` / `httpx` / `starlette`（保证核心逻辑能脱离数据库与 HTTP 单测）。
5. 4 张 `collab_*` 表必须出现在 `SQLModel.metadata` 里（防止有人忘了改 `init_db`）。
6. `api/sse.py` 不得再存在，且 `api/conversations.py` 必须引用 `transport.sse`。
7. 协作信封事件必须全部以 `collab.` 为前缀，且与 `EventType` 的 10 个成员无交集
   （确保协作没有偷偷扩协议）。

## 3. 概念模型

### 3.1 协作组（Team）= 一张图

```
Team {
  id, name, description,
  entry_key,              # 唯一入口节点（唯一能和用户对话的智能体）
  workspace_dir,          # 团队默认工作路径；新建协作任务时可覆盖，留空则用团队默认
  graph: { nodes[], edges[] },        # 可视化编排结果，整图 JSON 存储
  budget: { max_steps, max_tickets, max_rounds_per_ticket, max_tokens, timeout_seconds, step_timeout_seconds }
}

Node {
  key,                    # 稳定标识（工单里引用它），如 "ceo"
  agent_profile_id,       # 绑定的智能体（内核 + 网关 + 模型 + 人格 + 技能都来自这里）
  label, duty,            # duty ∈ coordinator | engineer | reviewer
  instructions,           # 职责说明（追加到人格之后）
  capability_scope,       # 能力边界文字（"不得写代码、不得访问工作目录"）
  workspace_scope,        # none | read | write → 权限声明与越权审计（cwd 统一为本次运行的工作路径）
  output_contract,        # 该节点必须产出的动作集合（delegate/review/…）
  x, y,                   # 画布坐标
  max_rounds              # 该节点同一工单的最大返工轮次
}

Edge {
  id, from_key, to_key,
  kind,                   # assign（派发/交付） | review（审查） | rework（打回回边）
  label
}
```

### 3.2 协作运行（Run）与工单（Ticket）

- **Run**：一次协作实例（左侧列表里的一项，类比"对话"）。有自己的目标、共享工作目录、状态、运行内预算消耗。
- **Ticket**：一次可交接的工作单元。**它可以有父工单**（CTO 把大工单拆成若干子工单给 SE）。
- **StepMessage**：工单时间线上的一条"发言/交付"（谁、在哪个工单上、做了什么动作、产出什么、花了多少 token）。
  它等同于 UI 上的一张卡片，承载 `parts`（思考/正文/工具调用），复用现有 `MessageView` 渲染。

### 3.3 工单状态机（这是 C7 的落地）

工单状态：`open`(待领取) · `working`(处理中) · `waiting_children`(等子工单) · `review`(待审查)
· `rework`(打回返工) · `escalated`(已上报，等发起人处理) · `done` · `rejected` · `blocked`(等用户)

| 动作 | 允许的 duty | 工单变化 | 下一个持有者 |
| --- | --- | --- | --- |
| `delegate` 派发 | coordinator / engineer | 本工单转交，或新建**子工单**并把父工单置 `waiting_children` | 目标节点（必须在 `assign` 边白名单内） |
| `submit` 提交 | engineer | 有 `review` 边 → `review`；没有 → 直接 `done` | 指向该节点的 `review` 边的另一端（审查者）；无审查者则结束 |
| `review` + `verdict=pass` | reviewer | 工单 `→ done`；父工单（若有）回到拆解者手里汇总 | 父工单的拆解者（`owner_key`）/ 运行收尾 |
| `review` + `verdict=changes_requested` | reviewer | `→ rework`，追加审查意见，`round += 1` | `rework_to` 指明的节点；缺省退回最近提交者（`submitter_key`） |
| `done` 确认 | coordinator / engineer | 工单 `→ done`（含子工单全通过） | 父工单发起者 / 运行收尾 |
| `ask_user` 提问 | 仅入口节点 | 运行 `→ waiting_user`；工单落库 `question + options`，界面渲染决策卡 | 用户 |
| `escalate` 上报 | coordinator / engineer / reviewer | `→ escalated`；落库 `escalation{from, reason, need, hops}`，运行继续 | 这张工单的**发起人**（`owner_key`；已到顶层则入口节点） |
| `reject` 否决 | coordinator / reviewer | `→ rejected`，向上抛错误 | 父工单发起者（分支终止） |

**运行状态**：`running` · `waiting_user` · `done` · `error` · `cancelled` · `interrupted`。

**上报是保底动作**：非入口节点无论 `output_contract` 怎么配，`escalate` 都会生效 ——
任何岗位都必须能说「我搞不定」，不能被配置成一条死路（真实事故：实施者的生效动作只剩 `done`，
卡住后唯一的出口是把失败写进结论）。反过来，入口节点不持有 `escalate`：它是链路顶端，没有更
上级可报，由 `ask_user` 收口。两条都由 `graph.effective_output_contract()` 统一给出，
提示词与运行时校验才会一致。

**收尾汇报（根工单完成后交回入口智能体）**：工单沿交接链一路往下走时，用户只能在入口智能体那条
主会话里说话；活干完了如果没人回来说一声，用户就只能自己去翻工单树。所以**根工单**收尾（`done`）后，
引擎把它重新交回**入口智能体**（`collab_tickets.report_at` 记下这次汇报），由入口智能体用一次 `done`
收尾：交给它的 prompt 里附上整次协作的**结构化汇总**（每张工单的标题 / 状态 / 交付摘要 / 产出文件），
并要求它用几句话讲清结论。这一轮**只允许** `done` / `ask_user` —— 绝不允许从这里重新派活。

三条护栏：
- 只对根工单：子工单仍按「谁拆的谁汇总」各归各的发起人；
- 最后拍板的就是入口智能体本人时不再汇报（它那一步已经把话说给用户听了）；
- 每轮收尾只汇报一次（`report_at`），用户追问重新开启根工单时会清零，下一轮收尾再汇报一次。

**打回目标**：`rework` 的终点允许是**协调者**（把审查意见交回 CTO 重新拆解），不限于原实施者。
图校验只强制"有 `review` 边就必须有 `rework` 回边"；具体退回给谁由审查者在工单里用 `rework_to`
指明，不指明时默认退回最近的提交者（`submitter_key`）。如果最近的提交者不在退路里、但审查者
只有一条 `rework` 出边，就按这条唯一退路自动打回（例如审查意见交回 CTO，不需要额外填
`rework_to`）。审查者有多条退路又没指明 → 挂起等人指定，**引擎不替你猜**。

**用户决策卡**：`ask_user` 不再只给一段问题文本，而是应当带 2~12 条 `options`（推荐 2~6 条）：
`{"label": "选项标题", "value": "点选后回传给智能体的完整答复", "description": "可选说明"}`。
需要用户自由填写时追加一条 `{"label": "我来补充", "value": "", "freeform": true}`，点它只把焦点交给输入框。
`question` 与 `options` 一起落进 `collab_tickets.result`，`collab.notice(level=ask)` 也携带同样字段；
桌面端与移动端都据此在输入框上方渲染可点卡片。旧记录只有 `question` 时，界面会补出「按你的默认方案来」
（问题里提到默认方案时）与「我来补充说明」两张兼容卡片，不要求重跑历史。模型漏给结构化选项时，
引擎还会从问题正文的项目符号或「A 还是 B」句式中恢复选项，并至少补出可继续的推荐项，
保证 `waiting_user` 不会退化成一张只有协议错误的死路。

**防死循环（必须显式；M1 落底座，M3 收口）**：
- 返工轮次上限：工单建立时先按 `min(节点 max_rounds, 运行预算 max_rounds_per_ticket)` 定
  `ticket.max_rounds`；审查者打回时若 `rework` 边带了 `max_rounds`，再收紧为
  `min(工单上限, 边上限)` 并写回工单（界面显示的「返工 x / y 轮」就是它）；
- 超过上限 → 工单置 `blocked`、运行置 `waiting_user`，`run.error` 与 `collab.notice(error)`
  都写明「已返工 N 轮仍未通过（上限 M 轮），需要你介入」，**不静默**；
- **拆解树深度上限** `MAX_TICKET_DEPTH = 5`：第 6 层还继续 `split` 会被拒绝，提示改用 `handoff`
  直接交给实施者（防"拆子工单再拆子工单"无穷套娃）；
- **重名子工单上限** `MAX_DUPLICATE_CHILD_TITLES = 2`：同一个父工单下第三次拆出**同名或近似同名**
  的子工单会被拒绝，提示「先看看下游已经交回了什么、直接汇总收尾，或换一件真正不同的事」
  （防"复读式拆解"空转烧钱）。近似判定见 `tickets.titles_look_duplicated()`：归一化（去空白/标点、
  转小写）后 **完全相等 / 互相包含 / 公共前缀 ≥ 80%** 都算同一件事 —— 只比完全同名是不够的，
  模型最常靠「在标题尾巴上追加几个字」绕开它（M5 的真实故障就是这样反复了 8 轮）；
- `max_steps` / `max_tickets` / `max_tokens` 任一超限 → 挂起并明确提示；`timeout_seconds`
  按「距上次进展」的时长判断，用户隔一段时间回来继续不会因创建时间较早而永久失效；
- 单次推进的防御性硬上限 `MAX_STEPS_PER_TURN = 200`，杜绝一条消息内无限自转。

上述任何一条触发时，动作**整体被拒**：不会先建子工单再挂起，也不会留下半截状态。

### 3.4 调度规则（波次并行，默认串行）

> 并行的动机、并发安全细节与运维用法展开在 `docs/collab-parallel.md`（专题），本节是设计基线。

- 引擎维护**就绪队列**：状态 ∈ {`open`, `rework`} 的工单，按 `(深度, created_at)` 排序。
- **默认（`max_parallel=1`）只有一个步骤在跑**：谁在等谁，UI 才说得清、日志才好查、取消语义才确定。
- **波次并行（`max_parallel > 1`，预算可配，上限 8）**：每一波从就绪队列挑
  「同一父工单拆出的兄弟工单、且持有者岗位互不相同」的一批同时推进；波次全部结束（join）后
  才做父工单汇总与下一波调度。三条护栏（缺一不可）：
  - **同一岗位永远串行**：一个岗位一个对话通道（与入口聊天通道同一约束），
    两张工单派给同一岗位时排队执行；
  - **只有兄弟工单才并行**：它们由同一次 `split` 拆出、天然无先后依赖；返工环、审查链、
    跨层工单仍按串行顺序推进（`_pick_wave` 只认同父且持有者互异）；
  - **join 点天然同步**：`_rollup_parents` / `_hand_back_to_entry` / 收尾汇报只在波次边界跑，
    并行分支各自开独立数据库会话，事件经队列汇流（信封带 `step_id`，前端按它分流）。
- **一次 split 可拆多张子工单**（`tickets` 数组，上限 8 张，每张可自带 `assignee` 分给不同岗位）：
  这是并行波次的输入来源；单张 `ticket` 写法完全兼容。多张拆分仍走全部护栏（深度 / 近似重名 / 逐张
  校验派发对象白名单），任何一张不合规整单拒绝。
- **计数与用量在并行下不丢账**：步骤序号用运行行上的 `step_seq` 计数器原子取号
  （`UPDATE ... RETURNING`）；`steps_used` 原子自增；`usage` 由波次 join 后的主循环单点聚合，
  聚合只写 `usage` 一列（整对象写回会把并行会话的自增盖掉）。
- **谁拆的谁汇总**：子工单全部完成 → 父工单自动回到**拆解者**手里（`ticket.owner_key` 记着"谁拆的"，
  `waiting_children → open`），协调者据此继续 `delegate` 或 `done`。
- 没有就绪工单且没有 `waiting_children` 的工单 → 若根工单已完成但还没汇报，先把它交回入口智能体
  做收尾汇报（见 §3.3），之后运行才进 `done`。

---

## 4. 工单协议（C4 / C5 / C6 的核心）

### 4.1 智能体输出格式

每个节点在**一轮发言的末尾**必须附一个可解析的工单块（框架在 prompt 里给出模板与示例）：

````
（这里是给人看的说明文字，进入时间线卡片正文）

```work_order
{
  "action": "delegate",
  "ticket": {
    "title": "登录页改造为暗色主题",
    "goal": "在不改动后端的前提下，让登录页支持暗色主题",
    "requirements": ["新增主题开关", "持久化用户选择"],
    "acceptance": ["刷新后主题保持", "对比度符合 WCAG AA"],
    "constraints": ["只改 frontend/src/**", "不得新增依赖"]
  },
  "assignee": "cto",
  "parent_ticket": null,
  "verdict": null,
  "review_comment": null,
  "artifacts": [{"kind": "file", "path": "frontend/src/pages/Login.tsx"}]
}
```
````

需要用户拍板时输出决策卡数据，而不是把选项写进正文：

````
```work_order
{
  "action": "ask_user",
  "question": "首期先做最小闭环，还是一次做全？",
  "options": [
    {"label": "先做最小闭环", "value": "先做材料与人工台账，报表导出下一阶段再做", "description": "最快可用"},
    {"label": "一次做全", "value": "材料、人工、成本汇总、报表导出一起做"},
    {"label": "我来补充", "value": "", "freeform": true}
  ]
}
```
````

**解析容错（`tickets.py`，已实现）**，按优先级依次尝试：

1. **严格 JSON**：` ```work_order ` 围栏（围栏缺失/被截断时按"到文本末尾"处理）→ 直接 `json.loads`。
2. **正则清理后再解析**：删除不可见控制字符（`\x00-\x1f`、BOM）、把中文智能引号 `“ ”` 还原成 `"`、
   迭代修掉尾随逗号（`,}` / `,]`）。这一步能救回大量因复制粘贴、截断、转义造成的低级语法错误。
3. **扫描正文里平衡的 `{...}`**：先内层后外层逐一尝试，能处理"JSON 前后裹了多余文字"的情况；
   仍然失败时启用**截断补齐**——从后往前回退到最后一个完整 `}`，或补齐未闭合的 `{`/`[`
   （必要时先补上被截断的引号），再回退到最后一个逗号重试。

三级都失败 → 产出 `error` 事件（"「CTO」的产出不符合工单协议，已暂停等待你介入/重试"），
工单**不推进**、运行置 `waiting_user`。**绝不猜、绝不静默**（铁律 11）。
特别地：**一轮出现多个合法工单块时取最后一个**，并额外产出一条 `collab.notice(warn)` 说明这次偏差。
工具型内核（Claude Code 等）会把「中间轮次的草稿」与「最终答复」拼进同一段输出，
一律判死会让整条链路白跑（实测：码农写完草稿又补了一份最终 `done`，运行被迫挂起）。
取最后一版与"模型改了主意"的语义一致，而且 `assignee` 仍要过边的白名单校验，
取错的代价被限制在**合法**对象之间（铁律 11 的可观测性由 notice 保证）。

解析后再做**语义校验**：`action` 是否在节点的 `output_contract` 内、`assignee` 是否在边的白名单内、
`pass` 是否带 `review_comment`、必填字段是否齐全。任一不通过 → 同上，作为显式错误。

### 4.2 交付载荷：下一个智能体到底看到什么（C5）

框架用**模板**（不是上一轮的对话）拼出下游 prompt：

```
[协作] 你在协作组「客户端研发组」中担任「SE」，职责：<node.instructions>
能力边界（不得越界）：<node.capability_scope>
协作组名册（系统提供的团队结构，只读）：       ← 我在和谁一起干活（V0.29）
  1) CEO（ceo）· 协调者 · 入口，唯一和用户对话的智能体 · 只负责理解需求与派活
  2) CTO（cto）· 协调者 · 负责拆解任务与汇总
  3) SE（se）· 实施者
  4) 审查员（qa）· 审查者
  名册只说明团队里有哪些人、谁负责什么；你能派给谁、谁审你，一律以「输出协议」里列出的对象为准。
流转规则：本轮结束必须附 ```work_order 块；你只能把工单交给：CTO（assign）。

[工单 T-12] 登录页改造为暗色主题          ← 只含工单字段
目标：…
需求：1) … 2) …
验收标准：1) … 2) …
约束：1) …
上游交付摘要：                           ← 摘要，不是对话
  - 「CTO」于 14:03 提交：把主题开关拆成 2 个子任务（子工单 T-13/T-14 已通过）
  - 变更文件：frontend/src/theme.ts
下游交付（我派出去的子工单，引擎汇总）：    ← 子工单交回了什么（M5）
  - T-13《实现主题开关》· done · 最后经手：se · 结论：已完成主题开关与持久化 · 产出：src/theme.ts
本工单历史：                             ← 只有同一工单的历史
  - 第 1 轮「SE」提交 → 「审查员」打回：「第 2 条验收未满足：…」
本轮要求：请按审查意见修改后重新提交。
```

**交付的两种形态**（`DeliveryMode`，M1 已落地）：
- `handoff` **转交**：同一张工单换个人做，新字段与旧字段合并（CEO→CTO 这种"同一件事往下传"）；
- `split` **拆子工单**：新建一张子工单并把父工单置 `waiting_children`（CTO→SE 这种"大任务拆小"），
  父工单由拆解者（`owner_key`）在子工单全通过后负责汇总。

隔离保证（有测试断言，且**在数据通路上结构性成立**，不靠实现者自觉）：
- `ctx.history = []`、`ctx.session_id = None`（**每个步骤都是全新会话**），绝不续接内核会话；
- prompt 里**不含**任何智能体的完整对话、其他工单正文、其他分支内容；
- 上游只能以「工单字段 + 一句话摘要 + 文件路径清单」的形式被看见；
- **下游**同理，只能以「工单字段 + 一句话摘要 + 文件路径清单」的形式被看见（M5 的「下游交付」区块，
  由 `engine._child_digest_lines()` 派生，**只取直接子工单**；子树的细节沿着每一层自己的 `result`
  逐级带上来，不重复塞进同一份 prompt）。这是「活干完了，上游必须看得见」的那条数据通路 ——
  少了它，上游只看得见自己几次派单记录，会把「已完成」误判成「还没回报」，一遍遍重派；
- 这一步的摘要取自引擎派生的 `collab_steps.gist`（只由结构化字段拼成），拼 prompt 时**只读
  `gist`、不读 `content`** —— 下游看不到对话原文不是靠自律，而是没有那条通路。
- **例外之一：全组名册**（V0.29）。每个节点都能看到「团队里有谁、谁负责什么、入口是谁」
  （`prompt.roster_block()`，由 `engine._team_roster()` 从编排图导出），聊天通道同样带上。
  它是**用户自己画的公开结构**，不是任何智能体的记忆，因此不越 C5 的线；少了它反而出事：
  入口智能体答不出「团队里有哪些人」，连组里配了审查者都不知情（真实故障：同一张图里 CTO
  说过「本协作没有安排审查者」）。名册**只说结构，不说权限** —— 谁能指挥谁是逐轮变化的，
  权威来源仍是该轮「输出协议」给出的派发 / 审查对象。

### 4.3 能力范围（C6）：分三层，写在方案里而不是指望提示词

| 层 | 机制 | 覆盖面 | 说明 |
| --- | --- | --- | --- |
| L1 提示词 | `capability_scope` + 输出契约注入 | 所有内核 | 只是约束，不是保证 |
| L2 工作目录 | 协作组在新建时可填「工作路径」；留空则按运行 ID 分配默认目录。所有岗位都拿到同一个绝对路径并作为 cwd，prompt 明确它是唯一允许访问的目录 | 所有内核 | 路径不再因智能体而异；`none` / `read` 仍由提示词约束 + 越权审计兜底 |
| L3 越权审计 | 运行前后对共享工作目录做指纹快照（文件路径 + 大小 + mtime + 内容哈希，带文件数/体积上限）；`read`/`none` 节点被检出改动 → `error` 事件 + 卡片标红 + 工单打回 | 所有内核 | 通用、无需内核知识 |

原设计里一度设过的「L3 参数覆盖」（节点级 `options_override`）已按弃用流程下线：V0.27 先在
本节标注并保留一个版本的兼容读取，V0.28 移除。**一个岗位用什么参数跑，完全由它绑定的智能体决定**，
与对话链路一致 —— 协作不该再有一套只在这里生效的适配器参数。

诚实声明（写进文档"已知限制"）：L3 是**事后发现**而非事前阻止；
真正的强隔离需要容器/沙箱，属于后续独立议题。

### 4.4 「能力范围」与「智能体设定」的分工（C2）

- **智能体（AgentProfile）**回答"他是谁"：内核 + 网关 + 模型 + 人格 + 技能 —— 沿用现有「智能体」页，不重复造。
- **节点（Node）**回答"他在这个协作里干什么"：职责、能力边界、可发起的动作、可派发给谁。
- 两者通过节点上的 `agent_profile_id` 下拉绑定；同一个智能体可以出现在多个协作组里、担任不同 duty。

---

## 5. 运行时引擎

### 5.1 一次步骤的时序

```
engine.prepare_turn(run, content)        # 校验图、把用户这句话接到入口工单上
engine.consume_turn(prepared)            # 波次推进直到挂起/完成（默认串行）
 ├─ 选出就绪工单 + 目标节点
 ├─ store 落库 StepMessage(status=streaming) 与 ActiveRun 句柄
 ├─ 载入 AgentProfile（+ Gateway） → get_agent(type_id) → 组装 AgentContext
 │    ├─ ctx.prompt / system_prompt：由 prompt.py 拼装（人格 + 协作段 + 工单载荷）
 │    ├─ ctx.history=[]、ctx.session_id=None            ← 不共享记忆
 │    ├─ ctx.workspace_dir：所有岗位统一为「本次协作任务」的工作路径（任务指定 > 团队默认；
 │    │                     workspace_scope 只约束读写权限与越权审计）
 │    └─ ctx.skills / ctx.home_dir：按节点绑定的智能体分配物化（scope_id = 运行内的作用域）
 ├─ async for event in execution.stream(node, ctx, handle):
 │    ├─ 原始 AgentEvent → 包进 collab.step.event 信封 → 立刻 SSE 推给前端（逐字打字效果）
 │    └─ 同时累积 text / parts / usage（复用 consume_run 同样的累积方式）
 ├─ 审计：read/none 节点的共享目录指纹比对
 ├─ 解析工单块 → tickets.parse() → 得到 action 与载荷
 ├─ store 落库 StepMessage + 更新 Ticket + 产出 collab.ticket.* 元事件
 └─ 决定下一个持有者；就绪队列空则收尾
```

### 5.2 传输协议（两层，互不干扰）

- **第一层（智能体协议）**：`agents/events.py` 的 10 种事件，**一字不改**。
- **第二层（协作信封，`collab/protocol.py`）**，只被新的协作页面消费：

| 事件 | 载荷 | 用途 |
| --- | --- | --- |
| `collab.run.started` | `run_id, team_id, entry_key` | 建立运行上下文 |
| `collab.step.started` | `step_id, ticket_id, role_key, duty, agent_name` | 时间线插入"正在处理"卡片 |
| `collab.step.event` | `step_id`, `event`（内嵌原始 AgentEvent） | 逐字流式渲染 |
| `collab.step.finished` | `step_id, status, action, usage` | 卡片收尾 |
| `collab.ticket.created` / `collab.ticket.updated` | 工单快照 | 看板增改 |
| `collab.run.status` | `status, reason` | 运行终态 / 等待用户 |
| `collab.notice` | `text, level, question?, options?` | 预算、审计、解析失败等提示；`level=ask` 时带决策卡数据 |

> 注意用词：这两个协议"层次不同"是刻意的 —— 铁律 5 约束的是**适配器协议**，
> 协作信封属于协作域自有传输协议，因此不需要改 `consume_run` / `applyEvent` / `architecture.md` 的事件表。

### 5.3 取消 / 超时 / 异常（铁律 10 / 11）

- 取消：`POST /api/collab/runs/{id}/cancel` → 置 `handle.cancel_event` + kill 子进程；
  当前步骤落库为 `cancelled`，运行置 `cancelled`，未完成工单保留（便于查看与续跑）。
- 超时：`budget.timeout_seconds` 是两次进展之间的空闲时限，不是从创建时间起累计的整轮上限；
  每次用户输入与步骤落库都会刷新 `run.updated_at`，因此 `waiting_user` 期间隔再久，用户回来继续时
  也会获得新的推进窗口。**单步限时**走独立字段 `budget.step_timeout_seconds`（默认 36000 秒，
  `0 = 不限`）：执行层在 `prepare_step` / `prepare_chat` 时把它转成
  `handle.timeout_seconds`（预算的 0 转成 -1），再交给 `runtime.runner.stream_run()`；
  runner 的语义是正数 = 限死、负数 = 不限、0 = 沿用全局 `settings.run_timeout_seconds`
  （全局默认为空 = 对话域不限时，所以这个哨兵不能表达「不限」，协作域不会把 0 传下去）。
- 预算超限挂起：步数 / 工单 / token 任一达到上限都会挂起并给出可读原因；用户可以
  `POST /api/collab/runs/{id}/budget` **只调高**对应上限（`max_tokens=0` 表示不限），
  清掉挂起原因后再 `POST /resume` 从原处续跑。新上限必须大于当前已用量，避免“调完立刻又挂起”。
  默认 `max_steps=200`：拆子工单的图里 60 步很容易在收尾前被掐断。
- 异常：任何步骤异常 → `error` 事件 + 运行置 `error`（工单保留现场，可重试该步）。
- 解析失败 / 语义非法：不抛异常、不猜 —— 当前步骤落库 `error`、工单置 `blocked`、运行置
  `waiting_user`；用户修好模型/剧本后 `POST /api/collab/steps/{id}/retry` 把工单放回队列，再
  `POST /api/collab/runs/{id}/resume` 续跑（后端 M1 已实现，UI 按钮留到 M4）。
- 客户端断开：**不再打断运行**。推进跑在 `collab/bus.py` 的独立后台任务里，SSE 只是它的一个订阅者；
  连接被关闭（手机锁屏、切后台、隧道抖动）只退订，重新打开页面或换设备用
  `GET /api/collab/runs/{id}/stream` 订阅同一份实时事件即可续看。前端在检测到流被切断时（没收到
  `stream.closed`）会自动重连订阅并提示「正在自动重连」。

### 5.4 重启恢复

`collab.recover_interrupted()`：启动时把所有 `running` 运行标记为 `interrupted`，并把残留的
`streaming` 步骤一并收尾（否则界面会一直显示「处理中」却点不出任何输出）。工单保留原状态，
UI 提供「继续」按钮（从就绪队列重新推进）。不删数据、不重建库（铁律 9）。

---

## 6. API 设计

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/collab/teams` | 协作组列表（含节点数/是否可运行） |
| `POST` | `/api/collab/teams` | 新建（可先空图） |
| `GET` | `/api/collab/teams/{id}` | 详情：元信息 + 完整图 + 上次校验结果 |
| `PUT` | `/api/collab/teams/{id}` | **整图保存**（元信息 + nodes + edges），返回校验结果；改图只影响还没开跑 / 之后新建的运行（已下工单的运行用自己的图快照） |
| `POST` | `/api/collab/teams/{id}/validate` | 只校验不保存（编辑器实时校验用） |
| `POST` | `/api/collab/teams/{id}/duplicate` | 复制 |
| `DELETE` | `/api/collab/teams/{id}` | 删除（有运行记录 → 409） |
| `GET` | `/api/collab/runs?team_id=` | 运行列表（左栏） |
| `POST` | `/api/collab/runs` | 创建运行 `{team_id, title?}`；目标由第一条 `messages` 给出（保持"用户在跟入口智能体对话"的语义） |
| `GET` | `/api/collab/runs/{id}` | 运行详情（工单 + 时间线 + 状态 + 预算消耗） |
| `GET` | `/api/collab/runs/{id}/steps/{step_id}/parts` | 单个步骤的完整 `parts`（懒加载步骤流水：详情只带 `parts_count`，点开「过程」时才取） |
| `POST` | `/api/collab/runs/{id}/messages` | **SSE**：用户对入口智能体说话 / 回复提问 |
| `GET` | `/api/collab/runs/{id}/stream` | **SSE**：订阅一次运行的实时事件（断开后重连、或在另一台设备上围观） |
| `POST` | `/api/collab/runs/{id}/cancel` | 停止 |
| `POST` | `/api/collab/runs/{id}/budget` | 预算超限挂起后调高步数 / 工单 / token 上限（只增不减；`max_tokens=0` 不限） |
| `POST` | `/api/collab/runs/{id}/resume` | 从就绪队列继续推进 |
| `POST` | `/api/collab/steps/{id}/retry` | 把某一步所在的工单放回队列（解析失败/中断后修复，随后 `resume` 续跑） |
| `DELETE` | `/api/collab/runs/{id}` | 删除运行（级联删工单与步骤消息） |

校验错误用 **422** + 结构化 `detail`（含 `errors[]`，编辑器逐条高亮节点/边）；
智能体的删除是**软删除**（见 `docs/architecture.md` §6）：协作历史里引用的智能体
不会因为删除而消失，仍被**未删除**的智能体引用的网关删除时返回 **409**。

**节点 key 是引用键，不是显示名**：工单（`owner_key` / `holder_key` / `submitter_key` / `escalation`）、
步骤（`role_key` / `target_key`）、运行（`entry_key`）与入口对话（`role_key`）里存的都是**当时的 key**，
改名不会自动迁移。**运行在第一次推进（下发第一张工单）时会把整张图快照进 `collab_runs.graph`**，
之后引擎只读这份快照 —— 所以改协作组（改 key、删节点、换边、换入口）只影响**还没开跑 / 之后新建**的运行，
已下工单的运行继续按自己的图跑完，不会再出现「改图打断在跑任务」的 409。还没开跑的运行不拍快照，
因此「先建任务、再回去修图」仍然能把这次任务救回来。升级前没有快照的老运行由启动回填
（`store.backfill_run_graphs`）或保存前的补拍冻结住正在用的那张图。同理，提示词里**不得出现任何
写死的示例 key**：节点 key 由用户在画布上起名，写死的示例（曾经是 `dev`）会被模型当成答案抄进
`assignee`，改名后每一轮派发都必错。

---

## 7. 前端设计

### 7.1 导航（已确认：协作运行独立成页，编排入口收进智能体页）

主导航（`App.tsx` 的 `NAV`）新增一项：`{ to: '/collab', icon: '🤝', label: '协作组' }`
（放在「智能体」之后，与移动端抽屉里的叫法一致）。协作页只保留运行；协作组的编排入口在
「智能体」页的第二个标签（卡片列表 → 点卡片进入画布）：

| 路由 | 位置 | 内容 |
| --- | --- | --- |
| `/collab` | 协作运行 | 左栏运行列表，主区时间线 + 工单条 + 输入框 |
| `/collab/runs/:runId` | 协作运行 | 直接打开某次运行（刷新/分享链接不丢上下文） |
| `/agents?tab=collab` | 智能体 → 协作组 | 协作组卡片列表（新建 / 点卡片进入设置） |
| `/agents?tab=collab&team=:teamId` | 智能体 → 协作组 | 画布编排器（可刷新还原） |
| `/collab/settings*` | 旧地址 | 重定向到 `/agents?tab=collab*`，书签不失效 |

### 7.2 协作运行页

```
┌ 左栏（复用 chat-list 样式） ┐┌ 主区 ────────────────────────────────────────┐
│ 🤝 客户端研发组              ││ 顶栏：组名 · 目标 · 状态 · [🧭进度][📋工单][停止] │
│   · 登录页暗色主题  进行中    │├──────────────────────────────────────────────┤
│   · 支付流程重构    完成      ││ 主会话只展示用户 ⇄ 入口领导者                  │
│ 协作组切换 ▾                 ││  👤 你：把登录页改成暗色主题                  │
│                              ││  🧑💼 CEO：…（说明文字）+ 📋 工单卡 T-12 →CTO │
│                              ││  🧑🔧 CTO：…（拆解）+ 📋 子工单 T-13 →SE      │
│                              ││  👩💻 SE：…（实现）+ 📎 变更文件 3 个         │
│                              ││  🔍 审查员：… ❌ 打回：第 2 条验收未满足       │
│                              ││  👩💻 SE：…（返工）✅ 提交                     │
│                              ││  🔍 审查员：✅ 通过 → 工单 T-12 完成           │
│                              │├──────────────────────────────────────────────┤
│                              ││ 底部输入框：直接和入口智能体对话（走 SSE）        │
└──────────────────────────────┘└──────────────────────────────────────────────┘
                                    岗位抽屉：内部步骤与实时输出；工单看板：工单详情
```

> 上图中 CTO、SE、审查员的发言现在只出现在各自岗位抽屉里；主会话只保留用户与入口领导者的往来。

**M2 已落地的部分**：左栏运行列表、主区只展示「用户 ⇄ 入口领导者」的对话、顶栏「协作进度」按钮、
横向的工单摘要条（状态 / 持有者 / 返工轮次 / 审查意见）、底部输入框直接和入口智能体说话、
顶部状态与预算消耗、停止 / 继续 / 重试 / 删除，以及 `collab.notice` 的即时提示。
`waiting_user` 且存在 `ask_user` 工单时，输入框上方显示决策卡；点选项等价于发送一条用户消息。
点击「协作进度」打开全屏进度页；每个岗位都可继续点开，查看该智能体在本次协作里的历史步骤与当前流式输出；其他智能体的发言不会
混进主会话。流式推进期间，用户刚发出的消息和入口领导者已完成的回复会先以临时步骤显示，
整轮结束后再由落库详情替换，避免“领导者明明回复了，主会话却空着”的断层。
**留到 M4**：右侧工单抽屉（子工单树、产出文件、越权审计）、按状态分列的看板、卡片视觉细化。

**跨设备同步**：桌面端与移动端共用 `lib/useAutoRefresh.ts`。当前页可见时，对话/运行详情每
3~4 秒、列表每 8 秒从后端对齐一次；切回标签页或应用回到前台时立即刷新。本机正在跑 SSE 时暂停
轮询，避免把尚未落库的流式内容清掉。对话详情若发现 `active_run` 属于另一台设备，输入区会暂时
禁用并提示等待完成，防止两端同时提交。

**岗位会话的交接链**：一个岗位的抽屉按时间顺序展示「谁对我说了什么 → 我做了什么」——入口领导者
先显示用户的开场消息，其余岗位显示上游智能体派工时的发言（步骤 `content`，即智能体说的人话）与该工单
的要点（`brief`）。派工步骤落库 `target_key`（把活交给了哪个节点），据此把交接卡精确挂到接收方；
旧数据没有 `target_key` 时退化为只显示本岗位自己的步骤（不影响展示，只是少了上游那句话）。

**岗位内部会话的「进行中」表达**：智能体的输出在**这一步跑完时**才落库，但运行总线
（`collab/bus.py`）会把本轮事件补发给任何订阅者——刷新后的页面、另一台设备、中途点开岗位抽屉
都能接到逐字流。前端在运行详情轮询时不能清空本端已收到的 `live` / `completed`，否则正在流式
输出的步骤会退化成落库时空空如也的 `streaming` 行；同一步骤同时出现在落库详情与流式结果里时，
以流式结果为准。真断线（网络抖动、服务重启）时，订阅循环按落库状态判断运行是否仍在推进并自动
重订阅，而不是把「流断了」误当成「这一轮结束了」。只有确实没有产出时才渲染占位：`streaming` →
「正在处理中……」，`interrupted` / 无产出 → 「被中断、没有留下输出，可点继续重跑」。服务重启时
（`collab.store.recover_interrupted`）除把残留 `running` 运行标记为 `interrupted` 外，也会把残留的
`streaming` 步骤一并收尾为 `interrupted`，否则这些步骤会永远停在「处理中」。

- 时间线卡片类型：用户发言 / 智能体发言 / **工单卡**（`delegate`）/ **审查卡**（`verdict`）/ **系统提示**（预算、审计、解析失败）。
- 工单卡可点击 → 抽屉展示工单字段、子工单树、历史轮次、产出文件、token 用量。
- "谁把什么交给了谁"用卡片上的 `→ 智能体名` 与看板列变化表达，无需用户读日志。

### 7.3 可视化编排器（C2 / C3）

```
┌ 左：智能体抽屉 ┐┌ 中：画布（网格背景） ────────────────────────┐┌ 右：属性面板 ┐
│ 🧑💼 学究CEO   │   ┌────────┐   assign    ┌────────┐            ││ 节点/边属性   │
│ 🧑🔧 CTO       │   │  CEO   │────────────▶│  CTO   │            ││ 智能体 ▾        │
│ 👩💻 SE        │   │协调者 ⭐│             │实施者   │            ││ 职责说明      │
│ 🔍 审查员      │   └────────┘             └───┬────┘            ││ 能力边界      │
│ （拖到画布）   │         ▲  rework            │ assign          ││ 工作目录范围 ▾│
│               │         │  （虚线回边）      ▼                 ││ 最大返工轮次  │
│               │   ┌─────┴────┐  review  ┌──────────┐           ││ 入口节点 ☑    │
│               │   │   审查员  │◀─────────│    SE    │           ││              │
│               │   └──────────┘          └──────────┘           ││              │
└───────────────┘└────────────────────────────────────────────────┘└──────────────┘
```

交互（全部手写，**不引入图库**，铁律 12；M2 已落地）：
- 节点：`position: absolute` 的 div，`pointerdown/move/up` 拖动（监听挂在 window 上，移出画布也不卡），
  坐标存进 `graph.nodes[].x/y`。
- 添加节点：三个入口都通往同一个 `addNode()` —— 点选智能体抽屉里的智能体、把智能体**拖放**到画布上、
  或由第一个节点自动当入口。新节点默认 `engineer`，第一个节点自动设为 `coordinator` + 入口。
- 连线：SVG `<path>` + 箭头 marker；三种边用颜色/线型区分
  （assign 灰实线 · review 青实线 · rework 黄虚线）。
- 端口：输入在节点左/上，输出在右/下；从任一出输出端口拖到目标输入端口即可。端口方向写进
  `edges[].from_side/to_side`，旧图没有该字段时按节点相对位置自动推断（上下排列走「下→上」，
  左右排列走「右→左」）。
- 走线：采用正交折线并做避障；路径避开所有节点卡片，且对既有连线的重叠、交叉施加额外代价，
  因此审查者从下方回接上方组织时，线路会沿空白通道绕回，而不是穿过节点或叠在别的线上。
  不做自动布局；边类型仍由两端岗位自动推断（目标是审查者→`review`；来源是审查者→`rework`；
  其余→`assign`），随后可在右侧面板改。
- 删除：选中节点/边后按 `Delete`、或点面板里的删除按钮；删节点会级联删掉它的边。
- 校验：每次改动**防抖 500ms** 调一次 `POST /api/collab/teams/{id}/validate`（与运行时同一套规则），
  错误/提醒就地高亮节点、连线和面板，并把「人话提示」列在画布下方；前端不重复实现一遍规则。
- 属性面板：智能体用现有 `listAgents()` 下拉；其余字段都是**协作语义**（职责、能力边界、
  工作目录范围、动作集合、连给谁），**没有节点级参数覆盖** —— 用什么参数跑由它绑定的智能体决定，
  与对话链路一致。
- 校验实时提示（本地 + 服务端双保险）：
  - 必须**唯一入口节点**；非入口节点必须至少有一条入边；
  - 存在 `review` 边则必须有 `rework` 回边（否则无法返工），反之亦然；
  - 环必须显式声明为 `rework`；`assign` 边不许成环；
  - 每个 duty 允许的动作集合与出边类型必须相容（如 `reviewer` 不能有 `assign` 出边）；
  - 引用的智能体存在且绑定了智能体。

**移动端（`/m/`）同样能编排，只是不画线**：`mobile/pages/MobileCollabTeamPage.tsx` 用表单做同一件事 ——
岗位是卡片列表（点开底部弹层编辑），连线是「从谁到谁 + 类型」的下拉选择，校验与保存走同一套接口。
岗位/连线表单在 `mobile/components/CollabNodeForm.tsx` / `CollabEdgeForm.tsx`；字段语义、默认值与
中文标签与桌面端共用 `lib/collabDraft.ts`（`DUTIES` / `EDGE_KINDS` / `SCOPES` / `ACTIONS` /
`clampBudget` / 草稿组装），有守卫测试盯着「移动端不得引用画布组件」。

### 7.4 复用与新增（避免两份流式归约器）

- `frontend/src/lib/agentStream.ts`（**已新增**）：把 `ChatPage.applyEvent` 的纯归约逻辑搬出来（行为不变），
  `ChatPage` 改为 import；协作页对 `collab.step.event` 解包后复用同一个归约器 —— 时间线渲染与对话页一字不差。
- `frontend/src/lib/collabStream.ts`（**已新增**）：协作信封事件的归约（进行中的步骤 + 工单 + 运行状态 + 提示）。
- 类型放 `lib/types.ts`、接口放 `lib/api.ts`（`streamPost` 加一个泛型参数以复用同一份 SSE 读取逻辑）。
- 样式按 **AGENTS.md §4.7** 写进 `index.css`（沿用 `card`/`btn`/`pill`/`banner`/`field` 等既有命名与 CSS 变量），
  不再单独引入 `collab.css`；`main.tsx` 不动。

---

## 8. 数据库设计

新增 4 张表（`collab/` 前缀，与现有表共存，**不改任何现有表**）：

```
collab_teams(id PK, name, description, entry_key, workspace_dir,
             graph JSON, budget JSON, owner_id, created_at, updated_at)

collab_runs(id PK, team_id → collab_teams RESTRICT, title, goal, status,
            entry_key, workspace_dir, graph JSON, budget JSON, usage JSON,
            steps_used, tickets_used, error, owner_id, created_at, updated_at)
            idx(team_id), idx(status), idx(owner_id)

collab_tickets(id PK, run_id → collab_runs CASCADE, parent_ticket_id → collab_tickets SET NULL,
               from_agent_profile_id → agent_profiles RESTRICT (可空，用户发起时为空),
               to_agent_profile_id → agent_profiles RESTRICT,
               title, brief JSON, holder_key, state, round, max_rounds,
               verdict, review_comment, result JSON, created_at, updated_at, closed_at)
            idx(run_id), idx(state), idx(parent_ticket_id)

collab_steps(id PK, run_id → collab_runs CASCADE, ticket_id → collab_tickets SET NULL,
             agent_profile_id → agent_profiles RESTRICT,
             role_key, duty, sequence, action, status, content, parts JSON, usage JSON,
             error, audit JSON, started_at, finished_at)
            idx(run_id), idx(ticket_id), idx(status)
```

设计取舍：

- **工单 `brief` / `result` 用 JSON**：工单字段（需求/验收/约束/产出）形态会演进，
  与现有 `messages.parts` 同一思路；同时也是"交付载荷"的直接数据源。
- **`collab_steps.content` + `parts`**：与 `messages` 表同构 → 前端能直接喂给 `MessageView`。
- **外键 `RESTRICT`（引用智能体 / 网关）**：沿用现有不变量，因此智能体只能软删除
  （打 `deleted_at`，从可选列表消失、历史照旧可读），协作表的外键不会被打断；
  删除"运行"是显式操作，级联清理工单与步骤（等价于"删对话必须清消息"的既有惯例）。
- **`parent_ticket_id` 用 `SET NULL`**：父工单被拒绝/删除时不连带毁掉子工单记录。
- **零迁移风险**：全部是新增表，`create_all` 会自动建；现有 `data/coda.db` 直接启动即可（铁律 9）。
- **运行快照 `collab_runs.graph`**：运行第一次推进时复制整张图，之后引擎只读这份快照（与 `budget` /
  `entry_key` / `workspace_dir` 同属「运行时的配置快照」）。协作组的后续修改只影响还没开跑的新运行，
  已下工单的运行不会被改 key / 删节点打断。后加列由 `ADDITIVE_COLUMNS` 幂等补上，老行留空。
- **多用户归属**：`collab_teams.owner_id` 与 `collab_runs.owner_id` 都是后加的可空列（旧库由
  `bootstrap.backfill_ownership` 回填给首任管理员）。协作组、运行、工单与步骤的读写接口按
  `owner_id` 隔离；协作图节点只能引用当前用户自己的智能体（管理员除外），否则运行时会用到
  对方的网关密钥 / 凭据。
- **后加的可选列怎么补**：SQLite 的 `create_all` **不会给已存在的表加列**。因此协作域把"后加的可选列"
  登记在 `collab/tables.py` 的 `ADDITIVE_COLUMNS`（对话域登记在 `models.py` 的同名常量），
  由 `db.init_db` 用 `ALTER TABLE ... ADD COLUMN`（带默认值）幂等补齐 —— 旧行原样保留，不重建库。
  加新列时必须同时登记，否则旧库启动后会报 "no such column"（有测试 `test_collab_migration.py` 守着）。

---

## 9. 测试与验收

**单元（纯函数，不启 DB）**
- `tickets.parse()`：标准围栏 / 无围栏裸 JSON / 缺字段 / 非法 `action` / 越权 `assignee` /
  同一轮出现多个块（取最后一个 + `warning`）/ 完全无法解析（必须产出显式错误而非猜）。
- `tickets.render()`：**断言下游 prompt 不含上游对话原文**（C5 的守护测试）、只含本工单历史。
- `prompt.build_step_prompt()`：名册列出全组成员（岗位 + 职责 + 谁是入口），且**排在输出协议之前**；
  不给名册时 prompt 与旧版逐字一致（老调用方不受影响）。
- `graph.validate()`：无入口 / 双入口 / 孤立节点 / `assign` 成环 / `review` 无边 / 缺 `rework` 回边 /
  reviewer 带 assign 出边 / 引用不存在的智能体。
- 状态机：§3.3 转移表逐条（含 `round` 超限、`max_steps` 超限）。

**集成（httpx + ASGI，沿用 `tests/conftest.py`）**
- 用 `tests/fakes/fake_collab_agent.py`（按 prompt 里的岗位 key 回不同剧本，能模拟协议违规）
  驱动一个 CEO→CTO→SE→审查（拆子工单 + 打回一次后通过）的完整流程：
  断言工单状态流转、`collab_steps` 落库顺序、SSE 事件序列、运行终态、
  以及**每个下游 prompt 的隔离性**。
- 收尾汇报：根工单完成后交回入口智能体（`done`）、汇总里含子工单结论、prompt 只允许 `done`/`ask_user`、
  入口智能体自己收尾时不重复汇报、用户追问后重新开启一轮汇报。
- 团队名册：**每个岗位**的 prompt（含入口智能体的聊天通道）都带上全组名册 —— 断言四类岗位的
  key / 显示名 / 岗位都出现，且名册之外没有第 5 个人。
- 取消 / 超时 / 解析失败 → 运行状态与事件正确。
- 重启恢复：造 `running` 残留 → `recover_interrupted()` → `interrupted`。

**架构守卫**：§2.5 的 6 条。

**验收（对应 §8 DoD）**
```bash
bash scripts/test.sh              # pytest 全绿 + 前端 tsc
cd frontend && npm run build      # 前端改动必须
curl -s http://127.0.0.1:8010/api/health
# 前端改完需重启服务再自测画布与协作页
```

**手工验收清单**：导航出现「协作」；画布能拖节点/连线/存图/刷新后还原；
错误图给得出人话提示；跑一次真实协作（≥3 个智能体、≥1 次打回）；
工单看板与时间线一致；取消按钮真的能杀掉子进程；`/api/agent-types`、现有对话页无回归。

---

## 10. 风险与取舍

| 风险 | 影响 | 处置 |
| --- | --- | --- |
| 模型不吐合法工单块 | 流程卡住 | §4.1 三级容错 + 显式错误 + 运行挂起 + 单步重试；提示词给足模板与示例 |
| 返工死循环 / token 爆炸 | 成本失控 | §3.3 三重轮次上限 + 运行预算 + 达到上限挂起等用户 |
| 能力边界只是"软约束" | CEO 仍可能写文件 | §4.3 L2/L3/L4；文档明确写"强隔离需沙箱" |
| 串行导致长流程慢 | 体验 | 已落地波次并行（§3.4）：兄弟工单 + 不同岗位可同波推进，`max_parallel` 默认 1 保持串行 |
| 与现有代码耦合 | 维护成本 | §2.4 文件级改造清单 + §2.5 守卫测试 |
| 引擎与 UI 双份事件语义 | 漂移 | 后端唯一信封协议（§5.2）；前端只做归约 |
| 技能契约改动（§2.4 第 4 条） | 影响面 | 向后兼容的可选参数；跑 `tests/test_skills.py` + 全量 pytest |

---

## 11. 分阶段实施计划

| 里程碑 | 内容 | 完成判据 |
| --- | --- | --- |
| **M0 契约冻结** ✅ | `tables.py` / `schemas.py` / `protocol.py` / `tickets.py` / `graph.py` + 单测；协作组路由；`init_db` 注册；`sse.py` 下沉 | 已完成，见 §13 |
| **M1 引擎 MVP** ✅ | `prompt.py` + `execution.py` + `engine.py` + 运行 API：`/runs/{id}/messages`（SSE）串行跑通 CEO→CTO→SE→审查（含拆子工单与一次打回返工），并支持 `cancel`/`resume`/`retry`、预算与轮次上限、`ask_user` 挂起、越权审计 | 已完成，见 §13 |
| **M2 编排设置** ✅ | 画布编辑器（拖智能体/拖节点/连线/实时校验/整图保存）+ 协作组编排入口（现在位于智能体页的「协作组」标签）；协作运行单独成页 | 已完成，见 §13（前端 `npm run build` 通过 + 后端契约已按前端调用顺序冒烟） |
| **M3 防死循环收尾** ✅ | 边级 `max_rounds`、工单树深度上限与重名子工单去重、`run.error` 与提示的统一表达 | 已完成：`test_collab_guards.py` 覆盖 §3.3 全表（4 项端到端） |
| **M4 协作运行页打磨** ✅ | 工单看板抽屉（子工单树 + 工单字段 + 产出文件 + 审查意见 + 越权审计）、顶栏预算「已用 / 上限」与工单状态摘要、编辑器里的边级返工上限 | 已完成：`npm run build` 通过 + 抽屉 SSR 冒烟（空/嵌套/脏数据）；时间线仍复用 `MessageView`（未拆出独立"工单卡"组件） |
| **M4b 用户决策卡** ✅ | `ask_user` 的 `question + options` 协议、解析与语义校验；桌面端与移动端把等待用户拍板渲染成可点选项；旧记录无选项时按默认方案/自由填写兼容；移动端外壳缓存版本 v6 | 已完成：后端协议/引擎测试；`bash scripts/test.sh` + `npm run build` |
| **M4c 领导者主会话 + 协作进度** ✅ | 主会话只显示用户与入口领导者；进度条展示岗位顺序、当前执行者、工单与连线；点岗位打开只读实时会话；流式期间即时回显；完成后可继续追问并重新打开根工单；空闲超时而非创建时长超时 | 已完成：桌面/移动端共用进度模型、两端 build、协作引擎与 API 测试 |
| **M4d 进行中岗位可见性修复** ✅ | 岗位抽屉对未产出步骤渲染「正在处理中 / 已中断」占位；进度对「运行已停但步骤还停在 streaming／被中断」的岗位标为「已中断」而非「处理中」；启动恢复同时收尾残留 `streaming` 步骤 | 已完成：`test_recover_interrupted_also_settles_streaming_steps`；`bash scripts/test.sh` + `npm run build` |
| **M4e 运行与连接解耦 + 岗位交接链** ✅ | 推进改跑后台任务（`collab/bus.py`），SSE 断开只退订、不再打断运行；新增 `GET /runs/{id}/stream` 订阅与前端自动重连；步骤记录 `target_key`（把活交给谁），岗位抽屉按时间还原「上游对我说了什么 → 我做了什么」 | 已完成：`test_client_disconnect_does_not_stop_the_run`、`test_stream_endpoint_replays_buffered_events_after_disconnect`（重连先补发再续看）、`test_delegate_step_records_target_for_handoff_chain`、`backend/tests/test_collab_bus.py`（补发 / 断线退订 / 缓冲回收）；`bash scripts/test.sh` + `npm run build` |
| **M4f 超长单行输出不再打断运行** ✅ | 执行层自建行缓冲（`runtime/runner.py` 的 `_StreamLines`）：内核把整段代码或工具结果塞进一行 JSONL 时，不再抛 「Separator is found, but chunk is longer than limit」把整轮运行判成 error；单行越过 64 MiB 上限只丢该行并补一条 `log(warn)`，stdout 与 stderr 同一条路径 | 已完成：`backend/tests/test_runner.py`（默认 `readline` 会炸的前提、整行读出、内存上限兜底、端到端超长单行跑通）；`bash scripts/test.sh` |
| **M4g 一轮多个工单块不再判死** ✅ | 同一轮里出现多个 `work_order` 块时取最后一个，并产出一条 `collab.notice(warn)` 说明偏差；草稿块不再进正文；提示词同步要求「整轮只输出一个、正文里不要粘贴草稿」 | 已完成：`test_parse_takes_the_last_block_when_a_round_has_several`、`test_round_with_two_work_order_blocks_uses_the_last_one`；`bash scripts/test.sh` |
| **M4h 收尾汇报** ✅ | 根工单完成后交回入口智能体汇报：引擎附上整次协作的结构化汇总，入口智能体用一次 `done` 把结论回复给用户；汇报轮只允许 `done`/`ask_user`；`report_at` 保证每轮只汇报一次（用户追问重新开启后清零） | 已完成：`test_root_ticket_closure_reports_back_to_entry`、`test_entry_closing_the_ticket_itself_does_not_report_twice`、`test_user_follow_up_opens_a_new_report_round`、`test_collab_migration.py` 的补列用例；`bash scripts/test.sh` |
| **M5 打磨** | ✅ 技能与 MCP 物化接入协作运行（与对话链路同一条路径）；待做：用量统计与导出、运行复制 / 模板 | 已完成部分：`tests/test_collab_provisioning.py` 端到端确认「仅分配生效」（未分配的不出现）、聊天与工单各一个隔离「家」；`bash scripts/test.sh` |
| **M6 波次并行** ✅ | 预算新增 `max_parallel`（默认 1 = 完全串行）；`delegate split` 支持 `tickets` 数组一次拆多张（每张可自带 `assignee`）；引擎按波次并行推进「同父兄弟 + 不同岗位」的工单，join 后汇总；步骤序号原子取号（`collab_runs.step_seq` 计数器）、步数原子自增、用量单点聚合 | 已完成：`tests/test_collab_parallel.py`（gate 卡位验证真并发 / 同岗位串行 / 默认行为不变）；既有引擎 32 用例全绿（串行零回归）；`bash scripts/test.sh` |

每个里程碑单独提交、单独验证（铁律 8），M1 完成前不碰前端画布。

---

## 12. 已确认的决策（评审结论）

| 议题 | 决策 | 落地位置 |
| --- | --- | --- |
| 导航形态 | **「协作」菜单只放协作运行；协作组编排放在「智能体」页的协作组标签**，主导航保持清爽 | §7.1；M2 |
| 工单协议 | **强制 `work_order` 结构化块**，三级容错解析；提示词给标准围栏 + 示例 | §4.1；M0 已实现 |
| 并发模型 | **波次并行**（默认串行）：只有同父兄弟工单、不同岗位才同波推进；同岗位串行杜绝对话通道竞态，join 后才汇总 | §3.4 |
| 审查关系 | **可选**；一旦存在 `review` 边，图校验就强制要求对应的 `rework` 回边 | §3.3、§2.5；M0 已实现 |
| 工作目录 | **一期团队共享一个目录**；文件锁 / git worktree 留作演进 | §4.3；M1 |
| 用户干预 | **一期必须支持** `waiting_user` 挂起与续跑（关键节点/出错时人能踩刹车） | §5.3、§5.4；M1/M3 |
| `sse.py` | **平移到 `app/transport/sse.py`**，成为与业务无关的通用传输设施（已落地） | §2.3、§2.5；M0 已完成 |
| JSON 容错 | 在第 1 级失败后先做**正则清理**（控制字符、智能引号、尾随逗号），再做截断补齐 | §4.1；M0 已实现 |
| 画布实现 | 四向端口（左/上入、右/下出）+ 正交避障折线；不做自动布局；前端不重复实现校验规则，一律调服务端 `validate` | §7.3；✅ M2，端口/避障已升级 |
| 节点参数 | **移除节点级参数覆盖**（V0.27 标注弃用并保留兼容读取 → V0.28 删除）：一个岗位用什么参数跑完全由它绑定的智能体决定（技能 / MCP 同理），协作层不再有只在这里生效的适配器设置；旧图里残留的 `options_override` 被忽略 | §4.3、§4.4；V0.28 |
| 团队可见性 | **每个岗位都看得见全组名册**（有谁 / 谁负责什么 / 谁是入口）：名册是图的公开结构，不是记忆，不越 C5；但**谁能指挥谁不写进名册**，仍以该轮「输出协议」为准 —— 看得见全组 ≠ 可以指挥全组 | §4.2；V0.29 |

---

## 13. 实施进度

**M0 · 契约冻结 —— 已完成。**

交付物（全部为新增文件，宿主改动共 4 处约 7 行）：

| 文件 | 内容 |
| --- | --- |
| `backend/app/collab/tables.py` | 4 张表：`collab_teams` / `collab_runs` / `collab_tickets` / `collab_steps` |
| `backend/app/collab/schemas.py` | 岗位/边类型/动作/状态枚举，节点与边的结构，预算，校验结果，请求响应模型 |
| `backend/app/collab/protocol.py` | 协作信封事件（`collab.*`），与智能体协议分属两层 |
| `backend/app/collab/tickets.py` | 工单协议：三级容错解析、语义校验、输出协议说明渲染（纯函数） |
| `backend/app/collab/graph.py` | 编排图查询与校验（纯函数，智能体存在性由调用方注入） |
| `backend/app/collab/store.py` | 协作组持久化、运行计数、启动恢复 |
| `backend/app/collab/api.py` | `/api/collab/teams` 的 CRUD + `validate` + `duplicate` |
| `backend/app/transport/sse.py` | 由 `api/sse.py` 平移而来的通用 SSE 设施 |
| `backend/tests/test_collab_tickets.py` | 29 项：解析容错、语义校验、协议说明（含「协议说明里不得写死示例 key」） |
| `backend/tests/test_collab_graph.py` | 14 项：主链通过、缺失入口、孤立节点、审查闭环、assign 成环、岗位边不相容、智能体被删… |
| `backend/tests/test_collab_api.py` | 9 项：CRUD、草稿可存但报错、`validate`、404、422、有运行记录删除返回 409、改节点 key 照常保存且已下工单的运行保留自己的图快照、未开跑的运行跟随最新图、启动回填只冻结已下工单的老运行（幂等） |
| `backend/tests/test_collab_architecture.py` | 7 条分层守卫（§2.5） |

验证（M0 当时的基线）：`bash scripts/test.sh` → **115 passed, 2 skipped** +
前端 `tsc --noEmit` 通过。

**M1 · 引擎 MVP —— 已完成。**

交付物（协作域新增 `engine/execution/prompt` 三个模块 + 运行 API；宿主动作仍是 §2.4 那几处）：

| 文件 | 内容 |
| --- | --- |
| `backend/app/collab/prompt.py` | `build_step_prompt()`：把「岗位说明 + 输出协议 + 工单字段 + 上游交付摘要 + 本工单历史 + 本轮要求」拼成一次输入（纯函数，**只读 `gist` 不读 `content`**） |
| `backend/app/collab/execution.py` | 全域**唯一**接触 `runtime`/`agents` 的边界：`prepare_step`（`history=[]`、`session_id=None`、按 `workspace_scope` 决定工作目录、按分配物化技能与 MCP）、`stream_step`、`snapshot_workspace/finish_audit`、`StepRegistry`（复用 `runtime.manager`） |
| `backend/app/collab/engine.py` | `prepare_turn`（校验图 → 落用户发言 → 建/唤醒根工单）+ `consume_turn`（预算检查 → 子工单汇聚 → 取就绪工单 → 跑一步 / 并行一波兄弟工单 → 解析动作 → 落库），`MAX_STEPS_PER_TURN=200`、`max_parallel` 默认 1 |
| `backend/app/collab/api.py` | 运行相关接口：`GET/POST /runs`、`GET/DELETE /runs/{id}`、`POST /runs/{id}/messages`（SSE）、`/resume`（SSE）、`/cancel`、`POST /steps/{id}/retry`；`GET /active-runs` 给一份「本进程里正在跑的协作智能体」汇总（工单步骤 + 入口对话，带运行标题）—— 协作的运行登记在协作域自己的句柄表里，对话域那份运行表看不到，设置页的「正在运行的智能体」要靠这条接口把两处一起列出来 |
| `backend/app/collab/tables.py` | 工单补 `owner_key/holder_key/submitter_key`，步骤补 `gist`（交付摘要的结构性落点） |
| `backend/tests/test_collab_engine.py` | 5 项端到端：完整闭环（7 步、1 次返工、`tickets_used=2`）、下游只见工单、协议违规挂起后重试续跑、入口 `ask_user` 后应答续跑、重复启动/无目标续跑被拒 |
| `backend/tests/test_collab_parallel.py` | 4 项波次并行：gate 卡位验证兄弟工单真并发（两岗位同时 streaming）、同岗位两张工单不并行、`max_parallel=1` 默认串行零回归、运行中 `/budget` 调并行度后继续即并行 |
| `backend/tests/fakes/fake_collab_agent.py` | 替身智能体：按 prompt 里的岗位 key 取剧本，可输出字符串模拟协议违规 |
| `backend/tests/test_collab_migration.py` | 2 项：给"旧库"（缺 `gist` 的 `collab_steps`）补列后旧行不变、重复启动幂等 |
| `backend/app/collab/tables.py` 的 `ADDITIVE_COLUMNS` | 后加可选列的登记处；`db.init_db` 据此幂等补列（本地真实库已实测通过） |

验证：`bash scripts/test.sh` → **125 passed, 1 skipped**（唯一 skip 是"`execution.py` 属于执行层边界"的
守卫用例，刻意跳过）+ 前端 `tsc --noEmit` 通过。

**M2 · 编排设置 —— 已完成。**

交付物（前端 7 个文件 + 3 处小改；后端零改动）：

| 文件 | 内容 |
| --- | --- |
| `frontend/src/lib/types.ts` | 追加协作域类型（节点/边/预算/校验/协作组/工单/步骤/运行/信封事件），字段名与 `schemas.py` 一致 |
| `frontend/src/lib/api.ts` | 追加 `/api/collab/**` 的 14 个方法；`streamPost` 加泛型参数（协作复用同一份 SSE 读取逻辑） |
| `frontend/src/lib/agentStream.ts` | 从 `ChatPage` 抽出的纯归约器（两页共用，行为不变） |
| `frontend/src/lib/collabStream.ts` | 协作信封事件的归约：进行中的步骤、工单、运行状态、提示 |
| `frontend/src/components/CollabCanvas.tsx` | 手写画布：拖节点、左/上输入与右/下输出端口、正交避障连线、拖放添加节点、Delete 删除 |
| `frontend/src/components/NodeInspector.tsx` | 节点/边/团队三种上下文的属性面板；节点只编辑协作语义字段（不含适配器参数） |
| `frontend/src/components/CollabSettingsPanel.tsx` | 协作组列表 + 画布 + 面板；防抖实时校验、整图保存、放弃改动 |
| `frontend/src/components/CollabRunsPanel.tsx` | 运行列表 + 时间线（复用 `MessageView`）+ 工单条 + 直接和入口智能体说话 |
| `frontend/src/pages/CollabPage.tsx` | 页签骨架 + 4 条路由 + 共享数据加载 |
| `frontend/src/App.tsx` / `index.css` / `MessageView.tsx` | `NAV` + `Routes`；协作样式；`MessageView` 加可选 `label` 显示岗位名 |

验证：`npm run build`（`tsc -b` + vite）通过；后端按**前端调用顺序**跑了一遍冒烟脚本
（建组 → validate → PUT 整图 → 发起运行 → SSE → 详情 → 列表 → 继续 → 停止 → 重试 → 删除 → 复制），
SSE 事件统计 `run.started 1 / step.started 5 / step.event 50 / step.finished 5 / ticket.created 1 /
ticket.updated 6 / run.status 1`，且 6 步时间线与 2 张工单的字段与前端类型逐一对上。

**M3 · 防死循环收尾 —— 已完成。**

| 文件 | 内容 |
| --- | --- |
| `backend/app/collab/schemas.py` | `EdgeSpec.max_rounds`（边级返工上限）；`RunRead.budget`（界面显示「已用 / 上限」） |
| `backend/app/collab/graph.py` | `rework_edge()`：取「审查者 → 打回目标」那条边，读它的 `max_rounds` |
| `backend/app/collab/engine.py` | `MAX_TICKET_DEPTH=5`、`MAX_DUPLICATE_CHILD_TITLES=2` 两个护栏；`_ticket_depth()`、`_child_count_with_title()`、`_reject_action()`（拒绝即挂起 + `run.error` + `waiting_user`）；`review/changes_requested` 分支按边级上限收紧 `ticket.max_rounds` |
| `backend/app/collab/store.py` | 运行详情/列表回传 `budget` |
| `backend/tests/test_collab_guards.py` | 端到端：边级上限（第 2 轮打回被拒 + 边字段可读写）、拆解第 5 层拒绝并提示 `handoff`、第三次重名子工单被拒、`budget` 出现在运行详情与列表；预算救援覆盖步数与 token 两种超限（调高上限 / 设为不限后 `resume` 能跑完） |

关于提示：护栏触发的说明**统一在本次推进收尾时**由 `_finalize` 发出一次（`notice(error)` + `run_status`），
不再各自重复播报；被拒的动作不会留下半截产物。

验证：`backend/.venv/bin/python -m pytest tests -q` → **131 passed, 1 skipped**。

**M4 · 协作运行页打磨 —— 已完成。**

| 文件 | 内容 |
| --- | --- |
| `frontend/src/components/TicketBoard.tsx` | 新增工单看板抽屉：左「父工单 → 子工单」树（按 `parent_ticket_id`，带状态与返工轮次），右为工单细节（`brief` 五段字段、`result.summary` 与 `artifacts`、`review_comment`、`返工 x / y`、本工单的步骤与**越权审计**）；产出物兼容「纯字符串」与后端规整后的 `{kind, path}` 两种形态，默认打开「等你 → 返工 → 待审 → 根工单」里最需要介入的那张；Esc / 点遮罩关闭 |
| `frontend/src/components/CollabRunsPanel.tsx` | 顶栏改为预算「已用 / 上限」+ 单工单返工上限 + 工单状态摘要；「📋 工单看板」入口取代横向工单条（细节归入抽屉） |
| `frontend/src/components/NodeInspector.tsx` | `rework` 边上新增「这条边的返工上限」输入（0 = 按团队默认） |
| `frontend/src/lib/types.ts` | `CollabEdge.max_rounds`、`CollabRun.budget` |
| `frontend/src/components/CollabSettingsPanel.tsx` | 载入旧图时把缺失的 `max_rounds` 补 0（前端不依赖后端一定回传新字段） |
| `frontend/src/index.css` | 抽屉样式（右侧滑出 + 两栏，窄屏降为单栏） |

验证：`npm run build`（`tsc -b` + vite）通过。另起一个临时后端（`CODA_DATA_DIR` 指向临时目录，
没碰真实库）真跑了一次协作（`done`，7 步、2 张工单、`rework` 边上限 1 生效、子工单 `round=1/1`），
把 `GET /api/collab/runs/{id}` 的**真实 JSON** 直接喂给抽屉用 `react-dom/server` 渲染，11 项断言全过：
默认选中、孤儿子工单兜底、`brief` 各字段、审查意见、`返工 1 / 1 轮`、产出文件、本工单步骤与审计结论、
「挑最该介入的那张」、以及脏数据类型（`goal` 是数字、`artifacts` 混入 `{kind:'file'}` 与 `null`）不崩。
其中 `result.artifacts` 为 `{kind: 'file', path}` 这一条正是冒烟发现的坑（原先会显示成 `[object Object]`）。

**已完成（M5 第一项）**：技能与 MCP 物化接入协作运行——按岗位分配物化进内核隔离目录，
与对话链路走同一条路径（`execution._build_context` 复用 `prepare_skill_home` / `prepare_mcp_home`），
不再向用户播报「协作运行暂未注入」。隔离「家」的作用域是「运行 × 智能体 × 通道」：
聊天通道与工单通道并行跑同一个智能体时互不覆盖。
**下一步（M5 其余项）**：用量统计与导出、运行复制 / 模板；
以及 M4 留白的时间线独立「工单卡 / 审查卡」（目前沿用 `MessageView` + `gist` 表达）。

**M4b · 用户决策卡 —— 已完成。**

| 文件 | 内容 |
| --- | --- |
| `backend/app/collab/tickets.py` | `AskOption` 与 `WorkOrder.options`；容错解析 `options/choices/suggestions`、字符串列表、`{label,value,description,freeform}`；`ask_user` 语义校验要求 `question` + 2~12 条选项，且至少一条可直接点选；运行时 `ensure_ask_options()` 可从正文项目符号/「A 还是 B」恢复选项并补兜底项 |
| `backend/app/collab/prompt.py` / `tickets.render_protocol_guide()` | 输出协议明确要求决策必须给 `options`，并为协调者追加一份 `ask_user` 示例 |
| `backend/app/collab/engine.py` / `tickets.resolve_order_targets()` | 把 `question/options` 落进 `collab_tickets.result`，`collab.notice(level=ask)` 同时携带同样字段；用户答复后续跑时清掉旧卡片，避免下一次挂起误显示。另把模型常写的岗位显示名（如「CTO」）在唯一匹配时归一成节点 key（如 `dev`），避免点完选项后继续推进时卡在 `assignee` 校验。`timeout_seconds` 改为按 `updated_at` 计算「距上次进展」，用户隔一段时间回来仍可续跑 |
| `frontend/src/lib/collabAsk.ts` | 两端共用的纯视图逻辑：规整选项；旧记录只有 `question` 时补「按你的默认方案来」（提到默认方案时）与「我来补充说明」兜底 |
| `frontend/src/components/CollabRunsPanel.tsx` / `frontend/src/mobile/pages/MobileCollabRunPage.tsx` | `waiting_user` 且工单带 `question` 时渲染决策卡；点普通选项等于发送一条用户消息，点 `freeform` 选项只聚焦输入框；详情刷新时同步工单，避免旧卡片残留 |
| `frontend/src/lib/useAutoRefresh.ts` / `frontend/src/pages/ChatPage.tsx` / `frontend/src/mobile/pages/MobileChatPage.tsx` / 协作运行页 | 桌面与移动端定时对齐会话/运行详情和列表；回到前台立即刷新；另一台设备运行中的对话暂时禁用本机输入 |
| `frontend/src/index.css` / `frontend/src/mobile/mobile.css` / `frontend/public/m/sw.js` | 桌面/移动决策卡样式；移动端外壳缓存名从 v2 升到 v4 |

验证：`backend/.venv/bin/python -m pytest tests/test_collab_tickets.py tests/test_collab_engine.py -q` →
19 passed；全量 `bash scripts/test.sh`（150 passed, 1 skipped）与 `cd frontend && npm run build` 通过。

**M4c · 领导者主会话 + 协作进度 —— 已完成。**

| 文件 | 内容 |
| --- | --- |
| `frontend/src/lib/collabProgress.ts` | 从入口节点做广度遍历得到用户可理解的推进顺序；按落库步骤、SSE 临时步骤、当前实时步骤、工单持有者与运行状态计算每个岗位的「未开始 / 执行中 / 等你决定 / 需处理 / 已完成」，并保留协作连线语义 |
| `frontend/src/components/CollabProgressBoard.tsx` / `AgentConversationPanel.tsx` | 桌面端顶栏「协作进度」按钮与全屏进度页；主线程只渲染 `role_key in (user, entry_key)`，其他智能体输出只在岗位抽屉里出现 |
| `frontend/src/mobile/components/MobileCollabProgress.tsx` / `MobileAgentConversationSheet.tsx` | 移动端横向滑动进度与全屏岗位实时会话；不引用桌面画布，符合移动端独立入口约束 |
| `frontend/src/lib/collabStream.ts` | SSE 完成的步骤先进入 `completed` 临时队列；用户发送中的消息也即时回显，详情刷新后清空临时队列并去重 |
| `backend/app/collab/engine.py` | 根工单完成后续追问会重新打开并交回入口节点；`timeout_seconds` 按 `run.updated_at` 的「距上次进展」计时；`ask_user` 漏选项时补卡片 |
| `frontend/public/m/sw.js` | 移动端外壳缓存名升到 v6，确保已添加到主屏的应用能拿到新样式与交互 |

验证：`bash scripts/test.sh`（156 passed, 1 skipped，含漏选项兜底、完成后续跑、断开不打断、派发目标记录等测试）与
`cd frontend && npm run build` 通过。

**M4f · 超长单行输出不再打断运行 —— 已完成。**

| 文件 | 内容 |
| --- | --- |
| `backend/app/runtime/runner.py` | 新增 `_StreamLines`：按块读取子进程输出并自行拆行，单行长度不受 `asyncio.StreamReader` 默认 64 KiB 上限约束；`_MAX_LINE_BYTES`（64 MiB）越界时丢弃该行并置位 `overflowed`，调用方产出 `log(warn)` 告知用户；stdout 与 stderr 都走这条路径，取消/超时/脱敏逻辑不变 |
| `backend/tests/test_runner.py` / `backend/tests/fakes/fake_long_line_agent.py` | 替身 CLI 输出一行 200 000 字符的 JSONL；用例覆盖「默认 `readline` 会炸」的前提、整行读出、内存上限兜底、端到端整轮运行完整落库 |

真实故障：`dev-2` 岗位（Claude Code 内核）在写大文件时，工具调用被放在一行 JSONL 里，
该行超过 64 KiB，`process.stdout.readline()` 抛 `ValueError` 把整轮运行判成
`error`，运行卡在 `waiting_user`，界面上只看到一句英文报错。

验证：`backend/.venv/bin/python -m pytest tests/test_runner.py -q` → 5 passed；
全量 `bash scripts/test.sh`（161 passed, 1 skipped）与 `cd frontend && npm run build` 通过。

**M4g · 一轮多个工单块不再判死 —— 已完成。**

真实故障：`dev-2`（码农）在 Claude Code 内核里先写了一份「草稿」工单块（同一轮的中间输出），
又在最终答复里补了一份完整工单块；`parse_work_order` 直接以「检测到多个 work_order 代码块」把整轮判成
`error`，运行挂在 `waiting_user`，用户只看到一条协议错误。

| 文件 | 内容 |
| --- | --- |
| `backend/app/collab/tickets.py` | 多个合法块改为**取最后一个**，并把偏差写进新的 `ParseResult.warning`；`render_protocol_guide` 的规则 2 补上「正文里不要粘贴草稿或示例」 |
| `backend/app/collab/engine.py` | 解析带 `warning` 时产出 `collab.notice(warn)`（带 `step_id`），与越权提醒同一条可见通道 |
| `backend/tests/test_collab_tickets.py` / `backend/tests/test_collab_engine.py` | 断言「取最后一个 + 有 warning」与「草稿块不进正文」；单块输出不得产生噪声 |

验证：`backend/.venv/bin/python -m pytest tests/test_collab_tickets.py tests/test_collab_engine.py -q` →
25 passed；全量 `bash scripts/test.sh`（162 passed, 1 skipped）与 `cd frontend && npm run build` 通过。

**M4h · 协作进度改为顶栏按钮 + 全屏页 —— 已完成。**

| 文件 | 内容 |
| --- | --- |
| `frontend/src/components/CollabRunsPanel.tsx` | 移除运行页内嵌进度条；顶栏新增「🧭 协作进度」按钮，显示已完成岗位数与实时输出指示点 |
| `frontend/src/components/CollabProgressBoard.tsx` | 全屏进度页：完成率环形摘要、推进中 / 等你决定 / 已完成 / 需处理统计、当前焦点、岗位卡片与协作流向；点岗位直接打开内部实时会话 |
| `frontend/src/index.css` | 进度按钮、全屏页背景、概览卡、岗位卡片状态色与流转向导样式 |

验证：桌面端 `tsc --noEmit`、`npm run build`、`bash scripts/test.sh` 通过；无头浏览器在 `1440×900`
实际点击按钮，确认页面铺满视口、5 个岗位卡片与流转关系正常渲染。

**M5 · 交付回流（上游看得见下游干完了什么）—— 已完成。**

真实故障：一次「你好，你看看现在的路径有哪些文件」的协作跑了 **23 步、11 张工单**，
`lead`（CEO）→ `dev`（CTO）→ `dev-2`（码农）反复「拆子工单 → 干完 → 退回 → 再拆一张」，
直到用户点「停止」。根因不是模型发疯，而是**信息断头**：子工单收尾后系统只把父工单退回给拆解者，
却**没有把子工单的交付一起带回去**；父工单的 `result` 始终是空的，历史里只有自己几次「我派出去了」的
记录 —— 于是每一层都把「已完成」误判成「还没回报」，重派同一件事。第二次兜底的护栏也失效：模型靠在
标题尾巴上追加几个字（「列出文件清单」→「…并回报」→「…并原样回报」）绕开了「同名」比对。

| 文件 | 内容 |
| --- | --- |
| `backend/app/collab/engine.py` | `done` 分支把 `remark`（或 `summary`）与 `artifacts` 落进工单 `result`：收尾也要留下结论，父工单汇总时不再只看到空壳；新增 `_direct_children()` / `_child_digest_lines()` / `_artifact_paths()`（与 `_run_digest` 共用取路径逻辑），把**直接子工单**压成结构化摘要；`_run_step()` 把「下游交付」与 `downstream_ready` 交给 prompt 层；`_child_count_with_title()` 改为按**近似同名**计数 |
| `backend/app/collab/prompt.py` | prompt 新增「下游交付（我派出去的子工单，引擎汇总，只有结构化字段）」区块；`expected_instruction()` 新增收尾分支：子工单全部收尾时明确要求「先汇总，能收尾就 `done`，不要为了再确认一遍重复派发同一件事」 |
| `backend/app/collab/tickets.py` | `normalize_title()` / `titles_look_duplicated()`（纯函数，可脱离数据库单测）；`render_protocol_guide()` 补一条「`done` 要给 `remark`，它是这张工单交给上游的交付摘要」 |
| `backend/tests/test_collab_engine.py` / `test_collab_guards.py` / `test_collab_tickets.py` | 7 项新用例：子工单结论回流到上游 prompt、`done` 的 `remark` 落库、完整链路里汇总方拿得到下游摘要、近似重名子工单被拦、标题归一化与近似判定、协议说明含「交付摘要」 |

验证：`bash scripts/test.sh`（pytest）→ **180 passed, 1 skipped**；
其中 `tests/test_skills.py::test_run_materializes_assigned_skills_into_isolated_home` 在本机预置了
`~/.codex/skills/.system` 的环境下会失败，**与本次改动无关**（改动前后各跑一次，同样失败）。
本轮未改前端，故未跑 `npm run build`。

面向非工程读者的图文说明另出一份单文件静态页：`docs/collab-guide.html`（无依赖，浏览器直接打开）。

**M6 · 工单块里的裸引号不再判死（自造字段也不再被丢掉）—— 已完成。**

真实故障：一次「帮我写一个建筑工程的业务记账系统…」的协作，`dev`（CTO）把子工单的详细规格
写进了 `ticket.goal`，里面有一句 `含"换电脑恢复"的具体步骤` —— 字符串正文里直接写了 ASCII 双引号，
第一个引号提前结束字符串，整块 JSON 判死，运行挂在 `waiting_user`。用户看到的报错还是三级容错里
**信息量最小**的那一句（`Expecting value`），既没有出错位置，也没说明原因。同一个工单块里还埋着第二个
坑：模型把整段规格塞进了自造的 `ticket.description` 字段，而 `TicketDraft` 只认正式字段 ——
就算 JSON 解析通过，这张子工单也会只剩一个标题，DOM 契约与验收标准**静默消失**。

| 文件 | 内容 |
| --- | --- |
| `backend/app/collab/tickets.py` | 新增 `escape_inner_quotes()` 并接进 `sanitize_json_text()`（第 2 级容错）：字符串正文里「后面不是 `,` `}` `]` `:` 也不是结尾」的 `"` 一律补转义 —— 合法 JSON 的收尾引号必然紧跟结构字符，所以对正常输出是恒等变换；`_extract()` 的报错改为**优先保留严格解析的报错**（带行列号），并在后面补一句可操作原因（「双引号没转义 / 输出被截断」）；`TicketDraft.from_value()` 接住 `description/desc/detail/details/spec/note(s)/说明/描述`：没有 `goal` 就当作 `goal`，否则并进 `requirements`；`render_protocol_guide()` 明确列出 ticket 的 6 个正式字段，从源头少犯 |
| `backend/tests/test_collab_tickets.py` | 4 项新用例：裸引号被自动补转义后照常解析、合法 JSON 不被改写、救不回来时报错带行列号与原因、`description` 不再被丢弃（进 `goal` / 进 `requirements` 两条路径） |

验证：用**线上那次真实产物**（`collab_steps.content` 原文）回放 `parse_work_order()`，由
「Expecting value」变为解析成功，且 `title / goal / deliverables` 三项都在；
`bash scripts/test.sh` → **184 passed, 1 skipped**（同一项与本改动无关的既有失败见 M5）。

**M7 · 入口智能体常驻对话（任务跑着也能找 CEO）—— 已完成。**

真实体验问题：一次协作开始推进后，主会话的发送入口会被 `409 / 等待运行结束` 挡住。
但入口智能体（CEO / 领导者）在大多数时间里其实是空闲的 —— 用户理应能继续细化要求、询问进度、
让 CEO 解释方案，甚至只是聊两句。把「协作」和「聊天」绑成同一条串行通道，是把两个生命周期
完全不同的事情硬塞进了同一个任务里。

设计选择：**给入口智能体开一条独立的、持久化的旁路会话**，而不是放宽 `/messages` 的并发限制。

| 关注点 | 处理 |
| --- | --- |
| 存储 | 新增 `collab_messages`，与 `collab_steps` 分开。聊天不会进入工单时间线、不增加 `steps_used`、不参与「谁把活交给了谁」的交接链 |
| 并发 | 新增 `chat_bus` 与 `execution.chat_registry`：聊天有独立后台任务和 SSE 缓冲区，主流程仍按当时的串行引擎推进（现为波次并行，默认串行，见 §3.4），两者互不覆盖；`/cancel` 会同时停掉两条通道 |
| 单一通道 | **所有用户消息都走聊天**（V0.43）：不再区分「推进任务 / 只聊天」两种模式。是否推进由入口智能体判断 —— 它认为需要把活干起来时在回复末尾附一个 `chat_advance` JSON 块，引擎把它带进 `collab.chat.finished` 的 `advance` 字段并落库进 `meta.advance`，前端渲染「推进任务」决策卡，用户点选后才把 `order`（或所选选项的 value）发进 `/messages` 进入工单通道。用户的答复、新任务、闲聊都从同一个输入框发出，选错模式就「没反应」的那类事故从结构上消失 |
| 记忆隔离 | 这是 C5 的**唯一例外**：聊天历史只回放给入口智能体本人。其他智能体的 prompt 仍然只由 `prompt.build_step_prompt()` 从工单结构化字段拼出，绝不读取 `collab_messages` |
| 进度问答 | CEO 拿到的是引擎生成的工单快照：标题、状态、当前持有者、目标/需求/验收、最近结论、产出文件、当前正在执行的岗位；不包含其他智能体的对话原文 |
| 细化要求 | 用户明确补充/修改要求时，CEO 在正常回复后附一个 `chat_update` JSON 块。引擎解析后**只追加**到根工单与所有未收尾工单的 `brief`（去重），从下一轮步骤开始生效；聊天本身不能直接改工单状态、派活或建子工单 |
| 失败处理 | 聊天解析失败只产生可读告警，不影响这条回复；执行失败会落库为 `error` 并推 `collab.chat.finished`；服务重启/叫停会把残留的 `streaming` 回复收尾为 `interrupted` |
| API | `POST /api/collab/runs/{id}/chat`（SSE）、`GET /api/collab/runs/{id}/chat`、`GET /api/collab/runs/{id}/chat/stream`；`GET /runs/{id}` 同时返回 `chat_messages` 供首屏恢复 |
| 事件 | `collab.chat.started` / `collab.chat.event` / `collab.chat.finished`（`advance` 字段带决策卡数据），与工单信封同属协作层，不扩展 `agents/events.py` |

| 文件 | 内容 |
| --- | --- |
| `backend/app/collab/tables.py` / `schemas.py` / `store.py` | `collab_messages` 表、`ChatMessageRead`、聊天历史/序列号、运行详情返回 `chat_messages`、重启时收尾残留聊天 |
| `backend/app/collab/prompt.py` / `tickets.py` | `build_chat_prompt()` 组装「岗位 + 工单快照 + 最近聊天 + 新消息 + 回答规则」；`parse_chat_update()` / `extract_chat_body()` / `merge_chat_update()` 是纯函数，容错与清洗复用既有 JSON 修复逻辑 |
| `backend/app/collab/execution.py` | `prepare_chat()` 复用智能体解析；聊天使用独立 `chat_registry`。主流程的 `is_running()` 只检查工单步骤，聊天不会把它顶成「正在跑」 |
| `backend/app/collab/engine.py` / `protocol.py` / `bus.py` / `api.py` | `prepare_chat()` / `consume_chat()`、聊天事件、独立 `chat_bus` 与三条聊天接口；`/chat` 不再受主流程 409 限制 |
| `frontend/src/lib/collabChat.ts` / `types.ts` / `api.ts` | 聊天事件归约器与类型；「已同步到 N 张工单」提示；`CollabEventType` 增加三种聊天事件 |
| `frontend/src/components/CollabRunsPanel.tsx` / `frontend/src/mobile/pages/MobileCollabRunPage.tsx` | 运行中输入框保持可用，聊天状态与主流程 `busy` 分开；两条时间线按时间合并；桌面与移动端都提示「CEO 在线」 |
| `frontend/public/m/sw.js` | 移动端外壳缓存名 v9 → v10 |

验证：新增端到端用例覆盖「主流程卡在审查步骤时 `/chat` 返回 200 而不是 409」「聊天 prompt 含工单状态、
结论与产出文件」「聊天原文不出现在任何工单智能体 prompt 里」「`chat_update` 写入未收尾工单并在下一轮
审查 prompt 生效」「收尾后仍可只聊天，不重开工单、不增加步数」；`bash scripts/test.sh` → **192 passed, 1 skipped**（技能隔离测试已改为容忍 Codex CLI 自己预置的 `.system` 系统技能，同时继续守住未分配技能不得出现），`npm run build` 通过。

**M8 · 误配 ask_user 与「发消息没反应」—— 已完成。**

真实事故（线上工单 `a99c538f`）：团队编辑器允许给实施者（dev-3）勾选 `ask_user`，节点契约里于是
写进了这个动作，提示词也照实告诉模型「你可以 ask_user」。模型照做后，运行层却判「只有入口节点能向
用户提问（ask_user）」把整轮中止；用户接着在输入框里问「进度怎么样了」，消息只落成一条用户发言，
界面没有任何回话。两个问题叠在一起，看起来就像「CEO 不回我」。

根因有两层：

* **配置 → 提示词 → 校验三层不一致**：`ask_user` 只有入口节点能用，但节点契约、提示词、动作白名单
  各自为政；编辑器还能把 `ask_user` 勾给任何岗位。
* **空闲消息默认走工单，且工单没有可推进项时静默退出**：`/messages` 在没有 ready 工单时直接结束，
  只留下一条用户发言；用户以为在「找 CEO 聊天」，实际走的是工单推进。

| 关注点 | 处理 |
| --- | --- |
| 契约层 | `effective_output_contract(node, allow_ask_user=...)`：非入口节点的动作里强制剔除 `ask_user`；`validate_graph` 对历史误配产出 `node.ask_user_not_entry` 提醒 |
| 提示词 | 非入口节点的「允许的动作 / 本轮要求」都不再出现 `ask_user`，统一改成「把问题交给上层或入口智能体转达」 |
| 校验层 | `validate_order` 把「只有入口节点能向用户提问」提到白名单判断之前，报错不再莫名其妙 |
| 编辑器 | 桌面与移动端：非入口节点的 `ask_user` 选项禁用，并提示只有入口节点可用；历史脏数据在下次改动时自动清掉 |
| 不再静默 | 用户发了消息但没有任何 ready 工单时，产出 `collab.notice` 说明卡在哪、去哪重试，而不是静静结束 |
| 失败可见 | 刷新运行详情时，从落库步骤重建「失败步骤」错误卡，保留一键重试入口（`makeCollabTurn(run, tickets, steps)`） |
| 空闲默认聊天 | ~~输入框默认先聊天（有待回答的问题时自动走推进）；按钮可切「推进任务 / 只聊天」~~ → V0.43 起模式切换整个移除：所有消息都走聊天，推进由 CEO 判断 + 决策卡确认 |

| 文件 | 内容 |
| --- | --- |
| `backend/app/collab/graph.py` / `tickets.py` / `prompt.py` | `allow_ask_user` 过滤、`ask_user` 错误前置、非入口提示词改写、`node.ask_user_not_entry` 校验提醒 |
| `backend/app/collab/engine.py` | 用统一 `is_entry` 驱动提示词与校验；无 ready 工单时产出明确 notice |
| `frontend/src/components/NodeInspector.tsx` / `mobile/components/CollabNodeForm.tsx` | 非入口节点禁用 `ask_user` 选项并提示 |
| `frontend/src/components/CollabRunsPanel.tsx` / `mobile/pages/MobileCollabRunPage.tsx` / `lib/collabStream.ts` | 空闲默认聊天（可切推进任务）；刷新后重建失败步骤错误卡 |
| `frontend/public/m/sw.js` | 移动端外壳缓存名 v10 → v11 |

验证：新增用例覆盖「非入口节点误配 ask_user 时提示词不含 ask_user、报错原因清晰」「用户发言在无可推进
工单时得到明确 notice 而不是静默」；`bash scripts/test.sh` → **195 passed, 1 skipped**，`npm run build` 通过。

**M9 · 上报（escalate）：卡住了要能往上走 —— 已完成。**

真实事故（线上工单 `a99c538f`）：UI（`dev-3`）实测时被权限闸门拒绝，只能把「环境禁止执行命令」
写进 `done` 的结论里。它没有别的出口 —— 实施者的生效动作只有 `done` 一个。CTO 收到后同样没有出口：
它可派给的对象只有 `dev-2`、`dev-3`（全是向下），契约里没有「上报」这个动作，`ask_user` 又只有入口
节点能用。于是它挑了唯一看起来有意义的动作 —— `delegate`，把同一件事改写成「静态核对，全程不执行
任何命令」又派给码农。阻塞原地转了一圈，白烧一轮，用户什么都没收到。

根因是**协议里根本没有「上报」这条路**：`delegate` 只朝下走，`reject` 是终止，`done` 是硬撑，
`ask_user` 被限定在入口节点。节点一旦「自己解决不了、又需要用户拍板」，就无路可走。

| 关注点 | 处理 |
| --- | --- |
| 契约层 | 新增动作 `escalate`，三个岗位默认契约都带上它；更关键的是**非入口节点无条件保留**它（保底出路，不受节点契约配置影响），入口节点则剔除（顶端无更上级，由 `ask_user` 收口）；新增状态 `escalated`，它属于「该有人动手」的就绪集合，发起人立刻能接起来 |
| 路由 | 上报给这张工单的**发起人**（`collab_tickets.owner_key`）；发起人自己也解决不了就继续往上报，直到入口节点。顶层工单的负责人沿父链上溯一层，真正到顶时给出「请改用 ask_user」的明确原因，绝不静默 |
| 收窄 | `effective_output_contract(..., escalated=True)` 剔除 `delegate`：上抛件不许把阻塞换个说法再往下压；到入口节点时再剔除 `escalate`（上面没人了，只剩转给用户） |
| 提示词 | 上抛件附一张「上报表」（谁报的、已经过谁、卡在哪、需要你什么）；非入口节点的本轮要求改为「解决不了就继续 escalate」，入口节点改为「用 ask_user 把问题转达给用户」 |
| 校验层 | `escalate` 必须给 `reason`；入口节点 `escalate` 直接报「用 ask_user」；上抛件上的 `delegate` 被白名单拦下 |
| 用户回答 | 提问的**未必是根工单**（上抛件是在发起人手里转成 `ask_user` 的），所以 `prepare_turn` 改成「重新打开最近一张在等用户回答的工单」，而不再写死根工单 |
| 可见性 | 每次上报都产出 `collab.notice`（谁把什么上报给了谁）；上报表与 `escalated` 状态在工单看板与看板徽标上可见 |

| 文件 | 内容 |
| --- | --- |
| `backend/app/collab/schemas.py` | `TicketAction.ESCALATE`、`TicketState.ESCALATED`、`TicketRead.escalation` |
| `backend/app/collab/tables.py` | `collab_tickets.escalation`（JSON，登记进 `ADDITIVE_COLUMNS`，旧库幂等补列） |
| `backend/app/collab/graph.py` | 三个岗位的默认契约加入 `escalate`；`effective_output_contract(escalated=...)` 的两条收窄 |
| `backend/app/collab/tickets.py` | `validate_order` 的 `escalate` 语义校验；协议说明与「非入口要用户拍板」的措辞改为上报 |
| `backend/app/collab/prompt.py` | 动作白名单统一取自 `effective_output_contract`（删掉重复的默认契约表）；上报表与上抛件的本轮要求 |
| `backend/app/collab/engine.py` | `escalate` 分支与 `_escalation_target()`、`_pending_question_ticket()`、就绪/活跃状态集合、`_order_gist` |
| `frontend/src/lib/{types,collabDraft,collabProgress}.ts` | 动作与状态类型、编辑器可选动作、看板标签 |
| `frontend/src/components/{TicketBoard,CollabRunsPanel}.tsx` | `escalated` 状态标签（「已上报」） |
| `frontend/public/m/sw.js` | 移动端外壳缓存名 v11 → v12 |

验证：新增用例覆盖「se → cto → ceo 逐级上报后由入口智能体 ask_user 交给用户并记录整条上报链」
「上抛件上再 `delegate` 被运行时拦下」「入口节点 `escalate` 被要求改用 `ask_user`」「escalate 缺 reason
被拒」「上抛件提示词不含 delegate」「用户回答重新打开的是提问的那张工单（非根工单）」；
`bash scripts/test.sh` → **200 passed, 1 skipped**，`npm run build` 通过。

### M10 提醒生命周期 · 进度页画成流程图（V0.1.1）

**关注点**

| 关注点 | 做法 |
| --- | --- |
| 「过时的提醒一直挂着」 | 待处理区以前是「历史失败的堆」：刷新会把它全部重建，实时产生的又永不退休。现在每条提醒有**稳定身份**（失败步骤 = `step:<id>`，其余 = 岗位 + 文本），并定下三条退休规则：① 运行已收尾（`done`/`cancelled`）就清空；② 每个岗位只看它**最后一次**参与的结果，后来的成功顶掉早先的失败；③ 同一岗位的同一条错误只留最新一条（重试指向最新那一步） |
| 临时提醒 | 审计、诊断这类没有 `stepId` 的提醒**只在当轮有效**：下一步开始时自动退休。刷新后本来就不会重建它们，两条路径行为一致 |
| 手动关闭 | 每条提醒带「×」。失败步骤类会记进本地存储（`lib/collabNotices.ts`，最多 20 个运行 × 50 条），刷新后不再重建；运行被删除时一并清掉；聊天里的一次性提示也能点掉 |
| 进度页为什么改版 | 以前是一列大卡片加一排流向标签，看不出「现在轮到谁、谁在闲置」。现在按 `orderedCollabNodes` 的推进顺序画成流程：工位之间用带动作名的箭头相连，往回走的边（review/rework）单独收在「打回与返工」里 |
| 工位上的小人 | 纯 CSS 画的（不引入图片与依赖）：头 / 眼睛 / 头发 / 身体 / 两条手臂 + 桌子 + 显示器（代码行在闪）。忙碌 = 手臂上下敲、脑袋点、屏幕闪；闲置 = 靠回椅背、屏幕暗、头顶 💤；等你决定 = 举起一只手 + ❓；出错 = 屏幕泛红 + 💥；收工 = 绿衣服 + ✅。只用现有 CSS 变量；**动画只加在「忙碌」这一种状态上**，闲置工位是静态的，不白烧 CPU |
| 两端一致、外观各表 | 桌面 `CollabProgressBoard.tsx`（全屏流程页）与移动 `MobileCollabProgress.tsx`（横向滑动条，尺寸小一号）各自写外观，**共用** `lib/collabProgress.ts` 的 `buildFlowLinks/linkBetween` 与 `lib/collabStream.ts` 的提醒规则；样式分别落在 `index.css` / `mobile.css` |
| 窄屏 | 流程图在窄屏竖排，箭头转为朝下 |

| 文件 | 内容 |
| --- | --- |
| `frontend/src/lib/collabStream.ts` | 提醒的 `key` / `nodeKey`、`noticeKey()` / `pruneNotices()` / `dismissCollabNotice()`，以及 `staleFailureNotices()` 的三条退休规则 |
| `frontend/src/lib/collabNotices.ts`（新增） | 「已关闭」记忆：本地存储读写，不可用时降级成「没有记忆」 |
| `frontend/src/lib/collabChat.ts` | `dismissChatNotice()` |
| `frontend/src/lib/collabProgress.ts` | 连线的 `fromKey` / `toKey`、`CollabFlowLink`、`buildFlowLinks()`、`linkBetween()` |
| `frontend/src/components/CollabProgressBoard.tsx` | 流程图布局 + `WorkerStation` / `FlowConnector` |
| `frontend/src/mobile/components/MobileCollabProgress.tsx` | 同款工位（小一号，横向滑动） |
| `frontend/src/index.css` / `frontend/src/mobile/mobile.css` | 工位与连线的样式（各端一份）；删掉被替换的 `.collab-progress-stage*` 死样式 |
| `frontend/src/components/CollabRunsPanel.tsx` / `mobile/pages/MobileCollabRunPage.tsx` | 提醒的「×」、关闭记忆接线、删除运行时清理记忆 |
| `frontend/public/m/sw.js` | 外壳缓存名 v12 → v13（样式表与移动端产物都变了） |

验证：`bash scripts/test.sh` → **202 passed, 1 skipped**；`tsc --noEmit` 干净；`npm run build` 通过。

> M11（V0.12）把本节这套**平面**工位（`WorkerStation` + 横向流程图 + 顶部概览卡）整块换成了
> 分层流程图与 3D 小人；M12（V0.13）又把「分层流程图」换成了等距视角的办公室（见文末）；
> M17（V0.1.3 之后）办公室动画退役，进度主视图回到**流程图节点**形态（`CollabFlowMap`，见文末 M17）。
> 下面这一节记录的只是当时的实现。

### M11 分层流程图 · 会动的 3D 小人（V0.12）

**关注点**

| 关注点 | 做法 |
| --- | --- |
| 去掉「本次协作概览」卡 | 它把完成率、当前焦点、四个状态计数又重复了一遍，与流程图和岗位卡上的信息重叠，白占一屏 → 整块删除（连 CSS 一起）。完成度只留区块标题右侧的「x/y 个岗位已完成」 |
| 按层级排布（用户要的形态） | 新增纯函数 `buildFlowRows(nodes, edges)`：入口是第 0 行（领导者独占一行），它派活的人在第 1 行，再下一层在第 2 行 —— 「上面一个、下面一排干活的」自然就出来了。判据与 `buildFlowLinks` 的 `isLoop` 同源：**只认顺着推进方向的边**，往回走的边（审查打回、返工）不改变层级，否则整行会被拽到流程末尾；没有前序的孤立岗位放到第 1 行，不与领导者挤一行 |
| 行与行之间的箭头 | 每两行之间画一条朝下的箭头，箭头上写这一层跨行的动作（`rowLinkLabels()`，同一行最多三条）；中间没有直接连线的就画虚线「流程继续」 |
| 3D 小人 | 纯 CSS 画的（无图片、无依赖），结构抽成共享组件 `components/WorkerAtDesk.tsx`，桌面端与移动端同一份：球面径向渐变 + 内侧阴影做体积，`perspective` 给显示器 / 桌面 / 键盘做透视，转椅、桌板、桌腿、键盘、咖啡杯各就各位 |
| ⚠️ 踩过的坑：`preserve-3d` | 场景与桌子**不能**开 `transform-style: preserve-3d`：身体绕 Y 轴转过之后，「离镜头更近的一侧」会赢过后面绘制的兄弟元素，桌上的键盘与手会被身体盖住。关掉它，绘制顺序回到 DOM / z-index，由我们自己决定谁在前面 |
| 手为什么单独画 | 手臂画在身体里（伸到桌子后沿就被桌子挡住），**手单独画在键盘上**并和手臂同频起伏 —— 比把手指接在手臂末端更好对齐，也顺手解决了「手被桌面吃掉」的问题 |
| 状态挂在场景根上 | 状态差异（打盹 / 敲键盘 / 举手 / 泛红 / 收工）一律写成 `.collab-station-scene.state-*`，不是挂在工位卡上：两端卡片的类名不同（`.collab-station` / `.m-station`），挂在场景根上同一套规则才能共用 |
| 忙碌 / 闲置一眼可辨 | 忙碌 = 手臂上下敲 + 手指按键盘起伏 + 屏幕里的代码一行行敲出来（`steps()` 打字机）+ 键帽轮次亮起 + 咖啡冒热气；闲置 = 趴着睡（眼睛压成一条线）+ 💤 + 屏幕暗；等你决定 = 举手 + ❓；出错 = 屏幕泛红 + 眼睛瞪大 + 抖动；收工 = 换绿衣服 + 屏幕熄灭 + ✅。**动画只加在「忙碌」上**，闲置工位不动，不白烧 CPU |
| 无障碍 | `@media (prefers-reduced-motion: reduce)` 下所有动画停下 |
| 移动端 | 同一套小人与分层规则，按触屏重排成「一行一层、行内可横向滑」；`transform: scale(0.742)` 把 210×138 的场景缩到 156×104 的卡片宽度（`.m-station .collab-station-scene`），样式在 `mobile.css` 里自成一份（移动端不加载 `index.css`） |

| 文件 | 内容 |
| --- | --- |
| `frontend/src/lib/collabProgress.ts` | `CollabFlowRow`、`buildFlowRows()`（分层）、`rowLinkLabels()`（行间箭头上的动作） |
| `frontend/src/components/WorkerAtDesk.tsx`（新增） | 3D 小人的共享结构（两端同一份 DOM，尺寸由各自样式表决定） |
| `frontend/src/components/CollabProgressBoard.tsx` | 删除概览卡；按层渲染 `collab-flow-row`；箭头改成朝下 + 动作标签；`WorkerStation` → `WorkerAtDesk` |
| `frontend/src/mobile/components/MobileCollabProgress.tsx` | 同款分层与小人，横向可滑 |
| `frontend/src/index.css` / `frontend/src/mobile/mobile.css` | 3D 工位样式（各端一份）：场景 / 小人 / 桌椅 / 显示器 / 键盘 / 手 / 杯子 + 五个状态 + 关键帧 |
| `frontend/public/m/sw.js` | 外壳缓存名 v13 → v14（样式表与移动端产物都变了） |

验证：`bash scripts/test.sh` → **202 passed, 1 skipped**；`tsc --noEmit` 干净；`npm run build` 通过；
用构建出的真实 CSS 渲染核对五个状态的桌面与移动外观（工位分层、手落在键盘上、状态动画）。

### M12 等距视角的办公室 · 热区标记（V0.13）

**关注点**：把「谁在忙、忙什么」做成一眼能看懂的实景，而不是一排卡片。

| 关注点 | 做法 |
| --- | --- |
| 为什么是等距（isometric） | 用户要 Gather.town 那种「走进办公室看一眼」的感觉。等距投影只用一个 `iso(ix, iy)` 公式就能把地砖坐标换算成画布坐标，不需要 3D 引擎 |
| 底图从哪来 | **脚本生成**，不是手画、也不是位图：`scripts/gen_office_bg.py`（纯标准库）输出深 / 浅两张 `iso-office.svg` 与 `iso-office-light.svg`。放大 2.4 倍也不糊，两端共用同一份几何（绝对路径，`/m/` 下同样取得到），前端按当前主题选图 |
| 坐标只有一份 | 工位的唯一真源是 `frontend/src/lib/collabOffice.ts` 的 `OFFICE_DESKS`；生成脚本**读它**去摆桌子，前端热区也钉在同一组坐标上 —— 桌子与标记不会错位。`backend/tests/test_frontend_assets.py` 会在两边不一致时拦下来（还校验提交的 SVG 就是当前代码的产物） |
| 工位按层摆 | `OFFICE_ROW_SIZES = [1, 1, 4, 4]`：第 0 行留给入口领导者（一人一桌，在房间最里面），第 1 行是它直接派活的人（通常是 CTO），再往外的两排并排坐干活的人。`seatFlowRows()` 把 `buildFlowRows()` 分好的层一一落到对应的桌子排上；一层的人比桌子多时从这排第一张桌重新坐起并整圈错开（`LAP_OFFSET`），标记不会叠在一起 |
| 缩放 / 平移 | 数学都在 `collabOffice.ts` 里（纯函数、可单测）：`fitOffice`（铺满居中）、`zoomOfficeAt`（**以指针为锚点**缩放，缩放前后指针下的内容不动）、`panOffice` + `clampOffice`（拖不出空白、比视口小就居中）。滚轮用**原生监听**挂 `{ passive: false }` —— React 的 `onWheel` 是 passive 的，`preventDefault` 不生效，页面会跟着一起滚 |
| 标记不跟着画布缩放 | 底图放在 `transform: translate3d() scale()` 的 `.office-map-stage` 里，标记**另算**：`officeToViewport(view, desk)` 得到视口坐标，再用 `left/top` 绝对定位。于是放大地图时标记不会变成巨无霸，名字始终清晰可读 |
| 点击弹信息卡 | 点标记弹出 `.office-popover`：谁 / 职责 / 状态、粗略进度条、`summary`、跑了多少步、几张工单在处理，以及「查看实时对话 →」（接的是原来的 `AgentConversationPanel`）。缩略图里放的还是 `WorkerAtDesk`，那个会动的 3D 小人没有白做 |
| 粗略进度是怎么来的 | 后端没有真实百分比，`agentProgressPercent()` 按状态给底数（干活中 45%、等你决定 62%、出错 30%），再按跑过的步数往上抬，停在 96% —— 界面上明写「粗略进度」，不假装精确 |
| 状态色 | 绿=在线工作（`active`，带脉冲动画）、红=忙碌 / 出问题（`error`）、黄=等你决定（`waiting`）、灰=闲置（`pending`）、绿勾=已完成（`done`）。映射表 `OFFICE_STATUS_TONE` 只有一份，两端取同一个 |
| 触屏 | 容器上 `touch-action: none`（整页外壳仍然是 `pan-x pan-y`，不锁死），`pointerdown/move/up` 里同时处理单指平移与双指捏合（按两指距离比例缩放，锚点是两指中点），`setPointerCapture` 保证手指划出画布也不丢；`tests/test_mobile_web.py` 的守卫相应收紧为「只有办公室画布能锁触摸手势」 |
| 无障碍 | 标记是 `<button>`（可 Tab、可回车）、带 `aria-label`；画布是 `role="application"` 并说明操作方式；图例把五种颜色写成文字；`prefers-reduced-motion` 下停掉脉冲与进度条动画 |

| 文件 | 内容 |
| --- | --- |
| `scripts/gen_office_bg.py`（新增） | 生成等距办公室底图：地板 / 两面墙（落地窗 + 白板）/ 地毯 / 书柜 / 饮水机 / 绿植 / 按 `OFFICE_DESKS` 摆的桌子与转椅；`box()` 是「底面落地、向上长 h」的画法，`z` 用来叠放（显示器立在桌面上、水桶坐在机身上） |
| `frontend/public/office/iso-office.svg` · `iso-office-light.svg`（新增，产物） | 深 / 浅两张底图，几何一致；每张桌子带 `data-desk` / `data-x` / `data-y` |
| `frontend/src/lib/collabOffice.ts`（新增） | 工位坐标与行布局、`OFFICE_STATUS_TONE`、缩放 / 平移 / 命中数学 |
| `frontend/src/components/CollabOfficeMap.tsx`（新增） | 画布交互 + 热区标记 + 进度信息卡（两端共用一份） |
| `frontend/src/lib/collabProgress.ts` | 新增 `agentProgressPercent()` |
| `frontend/src/components/CollabProgressBoard.tsx` | 流程图区块 → `CollabOfficeMap`；删掉 `FlowConnector` 与工位卡 |
| `frontend/src/mobile/components/MobileCollabProgress.tsx` | 同一间办公室（双指缩放 / 单指拖动），画布矮一点 |
| `frontend/src/index.css` / `frontend/src/mobile/mobile.css` | `.office-*` 样式（各端一份）；删掉被替换的 `.collab-flow*` / `.collab-station*` / `.m-station*` 死样式 |
| `backend/tests/test_frontend_assets.py`（新增） | 守卫：底图必须是最新产物、每张桌子都画了且落在地板内、行布局之和等于工位数 |
| `frontend/public/m/sw.js` | 外壳缓存名 v14 → v15（样式表与移动端产物都变了） |

验证：`bash scripts/test.sh` → **205 passed, 1 skipped**；`tsc --noEmit` 干净；`npm run build` 通过；
用构建出的真实 CSS 渲染核对办公室形态（桌椅 / 显示器 / 标记落位 / 信息卡 / 图例 / 工具条）。

### M13 工位上的 3D 小人 · 工牌 · 会走的挂钟（V0.14）

**关注点**：上一版只有「钉子一样的热区标记」，看不出有人在干活。这一版让每个工位真的坐着人。

| 关注点 | 做法 |
| --- | --- |
| 人在工位上 | 每个座位渲染一个 `CollabWorker`（和工位卡同一个 3D 小人），落在 `officeChairPoint(desk)` —— 底图那把转椅上，**整层跟着底图一起缩放**（`.office-worker > .collab-station-scene` 用 `translate(-50%, 0) scale(0.72)`，原点压在椅子落点），所以放大地图时人不会浮起来、也不会掉到桌子外面 |
| 干活时真的在动 | 状态挂在场景根上（`state-pending / active / waiting / error / done`），沿用 M11 那套关键帧：敲键盘（手臂 + 手）、呼吸、点头、眨眼、气泡浮动；**只有「干活」才动**，闲置打盹的人不动，不白烧 CPU。一排人各走各的节拍（`:nth-child` 给 `animation-delay` 错开），不会像机器人一样同频呼吸 |
| 工牌（这是谁） | 工牌挂在人的**头顶**，卡片上是「照片位 + 名字 + 职责」：名字取 `node.label`（CEO / CTO / 程序员…），职责取 `dutyLabel(node.duty)`（领导者 / 协调者 / 实施者 / 审查者），一眼看出这个工位归谁。工牌**不跟着缩放**（放大了也不会变成巨无霸）：位置按 `officeToViewport(view, 椅子点)` 算，头顶高度是 `PERSON_HEAD * zoom + BADGE_GAP`，缩放时始终贴着人 |
| 工牌分级显示 | 缩到 `LABEL_ZOOM`(0.58) 以下只剩头像气泡；到 `DETAIL_ZOOM`(0.64) 以上才把职责也写出来 —— 一排 4 个工位也不会糊成一片。点开还是那张信息卡（速度、进度条、summary、跳实时对话） |
| 挂钟按真实时间走 | 钟**面**（挂钉 + 钟壳 + 表盘 + 12 个刻度）画在底图里，圆心与半径是 `collabOffice.ts` 的 `OFFICE_CLOCK`（生成脚本读它，守卫测试比对）；钟**针**由 `components/OfficeClock.tsx` 读系统时钟画，秒针一秒一跳。钟面和指针共用同一个圆心，所以永远对得上 |
| 手机端看得清 | 窄屏默认视角改成「工位特写」：`OFFICE_FOCUS` 框住「人 + 工牌 + 桌子」那一块（原来框得太松，小人缩成一小团），桌面端仍然是「整间办公室」 |
| 谁压谁 | `.office-map-layer`（工牌）`z-index: 2` 压住图例 / 提示，`.office-map-tools`（＋ − ⤢）`z-index: 4` 压住工牌 —— 工牌永远看得见，按钮永远点得到 |

| 文件 | 内容 |
| --- | --- |
| `frontend/src/components/CollabWorker.tsx`（新增） | 只画「人」这一层（转椅 + 头 + 身体 + 手臂 + 气泡），桌子由使用方安排 |
| `frontend/src/components/WorkerAtDesk.tsx` | 改成 `CollabWorker` + 一张桌子（信息卡缩略图用） |
| `frontend/src/components/OfficeClock.tsx`（新增） | 按真实时间画挂钟的时针 / 分针 / 秒针（对齐到整秒跳） |
| `frontend/src/components/CollabOfficeMap.tsx` | 每个工位多一层「人」（跟底图缩放）+ 工牌（不缩放、分级显示）；信息卡锚到人身上 |
| `frontend/src/lib/collabOffice.ts` | 新增 `OFFICE_CHAIR_OFFSET` / `officeChairPoint()`（人坐哪）、`OFFICE_CLOCK`（挂钟圆心与半径）；`OFFICE_FOCUS` 收到工位上 |
| `scripts/gen_office_bg.py` | 新增 `read_clock()` / `clock_face()`：只画钟面；挂钟不再画指针 |
| `frontend/src/index.css` / `frontend/src/mobile/mobile.css` | `.office-worker`（人跟图缩放 + 节拍错开）、`.office-clock*`（指针）、工牌样式（照片位 / 名字 / 职责 / 状态点 + 分级显示）。这一段两端**各写一份**（移动端不加载 `index.css`），所以有守卫测试盯着「两份逐字一致」 |
| `backend/tests/test_frontend_assets.py` | 新增守卫：底图里的钟面圆心 / 半径必须等于 `OFFICE_CLOCK`、钟挂在左墙的墙面上；小人与工牌都钉在椅子上、且跟着底图缩放 |
| `backend/tests/test_mobile_web.py` | 新增守卫：两端的「协作办公室」样式段必须逐字一致（改了一边忘另一边，手机上就会走样） |
| `frontend/public/m/sw.js` | 外壳缓存名 v15 → v16（样式表与移动端产物都变了） |

验证：`bash scripts/test.sh` → **208 passed, 1 skipped**；`tsc --noEmit` 干净；`npm run build` 通过；
用构建出的真实 CSS 渲染核对：每个工位都坐着会动的小人、工牌写对名字与职责、挂钟秒针在跳，
桌面端与手机端各看一遍（手机端默认就是工位特写）。

### M14 工单看板重构：桌面三栏 · 移动端也能看（V0.15）

**关注点**：原来的看板是「子工单树 + 一坨细节」的两栏抽屉，看不出「一张工单上谁把什么交给了谁」；
而且移动端**根本没有**工单看板的入口 —— 手机上想查工单只能去协作进度里翻。

| 关注点 | 做法 |
| --- | --- |
| 三栏 | 左 25% 工单列表（标题两行截断 + 状态徽章 + 当前处理人）、中 50% 流转时间线、右 25% 静态规则；窄屏（< 1180px）自动降为单栏 |
| 时间线一条 = 一步 | 数据就是 `collab_steps`：头像取节点对应智能体的 `avatar`，标题优先用引擎的 `gist`，正文补 `content` 摘要；用户自己的发言单独一种徽章（「你」），只聊天的轮次也不会消失 |
| 动作徽章化 | 看板上只有徽章是高饱和色：派发=蓝、完成=绿、出错=红、问用户=紫，提交=青、审查=琥珀、上报=橙、否决=玫红；`error` 来自运行态 `status=error`，`note` 是没有动作的普通轮 |
| 视觉降噪 | token、耗时、工作目录指纹一律沉到卡片底部的小字；产出、审查意见、上报、调用过的工具名各自成块，不跟正文抢注意力 |
| 越权高亮 | `workspace_scope=read` 且 `audit.unchanged=false` → 整卡红框 + 红横幅 + 逐条列出 `added / modified / removed`；**可写**节点的改动只给中性青色 chip；`none`（协调者的一次性空目录）不判越权，只作中性说明 |
| 用量口径 | token 一律过 `lib/usage.ts:summarizeUsage()`，与顶栏 / 进度卡同口径（各内核字段名不同，直接相加会重复计数） |
| 移动端补齐 | `/m/collab/runs/:runId/tickets`：顶上一个原生下拉选工单（含子工单缩进与状态），下面「流转 / 工单规则」两个分段，一屏只回答一个问题；运行页的状态行与「运行操作」弹层都放了入口 |
| 一份逻辑、两套外壳 | 视图模型（`lib/ticketBoard.ts`）与卡片零件（`components/TicketBoardKit.tsx`）两端共用；卡片样式两端**各写一份**（移动端不加载 index.css），由守卫测试盯「逐字一致」；桌面三栏骨架只写在 index.css，移动端单栏外壳只写在 mobile.css |
| 词汇表收口 | 工单状态的中文标签原先在 `CollabRunsPanel` 与 `collabProgress` 各有一份，这次收归 `lib/collabDraft.ts:TICKET_STATES` 一份（顶栏、进度卡、看板共用） |

| 文件 | 内容 |
| --- | --- |
| `frontend/src/lib/ticketBoard.ts`（新增） | 纯视图模型：工单 / 步骤 → 看板条目（徽章档位、标题、交接对象、用量、作用域、审计、越权判定）、左栏筛选与「默认打开最需要介入的那张」 |
| `frontend/src/components/TicketBoardKit.tsx`（新增） | 两端共用零件：动作徽章、状态徽章、头像、工单卡、流转卡片、规则手风琴、徽章图例 |
| `frontend/src/components/TicketBoard.tsx` | 重写为三栏（列表 / 时间线 / 规则），原来看板里的产出、审查意见、上报信息全部保留；Esc 关闭 |
| `frontend/src/mobile/pages/MobileTicketBoardPage.tsx`（新增） | 移动端看板：下拉选工单 + 流转 / 规则分段；只复用 `lib/*` 与共享零件，不引用桌面页面 |
| `frontend/src/mobile/pages/MobileCollabRunPage.tsx` / `mobile/MobileApp.tsx` | 状态行与「运行操作」弹层加看板入口；注册 `/collab/runs/:runId/tickets` |
| `frontend/src/lib/collabDraft.ts` | 新增 `TICKET_STATES` / `ticketStateMeta()`：工单状态显示词汇的唯一来源 |
| `frontend/src/lib/collabProgress.ts` / `components/CollabRunsPanel.tsx` | 改用共享词汇表，删掉各自的工单状态副本 |
| `frontend/src/index.css` / `frontend/src/mobile/mobile.css` | `.board-*` 卡片样式（两端各一份，守卫测试盯逐字一致）；桌面三栏骨架只在 index.css，移动端单栏外壳只在 mobile.css |
| `backend/tests/test_mobile_web.py` | 新增守卫：两端卡片样式逐字一致、看板复用共享模型、移动端有看板页与入口；「词汇表单一来源」补上工单状态 |
| `frontend/public/m/sw.js` | 外壳缓存名 v16 → v17（移动端样式与产物都变了） |

验证：`bash scripts/test.sh` → **210 passed, 1 skipped**；`tsc --noEmit` 干净；`npm run build` 通过；
用一次真实运行（32 步 / 11 张工单）渲染核对：三栏比例、标题限行、动作徽章、底部小字元数据，
以及「只读节点却改动了工作目录」那条的红色高亮（往真实数据里插一条只读节点的越权步骤复现）；
移动端另外看了单栏看板与运行页上的入口按钮。

### M15 PWA 协作运行页 = 一间办公室 · 共享输入区 · 协作附件（V0.16）

**关注点**：协作运行页原来是「上面流程图 + 下面聊天时间线」两条并排，手机上两张都要看，
屏幕被切得很碎；底部还顶着一块输入框。这一版把它收成「主页面就是那间可缩放的办公室，
聊天点人才出来」，输入区与对话页共用同一份组件。

| 关注点 | 做法 |
| --- | --- |
| 主页面是办公室 | 协作运行页主区整屏铺 `MobileCollabProgress`（`/m/` 下那间可双指缩放 / 单指拖动的等距办公室），原来的时间线与「需要你决定」卡片不再平铺在下面 |
| 聊天点人才出来 | 点办公室里的**入口智能体**工牌弹出整屏对话弹层（`MobileSheet full`）；点其他岗位工牌仍开只读的岗位会话。**顶栏不再放对话按钮** —— 聊天界面就是「点人」这一条入口 |
| 底部只有输入框 | 输入区收成屏幕最下面那块（`.composer`，两端共用 `components/Composer.tsx`），框上方只有一行：左边是模式切换按钮，右边一句当前模式说明；「需要你决定 / 正在推进」这类状态跟在下面；平时底部干干净净 |
| 两种模式由用户切 | 输入框上方只有一个按钮：当前是「普通对话」，点一下变成「推进任务」，再点回「普通对话」（按钮上写的就是当前模式，`aria-pressed` 同步）。推进模式下把这句话接到工单上接着跑；不输入内容直接发就是「让领导者继续推进」（等价于 `POST /runs/{id}/resume`，原来那个「继续推进（不补充内容）」按钮因此取消）。有三处没法自由选，界面直接接管并把按钮置灰：工单正在推进（推进接口会 409，只能问进度）、带着附件（工单通道没有附件位，只能随对话发）、有待回答的问题（答复必须走推进，否则会被当闲聊）；任务还没开始时先不显示按钮（第一句就是任务描述） |
| 输入区只有一份 | 对话页与协作页共用 `frontend/src/components/Composer.tsx`（两端各一份样式、组件一份；受控：草稿、附件、上传、发送 / 停止都由页面给）。框里只有附件 / 语音 / 输入框 / 发送四个图标按钮，**没有 placeholder**，附件与语音的提示回给页面弹横幅 —— 改一处两端一起变 |
| 协作也能带附件 | 附件下沉成共享模块 `app/attachments.py`（校验 / 落盘 / 拼提示词），对话与协作走同一套：新增 `POST /api/collab/runs/{id}/attachments` 把文件存进这次协作的工作目录，`POST /runs/{id}/chat` 的 `attachments` 带相对路径，`engine.prepare_chat()` 用 `compose_with_attachments()` 把路径拼进 prompt 与落库内容（**只有附件、没有正文也允许**）；发消息时带附件会强制走入口智能体这条聊天通道（工单推进的输入是结构化工单，没有附件位） |
| 办公室跟着主题换色 | 底图有深 / 浅两张（`frontend/public/office/iso-office.svg` 与 `iso-office-light.svg`，同一份几何 —— 生成脚本按「反相 + 色相旋转 180°」在构建期换算，免去手机上对整张 1600×1000 图做实时滤镜），`CollabOfficeMap` 按 `lib/theme.ts` 的当前主题选；工牌 / 图例 / 工具按钮 / 信息卡的深色也改成 `--office-*` 变量（`index.css` 与 `mobile.css` 各一份，浅色只在 `[data-theme='light']` 覆盖）。手机是浅色模式时，办公室里的背景与卡片一起变浅 |

| 文件 | 内容 |
| --- | --- |
| `backend/app/attachments.py`（新增） | 附件共享模块：`decode_attachment()` / `store_attachment()` / `compose_with_attachments()`、上限 8 MB、安全文件名；对话与协作都走它，异常统一转成显式的 HTTP 状态码 |
| `backend/app/api/conversations.py` · `runtime/service.py` | 附件上传 / 拼提示词改调 `app.attachments`（原来各写一份的逻辑收口） |
| `backend/app/collab/schemas.py` / `engine.py` / `api.py` | `RunMessage.attachments`、`RunAttachmentUpload`；`prepare_chat()` 支持附件（校验落在工作目录内、拼进 prompt 与落库内容）；新增 `POST /runs/{id}/attachments` |
| `frontend/src/components/Composer.tsx`（新增） | 两端输入区共用：附件 chip、语音识别、发送 / 停止、框上方说明条、`allowEmptySend`（推进模式空发）；无 placeholder |
| `frontend/src/mobile/pages/MobileChatPage.tsx` / `MobileCollabRunPage.tsx` | 改用共享输入区；协作页主区改成整屏办公室、聊天收进弹层，输入框上方保留一个「普通对话 / 推进任务」模式按钮 |
| `frontend/src/components/CollabOfficeMap.tsx` · `scripts/gen_office_bg.py` | 按主题选深 / 浅底图；生成脚本新增 `light_color()` / `light_svg()`，同时输出两张图 |
| `frontend/src/index.css` / `frontend/src/mobile/mobile.css` | 新增 `--office-*` 变量（深色默认 + `[data-theme='light']` 覆盖），把办公室段里写死的深色改成变量；新增 `.m-composer-head` / `.m-run-stage` / `.m-run-chat-*` 等；共享段两端逐字一致 |
| `backend/tests/test_api.py` / `test_collab_engine.py` / `test_frontend_assets.py` / `test_mobile_web.py` / `test_appearance.py` | 附件落到工作目录并进 prompt、越界 / 空文件被拒；提交的浅色底图必须是最新产物；协作运行页「办公室为主、聊天点人才出来」、共享输入区与外观主题的守卫 |
| `frontend/public/m/sw.js` | 外壳缓存名 v20 → v29（样式表与移动端产物都变了） |

验证：`bash scripts/test.sh` → **246 passed, 1 skipped**；`tsc --noEmit` 干净；`npm run build` 通过；
在 390×844 的 headless Chromium 上核对：协作页深 / 浅两套主题（底部只有输入框且贴底、办公室铺满主区、
浅色底图与浅色卡片正确）、点工牌开进度卡、点「查看实时对话」开只读岗位会话、带附件发消息落库含附件说明。

### M16 单一聊天通道：是否推进由 CEO 判断 + 决策卡（V0.43）

**关注点**：协作输入区的「推进任务 / 普通对话」模式切换，把「这句话该走哪条线」的判断压在了
用户身上 —— 选错模式就是「发消息没反应」（M8 修过一次：空闲默认聊天 + 自动切回推进，但
按钮还在，误切仍然存在）。真正该做判断的是入口智能体本身：用户所有的话都先到它那里，
它来决定「聊一聊」还是「需要把活干起来」。

| 关注点 | 做法 |
| --- | --- |
| 单一通道 | 所有用户消息统一走 `/chat`（可带附件）：闲聊、问进度、补要求、新任务、答复待决策问题都从同一个输入框发出。模式切换控件、`effectiveWork` 判定、「空内容 = 继续推进」的空发语义全部移除；挂起后的续跑仍走「继续 / 重试 / 提高预算」按钮 |
| 判断协议 | CEO 聊天 prompt 的回答规则改为三分类：聊一聊 → 正常回复；补要求 → `chat_update`；需要推进 → 在回复末尾附 `chat_advance` JSON 块（`reason` 一句话理由 / `order` 建议发出的完整指令 / `options` 2-6 条可点备选）。有待回答的问题时 `order` 必须能作为对那个问题的答复直接发出 |
| 解析容错 | `parse_chat_advance()` 与 `chat_update` 同一套三级容错：解析失败只告警不判死；漏给 `options` 时从 `order` 的编号/项目符号恢复，拆不出来就补「按建议推进 / 我再想想」两张兜底卡；只给 `options` 没给 `order` 时取第一条可点选项当默认指令。`extract_chat_body()` 同时剥掉两种协议块 |
| 数据通路 | `engine.consume_chat()` 解析后把 advance 写进 `collab_messages.meta.advance` 并随 `collab.chat.finished` 的 `advance` 字段下发（`protocol.chat_finished`）；聊天本身仍是旁路 —— 卡片给出来了，但没有任何工单被推进，步数不动 |
| 决策卡 | 前端 `collabChat.ts` 归约出 `advance`，聊天结束后渲染「推进任务」卡片（品牌青调，与紫色的「需要你决定」卡区分）：点普通选项 → 把 `order` / 选项 value 发进 `/messages` 进入工单通道；点 `freeform` → 预填建议指令聚焦输入框；点「×」→ 当纯聊天关掉。刷新后从最近一条 CEO 聊天的 `meta.advance` 恢复，新一轮聊天开始时清空 |
| 首条消息 | 任务还没开始时同样先走聊天：CEO 判断这是任务描述并给出 `chat_advance`，用户点「按建议推进」才创建根工单 —— 开场也统一走「对话 → 判断 → 卡片确认」这一条路 |
| 两端同步 | 桌面 `CollabRunsPanel` 与移动 `MobileCollabRunPage` 同步改造：删掉 `idleMode` / `sendMode` 与切换控件（连同 `.collab-mode-switch` / `.m-mode-toggle` 死样式），新增 `.collab-advance-card` / `.m-advance-card` 决策卡样式 |

| 文件 | 内容 |
| --- | --- |
| `backend/app/collab/tickets.py` | `CHAT_ADVANCE_FENCE` / `ChatAdvance` / `parse_chat_advance()` / `_advance_options_from_order()`；`extract_chat_body()` 剥掉两种块 |
| `backend/app/collab/prompt.py` | `build_chat_prompt()` 的回答规则重写为三分类 + `chat_advance` 协议说明与示例；开场说明改为「由你判断」 |
| `backend/app/collab/engine.py` | `consume_chat()` 解析 advance、写 `meta.advance`、随 `chat_finished` 下发 |
| `backend/app/collab/protocol.py` | `chat_finished(..., advance=...)` |
| `frontend/src/lib/collabChat.ts` | `ChatTurnState.advance` / `chatAdvanceFrom()` / `dismissChatAdvance()`；`chat.finished` 归约出决策卡与提示 |
| `frontend/src/lib/types.ts` | `CollabEvent.advance` 类型 |
| `frontend/src/components/CollabRunsPanel.tsx` / `mobile/pages/MobileCollabRunPage.tsx` | 单一发送通道、推进决策卡渲染与点选、恢复/关闭；删模式切换 |
| `frontend/src/index.css` / `mobile/mobile.css` | 删模式切换死样式；新增推进决策卡样式（品牌青调） |
| `backend/tests/test_collab_tickets.py` / `test_collab_engine.py` / `test_mobile_web.py` / `test_office_env.py` | `chat_advance` 纯函数用例（可选/解析/兜底卡/容错）；端到端「CEO 判断 → 决策卡 → 用户确认 → 推进发生」「漏 options 自动补卡」「纯聊天不产出 advance」；移动端守卫改为「无模式切换、有决策卡」；外壳缓存 v78 → v79 |

验证：`bash scripts/test.sh` → **471 passed, 1 skipped**；`tsc --noEmit` 干净；`npm run build` 通过。

### M17 协作进度回到流程图节点视图（办公室动画退役）

**关注点**：等距办公室动画好看但**看不清进度** —— 工位坐标、小人姿势、工牌缩放这些「拟真」
占了心智，用户要的「谁做到哪了、轮到谁」反而要凑近了找。这一版把进度主视图换成参照
编排画布设计的**流程图节点视图**，拟人化基础设施（3D 小人 / 底图 / 生成脚本 / 拟人化调度）
整体拆除。

| 关注点 | 做法 |
| --- | --- |
| 节点只留必要信息 | 每个岗位一个节点：**头像 + 昵称**（职责、状态细节收进详情卡，图上不挤）；等待中的岗位半透明 |
| 状态一眼可辨 | 头像右上角一枚**状态徽标**：绿▶=执行中（呼吸动画）、黄?=提问 / 等你决定、红!=出错、灰…=等待、绿✓=完成 |
| 点头像看在做什么 | 点节点弹详情卡：当前状态、最近动作、跑了几步、在办工单、跳转该岗位实时对话 |
| 布局 | 沿用 `buildFlowRows` 的「一行一层」；节点坐标由 `layoutFlowNodes` 派生（同层均匀铺开、整层居中），不再手工维护工位坐标 |
| 连线 | 与编排画布同一套配色（assign / review / rework）；回流（打回 / 返工）画成往回走的虚线 |
| 画布交互 | 拖动平移 + 滚轮 / 双指缩放 + 复位按钮；点空白收起详情卡 |
| 无障碍 | 徽标呼吸 / 辉光在 `prefers-reduced-motion` 下静默，状态色保留 |
| 办公室退役 | 删除 `CollabOfficeMap` / `CollabWorker` / `WorkerAtDesk` / `OfficeClock` / `lib/collabOffice.ts` / `lib/collabEnv.ts` / `scripts/gen_office_bg.py` / `public/office/` 两张底图；`.office-*` / `.collab-station-scene` / `.collab-worker*` / `.collab-workdesk-*` 样式与 `--office-*` 变量一并清除 |

| 文件 | 内容 |
| --- | --- |
| `frontend/src/components/CollabFlowMap.tsx`（新增） | 流程图视图：分层布局、SVG 连线、状态徽标、详情卡、平移缩放（两端共用） |
| `frontend/src/components/CollabProgressBoard.tsx` | 桌面端进度板改用 `CollabFlowMap`；删除「打回与返工」单独区块（回流已在图上） |
| `frontend/src/mobile/components/MobileCollabProgress.tsx` | 移动端改用同一份 `CollabFlowMap` |
| `frontend/src/index.css` / `mobile/mobile.css` | 新增「协作流程图」共享段（两端逐字一致）；删除办公室与 3D 小人样式段 |
| `frontend/public/m/sw.js` | 外壳缓存名 +1（样式表与移动端产物都变了） |
| `backend/tests/test_mobile_web.py` | 守卫改为「流程图样式两端一致」「只有流程图画布锁 touch-action」「运行页主视图是流程图」 |
| `backend/tests/test_frontend_assets.py` | 删除办公室底图坐标类守卫（底图已不存在） |
| `backend/tests/test_office_env.py`（删除） | 拟人化 v2 守卫随之退役 |
| `docs/architecture.md` §11 | 重写为「协作流程图实况呈现层」 |

### M18 头像跟随 · 进度图内嵌 · 节点级技能 / MCP 临时分配

**关注点**：三件事收口 —— 协作视图的头像没跟智能体换头像；桌面端进度图开新页面切走了
对话上下文；协作任务想临时给岗位增减技能却只能改智能体的常规配置。

| 关注点 | 做法 |
| --- | --- |
| 头像跟随 | `CollabAgentProgress` 携带完整三字段（emoji / 头像包 / 自定义图）+ `agentId`；流程图节点、详情卡、岗位会话、移动端弹层统一改用共享 `AgentAvatar` 组件，换头像即时生效 |
| 进度图内嵌 | 桌面端 `CollabProgressBoard` 从全屏弹层改为 `collab-stack`（时间线 + 决策卡区域）内的覆盖层：顶栏与输入框保持可用，仍能与入口智能体对话；触发按钮变为开 / 关切换，Esc 收起 |
| 节点级分配 | `NodeSpec` 新增 `skill_ids` / `mcp_ids`（null = 跟随智能体页面；列表 = 协作专属，空列表 = 这个协作一个不给）。`runtime` 的 `skills_for_agent` / `mcp_servers_for_agent` / `prepare_skill_home` / `prepare_mcp_home` / `builtin_param_warnings` 增加 `override_ids` 透传；协作执行层 `_build_context` 按节点传入。安全边界不变：同内核、可用、归属校验；不写回智能体配置 |
| 两端对齐 | 桌面端 `NodeInspector` 与移动端 `CollabNodeForm` 都提供三态选择（跟随智能体 / 本协作不给 / 自定义），自定义弹层复用 `SkillPicker` / `McpPicker`（同一套数据源：技能 / MCP / 内核目录） |

| 文件 | 内容 |
| --- | --- |
| `backend/app/collab/schemas.py` | `NodeSpec.skill_ids` / `mcp_ids`（可选、默认 null，旧图兼容） |
| `backend/app/collab/execution.py` | `_build_context` 接收节点并把覆盖传给技能 / MCP 物化 |
| `backend/app/runtime/skills.py` · `mcp.py` | 查询与物化函数增加 `override_ids` 参数 |
| `backend/tests/test_collab_node_overrides.py`（新增） | 契约层（旧图解析 / JSON 往返）+ 执行层（覆盖透传）共 4 条 |
| `frontend/src/lib/collabProgress.ts` | `CollabAgentProgress` 头像三字段 + `agentId` |
| `frontend/src/components/CollabFlowMap.tsx` · `AgentConversationPanel` · `CollabAgentThread` · `MobileAgentConversationSheet` | 头像渲染统一走 `AgentAvatar` |
| `frontend/src/components/CollabProgressBoard.tsx` · `CollabRunsPanel.tsx` · `index.css` | 进度图改内嵌覆盖层（`.collab-stack` 锚定，保留输入框） |
| `frontend/src/components/NodeInspector.tsx` · `CollabSettingsPanel.tsx` · `pages/AgentsPage.tsx` | 桌面端节点面板加「协作专属技能 / MCP」三态区块 |
| `frontend/src/mobile/components/CollabNodeForm.tsx` · `mobile/pages/MobileCollabTeamPage.tsx` | 移动端岗位表单同步三态区块与选择弹层 |

### M19 进度图与运行顶栏只留图与按钮：文字摘要全部让位

**关注点**：打开协作任务后，顶栏把运行标题、状态、预算消耗（步数 / 工单 / 返工上限 / token）和
工单状态计数摊在一行；打开协作进度后，覆盖层顶部再铺一段「协作流程 / 协作进度 / 完成度 /
使用说明」。这些文字与流程图和工单看板上的信息重叠，纯占版面。

| 关注点 | 做法 |
| --- | --- |
| 顶栏瘦身 | `CollabRunsPanel` 的 `.collab-topbar` 只留操作按钮（协作进度开关、工单看板、停止 / 继续、提高预算、删除），运行标题与摘要移回左栏运行列表（列表本身已带状态徽标与步数 / 工单数） |
| 进度覆盖层去标题 | `CollabProgressBoard` 不再渲染标题栏（「协作流程 / 协作进度」、状态徽标、完成度、运行摘要、使用说明段一并删除）；开合由顶栏「协作进度」按钮承担，Esc 与点空白收起，详情卡由节点点击提供 |
| 画布铺满 | 去掉标题栏后，`.collab-progress-board-body` 的流程图直接 `flex: 1` 占满覆盖层；选中岗位时右侧对话面板仍按原比例分栏 |
| 移动端对齐 | `MobileCollabProgress` 的小标题与提示（「协作进度 / 拖动查看 · 点节点看对话」）同样删除，两端观感一致 |
| 清理 | 连带删除 `.collab-progress-board-head` / `-eyebrow` / `-title-row` / `-subtitle` / `-count` / `-close` / `-section*` / `-flow*` 等失去引用的样式，以及顶栏的工单状态计数（`ticketCounts`）与其词汇表引用 |

| 文件 | 内容 |
| --- | --- |
| `frontend/src/components/CollabRunsPanel.tsx` | 顶栏改为纯按钮行；进度按钮由「打开」变开合开关 |
| `frontend/src/components/CollabProgressBoard.tsx` | 删除整个标题栏与说明段，只留流程图 + 岗位对话面板；移除 `runTitle` / `teamName` / `statusLabel` / `statusPill` 四个已不使用的入参 |
| `frontend/src/components/MobileCollabProgress.tsx` · `mobile/mobile.css` | 删除小标题与提示，`.m-collab-progress-head` 样式随之移除 |
| `frontend/src/index.css` | 删除进度覆盖层标题相关样式；流程图在覆盖层内改为铺满 |
| `docs/architecture.md` §11 | 补记「界面只留图本身」 |

### M20 路由目标「源头减熵 + 克制归一 + 一次修复」

**关注点**：模型把「允许派发的对象」整串照抄进 `assignee`（真实故障：`SEO（dev）` 被原样
派发、语义校验打回、运行挂起等用户）；更早的设计里显示名一旦重名，别名映射整体失效，
连括号形式都救不回来。CLI 适配层没有 Schema / Tool 硬约束通道（`AgentContext` 无 schema
载荷、`Capabilities` 无能力位、11 个内置内核零支持），只能走「源头减熵 + 代码兜底」。

| 关注点 | 做法 |
| --- | --- |
| 源头减熵 | 协议说明在「允许派发的对象」之外单列一行**纯 key 清单**（`assignee / rework_to 的合法取值：dev、dev-2、dev-3`），id 与给人看的显示名分列；delegate 示例里的 `assignee` 一律写纯 key（示例是模型最强的模仿锚点，示例写拼接串就是教模型照抄拼接串） |
| 克制归一 | `resolve_order_targets` 接收全图 `node_keys` + `node_labels`：清洗只做 strip + NFKC；`显示名（key）` 括号形式**无条件**归一（key 自带、无歧义，显示名重名不影响）；纯 key 原样通过；裸显示名仍只认全图唯一（重名不猜）。刻意**不做**模糊正则截取：误撞另一节点的显示名会静默派错人，宁可报错阻塞 |
| 一次修复轮 | 语义校验报「不能把…」且错误目标**字面包含**某个合法 key / 显示名时，`engine._repair_round` 用 `build_repair_prompt`（只含原工单 + 错误 + 合法枚举，不带历史长对话）就地让模型重写一次；修复结果同样过归一 + 校验。预算见底不发起；编造角色（与任何合法值无字面关联）、漏字段、动作越权**绝不重试** |
| 用量与步数 | 修复轮的 token 并进本步 `usage`（共享字典），步数不另计 —— 它是同一步内的纠错，不是新的一步 |

| 文件 | 内容 |
| --- | --- |
| `backend/app/collab/tickets.py` | `clean_target_text()`（strip + NFKC）、`split_wrapped_target()` / `_key_from_wrapped()`、`_wrapped_forms()`（括号形式枚举）；`resolve_order_targets()` 增加 `node_keys` / `node_labels` 参数与括号形式无条件归一；`is_repairable_routing_error()`（含 `bad_target` 字面关联判定）；协议说明新增纯 key 清单行、示例 assignee 改纯 key |
| `backend/app/collab/prompt.py` | `build_repair_prompt()`：一次性修复轮的精简 prompt（原工单 + 错误 + 合法枚举 + 岗位头） |
| `backend/app/collab/engine.py` | `_RepairPlan` / `_repair_plan()` / `_repair_round()` / `_parse_validate()` / `_bad_routing_target()`；预算门槛检查；修复轮用量并入本步 |
| `backend/tests/test_collab_tickets.py` | 表驱动归一化用例：括号形式（重名/双侧/内侧空白）、清洗（全半角/首尾空白）、纯 key 与唯一显示名、重名与编造不猜、子工单 assignee 同路径、修复分类器（可修/不可修）、纯 key 清单渲染 |
| `backend/tests/test_collab_engine.py` | 端到端：拼写类错误就地修复后整链跑完（修复 prompt 只含枚举）；编造角色直接 BLOCKED 不发起修复轮 |
