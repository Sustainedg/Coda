# AI 编码规范（详版）

> 本文件是 [`AGENTS.md`](../AGENTS.md) 的展开版：`AGENTS.md` 是**硬约束清单**（必须遵守，会被编码代理自动加载），
> 这里解释**为什么这样分层、边界在哪里、怎么扩展、怎么评审**。
> 配套阅读：[`architecture.md`](./architecture.md) 讲"一次运行发生了什么"，本文件讲"代码应该怎么组织"。

---

## 1. 设计目标与原则

这个项目的生命周期里，唯一确定的事情是：**智能体会越来越多**（Claude Code、Codex、Gemini CLI
之后还会有更多 CLI 与 API 型智能体）。所以架构的目标不是"支持 N 个智能体"，
而是"**增加第 N+1 个智能体的成本趋近于零**"。

四条原则，按优先级排序：

1. **易变的东西必须被压缩到最小。** 每个智能体"怎么被调用、输出长什么样"是易变的；
   流式传输、会话记忆、持久化、表单渲染、取消与超时是稳定的。易变部分全部塞进 `agents/builtin/`。
2. **稳定层只认一种方言。** 框架与前端只理解"规范化事件"；不理解 Claude 的 `stream_event`，
   也不理解 Codex 的 `item.completed`。方言翻译在适配器里完成。
3. **能力用声明表达，不用 if 判断。** `capabilities` 与 `config_fields` 是**数据**，
   框架读数据决定行为；一旦某处开始写 `if type_id == ...`，架构就开始腐烂。
4. **依赖单向、叶子最稳。** `api → runtime → agents`，反向依赖一律禁止；
   公共数据结构放在被依赖的叶子层。

---

## 2. 功能分层（为什么这么分）

### 2.1 分层与职责

```
frontend/src        pages · components · lib(api·types)        纯展示 + 交互（桌面端 /）
frontend/src/mobile 移动端 pages/components + mobile.css · PWA   纯展示 + 交互（移动端 /m/）
        │  REST /api/*  ·  SSE POST /api/conversations/{id}/messages · GET .../{id}/stream
api/                路由 · Pydantic 校验 · SSE 封装 · HTTP 状态码   薄
        │
collab/             协作域（旁路）：编排图 · 工单协议 · 波次并行引擎 · 运行 API
im/                 IM 域（旁路）：连接登记 · 配对 · 平台适配器（叶子）· 运行桥
        │
runtime/service.py  编排：对话→智能体→适配器（网关解密密钥与地址）、能力决策、历史回放、落库
runtime/runner.py   执行：起子进程 / 发 HTTP、逐行/逐块转事件、双向 stdin 代写、超时、取消、脱敏
runtime/manager.py  运行表：注册、查询、取消、快照
        │
agents/base.py      契约（AgentDefinition/AgentContext/AgentRun/Invocation/ConfigField/Capabilities）
agents/events.py    事件协议（EventType + 构造器）
agents/registry.py  插件注册（@register_agent + 自动加载 builtin/*）
agents/skills.py    技能契约（SkillSpec / SkillRoot / SkillMount + 扫描）
agents/mcp.py       MCP 契约（McpServerSpec / McpMount + JSON / TOML 解析与渲染）
agents/builtin/*    ★ 各智能体的实现（唯一的扩展点）
        │
models.py db.py schemas.py settings.py security.py bootstrap.py   存储 / 契约 / 配置 / 密钥
builtin_mcp.py                                                     平台内置 MCP 的纯声明（不碰库 / HTTP / 运行时）
auth.py auth_deps.py                                               口令 / 令牌（含 PAT） / 当前用户 / 归属校验
```

### 2.2 每条依赖规则背后的理由

| 规则 | 理由 |
| --- | --- |
| 适配器**不得** import `api/runtime/db/models/settings` | 适配器是叶子。一旦它能读数据库或读全局配置，就无法独立测试，也无法保证"新增智能体不动框架" |
| IM 平台适配器**不得** import 上层；`cryptography` 只允许经 `im/crypto.py` | 与智能体适配器同理；把平台协议细节（企业微信 AES）收进一个叶子工具模块，依赖清单才精确可控 |
| 适配器**不得**自己持久化 | 持久化是 `service.consume_run` 的职责；适配器只产出事件。这样才能保证"换个存储不影响任何适配器" |
| `api/` 保持薄 | 参数校验用 `schemas.py`，业务决策在 `runtime/`。这样同一套逻辑既能被 REST 复用，也能被未来的 WebSocket/CLI 复用 |
| 前端**不得**知道任何智能体细节 | 表单由 `config_fields` 生成（`DynamicForm`），消息展示只认 `MessagePart`/`AgentEvent`。加智能体不该改一行前端 |
| 公共结构定义在叶子层 | `AgentContext` 定义在 `agents/base.py`，被 `runtime` 依赖，而不是反过来 |

