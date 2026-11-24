# 任务进度卡片 · 只读现状调研报告

> 状态：**只读调研**（本轮零业务代码改动，唯一写入即本文件；`git status` 已核对）。
> 目标读者：CTO（评审 / 拆实现任务）、QA（对照验收标准核查）。
> 调研方式：逐文件阅读 + 命令验证（见 §9）；每条结论都附文件路径，关键结论附行号。
> 行号为撰写本报告时的仓库快照（2026-11-24），后续改动可能使行号漂移。

---

## 1. 项目整体结构、技术栈与验证命令

| 项 | 事实 | 证据 |
| --- | --- | --- |
| 产品 | Coda —— 多智能体对话控制台（把 Claude Code / Codex / Gemini CLI 等内核配成「智能体」对话） | `README.md:1-30`、`AGENTS.md` §0 事实基线 |
| 后端 | Python 3.11 · FastAPI 0.115.6 · SQLModel 0.0.22 + SQLite(aiosqlite 0.20.0) · Pydantic v2 · uvicorn[standard] 0.34.0 | `backend/requirements.txt:3-6` |
| 前端 | React 18.3.1 + TypeScript 5.7.2 + Vite 6.0.7 · react-router-dom 6.28 · react-markdown 10.1.0 + remark-gfm 4.0.1 · **无 UI 框架，手写 CSS** | `frontend/package.json:12-27` |
| 后端入口 | `backend/app/main.py`（FastAPI 实例 `:126-132`，lifespan `:82-123`，路由聚合 `:149`） | `backend/app/main.py` |
| API 前缀 | `/api`，路由聚合在 `backend/app/api/__init__.py:22-38` | 同左 |
| 构建与启动 | `./scripts/serve.sh`（构建前端 + 后端托管，监听 `0.0.0.0:8010`）；前后端分离开发 `./scripts/dev.sh`（API `:8000` + Vite `:5173`，`/api` 反代） | `scripts/serve.sh:28-33`、`scripts/dev.sh:11-44` |
| 验证命令 | `bash scripts/test.sh` = 后端 pytest + 前端 `tsc --noEmit`；前端单独 `npm --prefix frontend run typecheck` / `npm run build` | `scripts/test.sh:26-37`、`frontend/package.json:7-11` |
| 编码规范 | `AGENTS.md`（铁律 §1：中文注释与文案、不新增依赖、数据库向后兼容、改完必须验证）；风格细则 §5 | `AGENTS.md:34-67,513-549` |
| 移动端 | 物理独立第二条链接 `/m/`（独立入口 `frontend/m/index.html` + `frontend/src/mobile/`，Vite 多入口 `:31`） | `frontend/vite.config.ts:25-33`、`frontend/src/mobile/main.tsx` |
| 协作域 | `backend/app/collab/`（多智能体工单协作，独立于对话域） | `AGENTS.md` §0、`backend/app/collab/__init__.py:1-8` |

**本轮实测**（只读验证）：

- `npm --prefix frontend run typecheck` → 通过（tsc --noEmit 零报错）。
- `backend/.venv/bin/python -m pytest backend/tests --collect-only -q --rootdir=backend` → **837 tests collected**（环境可用，未跑全套）。

## 2. 前端：对话页面技术栈与组件结构

