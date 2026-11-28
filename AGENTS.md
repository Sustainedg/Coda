# AGENTS.md · Coda 工程约定

本文件对**所有在本仓库工作的 AI 编码代理**具有约束力。只要在这个仓库里改代码，就必须遵守。
动手之前请先读完本文件；需要背景与理由，再读 `docs/架构/ai-coding-standard.md` 与 `docs/架构/architecture.md`。

本文件只收**硬约束与扩展路径**。规则的理由、历史决策与排错记录放在 `docs/` 详版文档里
（索引见 `docs/README.md`），各节给出指针；详版与代码冲突时，先改一边并在回复里说明。

优先级：**用户当次指令 > 本文件 > 既有代码风格**。但若本文件与代码现状冲突，先按本文件执行，
并在最终回复里说明你改动了什么、为什么。

---

## 0. 事实基线（不要凭想象）

| 项 | 事实 |
| --- | --- |
| 产品 | Coda —— 多智能体对话控制台，管理 Claude Code / Codex / Antigravity CLI 等智能体，为它们配置网关、模型与人格 |
| 后端 | Python 3.11 · FastAPI 0.115 · SQLModel + SQLite(aiosqlite) · Pydantic v2 · httpx（依赖见 `backend/requirements.txt`） |
| 前端 | React 18 + TypeScript + Vite 6 · react-router · **无 UI 框架，手写 CSS**（`frontend/src/index.css`） |
| 后端入口 | `backend/app/main.py`（`uvicorn app.main:app --app-dir backend`） |
| API 前缀 | `/api`，路由聚合在 `backend/app/api/__init__.py` |
| **唯一扩展点** | `backend/app/agents/builtin/`（新增一个模块 + `@register_agent` 即接入新智能体） |
| 端口/部署 | 监听 `0.0.0.0:8010`（`scripts/serve.sh`） |
| 移动端 | **物理独立的第二条链接 `/m/`**：入口 `frontend/m/index.html` + `frontend/src/mobile/`，PWA 资产在 `frontend/public/m/`（manifest + Service Worker） |
| 测试 | `bash scripts/test.sh` → pytest（`backend/tests/`：适配器 · API · 架构守卫）+ 前端 `tsc --noEmit` |
| 版本源 | `backend/app/__init__.py` 的 `__version__`（前端从 `/api/system/overview` 读取展示） |
| 真实数据 | `backend/data/`（SQLite + 工作目录 + 内核隔离配置目录）**含用户真实对话**，受 `.gitignore` 忽略 |
| 多用户 | `users` 表 + 资产表 `owner_id`；`CODA_AUTH_ENABLED` 默认开启，按 `owner_id` 隔离，口令只存 PBKDF2 哈希；长效程序化凭据是 PAT（`pat_` 前缀，库里只存 sha256） |
| 技能 | 各内核自带技能（`SKILL.md` 目录）由适配器只读扫描，统一编目进 `skills` 表，**只能通过智能体分配生效**（`agent_skills`） |
| MCP | MCP 服务器（stdio/http/sse）统一编目进 `mcp_servers` 表：平台可新建「通用」服务器，各内核配置里的只读收录；**只能通过智能体分配生效**（`agent_mcp_servers`）。平台内置服务器（`builtin_key` 非空，声明在 `app/builtin_mcp.py`：图片生成 / 系统管理 / 任务进度卡）启动时逐用户播种、连接方式由平台维护（详见 `docs/架构/architecture.md` §6） |
| 协作域 | `backend/app/collab/` —— 多智能体「工单」协作（可视化编排 + 波次并行引擎，默认串行），独立于对话域，见 `docs/计划任务/collaboration-design.md` |
| IM 域 | `backend/app/im/` —— 把智能体连到聊天软件（Telegram / 飞书 / 钉钉 / 企业微信 / QQ / Discord / Slack）；入口在每个智能体设定页的「IM」标签，见 `docs/功能文档/im.md` |

---

## 1. 铁律（MUST / MUST NOT）

1. **新增智能体只能走适配器。** 在 `agents/builtin/` 新增模块并 `@register_agent`。
   禁止在 `runtime/`、`api/`、前端里为某个具体 `type_id` 写分支。
2. **适配器是叶子节点。** 适配器**禁止** `import` 任何 `app.api.*`、`app.runtime.*`、`app.db.*`、`app.models`。
   它只能依赖 `agents/base.py`、`agents/events.py`、`agents/skills.py`、`agents/mcp.py`、
   `agents/metering.py`、`agents/builtin/util.py`、标准库与 `httpx`。
3. **适配器无状态、不碰数据库。** 一次运行所需的一切都从 `AgentContext` 取；解析过程的状态放进 `AgentRun.state`。
4. **所有可配置项必须声明为 `ConfigField`。** 禁止在 React 里为某个智能体硬编码表单字段。
5. **事件协议是框架与适配器之间唯一的方言。** 新增事件类型必须**同时**改：
   `agents/events.py`（枚举 + 构造器）、`runtime/service.py:consume_run`（累积/持久化）、
   前端 `lib/types.ts` + `lib/agentStream.ts:applyAgentEvent`、`docs/架构/architecture.md` 的事件表。
6. **凭据只经过 `app/security.py`。** 明文密钥禁止入库、进日志、进 SSE payload、进接口响应；
   命令行回显必须经 `runtime/runner.py:_redact` 脱敏。
7. **界面文案、代码注释、docstring 一律用中文。** 专有名词（Claude Code、MCP、SSE、JSONL、Fernet…）
   与代码标识符保持原样。术语表见 §6。
8. **改完必须验证。** 后端 ≥ `pytest`；前端改动还要 `tsc --noEmit` + `npm run build`；
   两者都用 `bash scripts/test.sh` 一次跑完。**前端改完不 build 不重启，线上看不到效果。**
9. **数据库变更必须向后兼容。** 本地库含真实数据，禁止"删库重建"式变更；加字段要能容忍旧行。
10. **长任务必须可取消、可超时。** 子进程/HTTP 调用都要响应 `run.cancel_event` 与
    `runner.resolve_timeout_limit(run)`；普通对话默认不限时（全局 `run_timeout_seconds` 为空），
    协作域的单步限时走自己的 `budget.step_timeout_seconds`，两者不要互相顶替。
11. **错误必须显式暴露**：要么抛 `HTTPException`（API 层），要么产出 `error` 事件（运行层）。
    禁止 `except Exception: pass` 静默吞掉。
12. **不要新增依赖**（后端 `requirements*.txt`、前端 `package.json`），除非任务确实需要；
    新增必须说明理由并与现有版本风格一致（精确版本号）。
13. **智能体能用的技能只能来自分配。** 技能的权威来源是 `agent_skills` 表；运行时必须「严格仅分配」——
    把分配到的技能物化进该内核的隔离配置目录，不允许把本机个人/插件技能顺手带进一次运行。
    某个内核把技能放在哪、怎么隔离，是**适配器**的知识（`discover_skills()` / `skill_mount()`）；
    框架层禁止硬编码技能目录或内核配置变量。

---

## 2. 功能分层与依赖方向

