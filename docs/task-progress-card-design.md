# 任务进度卡片 · 技术方案

> 状态：**设计文档**（本轮只产出本文档，零业务代码改动；`git status` 以交付时的实际输出为准）。
> 上游依据：[`docs/task-progress-card-research.md`](./task-progress-card-research.md)（只读现状调研）。
> 目标读者：实施者（照此实现）、QA（照此验收）。
> 行号为撰写本文时的仓库快照（2026-11-24）；实现时以符号名（函数 / 类 / 组件）定位，行号仅作索引。

---

## 0. 结论速览

| 决策点 | 结论 |
| --- | --- |
| 卡片渲染形态 | 独立组件 `TaskProgressCard`，挂在 `ConversationThreadItem.custom` 槽；桌面与移动端共用同一个组件 |
| 卡片位置 | **常驻输入框上方**（`Composer` 的 `header` 槽）；不插进消息流。`anchor_message_id` 仅保留兼容，不参与排版 |
| 折叠状态存放 | 前端 `localStorage`（`coda.taskcard.collapsed`，按卡片 id 记），后端不存折叠状态 |
| 进度存储 | 新表 `task_cards`（SQLite / SQLModel），一个对话一行，任务项存 JSON 列 |
| 恢复路径 | 页面刷新与进程重启都走同一条：`GET /api/conversations/{id}` 返回 `task_card` → 前端 `applyDetail` 写入状态 |
| 会话隔离 | `conversation_id` 唯一外键 + `_owned_conversation` 归属校验；卡片没有任何客户端写接口 |
| AI 触发 | 模型在正文末尾输出一个 `task_card` 围栏 JSON 块；运行收尾时解析、剥离、落库 |
| 建卡判定 | **确认满足条件后再建**（不是先建后删）：模型显式输出 + 服务端阈值校验双闸门 |
| 手动编辑入口 | 不提供：前端不渲染任何 checkbox / 输入框 / 点击修改；后端不暴露写接口 |
| 新增依赖 | 无（后端只用标准库 `json`/`re`，前端只用现有 React） |

---

## 1. 现状结论与依据（逐条可追溯）

本节把方案依赖的既有事实固定在源码上，实施时不得假设调研文档之外的行为。

### 1.1 对话消息链路

| 事实 | 证据 |
| --- | --- |
| 对话详情接口返回 `{conversation, messages, active_run}`，一次拉全量消息 | `backend/app/api/conversations.py:237-269`（`get_conversation`） |
| 运行准备阶段（`prepare_run`）负责上下文组装与占位消息落库 | `backend/app/runtime/service.py:346-476` |
| 重生成复用同一套上下文组装 | `backend/app/runtime/service.py:703-801` |
| 消息落库发生在运行收尾的 `finally`：`content/parts/usage/status` 一次性写入并 `commit` | `backend/app/runtime/service.py:665-694`（`consume_run` 的 `finally`） |
| `parts` 是 JSON 列，无 schema 校验，可存任意结构 | `backend/app/models.py:449-463`（`Message.parts`）、`backend/app/schemas.py:540-553`（`MessageRead.parts`） |
| 前端 `MessagePart.kind` 只认 `thinking/text/tool_call/tool_result` 四种 | `frontend/src/lib/types.ts:741-750` |
| 未知 `kind` 会被当成工具卡渲染，没有卡片分支 | `frontend/src/components/MessageView.tsx:205-224`（`groupTimeline`）、`:300-321`（`TimelinePart` 兜底 `:320`） |
| 3 秒轮询用 `sameMessageList` 比较，只比 `id/content/status/error` | `frontend/src/lib/useAutoRefresh.ts:58-69`；调用点 `frontend/src/pages/ChatPage.tsx:314-330` |
| 桌面聊天页把消息组装成 `ConversationThreadItem[]` 后交给 `ConversationThread` | `frontend/src/pages/ChatPage.tsx:946-990`、`:1088-1108` |
| 移动端聊天页**不走** `ConversationThread`，直接 `.map` 渲染 `MessageView` | `frontend/src/mobile/pages/MobileChatPage.tsx:1034-1085` |
| 线程条目有 `custom?: ReactNode` 槽，非普通消息内容可整块替换气泡 | `frontend/src/components/ConversationThread.tsx:6-21`、渲染分支 `:44` |
| 协作域已有「卡片挂进对话流」的现成先例（`TicketCard`） | `frontend/src/components/CollabAgentThread.tsx:48-96`、`:155-192` |

### 1.2 存储与恢复

| 事实 | 证据 |
| --- | --- |
| SQLite + aiosqlite + SQLModel；`init_db` 里 `create_all` 建缺失的表 | `backend/app/db.py:13-29`、`:51-64` |
| `ADDITIVE_COLUMNS` 只用于给**已存在**的表补列；**新表**由 `create_all` 直接建 | `backend/app/models.py:31-91`、`backend/app/db.py:32-49` |
| 对话删除是 `ON DELETE CASCADE`（消息随对话删） | `backend/app/models.py:415-446`（`Conversation`）、`:449-463`（`Message` 的 `conversation_id` 外键） |
| 对话归属校验统一走 `_owned_conversation`（严格归属，管理员也不能读别人的对话） | `backend/app/api/conversations.py:94-103`、`backend/app/api/conversations.py:237-269` |
| 进行中运行只存在内存（`ActiveRun`），重启即失；协作域有显式 `interrupted` 收尾，对话域没有 | `backend/app/runtime/manager.py:15-45`、`backend/app/collab/store.py:221-244`、`backend/app/main.py:83-123` |
| 消息本体重启后天然可恢复（全部落库，重新 GET 详情即可） | `backend/app/api/conversations.py:237-269`、`backend/app/runtime/service.py:665-694` |
| 平台键值存储 `PlatformSetting` 是「平台级设置」，没有对话外键、没有归属列 | `backend/app/models.py:525-537` |

### 1.3 可复用的成熟写法

