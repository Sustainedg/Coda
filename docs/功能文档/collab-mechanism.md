# 协作机制与功能手册

> 适用对象：想搞清楚「协作到底怎么跑、能做什么、不能做什么」的使用者，以及要改协作代码的工程师。
>
> 事实基线：Coda `V0.1`（`backend/app/__init__.py`），提交 `7c6c54c` + 当前工作区未提交改动。
> **代码是唯一事实来源**；本文与旧设计文档冲突时，以本文标注的「代码实际行为」为准，偏差清单见 §15。
>
> 相关文档：`docs/计划任务/collaboration-design.md`（设计与里程碑）、`docs/功能文档/collab-parallel.md`（波次并行专题）、
> `docs/功能文档/collab-guide.html`（面向使用者的图文版）、`docs/架构/architecture.md`（平台运行时序与事件协议）、
> `AGENTS.md`（工程铁律）。

---

## 0. 一分钟速查

| 你想知道 | 答案 |
| --- | --- |
| 协作是什么 | 把多个「智能体」按一张有向图排成岗位，用**工单**在它们之间交接任务，引擎**波次**推进（默认串行） |
| 智能体之间共享聊天记录吗 | **不会**。每一步都是全新会话，只拿到工单字段 + 结构化摘要 + 文件路径 |
| 谁跟用户说话 | 只有**入口节点**（必须是协调者）。其他岗位的输出只在岗位抽屉/看板里 |
| 同一时刻能跑几个步骤 | 默认 **1 个**；预算里 `max_parallel > 1` 时，同一父工单拆出、不同岗位的**兄弟工单**可同波并行（同一岗位永远串行） |
| 任务怎么开始 | 先建运行（空目标），用户在聊天框说第一句话；CEO 判断后给「推进任务」卡，点选后才真正开跑 |
| 卡住了怎么办 | 挂起为 `waiting_user` 并给红字原因；可「继续 / 重试 / 提高预算」，不会静默空转 |
| 会不会无限返工/无限拆单 | 有返工轮次、拆解深度 5 层、同名子工单 2 张、步数/工单/token/空闲时限四类预算 |
| 能并行吗 | 不能（一期设计如此） |
| 能强制隔离文件系统吗 | 不能。`none/read` 靠提示词 + **事后**越权审计，不是沙箱 |
| 能在聊天里直接改工单吗 | 不能。聊天只能追加需求（`chat_update`）或建议推进（`chat_advance`），流转仍走工单协议 |

---

## 1. 全景：一次协作由哪些东西组成

```
用户
 │  ① 在聊天框说话（所有消息都先进聊天通道 /chat）
 ▼
入口智能体（coordinator，唯一能 ask_user 的岗位）
 │  ② 判断：聊一聊 / 追加需求 / 需要推进
 │     需要推进 → 输出 chat_advance → 前端渲染「推进任务」决策卡
 │  ③ 用户点选 → 指令进入工单通道（POST /messages）
 ▼
根工单（Ticket）
 │  ④ 引擎取就绪工单 → 交给持有者节点 → 该节点跑一步（max_parallel>1 时兄弟工单同波并行）
 │     每步：拼 prompt → 起内核 → 流式事件 → 解析 work_order → 落库 → 改工单状态
 ▼
delegate(handoff/split) / submit / review / done / ask_user / escalate / reject
 │  ⑤ 子工单全收尾 → 父工单回到拆解者汇总
 │  ⑥ 根工单 done → 交回入口智能体做「收尾汇报」→ 运行 done
 ▼
用户看到结论 + 工单看板 + 协作进度（流程图实况）
```

三个必须记住的词：

- **协作组（Team）**：一张可视化编排图。节点 = 岗位，边 = 允许的流转。
- **岗位（Node）**：某个智能体在这张图里的角色（协调者 / 实施者 / 审查者）与权限。
- **工单（Ticket）**：智能体之间**唯一**的交接物。不是聊天记录，是结构化任务卡。

---

## 2. 概念与数据模型

### 2.1 七个概念

| 概念 | 说明 | 落库位置 |
| --- | --- | --- |
| 协作组 Team | 一张图 + 默认工作目录 + 默认预算 | `collab_teams` |
| 节点 Node | 图上的岗位：绑定智能体，声明 duty / 职责 / 能力边界 / 目录范围 / 动作集合 / 返工上限 / 坐标 | `collab_teams.graph.nodes[]` |
| 边 Edge | 有向流转：`assign` / `review` / `rework` | `collab_teams.graph.edges[]` |
| 运行 Run | 一次协作实例（左侧列表的一项，类比「对话」） | `collab_runs` |
| 工单 Ticket | 一次可交接的工作单元，可挂父工单形成拆解树 | `collab_tickets` |
| 步骤 Step | 时间线上的一个条目（某岗位在某工单上的一轮发言），用户发言也存这里 | `collab_steps` |
| 聊天消息 ChatMessage | 用户 ↔ 入口智能体的一对一对话，**唯一**允许携带历史原文的地方 | `collab_messages` |

### 2.2 五张表的关键字段

```text
collab_teams   id, name, description, entry_key, workspace_dir,
               graph(JSON), budget(JSON), owner_id, created_at, updated_at

collab_runs    id, team_id→teams RESTRICT, title, goal, status,
               entry_key, workspace_dir, graph(JSON), budget(JSON), usage(JSON),
               steps_used, tickets_used, error, owner_id, created_at, updated_at

collab_tickets id, run_id→runs CASCADE, parent_ticket_id→tickets SET NULL,
               from_agent_profile_id, to_agent_profile_id,
               owner_key, holder_key, submitter_key,
               title, brief(JSON), state, round, max_rounds,
               verdict, review_comment, result(JSON), escalation(JSON),
               report_at, created_at, updated_at, closed_at

collab_steps   id, run_id→runs CASCADE, ticket_id→tickets SET NULL,
               agent_profile_id, role_key, duty, sequence, action, target_key,
               status, content, gist, parts(JSON), usage(JSON), error, audit(JSON),
               started_at, finished_at

collab_messages id, run_id→runs CASCADE, sender(user|leader), role_key,
                agent_profile_id, sequence, status, content, parts(JSON),
                usage(JSON), error, meta(JSON), created_at, finished_at
```

三条设计取舍（读代码时必须知道）：

1. **`brief` / `result` / `usage` / `audit` / `meta` 都是 JSON**：工单字段会演进，加字段不需要迁移表结构。
2. **后加列登记在 `collab/tables.py:ADDITIVE_COLUMNS`**：SQLite 的 `create_all` 不会给旧表补列，`db.init_db` 会按登记做幂等 `ALTER TABLE ... ADD COLUMN`。**加新列必须同时登记**，否则旧库启动报 `no such column`。
3. **`owner_id` 归属隔离**：`collab_teams` / `collab_runs` 有 `owner_id`；工单与步骤经 `run_id` 间接归属。老库由 `bootstrap.backfill_ownership()` 回填给首任管理员。

### 2.3 引用键：`key` 是稳定标识，不是显示名

工单（`owner_key` / `holder_key` / `submitter_key` / `escalation`）、步骤（`role_key` / `target_key`）、
运行（`entry_key`）、聊天（`role_key`）里存的全是**当时的节点 key**。因此：

