# 轨迹调试视图 · 第一阶段技术方案

> 状态：方案文档（**本阶段不写实现代码、不改配置、不装依赖**）。
> 范围：当前桌面端对话页（`frontend/src/pages/ChatPage.tsx`）。
> 目标读者：CTO（评审 / 拆实现任务）、QA（对照验收标准核查）。

---

## 0. 现有数据流摸底结论（摘要）

| # | 数据类别 | 现状三态 | 一句话结论 | 关键证据 |
| --- | --- | --- | --- | --- |
| 1 | system prompt | **部分留存** | 人格配置存在 `agent_profiles.system_prompt`；每轮实际生效的文本（并入凭据说明、或折进 prompt）未落库 | `backend/app/models.py:189`、`backend/app/runtime/service.py:147`、`backend/app/credentials.py:100-105` |
| 2 | 每轮完整上下文 | **部分留存** | 对话的 user / assistant 最终文本在 `messages` 表；实际发给模型的 `messages[]`（system + 裁剪后的历史 + 本轮 prompt）只在内存 | `backend/app/runtime/service.py:30-31,163-173,294-333`、`backend/app/agents/builtin/openai_harness.py:111-120` |
| 3 | 工具调用（名称/参数/结果） | **部分留存** | 规范化事件已按 `message.parts` 落库（名称、入参、输出、is_error）；没有单条时间戳、耗时长，也没有与模型请求的关联键 | `backend/app/agents/events.py:53-62`、`backend/app/runtime/service.py:477-489`、`backend/app/models.py:435` |
| 4 | 模型请求体 | **缺失** | API 内核在 `request_payload()` 里临时构造后直接 `client.stream(..., json=payload)`；CLI 内核的 HTTP 请求发生在子进程内部，平台只持有 `Invocation(argv/stdin)` | `backend/app/agents/builtin/openai_harness.py:122-137,169,217`、`backend/app/runtime/runner.py:229,260-274` |
| 5 | 模型原始响应（含 finish_reason/usage） | **部分留存** | 只把流式 chunk 解析成正文/推理/工具/usage 事件；`finish_reason` 全仓库未读取，原始 chunk 丢弃。usage 聚合后落 `messages.usage` | `backend/app/agents/builtin/openai_harness.py:208-243`、`backend/app/runtime/service.py:490-491,532`、`backend/app/models.py:436` |
| 6 | 时间戳（消息级 / 请求级） | **部分留存** | 消息级有 `messages.created_at`；运行级只有内存里的 `ActiveRun.started_at`；事件与单次请求没有时间戳，运行结束即丢 | `backend/app/models.py:439`、`backend/app/runtime/manager.py:27,39-45`、`backend/app/agents/events.py:27-33` |

**一句话结论**：对话与最终消息的持久化链路是完整的；缺的是「模型调用边界」的请求/响应与时间信息。
但仓库**不存在统一的模型调用封装**：9 个内置内核里 8 个是 CLI 子进程（`Runtime.CLI`），只有
`openai-harness` 在进程内直接发 HTTP。因此采集必须分两条执行路径接入（详见 §3），
CLI 内核的真实 HTTP 报文在子进程内，平台不可见（见 §10 偏差说明）。

---

## 1. 仓库结构与数据流定位

### 1.1 前端（对话页）

| 职责 | 文件 | 关键函数 / 位置 |
| --- | --- | --- |
| 对话页容器 | `frontend/src/pages/ChatPage.tsx` | `ChatPage`（`:75`） |
| 发送消息入口 | 同上 | `send`（`:410`）、`submitDraft`（`:539`）、`streamPost`（`:477`） |
| 新建对话入口 | 同上 | `startConversation`（`:642`）；顶栏按钮 `:957-965`（`PlusIcon`） |
| 侧栏新建入口 | `frontend/src/components/ConversationList.tsx` | 「＋ 新建」按钮 `:186-190` |
| 消息渲染 | `frontend/src/components/ConversationThread.tsx` → `frontend/src/components/MessageView.tsx` | `ConversationThread`（`:36`）、`MessageView`（`:31`）、工具卡片 `ToolCard`（`:321`） |
| 流式事件归约 | `frontend/src/lib/agentStream.ts` | `applyAgentEvent`（`:37`） |
| 接口层 | `frontend/src/lib/api.ts` | 对话相关 `:392-411`；`streamPost` `:597-630` |
| 类型 | `frontend/src/lib/types.ts` | `Message` `:639`、`MessagePart` `:628`、`AgentEvent` `:657` |
| 设置页 | `frontend/src/pages/SettingsPage.tsx` | 标签定义 `:24-37`（`chat` 在 `:30`）；「对话」页内容 `:154-162` |
| 本机偏好存储 | `frontend/src/lib/finalOnly.ts` / `theme.ts` / `tts.ts` | 均用 `localStorage`，键带 `coda.` 前缀（如 `coda.tools-hidden`，`finalOnly.ts:15`） |

### 1.2 后端会话路由 / 服务

| 职责 | 文件 | 关键函数 / 位置 |
| --- | --- | --- |
| 发送消息路由 | `backend/app/api/conversations.py` | `send_message`（`:485-510`） |
| 重新生成 | 同上 | `regenerate`（`:513-525`） |
| 订阅运行事件 | 同上 | `stream_active_run`（`:434-450`） |
| 对话详情 | 同上 | `get_conversation`（`:228-261`） |
| 准备一次运行 | `backend/app/runtime/service.py` | `prepare_run`（`:269-378`）、`regenerate_last`（`:547-619`） |
| 后台执行 + 落库 | 同上 | `start_run`（`:436-453`）、`_execute_prepared`（`:393-433`）、`consume_run`（`:456-544`） |
| 执行层分发 | `backend/app/runtime/runner.py` | `stream_run`（`:178-207`）、`stream_cli_run`（`:210-424`） |

### 1.3 上下文组装（system prompt + 历史消息）

| 决策点 | 文件 | 位置 / 变量 |
| --- | --- | --- |
| 历史裁剪（最近 24 条 / 4 万字符） | `backend/app/runtime/service.py` | `MAX_HISTORY_MESSAGES` / `MAX_HISTORY_CHARS`（`:30-31`）、`_clip_history`（`:163-173`） |
| 运行上下文组装 | 同上 | `build_context`（`:106-153`）；system prompt 解析在 `:147` |
| 实际历史注入 | 同上 | `history = _clip_history(...)`（`:308`，`regenerate_last` 在 `:576`） |
| 不支持会话续接时 | 同上 | `transcript_prompt`（`:198-207`）、`prior_context_prompt`（`:210-226`）、能力分支 `:324-333` |
| API 内核 messages 数组 | `backend/app/agents/builtin/openai_harness.py` | `build_messages`（`:111-120`） |
| CLI 内核注入方式 | 各适配器 | Claude Code：`--system-prompt` / `--append-system-prompt` 与 stdin（`claude_code.py:483-489,516`）；Codex 折进 prompt（`codex.py:345-346`）；Gemini 写 `.coda/system.md`（`gemini_cli.py:207-211`） |