| 能力 | 先例 |
| --- | --- |
| 结构化输出围栏 + 三级容错解析（严格 JSON → 清理后 → 扫描平衡对象/截断修复） | `backend/app/collab/tickets.py:28-34`、`:551`（`sanitize_json_text`）、`:655-820`（`_extract` / `parse_work_order`） |
| 落库前把协议块从正文与 `parts` 里剥掉 | `backend/app/collab/tickets.py:780-782`（`extract_body`）、`backend/app/collab/engine.py:610-620`（`_chat_parts`） |
| 后台增强失败只留日志、不打扰用户 | `backend/app/runtime/titles.py:1-20`、`:172-236`（`generate`） |
| 把一段说明并进系统提示词的写法 | `backend/app/credentials.py:88-105`（`notice` / `with_notice`） |
| 本机偏好存 `localStorage`、坏数据丢弃、隐私模式降级 | `frontend/src/lib/composerDraft.ts:19-73`、`frontend/src/lib/sidebarCollapsed.ts:10-31` |
| 展开/折叠状态管理 | `frontend/src/lib/collapse.ts:14-22`（`useExpandMap`） |

### 1.4 必须守住的工程约束

* 事件协议不新增事件类型：新增事件要走「`agents/events.py` + `runtime/service.consume_run` + 前端 `types/agentStream` + `docs/architecture.md`」四处同步（`AGENTS.md:34-67` 铁律 5、`AGENTS.md:117`）。
* 适配器是叶子节点，不得为了卡片反向依赖上层（`AGENTS.md:34-67` 铁律 1/2/3）。
* 数据库变更向后兼容，禁止删库重建（铁律 9）；新表可直接 `create_all` 建，不需要 `ADDITIVE_COLUMNS`。
* 不新增依赖（铁律 12）；界面文案与注释用中文（铁律 7）。
* 框架层禁止反向依赖协作域（`AGENTS.md` §2 协作域边界），因此解析器不能 `import app.collab.*`，新模块自包含。

---

## 2. 设计总览

```
用户发一条消息
  │
  ├─ runtime/service.prepare_run（service.py:346-476）
  │    ├─ 若本对话已有卡片，或本轮用户消息像「多步任务」：
  │    │    把「任务进度卡协议 + 当前卡片状态」追加进 ctx.prompt
  │    └─ 其余逻辑不变
  │
  ├─ 内核正常输出正文（其中可能含一个 ```task_card``` 块）
  │
  └─ runtime/service.consume_run 的 finally（service.py:665-694）
       ├─ 从 content 与 text parts 里剥离 task_card 块（任何情况下都剥，避免把协议 JSON 当正文展示）
       ├─ 仅当本轮 status == "complete" 且是主场运行：解析块、按规则合并、写 task_cards
       └─ 先提交消息，再用独立会话 best-effort 写卡片（卡片失败不影响消息）

页面打开 / 3 秒轮询 / 发送结束后刷新
  │
  └─ GET /api/conversations/{id}（api/conversations.py:237-269）多返回一个 task_card
       └─ ChatPage.applyDetail / MobileChatPage.applyDetail 写入 taskCard 状态
            └─ 在 anchor 消息之后渲染 <TaskProgressCard>
```

关键取舍：卡片是**对话级对象**，不是消息的一部分。它不是新增的一种 `MessagePart`，理由见 §3.3。

---

## 3. 卡片渲染方式

### 3.1 组件形态与放置位置

**组件**：`frontend/src/components/TaskProgressCard.tsx`，纯展示、无副作用。

```tsx
<TaskProgressCard card={taskCard} />
```

结构：

```
┌─────────────────────────────────────────────────────────┐
│ 📋 任务标题 · 进度 3/7            [进度条]      [收起/展开] │
├─────────────────────────────────────────────────────────┤
│ ✓ 步骤一（已完成，划线）                                   │
│ ○ 步骤二（未完成）                                         │
│ ○ 步骤三（未完成）                                         │
└─────────────────────────────────────────────────────────┘
```

* **不渲染任何可编辑控件**：没有 `<input type="checkbox">`、没有可点击的任务项、没有编辑/删除按钮。任务项的完成标记只是 `<span>` 文本 + CSS 状态类（`task-progress-mark is-done`）。
* 唯一可点击的是整条标题栏（原生 `button`，支持 Enter / 空格），只切换显隐，不改任何数据。
* 空标题回退为「任务进度」；进度显示 `已完成数 / 总数`。

**放置位置（桌面）**：常驻输入框上方 —— 渲染在 `Composer` 的 `header` 槽（`.composer-head`）里的第一项，位于上下文用量 / 工作目录 / 智能体那几个小图标之上：

```tsx
header={
  <>
    {taskCard && <TaskProgressCard key={taskCard.id} card={taskCard} />}
    <div className="composer-scope">…</div>
  </>
}
```

它**不再插进消息流**：进度是「这条对话当前做到哪了」的状态栏，应该一直看得见；放在 `.chat-scroll` 里会跟着历史滚走、并被后续消息顶到看不到的位置。`key={taskCard.id}` 保证切到另一条对话时重新挂载、重新读取该卡的折叠偏好。

**放置位置（移动端）**：`frontend/src/mobile/pages/MobileChatPage.tsx` 同样放进 `Composer` 的 `header` 槽（与桌面端同一份组件、同一套语义），不在 `.m-thread` 的消息 `.map` 里渲染。

`anchor_message_id` **不再参与排版**：字段保留用于接口兼容 / 历史数据，语义仍是「建卡那一轮」；`apply_block` 只在 `create`（重规划）时更新它，普通 `update` 保持不动（见 §6.5）。

**插入时机**：卡片只在「一轮运行成功结束、协议块被解析落库」之后出现；流式过程中不渲染（不产生空卡、不闪半成品）。发送结束的 `finally` 会调用 `applyDetail` 刷新详情（`ChatPage.tsx:438-560`），3 秒轮询（`ChatPage.tsx:314-330`）与另一台设备也能在数秒内看到。

### 3.2 折叠展开交互与折叠状态