```
 ┌ 前端 frontend/src ────────────────────────────────────────────────┐
 │  pages/ (Chat·Collab·Agents·Skills·MCP·Settings) ·                 │
 │  mobile/ (独立入口：/m/ 的页面与样式表，PWA) ·                      │
 │  components/ (DynamicForm·MessageView·Modal·CollabCanvas…) ·       │
 │  lib/ (api·types·agentStream·collabStream)                         │
 │  —— 只认「规范化事件」与 REST，不知道某个智能体长什么样            │
 └──────────────────────────────┬────────────────────────────────────┘
                        REST /api/*   ·   SSE POST /messages · GET /stream
 ┌────────────────────────────────▼──────────────────────────────────┐
 │ api/            路由、请求校验(Pydantic)、SSE 封装、HTTP 状态码    │
 ├───────────────────────────────────────────────────────────────────┤
 │ collab/         协作域（旁路）：编排图 · 工单协议 · 波次并行引擎 · 运行 API │
 │ im/             IM 域（旁路）：连接登记 · 配对 · 平台适配器 · 运行桥   │
 ├───────────────────────────────────────────────────────────────────┤
 │ runtime/        service=编排+持久化 · runner=执行(CLI/API) ·       │
 │                 manager=运行表(注册/取消/追踪) · skills=技能编目/分配/物化 │
 │                 mcp=MCP 编目/分配/物化                              │
 │                 provisioning=运行时(CLI 内核)的安装/更新/卸载执行   │
 ├───────────────────────────────────────────────────────────────────┤
 │ agents/         ★ 适配器插件层：base(契约) · events(事件) ·         │
 │                 registry(注册) · skills(技能契约) · mcp(MCP 契约)  │
 │                 metering(用量归一化) · builtin/*(实现)             │
 ├───────────────────────────────────────────────────────────────────┤
 │ models/db · schemas · settings · security · auth/auth_deps · attachments · builtin_mcp · bootstrap│
 └──────────────────────────────────────────────────────────────────────────┘
```

依赖方向**只能自上而下**：`api → runtime → agents`；`api/runtime` 可依赖
`models/db/settings/security/auth/auth_deps/attachments`；**`agents` 不得反向依赖任何上层**。
跨层共享的数据结构定义在叶子层（如 `agents/base.py` 的 `AgentContext`）。
`auth.py`（口令哈希 / 令牌）与 `auth_deps.py`（当前用户 / 管理员 / 归属校验）放在顶层叶子层，
因为协作域有架构守卫禁止依赖 `app.api.*`，但协作路由同样需要鉴权。

两个**旁路域**（都不在主干上，都有架构守卫测试）：

- **协作域 `collab/`**：`api` 只挂子路由；它对 `runtime` 的**唯一边界**是 `collab/execution.py`；
  只允许依赖 `agents.events`、`models/db/settings` 与叶子工具 `app.credentials`、`app.attachments`
  （均与对话域共用同一份），不得 `import app.api.*`，也不得绕过 `execution.py` 直接 import
  `app.runtime.*`；`tickets.py`/`graph.py`/`prompt.py` 必须是不碰存储与 HTTP 的纯函数；
  协作事件是自有 `collab.*` 信封（`collab/protocol.py`），**不属于**铁律 5 的适配器事件协议；
  框架层禁止反向依赖 `collab`。
- **IM 域 `im/`**：与对话域的**唯一边界**是 `im/bridge.py`（调 `runtime.service.prepare_run` /
  `start_run`）；平台适配器（`im/platforms/*`）是**叶子节点**：只能依赖 `im/base.py`、`im/format.py`、
  `im/crypto.py`（企业微信媒体解密的 AES，`cryptography` 只在这里出现）、标准库、`httpx` 与
  `websockets`，不得 import `app.api.*` / `app.runtime.*` / `app.db` / `app.models`；
  「某个平台怎么收发消息」是适配器的知识，框架层（`im/service.py`）不得为具体平台开分支。

各层职责（改代码前先确认你改的是哪一层）：

| 层 | 负责 | 不负责 |
| --- | --- | --- |
| `agents/builtin/*` | 怎么调用某个智能体、怎么把它的输出翻译成事件 | 存储、HTTP、UI、取消（框架统一做） |
| `agents/base.py` `events.py` `registry.py` `skills.py` `mcp.py` `metering.py` | 契约 / 协议 / 用量归一化（改动影响面最大，慎改） | 具体产品逻辑 |
| `runtime/runner.py` | 起子进程/发 HTTP、读流、超时、取消、脱敏 | 业务规则 |
| `runtime/service.py` | 载入 对话→智能体→适配器（网关提供密钥与地址）、能力决策、历史回放、落库 | 具体命令拼装 |
| `runtime/skills.py` | 技能统一编目、智能体分配、物化进内核隔离目录 | 哪个内核把技能放哪（那是适配器的事） |
| `runtime/mcp.py` | MCP 服务器统一编目、新建/编辑/删除、智能体分配、写进内核隔离目录 | 哪个内核把 MCP 配置放哪、长什么形状（那是适配器的事） |
| `api/*` | 参数校验、状态码、把 service 的结果转成响应/SSE | 业务决策（尽量薄） |
| `models.py` `schemas.py` | 表结构与请求/响应契约 | 编排逻辑 |
| 前端 | 展示与交互 | 任何智能体特异逻辑 |

**违规示例**（出现即为评审不通过）：
```python
# ✗ runtime/service.py 里为具体智能体开小灶
if agent.type_id == "codex":
    ctx.prompt = special_codex_wrapper(ctx.prompt)

# ✗ 适配器直接读数据库
from ..models import AgentProfile   # in agents/builtin/foo.py

# ✗ 前端硬编码某适配器的字段
{typeId === 'antigravity-cli' && <input name="approval_mode" />}
```

---

## 3. 核心契约（改动这些等于改公共 API）

### 3.1 规范化事件协议（`agents/events.py`）
`run.started · message.delta · thinking.delta · tool.call · tool.result · usage · context.usage · context.window · context.compacted · session · log · error · run.finished`
外加传输层自有的 `turn.started`（`runtime/service.start_run()` 在启动后台运行前同步发出，携带 run_id 与消息 id）
与 `stream.closed`（`transport/sse.py` 收尾）。
**适配器只能产出前 12 种**（`run.started` / `run.finished` 由运行层产出）；`turn.started`/`stream.closed` 由框架产出，适配器不得越权。
新增事件类型必须走铁律 5 的四步同步改动（**事件表有两份**：`docs/架构/architecture.md` 与
`docs/架构/ai-coding-standard.md` §4.1，两处都要改）。
框架自有的旁路信封**不属于**本协议：协作域是 `collab.*`（`collab/protocol.py`），
「内核管理」页（设置 → 内核管理）的安装 / 更新 / 卸载是 `provision.*`（`runtime/provisioning.py`）——
两者只在各自的页面内部消费，由框架层产出，不参与铁律 5 的四处同步。

### 3.2 `AgentDefinition` 契约（新增适配器要实现/声明）
必需：`id`、`name`、`runtime`、`build_invocation()`、`parse_line()`（CLI）或 `stream()`（API）。
常用可选：`description`、`vendor`、`capabilities`、`config_fields`、`api_key_env`、`base_url_env`、
`default_gateway`、`default_model`、`protocol`（openai / anthropic / gemini）、`default_auth_style`、
`default_models`（官方链路预置模型）、`docs_url`、`logo`（官方 Logo 的前端静态资源路径，如
`/icons/kernels/claude.webp`）、`logo_style`（`mask` = 单色 SVG 遮罩染色；`image` = 真彩色位图
按原色显示）、`executable`、`auto_provision`、`legacy_ids`（取代过的旧 id，启动时据此迁移存量智能体）、
`availability()`、`probe()`/`probe_api()`、`auth_status()`（账号型内核的登录态探测，可选）、
`resume_supported(ctx)`（可选钩子）。
- `parse_line(line, run)` 里**不要吞异常**：解析不了就 `yield log(...)` 或忽略该行，别让整个运行崩掉。
- 需要"运行结束时补发事件"的适配器，请覆盖 **`AgentRun.finish()`**（基类钩子）。
  ⚠️ 注意：不要自创 `finalize()` 之类的方法名，运行层的唯一结尾钩子是 `AgentRun.finish()`。

