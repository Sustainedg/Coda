# 架构说明

## 1. 分层的目标

这个系统要长期承接「越来越多智能体」。因此把**易变**的部分（各种智能体的调用方式与输出格式）
压缩到一个极薄的插件层，把**稳定**的部分（流式传输、会话记忆、持久化、UI 表单、取消与超时）
全部沉淀到框架里。

```
             ┌────────────────────────── 稳定层（很少改） ──────────────────────────┐
 HTTP/SSE ──→ api/ ──→ runtime/service.py ──→ runtime/runner.py ──→ 规范化事件 ──→ 前端
                            │                       │
                            │                       └──→ agents/builtin/*   ← 易变层（唯一扩展点）
                            └──→ SQLModel / SQLite
             └──────────────────────────────────────────────────────────────────────┘
```

## 2. 一次运行的完整时序

```
前端  POST /api/conversations/{id}/messages  {content, attachments}
 │
 ├─ api/conversations.send_message()
 │    └─ service.prepare_run()
 │         ├─ 载入 Conversation → AgentProfile → AgentDefinition（+ Gateway 提供密钥/地址）
 │         ├─ 智能体技能：agent_skills → skills 表 → 物化到 agent-homes/<对话>/<内核配置目录>
 │         ├─ 智能体 MCP：agent_mcp_servers → mcp_servers 表 → 解密后按内核形状写进同一个隔离目录
 │         ├─ capability 决策：
 │         │    支持 session  → 只发本轮 prompt，靠智能体自己的记忆续接；
 │         │                   断档那轮（换智能体/目录、重置记忆）用最近几轮最终回复兜底，不失忆
 │         │    不支持 session → 回放数据库里的最近 N 轮（transcript）
 │         │    无原生人格   → 把 system prompt 折进 prompt
 │         ├─ @ 召唤（可选）：消息带 summon_profile_id 时，这一轮换被召唤的智能体作答
 │         │    —— 共享对话历史（最终回复前置上下文），但不续接主场会话、不共用隔离目录，
 │         │       技能 / MCP 按召唤者自己的分配物化到一次性 summon- 目录；归属与记忆不动
 │         ├─ 附件（可选）：先 POST /{id}/attachments 存进工作目录，这里把相对路径并进 prompt
 │         ├─ 落库 user message + 占位 assistant message(status=streaming)
 │         ├─ 自动为首轮对话生成标题
 │         └─ registry.register() 生成 run_id（供取消/追踪）
 │
 ├─ 返回 StreamingResponse(text/event-stream)
 │    └─ 后台任务 service._execute_prepared()（与 HTTP 连接解耦）
 │         └─ service.consume_run()
 │              └─ runner.stream_run()
 │              ├─ Runtime.CLI → create_subprocess_exec(argv, env, cwd, stdin)
 │              │     ├─ 逐行读取 stdout（自建行缓冲，不受 asyncio 64 KiB 上限约束）
 │              │     │     → definition.parse_line() → 规范化事件
 │              │     ├─ 物化 Invocation.files（如 gemini 的系统提示词文件）
 │              │     ├─ 每秒检查取消信号 / 全局超时
 │              │     └─ stderr 汇总，非 0 退出时作为 error 事件上报
 │              └─ Runtime.API → definition.stream(ctx)
 │                    └─ 流式 chat/completions + 工具循环（工具在工作目录内执行）
 │
 └─ 每个事件：既 publish 给当前订阅者，也在 consume_run 里被累积
      （文本、推理、工具调用/结果、usage、session_id）
    finally 块无论成功/失败/取消都会写回 assistant message 并 commit
```

**执行与连接解耦**：`send_message` 在 `prepare_run` 之后用 `service.start_run()` 把这一轮丢进
后台任务（`ActiveRun` 持有事件缓冲与订阅者队列），HTTP 的 SSE 只负责订阅。客户端刷新 /
关页面只会让订阅者离开，**不会** kill 子进程或中断这一轮；重新打开对话时，详情里的
`active_run` 会让前端改为订阅 `GET /api/conversations/{id}/stream`，先重放缓冲、再收实时增量
（另一台设备同样如此）。快照里的 `agent_name` / `agent_profile_id` / `agent_avatar` 描述的是
**本轮作答者**：@ 召唤轮是被召唤的智能体，不是对话归属的那一个 —— 重连时实时卡片据此显示
正确的名字与头像。真正取消只走 `POST /api/conversations/{id}/cancel`。

CLI 的 stdout 由 `runner._StreamLines` 拆行：`asyncio.StreamReader.readline()` 在单行超过 64 KiB
缓冲上限时会抛 `ValueError`（「Separator is found, but chunk is longer than limit」），而内核常常把整段代码
或工具结果塞进一行 JSONL，所以框架层不能把行长当成已知量。超过 64 MiB 的单行只丢该行并补一条
`log(warn)`，不会把整轮运行判成 error（铁律 10 / 11）。

**协作域**（`app/collab/`，见 `docs/collaboration-design.md`）是第二个编排器：它**不重写执行链路**，
每一步仍然调用 `runtime.runner.stream_run()`，因此取消、超时、脱敏都复用同一套实现。
单步限时不走全局默认：团队预算的 `budget.step_timeout_seconds`（默认 36000 秒，`0 = 不限`）经
`execution.prepare_step()` / `prepare_chat()` 落成 `handle.timeout_seconds`，其中 0 转成 -1
（runner 的「负数 = 不限」），再交给 `stream_run()`；全局 `run_timeout_seconds` 只服务对话域，
且**默认为空 = 不限时**——普通对话不该被平台级硬时限掐断，需要护栏的部署自行配置。
区别只在编排与记忆：

```
collab.engine.consume_turn()  ── 取就绪工单跑一步（max_parallel>1 时同父兄弟工单同波并行）
      └─ collab.execution.prepare_step()   # 全域唯一接触 runtime/agents 的边界
           ├─ ctx.history = []、ctx.session_id = None   ← 每一步都是全新会话，不共享记忆
           └─ ctx.prompt = prompt.build_step_prompt(...) # 只含工单字段与 gist 摘要
      └─ runner.stream_run(ctx) → AgentEvent → 累积 → 解析 work_order → 更新工单 → 交给下一个岗位
      └─ 根工单收尾 → 交回入口智能体做一次收尾汇报（附全流程工单汇总）→ 结论回到用户那条主会话
      └─ 入口智能体聊天（并行）→ prepare_chat() → 只读工单快照 + 最近聊天（+ 附件路径）→ 独立 chat_bus 推给用户
```

协作域使用自有的信封协议 `collab.*`（`app/collab/protocol.py`），与下面的智能体事件协议**分属两层**，
互不干扰；因此新增协作事件**不需要**改 `agents/events.py` / `consume_run` / `applyEvent`。

## 3. 规范化事件协议

| 事件 | 载荷 | 用途 |
| --- | --- | --- |
| `run.started` | `payload.run_id` | 建立运行标识 |
| `message.delta` | `text` | 正文增量 |
| `thinking.delta` | `text` | 推理增量（如 `reasoning_content`、Claude thinking） |
| `tool.call` | `name`, `call_id`, `payload.input` | 工具调用 |
| `tool.result` | `call_id`, `payload.output`, `payload.is_error` | 工具结果 |
| `usage` | 任意数值 | 整轮累计的 token / 费用 / 耗时（计费口径） |
| `context.usage` | `payload.tokens` | **单次请求**的输入 token（上下文窗口占用，落 `messages.context_tokens`） |
| `context.compacted` | `payload.trigger/pre_tokens/post_tokens/messages_summarized…` | 内核侧**原生压缩**通知（如 Claude Code auto-compact）；运行层发日志提醒并使压缩前的快照失效 |
| `session` | `payload.session_id` | 智能体侧会话 id，落库以支持多轮 |
| `log` | `text`, `payload.level` | 命令回显、诊断信息 |
| `error` | `text` | 可展示的错误 |
| `run.finished` | `payload.status` ∈ ok/error/cancelled | 终态 |

前端 `applyEvent()` 与后端 `consume_run()` 都只依赖这张表。

> 协作运行另有一层 `collab.*` 信封（`collab.run.started` / `collab.step.started` / `collab.step.event`
> / `collab.step.finished` / `collab.ticket.created` / `collab.ticket.updated` / `collab.run.status`
> / `collab.notice` / `collab.chat.started` / `collab.chat.event` / `collab.chat.finished`），
> 它包住原始 `AgentEvent` 后发给协作页；**不是**对这张表的扩展。
>
> 「内核管理」页（设置 → 内核管理）的安装 / 更新 / 卸载也走自有信封（`provision.started` / `provision.output` /
> `provision.qr` / `provision.input` / `provision.finished`），由 `runtime/provisioning.py` 产出，
> 同样**不属于**这张事件表。

## 4. 适配器的契约

```python
class AgentDefinition(ABC):
    id / name / vendor / runtime / capabilities / config_fields   # 元数据（驱动 UI）
    api_key_env / base_url_env / default_gateway / default_model  # BYO gateway 约定
    protocol / default_auth_style / default_models                 # 官方链路与自定义网关的匹配依据
    executable                                                    # 安装探测

    def availability() -> Availability          # 依赖 shutil.which + --version
    def coerce_options(raw) -> dict             # 按 config_fields 校验/补默认值
    def effective_env(ctx) -> dict              # 凭据 → 框架约定的环境变量
    def system_defaults() -> dict               # 读取本机真实配置（网关/模型/凭据）
    def resolve_gateway/resolve_model(value)    # 「未自定义」时回落到本机真实配置

    def build_invocation(ctx) -> Invocation      # CLI：argv/env/cwd/stdin/files
    def parse_line(line, run) -> Iterable[Event] # CLI：逐行解析（run.state 存放解析状态）
    async def stream(ctx) -> AsyncIterator[Event]# API：进程内直接产事件

    def skill_roots() -> list[SkillRoot]         # 技能：本内核的技能根（路径 + 来源 + 是否可安装）
    def discover_skills() -> list[SkillSpec]     # 技能：扫描上面这些根（默认由框架 scan_skill_roots 完成）
    def skill_mount(ctx) -> SkillMount | None    # 技能：把分配到的技能物化到哪里

    def discover_mcp_servers() -> list[McpServerSpec]  # MCP：只读扫描本机已有的 MCP 配置
    def mcp_mount(ctx) -> McpMount | None              # MCP：把分配到的服务器写到哪里

    logo / logo_style                             # 官方 Logo；mask=单色 SVG 遮罩染色，image=真彩色位图原色显示
    provisioner                                   # 运行时：如何安装 / 更新 / 卸载本内核
```

`capabilities` 决定了平台在框架层面**如何**照顾这个智能体：

- `sessions=True` → 平台把 `session_id` 存到对话上，下一轮只发增量；没有可续接会话的那一轮
  （新对话、刚换智能体 / 工作目录、手动重置记忆、隔离目录重建），平台把库里最近几轮的
  **最终回复**（`_clip_history` 的产物，不含工具调用与推理过程）作为前置上下文拼进 prompt，
  一旦本轮建立新的内核会话，下一轮自动恢复原生续接 —— 换智能体不再失忆。
- `native_system_prompt=False` → 平台把人格折进 prompt（或按适配器自己的方式注入）。
- `working_directory=True` → 为每个对话分配独立工作目录作为 cwd。
- `permission_modes / tools` → 供前端展示能力徽标，并按需渲染对应配置项。
- `mcp=True` → 该内核支持 MCP：适配器**必须**实现 `discover_mcp_servers()` 与 `mcp_mount()`
  （有架构守卫测试）；框架据 `mcp_mount` 把智能体分配到的 MCP 服务器写进隔离目录。