| 角色 | 文件 | 位置 |
| --- | --- | --- |
| 路由与外壳 | `frontend/src/App.tsx`（`/chat` 与 `/chat/:conversationId` 两条路由 `:197-198`；左侧栏含对话列表 `:149`） | 同左 |
| **对话页容器** | `frontend/src/pages/ChatPage.tsx`（1571 行）：状态 `messages/active/live` `:111-113`，打开对话 `openConversation` `:222-246`，发送 `send` `:438-555`，队列 `submitDraft` `:576-602`，跨设备轮询 `useAutoRefresh(3s)` `:330`，重连订阅 `:338-402` | 同左 |
| **消息列表组件** | `frontend/src/components/ConversationThread.tsx`（60 行）：条目模型 `ConversationThreadItem` `:6-21`，渲染 `ConversationThread` `:36-60` | 同左 |
| **单条消息渲染组件** | `frontend/src/components/MessageView.tsx`（398 行）：`RenderMessage` `:9-31`，`MessageView` `:33-185`，时间线分组 `groupTimeline` `:205-224`，工具卡 `ToolCard` `:323-340`，附件画廊 `AttachmentGallery` `:350-378` | 同左 |
| Markdown 渲染 | `frontend/src/components/MarkdownView.tsx`：react-markdown + remark-gfm，`skipHtml` `:18`，自定义 `pre/code/a/img` 渲染器 `:25-55` | 同左 |
| 移动端对话页 | `frontend/src/mobile/pages/MobileChatPage.tsx`（1505 行）：**不经 `ConversationThread`**，直接 `.map` 渲染 `<MessageView>` `:1034-1085` | 同左 |
| 流式状态归约 | `frontend/src/lib/agentStream.ts`：`applyAgentEvent` `:37-91`（桌面对话页与协作页共用） | 同左 |
| 接口层 | `frontend/src/lib/api.ts`（668 行）：对话方法 `:405-452`，SSE 封装 `streamPost/streamGet` `:585-668` | 同左 |
| 类型定义 | `frontend/src/lib/types.ts`（1402 行）：`Message` `:752-764`、`MessagePart` `:741-750`、`Conversation` `:690-718`、`ConversationDetail` `:573-577`、`ActiveRunInfo` `:555-570`、`AgentEvent` `:770-780` | 同左 |

## 3. 消息渲染链路（数据 → 屏幕）

三条链路汇聚到同一渲染终点（`threadItems` → `ConversationThread` → `MessageView`）：

**① 历史消息（打开 / 轮询刷新）**

```
GET /api/conversations/{id}          （api.ts:412 getConversation）
→ ChatPage.openConversation          （ChatPage.tsx:222-246）
→ applyDetail                        （ChatPage.tsx:197-220，setMessages/setActive/setActiveRun）
→ chatMessages 过滤占位 + 映射       （ChatPage.tsx:881-901；:878-889 藏 livePlaceholderId 对应的 streaming 占位）
→ threadItems 组装                   （ChatPage.tsx:946-990，含 @ 召唤身份 assistantIdentity :924-944）
→ <ConversationThread items>         （ChatPage.tsx:1088-1108）
→ MessageView → MarkdownView         （MessageView.tsx:33/142-161 → MarkdownView.tsx:15-23）
```

**② 本端发送（SSE 流式）**

```
Composer 输入 → submitDraft          （ChatPage.tsx:576-602）
→ send()                             （ChatPage.tsx:438-555）
→ streamPost POST /api/conversations/{id}/messages   （ChatPage.tsx:505-511 → api.ts:635-647）
→ 每条事件 applyAgentEvent 并入 live 状态             （ChatPage.tsx:525 → agentStream.ts:37-91）
→ liveMessage 组装                   （ChatPage.tsx:903-915，status=streaming）
→ threadItems 尾部追加 live 条目     （ChatPage.tsx:961-989）
→ 同一渲染终点
```

**③ 另一台设备 / 刷新后重连**

```
详情里 active_run 非空 → 订阅 effect  （ChatPage.tsx:338-402）
→ streamGet GET /api/conversations/{id}/stream       （ChatPage.tsx:368-372 → api.ts:662-668）
→ 同一 applyAgentEvent 归约；流结束回 refreshActive   （ChatPage.tsx:379-389）
```

**消息数据结构定义位置**

- 前端权威类型：`frontend/src/lib/types.ts:741-764`（`MessagePart.kind` 仅四种 `'thinking' | 'text' | 'tool_call' | 'tool_result'`，`:743`；`Message` 含 `content/parts/usage/status/error/agent_profile_id`）。
- 后端响应模型：`backend/app/schemas.py:540-553`（`MessageRead`，`parts: list[dict[str, Any]]` **无 kind 校验**，任意 JSON 都能落库返回）。
- 事件类型：后端 `backend/app/agents/events.py:14-24`（`EventType` 枚举 10 类）；前端 `frontend/src/lib/types.ts:766-780`（`AgentEventType` 12 类，多出传输层 `turn.started`/`stream.closed`，见 `runtime/service.py:587-596`、`transport/sse.py:66`）。

**是否支持自定义消息类型 / 富内容 / 附件**