### 3.3 `Capabilities` 驱动框架行为（声明即生效，别在别处打补丁）
`streaming`、`sessions`（服务端可续接→只发本轮 prompt，否则回放 transcript）、
`native_system_prompt`（False→框架把人格折进 prompt）、`tools`、`mcp`、`permission_modes`、
`working_directory`（True→为每个对话分配独立工作目录作 cwd）。
声明与实际行为不符 = bug。

### 3.4 `ConfigField` 契约
`key · label · FieldType · required · default · placeholder · help · options · advanced`。
`FieldType ∈ {string, secret, number, boolean, select, textarea, keyvalue, string_list}`。
用户输入的 options 由 `coerce_options()` 规整（含未知键透传，保持前向兼容）。
**新增一个可配置项 = 在适配器加一个 `ConfigField`**，前端表单自动出现，无需动前端。

### 3.5 `AgentContext` / `AgentRun`
`AgentContext` 是框架→适配器的唯一输入（`agent_id/name`、`options`、`gateway_url`、`api_key`、
`credentials`、`auth_style`、`model`、`system_prompt`、`workspace_dir`、`session_id`、`history`、
`prompt`、`skills`、`mcp_servers`、`home_dir`、`task_card_mcp`），用 `ctx.option(key, default)` 取值。
`task_card_mcp` 是框架给适配器的只读标记：本轮是否挂了平台内部任务进度卡 server，适配器据此
只预允许 `mcp__coda-task-card__update_task_card` 这一个工具（不要放开整个 server）。
`gateway_url` / `api_key` / `auth_style` 由框架从智能体引用的**网关**解密后注入（`runtime/gateway.py:bind_gateway`）；
`credentials` 是智能体自己的凭据（解密后的「环境变量名 → 明文」），由框架合并进子进程环境变量，
适配器不需要自己处理，也**不得**把它写进提示词、日志或事件。
`AgentRun` 是"无状态定义 + 一次具体运行"的绑定，持有 `state`（解析状态）、`emitted_text`、`thinking`、
`session_id`、`tool_calls`。适配器可把内核自带任务工具（如 CodeBuddy 的 `TaskCreate/TaskUpdate/TaskList`）
归一成平台卡片块写进 `AgentRun.task_card_block`；运行层在收尾时作为围栏 / MCP 之后的兜底——
平台 `task_cards` 仍是唯一展示源，适配器只产出数据、不持久化。
- **双向 stdin（常驻型 JSON-RPC 内核，如 Codex app-server）**：适配器在 `build_invocation()` 里置
  `Invocation.stdin_hold=True`（`stdin` 只写第一帧且不关闭），解析到应答后用 `AgentRun.send(line)`
  排队下一帧、用 `AgentRun.mark_done()` 声明收工。**写 stdin / 关 stdin 由执行层统一做**
  （照旧由 `runner` 负责），适配器仍然只生产数据、不碰子进程。

### 3.6 领域模型不变量（`models.py`）
- `Gateway`（网关，API 密钥的唯一来源）→ `AgentProfile`（智能体：内核 + 网关 + 模型 + 人格 + 技能）
  → `Conversation`（对话绑定一个智能体）。原「角色」与「智能体实例」已合并为 `AgentProfile`。
- **Conversation 快照 `agent_profile_id`**：智能体改了内核 / 网关 / 模型后，历史对话仍按原智能体复现，
  别改成实时跟随。
- 外键 `ON DELETE RESTRICT`：**不要**为了"方便"改成级联删除。删智能体 / 删自定义网关走**软删除**
  （写 `deleted_at`，新列登记在 `models.py` 的 `ADDITIVE_COLUMNS`）：实体从所有可选列表消失，
  但历史对话与协作历史照旧可读（历史对话仍能显示当时的名字/头像，也仍可继续）。
  只有仍被**未删除**的智能体引用的网关才返回 **409**；官方链路（`kind="official"`）一律不可删。
- 删除对话需显式清理消息（`api/conversations.py` 已处理）；SQLite 已开 `PRAGMA foreign_keys=ON`。

### 3.7 网关与密钥（`gateways` 表 + `api/gateways.py`）
网关是 API 密钥的**唯一来源**，分两类：`kind="official"`（启动播种按适配器声明生成、不可删）与
`kind="custom"`（用户在「设置 → 自定义网关」创建）。智能体只引用一个 `gateway_id`，模型必须从网关的
`selected_models` 白名单里选。
- **密钥只经 `app/security.py`**：明文密钥禁止入库 / 进日志 / 进 SSE payload / 进接口响应；
  接口只回 `api_key_set` 与掩码 `api_key_preview`。
- **「必须成功才能保存」是服务端不变量**：`selected_models` 至少一项；`POST /api/gateways/probe`
  只探测不落库，探测失败可由用户「手动旁路」录入模型名。
- 协议与鉴权方式固定为 `PROTOCOLS` / `AUTH_STYLES`（`runtime/gateway_probe.py`），新增要在那里声明。
- **界面只维护自定义网关**：设置页只列 `kind="custom"`；智能体配置里的「官方默认」是
  `gateway_id = null`（平台不接管路由，回落到适配器读到的本机配置），官方链路不作为可选项。

### 3.8 技能契约（`agents/skills.py`）
- `SkillSpec`：一个技能的只读描述（`type_id` / `slug` / `name` / `description` / `source_kind` / `path` /
  `fingerprint` / `files` / `owner_id` …）。共享技能（内核自带 / 宿主机）的 `id` 是
  `<type_id>:<slug>`；用户技能库里的技能是 `<type_id>:lib:<owner_id 或 local>:<slug>`，
  同名技能在不同用户下互不冲突。
- **用户技能库按用户隔离、按 slug 只存一份**：导入、市场安装、挂载到其它内核的技能都落在平台数据目录
  `<数据目录>/skill-libraries/<owner_id>/<slug>`（**不再按内核分子目录**）；一份技能被哪些内核引用
  登记在同级的 `<owner_id>/.mounts.json`，编目时按引用展开成「一个内核一行」，所有行共享同一个
  `path`——市场更新（重新安装）覆盖唯一实体后，挂载到它的全部内核同时生效。
  「复制到内核」实际只是登记引用，不再复制文件。**禁止**把用户技能写进内核的全局技能目录
  （那会让所有人共享）。
- `SkillMount`：适配器声明的**物化计划**（`home_name`、`skills_subdir`、`carry` 要携带的配置文件、
  `patch` 配置文件补丁、`notes` 用户提示）。框架只按这份声明执行，不认识具体内核。
- 适配器钩子：`skill_roots()`（声明本机原生技能根：路径 + 来源 + 是否可安装；用户技能库由框架自己
  托管，不写进这些根；会重名的一类技能按来源逐个声明根并填 `namespace`，否则同名技能会在编目与
  物化时互相覆盖）、`discover_skills()`（只读扫描，尽力而为、不得抛错；常见实现是
  `scan_skill_roots(self.skill_roots(), ...)`）、`skill_mount(ctx)`（返回 `None` 表示该内核不支持技能）
  与 `skill_invocation(tool_name, tool_input)`（把一次工具调用识别成技能调用时返回技能 slug，供
  「用量」页统计；默认不识别——「技能调用长什么样」是适配器的知识）。
- `AgentContext.skills` / `AgentContext.home_dir`：框架注入的「本次运行生效的技能」与
  「物化好的隔离目录」（技能与 MCP 共用同一个「家」），适配器在 `build_invocation()` 里据此
  设置自己的配置目录环境变量。
- 声明了 `Capabilities.skills=True` 的适配器**必须**实现 `skill_mount()`（有架构守卫测试）。
- **内核自带技能要按归属清单认，不能按目录猜**：把「自带」与「用户」混放的内核（如 Hermes 的
  `skills/.bundled_manifest`）**必须**给出等价的归属清单，再在 `discover_skills()` 里改写
  `source_kind`；不允许只靠目录路径推断。