- **运行在第一次推进（下发第一张工单）时冻结整张图**（`collab_runs.graph`），引擎每轮只读这份快照。
  改协作组（改 key / 删节点 / 换边 / 换入口）只影响还没开跑 / 之后新建的运行，已下工单的运行继续按
  自己的图跑完；保存不再返回 409。
- 还没开跑的运行不拍快照，仍跟着协作组最新图 —— 所以「先建任务、再回去修图」也能救回来。
- 升级前没有快照、且已经下过工单的老运行：启动时由 `store.backfill_run_graphs()` 用协作组当时的图补一份；
  保存协作图前也会先给它们补拍，保证改图不会打断任何已有工单。
- 提示词里**不得写死示例 key**：模型会把示例当答案抄进 `assignee`（历史事故，见 `test_protocol_guide_never_bakes_in_a_node_key`）。

---

## 3. 编排图：能画什么、不能画什么

### 3.1 三种岗位（`Duty`）

| 岗位 | 默认动作集合 | 默认工作目录范围 | 语义 |
| --- | --- | --- | --- |
| `coordinator` 协调者 | `delegate` / `ask_user` / `escalate` / `done` / `reject` | `none`（不给目录） | 只组织、派活、拍板，不写代码 |
| `engineer` 实施者 | `submit` / `delegate` / `escalate` / `reject` | `write` | 真正动手 |
| `reviewer` 审查者 | `review` / `escalate` / `reject` | `read` | 只读，只给审查意见 |

### 3.2 三种边（`EdgeKind`）与两端岗位约束

| 边 | 允许的起点岗位 | 允许的终点岗位 | 语义 |
| --- | --- | --- | --- |
| `assign` | coordinator / engineer | coordinator / engineer | 派发或交付 |
| `review` | engineer | reviewer | 提交给审查者 |
| `rework` | reviewer | engineer / **coordinator** | 打回；允许退回上级重新拆解 |

`rework` 终点允许协调者，是因为需求原话就是「审查意见交回 CTO，CTO 再交给 SE 完善」。

### 3.3 图校验规则（`graph.validate_graph`）

**硬错误（errors）—— 发起运行会被拒绝：**

| code | 规则 |
| --- | --- |
| `graph.empty` | 至少一个节点 |
| `node.duplicate_key` | 节点 key 唯一 |
| `node.agent_missing` | 节点绑定的智能体存在（且归当前用户） |
| `edge.duplicate_id` | 边 ID 唯一 |
| `edge.self_loop` | 边不能自己连自己 |
| `edge.dangling` | 边两端节点都存在 |
| `edge.duty_mismatch` | 边的两端岗位符合 §3.2 |
| `entry.missing` / `entry.not_found` / `entry.duty` | 必须指定唯一入口，且必须是 `coordinator` |
| `node.orphan` | 非入口节点必须有入边 |
| `graph.no_engineer` | 至少一个实施者 |
| `edge.review_without_rework` | 有 `review` 边，审查者就必须有 `rework` 出边 |
| `edge.rework_without_review` | 有 `rework` 边，就必须有对应的 `review` 边 |
| `edge.assign_cycle` | `assign` 边不允许成环（循环只能走 review/rework 显式闭环） |

**提醒（warnings）—— 能跑，但可能不是你想要的：**

| code | 含义 |
| --- | --- |
| `node.unreachable` | 从入口走不到 |
| `edge.rework_target_not_submitter` | 打回目标不是提交者，且审查者有多条退路（必须显式 `rework_to`） |
| `node.ask_user_not_entry` | 非入口节点勾了 `ask_user`（会被忽略） |
| `node.multiple_rework_targets` | 审查者多条退路，打回时必须指明目标 |
| `node.reviewer_unused` | 审查者没收到 review 边 |
| `node.reviewer_no_rework` | 审查者没有 rework 出边 |

**保存与校验的差别**：`PUT /teams/{id}` **不拦语义错误**（允许存草稿，编辑器要能边画边存），
但结构性错误（不是合法 JSON 形状 / 类型不对）由 Pydantic 直接 422。**发起运行**时才用 `validate_graph` 拒绝。

### 3.4 有效动作契约（`graph.effective_output_contract`）

这是「提示词说的」和「运行时允许的」**唯一共同来源**，三条规则：

1. `ask_user` **只有入口节点**能用；非入口节点即使勾了也会被剔除。
2. `escalate` **非入口节点无条件保留**（哪怕契约里没勾）—— 它是「我搞不定」的保底出路，
   不能被配置成死路；入口节点则剔除（它在顶端，没有上级可报，由 `ask_user` 收口）。
3. 工单处于 `escalated`（被下游上报上来）时**剔除 `delegate`**：上抛件只能「自己解决 / 继续往上 / 交给用户」，
   不许把阻塞换个说法再往下压（真实事故：CTO 把「跑不了」改写成「静态核对」又派给码农，白转一轮）。

### 3.5 预算（`BudgetSpec`）

| 字段 | 默认 | 约束 | 说明 |
| --- | --- | --- | --- |
| `max_steps` | 200 | ≥1 | 总步数上限 |
| `max_tickets` | 30 | ≥1 | 总工单数上限 |
| `max_rounds_per_ticket` | 3 | ≥1 | 单工单返工上限（节点/边/预算三者取最小） |
| `max_tokens` | 0 | ≥0 | 0 = 不限 |
| `timeout_seconds` | 3600 | ≥30 | **空闲时限**（距上次进展），不是从创建起累计 |
| `step_timeout_seconds` | 36000 | ≥0 | 单步限时；**0 = 不限**（执行层转成 -1 交给 runner） |

---

## 4. 运行生命周期

### 4.1 运行状态

| 状态 | 含义 | 怎么进入 |
| --- | --- | --- |
| `running` | 正在推进 | `prepare_turn` / 推进开始 |
| `waiting_user` | 等你介入 | 预算超限、协议错误、返工超限、拆解超限、`ask_user`、无可推进工单 |
| `done` | 全部收尾 | 根工单 `done`（且已完成收尾汇报） |
| `error` | 真错误 | 持有者不在图里、步骤准备失败（智能体被删/停用）、动作执行异常 |
| `cancelled` | 当前步骤被内核取消 | 停止时内核以 `cancelled` 收尾，且本轮流在 `_finalize` 前跑完 |
| `interrupted` | 被叫停 / 服务重启 | `/cancel` 对 `running` 的运行直接标 `interrupted`；启动恢复也标它 |

> 注意：`/cancel` 与「步骤自己被取消」存在时序竞争 —— 接口先把运行标成 `interrupted`，
> 而正在跑的那一轮 `_finalize` 可能随后写 `cancelled`。两者都表示「停下来了、数据保留、可继续」。
> 图不合法不会把运行置 `error`，而是在 `prepare_turn` 抛 `TurnError` → HTTP 409。

### 4.2 工单状态机

```
open ──(被领取)──▶ working ──submit(有审查者)──▶ review ──pass──▶ done
  ▲                   │                          │
  │                   │ split                    │ changes_requested
  │                   ▼                          ▼
  │            waiting_children               rework ──submit──▶ review …
  │                   │
  └──(子工单全 done/rejected)──┘

任意非终态 ──escalate──▶ escalated ──(发起人处理)──▶ …
任意非终态 ──reject────▶ rejected（分支终止）
任意非终态 ──ask_user──▶ blocked（等用户回答）
任意步骤失败/护栏触发 ──▶ blocked + 运行 waiting_user
```

