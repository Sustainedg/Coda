# 上下文用量按钮与自动压缩

输入框上方新增一枚「上下文」按钮，展示**已用 / 上限 / 百分比**；点开是详情面板
（当前已用、可配置上限、触发阈值、压缩策略、最近一次压缩状态），上限与阈值可在面板
里修改并持久化。用量达到阈值时，下一轮发送前会自动把更早的历史折叠成摘要。

## 计量口径（唯一一份）

取数分两档，展示（`GET /api/conversations/{id}/context`）、阈值判断与压缩决策
（`prepare_run` / `regenerate_last`）全部走 `runtime/context.py` 的同一个入口，
前端不另算一套。

### 1. 内核上报的真实快照（优先）

会话型内核（Claude Code、CodeBuddy、Codex app-server 等）自己维护上下文，平台看不到
它内部的历史。适配器每发生一次模型请求就上报一条 `context.usage` 事件，
运行层把它记到 `messages.context_tokens`：

* Claude Code / CodeBuddy：取 `stream_event` 里 `message_delta.usage` 的
  `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`，
  以及 `message_start.message.usage`、完整 `assistant` 消息的 `message.usage`；
* API 型内核（OpenAI 兼容 / DeepSeek Harness）：取每一步的 `prompt_tokens`；
* Codex app-server：取 `thread/tokenUsage/updated` 的 `last.inputTokens`。

> **`message_delta` 才是窗口占用真正的来源**（Claude Code 2.1.327 实测）：`message_start`
> 的 usage 常常是**全 0**，真实数字推迟到 `message_delta` 才给。早先适配器按旧版行为
> 直接跳过 `message_delta`（注释写"它只有 output_tokens"），结果就是**内核永远报不出窗口
> 快照**，面板一直只能显示按内容估算。实测同一轮工具循环：
>
> | 帧 | input_tokens | cache_read | 含义 |
> | --- | --- | --- | --- |
> | `message_start` | 0 | 0 | 全 0，不可用 |
> | `message_delta` | 75 | 14848 | 本次请求的窗口占用 ✅ |
> | `message_start` | 0 | 0 | 全 0，不可用 |
> | `message_delta` | 232 | 14848 | 第二次请求（多了工具结果）✅ |
> | `result` | 307 | 29696 | **整轮累计**，不能当窗口占用 |
>
> 老内核若 `message_delta` 真的只有 `output_tokens`，`context_tokens()` 会算出 0，不会误报。

下一次估算以**最近一次请求的输入 token** 为基准，再加上此后新增的正文。
快照在以下情况自动失效：会话被压缩 / 重置（`session_id` 清空）、快照采集时刻早于最近一次
平台压缩、早于最近一次**内核原生压缩**（见下节）、或快照来自 @ 召唤轮（不写回主场会话记忆）。

> **失效线比的是「快照采集时刻」，不是消息创建时刻**（`messages.context_snapshot_at`）。
> 本轮消息在轮次开始时就建好了，而快照产生于轮次中间；内核原生压缩后上报的
> `post_tokens` 正是「消息早、快照晚」的情况。拿 `created_at` 比压缩时刻，会把压缩后
> 新上报的快照误判成压缩前的旧数据作废，估算回落到「按内容估算」把内核已折叠的历史
> 又算一遍，用量虚高数倍——反过来触发一次多余的平台压缩。

> **记录网关（P1，默认关闭）开启时，窗口快照优先读一手账本**：`usage_records` 里该 run
> 最新一行的 `context_tokens`（代理从上游 `usage` 归一化：Anthropic = 输入 + 缓存读 + 缓存写；
> OpenAI Chat = `prompt_tokens`；OpenAI Responses = `input_tokens`）比内核二次上报更可信。
> 运行收尾用它校准 `messages.context_tokens`；没有账本行才回退上面的原生快照 / 内容估算。
> **运行中面板也直接读该 run 最新账本行**（记录网关请求终态即落账，消息还没落库也能看到
> 最新数字）；原生 / harness 的 `context.usage` 现在也**实时入账**——每个 run 一行、运行中
> 更新，收尾再补计费，所以官方直连内核在运行中同样能看到真实窗口。
> 运行结束后仍以消息快照为主，避免「最新请求 + 其后消息」重复计算。
> 详细口径与互斥规则见 `docs/usage-gateway.md`。