- 编目 / 重扫删除语义 / 插件技能「只认已安装」/ frontmatter `name` 同步 / 市场安装卸载的完整机制
  见 `docs/功能文档/skills-mechanism.md`；红线同样列在 §10 反模式清单。

### 3.9 MCP 契约（`agents/mcp.py`）
- `McpServerSpec`：一个 MCP 服务器的只读描述（`name` / `slug` / `transport`(stdio|http|sse) /
  `command` / `args` / `url` / `env` / `headers` / `type_id` / `source_kind`），`source_id` 为
  `<内核 id>:<slug>`（平台通用的服务器没有）。`env` / `headers` 的值可能含密钥：只能加密落库、
  接口只回掩码，**禁止**写进提示词、日志或事件。
- `McpMount`：适配器声明的**物化计划**（`home_name`、写进隔离目录的 `files`、写进工作目录的
  `workspace_files`、`notes`）。`home_name` 必须与同一适配器 `skill_mount()` 声明的一致：
  技能与 MCP 共用同一个隔离目录。
- 适配器钩子：`discover_mcp_servers()`（只读扫描本机已有的 MCP 配置，尽力而为、不得抛错）与
  `mcp_mount(ctx)`（返回 `None` 表示该内核不支持 MCP）。解析 / 渲染的共享工具（JSON / TOML 两种
  形状、`safe_slug`）在 `agents/mcp.py`；**「哪个内核把配置放哪、长什么形状」是适配器的知识**，
  框架不硬编码路径、键名或命令行开关（有架构守卫测试）。
- `AgentContext.mcp_servers` / `AgentContext.home_dir`：框架解密后注入的「本次运行生效的服务器」与
  「物化好的隔离目录」。密钥只能写进配置物化的产物（`files` / `workspace_files`）。
- 声明了 `Capabilities.mcp=True` 的适配器**必须**实现 `mcp_mount()`（有架构守卫测试）。
- **平台内置服务器**（`app/builtin_mcp.py` 的 `BUILTIN_SERVERS`）随平台分发：启动时为每个启用用户
  幂等播种一条（`builtin_key` 标记），连接方式由平台维护、接口拒绝修改、不能删除（不需要时停用）。
  用户只需填写业务参数；平台托管参数用 `BuiltinParam(managed=True, managed_source=…)` 声明来源
  （`public_base_url` / `platform_token`），`runtime/mcp.py` 只按声明执行，**不得**在框架层判断
  具体 `builtin_key` 或硬编码参数名。内置项按用户各一份：列表、详情与分配都只能看到 / 使用自己的
  那份（管理员也不跨用户查看）。
- 三类来源、编辑语义、任务进度卡内置项的完整机制见 `docs/架构/architecture.md` §6。

## 4. 扩展手册（照着做，别自创路径）

### 4.1 接入新的 CLI 智能体（例：Aider）
1. 新建 `backend/app/agents/builtin/aider.py`，`@register_agent` 装饰 `AiderAgent(AgentDefinition)`。
2. 声明 `id/name/vendor/logo/runtime=Runtime.CLI/executable/capabilities/config_fields` 与凭据/网关环境变量；
   `logo` 指向 `frontend/public/icons/kernels/` 下的静态资源：默认 `logo_style="mask"` 用 24×24
   单色 SVG（只画形状，染色由界面遮罩完成）；有真彩色品牌图的用 `logo_style="image"` +
   PNG/WebP（透明底、≤512×512，随仓库发布，不运行时外链）。
3. 实现 `build_invocation(ctx) -> Invocation`（`argv/env/cwd/stdin/files`）与 `parse_line(line, run)`。
4. 用 `builtin/util.py` 的 `as_list/truthy/flatten_content/dig`，不要重复造轮子。
5. 加测试：解析用真实样本；在 `backend/tests/` 参照 `test_adapters.py` 的写法。

### 4.2 接入新的模型供应商（无官方 CLI）
复用 `agents/builtin/openai_harness.py` 的 `OpenAIHarnessBase`，只写一个子类声明默认网关/模型/`config_fields`/`logo`，
参考同文件的 `OpenAICompatibleHarness`（`openai-harness`）。**不要再写一遍工具循环。**

### 4.3 零代码接入私有 CLI
用内置 `custom-cli` 适配器：填 `command/args/prompt_mode/output_format/..._path` 等 `ConfigField` 即可。
只有在"声明式表达不了"时才考虑写适配器。

### 4.4 新增可配置项
在对应适配器的 `config_fields` 加 `ConfigField(...)` → 前端自动渲染（`DynamicForm`）。**不要改前端。**

### 4.5 新增规范化事件
按铁律 5，四处 + 文档同步；同时更新 `docs/架构/architecture.md` 的事件表。

### 4.6 让某个适配器支持技能
1. `capabilities` 里声明 `skills=True`。
2. 实现 `skill_roots()` 声明本内核的技能根（`SkillRoot(path, source_kind, installable, max_depth, namespace)`；
   平台「技能市场」的安装目录是其中 `installable` 的一个，通常把 `.skillhub` 单独列一条、
   `source_kind=MARKET_SOURCE`），再用 `agents/skills.py` 的 `scan_skill_roots()` 实现 `discover_skills()`，
   标注来源（`user` / `project` / `plugin` / `system` / `market`），**只读、不得抛错**。
3. 实现 `skill_mount(ctx)`：声明隔离目录名、需要携带的配置文件、要关闭的内核自带技能，
   并在 `build_invocation()` 里把 `ctx.home_dir` 写进对应环境变量（如 `CLAUDE_CONFIG_DIR`）。
   隔离 HOME 时注意内核「跟着 HOME 走的数据」不止技能根：登录态、设备身份等必须留在真实
   目录的数据要一并软链进隔离 HOME（参考 `doubao_cli.py:_link_macos_data`），漏链会让内核在
   隔离目录重建设备身份、对话起不来。
4. 加测试：参考 `backend/tests/test_skills.py`（解析、物化、隔离变量）。

### 4.7 新增前端页面
- **桌面端**：`src/pages/XxxPage.tsx`，在 `src/App.tsx` 的 `Routes` 与 `NAV` 注册；样式写进
  `src/index.css`，沿用既有 class 命名（`card`/`btn`/`pill`/`banner`…）。
- **移动端（`/m/`）**：`src/mobile/pages/MobileXxxPage.tsx`，在 `src/mobile/MobileApp.tsx` 注册路由；
  样式写进 `src/mobile/mobile.css`。移动端页面**不得**引用 `src/pages/`、`src/App.tsx` 或
  `src/index.css`（有守卫测试 `tests/test_mobile_web.py`），但可以复用 `src/lib/*` 与纯展示组件
  （共享白名单见 `docs/架构/ai-coding-standard.md` §8，以守卫测试为准；对话气泡只有 `MessageView`
  一份、线程装配只有 `ConversationThread` 一份）。共享组件的样式段两端各写一份、共享段逐字一致
  （同样有守卫测试），改样式记得两边同步语义。
- **两端功能对齐、交互各表**：桌面端有的能力移动端也要能用，但按触屏重做交互——协作组改用
  表单式编辑（与桌面端共用 `lib/collabDraft.ts` 的字段语义），不引用桌面端的编排画布。
- **两端通用**：类型加在 `src/lib/types.ts`，接口调用加在 `src/lib/api.ts`；禁止在组件里直接 `fetch`。
- **移动端必须保持 PWA 可用**：新增页面不得依赖 `/m/` 之外的顶层路由；离线只会命中
  Service Worker 预缓存的外壳。**改了外壳（入口 HTML / 样式表 / 构建产物）必须把
  `public/m/sw.js` 的缓存名版本 +1**，否则已添加到主屏的独立应用会继续吃旧缓存
  （设置页的「更新到最新版本」走 `pwa.ts:refreshShell()`，是自助出口）。