`resume_supported(ctx)` 是可选钩子：Codex 老版本没有 `exec resume`，适配器可以在
选项里让用户切换成「回放 transcript」，框架会据此走另一条分支。

`system_defaults()` 是可选钩子：需要对齐 CLI 自身系统级配置的适配器可以覆盖它，
从本机读取真实默认值（Claude Code 读 `~/.claude/settings*.json` 的 `env` 与 `model`，
进程环境变量优先）。框架在新建智能体、展示配置与构建运行上下文时，会把「仍等于内置
默认值」的网关/模型视为未自定义，回落到这份真实配置；用户显式填写过的值则保持不变。

### 4.1 运行时管理（安装 / 更新 / 卸载）

「运行时」= 某个 CLI 适配器依赖的**本机可执行文件**（界面上这一栏叫「内核管理」——
「运行时」是代码里的说法，`runtime/` / `provisioner` / `/api/runtimes` 这些标识符都没改）。
「设置 → 内核管理」的列表卡片只留
**官方 Logo + 内核名 + 所属公司 + 当前版本 + 状态**，**点卡片进详情**（安装 / 更新 / 卸载 /
检查更新与安装日志都在详情里）：

- **Logo 由适配器声明**（`AgentDefinition.logo` 指向 `/icons/kernels/*`）。默认
  `logo_style="mask"`：前端用 `components/KernelLogo.tsx` 把单色 SVG 当遮罩染成当前文字色，
  深浅两套外观共用一份图形；`logo_style="image"` 时按原色渲染真彩色位图（PNG/WebP，透明底），
  品牌图标不再被染成剪影。前端**不**按 `type_id` 写映射（铁律 1）。
- **图标是内嵌的**（图标秒显）：`scripts/gen_kernel_logos.py` 把 `public/icons/kernels/*`
  的图形在构建期内嵌进 `frontend/src/lib/kernelLogos.ts`，界面通过
  `lib/kernelIcons.ts` 取地址 —— 运行期**不从外部路径读图形**，也没有图标的网络请求。
  原因：图标原本由后端的 SPA 兜底路由返回（带 `Cache-Control: no-cache`），每次进页面
  都要重新校验一遍；更要命的是管理员的「检查更新」（`npm view`）会把浏览器每域名的
  连接数占满，图标请求排在后面，实测要等 1.6～2.5 秒才画出来。内嵌后零请求、零排队。
  图形本身仍是手工 / 厂商产物（不归生成脚本管）；改了图形要重跑一次该脚本，
  `tests/test_frontend_assets.py` 会核对清单与文件一致。
  计时：页内埋点在 `window.__kernelLogoPerf`（`lib/kernelIcons.ts`），
  复测用 `node scripts/measure_kernel_icons.mjs <地址> 10`（要本机有 chromium）；
  完整的验证记录见 `docs/kernel-icon-perf.md`。
- **账号型内核可以登录**：适配器声明 `RuntimeProvisioner.login_argv` 与
  `AgentDefinition.auth_status()`，详情弹层据此显示「已登录 / 未登录」和「登录」按钮。
  登录命令按通用约定输出 `QR_IMAGE=<图片路径>`，框架读出图片（校验 PNG/JPEG/WebP 与大小）
  后以 `provision.qr` 事件推给前端；登录结束后重新调一次 `auth_status()` 收尾。
  需要把浏览器里的授权码贴回来的 OAuth 内核（如 Antigravity CLI）再输出一行
  `INPUT_PROMPT=<提示文本>`：框架据此产出 `provision.input` 事件并保持登录子进程的 stdin
  打开，界面弹出的输入框经 `POST /runtimes/{id}/login/input` 把文本写回子进程。
  只能交互式登录、没有可代跑子命令、也没有可编程登录入口的内核**不要声明
  `login_argv`**：`GET /api/runtimes` 会带 `auth_supported=true`，前端只展示登录状态，
  不显示一个点了必然失败的「登录」按钮。如果内核提供了本地 HTTP 登录 API / OAuth 设备码
  之类可编程入口，适配器可以写一个登录助手脚本（仍按 `QR_IMAGE=` 协议输出二维码，
  例如 CodeBuddy 的 `codebuddy_login.py`），框架不因此认识具体内核。
- **多账号**：适配器在 `auth_status()` 返回 `accounts`（`RuntimeAccount` 列表），并覆盖
  `activate_account()` / `remove_account()`，框架就提供 `POST /runtimes/{id}/accounts/{账号}/activate`
  与 `DELETE /runtimes/{id}/accounts/{账号}`；详情弹层据此渲染「切换 / 删除」。
  槽位怎么存（文件快照 / 独立 HOME / 内核自己的多账号）是适配器的知识，框架只做接口。
  智能体可以按 `AgentProfile.account_id` 指定用哪个槽位：只在「官方默认」链路生效（自定义网关
  自带地址与密钥，不看内核登录态），对话 / 协作 / 重跑等运行启动前由运行层切换一次；切换失败
  只告警、不阻断运行。自动起标题**不切号**：指定了非当前登录账号时直接跳过这次标题生成
  （后台任务不该动整台机器的登录态）。这是机器级设置，只有管理员能指定（普通成员编辑时原样
  带回不算修改）。
- **已经是最新版就不给「更新」按钮**：界面先查一次最新版本，只有确实更高才显示
  「更新到 <版本>」；查不到（该适配器没声明 `latest_argv`、查询失败或还没查）时保留普通
  「更新」，免得用户失去升级手段。

和技能一样，**「用哪个包管理器、包名叫什么、装到哪儿」是适配器的知识**：适配器声明
`provisioner = RuntimeProvisioner(manager=…, package=…, install_argv=…, update_argv=…,
uninstall_argv=…, latest_argv=…, prepare_argv=…, login_argv=…)`，框架（`runtime/provisioning.py`）只负责按声明执行：

```
适配器声明命令 ──▶ runtime/provisioning.stream_provision() ──▶ 受控子进程
                                                              ├─ 动作前先跑 prepare_argv（清扫残留，失败只告警）
                                                              ├─ 逐行流式回显（SSE provision.output）
                                                              ├─ 总超时（settings.provision_timeout_seconds）
                                                              ├─ 命令脱敏（runner._redact）
                                                              └─ 客户端断开即 kill（同一次运行的取消语义）
```

- **动作前先清扫（`prepare_argv`）**：上一次动作被中途打断（超时、服务重启）可能在本机
  留下让包管理器再也无法覆写安装目录的残留 —— 典型是 npm 的中转目录（卸载 / 覆盖安装前
  先把旧目录 rename 成 `.<包名>-<安装路径哈希>` 再删掉），而中转名只由绝对路径决定，
  于是之后**每一次**安装 / 更新 / 卸载都会永久撞上 `ENOTEMPTY`。适配器声明这条清扫命令
  （内置的 npm 内核用 `builtin/util.py:npm_prepare_argv()` 生成），框架在同一次动作的
  最前面跑一遍并回显它的输出；清扫与动作共用同一个总时限，清扫自己失败只回一行告警、
  **不**中断动作 —— 成败始终只由动作自己的退出码决定。

- **安装 / 更新还要自己处理「本机既有的同名可执行文件」**：`<npm 前缀>/bin/<命令>` 可能
  已经被别的安装占着（系统里另一个 node 装的同一内核、用户手写的垫片），而 npm 只肯覆盖
  **指向自己安装目录**的链接，否则整个动作直接失败在 `EEXIST`（连包都不装）。所以 npm 内核的
  `install_argv` / `update_argv` 由 `builtin/util.py:npm_install_argv()` 生成：先判断那个占位
  是不是自己的（解析后落在 npm 全局目录里就不动它），不是就挪到一个隐藏备份名
  （`.<命令>.coda-bak`）让出位置，npm 失败再把备份放回原位 —— 不留「命令没了、包也没装上」
  的半截状态。包装命令顺带负责调用 npm，所以它天然只挂在安装 / 更新上；卸载不建链接，
  `uninstall_argv` 保持原样，确认框里显示的就是它。

**安装状态与版本是缓存出来的，不是每次打开页面现探的**：探测要起一次 `内核 --version`
子进程（gemini 这类 Node CLI 冷启动接近 2 秒），而后端是单进程单事件循环 —— 同步探测会
把**整站所有接口**一起冻住（实测开网页时并发的读数请求被拖到 20 秒）。因此
`runtime/availability.py` 采用与技能编目同构的「编目 + 定时刷新」：

```
读接口 ──▶ runtime/availability.statuses() ──▶ runtime_status 表（只读，缺行时只查 PATH）
                     │ 未命中 / 过期 → 登记进待测集合（不等待、不建任务）
                     ▼
runtime/availability.scheduler()（lifespan 启动，随服务关闭取消）
        ├─ 启动补齐：缺行 / 过期才补测（重启频繁，光靠定点容易整天漏掉）
        ├─ 消费待测集合（每 WATCH_INTERVAL 秒看一次）
        └─ 每天 DAILY_CHECK_HOUR=7 点（东八区）全量重测
                     │
安装 / 更新 / 卸载收尾 ──┴─▶ refresh_definition() + 界面「重新检测」按钮
```

- 探测一律在**工作线程**里跑（`asyncio.to_thread`），即使慢也不占用事件循环；
- 界面卡片据此显示「检测于 …」；可用性只是展示元数据，一次运行并不依赖它
  （`runtime/service.prepare_run` 不读它），所以缓存过期最多是版本号晚几小时。
- `provision.*` 是安装器**自有的信封**（`provision.started / provision.output /
  provision.qr / provision.input / provision.finished`），与协作域的 `collab.*` 同理，
  **不是**适配器事件协议，因此不参与铁律 5 的四处同步。其中 `provision.qr` 与
  `provision.input` 只在「登录」动作里出现：前者是短期登录二维码，后者是「请把授权码贴回来」
  的提示（不含凭据）；登录子进程的输入写回接口同样只对管理员开放。
- 未声明 `provisioner` 的适配器（进程内 API 内核、`custom-cli`）卡片副标题写明
  「进程内运行」/「自备可执行文件」，平台不代管它们的环境。卸载属于破坏性操作，接口要求显式 `confirm`。

### 4.2 凭据库（Credential）

网关密钥是**模型提供方**的密钥；智能体干活时还会用到别的密钥（GitHub token、数据库口令…），
这类叫**凭据**，与网关彻底分开。凭据是**全局一份**：

- `credentials(id, name, description, value_encrypted)`：凭据本身，在「设置 → 凭据库」里维护；
- `agent_credentials(agent_profile_id, credential_id, position)`：智能体 → 凭据的引用关系，
  与 `agent_skills` 同一套思路 —— 智能体运行时能拿到的凭据只能来自这张表。

纯逻辑在 `app/credentials.py`（叶子模块，对话域与协作域共用），读库 / 落库 / 引用关系在
`runtime/credentials.py`：

- **落库即加密**：值只经 `security.encrypt()`；接口只回 `{id, name, description, has_value, masked}`，
  明文永不出库（`reads()`）。改值时值留空 = 不换值，所以回显掩码不会被写回去。
- **名即环境变量名**：`name` 必须是合法环境变量名（`^[A-Za-z_][A-Za-z0-9_]{0,63}$`），否则 422；
  库里重名同样 422。
- **两条入口**：智能体弹窗里点名字输入框就是凭据库（挑一条＝建立引用；输入新名字 + 值＝顺手新建一条
  并引用），设置页的「凭据库」负责改名 / 换值 / 删除。仍被未删除的智能体引用时删除返回 409。
- **只以环境变量注入**：`runner` 把 `ctx.credentials` 合并进子进程环境（适配器/网关给的同名变量
  优先，网关仍是模型密钥的唯一来源）；进程内 API 内核把同一份交给它的 `run_shell`。
  模型只知道变量名 —— 框架把人名的名字清单（`notice()`）并进人格文本，**值不进提示词**。