> **平台还会把上限 / 阈值翻译成内核自己的压缩配置**：Claude Code 下发
> `CLAUDE_CODE_AUTO_COMPACT_WINDOW` / `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`，Codex 下发
> `model_context_window` / `model_auto_compact_token_limit`。用户显式设了 `--autocompact`
> 时不覆盖（尊重显式配置）。这样「平台压缩」和「内核原生压缩」共用同一个阈值，不会出现
> 「面板说没超、内核却提前压」。上限在自动模式下跟随内核真实窗口（`context.window`）。

### 2. 按内容估算（没有快照时的兜底）

| 内容 | 估算规则 |
| --- | --- |
| 中日韩文字（含全角标点、假名、谚文） | 1 字 ≈ 1 token |
| 其余字符 | 4 字符 ≈ 1 token |
| 每条消息 | 额外 4 tokens（角色 / 分隔开销） |
| 工具调用 | 工具名 + 入参 JSON |
| 工具结果 | 输出全文 |
| 推理内容（thinking） | 全文 |

估算范围按内核能力区分，保证「算的就是真正会发出去的」：

* 会话型内核且 `session_id` 还在：内核转录里包含工具调用与工具结果，按全部有效消息
  + 工具片段估算；
* 非会话型内核（或会话已压缩 / 重置）：平台只回放最近 **24 条 / 40000 字符**的
  **最终回复**，工具片段不计（`runtime.context.clip_messages` 与实际发送共用同一份规则）。

`text` 片段与 `message.content` 是同一份内容，只计一次；正文为空但带 `parts` 的
消息（只调工具、还没产出文字的回合）不会被丢掉。

> 这是**近似估算**（快照档除外），不是计费口径。各内核上报的 `usage` 是整轮累计
> （用量页），不能直接当作「当前窗口占用」；`context.usage` 才是单次请求的窗口快照。

> **升级提示**：升级前的旧消息没有 `context_tokens` 快照，第一次打开时会按内容
> （含历史工具调用与结果）估算，数字可能明显高于旧版本；下一次运行完成后会自动
> 以内核上报的快照校准。若旧对话的估算已超过阈值，下一条消息会触发一次自动压缩
> （原文一条不丢，只是更早的部分折叠进摘要）。

默认值：上限 **128000** tokens，触发阈值 = 上限的 **80%**。

## 配置持久化

沿用仓库现有的键值配置表 `platform_settings`（与标题设置同一套读写约定）：

- 键：`context_settings:<owner_id>`（鉴权关闭 / 单用户部署时 owner 为空）
- 值：`{"limit_tokens": 128000, "threshold_ratio": 0.8}`
- 键不存在或损坏 = 回到默认值（日志留痕，不让坏数据弄崩界面）

接口：

- `GET /api/system/context`：读当前配置
- `PUT /api/system/context`：保存；`conversation_id` 可选，带了就校验「上限不得低于
  该对话当前已用量」
- `GET /api/conversations/{id}/context`：用量 + 阈值 + 压缩状态（按钮的数据源）

校验（前后端各一道，服务端为准，非法输入 422 且不落盘）：

- 上限必须是正整数、≥ 1000、且不低于当前已用量；
- 阈值（百分比）必须满足 `0 < 阈值 ≤ 100%`（换算成比例后自然满足「阈值 ≤ 上限」）。

## 自动压缩

触发点：`prepare_run`（以及 `regenerate_last`）在构建历史之前估算
「系统提示 + 历史 + 本轮提示」。达到阈值就压缩：

1. **保留系统提示**（不动 `AgentContext.system_prompt`）；
2. **保留最近若干轮**：按「上限的一半」从最新一条往前收，最多 8 条、至少 2 条；
3. **更早的历史折叠成摘要**：一行一条（用户/智能体 + 首 80 字），摘要最多占上限的
   1/5；原文一条不删，仍完整保存在 `messages` 表里（对话记录 / 导出不受影响）；
4. 压缩后清掉内核侧 `session_id`：session 型内核无法在自己的面板里裁剪上下文，
   这一轮改用「摘要 + 最近若干轮」注入；下一轮内核会建立新会话。