* 交互：整条标题栏是原生 `button`（带 `aria-expanded`，Enter / 空格可切换），点击切换；**默认收起**——常驻输入框上方时只占一行，点开才看任务项。展开时显示任务项列表，收起时只留标题、进度与进度条。
* 状态存放：**前端本地**，`localStorage` 键 `coda.taskcard.collapsed`，值形如 `{ "<cardId>": true }`；没有记录时按「收起」处理，老用户存过的偏好优先。
* 新增 `frontend/src/lib/taskCardCollapse.ts`，照 `composerDraft.ts` 的成熟写法：读盘异常 / 隐私模式 / 坏数据一律降级为「收起」，写入失败静默；最多保留 50 条，按写入顺序淘汰，避免本地存储无限膨胀（`composerDraft.ts:19-73` 的 `MAX_DRAFTS=50` 先例）。
* 选前端本地的理由：
  1. 折叠是**这台设备的显示偏好**，与 `coda.sidebar`（`frontend/src/lib/sidebarCollapsed.ts:10-31`）、`coda.theme`、`coda.tools-hidden` 同一定位；
  2. 后端不存折叠状态，就没有「前后端两份折叠状态打架」的问题（§8 风险 5）；
  3. 纯 UI 切换不产生任何后端写请求；跨设备各记各的，符合既有偏好类数据的处理方式。

### 3.3 为什么不用 `messages.parts` 也不寄生在 `messages` 里

* 消息是**不可变的历史原文**：`consume_run` 的 `finally` 一次性写入 `content/parts` 并提交（`service.py:665-694`）。勾选状态变化若写回旧消息，会与这条提交路径抢占同一行，且语义上是在篡改历史。
* 轮询比较不到：`sameMessageList` 只比 `id/content/status/error`（`useAutoRefresh.ts:58-69`），`parts` 变了不会触发重渲染（调研报告 §9.4）。
* 渲染没有通道：`MessagePart.kind` 没有卡片类型，未知 kind 会被当工具卡（`MessageView.tsx:300-321`）；要新增就得同步改事件归约与类型，反而更容易踩铁律 5。
* 任务卡本身不寄生在消息里，也不需要新增 message part；它由独立组件渲染在输入框上方。`ConversationThread` 的 `custom` 槽（`ConversationThread.tsx:17-19,44`，协作域 `TicketCard` 已验证）仍然是「对话流里放非消息卡片」的入口，只是任务卡不再走这条路。

### 3.4 实时流里的协议块不外露

模型输出是 SSE 文本增量，落库前的剥离发生在运行收尾，因此**流式过程中**原始 `task_card` 围栏块会短暂出现在气泡里。处理方式：新增纯函数 `stripTaskCardFence(text)`（`frontend/src/lib/taskCardFence.ts`），在 `MessageView` 渲染 text 片段前调用（`MessageView.tsx:142-161` 的 text 分支）；未闭合的围栏从起始标记一直剥到文本末尾，避免半截 JSON 闪屏。历史消息的服务端正文已经剥过，前端再剥一次是幂等的。

