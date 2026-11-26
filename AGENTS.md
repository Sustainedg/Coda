# AGENTS.md · Coda 工程约定

本文件对**所有在本仓库工作的 AI 编码代理**具有约束力。只要在这个仓库里改代码，就必须遵守。
动手之前请先读完本文件；需要背景与理由，再读 `docs/ai-coding-standard.md` 与 `docs/architecture.md`。

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
| MCP | MCP 服务器（stdio/http/sse）统一编目进 `mcp_servers` 表：平台可新建「通用」服务器，各内核配置里的只读收录；**只能通过智能体分配生效**（`agent_mcp_servers`）。平台内置服务器（`builtin_key` 非空，声明在 `app/builtin_mcp.py`：图片生成 / 系统管理）启动时逐用户播种，连接方式由平台维护 |
| 协作域 | `backend/app/collab/` —— 多智能体「工单」协作（可视化编排 + 波次并行引擎，默认串行），独立于对话域，见 `docs/collaboration-design.md` |
| IM 域 | `backend/app/im/` —— 把智能体连到聊天软件（Telegram / 飞书 / 钉钉 / 企业微信 / QQ / Discord / Slack）；入口在每个智能体设定页的「IM」标签，见 `docs/im.md` |

---

## 1. 铁律（MUST / MUST NOT）

1. **新增智能体只能走适配器。** 在 `agents/builtin/` 新增模块并 `@register_agent`。
   禁止在 `runtime/`、`api/`、前端里为某个具体 `type_id` 写分支。
2. **适配器是叶子节点。** 适配器**禁止** `import` 任何 `app.api.*`、`app.runtime.*`、`app.db.*`、`app.models`。
   它只能依赖 `agents/base.py`、`agents/events.py`、`agents/skills.py`、`agents/mcp.py`、
   `agents/builtin/util.py`、标准库与 `httpx`。