- **移动端锁定缩放**：入口视口写 `maximum-scale=1.0, user-scalable=no`，并在 `mobile.css` 用
  `touch-action: pan-x pan-y` 兜底；移动端输入框字号一律 ≥ 16px（iOS 聚焦小字号会强制放大页面）。
  **桌面端不做此限制**（浏览器缩放是它的无障碍能力）。
- **界面既定决策不重开**：对话页顶栏只放「导出 + 朗读」两个按钮（不要再塞「⋯」）；输入区只有
  `components/Composer.tsx` 一份（`queueWhenBusy` 开排队发送，别复制输入框）；外观（深色 / 浅色 /
  跟随系统）只有 `src/lib/theme.ts` 一份实现（新配色先加 CSS 变量、再补浅色值，不写死深色）；
  「要跟文字同色的操作图标」一律用 `components/Icons.tsx` 的线条 SVG，不再用 emoji；附件能力只有
  `backend/app/attachments.py` 一份。移动端外壳视口 / 安全区 / 键盘的处理与排错记录见
  `docs/架构/architecture.md` §10——**守卫测试是权威**：守卫变红先怀疑自己在违反约定，
  而不是去改测试。

### 4.8 扩展协作域
协作的一切都留在 `backend/app/collab/`：新增动作/状态改 `schemas.py` + `engine.py`（并同步
`docs/计划任务/collaboration-design.md`）；纯逻辑放 `tickets.py` / `graph.py` / `prompt.py`（有"必须是纯函数"的
守卫）；对 `runtime`/`agents` 的调用只能加在 `execution.py`。给协作表加列要同时登记
`ADDITIVE_COLUMNS`（SQLite 的 `create_all` 不会给旧表补列）。**不要**为了让协作跑通去改适配器或 `runtime/`。
- **运行快照**：`collab_runs.graph` 在第一次推进（下发第一张工单）时冻结整张图，引擎只读它。改协作组只影响还没开跑 / 之后新建的运行；
  不要再用「有运行记录就禁止改图」的方式保护老任务（老运行靠自己的快照，升级前的由
  `store.backfill_run_graphs()` / 保存前补拍兜底）。
- 协作聊天也能带附件：落盘用共享的 `app.attachments`，接口是
  `POST /api/collab/runs/{id}/attachments`，`chat` 只收相对路径 —— 附件是工作目录里的普通文件，
  不是协作域新表（也别往对话域塞）。

### 4.9 接入一个新的 IM 平台
1. 在 `backend/app/im/base.py` 的 `PLATFORMS` 加一条 `Platform(...)` 元数据（id / 名字 / 字段 /
   是否支持群与按钮 / 设置步骤）—— 设置页的添加表单由它生成，**不要改前端**。
2. 在 `backend/app/im/platforms/` 新建模块，实现 `check(fields)`（先核对一次凭据）与
   `connect(fields, handlers, runtime)`（返回一个 `ConnectionBase` 子类：实现 `send`，
   需要时覆盖 `typing` / `open`），并在 `platforms/__init__.py` 的 `ADAPTERS` 里注册。
3. 长连接 / 长轮询的游标这类小状态放进 `RuntimeState`（框架会节流落库到 `im_connections.runtime`），
   不要自己写文件。
4. 加测试：参考 `tests/test_im.py`（用假适配器验证框架行为）与 `tests/test_im_architecture.py`。
5. 平台只支持「客户端往外连」（WebSocket / 长轮询）：需要公网回调地址的接口不做。

### 4.10 让运行时可由平台安装 / 更新 / 卸载 / 登录
1. 在适配器声明 `provisioner = RuntimeProvisioner(manager=…, package=…, install_argv=…,
   update_argv=…, uninstall_argv=…, latest_argv=…, prepare_argv=…, login_argv=…)` —— 命令（用哪个包管理器、
   包名、装到哪、动作前要清掉什么残留）是**适配器的知识**，框架只负责按声明在受控子进程里执行、
   流式回显、超时与脱敏。`prepare_argv` 是动作前的清扫命令，失败只回一行告警、不中断动作。
   npm 系内核的安装 / 更新命令由 `builtin/util.py:npm_install_argv()` 生成（先让出被占用的同名
   可执行文件，失败时再放回去）。
2. 账号型内核（扫码 / OAuth，没有 API Key）额外声明 `login_argv`（如 `doubao login --qr=png`）与
   `AgentDefinition.auth_status()`。登录命令按通用约定输出 `QR_IMAGE=<图片路径>`，框架读成
   `data:` URL 经 `provision.qr` 事件推给界面；登录任务的任务列表 / 重连接口**只对管理员开放**
   （输出里带的是短期凭据，见 §7）。
   只能交互式登录、没有可代跑子命令的内核（例如只有 TUI `/login` 的内核）**不要声明假的
   `login_argv`**：只实现 `auth_status()` 即可 —— `supports_auth()` 会让「内核管理」卡片显示
   登录状态，但不出现一个点了必然失败的「登录」按钮。
   内核若有可编程登录入口，写一个登录助手脚本由 `login_argv` 调起（参考 `builtin/codebuddy_login.py`）；
   登录必须把浏览器里的授权码贴回来时，助手再额外打印 `INPUT_PROMPT=<提示文本>`（框架产出
   `provision.input` 事件、界面弹输入框，用户提交的文本由 `POST /runtimes/{id}/login/input`
   写回登录子进程的 stdin，参考 `builtin/antigravity_login.py`）。
3. 多账号：内核若只维护「当前这一个」登录态，适配器可以把每次登录的凭据存档成平台侧槽位，
   在 `auth_status()` 的 `accounts` 里列出（`RuntimeAccount`），并覆盖
   `activate_account()` / `remove_account()`（参考 `builtin/codebuddy_accounts.py`）。切换只影响
   之后启动的对话，正在跑的不换号。
4. 不要把「怎么装」写进 `api/` 或 `runtime/`（框架层不得硬编码包管理器或内核 id）。
5. 不支持由平台代管的适配器（进程内 API 内核、用户自备的 `custom-cli`）保持 `provisioner = None`，
   卡片副标题会写明「进程内运行」/「自备可执行文件」。

### 4.11 让某个适配器支持 MCP
1. `capabilities` 里声明 `mcp=True`。
2. 实现 `discover_mcp_servers()`：只读扫描本机已有的 MCP 配置，用 `agents/mcp.py` 的
   `parse_json_servers()` / `parse_toml_servers()` 解析成 `McpServerSpec`，`type_id` 填 `self.id`；
   **不得抛错**（配置不存在 / 损坏 / 无权限都当没有）。
3. 实现 `mcp_mount(ctx)`：用 `to_json_config()` / `render_toml_servers()` 把 `ctx.mcp_servers`
   序列化成自己内核要的形状，落点二选一（可同时用）——
   `files`（写进隔离配置目录，配 `home_dir` 环境变量使用）或 `workspace_files`（写进工作目录的项目级
   配置）；`home_name` 必须与 `skill_mount()` 一致。要「只认分配到的服务器」就同时关闭内核的其它来源
   （例如给内核加一个「只读这份配置」的命令行开关）。
4. 加测试：参照 `backend/tests/test_mcp.py`（解析、物化、隔离变量、密钥只回掩码）。

---

## 5. 编码风格