**就绪队列**（引擎每一轮取哪张工单）：`open` / `working` / `review` / `rework` / `escalated`，
按 `(created_at, id)` 排序，取第一张（`READY_STATES`）。

**汇总规则（谁拆的谁汇总）**：`waiting_children` 的工单，其**直接子工单全部** `done` 或 `rejected` 时，
自动回到 `owner_key` 手里（变 `open`）。

### 4.3 收尾汇报（根工单 done 之后）

根工单 `done` 后，引擎会把它重新交回**入口智能体**，附上整次协作的结构化汇总（每张工单的标题 / 状态 /
交付摘要 / 产出文件），要求它用几句话向用户讲清结论，然后用 `done` 收尾。

三条护栏：

1. **只对根工单**：子工单各归各的发起人。
2. **最后拍板的就是入口智能体本人时不汇报**（它那一步已经把话说给用户了）。
3. **每轮只汇报一次**：`collab_tickets.report_at` 记时间；用户追问重新打开根工单时清零，下一轮收尾再汇报。

汇报轮的 prompt 里动作被收窄为 `ENTRY_REPORT_ACTIONS = (done, ask_user)`，**绝不允许重新派活**。

### 4.4 续跑与追问

- `POST /runs/{id}/resume`：不追加用户消息，从就绪队列继续。
- 用户回答某个提问后：优先重新打开**最近一张带 `question` 的 `blocked` 工单**（未必是根工单 ——
  上抛件是在发起人手里转成 `ask_user` 的），交回其当前持有者。
- 运行已 `done` 后用户继续说话：重新打开根工单（清 `closed_at` / `verdict` / `report_at`），
  历史工单与产出保留，后续派活继续挂在同一棵树上。
- 每次用户发言都会落一条 `role_key="user"` 的步骤，进时间线。

---

## 5. 工单协议（核心）

### 5.1 智能体必须输出什么

每个节点在一轮发言的**末尾**必须附**一个** `work_order` 围栏代码块，内容是 JSON：

````
（这里给人看的说明文字，进入时间线卡片正文）

```work_order
{
  "action": "delegate",
  "mode": "split",
  "assignee": "se",
  "ticket": {
    "title": "登录页改造为暗色主题",
    "goal": "在不改动后端的前提下支持暗色主题",
    "requirements": ["新增主题开关", "持久化用户选择"],
    "acceptance": ["刷新后主题保持", "对比度符合 WCAG AA"],
    "constraints": ["只改 frontend/src/**"],
    "deliverables": ["src/theme.ts"]
  },
  "summary": null,
  "artifacts": [{"kind": "file", "path": "src/theme.ts"}]
}
```
````

### 5.2 七种动作的完整语义

| action | 谁能用 | 必填 | 工单变化 | 下一个持有者 |
| --- | --- | --- | --- | --- |
| `delegate` + `mode=handoff` | coordinator / engineer | `assignee` | 同一张工单换人；新字段与旧字段**合并**；状态 `working` | `assignee` |
| `delegate` + `mode=split` | coordinator / engineer | `assignee` + `ticket.title` | 新建子工单；父工单 `waiting_children`，`owner_key` 记为拆解者 | 新建子工单的 `assignee` |
| `submit` | engineer | `summary`（建议）+ `artifacts` | 有 `review` 出边 → `review` 并交给审查者；无 → 直接 `done` | 审查者 / 结束 |
| `review` + `verdict=pass` | reviewer | `verdict` | `done` | 父工单发起人 / 运行收尾 |
| `review` + `verdict=changes_requested` | reviewer | `verdict` + `review_comment` | `rework`，`round += 1`，记审查意见 | `rework_to`；缺省退回 `submitter_key`；只有一条退路则用它 |
| `done` | 默认只有 coordinator；engineer 仅当自定义契约含 `done` | `remark`（强烈建议，作为交付摘要） | `done`；`remark`/`summary` 写进 `result.summary` | 父工单发起人 / 收尾汇报 |
| `ask_user` | **仅入口节点** | `question` + 2~12 条 `options`（至少 1 条可直接点选） | `blocked`，`result.question` / `result.options` 落库；**持有者不变**，用户答复交回它 | 用户（答复后回到提问者） |
| `escalate` | 任意非入口节点（无条件保留） | `reason`（必须） | `escalated`，记 `{from, reason, need, hops}` | 这张工单的发起人（`owner_key`）；沿父链往上找，已到顶则入口节点 |
| `reject` | 三种岗位默认都有 | 建议 `reason` | `rejected`，分支终止 | 父工单发起人 |

**`options` 的形状**（`ask_user` 与 `chat_advance` 通用）：

```json
[
  {"label": "先做最小闭环", "value": "先做材料与人工台账，报表下一阶段", "description": "最快可用"},
  {"label": "我来补充", "value": "", "freeform": true}
]
```

- `freeform: true` 表示点它**不发送**，只把焦点交给输入框。
- 解析器容忍 `["A","B"]`、`[{label,value}]`、`{"A":"说明"}` 等写法（`_ask_options`）。

### 5.3 解析容错（`tickets.py`）

三级依次尝试，任何一级成功即停：

1. **严格 JSON**：` ```work_order ` 围栏内容（围栏缺失/被截断时按「到文本末尾」处理）。
2. **正则清理后解析**：去 BOM 与控制字符、把中文智能引号 `“ ” ＂` 还原成 `"`、修补字符串正文里**未转义**的
   双引号、迭代清掉尾随逗号。
3. **扫描正文里平衡的 `{...}`**（先内层后外层），仍失败则**截断补齐**：从后往前回退到最后一个完整 `}`，
   或补齐未闭合的 `{` / `[`（必要时先补被截断的引号），再回退到最后一个逗号重试。

其他容错与约定：

- **一轮出现多个合法块 → 取最后一个**，并产出 `collab.notice(warn)` 说明偏差。工具型内核（Claude Code 等）
  会把中间草稿和最终答复写进同一段输出，一律判死会让整条链路白跑。
- 解析后再做**语义校验**（`validate_order`），任一不通过 → 该步 `error`、工单 `blocked`、运行 `waiting_user`。
- **路由目标归一化**（`resolve_order_targets`）：`assignee` / `rework_to` / 子工单 `assignee` 在校验前先归一 ——
  清洗只做 strip + NFKC（全半角括号、全角空格）；「显示名（key）」这类**自带 key** 的括号形式
  **无条件**归一（key 本身无歧义，显示名重名也不影响）；纯 key 原样通过；裸显示名只在
  **全图唯一**时映射，重名不猜。刻意不做模糊正则截取：误撞另一个节点的显示名会**静默派错人**，
  宁可报错阻塞。
- **路由拼写错误的一次性修复轮**（`engine._repair_round`）：语义校验报「不能把…」且**错误目标里字面
  包含**某个合法 key / 显示名时，用只含「原工单 + 错误原因 + 合法枚举」的精简 prompt
  （`prompt.build_repair_prompt`，不带历史长对话）就地让模型重写一次；修复结果同样过归一 + 校验，
  仍不对则按原错误落 `blocked`。编造角色（与任何合法值无字面关联）、漏字段、动作越权**绝不重试**。