### 2.3 分层自检（可执行的守卫）

`backend/tests/test_architecture.py` 把上述规则固化成了断言：

- 适配器模块 AST 里出现指向上层模块的导入（如 `from ...models import ...`、`import app.runtime`）→ 测试失败；
- 事件枚举与文档中的协议表不一致 → 测试失败；
- `api/` 或 `runtime/` 里出现硬编码的适配器 id → 测试失败；
- 适配器的 `description` / 字段 `label` 不是中文 → 测试失败。

**新增能力时，如果守卫测试开始变红，先怀疑自己是不是在违反分层，而不是去改测试。**

---

## 3. 一次运行的链路（定位改动点用）

```
POST /api/conversations/{id}/messages
  api/conversations.send_message()
    └ runtime/service.prepare_run()          ← 载入 Conversation→AgentProfile→Gateway→AgentDefinition
         ├ build_context()                   ← 组装 AgentContext（合并 options、解密网关密钥、算最终 model/gateway）
         ├ supports_sessions() / transcript_prompt()  ← 能力决策：续接 or 回放
         └ registry.register()               ← 生成 run_id，供取消/追踪
  runtime/service.start_run()
    ├ run.publish(turn.started)             ← 框架自有的传输层事件，先入缓冲
    └ asyncio.create_task(_execute_prepared())  ← 运行与 HTTP 连接解耦
         └ runtime/service.consume_run()
              └ runtime/runner.stream_run()
                   ├ Runtime.CLI → create_subprocess_exec() → AgentRun.feed(每行) → parse_line()
                   └ Runtime.API → definition.stream(ctx)
         finally: 累积 text/thinking/tool/usage/session → 写回 assistant Message → commit
  StreamingResponse(sse_stream(run.subscribe()))   ← 只负责订阅事件，断开只退订
```

要点：
- **能力决策只在 `prepare_run` 里做一次**，不要在适配器里重复实现"要不要回放历史"。
- **正常路径的落库只在 `consume_run` 的 finally 里做一次**，保证成功/失败/取消都会写回消息（状态标记为
  `complete` / `error` / `interrupted`）。后台任务若在进入 `consume_run` 之前就失败，由
  `_execute_prepared` 兜底把占位消息标成 `error`（只此一条异常路径）。
- **执行与连接解耦**：客户端断连只让 SSE 订阅者离开，**不会**终止后台运行；刷新页面或另一台设备
  通过 `GET /api/conversations/{id}/stream` 订阅同一条 `ActiveRun`，先重放缓冲再收实时增量。
  真正的取消只来自显式 `POST /api/conversations/{id}/cancel`（或服务关闭）。

---

## 4. 契约详解

### 4.1 事件协议（唯一方言）

| 事件 | 载荷 | 用途 |
| --- | --- | --- |
| `run.started` | `payload.run_id` | 建立运行标识 |
| `message.delta` | `text` | 正文增量 |
| `thinking.delta` | `text` | 推理增量（如 `reasoning_content`、Claude thinking） |
| `tool.call` | `name`、`call_id`、`payload.input` | 工具调用 |
| `tool.result` | `call_id`、`payload.output`、`payload.is_error` | 工具结果 |
| `usage` | 任意数值键 | token / 费用 / 耗时 |
| `session` | `payload.session_id` | 智能体侧会话 id，落库以支持多轮 |
| `log` | `text`、`payload.level` | 命令回显、诊断信息 |
| `error` | `text` | 可展示的错误 |
| `run.finished` | `payload.status ∈ ok/error/cancelled` | 终态 |

框架额外产出两个传输层事件：`turn.started`（`runtime/service.start_run()` 在启动后台运行前同步发出，
携带 run_id 与消息 id）与 `stream.closed`（`transport/sse.py`，收尾）。**适配器不允许产出这两个。**

新增事件类型 = 改公共协议，必须同步四处：`events.py` → `service.consume_run` → 前端
`types.ts` + `applyEvent` → `docs/architecture.md`；`test_architecture.py` 会拦住漏改。