**容错**：实测模型会把协议块写成 ```json、甚至直接甩一段裸 JSON。前后端统一放宽到「`task_card` 围栏 / `json` 围栏 / 裸 JSON」，但这类非标准块**只认正文收尾处**，并且必须「长得像任务卡」（有 `action`，或 `items` 里是 `id/text/done` 字段）——回复中段举例用的 JSON 代码块必须原样保留。服务端与前端各有一份同语义实现，改动时两边同步（现有用例见 `backend/tests/test_task_cards.py`）。

IM 通道不同步走前端，需在 `backend/app/im/bridge.py:204-249`（`_collect` 汇总正文处）用后端同一份 `strip_task_card` 再剥一次，避免把协议 JSON 发到聊天软件；上面的容错同样生效。

### 3.5 本节对应改动文件

| 文件 | 改动 |
| --- | --- |
| `frontend/src/components/TaskProgressCard.tsx` | 新建只读卡片组件 |
| `frontend/src/lib/taskCardCollapse.ts` | 新建折叠偏好读写（localStorage） |
| `frontend/src/lib/taskCardFence.ts` | 新建实时流围栏剥离纯函数 |
| `frontend/src/components/MessageView.tsx` | text 片段渲染前调用剥离函数 |
| `frontend/src/pages/ChatPage.tsx` | `taskCard` 状态、`applyDetail` 写入、卡片渲染在 `Composer` 的 `header` 槽 |
| `frontend/src/mobile/pages/MobileChatPage.tsx` | 移动端同语义接入 |
| `frontend/src/index.css` / `frontend/src/mobile/mobile.css` | 卡片与移动端样式 |

---

## 4. 进度存储方案

### 4.1 数据结构

新增表 `task_cards`（`backend/app/models.py` 追加一个 `SQLModel` 类；`models` 已被 `db.init_db` 导入，`create_all` 会自动建表，`db.py:51-64`）：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `String(64)` 主键 | 卡片标识，`new_id()`（`models.py:93`） |
| `conversation_id` | `String(64)`，外键 `conversations.id ON DELETE CASCADE`，**唯一**，索引 | 会话 ID；唯一约束保证一个对话只有一张卡，从根上杜绝重复建卡 |
| `anchor_message_id` | `String(64)`，可空，索引 | 建卡那一轮的助手消息 id（仅 `create` 重规划时更新，`update` 不动）；**仅接口兼容，不参与排版** |
| `title` | `String(200)`，默认空串 | 卡片标题；为空显示「任务进度」 |
| `items` | `JSON`，默认 `[]` | 任务项数组，见下 |
| `version` | `Integer`，默认 1 | 乐观锁版本号，每次成功写入 +1 |
| `last_run_id` | `String(64)`，可空 | 最近一次写入它的运行 id；同一运行重复应用时幂等跳过 |
| `created_at` | `UTCDateTime` | 创建时间（`models.py:97` 的 `utcnow` / `:101` 的 `UTCDateTime`） |
| `updated_at` | `UTCDateTime` | 最近一次更新时间 |

`items` 每项的结构（JSON）：

```json
{
  "id": "t1",
  "text": "实现 TaskProgressCard 组件",
  "done": false,
  "done_at": null,
  "updated_at": "2026-11-24T08:00:00+00:00"
}
```

* `id`：卡片内唯一、稳定的短标识（模型可自带；没给则服务端按 `t1/t2/...` 生成）。
* `text`：任务文本，去首尾空白，超 120 字符截断。
* `done`：勾选状态，**只能由服务端按模型输出变更**。
* `done_at`：首次被标为完成的时间（UTC ISO 8601 字符串）；未完成 / 从未完成为 `null`。
* `updated_at`：该项最近一次变化时间（UTC ISO 8601 字符串）。
* 约束：去重后数量 2～20；任一项文本为空则丢弃该项；有效项不足 2 时整块忽略（不建卡）。

### 4.2 介质选型与复用点

| 复用对象 | 具体文件 / 符号 |
| --- | --- |
| SQLite / aiosqlite 引擎与会话工厂 | `backend/app/db.py:13-29`（`engine`）、`:67-69`（`get_session`）、`SessionLocal` |
| 建表 | `backend/app/db.py:51-64`（`init_db` → `SQLModel.metadata.create_all`） |
| 主键 / 时间工具 | `backend/app/models.py:93`（`new_id`）、`:97`（`utcnow`）、`:101`（`UTCDateTime`） |
| 外键级联删除范式 | `backend/app/models.py:415-446,449-463`（`conversations` / `messages`） |
| 归属校验 | `backend/app/api/conversations.py:94-103`（`_owned_conversation`） |
| 围栏解析范式 | `backend/app/collab/tickets.py:551,655-820`（算法参照，不 import） |

**为什么新建表**：

* `messages.parts`：见 §3.3（篡改历史、轮询不重渲染、无渲染通道）。
* `PlatformSetting`（`models.py:525-537`）：它是平台级键值配置，没有对话外键、没有级联删除、没有归属列；把用户进度塞进去会产生孤儿行，也无法靠唯一约束防重复。
* 新表完全复用现有 SQLModel/SQLite/`init_db` 设施，**不新增依赖、不需要 `ADDITIVE_COLUMNS`**（`ADDITIVE_COLUMNS` 只服务于旧表补列，`db.py:32-49`）。
* 一个对话一张卡（`conversation_id` 唯一）是刻意的简化：进度卡回答「这条对话当前这件事做到哪了」，同一对话并存多张卡只会让用户分不清哪张有效；模型后续给出新计划时按 §6.5 的合并规则更新同一行。

### 4.3 页面刷新与进程重启的恢复路径

| 场景 | 恢复路径 | 代码位置 |
| --- | --- | --- |
| 页面刷新 / 切回标签页 | 重新 `GET /api/conversations/{id}`，响应新增 `task_card`；前端 `applyDetail` 把它写入 `taskCard` 状态，卡片常驻在输入框上方渲染，折叠偏好按 `card.id` 从本地恢复 | 后端 `backend/app/api/conversations.py:237-269`；前端 `frontend/src/pages/ChatPage.tsx` 的 `applyDetail` / `Composer` header；移动端 `frontend/src/mobile/pages/MobileChatPage.tsx` 同语义 |
| 另一台设备 | 3 秒轮询同一详情接口，按 `version` 判断是否变化后重渲染 | `frontend/src/lib/useAutoRefresh.ts:17-56`、`ChatPage.tsx:314-330` |
| 进程重启 | 卡片在 SQLite 里，启动无需任何恢复逻辑；下一次详情请求原样读回。与消息本体同一保证（`service.py:665-694`），**不依赖**内存中的 `ActiveRun` | `backend/app/db.py:51-64`、`api/conversations.py:237-269` |
| 运行中途进程被杀 | 卡片写入只在「一轮成功结束」后发生；被杀时卡片保持上一次已提交状态，不会出现半勾选。消息占位的残留是既有行为，与本方案无关（调研报告 §7） | `service.py:600-701` |

`GET /api/conversations/{id}` 的响应体新增一个字段（其余不变）：

```json
{
  "conversation": { "...": "..." },
  "messages": [],
  "active_run": null,
  "task_card": {
    "id": "…",
    "conversation_id": "…",
    "anchor_message_id": "…",
    "title": "…",
    "items": [{ "id": "t1", "text": "…", "done": true, "done_at": "…", "updated_at": "…" }],
    "version": 3,
    "created_at": "…",
    "updated_at": "…"
  }
}
```

`task_card` 为 `null` 表示这条对话没有卡片。对应响应模型 `TaskCardRead` 加在 `backend/app/schemas.py`（紧随 `MessageRead`，`:540-553` 附近）。前端类型 `TaskCard` / `TaskCardItem` 加在 `frontend/src/lib/types.ts`，并给 `ConversationDetail`（`:573-577`）增加 `task_card?: TaskCard | null`。

### 4.4 查询与渲染更新判定

* 后端：`select(TaskCard).where(TaskCard.conversation_id == conversation.id)`，唯一索引保证最多一行。
* 前端：在 `frontend/src/lib/useAutoRefresh.ts` 里与 `sameMessageList`（`:58-69`）并列新增 `sameTaskCard(prev, next)`（比较 `id` 与 `version`），`applyDetail` 里只有版本变化才 `setTaskCard`，避免每 3 秒轮询都触发整卡重渲染；`sameMessageList` 保持不动，卡片状态与消息状态分开比较。
* 卡片写坏 / 解析失败时，`task_card` 照旧返回上一次已提交的内容，进度停留在上一状态，不会清零。

---

## 5. 会话隔离、重复写入与并发防护

### 5.1 按会话 ID 关联

* 数据关联：`task_cards.conversation_id` 外键 + 唯一约束；卡片 id 只是行标识，**不是**读写入口。
* 删除对话时外键 `ON DELETE CASCADE` 自动删卡，不留孤儿。
* 从卡片回到对话：只能通过 `conversation_id`，不存在「拿卡片 id 直接查」的公开接口。

### 5.2 归属校验点（具体位置）

| 时机 | 校验 | 位置 |
| --- | --- | --- |
| 读（详情） | `_owned_conversation` 做严格归属校验（`require_owned_strict`），再按 `conversation.id` 查卡片 | `backend/app/api/conversations.py:94-103`、`:237-269` |
| 写（运行收尾） | `conversation_id` 只取 `prepared.conversation.id`（服务端从库中加载，不来自请求体）；并校验 `prepared.assistant_message.conversation_id == conversation.id` | `backend/app/runtime/service.py:533-577`、`:600-701` |
| 写（更新语句） | `UPDATE ... WHERE conversation_id = :cid AND id = :id AND version = :v`，永不按客户端提供的卡片 id 定位 | 新模块 `backend/app/runtime/task_cards.py` |
| 锚点 | 只有属于同一对话的助手消息才能作为 `anchor_message_id` | 同上（写前断言） |

**没有任何客户端写入口**：不加 `POST/PATCH/DELETE /api/conversations/{id}/task-card`。前端拿不到写 API，也不渲染编辑控件；即便有人构造请求，路由层也直接 404。

### 5.3 重复写入防护

* **重复建卡**：`conversation_id` 唯一约束是最终防线。`create` 块在已有卡时按「重规划」合并到同一行；并发 INSERT 撞唯一约束时捕获 `IntegrityError` → 回滚 → 重读当 UPDATE 重试（最多 3 次）。
* **同一轮重复应用**：`last_run_id` 等于本次运行 id 时直接返回，幂等跳过（重试 / 重放不会重复推进）。
* **重复 UPDATE**：`done` 单调合并（§6.5），同一状态重复提交不产生任何变化，也不 bump `version`。

### 5.4 并发写入防护

服务端**没有**「同一对话只允许一个运行」的硬性拦截（`send_message` 只做归属校验，见 `api/conversations.py:571-598`；前端 `busy/remoteRunning` 只是界面自保），两台设备可能同时跑：

* 唯一约束防并发建卡；
* 乐观锁：每次 UPDATE 带 `version` 条件，`rowcount == 0` 视为冲突 → 重新读取 → 重新合并 → 重试（最多 3 次）；
* 重试仍失败：只记 `logger.warning`，放弃这一次卡片更新，**不影响消息落库与用户回复**（与 `titles.py` 的后台增强失败策略一致）；
* 卡片写入使用独立 `SessionLocal` 会话（在消息 `commit` 之后），卡片层异常不会回滚消息（`service.py:665-694` 的提交不被牵连），也不长时间占用请求会话。

---

## 6. AI 触发机制

### 6.1 输出协议（结构化格式）

统一围栏名 `task_card`，内容是一个 JSON 对象。**建卡**：

````
```task_card
{"action":"create","title":"任务进度卡片","items":[
  {"id":"t1","text":"后端存储与接口","done":false},
  {"id":"t2","text":"前端卡片组件","done":false}
]}
```
````

**更新勾选**（只报变化项）：

````
```task_card
{"action":"update","items":[{"id":"t1","done":true}]}
```
````

约定：

* 一轮最多一个块；出现多个时按**最后一个**生效并记录告警（与 `parse_work_order` 的容错策略一致，`tickets.py:800-810`）。
* 容错：模型写成 ```json 或直接甩裸 JSON 时，只要块在**正文收尾处**且结构像任务卡（有 `action` / 结构化 `items`），服务端与前端同样认；正文中段的 JSON 示例不会被误吞。
* `action` 缺省时：会话无卡视为 `create`，有卡视为 `update`。
* `create` 的 `items` 必须 2～20 项；`update` 只认已在卡里的 `id`。
* 围栏外可以有正常 Markdown 正文；块本身永远不展示给用户（服务端剥离 + 前端实时流剥离）。