- `MessagePart` **没有**自定义 kind 通道：前端 `MessageView.TimelinePart` 只认 `thinking/text`，其余一律当作工具卡塞进「过程」折叠块（`MessageView.tsx:300-321`，兜底 `:320 return <ToolCard part={part} />`；`groupTimeline` `:205-224` 把所有非 text 归为 process 块）。
- 富内容：正文走 Markdown（GFM 表格/代码块/图片），图片经 `MediaUrlProvider` 解析成后端只读文件 URL（`MarkdownView.tsx:62-68`、`frontend/src/lib/media.tsx:17-108`）；附件清单由后端写进正文末尾「📎 附件」块，前端拆出画缩略图（`MessageView.tsx:50-63`、`media.tsx:90-107`）。
- 结论：**消息级「卡片」目前没有渲染通道**，只能借下面 §8 的既有先例挂进线程。

## 4. 前端本地持久化现状（localStorage）

只使用 `localStorage`；**未找到** sessionStorage / IndexedDB 的使用（全仓 grep `sessionStorage|indexedDB` 零命中）。键名统一 `coda.` 前缀：

| 键 | 用途 | 文件 |
| --- | --- | --- |
| `coda.auth.token` / `coda.auth.user` | 会话令牌与用户 | `frontend/src/lib/auth.ts:10-11` |
| `coda.theme` | 主题模式 | `frontend/src/lib/theme.ts:22` |
| `coda.sidebar` | 侧栏折叠（`'collapsed'/'open'`，`useSyncExternalStore` + 自定义事件广播） | `frontend/src/lib/sidebarCollapsed.ts:10-31` |
| `coda.composer.drafts` | 每对话/每次协作运行的输入草稿（含容量淘汰 `MAX_DRAFTS=50` 与读入校验） | `frontend/src/lib/composerDraft.ts:19-73` |
| `coda.tools-hidden` | 「只看最终回复」开关 | `frontend/src/lib/finalOnly.ts:15` |
| `coda.tts` | 朗读开关 | `frontend/src/lib/tts.ts:13` |
| `coda.auto-check-updates` | 自动检查更新的小时 | `frontend/src/lib/autoCheckUpdates.ts:15` |
| `coda.collab.notices.dismissed` / `coda.collab.advance.consumed` | 协作页已关闭提醒 / 已消费推进 | `frontend/src/lib/collabNotices.ts:8`、`frontend/src/lib/collabAdvance.ts:10` |

可复用的折叠状态范式：`lib/sidebarCollapsed.ts`（模块级真值 + `useSyncExternalStore` + 同页广播）或 `lib/collapse.ts` 的 `useExpandMap`（页内 `Record<string, boolean>`，`frontend/src/lib/collapse.ts:14-22`，**不落盘**）；`lib/composerDraft.ts` 展示了「按 id 记忆 + 隐私模式降级 + 坏数据丢弃」的成熟写法。

## 5. 后端：服务框架、对话接口与流式输出

**框架与入口**：FastAPI（`backend/app/main.py:126-132`）；中间件顺序：`SSEExemptGZipMiddleware`（gzip 对 SSE 显式豁免，`main.py:140`、`backend/app/transport/gzip.py`）→ CORS（`main.py:141-147`）；SPA 兜底 `/api` 显式 404（`main.py:179-198`）。**传输方式：普通请求 + SSE，未找到 WebSocket**（IM 平台对第三方是 WebSocket 客户端，`backend/app/im/platforms/*`，与本平台 API 无关）。

**SSE 实现**：通用封装 `backend/app/transport/sse.py`（`format_event` `:18-21`、`sse_stream` 15 秒心跳 `:28-66`）；响应包装 `api/conversations.py:496-501`（`StreamingResponse, media_type=text/event-stream`）。前端手动 fetch 读流解析（`api.ts:591-633`，因 `EventSource` 无法带请求体）。

**对话相关接口清单**（路由前缀 `/api/conversations`，`backend/app/api/conversations.py:51`；均过 `current_user` 鉴权 + 严格归属校验 `_owned_conversation` `:94-103`）：