**Python**
- 文件顶部 `from __future__ import annotations`；统一 `from __future__` 后接标准库、三方、本地分组。
- 类型标注齐全；数据容器优先 `@dataclass(slots=True)`（沿用 `base.py` 风格）。
- 面向用户的字符串中文；异常信息中文且可操作（告诉用户"哪里不对、怎么修"）。
- 不引入全局可变状态到适配器；并发安全只依赖 `AgentRun.state`。
- 缩进 4 空格，行宽 ~100；长签名换行对齐沿用现有写法。

**TypeScript / React**
- 函数组件 + hooks；单文件单默认/命名导出，沿用现有命名（`AgentsPage`、`DynamicForm`）。
- 类型集中放 `src/lib/types.ts`，与后端 `schemas.py` 的字段名保持一致（`snake_case`）。
- 网络请求一律走 `src/lib/api.ts`；**不要**在组件里直接 `fetch`。
- 复用 `DynamicForm` 渲染所有适配器表单；复用 `MessageView` 渲染消息。
- 不加状态管理库、不加 UI 库；CSS 手写在 `index.css`。

---

## 6. 语言与术语

- **面向用户的一切（界面文案、后端提示、错误信息、接口 `description`、文档）用中文。**
- **代码注释与 docstring 用中文。**
- 保留的专有名词：Claude Code、Codex、Antigravity CLI、MCP、SSE、JSONL、Fernet、token、WebSocket、
  以及适配器 `id`（`claude-code`…）、环境变量名、字段名等标识符。

| 统一用词 | 不要写成 |
| --- | --- |
| 智能体（agent） | Agent、agent（正文里） |
| 智能体类型 | Agent Type、adapters（指概念时） |
| 网关 | Gateway |
| API 密钥 | API Key、API key |
| 适配器 | adapter（指概念时） |
| 智能体 · 对话 · 消息 | Agent、Conversation、Message（指概念时） |
| 智能体配置（`AgentProfile`） | Agent Instance、Role |
| 工作目录 | workspace |

---

## 7. 凭据与安全

- 读写密钥只用 `security.encrypt/decrypt/mask`；接口只回传掩码，永不回传明文。
- **网关密钥**（模型提供方）只属于 `Gateway`；**凭据**（工具/服务要用的第三方密钥）是**全局一份**
  （`credentials` 表），某个智能体能拿到哪些由 `agent_credentials` 引用关系决定（同「技能」的思路）。
  两者都只经 `security` 加解密。凭据的纯逻辑在 `app/credentials.py`、读库 / 落库 / 引用关系在
  `runtime/credentials.py`：校验（`validate_name`）、解密（`decrypt_all`）、只读掩码视图（`reads`）、
  提示词里的名字清单（`notice`）、文本擦除（`scrub` / `scrub_event`）只此一份；整表替换引用关系走
  `runtime/credentials.py:set_agent_credentials`。
- 凭据**只以环境变量**注入子进程（`runner` 合并 `ctx.credentials`，网关/适配器给的同名变量优先），
  模型只知道变量名；运行层逐条事件擦除凭据值**与网关密钥**（`runner._run_secrets` → `scrub_event`），
  因此它们进不了 SSE、落库内容与日志。
- 新增会把凭据放进 env/argv 的适配器时，确认 `runner._redact` 能覆盖（env 键名含 KEY/TOKEN/SECRET/PASSWORD）。
- `Runtime.API` 型智能体的文件/命令工具必须限制在工作目录内（见 `builtin/harness_tools.py` 的 `_resolve`）。
- `bypassPermissions` / `danger-full-access` 这类无人值守选项只应出现在显式配置里，不要设为默认值。
- **多用户归属**：网关、智能体、凭据、对话、MCP 服务器、协作组与协作运行都有 `owner_id`。
  接口按 `current_user` 过滤：非管理员列表只回自己的行（网关额外包含官方链路、扫描来的 MCP
  是只读共享目录），按 id 读取别人的行返回 404；跨用户引用（网关 / 凭据 / 平台通用 MCP /
  协作图节点上的智能体）必须在写入或启动运行前拒绝。**对话内容（含消息 / 附件 / 轨迹 /
  上下文用量 / 文件）始终只对本人可见，管理员也不豁免**（用 `require_owned_strict`）；
  其余资产管理员可看全部，但凭据仍只能以环境变量注入、事件仍要脱敏。
- **鉴权开关**：`CODA_AUTH_ENABLED` 默认开启（多用户形态）；单用户开箱即用可显式设为
  `false`。空库首启配置 `CODA_ADMIN_USERNAME` / `CODA_ADMIN_PASSWORD`，不配置时
  第一个注册的账号成为管理员；`CODA_REGISTRATION_OPEN` 控制自助注册。
  口令只用 `auth.hash_password()` / `verify_password()`，不得存明文。
- **令牌吊销**：JWT 带 `ver`，与 `users.token_version` 比对；改口令 / 管理员重置口令必须
  把 `token_version` +1，让旧令牌立即失效。新增会改口令的入口时不要漏掉这一步。
- **个人访问令牌（PAT）**：`pat_` 前缀、256 位随机熵，给 MCP 服务器等程序化调用用；
  库里只存 sha256（`users.pat_hash`），明文只在签发响应出现一次。它**不校验
  `token_version`**，所以改口令 / 管理员重置口令时必须显式清空 `pat_hash`（重置口令还要清
  `builtin_mcp_pat_hash`），清空即吊销。`current_user` 按前缀分流；新增吊销入口不要漏。
- **凭据变动 ↔ 内置 MCP 令牌联动**：任何改凭据的入口（签发 / 吊销 PAT、改口令、管理员重置
  口令）都必须调 `runtime.mcp.refresh_builtin_tokens(session, user_id)`：内置「系统管理」MCP
  配置里那份 `CODA_TOKEN` 失效时**当场补发**一份新的（不用等平台重启，否则系统管理 MCP
  会一直 401）；仍然有效的令牌不轮换。新增凭据入口时别漏这一步。
- **头像（用户）**：`app/avatars.py` 只此一份（校验 / 落盘 / 读取）。只收**位图**
  （PNG / JPEG / WebP，按魔数认，≤2 MB——SVG 能内嵌脚本，而头像直接进 `<img>`，放进来就是
  存储型 XSS）；文件名由服务端按「用户 id + 内容摘要」生成（挡目录穿越、缓存自动失效）；
  文件在 `<数据目录>/avatars/`，库里只存 `users.avatar_file`。前端取图走 `lib/avatar.ts` 带令牌
  `fetch` 成 blob + object URL，**不要**拼 `?access_token=`（令牌会进日志与历史）；
  展示统一用 `components/UserAvatar.tsx`。
- **智能体头像只保留头像包**（外加自定义上传，emoji 头像已下线）：显示优先级
  **自定义上传（`avatar_file`）> 内置头像包（`avatar_preset`）> 按 id 稳定兜底的一张头像包图
  （`lib/agentAvatar.ts::fallbackAvatarPreset`）**；老的 `avatar` emoji 字段只为兼容历史数据保留。
  头像包（3D 黏土风，人物 + 卡通两类）由 `scripts/gen_avatar_pack.py` 生成清单与文件、
  `scripts/make_avatars_transparent.py` 抠图（前端靠 `AVATAR_PACK_REV` 让缓存整体失效）；
  **编号一旦发布只能往后追加**（库里存的是编号，重排会让已选过头像的智能体集体换脸，
  `tests/test_frontend_assets.py` 会校验清单与文件一一对应）。「随机生成」只是从清单里抽一个
  编号，不调用生图接口。展示统一用 `components/AgentAvatar.tsx`；**任何界面都不要再把
  `avatar` emoji 当头像渲染**（含 `🤖` 兜底与下拉框前缀），文字列表只显示名字。

---

## 8. 完成定义（Definition of Done）

```bash
bash scripts/test.sh              # 必须全绿：pytest + 前端 tsc
cd frontend && npm run build      # 只要改了前端就必须执行
curl -s http://127.0.0.1:8010/api/health
```