### 4.2 `AgentDefinition` 元数据

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` / `name` | ✅ | `id` 是稳定标识（落库、路由、配置都用它），改名视为破坏性变更 |
| `description` / `vendor` | 推荐 | 中文；展示在配置页 |
| `runtime` | ✅ | `Runtime.CLI` 或 `Runtime.API` |
| `capabilities` | ✅ | 见 4.3，声明式驱动框架行为 |
| `config_fields` | 视需要 | 见 4.4 |
| `api_key_env` / `base_url_env` | 视需要 | 走"自带网关/自带密钥"约定时声明 |
| `protocol` / `default_auth_style` | 视需要 | 官方链路的协议（openai / anthropic / gemini）与鉴权头风格 |
| `default_gateway` / `default_model` / `default_models` | 视需要 | 播种官方网关与新建智能体时的默认值（`default_models` 是官方链路的预置模型清单） |
| `executable` | CLI 必填 | 用于 `shutil.which` 探测是否安装 |
| `auto_provision` | 视需要 | 首次播种是否自动建实例（模板类设为 `False`，如 `custom-cli`） |
| `docs_url` | 可选 | 配置页的"查看文档"链接 |

### 4.3 `Capabilities` 语义（声明即生效）

| 能力 | 声明为 True 时框架会做什么 |
| --- | --- |
| `streaming` | 前端按增量渲染（关闭时按整段渲染） |
| `sessions` | 把 `session_id` 存到对话上，下一轮只发本轮 prompt；否则回放最近 N 轮 transcript |
| `native_system_prompt` | 人格作为原生 system prompt 传入；为 False 时框架把人格折进 prompt |
| `tools` / `permission_modes` | 仅作能力徽标展示与对应配置项渲染，并提示用户该适配器能用哪些选项 |
| `mcp` | 该内核支持 MCP：适配器**必须**实现 `discover_mcp_servers()` 与 `mcp_mount()`（有架构守卫测试）；框架按 `mcp_mount` 把智能体分配到的 MCP 服务器写进隔离目录 / 工作目录 |
| `skills` | 该内核支持技能：适配器**必须**实现 `skill_roots()` / `discover_skills()` / `skill_mount()` |
| `working_directory` | 为每个对话分配独立工作目录并作为 cwd |

可选钩子 `resume_supported(ctx)`：当"声明支持续接、但某些配置下退化为回放"时使用
（参考 `codex.py` 的 `resume_style`）。**声明与实际行为必须一致。**

### 4.4 `ConfigField` 与 `FieldType`

`FieldType ∈ {string, secret, number, boolean, select, textarea, keyvalue, string_list}`。
`secret` 会被前端渲染成密码框；`*_path`、`*_env` 这类高级项用 `advanced=True` 折叠。
用户提交的 options 经 `coerce_options()` 处理：按声明的 `default` 补值、按类型转换、**未知键透传**
（保证老版本前端提交的字段不会丢）。

> 关键约定：**新增可配置项只加 `ConfigField`。** 一旦为了某个智能体去改 `DynamicForm.tsx`，
> 就违反了"配置声明式"这条线。`DynamicForm` 只应该认识 `FieldType`，不应该认识任何 `agent.id`。

### 4.5 `AgentContext` / `AgentRun`

`AgentContext` 字段语义：`options`（合并后的配置）、`gateway_url`/`api_key`/`auth_style`/`model`
（已解析最终值，由智能体引用的网关与所选模型解析而来；官方默认时不注入地址与密钥）、
`credentials`（智能体自己的凭据解密后的「环境变量名 → 明文」，框架合并进子进程环境，
**适配器不得写进提示词 / 日志 / 事件**）、`agent_name`/`system_prompt`（人格）、
`workspace_dir`(cwd)、`session_id`（续接或 `None`）、`history`（回放用的历史轮次）、`prompt`（本轮输入）。
另有 `skills` / `mcp_servers`（框架按分配注入的技能与 MCP 服务器）与 `home_dir`（框架物化好的
内核隔离配置目录，技能与 MCP 共用同一个「家」）；MCP 的 `env` / `headers` 值可能含密钥，
**适配器不得写进提示词 / 日志 / 事件**。

`AgentRun` 提供：`state`（本次运行私有字典）、`emitted_text`/`thinking`/`tool_calls`（框架自动累积）、
`build_invocation()`、`feed(line)`，以及**结束时补发事件的钩子 `finish()`**。

**双向 stdin（常驻型 JSON-RPC 内核，例如 Codex app-server）**：适配器在 `build_invocation()` 里置
`Invocation.stdin_hold=True`（`stdin` 只写第一帧、写完不关），解析到应答后用 `AgentRun.send(line)`
排队下一帧、用 `AgentRun.mark_done()` 声明收工。**写入与关闭 stdin 一律由 `runner` 完成**
（`write_stdin` / `close_stdin`，写不动或超过上限就停止投递），适配器仍然只生产数据、不碰子进程 ——
取消、超时、凭据擦除因此都只有一份实现。服务端反向请求（审批 / 提问）没有人工确认入口时必须
显式应答（拒绝并告知用户），否则该轮会一直等一个不会到来的答复。

---

## 5. 数据模型与存储演进

- 关系链：`Gateway → AgentProfile → Conversation → Message`；技能经 `AgentSkill`、凭据经
  `AgentCredential`、MCP 服务器经 `AgentMcpServer` 挂到智能体上（三者都是「编目 / 全局一份 +
  智能体引用」：技能与各内核配置里的 MCP 来自扫描，平台通用 MCP 与凭据来自界面新建）。
- **多用户归属**：`users` 表存账号（口令只存 PBKDF2 哈希，`token_version` 用于吊销旧 JWT）；
  网关、智能体、凭据、对话、
  MCP 服务器、协作组与协作运行都有可空 `owner_id`。非管理员列表只回自己的行（网关额外包含
  官方链路、扫描来的 MCP 是只读共享目录），按 id 读取别人的行返回 404；跨用户引用必须在
  写入或启动运行前拒绝。管理员可看全部。鉴权开关默认开启，单用户部署可显式关闭。
- **Conversation 快照 `agent_profile_id`**：历史对话必须可复现，即使智能体之后改了内核 / 网关 / 模型。
- 外键 `ON DELETE RESTRICT`：仍被**未删除**的智能体引用的网关删除返回 409（避免历史断裂）；
  智能体与自定义网关本身用 `deleted_at` 软删除 —— 被历史引用时只打标记，从可选列表消失但不打断历史。
- `parts` / `usage` / `parameters` / `options` 用 JSON 列：智能体之间形态差异极大，不为其建表。
- 网关是 API 密钥的**唯一来源**：智能体只引用 `gateway_id`，模型必须取自网关的 `selected_models` 白名单。

**后加的可选列**：SQLite 的 `create_all` 不会给已存在的表加列，所以新列要登记在
`models.py`（对话域）、`collab/tables.py`（协作域）或 `im/tables.py`（IM 域）的
`ADDITIVE_COLUMNS`，由 `db.init_db` 幂等补齐。

**schema 变更规则**（本地库有真实数据）：
1. 只做**加列/加表**这类向后兼容变更，新列必须有默认值或可空。
2. 不允许"删库重建"；如需数据迁移，写一次性脚本并说明如何回滚。
3. 变更后在**现有** `backend/data/coda.db` 上启动一次，确认历史对话仍可读、可继续对话。
4. 生产环境当前用 `create_all`（无迁移框架）。若将来要频繁演进 schema，**先引入 Alembic**，
   而不是在 `init_db` 里堆手写 DDL。

---

## 6. 扩展决策树

```
我要做的事是什么？
├─ 支持一个新的智能体
│   ├─ 有官方 CLI，输出可解析/可探测
│   │     → 写 CLI 适配器（§6.1）
│   ├─ 没有 CLI，只有 OpenAI 兼容 /chat/completions
│   │     → 继承 OpenAIHarnessBase 写子类（§6.2）
│   └─ 私有 CLI，不想写代码
│         → 用内置「自定义 CLI」声明式配置（§6.3）
├─ 让某个智能体多一个可调参数
│     → 加一个 ConfigField（§6.4）
├─ 让某个适配器支持技能 / MCP
│     → 声明 `capabilities.skills` / `capabilities.mcp`，再实现扫描与物化钩子
│       （`discover_skills()` / `skill_mount()`，`discover_mcp_servers()` / `mcp_mount()`）。
│       会重名的一类技能（如各插件自带的 `access`）要按来源逐个声明 `SkillRoot` 并填
│       `namespace`，否则同名技能会在编目与物化时互相覆盖（详见 `AGENTS.md` §3.8）
├─ 让界面多展示一种内容（例如"计划"）
│     → 新增规范化事件（§6.5）
├─ 换存储 / 换传输
│     → 只动 db.py+settings（存储）或 transport/sse.py+前端 streamPost（传输）
├─ 要在对话里生成图片
│     → 用平台自带的「图片生成」MCP 服务器（scripts/mcp/image_mcp_server.py）：启动时已自动播种，
│       在「MCP」页编辑它，填 IMAGE_GEN_GATEWAY / IMAGE_GEN_MODEL / IMAGE_GEN_API_KEY 三个环境变量（加密落库），
│       再到「智能体 → MCP」里分配 —— 不改框架任何一层，见 docs/architecture.md §6
├─ 新增一个平台内置 MCP 服务器
│     → 在 app/builtin_mcp.py 的 BUILTIN_SERVERS 加一条声明（脚本随 Release / Docker 打包）；
│       需要平台自动填值的参数用 managed=True + managed_source 声明，不要改 runtime/mcp.py
└─ 其它（智能体、网关、对话、页面、样式）
      → 按 §2.1 分层表定位到唯一一层，不要在多层同时改