| 方法与路径 | 函数 | 请求 → 响应 | 行号 |
| --- | --- | --- | --- |
| GET `/api/conversations` | `list_conversations` | `?include_archived` → `{items: ConversationRead[], active_runs: ActiveRun[]}` | `:111-180` |
| POST `/api/conversations/{id}/read` | `mark_conversation_read` | 空 → 204 | `:183-197` |
| POST `/api/conversations` | `create_conversation` | `ConversationCreate{title?, agent_profile_id?, workspace_dir?}`（`schemas.py:478-482`） → 201 `ConversationRead` | `:200-233` |
| GET `/api/conversations/{id}` | `get_conversation` | → `{conversation, messages: MessageRead[], active_run}` | `:236-269` |
| GET `/api/conversations/{id}/context` | `get_conversation_context` | → `ContextUsage`（估算口径在后端 `runtime/context.py`） | `:272-290` |
| GET `/api/conversations/{id}/trace` | `list_trace_events` | `?after_seq&limit` → `{items: TraceEventRead[], next_after_seq}` | `:293-339` |
| PATCH `/api/conversations/{id}` | `update_conversation` | `ConversationUpdate`（换智能体/目录清会话记忆） | `:342-400` |
| DELETE `/api/conversations/{id}` | `delete_conversation` | → 204 | `:402-419` |
| POST `/api/conversations/{id}/session/reset` | `reset_session` | → `{ok}` | `:421-438` |
| POST `/api/conversations/{id}/attachments` | `upload_attachment` | `AttachmentUpload{name, content_base64}`（≤8MB） → `AttachmentRef` | `:440-465` |
| GET `/api/conversations/{id}/files/{rel_path}` | `get_conversation_file` | → FileResponse（只读工作目录） | `:467-493` |
| GET `/api/conversations/{id}/stream` | `stream_active_run` | → **SSE**，订阅当前运行事件缓冲重放 | `:519-535` |
| GET `/api/conversations/{id}/models` | `conversation_models` | → `ConversationModelOptions` | `:538-567` |
| POST `/api/conversations/{id}/messages` | `send_message` | `SendMessage{content, attachments[], workspace_dir?, reset_session, summon_profile_id?}`（`schemas.py:590-611`） → **SSE** | `:570-598` |
| POST `/api/conversations/{id}/regenerate` | `regenerate` | → **SSE** | `:601-615` |
| POST `/api/conversations/{id}/cancel` | `cancel_run` | → `{ok}` | `:618-626` |

另有运行总览 `GET /api/runs` 与 `POST /api/runs/{run_id}/cancel`（`backend/app/api/runs.py:14-30`）。

**流式输出实现位置**：

1. `send_message` → `runtime/service.prepare_run`（准备上下文、落库用户消息与 `status="streaming"` 占位助手消息，`service.py:346-486`，占位 `:434`）→ `service.start_run`（`:580-597`）：先同步发布 `turn.started` 进缓冲，再 `asyncio.create_task(_execute_prepared)` **把运行与 HTTP 连接解耦**。
2. `_execute_prepared`（`:533-577`）自开 `SessionLocal` 执行 `consume_run`（`:600-701`）：逐事件 `run.publish(event.to_dict())` 进 `ActiveRun` 缓冲并广播订阅者；`finally` 把正文/parts/usage/终态写回 `messages` 并 commit（`:665-694`）。
3. HTTP 侧 `_stream_prepared` / `stream_active_run` 只订阅 `run.subscribe()`（`api/conversations.py:504-535`）；`ActiveRun.subscribe` 先重放缓冲再接实时事件（`runtime/manager.py:78-100`）。
4. 事件到 SSE 帧：`transport/sse.py:sse_stream`（心跳 15s）。

## 6. 存储：介质、数据访问层、会话 ID