5. 失败（例如最近两条本身就超过目标）**不改任何已存上下文**，本轮沿用原始历史，
   并通过运行日志 + 按钮状态给出「压缩失败，已保留原上下文」的提示。

压缩是确定性的裁剪 + 摘要，不调模型、不阻塞用户继续输入。同一对话有
`begin_compaction` 占位，避免重复触发。最近一次压缩的状态（进行中 / 成功 / 失败）
通过 `GET /api/conversations/{id}/context` 返回，按钮上显示徽标、面板里显示详情。

## 与内核原生压缩的协同（以 Claude Code 为例）

Claude Code 自带 auto-compact（`--autocompact <auto|tokens>`）：上下文逼近内核窗口时，
它**在会话内**把旧消息折叠成摘要，并向 stream-json 输出一条 `system/compact_boundary`
事件（含 `compact_metadata`：`trigger`、`pre_tokens` / `post_tokens`、
`messages_summarized` 等）。平台侧与它是两套独立机制，对接点有三：

1. **事件接收**：`claude_code.py` 的 `_dispatch` 解析 `compact_boundary`，上报
   `context.compacted` 事件（字段白名单透传）；`runtime/service.consume_run` 收到后
   在运行日志里发出中文提醒，并记录 `conversations.context_kernel_compacted_at`。
2. **快照失效**：内核压缩不动 `session_id`，但压缩前的 `context_tokens` 快照已不代表
   当前窗口。`_snapshot_index` 把 `context_kernel_compacted_at` 当作快照失效线：
   早于它的快照一律作废，估算回落到「按内容估算」档；若内核上报了 `post_tokens`
   （压缩后的窗口占用），它直接成为最新快照。
3. **参数透传**：智能体配置字段 `autocompact`（高级）透传 `--autocompact`；留空走
   内核默认。两套压缩的关系：平台压缩会清 `session_id` 重开会话（换历史注入），内核
   压缩在会话内完成（会话连续）；两者可以并存，互不替代。

### 每个内核分别怎么接（不要指望一个适配器通吃）

各内核的 wire 协议不同，必须逐内核对接；下表是**实测过 CLI 二进制 / 官方 schema**
的现状：

| 内核 | 窗口快照（`context.usage`） | 原生压缩事件（`context.compacted`） | `autocompact` 透传 |
| --- | --- | --- | --- |
| Claude Code | `message_start` / `assistant` 的 usage | `system/compact_boundary` + `compact_metadata` | ✅ `--autocompact` |
| CodeBuddy | 同上（协议同构） | `system/compact_boundary`（`-p` 会话手动 `/compact` 时发出，只带 `trigger` / `pre_tokens`） | ✅ `--autocompact` |
| Codex app-server | `thread/tokenUsage/updated` 的 `last.inputTokens` | 新协议 `item/completed` 的 `contextCompaction` 条目；老版本 `thread/compacted` 通知（官方标 Deprecated，两个都认） | ✖ 由内核自身配置控制 |
| DeepSeek Harness / OpenAI Harness | 每一步的 `prompt_tokens`（平台自己发请求，历史全由平台拼） | 无内核原生压缩：平台压缩就是唯一压缩 | — |
| Antigravity CLI / Custom CLI | ✖ 协议只在终态 `result` 给累计用量，没有「单次请求」窗口占用 | ✖ 无 | — |
| Doubao CLI | ✖ 现协议不上报 token 用量 | ✖ 无 | — |
| Hermes | ✖ `stream-json` 只在终态 `result` 给整轮累计 token（`session_input_tokens` 是累计值，不是窗口占用） | ✖ `stream-json` 不暴露其内部 context engine 的压缩 | — |

没有快照的内核不会「假装有数」：`measured` 为 `false`，界面明确显示「按内容估算」，
阈值判断与压缩决策照常工作。想让某个内核接入，先在它的 wire 协议里找到**单次请求的
输入 token**（不是整轮累计）与**压缩完成信号**，再按上表的同样两件事补齐。

「上限对齐」现在有两种模式（`context_settings` 的 `limit_mode`）：