改动类型 → 必须做的验证：

| 改动 | 必须验证 |
| --- | --- |
| 后端逻辑/适配器 | `pytest`（新增能力要有新测试） |
| 前端任意改动 | `tsc --noEmit` + `npm run build` + **重启服务**后自测页面 |
| 移动端 / PWA 改动 | 同上，并额外自测 `/m/` 深链可刷新、断网后外壳仍能打开（`pytest tests/test_mobile_web.py`） |
| 事件协议/持久化 | 端到端跑一次真实对话（SSE 事件齐全、消息落库正确） |
| 数据库结构 | 用**现有** `backend/data/coda.db` 启动验证旧数据可读，不得重置 |
| 新增适配器 | `/api/agent-types` 能看到、表单可渲染、连通性测试可用 |
| 技能（编目/分配/物化） | `pytest` 覆盖解析与物化；端到端跑一次真实对话确认技能生效、未分配的不出现 |
| MCP（编目/分配/物化） | `pytest` 覆盖解析、物化、密钥只回掩码；端到端跑一次真实对话确认分配到的服务器写进隔离配置、未分配的不出现 |

重启服务（本地部署时用你的进程管理器，或直接重新执行 `scripts/serve.sh`）。

---

## 9. 变更影响面矩阵（改前对照，改后自检）

| 你改了 | 必须同步检查 |
| --- | --- |
| `agents/base.py` 契约 | 全部内置适配器 + 测试 |
| `agents/events.py` | `runtime/service.consume_run`、前端 `applyEvent` + `types.ts`、`docs/架构/architecture.md` **与 `docs/架构/ai-coding-standard.md` §4.1 两张事件表**（两处各有一份，容易只改一处） |
| `runtime/context.py`（上下文口径：计量 / 快照失效 / 压缩 / 上限） | `runtime/service.py`（落库与压缩触发点）、`api/conversations.py` + `api/system.py`、前端 `ContextMeter` + `lib/contextUsage` + `lib/types.ts`、`models.ADDITIVE_COLUMNS`（新列必须登记）、`docs/架构/architecture.md` 事件表、`docs/功能文档/上下文窗口与自动压缩.md`、`tests/test_context.py`；**新增 / 修改内核适配器**还要同步 `tests/test_adapters.py` 与该功能文档 §6.1 的「各内核对接现状」矩阵（逐内核实测，别照抄） |
| `agents/skills.py` / 技能契约 | 全部声明 `skills=True` 的适配器 + `runtime/skills.py` + `tests/test_skills.py` |
| `agents/metering.py` / 用量归一化 | 全部适配器 + `runtime/usage_ledger.py` / `recording_gateway.py` + `tests/test_usage_*.py` |
| `runtime/skills.py` | 智能体分配接口、`service.prepare_run` 的物化步骤、前端技能页与智能体设定页 |
| `agents/mcp.py` / MCP 契约 | 全部声明 `mcp=True` 的适配器 + `runtime/mcp.py` + `api/mcp.py` + `tests/test_mcp.py` |
| `runtime/mcp.py` · `api/mcp.py` | 智能体分配接口、`service.prepare_run` 的物化步骤、前端 MCP 页（两端）与智能体设定页 |
| `app/builtin_mcp.py`（内置 MCP 声明） | 启动播种与 `ensure_builtin_servers`、`docs/架构/architecture.md` §6、`docs/功能文档/admin-mcp.md`、`tests/test_mcp.py` 的内置用例；脚本要随 `scripts/release.sh` 与 `docker/Dockerfile` 打包 |
| `runtime/gateway_probe.py` · `api/gateways.py` | 网关 CRUD、官方案例播种（`bootstrap.py`）、前端 `GatewayManager` / `MobileGatewaysPage`、适配器的 `protocol`/`default_auth_style`/`default_models` |
| 新增记录网关 / 一手账本（`runtime/usage_ledger.py` · `runtime/recording_gateway.py` · `api/usage_gateway.py`） | 接线：`runtime/gateway.bind_recording`、`service.prepare_run` / `consume_run`、`Capabilities.recording_gateway`、`models.UsageRecord`（新表自动建；`UsageRecord.api_key_id` **登记 `ADDITIVE_COLUMNS`**）；外部 API：`app/api_keys.py`、`api/api_keys.py`、`api/model_gateway.py`、定价优先级（手工价 > models.dev 目录价 > unknown）与 `apply_cost`；原生 `context.usage` 实时入账（`record_native_snapshot_isolated`）与原生压缩配置（`AgentContext.context_limit_tokens/trigger_tokens` + `apply_context_policy`，`tests/test_native_compaction.py`）；轨迹补采（`api/_forward.py`、`ProxyTarget.capture_trace`，见 `docs/功能文档/轨迹调试视图.md` §6）；前端「外部 API」标签与 `UsageReport`；**不新增事件类型**（复用 `usage` / `context.usage` 展示通道）。完整影响面与测试清单见 `docs/功能文档/usage-gateway.md` 与 `docs/架构/architecture.md` §4.4 / §4.5 |
| `models.py` / `schemas.py` | `api/*` 的读写、前端 `types.ts`、旧数据兼容性 |
| `auth.py` / `auth_deps.py` / `api/auth.py` | 登录 / 注册 / 用户管理、`current_user` 依赖、`owner_id` 过滤、管理员入口、前端 `AuthGate` / `AccountPanel` / `UserAdminPanel` 与 `lib/auth.ts` |
| `app/avatars.py` / `api/auth.py` 的头像接口 | `models.ADDITIVE_COLUMNS` 的 `users.avatar_file`、`schemas` / `types.ts` 的 `AuthUser.avatar`、前端 `lib/avatar.ts` + `UserAvatar` / `AvatarUploader`（两端共用）、`tests/test_auth.py` 的头像用例 |
| 任一资产表新增 `owner_id` 或新的共享目录 | 对应 `ADDITIVE_COLUMNS`、`bootstrap.backfill_ownership`、列表/详情/写入三处的归属过滤、成员端应隐藏的管理员按钮 |
| `runtime/runner.py` | CLI 与 API 两条路径、取消/超时、脱敏 |
| `runtime/provisioning.py` | 运行时维护动作：命令来自适配器、SSE 事件信封（含登录的 `provision.qr` / `provision.input` 与 `login/input` 写回）、超时、取消、脱敏；任务列表 / 重连接口收紧为管理员 |
| 新增「运行时可由平台安装 / 更新」 | 适配器的 `provisioner`（`RuntimeProvisioner`）+ `docs/架构/architecture.md` §4.1 |
| 新增 `ConfigField` | 无需改前端；但要在 `bootstrap.py` 播种默认值里确认不影响首次启动 |
| `AgentProfile` 新增字段 | `schemas.py` 读写、`ADDITIVE_COLUMNS` 登记、前端 `types.ts` 与智能体设定页、旧数据兼容 |
| 新增「全局一份 + 多智能体引用」的实体（如凭据） | 独立表 + 引用表（仿 `agent_skills` / `agent_credentials`）、运行时按引用注入、接口只回掩码、设置页与智能体设定页两条入口 |
| 新增「统一编目 + 智能体分配」的实体（如 MCP 服务器） | 编目表 + 分配表（仿 `skills` / `agent_skills`）、适配器声明扫描与物化钩子、`runtime/` 的编目/分配/物化、接口只回掩码、两端页面 + 智能体设定页两条分配入口 |
| 新增适配器模块 | `registry` 自动加载（确认 import 无副作用/无网络请求）、`/api/agent-types`、文档表格 |
| 前端新增页面 | `App.tsx` 的 `NAV` + `Routes`、`index.css` |
| `frontend/src/mobile/*` · `frontend/m/index.html` | `MobileApp.tsx` 路由、`mobile.css`、`vite.config.ts` 的多入口、`tests/test_mobile_web.py` |
| `frontend/public/m/*`（manifest / Service Worker）· `public/icons/*` | `main.py:resolve_spa_target` 的前缀分派；应用图标由 `scripts/gen_icons.py` 重新生成（`tests/test_frontend_assets.py` 会核对），母版位图在 `assets/brand/`；`public/icons/kernels/*.svg` 是各内核的品牌图，属于适配器声明，不归生成脚本管；**改了图标要同步 `components/BrandMark.tsx`**（界面品牌标直接用同一张品牌图，位图自带品牌色） |
| 内核图标（`public/icons/kernels/*`） | 图形本身是手工 / 厂商产物；但要**内嵌**进前端包（图标秒显，运行期不读外部路径）：改完图形跑 `python3 scripts/gen_kernel_logos.py` 重新生成 `frontend/src/lib/kernelLogos.ts`（`tests/test_frontend_assets.py` 会核对）。取地址统一走 `frontend/src/lib/kernelIcons.ts`（内嵌清单 + 内存缓存 + 预解码），别在页面里直接写 `/icons/kernels/...` |
| `im/*`（IM 域） | `pytest tests/test_im.py` + `tests/test_im_architecture.py`（分层 / 平台元数据 / 归属）；新增平台只加 `base.PLATFORMS` 一条 + `platforms/` 一个模块；IM 表加列登记 `im/tables.py` 的 `ADDITIVE_COLUMNS`；碰运行层只能改 `bridge.py`；同步 `docs/功能文档/im.md` |
| `collab/*`（协作域） | `pytest tests/test_collab_architecture.py`（分层守卫）；新增表要在 `db.init_db` 注册；给协作表**加列**必须登记 `collab/tables.py` 的 `ADDITIVE_COLUMNS`（旧库不会被 `create_all` 补列）；碰执行层只能改 `execution.py`；节点级技能 / MCP 覆盖走 `NodeSpec.skill_ids` / `mcp_ids` + `runtime` 各查询函数的 `override_ids`（不写回智能体配置）；同步 `docs/计划任务/collaboration-design.md` |
| 版本号 | 只改 `backend/app/__init__.py`（唯一版本源） |