### 1.4 模型 SDK / API 调用封装

- **没有统一封装**。执行层按 `Runtime` 二选一：`backend/app/runtime/runner.py:191-206`。
- `Runtime.API`（仅 `openai-harness`）：
  - 适配器自己建 `httpx.AsyncClient`：`backend/app/agents/builtin/openai_harness.py:97-99`；
  - 构造请求体：`request_payload`（`:122-137`）；
  - 真正发送：`_stream_request` 的 `client.stream("POST", url, json=payload, headers=...)`（`:217`）；
  - 解析 SSE：`:221-243`。
- `Runtime.CLI`（其余 8 个内核）：
  - 适配器只构造 `Invocation(argv, env, cwd, stdin, files)`：`backend/app/agents/base.py:203-217`，例如 `claude_code.py:463-516`；
  - 运行层 `asyncio.create_subprocess_exec` 启动子进程：`backend/app/runtime/runner.py:266-274`；
  - 平台只能看到子进程的 stdout/stderr；模型 HTTP 请求/响应在子进程内部，**不可见**。
- 已出现的请求侧线索只有一条：命令预览日志（脱敏、截断 400 字）`backend/app/runtime/runner.py:260-263`。

### 1.5 会话持久化

| 项 | 位置 |
| --- | --- |
| 数据库 | SQLite，默认 `backend/data/coda.db`（`backend/app/settings.py:80-83`） |
| 建表 / 补列 | `backend/app/db.py:51-64`（`create_all` + `ADDITIVE_COLUMNS` 幂等补列） |
| 对话表 | `backend/app/models.py:403-425`（`Conversation`，含内核侧 `session_id` `:414`） |
| 消息表 | `backend/app/models.py:428-442`（`Message`：`content`、`parts` `:435`、`usage` `:436`、`status`、`created_at` `:439`） |
| 落库时机 | `consume_run` 的 `finally`：`backend/app/runtime/service.py:515-544`（先落库，再 `registry.finish`，`:538-544`） |
| 运行事件缓冲 | `backend/app/runtime/manager.py:39-45`（`ActiveRun.events`，**仅内存**，运行结束即丢） |

### 1.6 设置项存储

| 形态 | 位置 / 例子 |
| --- | --- |
| 前端本机偏好 | `localStorage`，如 `coda.tools-hidden`（`frontend/src/lib/finalOnly.ts:15`）、主题（`theme.ts`）、朗读（`tts.ts`） |
| 后端平台级键值 | `platform_settings` 表（`backend/app/models.py:465-477`）；用法参考自动起标题 `backend/app/runtime/titles.py:62-117`、接口 `backend/app/api/system.py:116-138` |
| 后端用户级字段 | `users` 表（`backend/app/models.py:480-524`）；新字段通过 `ADDITIVE_COLUMNS` 兼容老库（`models.py:31-80`） |

---

## 2. 六类数据逐项取证

> 三态定义：**已留存**＝刷新/重启后仍能从库中读到；**部分留存**＝只留下其中一部分或只留下派生结果；
> **缺失**＝没有任何持久化。凡本仓库查不到的，明确写「未找到，需确认」，不做推测。

### 2.1 system prompt —— 部分留存

- 已留存：智能体人格原文在 `agent_profiles.system_prompt`
  （`backend/app/models.py:189`，`system_prompt: str = Field(default="", sa_column=Column(Text, nullable=False))`）。
- 运行时解析：`backend/app/runtime/service.py:147`，
  `system_prompt=with_notice(profile.system_prompt or None, credentials)`；
  凭据说明拼接见 `backend/app/credentials.py:100-105`（`with_notice`）。
- 实际发送：API 内核进 `messages[0]`（`openai_harness.py:113-114`）；
  CLI 内核走 argv（`claude_code.py:483-489`）、文件（`gemini_cli.py:207-211`）或折进 prompt（`codex.py:345-346`、
  `service.py:326-328`）。
- 缺口：**每轮最终生效的 system prompt 文本没有落库**；`messages` 表没有该字段（`models.py:428-442`），
  且 `with_notice` 拼接结果、非原生人格折进 prompt 的结果都只存在于 `AgentContext`。

### 2.2 每轮完整上下文 —— 部分留存

- 已留存：`messages` 表保存了每轮 user / assistant 的**最终文本**（`models.py:433-434`），
  以及创建时间（`:439`）；附件以相对路径写进用户消息正文（`service.py:188-195,302-305`）。
- 运行时组装：`session.py` 的 `_clip_history` 取最近 24 条、4 万字符（`service.py:30-31,163-173`），
  在 `prepare_run`（`:308`）与 `regenerate_last`（`:576`）重新计算；随后 `build_context` 写入
  `AgentContext.history`（`agents/base.py:242-244`）。
- API 内核最终请求数组：`openai_harness.py:111-120`（system + history + 本轮 prompt）。
- 无法续接会话时的两种兜底：`transcript_prompt`（`service.py:198-207`）、
  `prior_context_prompt`（`:210-226`，触发分支 `:324-333`）。
- 缺口：**「这一轮实际发出去的完整上下文」只在内存里**。`_clip_history` 每次重算，历史一旦增长/裁剪，
  事后无法还原当时发给模型的确切数组（尤其是 24 条 / 4 万字符边界与兜底 prompt）。

### 2.3 工具调用（名称 / 参数 / 结果）—— 部分留存

- 事件协议：`EventType.TOOL_CALL` / `TOOL_RESULT`（`backend/app/agents/events.py:18-19`），
  构造器 `tool_call` / `tool_result`（`:53-62`）。
- 适配器产出示例：`openai_harness.py:263,273`；`claude_code.py:609,616`；`codex.py:503-519,762-824`。
- 落库：`consume_run` 把工具事件追加进 `parts`（`service.py:477-489`），最终 `message.parts = parts`（`:531`）；
  表字段 `Message.parts`（`models.py:435`）。
- 已保留字段：`kind`（tool_call / tool_result）、`id`（`call_id`）、`name`、`input`、`output`、`is_error`。
- 缺口：
  - 没有单条调用/结果的**时间戳与耗时**（事件结构本身无时间字段，`events.py:27-33`）；
  - 没有与「第几次模型请求」的关联键（`call_id` 只配对调用与结果）；
  - CLI 内核的 `input` 是适配器从内核输出里转述的，不保证等于模型原始参数；
  - 工具事件不进独立的调试表，混在用户可见的 `message.parts` 里。

### 2.4 模型请求体 —— 缺失

- API 内核：`request_payload` 临时构造（`openai_harness.py:122-137`），在 `stream` 内每步调用（`:169`），
  随后直接 `client.stream(..., json=payload, ...)`（`:217`）；**没有传给任何持久化层**。
- CLI 内核：平台只构造 `Invocation`（`agents/base.py:203-217`；`claude_code.py:463-516`），
  由子进程自己组装模型请求；运行层只记录一条截断到 400 字、脱敏后的命令预览
  （`runner.py:260-263`），且该 `log` 事件不落库（`consume_run` 只持久化 content/parts/usage/error/status，`service.py:529-534`）。