```

### 6.1 新增 CLI 适配器骨架

```python
"""<厂商> <产品> CLI 适配器。"""
from __future__ import annotations

from typing import Any, Iterable

from ..base import (AgentContext, AgentDefinition, AgentRun, Capabilities,
                    ConfigField, FieldType, Invocation, Runtime)
from ..events import AgentEvent, log, session, text_delta, usage
from ..registry import register_agent
from .util import as_list, flatten_content, truthy


@register_agent
class AcmeAgent(AgentDefinition):
    id = "acme"
    name = "Acme CLI"
    vendor = "Acme"
    description = "一句话中文说明。"
    runtime = Runtime.CLI
    executable = "acme"
    api_key_env = "ACME_API_KEY"
    base_url_env = "ACME_BASE_URL"
    default_gateway = "https://api.acme.dev/v1"
    default_model = "acme-pro"
    logo = "/icons/kernels/acme.webp"  # 官方 Logo；真彩色位图按原色显示
    logo_style = "image"              # 单色 SVG 则留空（默认 mask，当遮罩染色）
    capabilities = Capabilities(sessions=True, native_system_prompt=True, tools=True,
                               working_directory=True)
    config_fields = (
        ConfigField("mode", "运行模式", FieldType.SELECT, default="auto",
                    options=[{"value": "auto", "label": "自动"},
                             {"value": "safe", "label": "安全"}]),
    )

    def build_invocation(self, ctx: AgentContext) -> Invocation: ...
    def parse_line(self, line: str, run: AgentRun) -> Iterable[AgentEvent]: ...