| 项 | 事实 | 证据 |
| --- | --- | --- |
| 介质 | SQLite（异步 aiosqlite + WAL + `PRAGMA foreign_keys=ON`），默认 `<data_dir>/coda.db`；可用 `CODA_DATABASE_URL` 覆盖；本仓 `backend/data/coda.db` 为真实数据（受 `.gitignore` 保护） | `backend/app/db.py:13-29`、`backend/app/settings.py:79-87`、`.env.example:6-11` |
| ORM / 数据访问层 | SQLModel；引擎与会话工厂 `db.engine/SessionLocal/init_db`（`db.py:13-64`，启动建表 + 幂等补列 `_add_missing_columns`）；领域模型集中在 `backend/app/models.py` | 同左 |
| 对话表 `conversations` | `id`(uuid hex) · `title` · `agent_profile_id` · **`session_id`** · `workspace_dir` · `model` · `archived` · `created_at/updated_at` · `last_read_at` · `context_summary/context_compacted_*` · `owner_id` | `backend/app/models.py:415-446` |
| 消息表 `messages` | `id` · `conversation_id`(FK CASCADE) · `role`(user/assistant/system) · `content` · **`parts`(JSON)** · `usage`(JSON) · `status` · `error` · `created_at` · `agent_profile_id` | `backend/app/models.py:449-463` |
| 轨迹表 `trace_events` | 会话内严格递增 `seq`，`type` 五类，`payload` JSON，`truncated/bytes` | `backend/app/models.py:466-502` |
| 协作域表 | `collab_teams/collab_runs/collab_tickets/collab_steps/collab_messages`（步骤含 `parts/usage/gist/audit`，消息含 `meta` JSON） | `backend/app/collab/tables.py:60-277` |
| 会话 ID 生成 | **平台不生成**：新建对话时 `session_id=None`（`runtime/service.new_conversation:803-810`），首轮运行由内核在运行中回报 `session` 事件，`consume_run` 写回 `conversation.session_id`（`service.py:642-648`；`persist_session` 语义 `:59-61`，@ 召唤轮不写回 `:373-420`）。主键 `id` 全部 `uuid4().hex`（`models.new_id:93-94`；运行 id `manager.py:118`） | 同左 |
| 会话 ID 传递 | 每轮 `build_context` 把 `conversation.session_id` 传进 `AgentContext.session_id`（`service.py:114-158`，`:158`），适配器据此 resume 内核会话；不支持 session 的内核回放最近 N 轮 transcript（`service.py:171-200,275-304`） | 同左 |
| 工作目录 | 显式指定或按对话 id 分配沙箱 `<data_dir>/workspaces/<conversation_id>`（`service.resolve_workspace:77-87`） | 同左 |

## 7. 重启恢复机制

| 域 | 重启后行为 | 证据 |
| --- | --- | --- |
| 对话与消息本体 | **完整持久化、天然可恢复**：`messages` 全部落库（`consume_run` finally commit，`service.py:665-694`）；重启后前端 `GET /api/conversations/{id}` 直接从库里读回全部消息（`api/conversations.py:236-269`），无需任何启动加载逻辑 | 同左 |
| 进行中的运行 | **没有恢复**：`ActiveRun`/事件缓冲全在内存（`runtime/manager.py:39-45,103-166`），重启即失；**对话域启动时没有把残留 `status="streaming"` 占位消息收尾的代码**（全仓检索 `interrupted` 仅命中协作域）。残留占位只靠两处兜底：列表排序不认 streaming 判「在跑」（`runtime/conversations.py:94-130`，注释 `:104-105` 明说重启残留不该钉顶）；未读判定跳过 streaming（`runtime/conversations.py:23-33`） | 同左 |
| 协作域 | 有显式恢复：启动时把残留 `running` 运行与 `streaming` 步骤/消息标为 `interrupted`，可「继续」 | `backend/app/collab/store.py:221-244`，接线 `main.py:101-106`（`collab.recover_interrupted`） |
| 会话记忆 | **可跨重启续接**：`conversation.session_id` 在库里，重启后下一轮 `build_context` 原样传给内核 resume（`service.py:158`） | 同左 |
| 其余启动逻辑 | 建表/播种/回填归属/技能编目/补编排图快照 | `main.py:82-123`、`backend/app/bootstrap.py:60-191` |

**与「进度卡片重启可恢复」相关的直接结论**：卡片数据只要落进 `messages.parts`（JSON 列，无 schema 校验，`schemas.py:547`）或新表，就能与消息同一条链路恢复；进程内状态（运行缓冲、压缩状态 `ContextCompactionState`，`types.ts:583-591`）重启即丢，不能作为卡片的持久化依据。

## 8. 可复用扩展点与结构化卡片先例