- 模型把长说明写进 `description` / `desc` / `detail` / `说明` 等字段时会被接住（没有 `goal` 就当 `goal`，
  否则并进 `requirements`），不会静默丢失。
- `ask_user` 漏给 `options` 时，`ensure_ask_options` 会从问题正文的项目符号或「A 还是 B」句式恢复，
  至少补「按你的推荐方案继续」+「我来补充说明」，保证不会变成一张只有协议错误的死路。

### 5.4 交付载荷：下游到底看到什么

`prompt.build_step_prompt` 按模板拼，**只读结构化字段**：

```text
[协作任务] 协作组「X」· 你的岗位：实施者（se）
职责：…
能力边界（必须遵守）：…
工作路径（本次协作唯一允许访问的目录）：/abs/path
路径权限（必须遵守）：…

—— 协作组名册（系统提供的团队结构，只读）——
  1) CEO（ceo）· 协调者 · 入口，唯一和用户对话的智能体
  …
  名册只说明团队里有哪些人、谁负责什么；你能把工单派给谁、谁审你、你能打回给谁，一律以「输出协议」为准。

输出协议：
1. 先写给人看的说明（可用 Markdown）。
2. 最后必须附一个 work_order 块 …
…（按岗位生成规则与示例，示例里的 assignee 取本次运行真实的派发对象）

—— 工单 T-xxxxxxxx ——
标题 / 目标 / 需求 / 验收标准 / 约束 / 交付物
—— 上报：下游卡住了，需要你处理（仅 escalated 时出现）——
—— 上游交付摘要：summary + 变更文件（仅 result 非空时）——
下游交付（我派出去的子工单，引擎汇总，只有结构化字段）（仅存在子工单时）——
本工单历史（仅这一张工单，最多 8 条，取 gist）——
本轮要求：…（按岗位、工单状态、是否收尾汇报、下游是否就绪生成）
```

**结构性隔离（不靠自觉）**：

- `ctx.history = []`、`ctx.session_id = None`：每步都是全新内核会话。
- 工单历史取 `collab_steps.gist`（引擎从工单字段派生的摘要），**不读 `content`** —— 对话原文没有那条数据通路。
- 只带**直接子工单**的汇总（最多 12 行），子树细节沿每一层自己的 `result` 逐级带上来。
- **唯一例外**：全组名册。它是用户自己画的公开结构，不是任何智能体的记忆；但不声明「谁能指挥谁」，
  那由该轮的输出协议给出。

---

## 6. 聊天通道（CEO 常驻联络）

### 6.1 为什么单独一条通道

`collab_messages` 是协作域里**唯一**允许携带历史原文的地方，只回放给入口智能体本人（最近 20 条）。
工单步骤的 prompt **永远不读这张表**。

聊天与工单推进**并行**：`prepare_chat` 刻意不碰工单状态、不检查主流程是否在跑。
两个通道各有独立的执行句柄表（`registry` / `chat_registry`）与独立的 SSE 总线（`bus` / `chat_bus`）。

### 6.2 三分类判断

聊天 prompt 要求入口智能体把每条用户消息归入三类：

| 类别 | 行为 | 协议块 |
| --- | --- | --- |
| 问进度 / 聊方案 / 闲聊 / 澄清 | 正常回复 | 无 |
| 明确补充或修改要求 | 正常回复 + 追加 | `chat_update` |
| 需要把活干起来（新任务 / 继续 / 答复待决策问题） | 正常回复 + 追加 | `chat_advance` |

### 6.3 `chat_update`：追加需求

```json
{
  "goal": "可选：只有目标确实变化时才写",
  "requirements_add": ["新增要求"],
  "acceptance_add": ["新增验收标准"],
  "constraints_add": ["新增约束"],
  "deliverables_add": ["新增交付物"]
}
```

- 合并规则（`merge_chat_update`）：列表字段**去重追加**，不覆盖已有条目；`goal` 只有明确给出才替换。
- 同步范围：根工单与所有**未收尾**工单（`ACTIVE_STATES`：open/working/waiting_children/review/rework/escalated/blocked）。
- 同步结果写进消息 `meta.update` / `meta.applied`，并随 `chat_finished` 的 `applied` 下发；
  前端据此在 CEO 消息旁挂一张「已同步到 N 张工单（改了哪些字段）」小卡片，点开进工单看板。
- **正在执行的那一步已经拿到指令，新要求从下一步生效**。

### 6.4 `chat_advance`：推进决策卡

```json
{
  "reason": "一句话：为什么这需要推进任务",
  "order": "建议发出的完整指令（点选后原样作为用户消息发出）",
  "options": [
    {"label": "按建议推进", "value": "…", "description": "可选说明"},
    {"label": "我来补充", "value": "", "freeform": true}
  ]
}
```

- 有**待回答的问题**（工单 `blocked` 且 `result.question` 非空）时，`order` 必须能直接作为该问题的答复。
- 漏给 `options`：从 `order` 的编号/项目符号恢复；拆不出来就补「按建议推进 / 我再想想」两张兜底卡。
- 只给 `options` 没给 `order`：取第一条可点选项的 `value` 当默认指令。
- 解析失败只告警，不判死整条回复。
- 数据通路：`consume_chat` 把 advance 写进 `collab_messages.meta.advance`，随 `chat_finished.advance` 下发。
  **聊天本身是旁路**：卡片给出来了，但没有任何工单被推进，步数不动。
- 消费：用户点普通选项 → 把 `order` / 选项 `value` 发进 `/messages`；点 `freeform` → 预填建议指令并聚焦；
  点「×」→ 当纯聊天关掉。刷新后从最近一条 CEO 聊天的 `meta.advance` 恢复。
- 引用决策卡发指令时带 `use_advance: true`（`POST /runs/{id}/messages` 的 `RunMessage` 字段）：
  `content` 仍是完整指令、照原样执行，但时间线只落一句「已按决策卡的建议推进」的短标记 ——
  指令在 CEO 的消息里已经写过，重发一遍既占屏幕又多花一遍 tokens。自由输入自己写的指令不带它。
- 旧卡清理（当前工作区新增）：指令真正进入工单通道时，`prepare_turn` 调 `_consume_chat_advances`，
  清掉 `meta.advance` 并记 `advance_consumed`；前端还有 `lib/collabAdvance.ts` 的本地「已消费」记忆兜底，
  以及 `advanceOvertakenByTicketSteps`（有更晚的用户步骤就认为卡已被消费）。

### 6.5 聊天附件

- `POST /runs/{id}/attachments`：base64 上传，落到 `<工作目录>/attachments/<时间戳>-<随机>-<安全文件名>`，
  单文件上限 8 MB。
- 发消息时把相对路径写进提示词（`📎 附件（已存入工作目录，可直接读取）：`），因此任何能读工作目录的内核都自动支持。
- `GET /runs/{id}/files/{rel_path}`：只读回传工作目录内的文件（附件预览、岗位产出的图片）；目录穿越一律拒绝。

---

## 7. 预算、护栏与防死循环