---

## 10. 反模式清单（出现即打回）

- ❌ 在 `runtime/`、`api/`、前端里 `if type_id == ...` 为单个智能体开分支。
- ❌ 适配器里 `import app.models` / 直接读写数据库 / 读全局配置。
- ❌ 前端硬编码某个智能体的表单字段（应走 `config_fields`）。
- ❌ 把明文密钥写进日志、SSE 事件、接口响应或测试快照。
- ❌ 新增事件类型却漏改 `consume_run` 或 `applyEvent`（界面直接看不到）。
- ❌ 新增可配置项却只在后端读、忘了声明 `ConfigField`（界面上无法配置）。
- ❌ 用"清空/重建数据库"来解决 schema 变更。
- ❌ 在框架层硬编码某个内核的技能目录或配置变量（应通过 `discover_skills()` / `skill_mount()` 声明）。
- ❌ 让智能体用到未分配的技能（把本机个人/插件技能顺手带进一次运行）。
- ❌ 技能「这一轮没扫到」就把 `skills` 记录和 `agent_skills` 分配一起删掉（目录还在、或连技能根
  都不在时必须只把 `available` 置 False，保住分配）；只有「技能目录确实不存在、技能根还在」
  才允许重扫自动解除分配并删行 —— 判据与顺序见 §3.8 与 `docs/功能文档/skills-mechanism.md` §3。
- ❌ 在框架层硬编码某个内核的 MCP 配置文件、键名或命令行开关（应通过 `discover_mcp_servers()` /
  `mcp_mount()` 声明）。
- ❌ 让智能体连上未分配的 MCP 服务器（把本机配置里的服务器顺手带进一次运行）。
- ❌ 把 MCP 的 `env` / `headers` 明文写进日志、SSE 事件或接口响应（只能加密落库 + 只回掩码）。
- ❌ 在网关之外另存 API 密钥（或让智能体直填密钥）——密钥只经网关、只经 `security`。
- ❌ 把凭据值写进提示词 / 日志 / 事件 / 接口响应（只能以环境变量注入，值一律掩码或擦除）。
- ❌ 允许协作图 / 对话 / 智能体引用不属于当前用户的资产（网关、凭据、平台通用 MCP、智能体），
  或让列表能看到的共享行在详情接口又变成 404。
- ❌ 管理员接口收紧后，前端仍向普通成员显示按钮或自动调用（成员只会拿到 403）。
- ❌ 前端改完不 build、不重启就宣称完成。
- ❌ 静默吞异常，或把错误只打到 stdout 不产出 `error` 事件。
- ❌ 引入 UI 框架/状态管理库/新依赖来解决一个 CSS 或几行状态能解决的问题。

---

## 11. 部署速查

- 生产：`bash scripts/serve.sh`（首次会自动建 venv、缺 `frontend/dist` 时自动构建），监听 `0.0.0.0:8010`。
- 环境变量：`CODA_HOST`（默认 `0.0.0.0`）、`CODA_PORT`（默认 `8010`）；
  注意：某些进程管理器给子进程注入自己的 `HOME`，会导致 `claude` 报 "Not logged in"，
  此时在服务环境里显式设 `HOME` 为真实用户家目录。
- 前后端分离开发：`bash scripts/dev.sh` → API `:8000` + Web `:5173`。
- 数据目录：`backend/data/`（SQLite `coda.db`、`workspaces/`、`secret.key`）；**不要在未备份时动它**。

### 版本发布与更新（版本级 · GitHub Release）

- 版本号规则：**只在公开发版时递增**，日常提交不动 `__version__` / `VERSION_HISTORY`
  （旧的“每次提交 bump 一版”会让版本号通胀，V0.01–V0.43 就是这么堆出来的）。
  V0.1 是首个公开版本，此前的内部迭代编号不再单列。
- 发版：改 `backend/app/__init__.py` 的 `__version__`（唯一版本源）→ 提交 →
  `bash scripts/release.sh`（构建前端、打 `coda.tar.gz` + `.sha256`、`gh release create`）。
  tag 必须与 `__version__` 一致（`v0.2` ↔ `V0.2`），脚本会校验。
- Docker：`docker/Dockerfile` 把**源码构建进镜像**（`/image-app`），`/app` 卷只存版本目录
  与 venv。空卷首启直接用镜像自带代码落一个版本（「构建什么就运行什么」）；
  设 `CODA_FETCH_RELEASE_ON_EMPTY=1` 才恢复「空卷先联网拉最新 Release」的旧行为。
  版本级更新走「设置 → 关于」：下载 Release → `releases/<版本>/` → 原子切 `current` →
  `os.execv` 原地重启（uvicorn 是 PID 1，容器不退出）。
- 首启装依赖的坑：pip **不认** `CODA_INSTALL_PROXY`（那是给 curl/npm/uv 的），必须由
  entrypoint 转成 `HTTP(S)_PROXY`；并可用 `CODA_PIP_INDEX_URL` 指向国内镜像。
- 部署配置与仓库分离：真实内网地址（代理、图床、网关）只写在部署侧 compose（不入 git），
  仓库模板一律用 `<placeholder>`。镜像源/代理都用环境变量注入，不改代码。
- 源码模式（仓库里直接跑）：关于页只提示新版本，不给一键更新按钮 —— 升级走 git。
- 仓库与 API 可用 `CODA_UPDATE_REPO` / `CODA_UPDATE_GITHUB_API` 覆盖（fork / GHE 部署）。