### 6.2 协议说明如何到达模型

新增 `backend/app/runtime/task_cards.py` 的 `render_notice(card | None, allow_create: bool) -> str`，内容示例：

```
【任务进度卡片协议】
当本轮确实在推进一件需要多步完成的工作时，在正文最后追加一个 task_card 围栏代码块（整轮只能有一个）：
- 首次给出计划：{"action":"create","title":"一句话任务名","items":[{"id":"t1","text":"步骤一","done":false},{"id":"t2","text":"步骤二","done":false}]}
- 后续轮次只报告状态变化：{"action":"update","items":[{"id":"t1","done":true}]}
规则：items 数量 2~20；id 短且稳定；只把确实完成的项标 done=true；闲聊、问答、单步指令不要输出这个块。
当前卡片：无
```

有卡时把「当前卡片」替换为标题与逐项 `id/文本/状态`（已完成的也列出来，模型才知道不要重复报告）。

注入位置（两处，逻辑一致）：

* `prepare_run`：在 `build_context` 之后、所有「无原生 system prompt / 无会话续接」的 prompt 改写分支（`service.py:419-431`）**之后**、创建占位助手消息之前。
* `regenerate_last`：在同类改写分支（`service.py:755-761`）之后、创建占位助手消息之前。

注入条件：

1. 只对**主场运行**注入；`summon_profile_id` 非空（`persist_session=False`，`service.py:373-420`）时不注入，避免被 @ 召唤的智能体篡改进度。
2. 已有卡片 → 每轮都注入「当前卡片 + update 协议」。
3. 无卡片且 `looks_like_task_request(用户正文)` 为真 → 注入「create 协议」。`prepare_run` 传 `payload.content`（用户原始输入，不含附件提示块）；`regenerate_last` 传被重跑的那条用户消息正文。
4. 两者都不满足 → 不注入，闲聊对话没有额外 token 开销。

注入到 `ctx.prompt`（而不是 `ctx.system_prompt`）：`ctx.prompt` 是每轮必发的新输入，session 型与非 session 型内核都会带上；`system_prompt` 会被 `transcript_prompt` 折叠逻辑改写（`service.py:275-285`），放在 `ctx.prompt` 能保证一定送达。

### 6.3 解析与落库

新增 `backend/app/runtime/task_cards.py`（框架层自包含，只依赖标准库 + `models/db`，不 import 协作域）：

```python
FENCE = "task_card"
MAX_SCAN_CHARS = 200_000
MIN_ITEMS, MAX_ITEMS = 2, 20
MAX_ITEM_CHARS, MAX_TITLE_CHARS = 120, 200

def looks_like_task_request(text: str) -> bool: ...          # §7
def strip_task_card(text: str) -> str: ...                   # 剥正文
def strip_task_card_from_parts(parts: list[dict]) -> list[dict]: ...   # 跨片段状态机剥 parts
def parse_task_card(text: str) -> tuple[dict | None, str | None]: ...  # 三级容错
def normalize_block(raw: dict) -> tuple[dict | None, str | None]: ...   # 校验 / 去重 / 截断
def merge_card(existing, block, *, anchor_message_id, run_id) -> list[dict] | None: ...
async def load_card(session, conversation_id) -> TaskCard | None: ...
async def apply_block(conversation_id, anchor_message_id, block, run_id) -> TaskCard | None: ...
```