| 护栏 | 常量/来源 | 触发后 |
| --- | --- | --- |
| 总步数 | `budget.max_steps` | 挂起 `waiting_user`，提示「提高预算」 |
| 总工单数 | `budget.max_tickets` | 同上 |
| token | `budget.max_tokens`（0 不限） | 同上 |
| 空闲时限 | `budget.timeout_seconds`（按 `run.updated_at`） | 挂起，提示距上次进展超时 |
| 单次推进步数 | `MAX_STEPS_PER_TURN = 200` | 暂停，提示稍后点「继续」 |
| 单工单返工轮次 | `min(节点 max_rounds, 预算 max_rounds_per_ticket, 边 max_rounds)` | 工单 `blocked`，运行挂起，说明已返工 N 轮 |
| 拆解深度 | `MAX_TICKET_DEPTH = 5` | 拒绝本次 split，工单 `blocked`，提示改用 `handoff` |
| 同名子工单 | `MAX_DUPLICATE_CHILD_TITLES = 2` | 拒绝本次 split，提示先汇总收尾或换标题 |
| 上抛件下派 | `effective_output_contract(escalated=True)` | 运行时判越权，该步 `error` + 挂起 |

**「近似同名」判定**（`titles_look_duplicated`）：归一化（去空白与中英文标点、转小写）后，
**完全相等 / 互相包含 / 公共前缀 ≥ 80%** 都算同一件事。只比完全同名不够 ——
模型最常靠「在标题尾巴上加几个字」绕开（真实故障这样反复了 8 轮）。

**任何护栏触发都是「整体拒绝」**：不会先建子工单再挂起，也不会留下半截状态。

**预算救援**：`POST /runs/{id}/budget` 只允许**调大**（新值必须大于当前已用量；`max_tokens=0` 表示不限），
成功后清掉「上限 / 运行时限」类的挂起原因，再 `/resume` 从原处续跑。

---

## 8. 工作目录、权限与越权审计

### 8.1 工作目录怎么定

优先级：**本次运行显式指定 > 协作组默认 > 按运行 ID 自动分配**
（默认 `settings.workspaces_root / f"collab-{run_id}"`）。

- 协作组 / 运行填的 `workspace_dir` 必须是**绝对路径**（允许 `~` 展开）；留空 = 用默认。
- 所有岗位**统一 cwd** = 本次运行的工作目录；`workspace_scope` 只约束读写权限与审计。

### 8.2 三层能力边界

| 层 | 机制 | 性质 |
| --- | --- | --- |
| L1 提示词 | `capability_scope` + `workspace_scope` 说明 + 输出契约 | 软约束 |
| L2 工作目录 | 所有岗位拿到同一个绝对路径作为 cwd | 路径统一，不是隔离 |
| L3 越权审计 | `none` / `read` 节点运行前后对共享目录做指纹快照（路径 + 大小 + mtime + 小文件内容哈希），比对出新增/修改/删除 | **事后发现**，不是事前阻止 |

审计结论写进 `collab_steps.audit`，并由引擎发一条 `collab.notice(warn)`；工单看板会把越权整条标红并列出被改文件。

**诚实声明**：L3 是事后发现而非阻止；真正的强隔离需要容器/沙箱，属于后续议题。
`workspace_scope=none` 的节点仍然拿到同一个 cwd（只是提示词禁止读写），不要把它当成「没有目录权限」。

### 8.3 技能与 MCP

- 与对话链路**走同一条路径**：`prepare_skill_home` → `prepare_mcp_home`，按岗位绑定的智能体分配物化。
- 隔离「家」的作用域是 **运行 × 智能体 × 通道**：`{run_id}-{profile_id}-step` 与 `{run_id}-{profile_id}-chat`，
  聊天与工单并行跑同一个智能体时互不覆盖。
- 严格仅分配：未分配的技能 / MCP 不出现在隔离目录里。

---

## 9. 执行层（`collab/execution.py`）

这是协作域里**唯一**接触 `runtime` / `agents` 的模块（有架构守卫）。

| 函数 | 作用 |
| --- | --- |
| `workspace_root(run)` / `ensure_workspace(run)` | 解析并创建本次运行的工作目录 |
| `_resolve_profile` | 读 `AgentProfile` + `Gateway`，解密凭据，取适配器定义与 options |
| `_build_context` | 组装 `AgentContext`（`history=[]`、`session_id=None`），物化技能与 MCP |
| `prepare_step` / `prepare_chat` | 准备一次工单步骤 / 一次聊天；步骤额外做只读节点的运行前快照 |
| `stream_step` | 把单步限时写到 `handle.timeout_seconds`，调 `runtime.runner.stream_run` |
| `finish_audit` | 只读节点的运行后比对 |
| `registry` / `chat_registry` | 句柄表（复用 `runtime.manager.ActiveRun`），供取消与「正在跑什么」查询 |
| `active_runs()` | 本进程里正在跑的协作智能体（工单 + 聊天），供设置页「正在运行的智能体」 |

**单步限时的翻译**：预算里 `0 = 不限`，而 runner 的语义是「正数限死 / 负数不限 / 0 = 这次没指定」，
所以 `_runner_step_timeout` 把 0 转成 **-1**。全局 `run_timeout_seconds` 默认为空（对话域不限时），
但只要部署方给对话配了护栏，这个翻译就不能省——否则协作域的单步「不限」会被全局值顺带掐断。

**取消**：`cancel_run` 置 `cancel_event` 并 kill 子进程；当前步骤落库 `cancelled`，未完成工单保留。
`cancel_step` 存在但**当前没有 HTTP 路由**使用它（只有整运行取消）。

---

## 10. API 全表

前缀 `/api/collab`（`backend/app/collab/api.py`）。

### 10.1 协作组

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/teams` | 列表（含节点数与实时校验结果） |
| POST | `/teams` | 新建（可空图），201 |
| GET | `/teams/{id}` | 详情：元信息 + 完整图 + 校验结果 |
| PUT | `/teams/{id}` | 整图保存；删改仍被历史运行引用的 key → 409 |
| POST | `/teams/{id}/validate` | 只校验不保存（编辑器实时校验用） |
| POST | `/teams/{id}/duplicate` | 复制，201 |
| DELETE | `/teams/{id}` | 删除；有运行记录 → 409 |

### 10.2 运行

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/runs?team_id=` | 运行列表 |
| GET | `/active-runs` | 本进程正在跑的协作智能体（带运行标题） |
| POST | `/runs` | 创建运行 `{team_id, title?, workspace_dir?}`，201；目标由第一条消息给出 |
| GET | `/runs/{id}` | 详情：工单 + 步骤摘要 + 聊天 + `chat_active` |
| GET | `/runs/{id}/steps/{step_id}/parts` | 单步完整 `parts`（懒加载） |
| DELETE | `/runs/{id}` | 删除（级联工单与步骤），204 |
| POST | `/runs/{id}/cancel` | 停止（工单步骤 + 聊天回复都取消） |
| GET | `/runs/{id}/stream` | **SSE**：订阅实时事件（断线重连 / 换设备围观） |
| POST | `/runs/{id}/messages` | **SSE**：用户对入口智能体下达推进指令 / 回复提问 |
| POST | `/runs/{id}/resume` | **SSE**：从就绪队列继续，不追加消息 |
| POST | `/runs/{id}/budget` | 只调高预算 |
| POST | `/steps/{step_id}/retry` | 把该步所在工单放回队列（随后 `/resume`） |