- 结论：**缺失**。请求头（含 `Authorization`）也不在任何持久化数据里。

### 2.5 模型原始响应（含 finish_reason / usage）—— 部分留存

- API 内核：`_stream_request` 逐行解析 SSE（`openai_harness.py:221-243`），只产出：
  正文增量、推理增量、工具调用分片、usage（`:231-233`）；`[DONE]` 即停（`:225-226`）。
- `finish_reason`：**未找到，需确认**。`grep -rn "finish_reason" backend/app frontend/src` 无产品代码命中
  （仅命中 `backend/data/workspaces/...` 下的第三方内核工作区文件，不属于本产品代码）。
- CLI 内核：原始 stdout 行在 `runner.py:368-369` 解码后交给 `agent_run.feed()`；未知/脏行最多产出 `log(..., "debug")`
  （如 `codex.py:424-433`），**原始行不落库**。
- 已留存的下游产物：usage 事件在 `service.py:490-491` 聚合，`finally` 写进 `messages.usage`（`:532`、`models.py:436`）。
- 错误响应体：API 内核只保留 500 字并塞进 error 事件（`openai_harness.py:176-178`），不落库。
- 结论：**部分留存**——`usage` 聚合值已留存；原始响应体与 `finish_reason` 缺失。

### 2.6 时间戳（消息级 / 请求级）—— 部分留存

- 消息级：`Message.created_at`（`models.py:439`），落库；`Conversation.updated_at`（`:420`）。
- 运行级：`ActiveRun.started_at`（`manager.py:27`）是 `time.time()` 浮点，只用于运行中的
  `elapsed_seconds` 快照（`:56`），运行结束 `registry.finish` 后即丢（`:138-141`）。
- 事件级：`AgentEvent` 只有 `type/text/name/call_id/payload`（`events.py:27-33`），**没有 timestamp**；
  SSE 缓冲 `ActiveRun.events` 也是内存态（`manager.py:39`）。
- 请求级（每次模型 HTTP 请求的起止时间、耗时）：**未找到，需确认**。
- 结论：**部分留存**（仅消息级）。

---

## 3. 缺失项采集层方案

### 3.1 为什么不选「请求构造层」或「会话存储层」

- **请求构造层**（`request_payload` / `build_invocation`）：
  只覆盖 `openai-harness` 一个内核；CLI 内核没有这个函数，且构造出的 `argv` 不等于真实 HTTP 请求体。
  在构造层采集会得到「平台以为发了什么」，而不是「模型实际收到什么」。
- **会话存储层**（`Message` / `message.parts`）：
  只能看到最终结果；工具结果可能被清洗/截断，请求与原始响应根本不存在。往上加字段会让
  用户对话数据与调试数据混在一起，且需要改消息表、影响历史数据与分享/导出逻辑。
- **结论**：采集应放在**执行边界**——既靠近模型调用、又同时覆盖两条 runtime，且不污染消息表。

### 3.2 现状：没有统一调用封装

- 分发点：`runtime/runner.py:191-206`（`Runtime.API` → `definition.stream(ctx)`；`Runtime.CLI` → `stream_cli_run`）。
- API 内核内部：`openai_harness.py:154-206`（`stream` 的工具循环）、`:208-243`（HTTP/SSE）。
- CLI 内核内部：`runner.py:210-424`（子进程生命周期）；适配器只做 argv/stdin 与 stdout 解析。
- 因此不存在「改一处就全采集到」的位置，方案必须明确两条路径的插入点。

### 3.3 推荐方案：执行边界 + 唯一 API 内核调用点（最小侵入）

公共组件（新增）`backend/app/runtime/trace.py`：

- `TraceRecorder`：持 `conversation_id / owner_id / run_id / turn_index / enabled`，提供
  `context(...)`、`request(...)`、`response(...)`、`on_event(event)`、`flush()`。
- `TraceWriter`：独立 `SessionLocal` 批量写 `trace_events`；写失败只 `logger.warning(..., exc_info=True)`，
  绝不打断运行（符合铁律 11：显式记录，不静默）。
- `sanitize(payload)`：复用 `backend/app/credentials.py:108-116` 的 `scrub`，并统一掩码
  `Authorization` / `Cookie` / `Set-Cookie` / `api_key` / `token` / `secret` / `password` 字段。

接入点一：**API 内核（唯一在进程内发 HTTP 的内核）**

- 在 `AgentContext` 增加可选 `trace: TraceSink | None`（叶子协议定义在 `backend/app/agents/base.py`，
  不引入上层依赖）；`openai_harness.py` 只调用协议方法，不知道存储实现。
- `request`：`openai_harness.py:169` 构造出 `payload` 后、`:217` 发送前，记录
  `{transport:"http", url, model, headers(脱敏), body: payload, step}`。
- `response`：在 `_stream_request` 的 `async for line` 结束后，汇总
  `{transport:"http", status_code, finish_reason, usage, raw_sse, chunk_count}`；
  同时补读 `choice.get("finish_reason")`（今天完全没读，见 §2.5）。
- 这一处只改 `openai-harness` 一个适配器 + `agents/base.py` 一个可选字段，改动面可控。

接入点二：**CLI 内核（其余 8 个）**

- `request`：`runner.stream_cli_run` 在 `build_invocation()` 之后（`runner.py:229`）、
  启动子进程（`:266`）之前，记录 `{transport:"cli", argv(脱敏), stdin, env_keys, files, cwd}`。
  注意：这**不是**模型 HTTP 请求体，字段名要如实叫 `invocation`（见 §10 偏差说明）。
- `response`：在读取 stdout 的循环里（`runner.py:368-369`）旁路累积原始行，
  运行结束/取消时写一条 `{transport:"cli", granularity:"run", raw_lines, exit_code, stderr_tail}`。
  双向 JSON-RPC 内核（Codex app-server）把 `agent_run.take_outbound()`（`:373`）的写回也记进 `request`。
- 子进程内部的真实 HTTP 请求/响应**仍不可见**，文档与界面都不得暗示已采集到。

接入点三：**工具调用（两条路径统一）**

- 在 `consume_run` 的事件循环里（`service.py:471-509`）调用 `prepared.trace.on_event(event)`。
  这里是两条 runtime 汇合、且事件已经过 `scrub_event`（`runner.py:200,369`）的唯一位置：
  `tool.call` / `tool.result` 直接落 `tool_call` / `tool_result` 轨迹事件。
- 优点：不改任何 CLI 适配器；`call_id` 天然可用于调用/结果配对。

### 3.4 流式响应（stream chunk）如何拼装完整原始响应

- **API（SSE）**：`aiter_lines()` 的每一行按顺序追加到 `raw_lines`；`data:` 去掉前缀后保留原文
  （含 `[DONE]` 前最后一条），心跳/空行跳过。同时维护 `chunk_count`、`first_chunk_at`、`last_chunk_at`；
  从每个 `choices[*]` 读 `finish_reason`（取最后一个非空值）。
  写库时序列化为一个字符串或 `{lines:[...]}`；超过单事件上限时保留「头 128 KiB + 尾 64 KiB」并标记 `truncated`。