解析算法（参照 `tickets.py:655-820` 的思路，独立实现）：

1. 用正则取出所有 `task_card` 围栏（大小写不敏感；未闭合时吃到文本末尾），按最后一段解析；
2. 严格 `json.loads`；
3. 失败则清理后重试：去 BOM / 不可见控制字符、中文引号还原为 `"`、去尾随逗号；
4. 仍失败则扫描正文中平衡的 `{...}` 对象，被截断时逐段回退到最后一个可解析 `}`（`MAX_SCAN_CHARS`、`MAX_REPAIR_TRIES` 上限防拖死）；
5. 都失败 → 返回错误串，**不写卡**。

落库位置：`consume_run` 的 `finally`（`service.py:665-694`），顺序固定：

```python
raw_text = "".join(text_parts)
content = strip_task_card(raw_text)                 # 任何情况下都剥，失败也剥
parts = strip_task_card_from_parts(parts)          # 跨 text 片段状态机，防止块被思考/工具打断后残留
# ... 现有 message.content / parts / status 赋值与 commit 不变 ...
# commit 成功之后、registry.finish() 之前：
if status == "complete" and prepared.persist_session:
    block, parse_error = parse_task_card(raw_text)
    if block:
        await apply_block(conversation.id, assistant_message.id, block, run.id)  # 内部吞异常并记日志
    elif parse_error:
        logger.warning("任务卡片块解析失败，已忽略：%s", parse_error)
```

`apply_block` 用独立 `SessionLocal`，异常只 `logger.warning`（后台增强的失败不打断用户回复，参照 `titles.py`）。

### 6.4 建卡时机与勾选时机

| 动作 | 时机 | 硬条件 |
| --- | --- | --- |
| 建卡（create） | 一轮运行**成功结束**后解析到合法 `create` 块 | 本轮 `status == "complete"`；主场运行；会话当前无卡；有效项 2～20；用户消息通过 §7 判定 |
| 勾选（done=true） | 同一收尾点解析到 `update` 块（或已有卡上的 `create` 重规划） | 本轮 `status == "complete"`；主场运行；卡片存在；`id` 在卡内；`done` 只允许 `false → true` |
| 取消 / 报错 / 中断 | 不写卡片 | `status != "complete"` 直接跳过，避免把失败回合误判为完成 |

勾选**不发生在流式过程中**：只在整轮结束后一次性应用，避免半截 JSON 被当成进度。前端要等详情刷新（发送结束立即刷新，最迟 3 秒轮询）才看到勾选变化。

### 6.5 合并语义（防空卡、防误勾选的关键）

`create`：

* 没有卡 → 新建行，`anchor_message_id` = 本轮助手消息 id。
* 已有卡 → 同一行的**重规划**：按 `id` 保留旧项的 `done`（`done = old.done or new.done`，已完成的绝不会因重规划被抹掉）；新 `id` 采用模型给的 `done`；旧卡里新块没有的 `id` 删除；**只有 `create` 重规划才把 `anchor_message_id` 更新到本轮消息**。
* 有效项去重后不足 2 / 超过 20 → 整块忽略，日志告警，不建空卡。

`update`：

* 没有卡 → 忽略（`update` 永远不能凭空造卡，从根上杜绝空卡）。
* 只处理卡内已存在的 `id`；未知 `id` 忽略并记告警。
* **不移动 `anchor_message_id`**：普通报进度不该改变卡片归属的那一轮（旧实现每轮都改，导致卡片被一路拖到对话末尾）。
* `done` 单调：`false → true` 允许并写 `done_at`；`true → false` 忽略并记告警（防止模型反复横跳造成「误取消 / 误勾选」）。
* 同一 `id` 在一次块里出现多次：按最后一次为准并记告警。
* 全部无效 / 没有任何状态变化 → 不写库、不 bump `version`。

### 6.6 三类失败情形的降级策略

| 失败情形 | 降级策略 | 用户可见影响 |
| --- | --- | --- |
| **AI 未输出块** | 不建卡、不改卡；已有卡保持原状 | 无。卡片停在上一进度，正文正常展示 |
| **格式非法**（JSON 坏 / 截断 / 缺 items） | 放弃本次写入；**仍然剥离**原始块，避免协议 JSON 出现在正文；`logger.warning` 记录原因；已有卡保持原状 | 无报错气泡。卡片不变，聊天正文干净 |
| **判断冲突**（update 无卡 / 未知 id / done 回退 / 重复 id / 同一轮多个块） | 只应用合法子集：未知 id 与回退忽略并告警；无卡 update 整体忽略；重复 id 以最后一次为准；多个块取最后一个并告警 | 最坏情况是某项这次没被勾上，下一轮模型可以再报 |
| 运行失败 / 取消 / 中断 | 整轮不碰卡片 | 卡片不会因失败回合被误勾 |
| 写入并发冲突 | 乐观锁重试 3 次；仍失败放弃并记录日志 | 最多丢失一次进度更新，消息与回复不受影响 |

失败属于「后台增强失败」，按仓库既有约定只留日志、不弹用户错误（`titles.py:1-20` 明确这是铁律 11 的例外）。

### 6.7 防空卡 / 防误勾选的规则汇总

1. `create` 必须 ≥2 个有效项；`update` 永不建卡；
2. 只在 `status == "complete"` 的**主场**运行上应用；
3. `done` 只能由模型显式声明，且只允许单向 `false → true`；
4. 协议块只从模型输出来，客户端没有任何写入口；
5. 解析失败、冲突、并发冲突一律「保持原状态」，不做猜测式修补；
6. 一个对话一行，唯一约束 + 重规划合并，杜绝重复卡。

---

## 7. 非任务型对话不建卡的判定策略

采用**「确认满足条件后再建」**（不是「先建后删」）。理由：卡片是持久化数据，先建后删会留下无意义的行、需要额外的删除路径、可能在删除前被轮询到并闪一下；而「确认后再建」的代价只是模型判断错了就不出卡，属于可接受的漏报。判定分两层：