### 10.3 聊天与附件

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/runs/{id}/chat` | 聊天历史（推进期间也可读） |
| GET | `/runs/{id}/chat/stream` | **SSE**：订阅正在生成的 CEO 回复（先补发再续看） |
| POST | `/runs/{id}/chat` | **SSE**：和 CEO 说话（可带附件；不受主流程是否在跑影响） |
| POST | `/runs/{id}/attachments` | base64 上传附件，201 |
| GET | `/runs/{id}/files/{rel_path}` | 回传工作目录内的文件（只读） |

### 10.4 常见状态码

| 码 | 场景 |
| --- | --- |
| 404 | 协作组/运行不存在，或**不属于当前用户**（不说 403，避免泄露存在性） |
| 409 | 正在推进 / CEO 正在回复；协作组下还有运行（删除时）；预算只能调大；图不能跑 |
| 422 | 请求体结构非法；节点引用了不属于自己的智能体；附件不是合法 base64 |
| 413 | 附件超过 8 MB |

---

## 11. 实时事件协议（`collab.*`）

与适配器协议（`agents/events.py` 的 10 种事件）**分属两层**，互不干扰；协作信封全部以 `collab.` 为前缀。

| 事件 | 载荷 | 用途 |
| --- | --- | --- |
| `collab.run.started` | `run_id, team_id, title, entry_key` | 建立运行上下文 |
| `collab.step.started` | `step_id, run_id, ticket_id, node_key, duty, agent_name` | 时间线插入「正在处理」 |
| `collab.step.event` | `step_id, event`（内嵌原始 AgentEvent） | 逐字流式渲染 |
| `collab.step.finished` | `step_id, status, action, usage, error` | 卡片收尾 |
| `collab.ticket.created` | `ticket` | 看板新增 |
| `collab.ticket.updated` | `ticket` | 看板更新 |
| `collab.run.status` | `status, reason` | 运行终态 / 等待用户 |
| `collab.notice` | `text, level, question?, options?, step_id?` | 预算、审计、解析失败、`level=ask` 决策卡 |
| `collab.chat.started` | `message_id, run_id, agent_name, role_key` | 聊天开始 |
| `collab.chat.event` | `message_id, event` | 聊天逐字流 |
| `collab.chat.finished` | `message_id, status, usage, error, warning, applied, advance` | 聊天收尾 + 决策卡 |

**传输细节**：

- SSE 由 `app/transport/sse.py` 统一封装，每 15 秒一行 `: keep-alive` 心跳；
  心跳**不会取消**正在等待的事件（避免长思考被静默掐断）；流末尾补 `{"type":"stream.closed"}`。
- gzip 中间件显式**豁免** `text/event-stream`，否则事件会被攒到响应结束才下发。
- 推进跑在 `collab/bus.py` 的独立后台任务里，SSE 只是订阅者：**客户端断开只退订，不打断运行**；
  重新订阅会先补发本轮已产生的事件（每运行最多缓存 4000 条），再续看实时增量。

---

## 12. 前端

### 12.1 页面与路由

| 路由 | 内容 |
| --- | --- |
| `/collab` | 协作运行：左栏运行列表，主区时间线 + 工单条 + 输入框 |
| `/collab/runs/:runId` | 直接打开某次运行 |
| `/agents?tab=collab` | 协作组卡片列表（新建 / 点卡片进入编排） |
| `/agents?tab=collab&team=:teamId` | 画布编排器（可刷新还原） |
| `/collab/settings*` | 旧地址，重定向到 `/agents?tab=collab*` |

移动端（`/m/`）：

| 路由 | 内容 |
| --- | --- |
| `/collab` | 协作运行列表 |
| `/collab/teams/:teamId` | 表单式编排（**不引用桌面画布**） |
| `/collab/runs/:runId` | 主区整屏是流程图实况；点入口节点弹出聊天弹层，点其他岗位开只读岗位会话 |
| `/collab/runs/:runId/tickets` | 移动端工单看板 |

### 12.2 协作运行页能做什么

- **单一聊天通道**：所有消息都发进 `/chat`，没有「普通对话 / 推进任务」模式切换（选错模式就是「发消息没反应」）。
- **顶栏**：组名、状态徽章、步数/工单/返工上限/累计 token、工单状态摘要；
  `🧭 协作进度`、`📋 工单看板`、`■ 停止`、`⤴ 提高预算`、`▶ 继续`、`删除`。
- **主会话只显示用户 ⇄ 入口智能体**（`role_key in (user, entry_key)`），其他岗位的输出只在岗位抽屉里。
- **决策卡**：
  - 「需要你决定」（紫色，来自工单 `blocked` + `result.question`）；
  - 「推进任务」（品牌青，来自 `chat_advance`）；
  - 点普通选项 = 发一条用户消息；点 `freeform` = 聚焦输入框；点「×」= 当纯聊天关掉。
- **提醒（notice）**：有稳定身份（失败步骤用 `step:<id>`，其余用「岗位 + 文本」），
  三条退休规则：运行收尾就清空；每个岗位只看最后一次结果；同一条只留最新一条。
  失败类可「重试这一步」，也可手动「×」并在本地记住。
- **跨设备同步**：运行中每 5 秒对齐详情、挂起/完成后 20 秒；列表 15 秒；切回前台立即刷新；
  本机 SSE 推进期间暂停轮询。刷新时 `syncTurnWithRun` 会**保住本端流式状态**，不会把正在输出的步骤打回空壳。

### 12.3 工单看板（`TicketBoard` / `ticketBoard.ts`）

三栏：左「父工单 → 子工单」树（状态 + 返工轮次）· 中「流转时间线」（一步一行，动作徽章：
派发 / 提交 / 审查 / 完成 / 提问 / 上报 / 否决 / 出错 / 记录 / 你）· 右「工单规则」（目标 / 要求 / 验收 / 约束 / 交付物）。

- 默认选中「最需要人介入」的那张：卡住 → 返工中 → 待审查 → 根工单 → 第一张。
- 越权审计整条标红并列出被改文件；token、耗时压在小字里。
- 桌面与移动端共用 `lib/ticketBoard.ts` 的视图模型（徽章配色、越权判定只有一份）。

### 12.4 协作进度 / 流程图实况

- 布局**优先还原编排画布保存的版式**（节点 `x`/`y` 原样照搬，连线交给与协作组设定画布
  同一个路由器 `lib/collabRouting.routeEdge` —— 端口方向、避障、多条线错开同源，两边画出来一致）；
  旧图没存坐标时回退 `buildFlowRows` 的「一行一层」自动分层（入口领导者独占第一行，往下每行并排；
  `rework` 回边在图上画成虚线）。
- 「🧭 协作进度」打开全屏流程图：每个岗位一个节点（头像在左、昵称与状态在右），头像右上角的**状态徽标**
  区分状态（绿▶=执行中、黄?=等你决定、红!=出错、灰…=等待、绿✓=完成）。
- 点节点弹「现在具体在做什么」的详情卡（状态、最近动作、步数、在办工单），并能跳到
  该岗位的实时对话；画布可拖动平移 / 滚轮缩放。
- 滚轮/双指缩放、拖动平移、右下角一键复位；点工牌弹信息卡（谁 / 在忙什么 / 粗略进度 / 步数 / 在处理的工单）。

---

## 13. 多用户、鉴权与归属

- `CODA_AUTH_ENABLED` 默认开启；鉴权关闭时 `current_user` 返回虚拟管理员，所有归属检查跳过。
- 协作组、运行按 `owner_id` 隔离；工单/步骤经运行间接归属。
- 非管理员：
  - 列表只返回自己的协作组/运行；
  - 图里只能引用**自己的**智能体（否则运行时会用到对方的网关密钥/凭据），保存与创建运行都会拦（422）；
  - `GET /active-runs` 也只列自己的。
- 资源不属于当前用户时返回 **404**（不返回 403，避免泄露「存在但无权」）。

---

## 14. 明确不能做什么（能力边界清单）

**机制层面**

1. **默认串行**（`max_parallel=1`）：同一时刻只推进一个步骤；调高后只有「同父兄弟工单 + 不同岗位」能同波并行，同一岗位永远串行。
2. **不能共享记忆**：智能体之间只有工单，没有聊天记录；每个步骤都是全新内核会话。
3. **不能自由流转**：必须输出 `work_order` 块并通过语义校验，没有「随便说句话就推进」的路径。
4. **不能跨图**：一张运行只对应一个协作组，下过工单后用的是第一次推进时冻结的图快照；运行中途改协作组不会改变这次运行的图（想换图请新建运行）。
5. **不能把历史 key 交给新图**：运行/工单/步骤里记的是当时的节点 key；老运行靠自己的图快照继续，改协作组的 key 不会迁移到已有运行里。
6. **不能把预算调小**：只能调高，且必须大于当前已用量。
7. **不能强隔离文件系统**：`none` / `read` 是提示词约束 + 事后审计，不是沙箱。
8. **不能中途注入新指令到正在跑的步骤**：聊天里追加的要求从下一步生效。
9. **不能在聊天里直接改工单状态 / 派活 / 建子工单**：只能 `chat_update` 追加需求或 `chat_advance` 建议推进。
10. **不能让非入口节点问用户**：`ask_user` 只在入口节点生效；其他岗位只能 `escalate` 往上报。
11. **不能让上抛件原地往下派**：`escalated` 状态下 `delegate` 被剔除。
12. **不能让入口节点 `escalate`**：它是顶端，需要用户拍板时用 `ask_user`。
13. **不能让审查者单方面「通过」而没有退路**：有 `review` 边就必须有 `rework` 边。
14. **不能让 `assign` 成环**：要循环只能走 review ⇄ rework 显式闭环。
15. **不能拆超过 5 层**，同一父工单下同名/近似同名子工单不能超过 2 张。
16. **不能无限推进**：`MAX_STEPS_PER_TURN = 200`，预算与轮次上限会挂起。
17. **不能在没有目标时开跑**：`resume` 会明确拒绝「请先告诉入口智能体你想做什么」。
18. **不能同时开两轮推进**：`/messages` 与 `/resume` 在推进中返回 409（可直接围观实时输出）。
19. **不能让 CEO 并发回复**：上一条还在生成时再发聊天返回 409。
20. **不能把「停止」当成回滚**：停止只取消当前步骤，已收尾的工单、文件与结论都保留。

**平台/实现层面**

21. **协作运行不做版本级自动更新/回滚**：那是平台自更新（`selfupdate.py`）的事，与协作无关。
22. **协作运行不会自动发 IM 消息**：IM 域（`app/im/`）只桥接对话域，与协作域没有代码耦合。
23. **节点没有适配器参数覆盖**：一个岗位用什么参数跑，完全由它绑定的智能体决定（旧图的 `options_override` 被忽略）。
24. **步骤详情不返回完整 `parts`**：只给 `parts_count` / `tool_names`，完整流水要单独懒加载。
25. **流程图实况只读**：节点不可拖动重排（布局优先还原编排画布保存的坐标，旧图无坐标时由层级派生）；
    交互只有点节点看详情卡与「查看实时对话」入口。
26. **移动端不画线**：协作组编排用表单（岗位卡片 + 「从谁到谁 + 类型」下拉），与桌面端共用同一套字段语义与校验接口。

---

## 15. 已知限制与文档偏差

### 15.1 代码与旧设计文档不一致的地方（以代码为准）

| 项 | 旧文档说法 | 代码实际行为 |
| --- | --- | --- |
| 协作是否注入 MCP | `architecture.md` §9 写「协作域暂不注入 MCP」 | **已经注入**：`execution._build_context` 调 `prepare_mcp_home`，`test_collab_provisioning.py` 有端到端断言 |
| 工单表数量 | 多处写「4 张表」 | **5 张**：多一张 `collab_messages`（聊天通道），架构守卫也按 5 张检查 |
| 就绪队列 | 设计文档写 `{open, rework}` | 实际是 `READY_STATES = {open, working, review, rework, escalated}` |
| 聊天消息角色 | 文档只写 user / leader | 代码一致；但**用户发言也会落 `collab_steps`**（`role_key="user"`），主会话合并展示两者 |
| 停止后的运行状态 | 文档多处写 `cancelled` | `/cancel` 实际把 `running` 的运行标成 **`interrupted`**；只有步骤被内核取消时运行才是 `cancelled` |
| 单步限时默认 | 文档写 36000 秒 | 代码一致（`BudgetSpec.step_timeout_seconds = 36000`），但 0 表示不限需执行层转 -1 |
| 实施者能否 `done` | 设计文档表格写 `done` 给「协调者 / 实施者」 | `DUTY_OUTPUT_CONTRACT` 里实施者默认只有 `submit/delegate/escalate/reject`，发 `done` 会被判越权；只有节点自定义了 `output_contract` 含 `done` 时才允许（`prompt.expected_instruction` 也据此在无 `submit` 时建议 `done`） |

### 15.2 真实限制

- **并行下的写冲突**：只有「同父兄弟 + 不同岗位」的工单才同波推进，同一岗位串行；步骤序号与步数在数据库层原子计数，用量在波次收口后单点聚合。
- **越权审计有上限**：最多扫 2000 个文件；大于 512 KB 的文件只比大小 + mtime，不比内容哈希。
- **审计可能漏判**：审计只比对快照，`none` 节点仍拿到 cwd；真正的隔离需要沙箱。
- **`escalate` 的目标解析**：沿 `owner_key` 与父链往上找「不是我的节点」；一路到顶则要求改用 `ask_user`。
- **`reject` 的工单会终止分支**：父工单汇总时它算「已收尾」（`FINISHED_STATES` 含 `rejected`），不会自动重开。
- **`report_at` 是每轮一次的开关**：如果用户在汇报轮又提问，根工单重新打开后才会再次汇报。
- **SSE 缓冲只在内存**：服务重启后本轮缓冲消失，靠落库详情续看（`recover_interrupted` 会把残留收尾）。
- **附件单文件 8 MB**：走 base64，大文件请直接放进工作目录。
- **移动端流程图实况只读**：布局优先还原编排画布保存的坐标（旧图无坐标时由层级派生）；聊天收进弹层。

---

## 16. 排错手册

| 现象 | 常见原因 | 怎么处理 |
| --- | --- | --- |
| 发消息没反应，只多一条用户发言 | 有工单卡在 `blocked`，没有可推进的工单 | 打开工单看板看受阻原因；点错误卡片「重试这一步」；或直接问 CEO 进度 |
| 运行 `waiting_user`，红字「产出不符合工单协议」 | 模型没吐合法 `work_order`，或动作越权 | 修模型/人格/剧本，点「重试这一步」再「继续」 |
| 红字「已达步数/工单/token 上限」 | 预算用尽 | 「⤴ 提高预算」调高（必须大于已用量）再「继续」 |
| 红字「已返工 N 轮仍未通过」 | 返工轮次上限 | 放宽节点/边/团队上限，或自己接手 |
| 红字「第 5 层…请改用 handoff」 | 拆解太深 | 让上游改用 `handoff` 直接交给实施者 |
| 红字「已经拆出过 N 张同名子工单」 | 复读式拆解 | 让上游先看「下游交付」并汇总收尾，或换真正不同的事 |
| 红字「只有入口节点能向用户提问」 | 非入口节点配了 `ask_user` | 改图：让它 `escalate` 往上报，由入口节点问用户 |
| 红字「这张工单没有可接手的上一级」 | 上抛到顶 | 让入口节点用 `ask_user` 交给用户 |
| 「继续」之后立刻又停住 | 同一处没修好（密钥、目录权限、协议） | 看红字；同一原因会一直停在同一张工单 |
| 岗位抽屉一直「正在处理中」 | 服务重启残留 `streaming` | 启动恢复会把它们收尾成 `interrupted`；点「继续」重跑 |
| 报 `cannot be used with root/sudo privileges` | 以 root 运行且岗位勾了 `bypassPermissions` | 换非 root 运行，或调整该智能体的权限模式 |
| 刷新后旧决策卡又冒出来 | 旧数据 `meta.advance` 未清 | 当前工作区已修：指令进入工单通道时清 `meta.advance`，前端另有本地「已消费」记忆兜底 |
| 看不到某个岗位的输出 | 主会话只显示用户 ⇄ 入口智能体 | 打开「🧭 协作进度」，点该岗位工牌看只读岗位会话 |
| 工单看板某条翻红 | 只读/无目录节点改动了工作目录 | 这是越权审计；看列出的文件，必要时调整该节点 `workspace_scope` 或提示词 |

---

## 17. 代码地图与扩展指引

```
backend/app/collab/
  __init__.py     对外只暴露 recover_interrupted
  tables.py       5 张表 + ADDITIVE_COLUMNS（后加列登记）
  schemas.py      枚举、节点/边/预算、请求响应模型
  graph.py        ★ 纯函数：图查询与校验、有效动作契约、有效目录范围
  tickets.py      ★ 纯函数：work_order / chat_update / chat_advance 解析、语义校验、协议说明渲染
  prompt.py       ★ 纯函数：step prompt 与 chat prompt 组装、名册、本轮要求
  engine.py       ★ 编排状态机：prepare_turn / consume_turn / consume_chat / 动作应用 / 收尾汇报
  execution.py    ★★ 唯一执行层边界：智能体解析、上下文、技能/MCP 物化、审计、句柄表
  store.py        持久化：协作组 CRUD、运行读写、启动恢复、key 引用检查
  protocol.py     collab.* 信封事件
  bus.py          后台推进任务 + 每运行事件缓冲 + 可重入订阅
  api.py          APIRouter(prefix="/collab")，薄层