- **CLI（JSONL）**：一次子进程 = 一轮用户消息，可能包含多次模型调用，平台无法切分。
  以 `granularity:"run"` 记录原始行数组；工具调用仍按事件切成独立条目，时间线按 `seq` 排。
- **重复调用**（`openai-harness` 的工具循环 `max_steps`）：每一次 `request`/`response` 都是一对独立轨迹事件，
  `payload.step` 递增，共享同一 `turn_index`；工具调用事件通过 `call_id` 与对应 step 关联。

### 3.5 采集开关的判定层（先在此定义，持久化细节见 §5.6）

- 判定只在**后端运行层**做一次：`prepare_run` / `regenerate_last` 收到 `trace_enabled` 后才创建 `TraceRecorder`；
  为 `False` 时不创建、不写库（前端按钮是否显示只是 UI 约束，不可作为采集依据）。
- `trace_enabled` 由 API 层从**当前登录用户**的设置读取（见 §6.1），不从请求体读取，避免客户端伪造。

---

## 4. 轨迹条目数据结构

### 4.1 JSON Schema（后端权威定义）

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "TraceEvent",
  "type": "object",
  "additionalProperties": false,
  "required": ["id", "session_id", "turn_index", "seq", "timestamp", "type", "payload"],
  "properties": {
    "id":          { "type": "string", "description": "uuid4 hex，主键" },
    "session_id":  { "type": "string", "description": "= Conversation.id（平台对话 id，不是内核 session_id）" },
    "owner_id":    { "type": ["string", "null"], "description": "对话归属用户；鉴权关闭时可为 null" },
    "run_id":      { "type": ["string", "null"], "description": "= ActiveRun.id / turn.started.payload.run_id" },
    "turn_index":  { "type": "integer", "minimum": 0, "description": "该对话第几轮用户消息（0 起）" },
    "seq":         { "type": "integer", "minimum": 0, "description": "同一 run 内严格递增，时间线排序的稳定键" },
    "timestamp":   { "type": "string", "format": "date-time", "description": "UTC ISO8601（带 +00:00）" },
    "type": {
      "type": "string",
      "enum": ["request", "response", "tool_call", "tool_result", "context_snapshot"]
    },
    "payload":     { "type": "object", "description": "按 type 约定，见 4.4" },
    "duration_ms": { "type": ["integer", "null"], "minimum": 0, "description": "request/response/tool 的耗时长；快照为 null" },
    "error":       { "type": ["string", "null"], "description": "该条采集的错误说明（如 HTTP 5xx、超时）" },
    "truncated":   { "type": "boolean", "default": false, "description": "payload 是否因体积上限被截断" },
    "bytes":       { "type": "integer", "minimum": 0, "description": "payload 序列化后的 UTF-8 字节数（截断前）" }
  }
}
```

### 4.2 TypeScript 镜像（前端类型）

```ts
export type TraceEventType =
  | 'request' | 'response' | 'tool_call' | 'tool_result' | 'context_snapshot'