**第一层（模型侧，写进 §6.2 的协议说明）**：明确要求「闲聊、问答、单步指令不要输出这个块」。

**第二层（服务端硬闸门，`looks_like_task_request`，全部满足才允许 create）**：

| 条件 | 阈值 |
| --- | --- |
| 用户正文去空白后的长度 | ≥ 10 个字符 |
| 命中任务动作词 | ≥ 1 个：实现 / 开发 / 修复 / 重构 / 编写 / 设计 / 调研 / 搭建 / 部署 / 优化 / 迁移 / 测试 / 整理 / 排查 / 检查 / 完成 / 添加 / 支持 / 接入 / 改造 / 升级 / 生成 / 分析 / 梳理 / 写 / 做 / 改 / 查 |
| 或包含清单结构 | ≥ 2 个列表标记：`1.` `2.` `-` `•` `①` 或 ≥2 个以「；」分隔的祈使短句 |
| 卡片自身 | 有效任务项 2～20 个，每项文本 1～120 字符 |

说明：

* 阈值故意保守：短句「好的」「继续」「这是什么」不会命中动作词，也就不建卡；「帮我排查这个 bug」命中「排查」，允许建卡。
* 已有卡片时不走这条判定：每轮都允许 `update`，否则任务中途用户回一句「继续」就没法勾选了。
* 判定函数是纯函数，单测直接覆盖边界（9/10 字符、1/2 个列表标记、空项、21 项）。

---

## 8. 风险点与应对

| 风险 | 具体应对 |
| --- | --- |
| **1. AI 判断不准**（任务项乱列、进度乱勾） | 协议说明明确「只标确实完成的」；服务端只接受 2～20 个非空项；`done` 单向单调，模型可下一轮补报但不能回退；每轮只应用一次、失败回合不应用；判断错的代价被限制在卡片展示层，正文与消息不受影响。后续若需要，可由模型用 `create` 重规划整张卡来纠错（不是新增功能，是协议既有动作） |
| **2. 重复建卡** | `conversation_id` 唯一约束是最终防线；`create` 作用于已有卡时按重规划合并同一行；前端按 `card.id` 作为渲染 key，不存在同卡渲染两次；多块取最后一个 |
| **3. 并发写入** | 乐观锁 `version` + `WHERE version = :v`；冲突重读重试（≤3 次）；唯一约束兜底并发建卡；`last_run_id` 幂等；独立会话写入，卡片失败不回滚消息；失败只记日志 |
| **4. 存储增长** | 一个对话最多一行、最多 20 项、每项 ≤120 字符，单卡上限约数 KB；不保存历史快照（只留最新一版）；对话删除级联删卡；折叠偏好本地最多 50 条 |
| **5. 前后端折叠状态不一致** | 折叠状态**只存前端**，后端没有对应字段，结构上不存在前后端两份状态打架；跨设备折叠状态各自独立（与侧栏、主题一致的偏好语义）；卡片内容变化只 bump `version`，折叠 key 仍是稳定 `card.id`，刷新后展开/收起状态保留；卡片被删（对话删除）后残留的本地 key 无害，且有 50 条上限 |
| **6. 实时流短暂露出协议 JSON** | 前端 `stripTaskCardFence` 在 `MessageView` 渲染 text 前剥离（未闭合围栏剥到末尾）；服务端落库前再剥一次；IM 在 `_collect` 汇总处剥离 |
| **7. 轮询不重渲染** | 卡片状态与消息状态分开：`applyDetail` 用 `version` 比较后单独 `setTaskCard`，不复用只比四个字段的 `sameMessageList` |
| **8. 提示词开销** | 无卡且非任务消息不注入；有卡时注入内容随项数线性增长，最多 20 行，量级可控 |

---

## 9. 设计约束：界面不提供任何手动勾选或修改入口

这是本方案的产品前提，实施时必须守住。具体落点：

| 位置 | 必须满足 |
| --- | --- |
| `frontend/src/components/TaskProgressCard.tsx` | 任务项是纯文本 + 状态图标；不得出现 `<input type="checkbox">`、`<button>` 包裹任务项、`onClick` 修改状态、编辑/删除/新增按钮 |
| `frontend/src/lib/api.ts` | 不新增任何 task-card 写方法 |
| `backend/app/api/conversations.py` | 只读：卡片随详情接口返回；不得新增 POST/PATCH/DELETE 卡片路由 |
| `backend/app/schemas.py` | 只加 `TaskCardRead`（响应模型），不加卡片写请求模型 |
| `backend/app/runtime/service.py` | 唯一的写入口是 `consume_run` 收尾，输入来自模型输出，不来自 HTTP 请求体 |
| 测试 / 评审 | 后端测试断言不存在卡片写路由；前端评审检查组件里没有可编辑控件 |

---

## 10. 实现子任务清单（按依赖顺序）

> 统一验收命令：`bash scripts/test.sh`（后端 pytest + 前端 `tsc --noEmit`）、`npm --prefix frontend run build`。下表「验收方式」列的是额外针对性验证。