frontend/src/
  pages/CollabPage.tsx                 运行页骨架（路由参数 runId）
  components/CollabRunsPanel.tsx       运行列表 + 顶栏 + 时间线 + 决策卡 + 输入区
  components/CollabSettingsPanel.tsx   协作组列表 + 画布 + 属性面板
  components/CollabCanvas.tsx          手写画布（拖动 / 四向端口 / 正交避障连线）
  components/NodeInspector.tsx         节点 / 边 / 团队属性面板
  components/TicketBoard.tsx           三栏工单看板
  components/CollabProgressBoard.tsx   全屏协作进度（流程图 + 岗位对话侧栏）
  components/CollabFlowMap.tsx         流程图视图（节点 + 状态徽标 + 详情卡，两端共用）
  components/CollabAgentThread.tsx     岗位只读实时会话
  lib/collabDraft.ts                   草稿与字段语义（两端共用）
  lib/collabStream.ts                  信封事件归约（步骤 / 工单 / 提醒）
  lib/collabChat.ts                    聊天归约（live / notice / advance）
  lib/collabAsk.ts                     决策卡视图（含旧记录兜底）
  lib/collabAdvance.ts                 推进卡的本地消费记忆
  lib/collabProgress.ts                进度模型（岗位状态 / 流程图 / 连线）
  lib/collabNotices.ts                 提醒「已关闭」本地记忆
  lib/ticketBoard.ts                   看板纯视图模型（两端共用）
  mobile/pages/MobileCollabRunPage.tsx 移动端运行页（流程图 + 聊天弹层）
  mobile/pages/MobileCollabTeamPage.tsx 移动端表单式编排
  mobile/pages/MobileTicketBoardPage.tsx 移动端工单看板