3. **适配器无状态、不碰数据库。** 一次运行所需的一切都从 `AgentContext` 取；解析过程的状态放进 `AgentRun.state`。
4. **所有可配置项必须声明为 `ConfigField`。** 禁止在 React 里为某个智能体硬编码表单字段。
5. **事件协议是框架与适配器之间唯一的方言。** 新增事件类型必须**同时**改：
   `agents/events.py`（枚举 + 构造器）、`runtime/service.py:consume_run`（累积/持久化）、
   前端 `lib/types.ts` + `lib/agentStream.ts:applyAgentEvent`、`docs/architecture.md` 的事件表。
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
 ┌──────────────────────────────▼────────────────────────────────────┐
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
 │                 registry(注册) · skills(技能契约) · mcp(MCP 契约) · builtin/*(实现) │
 ├───────────────────────────────────────────────────────────────────┤
 │ models/db · schemas · settings · security · auth/auth_deps · attachments · builtin_mcp · bootstrap│
 └───────────────────────────────────────────────────────────────────────────────────┘
```

依赖方向**只能自上而下**：`api → runtime → agents`；`api/runtime` 可依赖
`models/db/settings/security/auth/auth_deps/attachments`；
**`agents` 不得反向依赖任何上层**。跨层共享的数据结构定义在叶子层（如 `agents/base.py` 的 `AgentContext`）。
`auth.py`（口令哈希 / 令牌）与 `auth_deps.py`（当前用户 / 管理员 / 归属校验）放在顶层叶子层，
因为协作域有架构守卫禁止依赖 `app.api.*`，但协作路由同样需要鉴权。

IM 域 `im/` 同样是**旁路功能**，不在主干上：`api/__init__.py` 只把 `im/api.py` 挂成一条子路由，
它与对话域的**唯一边界**是 `im/bridge.py`（调 `runtime.service.prepare_run` / `start_run`）。
平台适配器（`im/platforms/*`）是**叶子节点**：只能依赖 `im/base.py`、`im/format.py`、
`im/crypto.py`（企业微信媒体解密用的 AES，`cryptography` 只在这里出现）、标准库、
`httpx` 与 `websockets`，不得 import `app.api.*` / `app.runtime.*` / `app.db` /
`app.models`（有守卫测试 `tests/test_im_architecture.py`）。「某个平台怎么收发消息」是
适配器的知识，框架层（`im/service.py`）不得为具体平台开分支。

协作域 `collab/` 是**旁路功能**，不在主干上：`api` 只把它当一条子路由挂上去，它在内部经**唯一边界**
`collab/execution.py` 调用 `runtime.runner`。硬约束（有守卫测试 `tests/test_collab_architecture.py`）：
`collab/*` 只允许依赖 `agents.events`（事件契约）、`models/db/settings` 与叶子工具模块
`app.credentials`（凭据解密 / 掩码 / 事件擦除，与对话域共用同一份）、
`app.attachments`（附件落盘 / 拼提示词，与对话域共用同一份），**不得** `import app.api.*`，
也**不得**绕过 `execution.py` 直接 `import app.runtime.*`；`tickets.py`/`graph.py`/`prompt.py` 必须是
不碰存储与 HTTP 的纯函数。框架层禁止反向依赖 `collab`。协作事件是自有的 `collab.*` 信封
（`collab/protocol.py`），**不属于**铁律 5 的适配器事件协议。

各层职责（改代码前先确认你改的是哪一层）：

| 层 | 负责 | 不负责 |
| --- | --- | --- |
| `agents/builtin/*` | 怎么调用某个智能体、怎么把它的输出翻译成事件 | 存储、HTTP、UI、取消（框架统一做） |
| `agents/base.py` `events.py` `registry.py` `skills.py` `mcp.py` | 契约与协议（改动影响面最大，慎改） | 具体产品逻辑 |
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
新增事件类型必须走铁律 5 的四步同步改动（**事件表有两份**：`docs/architecture.md` 与
`docs/ai-coding-standard.md` §4.1，两处都要改）。
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
`prompt`、`skills`、`mcp_servers`、`home_dir`），用 `ctx.option(key, default)` 取值。
`gateway_url` / `api_key` / `auth_style` 由框架从智能体引用的**网关**解密后注入（`runtime/gateway.py:bind_gateway`）；
`credentials` 是智能体自己的凭据（解密后的「环境变量名 → 明文」），由框架合并进子进程环境变量，
适配器不需要自己处理，也**不得**把它写进提示词、日志或事件。
`AgentRun` 是"无状态定义 + 一次具体运行"的绑定，持有 `state`（解析状态）、`emitted_text`、`thinking`、
`session_id`、`tool_calls`。
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

---

### 3.8 技能契约（`agents/skills.py`）
- `SkillSpec`：一个技能的只读描述（`type_id` / `slug` / `name` / `description` / `source_kind` / `path` /
  `fingerprint` / `files` / `owner_id` …）。共享技能（内核自带 / 宿主机）的 `id` 是
  `<type_id>:<slug>`；用户技能库里的技能是 `<type_id>:lib:<owner_id 或 local>:<slug>`，
  同名技能在不同用户下互不冲突。
- **用户技能库按用户隔离、按 slug 只存一份**：内核自带 / 宿主机上已有的技能由适配器扫描，
  `owner_id` 为空、全平台共享只读；导入、市场安装、挂载到其它内核的技能都落在平台数据目录
  `<数据目录>/skill-libraries/<owner_id>/<slug>`（**不再按内核分子目录**），归属到当前用户。
  一份技能被哪些内核引用登记在同级的 `<owner_id>/.mounts.json`（`{"<slug>": ["claude-code", …]}`），
  编目时按引用展开成「一个内核一行」（`skills.id = <type>:lib:<owner>:<slug>`），所有行共享同一个
  `path`。因此市场更新（重新安装）覆盖唯一实体后，挂载到它的全部内核同时生效。
  「复制到内核」实际只是登记引用，不再复制文件。技能页删除（`DELETE /api/skills/{id}`）只摘
  本内核引用，最后一个引用消失才删实体；市场页卸载（`POST /api/skillhub/uninstall`）按**实体整体**
  移除（断开全部智能体分配、删全部编目行与唯一目录）——市场安装默认挂到全部支持技能的内核，
  一份实体就是一个整体。升级前装进内核原生目录（`.skillhub`）的老市场技能仍走兼容删除路径。
  列表 / 详情 / 分配 / 导出 / 删除 / 用量都只对「共享 + 自己」生效；管理员可看全部并带归属用户名。
  旧布局 `<owner_id>/<type_id>/<slug>` 在重扫时自动迁移（同 slug 合并引用；内容冲突时市场份优先，
  另一份挪进 `<owner_id>/.conflicts/`）。**禁止**把用户技能写进内核的全局技能目录（那会让所有人共享）。
- `SkillMount`：适配器声明的**物化计划**（`home_name`、`skills_subdir`、`carry` 要携带的配置文件、
  `patch` 配置文件补丁、`notes` 用户提示）。框架只按这份声明执行，不认识具体内核。
- 适配器钩子：`skill_roots()`（声明本机原生技能根：路径 + 来源 + 是否可安装；框架据此
  统一扫描宿主机技能；用户技能库由框架自己托管，不写进这些根）、`discover_skills()`（只读扫描，尽力而为、不得抛错；常见实现是
  `scan_skill_roots(self.skill_roots(), ...)`）、`skill_mount(ctx)`（返回 `None` 表示该内核不支持技能）
  与 `skill_invocation(tool_name, tool_input)`（把一次工具调用识别成技能调用时返回技能 slug，
  供「用量」页统计调用次数；默认不识别——「技能调用长什么样」是适配器的知识）。
  技能可来自「技能市场」（`source_kind=market`），但它和用户导入的技能一样装进
  `<数据目录>/skill-libraries/<owner_id>/<slug>`（可卸载、可多内核共享引用）；不再写进内核的
  全局技能目录。适配器只负责「本机原生技能根在哪」，平台托管的用户库路径由框架统一维护。
- **插件技能只认「已安装」**：内核自带的插件市场目录（如 Claude Code 的
  `plugins/marketplaces/<市场>/`）只是**可浏览的货架**，里面的技能并没有安装。插件技能必须来自
  该内核自己的「已安装清单」（Claude Code 是 `plugins/installed_plugins.json` 里登记的
  `installPath`），否则技能页会把未安装的候选当成可分配技能，还能「删除」掉市场克隆本身。
  `source_kind=plugin` 的技能归插件管理器所有：**只读、不可单独删除**（卸载要整包走插件管理器），
  目录消失后也不在列表里留「已失效」行。
- **同名技能必须靠命名空间共存**：技能标识 `SkillSpec.id` 与物化目录名都取自
  `SkillSpec.slug`（技能目录名），不同来源各带一个同名技能时（多个插件各有 `access`）
  会互相覆盖、只剩一个。`SkillRoot.namespace` 用来给某个根下所有技能加
  `<namespace>-` 前缀（Claude Code 的已安装插件按插件名声明），原始目录名存进
  `SkillSpec.extra["original_slug"]`，展示名带上来源（`access · telegram`）。
- **物化时要同步 frontmatter 的 `name`**：Claude Code 按**目录名**调用技能，Hermes 按
  frontmatter 的 `name` 查找。加了前缀的副本若仍写着 `name: access`，Hermes 就找不到。
  `runtime/skills.materialize()` 在复制后调用 `_sync_frontmatter_name()` 只改**副本**，
  源目录始终只读；其余元数据不动。
- **内核自带技能要按归属清单认，不能按目录猜**：Hermes 把内置技能和用户技能**混在同一个**
  `skills/` 目录里，归属只能读 `skills/.bundled_manifest`（每行 `name:hash`，v1 是纯名字；
  `.curator_suppressed` 里被裁掉的内置技能也算自带）。清单里登记过的标 `source_kind=system`
  （只读、不可删），其余才是 `user`。清单缺失时退回「全按用户技能」（旧内核行为）。
  判断时要排除 `.skillhub`（市场目录也躺在 `skills/` 下，归平台管）。
  新内核若也把「自带」与「用户」混放，**必须**给出等价的归属清单，再在 `discover_skills()`
  里改写 `source_kind`；不允许只靠目录路径推断。
- **删除语义**：用户显式删除（`DELETE /api/skills/{id}`）或从市场卸载
  （`POST /api/skillhub/uninstall`）时，先断开 `agent_skills` 分配、再删编目行；磁盘目录删不掉
  要抛错，不能静默当作成功。用户只能删自己技能库里的技能；共享技能与插件技能只读
  （管理员可以删共享 / 他人的技能）。
  **技能目录真的从磁盘消失**（不是用户点删除）时，重扫会一并解除 `agent_skills` 分配并删掉
  编目行 —— 技能本体都没了，分配就是幽灵引用，运行时本来也会跳过它；目录恢复后重扫重新
  登记，但分配要重新勾选。判据必须是「技能目录确实不存在、**而且**它的技能根（父目录）还在」：
  目录还在、只是这一轮适配器没扫到（权限 / 内核没装好 / 扫描失败），或者连技能根都不在了
  （数据卷 / 内核配置目录暂时没挂上）时，只把 `available` 置 False，**不能动分配**，
  免得一次扫描落空就把用户的分配全清掉。用户技能库实体整个消失时，同级的 `.mounts.json`
  幽灵引用也一并摘掉（技能库根目录本身不在时不动登记）。
- `AgentContext.skills` / `AgentContext.home_dir`：框架注入的「本次运行生效的技能」与
  「物化好的隔离目录」（技能与 MCP 共用同一个「家」），适配器在 `build_invocation()` 里据此
  设置自己的配置目录环境变量。
- 声明了 `Capabilities.skills=True` 的适配器**必须**实现 `skill_mount()`（有架构守卫测试）。

### 3.9 MCP 契约（`agents/mcp.py`）
- `McpServerSpec`：一个 MCP 服务器的只读描述（`name` / `slug` / `transport`(stdio|http|sse) /
  `command` / `args` / `url` / `env` / `headers` / `type_id` / `source_kind`），`source_id` 为
  `<内核 id>:<slug>`（平台通用的服务器没有）。`env` / `headers` 的值可能含密钥。
- `McpMount`：适配器声明的**物化计划**（`home_name`、写进隔离目录的 `files`、写进工作目录的
  `workspace_files`、`notes`）。`home_name` 必须与同一适配器 `skill_mount()` 声明的一致：
  技能与 MCP 共用同一个隔离目录。
- 适配器钩子：`discover_mcp_servers()`（只读扫描本机已有的 MCP 配置，尽力而为、不得抛错）与
  `mcp_mount(ctx)`（返回 `None` 表示该内核不支持 MCP）。解析 / 渲染的共享工具（JSON / TOML 两种
  形状、`safe_slug`）在 `agents/mcp.py`；**「哪个内核把配置放哪、长什么形状」是适配器的知识**，
  框架不硬编码路径、键名或命令行开关（有架构守卫测试）。
- `AgentContext.mcp_servers` / `AgentContext.home_dir`：框架解密后注入的「本次运行生效的服务器」与
  「物化好的隔离目录」。密钥只能写进配置物化的产物（`files` / `workspace_files`），
  **禁止写进提示词、日志或事件**。
- 声明了 `Capabilities.mcp=True` 的适配器**必须**实现 `mcp_mount()`（有架构守卫测试）。
- **平台内置服务器**（`app/builtin_mcp.py` 的 `BUILTIN_SERVERS`）随平台分发：启动时为每个启用用户
  幂等播种一条（`builtin_key` 标记），连接方式（传输 / 命令 / 参数）由平台维护、接口拒绝修改，
  且不能删除（不需要时停用）。用户只需填写业务参数；平台托管参数用
  `BuiltinParam(managed=True, managed_source=…)` 声明来源（`public_base_url` / `platform_token`），
  `runtime/mcp.py` 只按声明执行，**不得**在框架层判断具体 `builtin_key` 或硬编码参数名。
  内置项按用户各一份：列表、详情与分配都只能看到 / 使用自己的那份（管理员也不跨用户查看）。

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
按铁律 5，四处 + 文档同步；同时更新 `docs/architecture.md` 的事件表。

### 4.6 让某个适配器支持技能
1. `capabilities` 里声明 `skills=True`。
2. 实现 `skill_roots()` 声明本内核的技能根（`SkillRoot(path, source_kind, installable, max_depth, namespace)`；
   平台「技能市场」的安装目录是其中 `installable` 的一个，通常把 `.skillhub` 单独列一条、
   `source_kind=MARKET_SOURCE`），再用 `agents/skills.py` 的 `scan_skill_roots()` 实现 `discover_skills()`，
   标注来源（`user` / `project` / `plugin` / `system` / `market`），**只读、不得抛错**。
   会重名的一类技能（如各插件自带的 `access`）按来源逐个声明根并填 `namespace`，
   否则同名技能会在编目与物化时互相覆盖。
3. 实现 `skill_mount(ctx)`：声明隔离目录名、需要携带的配置文件、要关闭的内核自带技能，
   并在 `build_invocation()` 里把 `ctx.home_dir` 写进对应环境变量（如 `CLAUDE_CONFIG_DIR`）。
   隔离 HOME 时注意内核「跟着 HOME 走的数据」不止技能根：登录态、设备身份等必须留在真实
   目录的数据（macOS 上按 `Application Support` 解析的，见 `doubao_cli.py:_link_macos_data`）
   要一并软链进隔离 HOME，漏链会让内核在隔离目录重建设备身份、对话起不来。
4. 加测试：参考 `backend/tests/test_skills.py`（解析、物化、隔离变量）。

### 4.7 新增前端页面
- **桌面端**：`src/pages/XxxPage.tsx`，在 `src/App.tsx` 的 `Routes` 与 `NAV` 注册；样式写进
  `src/index.css`，沿用既有 class 命名（`card`/`btn`/`pill`/`banner`…）。
- **移动端（`/m/`）**：`src/mobile/pages/MobileXxxPage.tsx`，在 `src/mobile/MobileApp.tsx` 注册路由；
  样式写进 `src/mobile/mobile.css`。移动端页面**不得**引用 `src/pages/`、`src/App.tsx` 或
  `src/index.css`（有守卫测试 `tests/test_mobile_web.py`），但可以复用 `src/lib/*` 与
  `DynamicForm` / `MessageView` / `MarkdownView` / `ConversationThread` / `CollabAgentThread` /
  `KernelLogo` / `TicketBoardKit` / `Composer` / `ContextMeter` / `Icons` / `McpPicker` /
  `McpServerForm` / `McpAssignPanel` / `TeamAvatarStack` / `ConversationExportForm` /
  `LoginInput` / `TaskProgressCard` / `AgentImSettings` / `AgentUsagePanel` /
  `UsageReport` / `PlatformIcon` / `Modal`
  这些纯展示组件（对话气泡只有 `MessageView` 一份，线程装配只有 `ConversationThread` 一份）。
  `AgentImSettings` / `AgentUsagePanel` 是智能体设定页「IM / 统计」标签的共用面板 ——
  它们渲染的 `usage-*` / `.modal` / `.platform-icon` 类在 `index.css` 与 `mobile.css`
  **各写一份**（移动端按手机宽度重排，桌面端在用量页样式段），改样式记得两边同步语义。
- **两端功能对齐、交互各表**：桌面端有的能力（例如协作组编排）移动端也要能用，但按触屏重做交互 ——
  移动端**不引用**桌面端的编排画布（`components/CollabCanvas` 等），协作组改用表单式编辑
  （`mobile/pages/MobileCollabTeamPage.tsx`，与桌面端共用 `lib/collabDraft.ts` 的字段语义）。
- **两端通用**：类型加在 `src/lib/types.ts`，接口调用加在 `src/lib/api.ts`；禁止在组件里直接 `fetch`。
- **移动端必须保持 PWA 可用**：新增页面不得依赖 `/m/` 之外的顶层路由；离线只会命中
  Service Worker 预缓存的外壳，任何新资源都要能从 `/m/` 入口按相对/绝对路径取到。
  **改了外壳（入口 HTML / 样式表 / 构建产物）必须把 `public/m/sw.js` 的缓存名版本 +1**，
  否则已添加到主屏的独立应用会继续吃旧缓存（它没有刷新按钮；设置页的「更新到最新版本」
  走 `pwa.ts:refreshShell()` 清缓存重载，是自助出口）。
- **移动端外壳钉在可视视口上**：`.m-app` 用 `position: fixed` + 四边贴 0，**不要**用
  `100vh`/`100dvh` 配 `height: 100%` 父链 —— 真机上地址栏与底部工具栏会伸缩，两者高度经常
  对不齐，底栏就会浮起来留缝或被工具栏压住；`env(safe-area-inset-*)` 负责刘海与 Home 条。
- **外观（深色 / 浅色 / 跟随系统）只有一份实现**：`src/lib/theme.ts`（模式存在 localStorage，
  解析结果写在 `<html data-theme>`；两个入口 HTML 各有一段等价的内联脚本做首屏防闪）。
  **没设置过默认「跟随系统」**（`readThemeMode()` 返回 `'auto'`，只有显式选过深色才固定深色）——
  模块与两段内联脚本必须同一套判定，否则首屏会闪错色。
  配色一律走 CSS 变量：`index.css` / `mobile.css` 的 `:root` 是深色兜底（脚本没跑起来时用），
  浅色只加 `[data-theme='light']` 覆盖块 —— **新写颜色不要写死深色**，先加变量、再补浅色值；
  原生外壳（状态栏 / 系统导航条）的配色由 `applyTheme()` 同步 `meta[name=theme-color]`。
- **移动端外壳贴住屏幕底边**：输入区（`.m-composer`）**不照搬**底部安全区 —— iOS 独立应用里
  `env(safe-area-inset-bottom)` 是 34px，而 Home 条本身只占 ~13px，照搬会在输入框下面留出一条
  空白（「界面底部空出一截」）；现在只留 6px 收边，并给 `html` 铺主题底色。输入框本身就是那个
  「框」：附件 / 语音在框内左侧，发送 / 停止在框内右侧，**不要**再写背景提示文字。
- **输入区 / 顶栏的图标是线条 SVG**：`components/Icons.tsx`（两端共用，`stroke="currentColor"`，
  尺寸走 `size`）。各系统的 emoji 字体大小、粗细都不一样（📎 在 iOS 上尤其大），一排小图标永远
  对不齐，也不跟主题换色；这类「要跟文字同色的操作图标」**不要**再用 emoji，新的就往这个文件里加。
  **左侧主导航（桌面侧栏 + 移动抽屉）也已改成线条 SVG**：`NAV[].icon` 存 `NavIconName` key
  （`'chat' | 'agents' | 'collab' | 'skills' | 'mcp' | 'usage'`），图形统一由 `NavIcon` 按 key 取；
  两端菜单各自写字面量、由 `test_main_nav_is_the_same_menu_on_both_ends` 比对一致 ——
  加菜单项要同时改两处 `NAV` + `Icons.tsx` 的映射表。（空状态里那种大号装饰性图标仍可用 emoji。）
- **输入区只有一份**：`components/Composer.tsx`（两端共用，受控：草稿 / 附件 / 上传 / 发送都
  由页面给）。对话页与协作运行页（含聊天弹层）都用它，**不要**再各写一个输入框 —— 两处的交互与
  样式必须完全一致（样式仍是两端各一份：共享段逐字一致，有守卫测试
  `tests/test_mobile_web.py::test_composer_styles_are_identical_on_both_ends`）。
  附件与语音的提示通过 `onNotice` 回给页面，输入框不自己弹东西。页面自己的
  语义（协作的状态提示、「需要你决定」入口、推进中的停止条）走 `header` 插槽传进去；要允许空发送就开
  `allowEmptySend`（别为这类差异复制一份输入区，也别把页面语义塞进组件）。
  **回复期间继续输入**：开 `queueWhenBusy`，框里有内容时右侧按钮从「停止」变成「排队发送」，
  发送只是交给页面入队（队列状态与「本轮结束自动发出」的 effect 在页面里，组件不持有队列），
  两个按钮不并存 —— 别为此再写一套输入框。
- **协作运行页主页面就是那张协作流程图**：`MobileCollabRunPage` 主区整屏铺 `MobileCollabProgress`
  （内部是两端共用的 `CollabFlowMap`：节点 = 头像 + 昵称，头像右上角是状态徽标，点节点弹
  「在做什么」详情卡），时间线与「需要你决定」卡片收进 `MobileSheet full` 的对话弹层 ——
  **点入口智能体节点**才出来（顶栏不要再放对话按钮），点其他岗位开只读岗位会话。
- **协作运行页只有一条聊天通道，没有「模式」可选**：所有消息统一发进入口智能体的聊天通道
  （`POST /api/collab/runs/{id}/chat`，可带附件），**不要**再加「普通对话 / 推进任务」模式按钮
  或两档选项框 —— 用户手动选错模式就是「发消息没反应」，这正是它被删掉的原因。是否推进任务由
  **CEO（入口智能体）判断**：它认为需要推进时在回复末尾输出 `chat_advance` 块，系统解析后渲染
  一张「推进任务」决策卡（`reason` / `order` / `options`；桌面 `collab-advance-card`、移动
  `m-advance-card`）；**用户点选后**才把 `order`（或所选选项的 `value`）作为消息发进工单通道
  （`POST /api/collab/runs/{id}/messages`）。任务还没开始时，这第一条指令就是开场的任务描述。
  `freeform` 选项**只把建议指令预填进输入框并聚焦，不直接发送**。决策卡数据来自
  `collab.chat.finished` 的 `advance` 字段，刷新后从落库 meta 的 `advance` 恢复；点选 / 关掉 /
  下一轮聊天开始即清空，`busy` / `chatBusy` 期间不显示。**空内容不能发送**；从挂起点续跑仍走
  「继续」（`/resume`），不做「空发送 = 继续推进」的特例。
- **附件能力下沉在 `backend/app/attachments.py`**：校验 / 落盘 / 拼提示词只有一份，对话
  （`POST /api/conversations/{id}/attachments`）与协作（`POST /api/collab/runs/{id}/attachments`）
  都调用它，别再各写一份 base64 解码与目录穿越校验。
- **朗读（语音合成）只有一份实现**：`src/lib/tts.ts`（开关存在 localStorage 的 `coda.tts`，
  **默认关闭**；开启后新回复会被念出来，Markdown 先转成纯文本）。两个 iOS 坑必须一起处理：
  ① 首次发声要在**用户手势里**解锁（点开关时 `unlockSpeech()`）；② 打断上一条时 `cancel()` 与
  `speak()` 不能在同一轮里（iOS 会吞掉），要丢到下一个任务。对话页顶栏右上角只有「导出 + 朗读」
  两个按钮 —— **不要再往顶栏塞「⋯」**。桌面端左侧栏对话行只放「✎ 重命名 / 🗑 删除」两枚悬停图标，
  不再有「⋯」与会话操作弹窗（手机抽屉的「⋯」仍保留其余会话操作）。
  **清除智能体记忆用输入框里的 `/clear` 命令**：两端在提交时拦下它，调 `resetSession` 后给一条提示，
  不把它当正文发给智能体。
- **导出对话**：对话页顶栏的导出按钮打开 `components/ConversationExportForm.tsx`（两端共用），
  可选「图片 / Markdown」×「对话内容 / 完整数据（含思考与工具调用）」。图片由
  `src/lib/shareImage.ts`（Canvas 手绘，不引依赖）渲染成长图；Markdown 由
  `src/lib/conversationExport.ts` 的 `buildConversationMarkdown()` 生成；两者都经
  `src/lib/download.ts` 落盘（导出与技能下载共用这一份）。消息组装统一走
  `buildExportMessages()`，页面不要另写一套导出格式。
- **安装归「内核管理」，配置归「智能体」**：一个内核 = 一份安装 + N 个智能体，所以内核的
  安装 / 更新 / 卸载留在**设置 → 内核管理**（原「运行时」，只是界面这一栏改名；代码里的
  `runtime/` / `provisioner` / `/api/runtimes` 等标识符不变），智能体（网关 · 模型 · 人格 · 技能 ·
  适配器选项）有自己的配置页 `mobile/pages/MobileAgentsPage.tsx`。适配器选项仍由 `DynamicForm`
  渲染，**不要**在前端硬编码字段。内核管理**只有一处**（桌面端是设置里的标签页
  `components/KernelManager.tsx`；移动端是独立一页），别再为它加第二个入口。
- **网关是 API 密钥的唯一来源**：移动端设 `mobile/pages/MobileGatewaysPage.tsx`（自定义网关 CRUD +
  获取模型 + 手动旁路）。官方链路由启动播种生成，用户在智能体配置里直接选。
- **移动端设置页只留「现在要动的开关」**：外观 + 配置入口（网关 / 技能 / MCP / 内核管理 / 智能体）+
  一行「更多」。运行概览、版本记录这些信息看桌面端即可；「更新到最新版本」
  （`pwa.ts:refreshShell()`）必须留着 —— 独立应用里没有刷新按钮，那是卡在旧外壳时的唯一出口。
- **MCP 有两个入口**：主导航里的「MCP」页负责编目（新建 / 编辑 / 停用 / 删除 + 重新扫描），
  「分配给哪些智能体」既可在 MCP 页做（`McpAssignPanel`），也可在智能体表单里勾（`McpPicker`，
  两端共用同一份组件）。
- **移动端锁定缩放**：移动端是「固定外壳」的应用形态（顶栏 + 左侧抽屉 + 内部滚动），入口视口写
  `maximum-scale=1.0, user-scalable=no`，并在 `mobile.css` 用 `touch-action: pan-x pan-y` 兜底
  （iOS Safari 忽略 `user-scalable=no`，只有 `touch-action` 真正生效）。配套约束：移动端输入框字号
  一律 ≥ 16px，否则 iOS 聚焦时会强制放大页面。**桌面端不做此限制**（浏览器缩放是它的无障碍能力）。

### 4.8 扩展协作域
协作的一切都留在 `backend/app/collab/`：新增动作/状态改 `schemas.py` + `engine.py`（并同步
`docs/collaboration-design.md`）；纯逻辑放 `tickets.py` / `graph.py` / `prompt.py`（有"必须是纯函数"的
守卫）；对 `runtime`/`agents` 的调用只能加在 `execution.py`。给协作表加列要同时登记
`ADDITIVE_COLUMNS`（SQLite 的 `create_all` 不会给旧表补列）。**不要**为了让协作跑通去改适配器或 `runtime/`。
- **运行快照**：`collab_runs.graph` 在第一次推进（下发第一张工单）时冻结整张图，引擎只读它。改协作组只影响还没开跑 / 之后新建的运行；
  不要再用「有运行记录就禁止改图」的方式保护老任务（老运行靠自己的快照，升级前的由
  `store.backfill_run_graphs()` / 保存前补拍兜底）。
协作聊天也能带附件：落盘用共享的 `app.attachments`，接口是
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
   update_argv=…, uninstall_argv=…, latest_argv=…, prepare_argv=…, login_argv=…)` —— 命令（用哪个包管理器、包名、装到哪、
   动作前要清掉什么残留）是**适配器的知识**，框架只负责按声明在受控子进程里执行、流式回显、超时与脱敏。
   `prepare_argv` 是动作前的清扫命令（清掉上一次被中断的动作留下的残留），失败只回一行告警、不中断动作。
   npm 系内核的安装 / 更新命令由 `builtin/util.py:npm_install_argv()` 生成：它会先让出被别的安装
   占着的同名可执行文件（否则 npm 直接失败在 `EEXIST`），失败时再把对方放回去。
2. 账号型内核（扫码 / OAuth，没有 API Key）额外声明 `login_argv`（如 `doubao login --qr=png`）与
   `AgentDefinition.auth_status()`。登录命令按通用约定输出 `QR_IMAGE=<图片路径>`，框架读成
   `data:` URL 经 `provision.qr` 事件推给界面；登录任务的任务列表 / 重连接口**只对管理员开放**
   （输出里带的是短期凭据，见 §7）。
   只能交互式登录、没有可代跑子命令的内核（例如只有 TUI `/login` 的内核）**不要声明假的
   `login_argv`**：只实现 `auth_status()` 即可 —— `supports_auth()` 会让「内核管理」卡片显示
   登录状态，但不出现一个点了必然失败的「登录」按钮。
   如果内核另有**可编程的登录入口**（本地 HTTP API / OAuth 设备码 / 扫码接口），在适配器目录里
   写一个登录助手脚本，由 `login_argv` 调起，并仍按 `QR_IMAGE=` 协议输出二维码。助手是适配器的
   一部分，框架仍然不认识具体内核；参考 `builtin/codebuddy_login.py`（`--serve` API → 授权链接
   → 二维码 → 轮询完成）。
   如果内核的登录**必须把浏览器里的授权码贴回来**（例如 Antigravity CLI 的 OAuth 授权码模式），
   助手再额外打印一行 `INPUT_PROMPT=<提示文本>`：框架据此产出 `provision.input` 事件、界面弹出
   输入框，用户提交的文本由 `POST /runtimes/{id}/login/input` 写回登录子进程的 stdin
   （框架层 `LoginInputChannel`，登录子进程的 stdin 因此保持打开）。参考
   `builtin/antigravity_login.py`（`ANTIGRAVITY_REMOTE_OOB_AUTH=1` 的非交互 OOB 认证 →
   链接 / 二维码 / 输入框 → 轮询凭据落盘）。
3. 多账号：内核若只维护「当前这一个」登录态，适配器可以把每次登录的凭据存档成平台侧槽位，
   在 `auth_status()` 的 `accounts` 里列出（`RuntimeAccount`），并覆盖
   `activate_account()` / `remove_account()`。框架提供 `POST /runtimes/{id}/accounts/{账号}/activate`
   与 `DELETE /runtimes/{id}/accounts/{账号}`，前端在内核详情里渲染账号列表；
   `supports_accounts()` 由「是否覆盖 `activate_account()`」判断。参考
   `builtin/codebuddy_accounts.py`。切换只影响之后启动的对话，正在跑的不换号。
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
  `runtime/credentials.py`：
  校验（`validate_name`）、解密（`decrypt_all`）、只读掩码视图（`reads`）、提示词里的名字清单（`notice`）、
  文本擦除（`scrub` / `scrub_event`）只此一份；整表替换引用关系走 `runtime/credentials.py:set_agent_credentials`。
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
- **头像**：`app/avatars.py` 只此一份（校验 / 落盘 / 读取）。硬约束：
  ① **只收位图**（PNG / JPEG / WebP，按魔数认，≤2 MB）—— SVG 能内嵌脚本，而头像会直接进
  `<img>`，放进来就是存储型 XSS；② 文件名由服务端按「用户 id + 内容摘要」生成，不采信客户端
  文件名（挡目录穿越，且「换头像 = 换文件名」让缓存自动失效）；③ 文件在
  `<数据目录>/avatars/`，库里只存 `users.avatar_file`。
  前端取图走 `lib/avatar.ts` 带令牌 `fetch` 成 blob + object URL，**不要**把头像地址塞进
  `<img src>` 拼 `?access_token=`（令牌会进日志与历史）。展示统一用 `components/UserAvatar.tsx`
  （有图显示图、没图显示显示名首字母）。
- **智能体头像**：**只保留头像包**（外加自定义上传），emoji 头像已全部下线 ——
  显示优先级 **自定义上传（`avatar_file`）> 内置头像包（`avatar_preset`）> 按智能体 id
  稳定兜底的一张头像包图（`lib/agentAvatar.ts::fallbackAvatarPreset`）**；老的 `avatar`
  emoji 字段只为兼容历史数据保留，界面不展示、选择器也不再提供。内置头像包是 3D 黏土风角色（**人物 + 卡通**两类），
  图片在 `frontend/public/avatars/`、清单在 `frontend/src/lib/avatarPack.ts`，
  **清单与文件由 `scripts/gen_avatar_pack.py` 一次生成**（`SOURCES` 里每张拼图标分类，
  `CATEGORIES` 定分组名，清单会带出 `AVATAR_GROUPS` 供画廊分组）；白底抠成透明背景用
  `scripts/make_avatars_transparent.py` 原地处理（文件名不变，前端靠 `lib/agentAvatar.ts` 的
  `AVATAR_PACK_REV` 让缓存整体失效）。**编号一旦发布只能往后追加**（库里存的是编号，
  重排会让已选过头像的智能体集体换脸）。
  两处都必须与清单一一对应（`tests/test_frontend_assets.py` 会校验）。
  动画帧（`scripts/gen_mascot_pack.py`）只是**离线实验工具**，界面不再使用，也不参与运行时。
  「随机生成」只是从清单里抽一个编号，不调用生图接口 —— 秒出、离线可用、零成本。
  编号形状（`pack-NNN`）由 `app/avatars.is_valid_preset` 校验；展示统一用
  `components/AgentAvatar.tsx`（取自定义图同样走带令牌 blob，不要拼 `?access_token=`）。
  **任何界面都不要再把智能体的 `avatar` emoji 当头像渲染**（含 `🤖` 兜底与下拉框前缀）——
  文字列表 / 下拉框只显示名字，需要智能体头像的地方一律用 `AgentAvatar`。
  `tests/test_frontend_assets.py::test_agent_avatars_only_come_from_the_avatar_pack` 会拦。

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
| `agents/events.py` | `runtime/service.consume_run`、前端 `applyEvent` + `types.ts`、`docs/architecture.md` **与 `docs/ai-coding-standard.md` §4.1 两张事件表**（两处各有一份，容易只改一处） |
| `runtime/context.py`（上下文口径：计量 / 快照失效 / 压缩 / 上限） | `runtime/service.py`（落库与压缩触发点）、`api/conversations.py` + `api/system.py`、前端 `ContextMeter` + `lib/contextUsage` + `lib/types.ts`、`models.ADDITIVE_COLUMNS`（新列必须登记）、`docs/architecture.md` 事件表、`docs/功能文档/上下文窗口与自动压缩.md`、`tests/test_context.py`；**新增 / 修改内核适配器**还要同步 `tests/test_adapters.py` 与该功能文档 §6.1 的「各内核对接现状」矩阵（逐内核实测，别照抄） |
| `agents/skills.py` / 技能契约 | 全部声明 `skills=True` 的适配器 + `runtime/skills.py` + `tests/test_skills.py` |
| `runtime/skills.py` | 智能体分配接口、`service.prepare_run` 的物化步骤、前端技能页与智能体设定页 |
| `agents/mcp.py` / MCP 契约 | 全部声明 `mcp=True` 的适配器 + `runtime/mcp.py` + `api/mcp.py` + `tests/test_mcp.py` |
| `runtime/mcp.py` · `api/mcp.py` | 智能体分配接口、`service.prepare_run` 的物化步骤、前端 MCP 页（两端）与智能体设定页 |
| `app/builtin_mcp.py`（内置 MCP 声明） | 启动播种与 `ensure_builtin_servers`、`docs/architecture.md` §6、`docs/admin-mcp.md`、`tests/test_mcp.py` 的内置用例；脚本要随 `scripts/release.sh` 与 `docker/Dockerfile` 打包 |
| `runtime/gateway_probe.py` · `api/gateways.py` | 网关 CRUD、官方案例播种（`bootstrap.py`）、前端 `GatewayManager` / `MobileGatewaysPage`、适配器的 `protocol`/`default_auth_style`/`default_models` |
| `models.py` / `schemas.py` | `api/*` 的读写、前端 `types.ts`、旧数据兼容性 |
| `auth.py` / `auth_deps.py` / `api/auth.py` | 登录 / 注册 / 用户管理、`current_user` 依赖、`owner_id` 过滤、管理员入口、前端 `AuthGate` / `AccountPanel` / `UserAdminPanel` 与 `lib/auth.ts` |
| `app/avatars.py` / `api/auth.py` 的头像接口 | `models.ADDITIVE_COLUMNS` 的 `users.avatar_file`、`schemas` / `types.ts` 的 `AuthUser.avatar`、前端 `lib/avatar.ts` + `UserAvatar` / `AvatarUploader`（两端共用）、`tests/test_auth.py` 的头像用例 |
| 任一资产表新增 `owner_id` 或新的共享目录 | 对应 `ADDITIVE_COLUMNS`、`bootstrap.backfill_ownership`、列表/详情/写入三处的归属过滤、成员端应隐藏的管理员按钮 |
| `runtime/runner.py` | CLI 与 API 两条路径、取消/超时、脱敏 |
| `runtime/provisioning.py` | 运行时维护动作：命令来自适配器、SSE 事件信封（含登录的 `provision.qr` / `provision.input` 与 `login/input` 写回）、超时、取消、脱敏；任务列表 / 重连接口收紧为管理员 |
| 新增「运行时可由平台安装 / 更新」 | 适配器的 `provisioner`（`RuntimeProvisioner`）+ `docs/architecture.md` §4.1 |
| 新增 `ConfigField` | 无需改前端；但要在 `bootstrap.py` 播种默认值里确认不影响首次启动 |
| `AgentProfile` 新增字段 | `schemas.py` 读写、`ADDITIVE_COLUMNS` 登记、前端 `types.ts` 与智能体设定页、旧数据兼容 |
| 新增「全局一份 + 多智能体引用」的实体（如凭据） | 独立表 + 引用表（仿 `agent_skills` / `agent_credentials`）、运行时按引用注入、接口只回掩码、设置页与智能体设定页两条入口 |
| 新增「统一编目 + 智能体分配」的实体（如 MCP 服务器） | 编目表 + 分配表（仿 `skills` / `agent_skills`）、适配器声明扫描与物化钩子、`runtime/` 的编目/分配/物化、接口只回掩码、两端页面 + 智能体设定页两条分配入口 |
| 新增适配器模块 | `registry` 自动加载（确认 import 无副作用/无网络请求）、`/api/agent-types`、文档表格 |
| 前端新增页面 | `App.tsx` 的 `NAV` + `Routes`、`index.css` |
| `frontend/src/mobile/*` · `frontend/m/index.html` | `MobileApp.tsx` 路由、`mobile.css`、`vite.config.ts` 的多入口、`tests/test_mobile_web.py` |
| `frontend/public/m/*`（manifest / Service Worker）· `public/icons/*` | `main.py:resolve_spa_target` 的前缀分派；应用图标由 `scripts/gen_icons.py` 重新生成（`tests/test_frontend_assets.py` 会核对），母版位图在 `assets/brand/`；`public/icons/kernels/*.svg` 是各内核的品牌图，属于适配器声明，不归生成脚本管；**改了图标要同步 `components/BrandMark.tsx`**（界面品牌标直接用同一张品牌图，位图自带品牌色） |
| 内核图标（`public/icons/kernels/*`） | 图形本身是手工 / 厂商产物；但要**内嵌**进前端包（图标秒显，运行期不读外部路径）：改完图形跑 `python3 scripts/gen_kernel_logos.py` 重新生成 `frontend/src/lib/kernelLogos.ts`（`tests/test_frontend_assets.py` 会核对）。取地址统一走 `frontend/src/lib/kernelIcons.ts`（内嵌清单 + 内存缓存 + 预解码），别在页面里直接写 `/icons/kernels/...` |
| `im/*`（IM 域） | `pytest tests/test_im.py` + `tests/test_im_architecture.py`（分层 / 平台元数据 / 归属）；新增平台只加 `base.PLATFORMS` 一条 + `platforms/` 一个模块；IM 表加列登记 `im/tables.py` 的 `ADDITIVE_COLUMNS`；碰运行层只能改 `bridge.py`；同步 `docs/im.md` |
| `collab/*`（协作域） | `pytest tests/test_collab_architecture.py`（分层守卫）；新增表要在 `db.init_db` 注册；给协作表**加列**必须登记 `collab/tables.py` 的 `ADDITIVE_COLUMNS`（旧库不会被 `create_all` 补列）；碰执行层只能改 `execution.py`；节点级技能 / MCP 覆盖走 `NodeSpec.skill_ids` / `mcp_ids` + `runtime` 各查询函数的 `override_ids`（不写回智能体配置）；同步 `docs/collaboration-design.md` |
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
  才允许重扫自动解除分配并删行 —— 见 §3.8「删除语义」。用户显式删除 / 市场卸载走
  `runtime/skills.py` 的删除流程，先断开分配再删行。
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
- 部署配置与仓库分离：真实内网地址（代理、图床、网关）只写在部署侧 compose（不入 git），
  仓库模板一律用 `<placeholder>`。镜像源/代理都用环境变量注入，不改代码。
- 首启装依赖的坑：`/app` 卷为空且镜像 venv 的 stamp 与 requirements 对不上时会走在线
  `pip install`。pip **不认** `CODA_INSTALL_PROXY`（那是给 curl/npm/uv 的），必须由
  entrypoint 转成 `HTTP(S)_PROXY` —— 否则就是直连 PyPI，国内网络实测只有 ~15KB/s，
  装完要二十多分钟。entrypoint 已把 pip 包进 `run_with_install_proxy`，并支持
  `CODA_PIP_INDEX_URL` 指向国内镜像（清华 / 阿里，实测 17–24 秒装完全套）。
- 源码模式（仓库里直接跑）：关于页只提示新版本，不给一键更新按钮 —— 升级走 git。
- 仓库与 API 可用 `CODA_UPDATE_REPO` / `CODA_UPDATE_GITHUB_API` 覆盖（fork / GHE 部署）。