| # | 子任务 | 改动文件 | 完成标准 | 验收方式 | 依赖 / 并行 |
| --- | --- | --- | --- | --- | --- |
| S1 | 新增卡片数据模型 | `backend/app/models.py` | 新增 `TaskCard` 表：唯一 `conversation_id` 外键级联、`items` JSON、`version`、`last_run_id`、时间列；`init_db` 能建表 | 新增 `test_task_cards.py` 断言 `PRAGMA table_info(task_cards)` 列齐全、删除对话后卡片随级联消失；`pytest tests/test_migration.py` | 无依赖；可与 S2 并行 |
| S2 | 协议解析与校验纯函数 | 新建 `backend/app/runtime/task_cards.py`（纯函数部分）、`backend/tests/test_task_cards.py` | `FENCE`、`parse_task_card`（三级容错）、`normalize_block`（2～20 项、去重、截断）、`strip_task_card`、`strip_task_card_from_parts`（跨片段状态机）、`looks_like_task_request`（§7 阈值）全部实现 | 单测覆盖：合法 create/update、无块、坏 JSON、截断修复、多块取最后、去重、边界 1/2/20/21 项、跨片段剥离 | 无依赖；可与 S1 并行 |
| S3 | 卡片读写与并发合并 | `backend/app/runtime/task_cards.py`（DB 部分） | `load_card`、`apply_block`：create/update 合并语义（§6.5）、唯一约束冲突重试、乐观锁重试、`last_run_id` 幂等、独立 `SessionLocal`、异常只记日志 | 单测：两会话并发写同一行（版本冲突后重试成功）、并发建卡（唯一约束后转更新）、done 单向、未知 id 忽略 | 依赖 S1、S2 |
| S4 | 提示词注入 | `backend/app/runtime/service.py`（`prepare_run` 与 `regenerate_last`） | 按 §6.2 条件与位置追加协议说明；主场/召唤、有无卡、是否任务消息四种组合行为正确 | 单测 `render_notice` 文案；接口测试断言任务型消息的 `ctx.prompt` 含 `task_card`、闲聊不含、召唤轮不含 | 依赖 S2、S3 |
| S5 | 运行收尾解析与落库 | `backend/app/runtime/service.py`（`consume_run` `finally`） | 始终剥离协议块；仅 complete + 主场写入；解析失败/冲突只告警；消息提交不受卡片失败影响 | 接口测试：假内核输出含块的正文 → 消息 content/parts 不含块、详情返回 `task_card`；第二轮 update → done=true；失败/取消轮不写卡 | 依赖 S3、S4 |
| S6 | 详情接口与响应模型 | `backend/app/api/conversations.py`、`backend/app/schemas.py` | `get_conversation` 返回 `task_card`（无卡为 `null`）；新增 `TaskCardRead`；不加写路由 | `pytest tests/test_api.py` 新增断言：无卡返回 null、有卡字段完整；归属校验沿用 `_owned_conversation`，他人对话 404 | 依赖 S1、S3；可与 S4/S5 并行 |
| S7 | 前端类型与比较器 | `frontend/src/lib/types.ts`、`frontend/src/lib/useAutoRefresh.ts` | 新增 `TaskCard` / `TaskCardItem`；`ConversationDetail.task_card`；`sameTaskCard`（比 `id` + `version`） | `npm --prefix frontend run typecheck` | 依赖 S6 的响应契约；可与 S8 并行 |
| S8 | 卡片组件与样式 | `frontend/src/components/TaskProgressCard.tsx`、`frontend/src/lib/taskCardCollapse.ts`、`frontend/src/index.css`、`frontend/src/mobile/mobile.css` | 只读卡片：标题/进度/进度条/任务项状态图标；折叠按钮 + `localStorage` 记忆（上限 50、坏数据降级）；**无任何编辑控件** | `typecheck` + `build`；人工检查组件源码无 `input` / 编辑 `onClick`；桌面与移动窄屏各看一次 | 依赖 S7；可与 S9/S10 并行 |
| S9 | 桌面聊天页接入 | `frontend/src/pages/ChatPage.tsx` | `taskCard` 状态 + `applyDetail` 比较写入；卡片常驻渲染在 `Composer` 的 `header` 槽（`key={card.id}`），不进消息流；实时流不渲染卡片 | 手测：任务型对话收到回复后卡片出现在输入框上方；刷新后位置与勾选不变；另一设备 3 秒内同步；删除对话后卡片消失 | 依赖 S7、S8 |
| S10 | 移动端接入 | `frontend/src/mobile/pages/MobileChatPage.tsx` | 与 S9 同语义：状态、`applyDetail`、卡片放在 `Composer` 的 `header` 槽、移动样式 | 同 S9 的移动端手测 | 依赖 S7、S8；可与 S9 并行 |
| S11 | 实时流协议块隐藏（前端） | `frontend/src/lib/taskCardFence.ts`、`frontend/src/components/MessageView.tsx` | text 片段渲染前剥离 `task_card` 围栏（含未闭合）；兼容收尾处的 ```json / 裸 JSON 块，中段示例不动；历史消息幂等 | `typecheck` + `build`；手测：流式输出中不闪 JSON 块 | 依赖 S2 的围栏格式；可与 S9/S10 并行 |
| S12 | IM 正文剥离 | `backend/app/im/bridge.py`（`_collect`） | 汇总正文时调用后端 `strip_task_card`，聊天软件看不到协议 JSON | `pytest tests/test_im.py` 新增断言（正文不含围栏内容） | 依赖 S2；可与 S5 并行 |
| S13 | 文档同步与回归 | `docs/architecture.md`（数据模型 §7 与运行时序补一段）、本文档 | 架构文档记录 `task_cards` 表与 `task_card` 协议；全套测试通过 | `bash scripts/test.sh`、`npm --prefix frontend run build`；`git status` 只含文档与实现文件 | 依赖全部；最后执行 |

**并行说明**：S1 / S2 可同时开工（模型与纯解析互不依赖）；S4 / S5 / S6 / S12 在 S3 完成后可并行；S7 / S8 / S11 前后端可并行；S9 / S10 是两端的独立接入，可并行；S13 收尾。

**明确不做**：不新增依赖；不新增事件类型（不走铁律 5 的四处同步）；不加卡片写接口；不做多卡片 / 历史版本 / 手动编辑。

---

## 11. 验证清单（交付前逐条核对）

1. `GET /api/conversations/{id}` 无卡时 `task_card == null`，有卡时字段完整、内容不含协议围栏。
2. 同一对话连续两轮：第一轮 create 建卡，第二轮 update 勾选；`task_cards` 仍只有一行，`version` 递增。
3. 页面刷新后卡片位置、标题、勾选状态与刷新前一致（§4.3）。
4. 重启后端进程后卡片与勾选状态仍在（同一接口读回）。
5. AI 不输出块、输出坏 JSON、输出无卡 update：均不改卡，正文不出现协议 JSON。
6. 失败 / 取消的一轮：卡片不被修改。
7. 删除对话：`task_cards` 对应行随级联删除（`PRAGMA foreign_keys=ON` 已由 `db.py:13-29` 开启）。
8. 组件与路由中不存在任何手动勾选 / 修改入口（§9）。
9. `bash scripts/test.sh` 与 `npm --prefix frontend run build` 通过；`git status` 无业务代码之外的意外改动。