* `auto`（默认，新用户）：上限**跟随内核上报的真实窗口**——Codex 的
  `tokenUsage.modelContextWindow`（官方 schema 里的稳定字段），经新增的 `context.window`
  事件落到 `conversations.context_kernel_window_tokens`。
  Claude Code 的解析器也实现了（顶层 `autocompact_state` 帧的 `value.effective_window`），
  但 2.1.327 的 `-p` print 模式**实测不下发该帧**，所以实际走兜底值。
  内核没报过就退回用户配置值（默认 128k）。老配置行没有这个键 = 读作 `manual`，
  升级不会悄悄改掉谁的上限。
* `manual`：上限永远以用户设定为准，**自动对齐不会覆盖人的选择**。

想让内核压缩先出手：把上限调高或给智能体配 `autocompact`；想让平台接管：手动模式 +
一个较小的上限——平台压缩会在内核窗口打满前（按平台口径）先把历史折叠掉。

## 改动文件

后端：

- `backend/app/runtime/context.py`（新增）：估算口径、配置读写与校验、压缩、状态；
- `backend/app/models.py`：`Conversation` 增加 `context_summary` /
  `context_compacted_count` / `context_compacted_at` / `context_kernel_compacted_at`，
  `Message` 增加 `context_tokens` / `context_snapshot_at`（含旧库补列声明）；
- `backend/app/agents/events.py`：新增 `context.usage`（单次请求的输入 token）、
  `context.window`（内核真实窗口）与 `context.compacted`（内核原生压缩）三个事件；
- `backend/app/agents/builtin/{claude_code,codebuddy_cli,codex,deepseek_harness,
  openai_harness}.py`：在每次模型请求后上报 `context.usage`；
  `claude_code` / `codebuddy_cli` 解析 `compact_boundary`，`codex` 解析
  `contextCompaction` / `thread/compacted`，三者上报 `context.compacted`；
  `claude_code` / `codebuddy_cli` 支持 `autocompact` 参数透传；
- `backend/app/runtime/service.py`：`prepare_run` / `regenerate_last` 接入压缩，
  任务卡与凭据说明并入估算，`context.usage` 落到 `messages.context_tokens` 并记录
  `context_snapshot_at`，`context.compacted` 记录 `context_kernel_compacted_at`；
- `backend/app/api/conversations.py`：`GET /conversations/{id}/context`；
  `/clear` 与删除对话时清理压缩状态；
- `backend/app/api/system.py`：`GET/PUT /system/context`；
- `backend/tests/test_context.py`（新增）：估算、配置持久化与校验、自动压缩成功 /
  失败兜底、清除记忆。

前端：

- `frontend/src/components/ContextMeter.tsx`（新增）：按钮 + 详情面板；
- `frontend/src/lib/contextUsage.ts`（新增）：取数 / 校验 / 展示格式化；
- `frontend/src/lib/api.ts`、`frontend/src/lib/types.ts`：接口与类型；
- `frontend/src/pages/ChatPage.tsx`、`frontend/src/mobile/pages/MobileChatPage.tsx`：
  接入输入框上方；
- `frontend/src/index.css`、`frontend/src/mobile/mobile.css`：两端样式（共享段逐字一致；
  移动端在共享段之后补了一条「面板输入框 16px」的微调，避免 iOS 聚焦时强制放大页面）。

面向使用者的完整功能说明见 `docs/功能文档/上下文窗口与自动压缩.md`。

## 手动验证

1. 打开任意对话，输入框上方出现「上下文 x/y · z%」按钮；消息变化后数字跟着变。
2. 点开按钮：能看到当前已用、可配置上限、触发阈值（tokens + %）、压缩策略与估算口径。
3. 把上限改成 20000、阈值 80% 保存 → 刷新页面，数值仍是 20000 / 80%。
4. 非法输入各试一次：`abc`、`0`、`-1`、比当前已用更小的上限 → 面板内出现中文提示，
   刷新后配置未变。
5. 自动压缩：新建一条对话，把上限设为 4000、阈值 80%，连续发送几条较长的消息，
   直到按钮百分比越过 80%；再发下一条 → 运行日志出现「上下文已自动压缩：N 条较早
   消息折叠为摘要」，按钮出现「已压缩」徽标，对话可继续。
6. 失败兜底（自动化测试覆盖）：`backend/tests/test_context.py`
   `test_compaction_failure_keeps_original_context` 断言压缩抛错时消息一条不少、
   状态为 `error`、本轮照常完成。