- **值会被擦除**：运行层对每条事件过一遍 `scrub_event()`，即使子进程把值回显出来，
  也进不了 SSE、落库内容与命令行回显（`runner._redact`）。

### 4.2.1 凭据信任边界与网络出口过滤

**信任边界声明**：有 shell 访问或网络访问的智能体（Claude Code、Codex、Gemini CLI、DeepSeek Harness、
custom-cli、OpenAI 兼容内核）原则上能读到注入的凭据值。平台做了多层防护，但无法阻止一个有 shell + 网络的 AI
把信息间接传出去——这是信任边界，不是 bug。

**网络出口过滤**（新增）：

- 设置项 `CODA_EGRESS_MODE`（`off` / `block` / `allowlist`，默认 `off`）与
  `CODA_EGRESS_ALLOWED_HOSTS`（逗号分隔域名白名单）。
- `off`：不限制（默认，保持向后兼容）。
- `block`：禁止所有出站网络——子进程注入 blackhole 代理环境变量
  `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` 指向不可达端口；API 内核的 httpx 客户端用
  `EgressTransport` 包装，拒绝非白名单连接。
- `allowlist`：只允许白名单里的域名（网关地址自动加入白名单）。
- 纯逻辑在 `app/egress.py`（叶子模块），CLI 注入在 `runner.py`，API 注入在 `openai_harness.py`。

**API 内核工具结果擦除**（新增）：进程内 API 内核的工具输出（`run_shell` 等）在回填给模型的
`messages` 列表前，先用 `credentials.scrub()` 擦掉凭据值。这样模型在后续推理的上下文里也看不到
明文（之前只擦了事件流，没擦回填给模型的工具结果）。

**纵深防御建议**：对高危服务（如 GitHub、数据库），可选用「凭据代理 MCP」模式——把凭据封装在一个
MCP 服务器里，AI 只能调用暴露的工具（如 `create_pr`），永远拿不到原始密钥。这是**可选项**，不是
通用方案：要为每个服务单独封装，且无法覆盖用户自定义凭据。当前平台不内置这类 MCP，但支持用户自己建。

**已有的保护链路**（简要回顾，不重复细节）：加密落库 → 只以环境变量注入 → 事件流 `scrub_event`
擦除 → 命令回显 `_redact` 脱敏 → 接口只回掩码。新增的两层（网络出口 + 工具结果擦除）补上了之前
缺失的两个环节。

### 4.3 网关与密钥（Gateway）

**网关是 API 密钥的唯一来源**（`gateways` 表）：每个网关一条 `base_url` + 一份加密密钥，
由两类组成 ——

- **官方链路**（`kind="official"`）：启动播种按适配器声明的 `default_gateway` / `default_model`
  / `protocol` / `default_auth_style` / `default_models` 生成，一个内核一条，不能被删除；
- **自定义网关**（`kind="custom"`）：用户在「设置 → 自定义网关」里创建，带 `protocol`
  （openai / anthropic / gemini）与 `auth_style`（bearer / x-api-key / query / none）。

界面约定：设置页只列**自定义网关**（官方链路不在这里维护）；智能体配置里的「官方默认」
就是 `gateway_id = null` —— 等价于「平台不接管路由」，`resolve_runtime()` 回落到适配器从本机
读到的配置（`system_defaults()`）并且不注入密钥，由内核自己按环境 / 配置文件里的凭据处理。
因此智能体侧永远只有两个选项：官方默认，或一条自定义网关。

智能体（`AgentProfile`）只引用一个 `gateway_id` 并从它的 `selected_models` 白名单里选模型；
运行时由 `runtime/gateway.py:bind_gateway()` 决定这次注入什么（`resolve_runtime()` 只做转发）：
只有**自定义**网关才注入 `base_url` + 解密后的密钥 + `auth_style`，官方默认（或没选）一律
回落到 `resolve_gateway(None)`。密钥**从不下发**：接口只回掩码（`api_key_set` /
`api_key_preview`）。

模型优先级：**这条对话单独指定的模型**（`Conversation.model`，聊天页输入框上方的芯片可改）→
智能体默认模型（`AgentProfile.model`）→ 内核默认。对话级模型只影响下一轮，不清空会话记忆；
换智能体时会自动清掉（旧模型可能属于另一个网关）。可选清单由
`GET /api/conversations/{id}/models` 给出：自定义网关只返回白名单，官方默认链路返回空清单、
由界面改为手填模型名。

- **「获取模型」= 服务端探测**：`POST /api/gateways/probe` 按协议打对应端点
  （openai `/models`、anthropic `/v1/models`、gemini `/v1beta/models`），返回清单但不落库；
  `runtime/gateway_probe.py` 负责 URL 归一化与各协议的鉴权头。
- **「必须成功才能保存」是服务端不变量**：`selected_models` 至少一项 —— 探测成功勾选，
  或走「手动旁路」录入（界面在探测失败时自动展开）。
- 被**未删除**的智能体引用的网关不能删（409）；官方链路一律 409。

## 5. 技能（Skill）

技能是各智能体内核的能力包（一个目录 + ``SKILL.md``）。平台把它们**统一编目**，
再通过**智能体分配**决定某个智能体能用哪些技能。技能分两类：

* **共享技能**：内核自带 / 宿主机上已有的技能，由适配器 ``skill_roots()`` 只读扫描，
  ``owner_id`` 为空，全平台可见、只读；
* **用户技能库**：导入、市场安装、挂载到其它内核的技能，**按 slug 只存一份**，落在
  ``<数据目录>/skill-libraries/<owner_id>/<slug>``，按用户隔离；被哪些内核引用登记在
  ``<owner_id>/.mounts.json``。

一份技能文件被多个内核共用（引用挂载），市场更新覆盖唯一实体后，所有挂载它的内核同时生效；
运行时再按「智能体分配」物化进各自的隔离目录。

```
内核技能根（共享） ──discover_skills()──┐
                                        ├─▶ 统一目录(skills 表) ──分配──▶ agent_skills ──▶ 运行时物化
用户技能库（按 owner） ──library scan────┘   （owner_id 隔离）   （智能体弹窗）      agent-homes/<对话>/…
  ~/.claude/skills
  ~/.claude/plugins/cache（已安装插件）
  $CODEX_HOME/skills
  <数据目录>/skill-libraries/<用户>/<技能>          # 一份实体
  <数据目录>/skill-libraries/<用户>/.mounts.json    # 被哪些内核引用
```

- **编目**：适配器用 `skill_roots()` 声明自己的技能根（路径 + 来源 + 是否可安装），框架用
  `scan_skill_roots()` 统一扫描，去重（同一个 slug 按「平台技能 > 市场技能 > 个人 > 项目 > 插件 > 系统」
  取优先级最高的一份）后写入 `skills` 表；同时扫描各用户的技能库
  （`<数据目录>/skill-libraries/<owner>/<技能>`），按 `.mounts.json` 展开成「一个内核一行」，
  `owner_id` 记归属，所有行共享同一个 `path`。重扫时以磁盘为准：技能目录**确实不存在、
  且技能根（父目录）还在**时，解除 `agent_skills` 分配并删掉编目行（技能都没了，分配是
  幽灵引用）；目录还在、只是这一轮没扫到，或连技能根都不在（数据卷没挂上）时只把
  `available` 置 False，保住分配。用户技能库实体消失时同步摘掉 `.mounts.json` 里的失效
  引用（技能库根目录本身不在时不动登记）。旧布局 `<owner>/<内核>/<技能>` 在重扫时自动迁移
  （同 slug 合并引用，内容冲突时市场份优先、另一份挪进 `.conflicts/`）。
  来源标签见 `runtime/skills.py:SOURCE_LABELS`。
- **按用户隔离**：共享技能（`owner_id` 为空）所有人可见只读；用户技能只有主人与管理员可见，
  标识带归属（`<type>:lib:<owner 或 local>:<slug>`），因此不同用户导入同名技能不会冲掉对方。
  列表 / 详情 / 分配 / 用量 / 删除全链路按这条规则过滤。
- **插件技能只认「已安装」**：`plugins/marketplaces/<市场>/` 只是市场目录的克隆（可浏览、未安装），
  真正的插件技能在 `plugins/cache/<市场>/<插件>/<版本>/`，由 `installed_plugins.json` 指向。
  插件技能只读（卸载走插件管理器），目录消失后不在列表里留失效行。
- **同名技能靠命名空间共存**：技能标识与物化目录名都取自 `slug`（技能目录名）。多个插件各带一个
  `access` 时，`SkillRoot.namespace` 会把它变成 `<插件名>-access`，原始目录名记在
  `SkillSpec.extra["original_slug"]`，展示名带上来源（`access · telegram`）。
- **分配**：`agent_skills` 是智能体能用的技能的**唯一来源**；`PUT /api/agents/{id}/skills` 整表替换。
  类型与智能体的内核不一致、或技能不在该智能体主人可见范围（别人的用户技能）时，
  界面直接禁止选择，服务端再兜底返回 422。
- **导入 / 导出 / 跨内核挂载**：技能页可以把一个技能导出成 zip（`GET /api/skills/{id}/export`，只读），
  把 zip 导入自己的技能库（`POST /api/skills/import`，内容走 base64，同名不覆盖），
  或把同一份技能挂载到自己另一个内核（`POST /api/skills/{id}/copy`，只登记引用，不复制文件）。
  共享技能第一次挂载时会收进技能库；之后所有内核共用这一份。三者都只动技能目录 / 引用登记，
  导入 / 挂载后仍需「智能体分配」才会在运行时生效。
- **物化（严格仅分配）**：适配器的 `skill_mount(ctx)` 声明「我的内核从哪里读技能」——
  例如 Claude Code 用 `CLAUDE_CONFIG_DIR` 指向一个只含「分配到的技能 + 本机 settings」的隔离
  配置目录，并关闭内核自带的 bundled 技能与内置插件；Codex 用 `CODEX_HOME`。
  框架按这份声明把技能目录复制进 `agent-homes/<对话ID>/<内核配置目录>`，并携带/打补丁
  必要的配置文件。复制**优先硬链接**（技能库与隔离目录同在数据目录下、同盘）：
  inode 共享、零拷贝；链接不可用（跨盘 / 平台或权限不支持）时自动降级普通复制，
  产物内容一致。会被框架改写的副本在改写前先断开链接（frontmatter 同步的
  `SKILL.md`），源目录始终只读；`carry` 配置文件始终普通复制（它随后会被补丁改写）。
  加了前缀的副本会同步改写 `SKILL.md` 的
  `name`（Claude Code 按目录名调用，Hermes 按 frontmatter 的 `name` 查找）。
  物化带**无变化快速路径**：每轮成功物化后在隔离目录写 `.skills-manifest.json`
  （技能集合 + 每个源文件的「大小 + mtime」签名）；下一轮先比对，集合与源内容都没变
  （且物化目录都还在）时跳过重建直接复用，多轮对话不再重抄同一批文件。取消分配、
  技能更新、清单损坏或任何比对失败都回到全量重建——「取消分配即立刻消失」的强一致
  语义不变；`carry` / `patch`（内核配置携带与补丁）不走快速路径，每轮照做。
  每次重建技能子目录，残留没删干净时不写清单（宁可下轮重建，不把脏目录标记成可复用）。
  市场更新（先删源目录再重写）不会影响已物化副本持有的旧 inode；内容偏旧由
  清单的 mtime 签名捕捉，下一轮自动重建拿到新内容。

  隔离目录放在数据目录而不是临时目录：内核的会话记录也写在自己的配置目录下，
  每次运行都换目录会导致多轮对话无法续接。