| 扩展点 | 现状 | 证据 |
| --- | --- | --- |
| **线程条目 `custom` 槽** | `ConversationThreadItem.custom?: ReactNode`：非普通消息内容直接替换整个气泡（`ConversationThread.tsx:17-19`，渲染分支 `:44`）。**单聊 ChatPage 未使用**；协作岗位线程已用它渲染「工单交接卡」 | `frontend/src/components/CollabAgentThread.tsx:155-192` |
| **线程条目 `meta` 槽** | 气泡上方的岗位/工单元信息行（协作步骤编号、工单 chip、状态徽标） | `ConversationThread.tsx:16-17,43`、`CollabAgentThread.tsx:197-214` |
| 工单卡组件先例 | `TicketCard`（标题 + 状态 pill + 处理人 + 摘要，可点开完整工单）即「对话里的卡片」现成实现 | `CollabAgentThread.tsx:48-96` |
| 看板卡片先例 | `BoardTicketCard` / `BoardEntryCard`（动作卡片：头像 + 徽章 + 技术元数据沉底） | `frontend/src/components/TicketBoardKit.tsx:47-81,111-140` |
| 进度图先例 | `CollabProgressBoard` / `CollabFlowMap`（岗位状态节点 + 回流虚线） | `frontend/src/components/CollabProgressBoard.tsx:38-85` |
| Markdown 渲染器扩展 | `markdownComponents` 映射可增自定义标签渲染（现有 `pre/code/a/img` 四项） | `MarkdownView.tsx:25-55` |
| 后端存储通道 | `messages.parts` 是无 schema 的 JSON 列（`schemas.py:547`），新增结构化片段无需迁移；但**前端没有未知 kind 的渲染分支**（见 §3 风险） | 同左 |
| 消息元数据 | `MessageView` 的 `meta` 仅存在于线程条目层；`RenderMessage` 可经 `ConversationThreadItem.message` 携带，但聊天页映射 `:890-901` 未透传额外字段 | `MessageView.tsx:9-31` |

## 9. 初步风险点（只列事实依据，不做方案结论）

1. **无卡片渲染通道**：`MessagePart.kind` 前端仅四种（`types.ts:743`）；未知 kind 会落进「过程」折叠块被画成工具卡（`MessageView.tsx:300-321` 兜底 `:320`）——新增卡片必须同时改前端渲染与（若走 SSE）事件归约 `lib/agentStream.ts:37-91`。
2. **两端双份渲染路径**：移动端聊天页不走 `ConversationThread`，直接渲染 `MessageView`（`MobileChatPage.tsx:1034-1085`）；卡片 UI 需要两处接入或复用线程组件。
3. **对话域重启无收尾**：残留 `streaming` 占位消息重启后不会像协作域那样被标 `interrupted`（对比 `collab/store.py:221-244`）；列表/未读有兜底（`runtime/conversations.py:23-33,94-130`），但聊天页里该气泡会以空正文展示（`chatMessages` 过滤逻辑只对「有 active_run」时隐藏占位，`ChatPage.tsx:878-889`）。
4. **详情刷新不比较 parts**：`sameMessageList` 只比 `id/content/status/error`（`lib/useAutoRefresh.ts:58-69`）；若卡片状态藏在 `parts` 里而其余字段不变，3 秒轮询不会触发重渲染。
5. **折叠状态持久化是本机偏好**：现有先例都是 localStorage（§4），跨设备不同步（对话详情本身有 3 秒轮询对齐，`ChatPage.tsx:330`）。
6. **规范约束**：事件协议改动必须四处同步（`agents/events.py`、`runtime/service.consume_run`、`lib/types.ts` + `lib/agentStream.ts`、`docs/architecture.md` 事件表）——`AGENTS.md` §1 铁律 5；数据库加列必须登记 `ADDITIVE_COLUMNS`（铁律 9、`models.py:28-90`）。

## 10. 未找到项（检索过但不存在）

| 检索目标 | 检索方式 | 结果 |
| --- | --- | --- |
| sessionStorage / IndexedDB 使用 | grep 全 `frontend/src` | 无 |
| 平台侧 WebSocket 服务端接口 | grep `websocket` 于 `backend/app/api/`、`transport/` | 无（仅 IM 平台客户端长连接，`backend/app/im/platforms/*`） |
| 对话域启动时的 streaming 消息收尾逻辑 | grep `interrupted` / `Message.status` 于 `runtime/`、`bootstrap.py` | 无（仅协作域有，见 §7） |
| 消息级自定义卡片渲染分支 | 读 `MessageView.tsx` 全文 + grep `custom` | 单聊无（仅线程条目 `custom` 槽在协作线程使用） |
| 任务进度卡片相关既有实现 | grep 「进度卡」/ `progress card` 于 `frontend/src` | 无（`CollabProgressBoard` 是协作流程图，非消息卡片） |