```

要求：`parse_line` 必须容忍脏行（解析失败 → `log(...)` 或忽略，不得抛出导致整轮失败）；
需要的解析状态放 `run.state`；结束时要补发事件就覆盖 `AgentRun.finish()`。
Logo 是**适配器声明的静态资源**（`frontend/public/icons/kernels/*`）：
默认 `logo_style="mask"`，24×24 单色 SVG、只画形状，界面当遮罩染成当前文字色，
深色 / 浅色共用一份；有真彩色品牌图的用 `logo_style="image"`，PNG/WebP（透明底、
≤512×512）按原色显示。没有官方图形的自备内核用中性线条图形。
**不要**在前端按 `type_id` 写 Logo 映射（铁律 1）。
新增图形后跑一次 `python3 scripts/gen_kernel_logos.py`：界面走的是**内嵌**的图形
（`frontend/src/lib/kernelLogos.ts`，运行期不读外部路径），不重跑就还是旧图形。

### 6.2 新增 API 型供应商

只写子类，复用工具循环：

```python
@register_agent
class AcmeHarness(OpenAIHarnessBase):
    id = "acme-harness"
    name = "Acme 内核"
    vendor = "Acme"
    description = "……"
    api_key_env = "ACME_API_KEY"
    base_url_env = "ACME_BASE_URL"
    default_gateway = "https://api.acme.dev/v1"
    default_model = "acme-chat"
    logo = "/icons/kernels/acme.webp"
    logo_style = "image"
```

`OpenAIHarnessBase` 已提供：流式补全、工具循环、`extra_headers`/`extra_body`、
工作目录沙箱工具（`harness_tools.WorkspaceTools`）。**不要重写一遍。**

### 6.3 零代码接入

用 `custom-cli`：填 `command`、`args`（支持 `{prompt} {model} {gateway} {api_key} {workspace} {session} {agent}`
占位符）、`prompt_mode`、`output_format`、以及 `text_path/tool_path/..._path` JSON 取值路径。
只有在"声明式表达不了"时才升级为写适配器。

### 6.4 新增配置项
加 `ConfigField` → 前端自动出现。同时检查 `bootstrap.py` 的播种是否受影响（正常情况下不受影响，
因为 `coerce_options` 会用 `default` 补齐）。

### 6.5 新增事件类型
按 §4.1 的四处 + 文档同步；并补一个"事件 → 前端渲染"的测试或手测用例。

### 6.6 新增前端页面
`src/pages/XxxPage.tsx` + 在 `src/App.tsx` 的 `NAV`/`Routes` 注册 + 类型进 `src/lib/types.ts`
+ 接口进 `src/lib/api.ts` + 样式进 `src/index.css`（复用既有 class）。

---

## 7. 面向未来：如何把新智能体接进来

架构已经为下列方向预留了位置，接入时**不应**触碰 `api/`、`frontend/`、`models.py`：

| 目标 | 现状 | 接入方式 |
| --- | --- | --- |
| Gemini CLI | 已有 `gemini_cli.py`（`GEMINI_SYSTEM_MD` 注入人格、`--resume` 续接） | 已接入；新版本差异用 `ConfigField` 暴露开关 |
| 任意 OpenAI 兼容端点 | 已有 `openai_harness.py`（OpenRouter/vLLM/Ollama/Kimi/GLM） | 新供应商 = 一个子类 |
| 未来的 Aider / Cline / Continue 等 CLI | 无适配器 | 按 §6.1 写模块；或先用 §6.3 |
| 非 OpenAI 协议的 API（如 Anthropic Messages API） | 复用 `OpenAIHarnessBase` 不合适 | 以 `Runtime.API` 直接实现 `async stream(ctx)`，产出标准事件即可 |
| 需要"多智能体协作/编排" | 已有独立协作域 `app/collab/`（可视化编排 + 工单协议 + 波次并行引擎，默认串行），见 `docs/collaboration-design.md` | 在 `app/collab/` 内扩展；每一步仍复用 `runtime.runner` 与事件协议，不要改适配器，也不要让框架层反向依赖协作域 |

判断标准始终是：**这次改动是否只落在 `agents/builtin/` 一个文件里？** 如果能，就说明架构还健康。

---

## 8. 前端规范

- 请求必须走 `src/lib/api.ts`；组件里禁止裸 `fetch`。
- 类型集中在 `src/lib/types.ts`，字段名与后端 `schemas.py` 保持一致。
- 表单统一用 `DynamicForm`（读 `config_fields`）；消息统一用 `MessageView`（读 `MessagePart`）。
- **对话只有一份展示模块**：所有对话（单聊、协作主对话、协作岗位实时对话，含移动端）都渲染
  `ConversationThread`（内部只用 `MessageView` 画气泡）；岗位线程的装配逻辑只有
  `CollabAgentThread` 一份。禁止再写第二套气泡 / 线程布局。
- 事件并入状态统一在 `src/lib/agentStream.ts` 的 `applyAgentEvent()`（对话页与协作页共用这一份），
  新增事件类型在这里加一个 `case`。
- 无 UI 框架 / 无状态管理库；CSS 手写，沿用 `card`、`btn`、`pill`、`banner`、`grid` 等既有 class。
- 面向用户的文案中文；错误展示沿用 `describe(error)`。
- **移动端（`/m/`）是独立入口，不是桌面端的窄屏适配**：入口 `frontend/m/index.html` + `src/mobile/`，
  有自己的路由（`MobileApp.tsx`）、样式表（`mobile.css`）、左侧抽屉与底部弹层。桌面端页面 / 入口 /
  样式表与移动端**互不引用**（守卫见 `backend/tests/test_mobile_web.py`）；两端只共享 `lib/*` 与
  `DynamicForm` / `MessageView` / `MarkdownView` / `ConversationThread` / `CollabAgentThread` /
  `TicketBoardKit` / `Composer` / `Icons` / `McpPicker` / `McpServerForm` / `McpAssignPanel` /
  `AuthGate` / `LoginScreen` / `AccountPanel` / `UserAdminPanel` / `TeamAvatarStack` /
  `ConversationExportForm`
  等纯展示组件，以及 `lib/agentStream` 里的事件归约。共享组件的样式必须两端样式表各写一份
  （`account-*` / `auth-*` 就是例子）。
- 移动端原语沿用既有类名（`btn` / `pill` / `banner` / `field` / `input` / `card` / `grid` /
  `card-section-title`…——定义在 `mobile.css`，因为移动端不加载 `index.css`）；只有移动端专有结构才用
  `m-` 前缀（`m-drawer` / `m-sheet` / `m-item` / `m-thread`…）。
- **PWA 资产只放 `frontend/public/m/`**（`manifest.webmanifest` + `sw.js`）：`scope`/`start_url` 固定
  `/m/`，`/api/`（含 SSE 长连接）**永不进入缓存**。新增页面要保证断网时应用外壳仍能打开（离线只保证
  「界面能打开」，对话仍需联网）。
- **朗读（`src/lib/tts.ts`）只有一份实现**：开关默认关闭、存在 localStorage，开启后念的是
  Markdown 转出来的纯文本；对话页顶栏右上角只有「导出 + 朗读」两个按钮。
- **「操作图标」用线条 SVG**（`src/components/Icons.tsx`，两端共用）：输入区、顶栏这类
  需要与文字同色的小图标，emoji 的尺寸与颜色随系统而变、同一排对不齐；这类图标一律
  `stroke="currentColor"`，尺寸走 `size` 参数。
  **左侧主导航（桌面侧栏 + 移动抽屉）也是线条 SVG**：`NAV[].icon` 存 `NavIconName` key
  （`'chat' | 'agents' | 'collab' | 'skills' | 'mcp' | 'usage'`），图形统一由 `NavIcon` 按 key 取；
  两端菜单各自写字面量、由 `tests/test_mobile_web.py::test_main_nav_is_the_same_menu_on_both_ends`
  比对一致，加菜单项要同时改两处 `NAV` 与 `Icons.tsx` 的映射表。
- **安装归「内核管理」，配置归「智能体」**：内核的安装 / 更新 / 卸载在「设置 → 内核管理」
  （两端都有，桌面端是设置里的标签页）；智能体（内核 + 网关 + 模型 + 人格 + 技能）有自己的配置页
  —— 移动端是 `mobile/pages/MobileAgentsPage.tsx`（有守卫断言它必须存在），**不要**再把「实例配置」
  塞回内核页（`MobileAgentSettings.tsx` 已取消：密钥归网关）。
- **外观（深色 / 浅色 / 跟随系统）只有一份实现**（`src/lib/theme.ts`）：**没设置过默认「跟随系统」**，
  解析结果写在 `<html data-theme>`，两端样式表各自的 `:root` 是深色兜底（脚本未执行时用）、浅色只加
  `[data-theme='light']` 覆盖块。**界面里不要写死深色**：先加 CSS 变量、再补浅色值；两端都要用到的新配色，
  两份样式表一起加（共享段还要逐字一致，见 `backend/tests/test_mobile_web.py`）。原生外壳的配色由
  `applyTheme()` 同步 `meta[name=theme-color]`，别在别处硬编码主题色。
- **输入区只有一份**：`src/components/Composer.tsx`（受控：草稿 / 附件 / 上传 /
  发送都由页面给），两端的对话页与协作运行页共用；不要再各写一个输入框。
  `src/components/Icons.tsx` 同理：线条图标两端共用一份。样式是**组件一份、样式表各一份**
  （共享段逐字一致，守卫见 `tests/test_mobile_web.py::test_composer_styles_are_identical_on_both_ends`；
  两端的尺寸微调写在共享段之后）。**回复期间继续输入**用 `queueWhenBusy`：两端对话页都开着它，
  框里有内容时右侧按钮是「排队发送」（点一下交给页面入队），空着时才是「停止」；队列与
  「本轮结束自动发出」的逻辑属于页面（`ChatPage` / `MobileChatPage`），组件只提供开关。
- **移动端锁定缩放**：它是「固定外壳」的应用形态，入口视口写 `maximum-scale=1.0, user-scalable=no`，
  并在 `mobile.css` 用 `touch-action: pan-x pan-y` 兜底（iOS Safari 忽略 `user-scalable=no`）；
  因此移动端输入框字号必须 ≥ 16px（iOS 聚焦小于 16px 的输入框会强制放大页面）。桌面端不禁用缩放。
- **前端依赖只允许这两个**：`react-markdown@10.1.0` 与 `remark-gfm@4.0.1`（精确版本）。理由：各内核的
  产出以 Markdown 为主（代码块、表格、列表、任务清单），此前用 `white-space: pre-wrap` 直出不可读；
  手写解析器等于重复造轮子，且转义/边界极易写错。为此新增 `components/MarkdownView.tsx`
  （统一代码块/表格/列表样式、复用既有 CSS 变量，并显式 `skipHtml` —— 不渲染原始 HTML，避免注入），
  `MessageView` 的文本段统一经它渲染。**除这两个库外不得再加前端依赖**；确实需要时先在本文件写明理由。

---

## 9. 维护规则

- **版本**：只在公开发版时改 `backend/app/__init__.py` 的 `__version__`（唯一版本源，前端从
  `/api/system/overview` 读取展示）。日常提交不动版本号，也不往 `VERSION_HISTORY` 追加条目。
- **依赖升级**：后端 `requirements*.txt` 用精确版本号；前端升级后必须 `npm run build` 通过。
  升级 FastAPI / Pydantic / SQLModel 大版本时，重点回归：SSE 流、外键 RESTRICT、JSON 列的读写。
- **弃用**：要下线一个配置项或事件类型，先在文档里标注，再在一个版本内保留兼容读取，
  最后删除；不要在同一次改动里既删旧又加新。
- **文档同步**：改了事件协议 → 改 `architecture.md`；改了分层/扩展方式 → 改本文件与 `AGENTS.md`。
- **不要引入**未被要求的中间件、DI 框架、日志框架、ORM 迁移工具；先写进文档讨论，再动手。

---

## 10. 评审 / 自检清单

改动提交前逐条对照（AI 与人都适用）：

- [ ] 这次改动落在**哪一层**？有没有越层或反向依赖？
- [ ] 有没有在框架层写 `if type_id == ...`？（守卫测试会拦）
- [ ] 新增的配置项是否用 `ConfigField` 声明，而不是改前端？
- [ ] 如果动了事件协议，四处是否同步？
- [ ] 凭据有没有可能进入日志 / SSE / 接口响应？命令行回显是否被 `_redact` 覆盖？
- [ ] 新增的智能体密钥是不是放在了 `credentials`（工具用）而不是网关（模型用）？值是否只经
      `security`、只以环境变量注入、事件是否过了 `scrub_event`？
- [ ] 多用户相关改动：列表 / 详情 / 写入 / 运行前是否都做了 `owner_id` 校验？共享目录
      （官方网关、扫描来的 MCP）是否在列表与详情里口径一致？
- [ ] 口令 / 令牌：改口令或管理员重置口令的入口是否递增 `users.token_version`（旧 JWT 必须立即失效）？
      令牌是否只含 `sub/role/ver/iat/exp`，明文口令是否从不入库 / 进日志 / 进响应？
- [ ] 收紧为管理员接口后，前端是否同步隐藏/禁用入口，且没有在页面加载时自动调用？
- [ ] 错误是否显式暴露（HTTPException 或 `error` 事件），而不是被吞掉？
- [ ] 长任务是否响应取消与超时？
- [ ] 中文规范：界面文案、注释、docstring 是否都是中文？专有名词是否保留？
- [ ] 移动端改动是否守住了物理隔离（不引用 `src/pages/`、`App.tsx`、`index.css`），且没有把内核专有知识（技能目录、配置文件路径）写进前端？
- [ ] PWA 相关改动是否仍然**不缓存 `/api/`**（含 SSE）？
- [ ] `bash scripts/test.sh` 是否全绿？新增能力是否有对应测试？
- [ ] 前端改动是否 `npm run build` 且重启了服务？
- [ ] schema 变更是否向后兼容，并在**现有数据库**上验证过？

---

## 11. 已知技术债（改到相关代码时顺手清理，但别扩大范围）

| 位置 | 问题 | 处理建议 |
| --- | --- | --- |
| 无迁移框架 | `init_db()` 用 `metadata.create_all`，无法处理列变更 | 频繁演进 schema 前引入 Alembic |
| `AgentDefinition.stream` | 基类用 `NotImplementedError`，靠 `Runtime.API` 分支兜底 | 可接受；新增 API 适配器务必实现 `stream()` |

---

## 12. 快速命令

```bash
bash scripts/test.sh                                  # pytest + 前端 tsc（含架构守卫）
cd frontend && npm run build                          # 改前端后必须构建
bash scripts/serve.sh                                 # 生产：0.0.0.0:8010
bash scripts/dev.sh                                   # 开发：API :8000 + Web :5173
curl -s http://127.0.0.1:8010/api/health              # 线上自检
curl -s http://127.0.0.1:8010/api/agent-types | python3 -m json.tool | head -40
```