```

**扩展指引（与 `AGENTS.md` §4.8 一致）**：

- 新增动作/状态：改 `schemas.py`（枚举）+ `engine._apply_action`（流转）+ `graph.effective_output_contract`（谁可用）
  + `tickets.validate_order`（语义校验）+ `prompt.expected_instruction`（本轮要求）+ 前端 `collabDraft.ACTIONS`
  与 `ticketBoard.BOARD_TONES`，并同步 `docs/计划任务/collaboration-design.md`。
- 纯逻辑只放 `tickets.py` / `graph.py` / `prompt.py`（有「必须是纯函数」的架构守卫）。
- 对 `runtime` / `agents` 的调用只能加在 `execution.py`（守卫会红）。
- 给协作表加列必须登记 `ADDITIVE_COLUMNS`（`test_collab_migration.py` 守着）。
- **不要**为了让协作跑通去改适配器或 `runtime/`。
- 改完跑：`bash scripts/test.sh`（pytest + 前端 `tsc --noEmit`）；前端改动还要 `npm run build` 并重启服务。

---

## 18. 验证记录（本次核对）

以下命令在当前工作区实际跑过，用于确认本文描述与代码一致：

```text
backend/.venv/bin/python -m pytest \
  tests/test_collab_tickets.py tests/test_collab_graph.py \
  tests/test_collab_api.py tests/test_collab_guards.py -q
→ 72 passed

backend/.venv/bin/python -m pytest \
  tests/test_collab_engine.py tests/test_collab_bus.py \
  tests/test_collab_provisioning.py tests/test_collab_migration.py \
  tests/test_collab_architecture.py -q
→ 59 passed, 1 skipped（skip 的是「execution.py 属于执行层边界」的守卫用例，刻意跳过）

```

> 注：以上命令在仓库根目录执行即可（`backend/tests/conftest.py` 会把 `backend/` 加进 `sys.path`）。