export interface TraceEvent {
  id: string
  session_id: string            // = Conversation.id
  owner_id?: string | null
  run_id?: string | null
  turn_index: number            // 0 起
  seq: number                   // 同一 run 内递增
  timestamp: string             // ISO8601 UTC
  type: TraceEventType
  payload: Record<string, unknown>
  duration_ms?: number | null
  error?: string | null
  truncated: boolean
  bytes: number
}
```

### 4.3 字段类型 / 可空 / 嵌套说明

| 字段 | 类型 | 可空 | 嵌套 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | string | 否 | 否 | `uuid4().hex`，`new_id()` 同款 |
| `session_id` | string | 否 | 否 | 关联 `conversations.id`；**不是** `conversations.session_id` |
| `owner_id` | string | 是 | 否 | 与 `conversations.owner_id` 一致，读接口二次校验用 |
| `run_id` | string | 是 | 否 | 同一轮内所有事件共享；可关联 `turn.started` 事件 |
| `turn_index` | int | 否 | 否 | 关联键之一，见 4.5 |
| `seq` | int | 否 | 否 | 采集器内递增；排序用 `(timestamp, seq)` |
| `timestamp` | string | 否 | 否 | 采集时刻（UTC，`isoformat()` 带偏移） |
| `type` | enum | 否 | 否 | 五类，见 4.1 |
| `payload` | object | 否 | **是** | 各 type 的子结构见 4.4；可含嵌套数组/对象 |
| `duration_ms` | int | 是 | 否 | `context_snapshot` 为 null |
| `error` | string | 是 | 否 | 错误摘要，最长 800 字（与 `runner.py:421` 口径一致） |
| `truncated` | bool | 否 | 否 | 默认 false |
| `bytes` | int | 否 | 否 | 截断前大小，用于前端提示 |

### 4.4 各 type 的 payload 结构

| type | payload 字段（均可缺省） | 说明 |
| --- | --- | --- |
| `context_snapshot` | `system_prompt`(string\|null)、`prompt`(string)、`history`(array<{role,chars}>)、`history_count`(int)、`model`(string\|null)、`workspace_dir`(string\|null)、`kernel_session_id`(string\|null)、`agent_profile_id`(string)、`skills`(string[])、`mcp_servers`(string[])、`user_message_id`(string) | 每轮组装完成后写一条；历史正文按上限截断（默认只存元数据 + 截断文本，见 §8.2） |
| `request` | `transport`("http"\|"cli")、`url`?、`method`?、`model`?、`headers`?（脱敏）、`body`?（HTTP JSON）、`step`?、`invocation`?({argv,stdin,env_keys,files,cwd})、`outbound`?（双向 JSON-RPC 写回） | HTTP 与 CLI 共用 type，靠 `transport` 区分 |
| `response` | `transport`、`status_code`?、`finish_reason`?(string\|null)、`usage`?(object)、`raw`?(string 或 {lines:string[]})、`chunk_count`?、`granularity`?("request"\|"run")、`exit_code`?、`stderr_tail`? | API：每次 HTTP 一条；CLI：按 run 一条 |
| `tool_call` | `name`(string)、`call_id`(string\|null)、`input`(object)、`step`?(int) | 来自规范化事件 |
| `tool_result` | `call_id`(string\|null)、`output`(string)、`is_error`(bool) | 与 `tool_call` 通过 `call_id` 配对 |

### 4.5 与 message / turn 的关联键

- `session_id` ↔ `Conversation.id`：一条对话的全部轨迹 → 轨迹视图按会话读取。
- `turn_index`：`prepare_run` 里已有 `existing = select(Message)...order_by(Message.created_at)`（`service.py:294-300`），
  取其中 `role == "user"` 的条数即为本轮 `turn_index`（0 起）；`regenerate_last` 取被重跑的那条 user 消息的序号。
  该值只写进轨迹事件，**不改 `Message` 表结构**。
- `run_id` ↔ `ActiveRun.id`：与 `turn.started` 事件（`service.py:443-452`）一致，可在轨迹里关联实时运行。
- `payload.user_message_id` / `payload.assistant_message_id`：`prepare_run` 已生成并 `refresh` 过（`service.py:352-353`），
  直接写进 `context_snapshot`，用于从轨迹跳回原消息。
- `tool_call.call_id` ↔ `tool_result.call_id`：沿用现有事件协议（`events.py:53-62`）。

---

## 5. 持久化方案

### 5.1 存储位置与介质选择

**选 SQLite 新表 `trace_events`，与对话同库（`backend/data/coda.db`）。**

理由：

1. 与当前会话同介质、同事务体系；`db.py:27` 已开 `PRAGMA foreign_keys=ON`，可直接用外键级联删除。
2. 读接口可复用现有归属校验（`api/conversations.py:91-97`），分页/过滤是 SQL 的基本能力。
3. JSON 文件方案需要额外的目录、锁、体积统计与清理逻辑，还要自己防路径穿越；本项目已有
   `ADDITIVE_COLUMNS` + `create_all` 的迁移惯例，加表成本最低。
4. 备份/迁移沿用现有数据库即可，不需要单独的数据目录。

### 5.2 表结构（建议）

在 `backend/app/models.py` 新增：

```python
class TraceEvent(SQLModel, table=True):
    __tablename__ = "trace_events"

    id: str = Field(default_factory=new_id, primary_key=True)
    conversation_id: str = Field(
        sa_column=Column(String(64),
                         ForeignKey("conversations.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    )
    owner_id: str | None = Field(default=None, sa_column=Column(String(64), index=True))
    run_id: str | None = Field(default=None, sa_column=Column(String(64), index=True))
    turn_index: int = Field(default=0, sa_column=Column(Integer, nullable=False, default=0))
    seq: int = Field(default=0, sa_column=Column(Integer, nullable=False, default=0))
    type: str = Field(sa_column=Column(String(24), nullable=False, index=True))
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict))
    duration_ms: int | None = Field(default=None, sa_column=Column(Integer))
    error: str | None = Field(default=None, sa_column=Column(Text))
    truncated: bool = Field(default=False, sa_column=Column(Boolean, nullable=False, default=False))
    bytes: int = Field(default=0, sa_column=Column(Integer, nullable=False, default=0))
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime, index=True))
```

索引：`conversation_id`、`(conversation_id, created_at)`（时间线读取）、`(conversation_id, run_id)`（按轮查看）。
新表由 `create_all` 直接建立（`db.py:61-62`），无需 `ADDITIVE_COLUMNS`。

### 5.3 写入时机与事务 / 落盘策略

| 事件 | 写入时机 | 事务策略 |
| --- | --- | --- |
| `context_snapshot` | `prepare_run` 末尾（上下文最终形态确定后，`:324-333` 之后） | **同步 await 写一条**，保证运行崩溃也留得下本轮上下文 |
| `request`（CLI） | `stream_cli_run` 构建完 `Invocation` 后、启动子进程前（`runner.py:229`） | 同步 await；即使子进程起不来也能回看启动参数 |
| `request`（API） | 每次 `client.stream()` 之前（`openai_harness.py:217`） | 异步队列写；采集失败不影响请求 |
| `response` | API：一次 SSE 读完/出错时；CLI：子进程结束/取消时 | 异步队列写，`finally` 前强制 `flush()` |
| `tool_call` / `tool_result` | `consume_run` 收到事件即入队（`service.py:471-509`） | 异步队列，按 200 ms 或 20 条批量提交 |

- 采集写入使用**独立短会话**（`SessionLocal`），不与 `consume_run` 的消息落库共用事务：
  轨迹是旁路，失败不能把正常回复一起回滚。
- `consume_run` 的 `finally`（`service.py:515`）里调用 `await trace.flush()`；顺序放在消息 commit 之后，
  确保「消息已可见 → 轨迹也可读」。
- 写失败：`logger.warning("轨迹写入失败", exc_info=True)`，不向 SSE / 用户抛错（与图片取回失败同一策略，`service.py:527-528`）。
- 关停 / 取消：`_execute_prepared` 的 `CancelledError` 分支（`service.py:412-417`）也 `flush()`，避免丢最后一批。

### 5.4 与 session 的关联方式

- `trace_events.conversation_id` = `Conversation.id`；
- 读取时先用 `_owned_conversation(conversation_id, session, user)`（`api/conversations.py:91-97`）校验归属；
- 删除对话时，现有代码显式删 `Message` 再删 `Conversation`（`api/conversations.py:331-336`），
  `trace_events` 由 `ondelete="CASCADE"` 跟随删除（依赖 `db.py:27` 的 `PRAGMA foreign_keys=ON`）。
- `owner_id` 冗余存一份，便于后续按用户做保留期/配额；读接口仍以对话归属为准。

### 5.5 刷新 / 重启后的读取路径

- 新增只读接口（`backend/app/api/conversations.py`）：
  - `GET /api/conversations/{id}/trace?after_seq=0&limit=100`
    → `{ items: TraceEventRead[], next_after_seq: number|null }`；
  - `GET /api/conversations/{id}/trace/{event_id}`（可选，单条查看）；
  - 导出走 `GET /api/conversations/{id}/trace/export`（阶段 4 可选，见 §6.5）。
- 数据全部来自 SQLite，**不依赖 `ActiveRun.events` 内存缓冲**，因此刷新/重启后可回看。
- 前端进入对话页时若开关开启，可按需拉第一页；不预加载整段。

### 5.6 体积上限与裁剪策略

- **默认不采集**：`trace_enabled` 为 `False` 时不创建 `TraceRecorder`，零写库；判定点在运行层
  `prepare_run` / `regenerate_last`（见 §3.5、§8.1）。
- 单事件 `payload` 上限 **256 KiB**：超限保留头/尾并置 `truncated=true`；
  二进制（图片、base64、附件内容）一律替换为 `{"__omitted__": "binary", "mime": ..., "bytes": ...}`。
- 单对话上限 **2000 条 / 20 MiB**（按 `bytes` 累加）：写入时若超限，按 `created_at` 从旧到新删除，
  保证最新一轮永远可查；被裁剪的条目在导出的元信息里标注 `pruned: true`。
- 单条输出（工具结果）沿用现有上限：`harness_tools.MAX_OUTPUT = 8000`（`harness_tools.py:16`）；
  CLI 原始行沿用 `_MAX_LINE_BYTES = 64 MiB`（`runner.py:28`），但写入轨迹前先截断到 256 KiB。
- 保留期：随对话删除；不做跨对话全局清理（阶段 2 不引入定时任务）。

---

## 6. 前端交互方案

### 6.1 「调试模式」开关：存后端，默认关闭

**决策：存后端用户级字段 `users.trace_debug`（新增可空列，`ADDITIVE_COLUMNS` 补 `BOOLEAN NOT NULL DEFAULT 0`），默认 `False`。**

- 理由：采集必须由后端判定（§3.5、§8.1）；存 `localStorage` 只能控制按钮显隐，无法约束采集，
  也无法在多设备间保持一致。
- 接口：扩展现有「我的资料」链路，不新增独立资源：
  - `UserRead` 增加 `trace_debug: bool = False`（`backend/app/api/auth.py:57-68`）；
  - `UpdateUserRequest` 增加 `trace_debug: bool | None = None`（`:90-94`）；
  - `_to_read` 与 `update_me` 同步读写（`:98-106,241-263`）；
  - 前端 `AuthUser` 增加 `trace_debug?: boolean`（`types.ts:1167-1180`）、`api.updateMe` 入参增加该字段（`api.ts:137-138`）。
- 设置页位置：`SettingsPage.tsx` 的「对话」标签（`:30`、`:154-162`）内新增 `TraceSettingsSection`，
  所有登录用户可见（与「自动起标题」的管理员卡片并列）。
- 默认值：新老用户均为 `False`；开关打开后下一次运行开始采集，关闭后下一次运行不再采集（历史轨迹仍可查看）。

### 6.2 按钮：新建对话图标旁、默认隐藏

- 位置：`ChatPage.tsx` 顶栏左侧 `.chat-topbar-lead` 内，`PlusIcon` 按钮（`:957-965`）右侧新增一个图标按钮
  （建议 `BugIcon` / `RouteIcon`，见 `frontend/src/components/Icons.tsx`）。
- 显隐：`user.trace_debug === true` 才渲染；默认不渲染（不是 `display:none`，避免被误点）。
- 点击行为：打开同路由的轨迹抽屉，**不新建对话、不改变当前对话、不发任何写请求**。
- 侧栏「＋ 新建」（`ConversationList.tsx:186-190`）不动：工单明确按钮位于「新建对话图标旁」，以顶栏为准。

### 6.3 轨迹视图与原对话视图的切换方式：同路由抽屉（推荐）

- 形态：覆盖在对话页右侧的抽屉（复用现有 `.collab-drawer` / `.collab-drawer-backdrop` 样式，
  `frontend/src/index.css:1814-1847`，或新增 `.trace-drawer`），左侧保留对话视图的遮罩。
- 理由（相对「新路由 / 整页切换」）：
  1. 不卸载 `ChatPage`，正在进行的 SSE 订阅与滚动位置不丢（对话页的运行与连接解耦，但组件卸载会 abort 本地流，`ChatPage.tsx:410-490`）；
  2. 刷新/深链仍停在原对话路由，不需要新增路由与权限判断；
  3. 抽屉天然是「只读旁路」，不污染对话视图的布局与状态。
- 也可以做成右侧分栏（窄屏抽屉、宽屏并排）；阶段 3 先实现抽屉，分栏作为可选优化。

### 6.4 时间线排列顺序

- 按 `timestamp` 升序（旧 → 新），同一时刻按 `seq` 升序；顶部显示 `turn_index` 分组标题（「第 N 轮」）。
- 默认滚到底部（最新）；提供「跳到最新」按钮；保持与对话页「跟随下沿」一致的手感。
- `tool_call` 与 `tool_result` 紧邻展示，`call_id` 相同则用连线/缩进表示配对。

### 6.5 条目展开 / 复制 / 导出

- 条目：`<details>` 折叠（与 `MessageView.tsx:321-340` 的 `ToolCard` 同款交互），
  头部显示：类型徽标、时间（`lib/datetime.ts` 的格式化）、`duration_ms`、`error` / `truncated` 警示。
- 展开后：`<pre>{JSON.stringify(payload, null, 2)}</pre>`；超长 payload 默认只渲染前 200 行，
  点「显示全部」再渲染（避免大 JSON 卡死）。
- 复制单条：`navigator.clipboard.writeText(JSON.stringify(event, null, 2))`，失败回退到临时 `textarea` + `execCommand`；
  按钮文案「已复制」反馈。
- 导出整段会话：
  - 结构：`{ version: 1, exported_at, conversation: { id, title, agent_profile_id }, events: TraceEvent[] }`；
  - 下载：`Blob` + `URL.createObjectURL`，文件名 `trace-<conversationId>-<yyyyMMdd-HHmmss>.json`；
  - 若前端尚未加载全部事件，先按 `after_seq` 拉完再导出；超过 10 MiB 先弹确认；
  - 导出前可选「脱敏导出」（隐藏 `context_snapshot.system_prompt` 正文与 `request.headers`），默认勾选。
- 阶段 4 可加后端导出接口（流式 JSON），避免大会话在浏览器里拼装；本阶段先按前端导出设计。

### 6.6 组件拆分与状态管理

| 组件 / 模块 | 职责 |
| --- | --- |
| `frontend/src/lib/trace.ts`（新增） | `TraceEvent` 类型、`fetchTrace` 分页、`exportTrace`、格式化与脱敏工具 |
| `frontend/src/components/TraceDrawer.tsx`（新增） | 抽屉外壳、标题、导出/刷新/关闭、加载与错误态 |
| `frontend/src/components/TraceTimeline.tsx`（新增） | 时间线渲染、按 `turn_index` 分组、滚动与懒加载 |
| `frontend/src/components/TraceEntry.tsx`（新增） | 单条折叠、pretty JSON、复制按钮 |
| `frontend/src/components/TraceSettingsSection.tsx`（新增） | 设置页开关（读写 `api.updateMe`） |
| `frontend/src/pages/ChatPage.tsx`（修改） | 按钮、`traceOpen` 状态、抽屉挂载 |
| `frontend/src/pages/SettingsPage.tsx`（修改） | 「对话」标签插入设置区块 |
| `frontend/src/index.css`（修改） | 抽屉 / 时间线 / 条目样式 |
| `frontend/src/lib/api.ts`、`lib/types.ts`（修改） | 接口与类型 |

- 状态管理：沿用项目现状（无全局 store）。`traceOpen` 放 `ChatPage` 的 `useState`；
  事件数据放 `TraceDrawer` 内部（`useEffect` 拉取 + `useState` 分页），不进入对话的消息状态；
  切换对话时通过 `conversationId` prop 重置。
- 只读：面板内**没有任何** `POST/PATCH/DELETE`；打开抽屉不调用 `api.getConversation` 之外的接口，
  不触发重放、不改会话、不写消息表。

---

## 7. 改动文件清单与不改动清单

### 7.1 逐文件清单（按阶段）

| 文件 | 新增/修改 | 改动要点 | 阶段 |
| --- | --- | --- | --- |
| `docs/trace-debug-view-plan.md` | 新增 | 本文档 | 1（本阶段，已完成） |
| `backend/app/models.py` | 修改 | 新增 `TraceEvent` 表；`users.trace_debug` 列入 `ADDITIVE_COLUMNS` | 2 |
| `backend/app/agents/base.py` | 修改 | `AgentContext.trace: TraceSink | None`，叶子协议（不 import 上层） | 2 |
| `backend/app/runtime/trace.py` | 新增 | `TraceRecorder` / `TraceWriter` / `sanitize` / 裁剪 | 2 |
| `backend/app/runtime/service.py` | 修改 | `prepare_run`/`regenerate_last` 计算 `turn_index`、创建 recorder、写 `context_snapshot`；`consume_run` 采集工具事件、`finally` flush；`trace_enabled` 入参 | 2 |
| `backend/app/runtime/runner.py` | 修改 | CLI `request`（Invocation）/`response`（原始 stdout）采集 | 2 |
| `backend/app/agents/builtin/openai_harness.py` | 修改 | API `request`/`response` 原始采集；读取 `finish_reason` | 2 |
| `backend/app/api/conversations.py` | 修改 | `GET /{id}/trace`（分页）读接口；发送/重生成时把 `user.trace_debug` 传给服务层 | 2 |
| `backend/app/api/auth.py` | 修改 | `UserRead` / `UpdateUserRequest` / `_to_read` / `update_me` 增加 `trace_debug` | 2 |
| `backend/app/schemas.py` | 修改 | `TraceEventRead`（只读响应模型） | 2 |
| `backend/tests/test_trace.py` | 新增 | 开关默认关、采集内容、脱敏、重启后可读、级联删除、上限裁剪 | 2 |
| `frontend/src/lib/types.ts` | 修改 | `TraceEvent`、`AuthUser.trace_debug` | 3 |
| `frontend/src/lib/api.ts` | 修改 | `fetchTrace`、`updateMe` 增加 `trace_debug` | 3 |
| `frontend/src/lib/trace.ts` | 新增 | 分页拉取、格式化、导出、脱敏 | 3（导出在 4 完善） |
| `frontend/src/components/TraceDrawer.tsx` | 新增 | 抽屉外壳与加载态 | 3 |
| `frontend/src/components/TraceTimeline.tsx` | 新增 | 时间线与分组 | 3 |
| `frontend/src/components/TraceEntry.tsx` | 新增 | 单条折叠 / pretty JSON / 复制 | 3（复制在 4 完善） |
| `frontend/src/components/TraceSettingsSection.tsx` | 新增 | 设置页开关 | 3 |
| `frontend/src/pages/ChatPage.tsx` | 修改 | 顶栏按钮、`traceOpen`、挂载抽屉 | 3 |
| `frontend/src/pages/SettingsPage.tsx` | 修改 | 「对话」标签插入设置区块 | 3 |
| `frontend/src/index.css` | 修改 | 抽屉 / 时间线 / 条目样式 | 3 |
| `frontend/src/lib/trace.ts` | 修改 | 导出 JSON 文件、大会话分批 | 4 |
| `backend/app/api/conversations.py` | 修改（可选） | `GET /{id}/trace/export` 流式导出 | 4 |

### 7.2 明确不改动的文件与页面（范围边界）

- **移动端全部**：`frontend/src/mobile/**`（`MobileChatPage.tsx`、`MobileSettingsPage.tsx` 等）。
  移动端是物理独立入口 `/m/`（`AGENTS.md` §0），本工单只覆盖桌面端对话页。
- **对话渲染组件**：`frontend/src/components/ConversationThread.tsx`、`MessageView.tsx`、`Composer.tsx`、`ConversationList.tsx`
  —— 轨迹视图是独立抽屉，不改造消息气泡。
- **其他页面**：`CollabPage.tsx`、`AgentsPage.tsx`、`SkillsPage.tsx`、`McpPage.tsx`、`UsagePage.tsx`；
  `SettingsPage.tsx` 只加「对话」标签内的一块，其他标签不动。
- **其他后端域**：`backend/app/collab/**`、`backend/app/im/**`、`backend/app/api/skills.py`、`gateways.py`、`mcp.py` 等。
- **其他适配器**：`backend/app/agents/builtin/` 下除 `openai_harness.py` 外的 8 个内核一律不改。
- **消息与会话表结构**：`Message`（`models.py:428-442`）与 `Conversation`（`:403-425`）字段不变；
  只新增 `trace_events` 表与 `users.trace_debug` 列。
- **依赖与配置**：不新增 Python / npm 依赖，不改 `.env`、`requirements*.txt`、`package.json`。

---

## 8. 风险与应对

### 8.1 调试模式默认关闭，且后端同样校验

- 默认值：`users.trace_debug` 新列 `BOOLEAN NOT NULL DEFAULT 0`，老库补列后全部为 `False`。
- 判定点（唯一）：`runtime/service.py` 的 `prepare_run` / `regenerate_last`。
  API 层从 `current_user.trace_debug` 取值传入；**不读请求体里的任何开关**，前端无法开启采集。
- 关闭时：不创建 `TraceRecorder`，不写 `trace_events`；`GET /trace` 对历史数据仍可读（只读、归属校验），
  但新数据不会产生。
- 验收：开关关闭发一条消息 → `SELECT count(*) FROM trace_events` 为 0；开关打开再发 → 有数据。
- UI 只是第二道门：按钮在开关关闭时不渲染。

### 8.2 敏感信息（system prompt / API key / 用户隐私）

- **入库前统一脱敏**：复用 `backend/app/credentials.py:108-116` 的 `scrub`；运行层已有的
  `_run_secrets(ctx)`（`runner.py:89-98`）与 `scrub_event`（每事件都过，`runner.py:200,369`）继续作为第一道过滤。
- 请求头只存白名单字段（`Content-Type`、`anthropic-version` 等）；`Authorization` / `Cookie` /
  `Set-Cookie` / `x-api-key` 一律替换为 `***`。
- 字段级掩码：`api_key`、`token`、`secret`、`password`、`authorization`、`cookie` 等键名命中即掩码，
  并在 payload 里记 `redacted_fields: [...]`。
- 展示/复制/导出：
  - 抽屉顶部常驻提示「轨迹含系统提示词与用户内容，请勿外传」；
  - 导出弹窗提供「脱敏导出」开关（默认开）：隐藏 `context_snapshot.system_prompt` 正文（保留长度/hash）、
    隐藏 `request.headers`；单条复制也提示可能含敏感内容。
- `context_snapshot.history` 默认只存 `{role, chars}` 元数据 + 截断正文（上限 8 KiB），
  避免整段历史再复制一份；需要完整上下文时由 CTO 拍板是否放宽（开放问题）。

### 8.3 轨迹数据体积与性能（大 payload / base64 / 图片、虚拟列表、懒加载、分页）

- 写入侧：单事件 256 KiB、单会话 2000 条 / 20 MiB 上限，超限裁旧（§5.6）；
  图片与 base64 只存引用/摘要，不入库。
- 读取侧：`GET /trace` 按 `after_seq` 分页（默认 100 条）；前端懒加载，首屏只拉一页。
- 渲染侧：条目默认折叠；展开的 `<pre>` 默认只渲染前 200 行；超过 300 条时使用窗口化/虚拟列表
  （阶段 4 视实测再决定是否引入，先在 300 条阈值降级为「按轮折叠」）。
- DB：`trace_events` 只建必要索引；WAL 已开启（`db.py:28`），批量写 200 ms/20 条一次，避免逐条事务。
- 大响应：SSE 原始文本按头/尾截断并置 `truncated=true`，前端显示「已截断」徽标。

### 8.4 只读约束（视图不触发重放、不修改会话状态、不写消息表）

- 接口层：轨迹路由**只注册 GET**；不调用 `prepare_run` / `start_run` / `registry.*`；
  不读取 `ActiveRun.events`（避免「重放/继续运行」语义）。
- 前端层：抽屉只调用 `GET /trace`；不调用 `POST /messages`、`/regenerate`、`/cancel`、`PATCH /conversations`。
- 数据层：只读 `trace_events`；不写 `messages` / `conversations`（除开关本身写 `users.trace_debug`，与轨迹视图无关）。
- 验收：
  - 抓包确认打开抽屉只有 GET；
  - 打开前后 `messages` 条数、`conversations.updated_at` 不变；
  - 轨迹页不产生新的 `run.started` / SSE 订阅。

---

## 9. 实现阶段划分

| 阶段 | 内容 | 涉及文件 | 可判定验收点 |
| --- | --- | --- | --- |
| **阶段 1（本阶段）** | 仅方案文档 | `docs/trace-debug-view-plan.md` | `git status` 只有本文件；文档逐项覆盖六类数据、采集层、schema、持久化、前端、风险、阶段 |
| **阶段 2** | 后端采集 + 持久化 | `models.py`、`agents/base.py`、`runtime/trace.py`（新）、`runtime/service.py`、`runtime/runner.py`、`agents/builtin/openai_harness.py`、`api/conversations.py`、`api/auth.py`、`schemas.py`、`tests/test_trace.py`（新） | ① 开关关：0 条；开关开：每个 API 请求有 request/response，每轮有 context_snapshot；② 工具调用有 name/input/output；③ 重启服务后 `GET /trace` 仍返回同样数据；④ 全表 grep 不到测试用密钥；⑤ 删除对话后 trace 行随级联消失；⑥ 超限后旧事件被裁、`truncated` 正确；⑦ `pytest` + `tsc` 通过 |
| **阶段 3** | 前端按钮 + 轨迹视图 | `lib/types.ts`、`lib/api.ts`、`lib/trace.ts`（新）、`TraceDrawer/TraceTimeline/TraceEntry/TraceSettingsSection`（新）、`ChatPage.tsx`、`SettingsPage.tsx`、`index.css` | ① 默认不显示按钮；设置打开后按钮出现；② 打开抽屉按时间线展示事件；③ 展开看到 pretty JSON；④ 刷新/重启后仍可看；⑤ 打开抽屉不发写请求；⑥ 移动端页面无改动、`npm run build` 通过 |
| **阶段 4** | 复制 / 导出 / 性能优化 | `TraceEntry.tsx`、`TraceDrawer.tsx`、`lib/trace.ts`、可选 `api/conversations.py` 导出接口 | ① 单条复制内容与展示一致；② 导出 JSON 可解析、包含元信息与全部事件；③ 大会话（>200 条）分页/懒加载可用；④ 脱敏导出不包含 system prompt 正文与请求头；⑤ 前端 build 与后端测试通过 |

---

## 10. 偏差说明（仓库实际 vs 工单假设）

1. **不存在「统一模型调用封装」**：工单要求优先在模型调用层做单点包装。实际有两条执行路径
   （`Runtime.API` 仅 `openai-harness`；其余 8 个内核 `Runtime.CLI` 起子进程）。
   CLI 内核的 HTTP 请求/响应在子进程内部，平台只能看到 `Invocation` 与 stdout/stderr，
   **无法拿到真实的模型请求体与 HTTP 原始响应**。本方案的 `transport:"cli"` 事件如实记录
   进程入参与原始输出，不冒充「模型请求/响应」。若必须拿到 CLI 真实报文，需要引入本地代理/抓包层，
   属于独立工单，本阶段不做。
2. **对话页有两套物理实现**：桌面端 `frontend/src/pages/ChatPage.tsx` 与移动端
   `frontend/src/mobile/pages/MobileChatPage.tsx`（`/m/` 独立入口）。本方案只覆盖桌面端；
   移动端不改。若移动端也要轨迹视图，需另开工单。
3. **工具调用并非完全缺失**：工单把六类统一按「有无」调研；实际工具调用已部分落在
   `messages.parts`（名称/入参/输出），只是缺时间戳、耗时与请求关联。本方案保留该现状，
   轨迹表另存一份结构化事件，不迁移历史 `parts`。
4. **`finish_reason` 全仓库未读取**：`grep -rn "finish_reason" backend/app frontend/src` 无命中；
   阶段 2 在 `openai_harness` 解析 chunk 时补读，CLI 内核仍依赖内核自身输出（多数不提供）。
5. **设置页「对话」标签目前只有管理员可用的「自动起标题」**（`SettingsPage.tsx:154-162`）；
   本次调试开关是**用户级**，因此同一标签下会出现一个普通用户也能改的区块，需 CTO 确认。
6. **`session_id` 命名冲突**：轨迹 schema 的 `session_id` 指平台对话 id（`Conversation.id`）；
   现有 `Conversation.session_id` 是内核侧会话 id（`models.py:414`）。文档与代码注释必须写清，
   内核会话 id 在 payload 里叫 `kernel_session_id`。
7. **没有现成的 `turn_index` 字段**：由采集器在 `prepare_run` 时按 `messages` 中 user 消息条数计算，
   不改 `Message` 表；`regenerate` 复用被重跑那一轮的序号。
8. **调试开关不随请求体传递**：为保证后端权威，前端无法通过请求参数临时开启采集；
   这会让「在别的设备打开调试」需要先保存设置、下一轮生效，属预期行为。

---

## 11. 仍需 CTO / CEO 拍板的开放问题

1. 阶段 2 是否接受「API 内核完整、CLI 内核只能记录进程入参与原始输出」的采集精度？
   还是要求先做本地代理抓包（范围明显更大）？
2. 调试开关的归属：**用户级**（本方案）还是平台级/仅管理员？若平台级，需改用 `platform_settings`。
3. `context_snapshot` 是否允许保存完整系统提示词与完整历史正文（默认脱敏 + 截断）？
   这直接影响排障效率与隐私风险。
4. 轨迹保留上限（2000 条 / 20 MiB / 单事件 256 KiB）是否合适？是否需要按部署可配置。
5. 是否把 `@ 召唤轮` 与「重新生成」也纳入同一轨迹（本方案默认纳入，建议保留）。
6. 导出是否需要在阶段 4 增加后端流式接口（大会话）还是先只做前端 Blob 下载。

---

## 12. 本阶段 git 工作区自查

> 本阶段只允许新增方案文档。提交前执行 `git status --short` 自查，结果见交付说明；
> 预期为仅 `?? docs/trace-debug-view-plan.md`，无实现代码、配置、依赖改动。