- **调用统计**：适配器的 `skill_invocation(tool_name, tool_input)` 声明「本内核的技能调用长什么样」
  （Claude Code 是内置的 `Skill` 工具，参数带技能目录名；默认不识别）。「用量」页据此从已落库的
  消息 `parts` 里数出技能**调用次数**（`GET /api/usage/overview`），不额外落库、对历史消息同样生效。

- **边界**：内核自带的少量技能（如 Claude Code 的 `doctor`）需要通过 `skillOverrides` 逐个关闭；
  内核还会读取工作目录下的 `<工作目录>/.claude/skills`，这类技能不在分配范围内，运行时会给出提示。

### 5.1 技能市场（SkillHub）

平台的「技能」页可以把**腾讯 SkillHub**（[skillhub.cn](https://skillhub.cn)）的技能**装进自己的技能库**：

```
skillhub.cn ──/api/skills 列表 / /api/v1/skills/{slug} 详情──▶ 浏览
           └─/api/v1/download?slug=…（zip）──▶ 安全解压到
                <数据目录>/skill-libraries/<owner_id>/<slug>
```

- **安装 = 写当前用户的技能库**（不是内核全局目录）：落点是平台托管的
  `skill_libraries_root/<owner>/<技能>`，天然按用户隔离；适配器只负责声明
  「本机原生技能根在哪」，不参与用户库路径。
- **一份实体、多内核引用**：安装不再指定内核，默认挂到**全部支持技能的内核**；重新安装同一个
  slug = 覆盖唯一实体，所有内核同时生效。某个内核已有同 slug 的其它技能时跳过它并在 `warnings`
  里说明（物化会撞名）。市场页卸载按**实体整体**移除：断开全部智能体分配、删除全部编目行与唯一
  目录；技能页的 `DELETE /api/skills/{id}` 仍只摘本内核引用（最后一个引用消失才删实体）。
- **装完仍需分配**：安装只是把技能放进了该用户的编目范围（`source_kind=market`），要
  智能体真正用上，还得在「智能体 → 技能」里分配——与内核自带技能同一套「严格仅分配」规则。
- **可卸载，但有护栏**：市场页卸载要求目录内有市场安装标记（``.skillhub.json``）**且**目录落在
  当前用户的技能库内；别人的技能、内核自带技能永远不会被市场卸载接口删掉。
  卸载按实体整体断开「智能体 → 技能」分配、删掉全部内核的编目记录并移除唯一目录——已卸载的技能
  不再出现在技能页/市场列表里（目录被外部删掉的旧记录也会被列表过滤掉）。
- **技能页可删自己的技能**：`DELETE /api/skills/{id}` 允许删除自己技能库里、非内核自带
  （`source_kind != system`）的技能；管理员可以删共享 / 他人的技能。删除同样断开分配、删编目记录；
  磁盘侧只允许删用户技能库（或内核技能根）之内、含 `SKILL.md` 的目录，越界路径一律拒绝。
- **解压安全**：拒绝 zip-slip（绝对路径 / `..`）、跳过符号链接、限制文件数与解压后总字节数。
- **接口**：`GET/POST /api/skillhub/*`（列表 / 内核 / 详情 / 安装 / 卸载），见 ``api/skillhub.py``。

## 6. MCP 服务器（Model Context Protocol）

MCP（Model Context Protocol）服务器是内核使用的**外部工具服务**：一个本地进程（stdio）或一个
HTTP / SSE 端点。与技能完全对称，平台把它们**统一编目**，再通过**智能体分配**决定某个智能体
能连哪些服务器；「哪个内核把 MCP 配置放在哪、长什么形状」是**适配器的知识**，框架不硬编码。

```
各内核 MCP 配置 ──discover_mcp_servers()──▶ 统一目录(mcp_servers) ──分配──▶ agent_mcp_servers ──▶ 运行时物化
  ~/.claude.json                          （只读索引 + 平台新建通用项） （智能体弹窗）      agent-homes/<对话>/…
  $CODEX_HOME/config.toml
  ~/.gemini/settings.json
```

- **三类来源**：`type_id is None`（`source_kind=manual`）是平台通用服务器，可以分配给任意内核的
  智能体。其中 `builtin_key` 非空的是**平台内置**（图片生成 / 系统管理），启动时按
  `app/builtin_mcp.py` 的声明为每个用户幂等播种：连接方式由平台维护，且不能删除
  （不需要时停用）；内置项**只对所属用户可见与可分配**，管理员也不跨用户查看别人的副本
  （系统管理 MCP 的 PAT 不能串用）。系统管理的平台地址与专用令牌由平台自动填写
  （`CODA_PUBLIC_BASE_URL` 可覆盖地址），用户只需关心权限模式；生图等部署相关参数仍需用户填写。
  `builtin_key` 为空的则是用户在「MCP」页新建的，可编辑 / 停用 / 删除。
  `type_id` 非空（`source_kind=user`）是从某个内核自己的配置里只读扫出来的，**只能分配给同内核**
  的智能体，平台不接管它的内容（改 / 删返回 403）。
- **编目**：适配器用 `discover_mcp_servers()` 只读扫描本机配置（解析 / 渲染的共享工具在
  `agents/mcp.py`），`POST /api/mcp-servers/rescan` 把结果同步进 `mcp_servers` 表。配置里不再存在的
  服务器只标记 `available=False`，**不删行**（分配关系不断链）。
- **分配**：`agent_mcp_servers` 是智能体能挂的 MCP 的**唯一来源**；
  `PUT /api/agents/{id}/mcp-servers` 整表替换。不在同一内核的扫描项、以及同一次分配里两个重名标识
  （写进内核配置会互相覆盖）都会被拒（422）。换内核时，来自原内核配置的分配自动丢弃，平台通用的保留。
- **物化（严格仅分配）**：适配器的 `mcp_mount(ctx)` 声明「把分配到的服务器写到哪里」——写进隔离
  配置目录的文件（`McpMount.files`）或工作目录里的项目级配置（`McpMount.workspace_files`）：

  | 内核 | 落点 | 隔离手段 |
  | --- | --- | --- |
  | Claude Code | 隔离目录的 `mcp.json` | `--mcp-config <file> --strict-mcp-config`（本机 / 项目的其它 MCP 一律不认） |
  | Codex | 隔离 `CODEX_HOME/config.toml` 的 `[mcp_servers.*]` | 先剥掉技能物化带过来的本机配置，再追加分配到的 |
  | Antigravity CLI | 暂未支持 | `agy mcp` 的本地落盘格式尚未确认，v1 不声明 MCP mount（宁可不支持，也不写假的配置面） |

  技能与 MCP **共用同一个「家」**（`AgentContext.home_dir`）：同一个隔离目录里既有分配到的技能，
  也有分配到的 MCP 声明，每次运行重建，取消分配即立刻消失。（只把配置写进工作目录的内核
  没有这个家，框架不会凭空给它建一个空目录。）
- **密钥只经 `app/security.py`**：`env` / `headers` 的值加密落库（键明文、值密文），接口只回键名与
  掩码，明文只在「解密后注入适配器」的一瞬间存在。编辑语义：**列出的键留空＝保持原值、删掉整行＝
  删除、新键必须给值**（`runtime/mcp.py:_merge_pairs`）。
- **被引用时不可删**：`DELETE /api/mcp-servers/{id}` 在仍有未删除的智能体挂着它时返回 409；
  平台新建的服务器走软删除（`deleted_at`），历史对话与协作历史照旧可读。
- **接口**：`GET/POST /api/mcp-servers`、`POST /api/mcp-servers/rescan`、
  `GET/PATCH/DELETE /api/mcp-servers/{id}`、`PUT /api/agents/{id}/mcp-servers`，见 `api/mcp.py`。
- **界面**：「MCP」页左侧是编目列表，点一条在右侧看详情。详情由
  `components/McpDetailBody.tsx` **一处实现、两端共用**（桌面端右侧面板 / 移动端详情弹层，
  移动端多传 `showName={false}` 因为弹层标题已经写了名字）：身份头（图标 + 名称 + 来源 · 内核 ·
  传输方式 · 标识 + 可用状态）→ 操作 → 「连接方式 / 密钥 / 分配」三张带图标的卡片 → 一句说明。
  值一律**换行**而不是省略号截断（启动命令很长）；样式是两端各一份的 `.mcp-*` 共享段
  （`tests/test_mobile_web.py::test_mcp_detail_styles_are_identical_on_both_ends` 守着逐字一致）。

**平台内置的通用服务器**：`scripts/mcp/image_mcp_server.py` 是随平台分发的一个「图片生成」MCP
服务器（stdio、纯标准库）——把 OpenAI 兼容网关的生图接口包成一个 `generate_image` 工具：
不传参考图走 `POST /v1/images/generations`（文生图）；传 `image` / `images` 则走
`POST /v1/images/edits`（图生图 / 图片编辑，支持一次给多张参考图）。

它是**内置服务器**（声明在 `backend/app/builtin_mcp.py`）：平台启动时为每个用户自动播种一条
平台通用记录（`runtime/mcp.py:ensure_builtin_servers_for_all`，注册新用户时也会补），用户
不需要自己新建、也不需要找脚本路径。连接方式（启动命令 = 当前解释器、参数 = 脚本路径）由
平台维护，界面上只读；用户只需要在「MCP」页的**参数**里填写网关地址与模型名 —— **模型不写死**，
按自己的网关填。参数最终仍以环境变量注入（加密落库、运行时注入）：

| 参数（环境变量） | 说明 |
| --- | --- |
| `IMAGE_GEN_GATEWAY` | 生图网关地址（裸域名或带 `/v1` 都行，自动归一化），必填 |
| `IMAGE_GEN_MODEL` | 生图模型名（需支持图片编辑时才有图生图能力，如 `doubao-seedream-5-lite`），必填 |
| `IMAGE_GEN_API_KEY` | 网关密钥（无鉴权的本地网关可不填） |
| `IMAGE_GEN_TIMEOUT` | 单次生图超时秒数（默认 300） |
| `IMAGE_PUBLIC_BASE_URL` | 可选：图片的对外访问前缀（如静态图床 `https://img.example.com`）。设置后工具结果回**绝对 URL**，适合经 MCPHub 等网关代跑、与平台不在同一文件系统的场景；不设置则回工作目录内的相对路径 |
| `IMAGE_GEN_INPUT_ROOTS` | 可选：额外的只读输入根目录（冒号分隔）。网关代跑时按相对路径到这些根目录的下一层查找调用方留在工作区里的参考图 |
| `IMAGE_RETENTION_MINUTES` | 可选：生成图片本地保留多少分钟后自动清理（0 / 未设置＝不清理）。网关代跑时图片只是给客户端取回的中转，客户端（平台的取图逻辑见下文）拿到副本后即可清理 |

**独立 HTTP 容器（集中管理形态）**：不想让生图跟着 MCPHub 的 stdio 进程走时，可以用
`docker/image-mcp/` 把它单独跑成一个容器：`server.py` 用 MCP SDK 的 Streamable HTTP 传输
把同一份 `generate_image` 暴露成 `POST /mcp`，并在同一端口用 `GET /images/<文件名>`
托管生成的图片（不再需要单独的 nginx 图床）。生图逻辑仍复用
`scripts/mcp/image_mcp_server.py`，两份入口共享同一套环境变量与 `generated-images/` 目录。

* 镜像：`docker/image-mcp/Dockerfile`（构建上下文是仓库根目录），MCP 依赖只有
  `mcp==1.30.0` + `uvicorn`，不引入到后端 `requirements.txt`；
* 运行配置：宿主机 `/vol1/1000/docker/image-mcp/`（`docker-compose.yml` + `.env` + `data/`），
  端口 `3070`；`.env` 里的 `IMAGE_MCP_TOKEN` 同时是 `/mcp` 的 Bearer 令牌；
* `IMAGE_PUBLIC_BASE_URL` 指向本容器自己的 `/images/`（如 `http://<宿主机IP>:3070/images`），
  因此工具结果直接是可访问 URL，配合 `CODA_IMAGE_INGEST_PREFIXES` 落库前取回；
* 图生图参考图：把 Coda 的 `workspaces/` 只读挂到容器里，用 `IMAGE_GEN_INPUT_ROOTS` 指过去；
* Magpie（`/library`）用 `library mcp add image-gen http://image-mcp:8000/mcp
  header=Authorization="Bearer <IMAGE_MCP_TOKEN>"` 登记，就能在它的 MCP hub 里集中路由；
  对外暴露的工具名是 `image-gen__generate_image`（MCPHub 那份是 `generate_image`）。

参考图取值：**工作目录内的相对路径**（如 `attachments/a.png`、`generated-images/b.png`）或
http(s) 图片地址。本地文件由服务器读成 base64 data URL 再发给网关 —— 网关在另一台机器上，
拿不到本机路径。只支持 PNG / JPEG / WebP，单张 ≤ 10 MB、一次最多 10 张。

生成的图片落在本进程工作目录的 `generated-images/` 下；工具结果回什么取决于部署形态：

* **本地直连**（平台内直接起进程，工作目录就是对话工作目录）：回**工作目录内的相对路径**
  （如 `generated-images/image-….png`）——大图不进上下文，模型把该路径写进 `![](…)` 即可
  在页面上显示成图（前端按相对路径换成只读文件出口的 URL）。
* **网关代跑**（配了 `IMAGE_PUBLIC_BASE_URL`）：回**绝对 URL**（图片由图床托管，跨进程 /
  跨容器可访问）。这种形态下图片只在 MCP 侧有一份，所以平台在助手消息落库前会把它取回
  对话工作目录并改写成相对路径，保证对话历史自包含（`app/image_ingest.py`，前缀由
  `CODA_IMAGE_INGEST_PREFIXES` 显式配置）。

**纯文本图片路径也要能显示**：模型/工具不一定按约定写 Markdown 图片，常见形态是
「文件：xxx.jpg」+「位置：/app/data/workspaces/<id>/」或直接贴工作目录绝对路径。
前端 `lib/media.tsx` 的 `extractImagePaths()` 从正文里识别图片路径，原样交给对话/协作的
只读文件出口；后端 `attachments.resolve_workspace_file()` 仍严格校验路径必须位于当前工作目录内
（容器内的绝对路径只要确实在当前工作目录下也能直接取到）。这条兜底只影响展示，不改变生成/落盘格式。

服务器内部按「协议层 → 工具层 → 客户端层 → 配置层」单向分层，测试见
`backend/tests/test_image_mcp.py`。

## 7. 数据模型

```
users(id, username UNIQUE, display_name, email, password_hash, token_version, role(admin|member),
      is_active, avatar_file, pat_hash, pat_issued_at, created_at, updated_at)
                                                         ← 口令只存 PBKDF2 哈希；token_version 用于吊销旧令牌；
                                                           pat_hash 是个人访问令牌（PAT）的 sha256，明文只在签发时返回一次
gateways(id, name, kind(official|custom), type_id, protocol, auth_style, base_url,
         api_key_encrypted, selected_models JSON, model_catalog JSON, extra_headers JSON,
         enabled, last_probed_at, deleted_at, owner_id)   ← API 密钥的唯一来源
agent_profiles(id, name, avatar, avatar_preset, avatar_file, tagline, description, system_prompt,
               type_id, gateway_id→gateways, model, options JSON, enabled, deleted_at, owner_id)
               -- 头像优先级：avatar_file（自定义上传）> avatar_preset（内置头像包）
               -- 两者都空时按 id 稳定兜底一张头像包图片；avatar（emoji）已下线，仅兼容历史数据
               -- 内置头像包分「人物 / 卡通」两类，画廊按分类分组；清单见 frontend/src/lib/avatarPack.ts
credentials(id, name, description, value_encrypted, created_at, updated_at, deleted_at, owner_id)
agent_credentials(agent_profile_id→agent_profiles, credential_id→credentials，复合主键 + position)
conversations(id, title, agent_profile_id→agent_profiles, session_id, workspace_dir, model, archived,
              last_read_at, context_summary, context_compacted_count, context_compacted_at, owner_id)
              # last_read_at 为空 = 升级前的旧对话，一律当已读
              # context_summary / context_compacted_count 是自动压缩的摘要与已折叠条数（原文仍全在 messages）
messages(id, conversation_id→conversations, role, content, parts JSON, usage JSON, status, error,
         agent_profile_id, context_tokens)   # agent_profile_id 为空 = 对话归属的智能体；非空 = @ 召唤的智能体
                               # 回答的那一轮（消息仍在对话流里，归属与主场会话记忆不变）
                               # context_tokens = 本轮最后一次模型请求的输入 token（上下文窗口快照）
task_cards(id, conversation_id→conversations UNIQUE, anchor_message_id, title, items JSON,
           version, last_run_id, created_at, updated_at)
           # 一个对话最多一张；items = [{id,text,done,done_at,updated_at}]
           # 客户端没有任何写接口：唯一写入方是运行收尾解析模型输出的 task_card 块
           # anchor_message_id 决定卡片插在对话流的哪一条助手消息之后
skills(id="<type_id>:<slug>", type_id, slug, name, description, source_kind, path, fingerprint,
       version, author, when_to_use, allowed_tools JSON, files JSON, total_bytes, meta JSON,
       available, note, source_url)   # source_kind 含 market（市场技能，可卸载）
agent_skills(agent_profile_id→agent_profiles, skill_id→skills，复合主键 + position)
mcp_servers(id, name, slug, description, type_id(空=平台通用), source_kind(manual|user),
            builtin_key(空=非内置，如 image-gen), transport(stdio|http|sse), command, args JSON, url,
            env_encrypted JSON, headers_encrypted JSON, enabled, available, note,
            discovered_at, updated_at, deleted_at, owner_id)
agent_mcp_servers(agent_profile_id→agent_profiles, mcp_server_id→mcp_servers，复合主键 + position)

-- 协作域（独立于对话域，见 docs/collaboration-design.md）
collab_teams(id, name, description, entry_key, workspace_dir, graph JSON, budget JSON, owner_id)
collab_runs(id, team_id→collab_teams, title, goal, status, entry_key, workspace_dir, budget, usage,
            steps_used, tickets_used, error, owner_id)
collab_tickets(id, run_id→collab_runs, parent_ticket_id→collab_tickets,
               from_agent_profile_id→agent_profiles, to_agent_profile_id→agent_profiles,
               owner_key, holder_key, submitter_key, title, brief JSON, state,
               round, max_rounds, verdict, review_comment, result JSON, closed_at, report_at)
collab_steps(id, run_id→collab_runs, ticket_id→collab_tickets, agent_profile_id→agent_profiles,
             role_key, duty, sequence, action, status, content, gist, parts JSON, usage JSON,
             error, audit JSON)
collab_messages(id, run_id→collab_runs, sender, role_key, agent_profile_id→agent_profiles,
                sequence, status, content, parts JSON, usage JSON, error, meta JSON)

-- IM 域（旁路：通道，不是成员，见 docs/im.md）
im_connections(id, platform, account_name, account_id, fields JSON, secrets JSON,
               agent_profile_id→agent_profiles, enabled, last_status, last_error,
               runtime JSON, options JSON, owner_id)
im_peers(id, connection_id→im_connections, sender_id, sender_name,
         state(pending|allowed|denied), chat_key, chat_kind, chat_title)
im_chats(id, connection_id→im_connections, chat_key, kind(direct|group), title,
         agent_profile_id→agent_profiles, conversation_id→conversations)
```

设计取舍：

- **对话快照 `agent_profile_id`**：之后即便智能体改了内核 / 网关 / 模型，历史对话仍可复现。
- **默认智能体按用户各有一颗星**：`agent_profiles.is_default`（同一 `owner_id` 下至多一个，
  点亮时会摘掉同一用户其它智能体的星，删掉带星的那颗会自动补一颗）；新建对话不传
  `agent_profile_id` 时回落到当前用户的星 —— 还没有星时先用他自己「唯一启用的那个」，
  一个可用的都没有才报 422。老库启动时由 `ensure_default_agent` 补星。
- **任务进度卡片是对话级对象，不是消息的一部分**：进度会反复变化，写回旧消息会篡改历史、
  也会和消息落库路径抢同一行；因此单独建 `task_cards`（一个对话唯一一张）。模型在正文末尾
  输出一个 `task_card` 围栏 JSON 块声明建卡 / 勾选，运行收尾时服务端解析、从正文与
  `parts` 里剥离后落库；**没有客户端写接口**，界面只读。`done` 单向（`false → true`），
  解析失败 / 冲突一律保持原状态并记日志。模型把块写成 ```json 或裸 JSON 时同样认
  （只认正文收尾处、且结构像任务卡，中段 JSON 示例不动）。卡片**常驻渲染在输入框上方**，
  默认收起成一行，点开看任务项；折叠状态只存前端 localStorage。
- **`parts` 用 JSON**：工具调用/结果/推理的形态随智能体变化，无需为每种智能体建表；
  它是按真实发生顺序排列的时间线（`thinking` / `text` / `tool_call` / `tool_result`），
  前端据此按**真实顺序**渲染，只把**连续**出现的「思考 / 工具调用 / 工具结果」收进一个默认折叠的
  「过程」块（点开可按原顺序逐条查看），正文仍内联显示 —— 折叠不改变先后顺序，也不按类型归组。
  旧消息没有内联 `text` 段，前端会用 `content` 合成一段兜底，保持向后兼容。
- **密钥只存在网关里**：智能体不直接持有密钥，只引用 `gateway_id`；要换密钥改网关一处即可，
  多个智能体共享同一条链路时不会各存一份。
- **凭据是全局一份 + 引用**：`credentials` 存值、`agent_credentials` 存「哪个智能体能拿到」，
  多个智能体能共用同一条（改一处即全体生效）；加密落库、只以环境变量注入、值不进提示词 /
  日志 / 事件 / 响应。工作目录不在智能体上（建对话、建协作任务时才指定，
  `workspace_dir` 只属于 `Conversation` / `collab_*`）。
- **技能目录消失 → 分配一起消失**：技能是从文件系统扫描出来的，重扫发现目录**确实不存在、
  且技能根（父目录）还在**时，会解除 `agent_skills` 里的分配并删掉编目行（连同用户技能库里
  失效的 `.mounts.json` 引用）——技能本体都没了，留着分配只会让智能体的技能列表和磁盘对不上。
  只有「目录还在、这一轮没扫到」，或连技能根都不在（数据卷暂时没挂上、这一轮无法判断）时，
  才只标记 `available=false` 并保住分配（防一次扫描落空就清空配置）。智能体被删除时分配关系
  保留（见下条）。
- **MCP 也是只增不删 + 严格仅分配**：扫描来的服务器在配置里消失只标记 `available=false`，
  `agent_mcp_servers` 是智能体能挂什么 MCP 的唯一来源；平台新建的服务器可软删除，
  但仍被未删除的智能体引用时拒绝删除（409）。
- **删除智能体 / 网关只做软删除**：外键全是 `RESTRICT`，而历史对话与历史工单都引用着智能体，
  硬删会连历史一起打断。所以 `DELETE /api/agents/{id}` 只写 `deleted_at`：它从所有可选列表里
  消失（新建对话、新建协作节点都拒绝它），但历史对话仍能显示当时的名字 / 头像、也仍能继续对话。
- **协作域的分工键**：工单用 `owner_key`（谁拆的谁汇总）、`holder_key`（现在谁握着）、
  `submitter_key`（最近提交者，打回时默认退回给他）记岗位流转；步骤用 `gist` 存**引擎派生**的
  一句话摘要 —— 拼下游 prompt 时只读 `gist`、不读 `content`，从数据通路上保证"下游只看工单、
  看不到上游对话"。岗位用 key 而不是智能体 id，是因为智能体可以改绑，历史工单仍要能复现。
- **IM 是通道，不是成员**：`im_connections` 把「聊天软件里的一个机器人」绑到一个智能体，
  消息进来就是**用户说的话**（交给 `runtime.service` 的同一套运行链路），回复只发回发起这一轮
  的那个聊天；`im_peers` 保证只有配对过的账号能进智能体，`im_chats` 记聊天 → 对话的对应关系
  （一条聊天固定一条对话，上下文才连续）。凭据按字段加密存在 `secrets` 里，接口只回字段名。
- **加字段要能容忍旧行**：`create_all` 只建新表、**不会给已存在的表加列**，所以"后加的可选列"
  登记在 `models.py` 的 `ADDITIVE_COLUMNS`（对话域）、`collab/tables.py` 的
  `ADDITIVE_COLUMNS`（协作域）与 `im/tables.py` 的 `ADDITIVE_COLUMNS`（IM 域），
  由 `db.init_db` 用 `ALTER TABLE ... ADD COLUMN`（带默认值）
  幂等补齐：现有 `coda.db` 启动后自动对齐，不重建、不丢数据。
- **外键用 `ON DELETE RESTRICT`**：仍被**未删除**的智能体引用的网关不能删（409，避免历史断裂）；
  删除对话时显式清理消息（SQLite 已开启 `PRAGMA foreign_keys`）。

### 7.1 多用户与鉴权

- **会话**：登录 / 注册返回无状态 JWT（HS256，7 天），签名密钥由 `security._load_key()`
  再派生一层（密钥来自 `CODA_SECRET_KEY`，未配置时用 `data/secret.key`）。前端存
  localStorage，`lib/api.ts` 统一附加 `Authorization: Bearer`；`<img src>` 这类不能带请求头
  的资源允许 `?access_token=`（只有 `app/auth_deps.py` 在没有 Bearer 头时才读它）。
  令牌带 `ver`，与 `users.token_version` 比对；改口令 / 管理员重置口令会 +1，旧令牌立即失效。
  改自己的口令时响应会返回一份新令牌，前端保存后当前设备不必重新登录。
- **个人访问令牌（PAT）**：`pat_` 前缀的长效程序化凭据，给 MCP 服务器等脚本以用户身份
  调 REST API 用。每个用户同时只有一份（`users.pat_hash`，只存 sha256 哈希，明文仅在
  「设置 → 账号」生成时返回一次）；`current_user` 按前缀分流 —— PAT 直接按哈希等值查库，
  查不到即 401（吊销 = 清空哈希）。接口：`POST/GET/DELETE /api/auth/me/pat`（签发 / 状态 /
  吊销，均限本人）；重新生成立即吊销旧令牌，改口令 / 管理员重置口令时一并吊销。
  平台自带的「系统管理」MCP 另用一份**自动签发的专用令牌**（`users.builtin_mcp_pat_hash`），
  与用户自己的 PAT 分开：启动 / 登录时若配置里没有可用令牌就自动补发，不轮换用户已有 PAT。
  用户的凭据一变（重新生成 / 吊销 PAT、改口令、管理员重置口令）或配置里的令牌被删改时，
  `runtime.mcp.refresh_builtin_tokens` **当场**补发一份新的写回加密配置 —— 不用等平台重启，
  系统管理 MCP 不会中途 401；仍然有效的令牌不会被多此一举地轮换。
- **口令**：`auth.py` 用 PBKDF2-HMAC-SHA256（600k 次、16 字节盐）存
  `pbkdf2$<迭代>$<盐>$<哈希>`；明文口令永不入库 / 进日志 / 进响应。哈希 / 校验在接口里
  放线程池执行，避免阻塞事件循环。
- **头像**：`app/avatars.py` 是唯一实现（校验 / 落盘 / 读取）。只收 PNG / JPEG / WebP 位图
  （按魔数认，≤2 MB；SVG 会被拒绝 —— 它能内嵌脚本，而头像直接进 `<img>`），文件名由服务端按
  「用户 id + 内容摘要」生成并落在 `<数据目录>/avatars/`，库里只存 `users.avatar_file`。
  接口：`PUT/DELETE /api/auth/me/avatar`（本人）、`GET /api/auth/users/{id}/avatar`（任何登录用户，
  用户列表与协作消息要显示）。前端 `lib/avatar.ts` 带令牌 `fetch` 成 blob + object URL 渲染，
  避免把令牌拼进图片 URL；展示统一走 `components/UserAvatar.tsx`。
- **归属**：`owner_id` 覆盖网关、智能体、凭据、对话、MCP 服务器、协作组与协作运行。
  非管理员的列表只回自己的行：网关额外包含官方链路，扫描来的 MCP 是只读共享目录；
  按 id 读取别人的行返回 404（官方网关与扫描来的 MCP 保持与列表一致）。跨用户引用
  （网关 / 凭据 / 平台通用 MCP / 协作图节点上的智能体）在写入或启动运行前拒绝。
  用量与运行表也按归属过滤。
- **管理员**：可看全部用户资产、管理用户（改角色 / 禁用 / 重置口令）；不能降级或禁用
  当前登录的自己，系统至少保留一名启用中的管理员。
- **开关**：`CODA_AUTH_ENABLED` 默认开启（多用户形态），单用户部署可显式设为 `false`；
  开启后 `current_user` 校验令牌，未登录返回 401。`CODA_REGISTRATION_OPEN=false` 关闭自助注册，
  只能由管理员建用户。首任管理员可由 `CODA_ADMIN_USERNAME` / `CODA_ADMIN_PASSWORD`
  在空库首启时创建；老库的存量行由 `bootstrap.backfill_ownership` 回填给管理员
  （`.ownership_backfilled` 防重）。
- **前端**：`components/AuthGate.tsx` 是唯一入口 —— 未登录显示 `LoginScreen`，登录后
  桌面侧边栏 / 移动抽屉显示当前账号；个人资料与用户管理两端共用
  `AccountPanel` / `UserAdminPanel`。

## 8. 扩展点

| 想做的事 | 需要改的地方 |
| --- | --- |
| 接入新 CLI 智能体 | 在 `agents/builtin/` 加一个模块 |
| 接入新模型供应商（无官方 CLI） | 复用 `openai_harness.py` 的基类，加一个子类声明默认网关/模型 |
| 零代码接入私有 CLI | 用 `custom-cli` 适配器填命令 + JSON 取值路径 |
| 让运行时可由平台安装 / 更新 | 在该适配器声明 `provisioner`（`RuntimeProvisioner`），界面自动出现按钮 |
| 新增可配置项 | 在该适配器加一个 `ConfigField`，前端表单自动出现 |
| 新增移动端页面 | 在 `frontend/src/mobile/` 加页面并在 `MobileApp.tsx` 注册路由（桌面端不受影响） |
| 让某个适配器支持技能 | 声明 `capabilities.skills=True`，实现 `skill_roots()`、`discover_skills()` 与 `skill_mount()` |
| 从技能市场安装 / 卸载技能 | 框架已内置（`app/skillhub.py` + `api/skillhub.py`）；只要适配器声明了 `installable` 的技能根 |
| 让某个适配器支持 MCP | 声明 `capabilities.mcp=True`，实现 `discover_mcp_servers()` 与 `mcp_mount()` |
| 接入一个新的 MCP 服务器 | 平台新建（「MCP」页）或写进内核自己的配置后「重新扫描」；分配在「智能体 → MCP」里做 |
| 新增一个平台内置 MCP 服务器 | 在 `app/builtin_mcp.py` 的 `BUILTIN_SERVERS` 加一条声明（脚本随 Release 打包），启动时自动播种 |
| 新增协作能力（多智能体工单流转） | 全部在 `app/collab/`（独立域，不碰框架层），见 `docs/collaboration-design.md` |
| 换掉事件传输（SSE→WebSocket） | 只改 `transport/sse.py` 与前端 `streamApi` |
| 换掉存储（SQLite→Postgres） | 只改 `settings.database_url` 与 `db.py` |
| 发布一个新版本 | 改 `backend/app/__init__.py` 的 `__version__`（唯一版本源；**只在公开发版时递增**，日常提交不动）→ 提交 → `bash scripts/release.sh`（构建前端、打 tar.gz、`gh release create`）；运行端在「设置 → 关于」一键更新 |

### 8.1 平台自更新（版本级 · GitHub Release）

「设置 → 关于」的检测更新吃的是 **GitHub Release**，不是 main 分支 —— 版本级发布才构成
一次更新，日常 commit 不算。两种部署形态：

- **托管模式**（Docker / 任意进程管理器）：`releases/` 下按版本各占一个子目录，
  `current` 软链指向正在运行的版本。更新 = 下载 Release 产物包（`coda.tar.gz`，
  含预构建 `frontend/dist`）→ 校验 SHA256 → 安全解压成新子目录 → 原子切指针 →
  `os.execv` 原地重启进程。**同 PID 自替换**：容器里 uvicorn 是 PID 1，容器不退出、
  不需要 docker.sock；进程管理器（PyPanel 等）完全无感知。回滚 = 指针切回旧目录 +
  同样重启；`releases/` 保留最近 3 个版本。
- **源码模式**（仓库里直接跑，无 `current` 链）：只检测、不安装 —— 升级动的是正在
  运行的代码本身，开发机上由 git 管。

```
GitHub Release ──/releases/latest──▶ app/selfupdate.check_latest()   （代理走 platform_settings.proxy_url）
        │
        └─/coda.tar.gz（+.sha256）─▶ install_release()：下载 → 校验 → 安全解压（防穿越/跳链接）
                                            → releases/<版本>/ → 原子切 current → os.execv 重启
界面（关于页 UpdatePanel）：SSE 收 update.* 事件 → 轮询 /api/health 等版本号变化 → 整页重载
```

实现在 `app/selfupdate.py`（叶子模块：标准库 + httpx + settings，不碰数据库以外的层），
路由在 `api/update.py`（`update.*` SSE 信封是本模块自有的，不属于适配器事件协议）。
发布侧 `scripts/release.sh`；Docker 形态 `docker/Dockerfile` + `docker/entrypoint.sh`。
**源码在构建时打进镜像**（`/image-app`），`/app` 卷只存版本目录与 venv：空卷首启直接用
镜像自带代码落一个版本，即「构建什么就运行什么」；`CODA_FETCH_RELEASE_ON_EMPTY=1`
才恢复「空卷先联网拉最新 Release」的旧行为。后续升级统一走上面的更新机制。
entrypoint 的依赖安装**必须**走 `run_with_install_proxy`：pip 只认 `HTTP(S)_PROXY`，
不认 `CODA_INSTALL_PROXY`，漏了就是直连 PyPI（国内网络会卡二十多分钟）；
`CODA_PIP_INDEX_URL` 可改走国内镜像，二者取其一即可把首启压到几十秒。
真实内网地址只写在部署侧 compose（不入 git），仓库模板用占位符。

## 9. 已知边界

- CLI 适配器的多轮依赖各工具自身的会话续接；`custom-cli` 若声明不出 `session_path`，
  平台会退化为 transcript 回放。
- `codex exec` 与 `codex exec resume` 认的旗标不一样：`-s/--sandbox`、`-C/--cd` 只挂在 `exec` 上，新版 `exec` 也去掉了 `--full-auto`。适配器按子命令分别表达 —— 沙箱在 `exec` 上用 `-s`、恢复会话时走 `-c sandbox_mode=…`；全自动走 `-c approval_policy=never`。`resume` 这一子命令形式在不同 Codex 版本间也有差异，所以做成了可选策略（app-server 模式下对应 `thread/resume`）。
- 新版 Codex 不再读 `OPENAI_BASE_URL`（内置 provider 拿着它也会直奔 api.openai.com），所以自定义网关
  一律经一个具名 `model_provider` 注入（名字取「自定义供应商名称」，留空用内置名 `coda`）；
  没走网关时（官方默认 / 本机登录态）不注入任何 provider。`wire_api` 也不再由平台下发 ——
  0.4x 起只剩 `responses`、更早的版本默认 `chat`，写死任何一边都会在另一边直接报错。
- Codex 默认走「接口模式 = app-server」：`codex app-server --listen stdio://` 是 Codex 自己的
  JSON-RPC 通道（TUI 也用这条），回复按 `item/agentMessage/delta` **逐字**推送，思考走
  `item/reasoning/summaryTextDelta`，用量走 `thread/tokenUsage/updated`。个别不支持 app-server
  的旧版本可在智能体选项里切回「接口模式 = exec」（`codex exec --json` 只在整条消息完成时给一个
  `item.completed`，回复是整段出现的）。
- app-server 是**常驻**内核：适配器只声明第一帧 `initialize`（`Invocation.stdin_hold=True`），
  拿到应答后再用 `AgentRun.send()` 排队 `thread/start`（或 `thread/resume`）与 `turn/start`，
  由执行层统一代写 stdin，并在适配器 `mark_done()` 后关掉 stdin 收工（关不掉则 5 秒后强杀）。
  适配器仍然只生产数据、不碰子进程，取消 / 超时 / 凭据擦除依旧只有一份实现。
- app-server 没有人工确认入口，所以服务端反向请求（命令 / 补丁审批、`requestUserInput`、
  MCP elicitation）一律自动拒绝，并在对话里显式提示——否则该轮会一直等一个不会到来的答复。
- Codex 默认把 OTEL 指标导出到它的分析后端，连不上时会在**退出前**干等满 10 秒
  （表现为「回复完还要等一会儿才结束」）。适配器用 `-c otel.metrics_exporter="none"` 关掉它；
  要自己接 OTEL 的可以在「额外命令行参数」里再覆盖回来。
- Antigravity CLI 没有系统提示词旗标，人格统一拼进提示词最前面（`AGENTS.md` 属于用户项目，不写）。
- 工具循环（API harness）的权限模型是「工作目录沙箱」，没有做命令级审批。
- 技能「严格仅分配」只保证**平台注入的技能 == 智能体分配**：内核自带的个别技能（Claude Code 的
  `doctor`）要靠 `skillOverrides` 关闭；工作目录自带的 `.claude/skills` 会被内核直接读取，
  平台只提示、不删除用户文件。
- 技能目前只支持「查看 + 分配」；编辑、安装与上传留待后续（技能目录仍由各内核自己管理）。
- MCP 的「严格仅分配」同样只保证**平台注入的服务器 == 智能体分配**，各内核的隔离强度不一样：
  Claude Code 用 `--strict-mcp-config` 能彻底关掉其它 MCP（工作目录自带的 `.mcp.json` 一律不生效，
  运行时会提示）；Codex 靠改写隔离 `config.toml` 做到；Antigravity CLI 的本地 MCP 配置面
  尚未确认，v1 直接不声明 MCP 支持，避免「写了配置但内核不认」的假隔离。
- 协作域（`collab/`）与技能一样**暂不注入 MCP**：挂着 MCP 的智能体参加协作运行时，
  引擎只在工单里给一行提示（要真正用上 MCP，得先按对话运行）。
- 运行时的安装 / 更新按适配器声明的命令执行（内置 CLI 内核为官方 npm 包）。若某台机器上的
  内核是用别的方式装的（例如发行版包、容器、垫片脚本），平台更新会按声明的包管理器**另装一份**；
  页面会显示当前可执行文件路径供核对，但不会自动识别、也不会去卸载非本平台安装的副本。

## 10. 移动端与 PWA

移动端不是桌面端的「窄屏适配」，而是**另一条物理独立的链接**：`/m/`。

```
frontend/
├── index.html              # 桌面端入口 → /
├── m/index.html            # 移动端入口 → /m/
├── public/m/               # 移动端 PWA 资产（manifest + Service Worker）
├── public/icons/           # 两端口共用的应用图标（由 scripts/gen_icons.py 生成；
│                           #   界面品牌标 components/BrandMark.tsx 直接用这张品牌图）
└── src/
    ├── App.tsx · pages/ · index.css                  # 桌面端
    ├── components/                                   # 两端共用的纯展示组件（Composer · Icons · MessageView…）
    └── mobile/                                       # 移动端（独立页面与样式表）
        ├── main.tsx · MobileApp.tsx · mobile.css
        ├── components/（顶栏 · 左侧抽屉 · 底部弹层）
        └── pages/（对话 · 智能体 · 技能 · MCP · 协作 · 设置）
```

- **只共享基础设施，不共享界面**：两端共用 `lib/`（`api` / `types` / `agentStream` / `usage` / `collabDraft`）
  与纯展示组件（`DynamicForm` / `MessageView` / `MarkdownView` / `ConversationThread` /
  `CollabAgentThread` / `Composer` / `Icons`）；页面、路由、样式表各自独立
  （移动端用底部弹层替代桌面端的居中 Modal）。
- **后端只保留一个 SPA 兜底**：`main.py:resolve_spa_target()` 按前缀把深链分派到 `dist/index.html`
  或 `dist/m/index.html`，其余静态文件原样返回；`/assets` 由两端口共享。
  ⚠️ `/api/` 下的漏网请求**不**走这个兜底：`main.py` 明确回 JSON 404（`接口不存在：/api/xxx`）。
  兜底一旦回 HTML，前端 `response.json()` 就会解析失败 —— WebKit 抛出的是与业务无关的
  "The string did not match the expected pattern."（Chromium 又是另一种说法），排查成本极高。
- **接口错误文案不交给浏览器**：`lib/api.ts` 先把响应读成文本再自己 `JSON.parse`：
  空响应体按「没有返回」处理，非 JSON（反向代理的登录页 / 静态兜底）转成中文提示，
  并带上 HTTP 状态与 `Content-Type`，不暴露浏览器方言的解析异常。
- **PWA**：`public/m/manifest.webmanifest`（`scope` 与 `start_url` 都是 `/m/`）+
  `public/m/sw.js`（安装期预缓存入口 HTML 与其引用的构建产物；导航网络优先、离线回落外壳）。
  Service Worker 的作用域只在 `/m/`，且 `/api/`（含 SSE 长连接）永不缓存、一律直连后端——
  离线只保证「界面能打开」，对话仍需联网。
- **改了外壳就要 bump 缓存版本**：`sw.js` 的缓存名带版本号（`coda-m-shell-v2`…），
  动过入口 HTML / 样式表 / 构建产物就 +1 —— 已安装的独立应用才会在下次打开时换掉旧外壳。
  独立应用没有地址栏也没有刷新按钮，因此设置页提供了「更新到最新版本」（`pwa.ts:refreshShell()`：
  注销 SW、清掉 `coda-m-*` 缓存再重载），作为卡在旧外壳时的自助出口。
- **移动端没有底部 Tab，只有左侧抽屉**：打开就是聊天界面（`/chat` 自动进入最近一次对话，
  一条都没有时给空状态引导），点顶栏左上角的 logo 弹出左侧抽屉
  （`components/MobileDrawer.tsx`）—— **上半部分是主导航**（对话 / 智能体 / 协作组 / 技能 / MCP /
  设置，与曾经的底部 Tab 一一对应），**下半部分是对话列表**（`＋ 新建` 直接选智能体开聊）。
  桌面端左侧栏（`App.tsx` 的 `NAV` + `components/ConversationList.tsx`）现在是**同一份菜单**
  （顺序、命名、行样式都对齐）；「内核管理」与「自定义网关」两边都不占菜单，收在「设置」页里（桌面端是标签页，移动端是配置入口）。
  抽屉由 `MobileApp` 统一持有，任何顶级页面的顶栏默认就是那个 logo 按钮（`MobileTopBar`
  在没有 `onBack` 时自动渲染），子页面仍是返回箭头。
  两端的对话列表项都显示时间：今天只显示 `14:23`，其余显示日期 + 时间（口径统一在 `lib/datetime.ts`）。
  **行首是这条对话的头像**（`components/ConversationAvatar.tsx`）：单聊画一枚；被 @ 召唤过的对话
  按群聊样式把多枚头像叠成一摞（最多 3 枚 + 「+N」）。参与者来自列表接口的 `participants`
  （主场智能体排第一，召唤者按首次出场排；只查一次 `messages.agent_profile_id` 分组，不打 N+1）。
  **列表先按「是否还在回复」分组，再按「最近活动」倒序**：AI 还没回复完的对话（运行表
  `registry` 里还有它的运行）整组压在所有已完成的对话上面，组内按
  `conversations.updated_at` 与最后一条消息时间的较晚者排序 —— 一轮长回复期间用户新开的
  对话不会把还在出字的那条挤下去。前端 `lib/conversations.ts:sortConversationsByRecent`
  用同一口径再兜底排一次（传入 `active_runs` 拼出的运行中 id 集合）—— 刚发过消息的对话立刻置顶。
  手机抽屉里每个对话行右侧的「⋯」是会话级操作（重新生成 / 重置记忆 / 复制工作目录 / 删除）的入口：
  它只把路由 state（`openActions`）交给聊天页，由聊天页弹面板 —— 那几件事要用到消息与会话状态。
  桌面端左侧栏对话行不提供「⋯」：悬停只浮出「✎ 重命名 / 🗑 删除」，清除记忆改用输入框里的
  `/clear` 命令（提交时拦截，调 `resetSession` 后提示，不发送消息）。
- **朗读（语音合成）**：开关在对话页顶栏右上角（原有「⋯ 更多操作」的位置），**默认关闭**，
  开启后新回复会被念出来（`lib/tts.ts`：Markdown 先转纯文本，长文按句读切段排队，新输出打断旧的
  一条；开关存 `localStorage['coda.tts']`，只影响这台设备）。判定「新回复」放在聊天页观察
  `messages` 的效果里，所以另一台设备写入、由 `useAutoRefresh` 拉回来的回复同样会念；iOS 两个坑
  一起处理：首次发声在点开关的手势里 `unlockSpeech()` 解锁，打断旧的一条时 `cancel()` 与 `speak()`
  分开两轮执行。
- **导出对话**：对话页顶栏的导出按钮打开一张导出表单（`components/ConversationExportForm.tsx`，
  两端共用），可选「图片 / Markdown」与「对话内容 / 完整数据（含思考与工具调用）」。图片由
  `lib/shareImage.ts` 把当前对话（标题 + 气泡 + 代码块 + 页脚）用 Canvas 画成长图后直接下载
  （不引第三方依赖；过长时整体降采样，避免撞上 iOS 的画布面积上限）；Markdown 由
  `lib/conversationExport.ts` 按同一份消息数据生成，同样走浏览器本地下载。
- **对话页顶栏只有一行居中的对话名称**：不再显示智能体名与内核 / 会话说明（那是开始聊之前的信息，
  聊起来之后更该知道「这段在说什么」），右边仍是导出与朗读两个按钮；两端的顶栏都是三栏布局，
  标题落在正中，超长自动省略。
- **对话标题自动生成**：新对话的第一句话之后，平台用「设置 → 对话标题」指定的智能体与模型
  跑一次一次性运行，把首条消息概括成短标题写回（`runtime/titles.py`）。不指定就跟随「默认智能体」、
  用它的默认模型；开关默认开启。指定了非当前登录内核账号的智能体会**跳过**这次标题生成
  （后台任务不切号，避免与前台对话串号或误扣别的账号额度）。它是**尽力而为**的后台增强：
  失败保留首行兜底标题，且只改标题、不动 `updated_at`（换名字不是一次「活动」，不该把对话顶到列表最前）。起标题同样按「严格仅分配」
  物化技能与 MCP，工作目录用一个跑完即删的临时目录。**失败不打扰用户但留痕**：原因与时间写进
  平台设置（`title_last_error*`），显示在「设置 → 对话」里，成功一次就清掉 —— 后台增强也得
  有自查出口（铁律 11）。
- **分组与折叠**：智能体有 `group_name`（旧库自动补空串），智能体页按分组折叠展示（默认展开）；
  技能页按内核折叠（**默认折叠**）。两处都用同一个组件 `components/CollapsibleGroup.tsx` 与
  同一份展开状态 `lib/collapse.ts`，样式在两端各写一份、共享段逐字一致（有守卫测试）。
- **配置在「智能体」页，安装在内核管理**：网关升格为密钥的唯一来源后，「实例配置」这个概念
  取消了 —— 智能体（内核 + 网关 + 模型 + 人格 + 技能）在两端各自的「智能体」页维护，
  适配器选项仍由 `DynamicForm` 渲染；内核只管**本机那份可执行文件**的装 / 升 / 卸。
  **内核管理只有一处**（桌面端「设置 → 内核管理」；移动端「设置 → 内核管理」进独立一页），
  老地址 `/runtimes` 重定向到桌面端那个标签页。
- **桌面端设置页按设置类型分标签页**：外观 · 对话 · 网关 · 凭据 · 网络 · 内核管理 · 关于
  （`pages/SettingsPage.tsx` 的 `TABS`），当前页写在 `?tab=` 上（刷新、分享链接、老地址重定向
  都能落到正确的页）。标签栏独占一行（页头只留标题，分割线交给标签栏），7 个标签才有整行的宽度。
  分页只是分组 —— 各项设置的组件与语义没变，每个组件仍自带标题与说明；**只读信息收进「关于」**
  （系统信息 · 版本记录）；「内核管理」直接给内核卡片
  （`components/KernelManager.tsx`，点卡片进详情），**不再嵌套一个「运行时」入口 + 适配器清单**。
  概览只在「关于」按需拉一次（进去时拉，不轮询），别的标签页不做无谓请求。
- **「正在运行」随内核进「内核管理」**：任务运行状态与内核安装态同页展示，每 8 秒刷新。
  它要问两处：对话的运行登记在 `runtime.manager.registry`（`GET /api/system/overview`
  的 `active_runs`），协作的岗位与入口对话登记在**协作域自己**的句柄表里
  （`collab/execution.py` 的 `registry` / `chat_registry`）—— 两套表互不相通，所以「内核管理」
  同时取 `overview.active_runs` 与 `GET /api/collab/active-runs` 再合并展示（后者由协作域自己给，
  框架层不反向依赖协作域）。**只查一处就会漏掉另一半正在跑的智能体。**
- **标签页只有一份样式**：`.tabs` / `.tab`（设置 / 技能 / 用量 / 协作导航共用，两端各写一份、
  逐字一致）。形态是**凹槽 + 抬起的选中块**（更高的面 + 外阴影 + 一道细边，选中态前面一颗会亮起的
  品牌色圆点 —— 不选中也占位，切换时文字不会左右跳），带 `:focus-visible` 焦点环与
  `prefers-reduced-motion` 降级；选中态文字用 `--text` 压在面板色上，两种主题都过 WCAG AA。
- **移动端设置页**只留「外观 / 配置 / 更多」三节：运行概览、版本记录这类信息看桌面端即可；
  「更新到最新版本」（自助清缓存重载）保留在「更多」里 —— 独立应用没有刷新按钮。
  **移动端不跟桌面端的分标签页**：它刻意只留「现在要动的开关」。
- **移动端输入框**：附件下沉成共享模块 `app/attachments.py`（校验 / 落盘 / 拼提示词只有一份）；
  先 `POST /api/conversations/{id}/attachments` 落到对话工作目录的 `attachments/`（内容走 base64，
  省掉 multipart 依赖），发消息时随 `attachments` 一起提交，由 `service.compose_user_content()` 把
  路径写进用户提示词 —— 与内核无关，任何能读工作目录的智能体都自动支持；单个附件上限 8 MB。
  协作聊天走同一套：`POST /api/collab/runs/{id}/attachments` + `engine.prepare_chat()` 的
  `compose_with_attachments()`（只有附件、没有正文也允许）。
  语音走浏览器的 Web Speech API（`lib/speech.ts`，两端共用；不支持时按钮仍显示，点一下给出中文说明），
  只把识别文字填进草稿。
  输入区就是屏幕最下面那一块，只留 6px 收边：token 用量在输入框**上方**，底部安全区**不照搬** ——
  iOS 独立应用里安全区 34px，Home 条本身只占 ~13px，照搬会在输入框下面空出一截；输入区底色一直
  铺到屏幕底边。附件 / 语音 / 发送（运行中是停止）都是**框内**的图标按钮（线条 SVG，见
  `components/Icons.tsx`；emoji 在各系统上大小不一），输入框不再有背景提示文字。
- **输入区只有一份**：`components/Composer.tsx`（受控：草稿 / 附件 / 上传 / 发送都由
  页面给），两端的对话页与协作运行页（含聊天弹层）共用，交互与样式必须完全一致；
  样式段在 `index.css` 与 `mobile.css` 各写一份（桌面端 / 移动端各一段微调），共享那段逐字一致。
- **协作运行页 = 一张协作流程图**：`/m/collab/runs/:id` 主区整屏铺那张可拖动 / 双指缩放的
  分层流程图（`MobileCollabProgress` → `CollabFlowMap`，两端共用同一份组件），时间线与
  「需要你决定」卡片收进整屏对话弹层 —— **点入口智能体节点**才出来（顶栏不放对话按钮），点其他
  岗位开只读岗位会话。**没有「聊天 / 推进」模式切换**（V0.43 移除）：所有消息统一走 `/chat`
  （可带附件），是否推进任务由入口智能体判断 —— 它认为需要推进时在回复末尾附 `chat_advance` 块，
  界面渲染「推进任务」决策卡，用户点选后指令才进 `/messages`；挂起后的续跑走「继续 / 重试」按钮。
  流程图沿用编排画布的视觉语言（点阵底纹 + 正交折线 + 箭头），配色走 `--canvas-*` 主题变量，
  浅色下整张图一起变浅。
- **外观（深色 / 浅色 / 跟随系统）**：两端共用 `lib/theme.ts` —— 模式存在 localStorage 的
  `coda.theme`，**没设置过默认「跟随系统」**（按系统的 `prefers-color-scheme` 决定首屏外观），
  解析结果写在 `<html data-theme>`，`applyTheme()` 同时更新
  `meta[name=theme-color]`（状态栏 / Android 系统导航条这些「原生外壳」跟着换色）与 iOS 独立应用的
  状态栏样式。两个入口 HTML 各有一段等价的内联脚本，在首屏渲染前就把主题定下来（否则浅色系统用户
  每次打开都会先闪一下深色外壳）。两份样式表各自的 `:root` 是深色兜底（脚本未执行时用），浅色只是
  `[data-theme='light']` 覆盖块（变量 + 少量写死深色的装饰），`html` 也铺主题底色 —— 系统预留或
  回弹露出的那一条才不会跟页面两个色。桌面壳的窗口底色与启动页同样按 `nativeTheme` 跟随系统。
- **移动端的取舍**：协作域不提供可视化画布（连线需要精确拖拽，手机上体验很差），
  改在「协作组」详情页用**表单**编排：岗位是卡片、连线是「从谁到谁」的下拉选择；
  两端的字段语义、默认值与中文标签共用 `lib/collabDraft.ts` 一份，校验一律走服务端
  `POST /api/collab/teams/{id}/validate`。
- **外壳钉在可视视口上**：`.m-app` 用 `position: fixed; top/right/bottom/left: 0`，而不是
  `100vh`/`100dvh` 配 `height: 100%` 父链 —— 真机上地址栏与底部工具栏会伸缩，两者高度经常对不齐，
  底栏就会浮起来留一条缝或被工具栏压住。固定定位跟的是可视视口，刘海与 Home 条交给
  `env(safe-area-inset-*)`（守卫测试 `test_mobile_shell_is_pinned_to_the_visible_viewport`）。
- **移动端锁定缩放**：移动端采用「固定外壳」布局（顶栏 + 内部滚动区 + 左侧抽屉），随手指缩放会
  让外壳错位，因此入口视口写 `maximum-scale=1.0, user-scalable=no`，并用 `mobile.css` 的
  `touch-action: pan-x pan-y` 兜底（iOS Safari 自 iOS 10 起忽略 `user-scalable=no`，只有
  `touch-action` 真正生效）。因此移动端输入框字号统一 ≥ 16px —— iOS 聚焦小于 16px 的输入框会
  强制放大页面。桌面端不禁用缩放（浏览器缩放是它的无障碍能力）。

---

## 11. 协作流程图实况呈现层

协作进度的主视图是一张**分层流程图**（`components/CollabFlowMap.tsx`，桌面端进度板与
移动端运行页共用同一份组件）：把这次协作画成「谁向谁派活」的节点图，替代早期版本的
等距办公室动画（拟人化 v2 已随动画一起退役，相关底图 / 生成脚本 / 拟人化守卫均已删除）。

### 11.1 视觉与交互

| 元素 | 形态 |
| --- | --- |
| **节点** | 头像 + 昵称 + 状态一行的小卡片；等待中的岗位半透明 |
| **状态徽标** | 头像右上角一枚圆徽标：绿▶=执行中（呼吸）、黄?=等你决定、红!=出错、灰…=等待、绿✓=完成 |
| **连线** | 与编排画布同一套配色（assign 灰紫 / review 青 / rework 黄），回流（打回 / 返工）为虚线 |
| **详情卡** | 点节点弹出：状态、最近动作、跑了几步、在办的工单、跳转该岗位实时对话 |
| **画布** | 点阵底纹 + 拖动平移；桌面端支持滚轮缩放，移动端单指拖动 |

布局优先还原协作组设定画布保存的坐标（`layoutFlowNodesFromCanvas`，原样照搬）；旧图没有
坐标时才用 `lib/collabProgress.ts` 的 `buildFlowRows` 派生「一行一层」（入口领导者独占第一行，
往下每行并排）。有画布坐标时连线复用 `lib/collabRouting.routeEdge`，与设定画布同一套路由，
端口方向 / 避障 / 多条线错开都一致。

**界面只留图本身**：桌面端进度覆盖层（`CollabProgressBoard`）与移动端运行页的流程图区
都不再带标题、状态、完成度或使用说明 —— 「协作进度」开关挂在顶栏，状态徽标与图例
（`.flow-map-legend`）自解释，说明文字与运行摘要（步数 / 工单 / 预算 / token、工单状态计数）
只保留在左栏运行列表与工单看板里。覆盖层没有关闭按钮时由顶栏开关或 Esc 收起。

### 11.2 样式与守卫

- 样式段在 `index.css` 与 `mobile.css` 各写一份（「协作流程图」横幅），共享段逐字一致，
  守卫 `test_mobile_web.py::test_flow_styles_are_identical_on_both_ends`；
- 配色走 `--canvas-*` 主题变量（与编排画布共用），浅色由 `[data-theme='light']` 覆盖，
  不写死深色；
- 状态徽标的呼吸 / 辉光动画在 `prefers-reduced-motion: reduce` 下静默，状态色保留；
- 移动端外壳缓存版本随外壳样式表改动 +1（`public/m/sw.js`），否则已装到主屏的 PWA
  换不到新样式。
