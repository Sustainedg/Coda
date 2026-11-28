# CodeBuddy CLI 接入说明

> 调研对象：腾讯 CodeBuddy Code CLI（`@tencent-ai/codebuddy-code`）。
> 本文说明它是什么、平台怎么接、已验证的能力与边界。实现以
> `backend/app/agents/builtin/codebuddy_cli.py` 为准。

---

## 1. 内核定位

**CodeBuddy CLI 是 Claude Code 家族的内核，headless 协议与 Claude Code 几乎逐字对齐**
（包内类型声明直接写着 "vendored from Claude Code 2.1.316"）。这意味着：
接入它 ≈ 复制一份 `claude_code.py` 适配器，改掉内核名、配置目录、鉴权方式三件事。
核心链路（`-p --output-format stream-json` + `--resume` + `--mcp-config` + 技能目录）
全部现成，不需要任何新框架能力、不需要新增事件类型。

主要差异有两点：

1. **模型网关的接法**：官方链路走自家协议（`CODEBUDDY_BASE_URL` 会被拼成
   `<base>/v2/chat/completions`，不是标准 OpenAI 端点），第三方模型只能通过
   `models.json` 注入（仅支持 OpenAI 兼容格式）。所以自定义网关要写 `models.json`，
   而不是走 `base_url_env` 环境变量。
2. **官方登录的形态**：没有 `codebuddy login` 子命令，交互登录只在 TUI 的 `/login` 里。
   但 `codebuddy --serve` 的本地 HTTP API 把同一条链路暴露了出来
   （`POST /api/v1/auth/account/login` → `authUrl`，`GET /api/v1/auth/account/status`
   → `authenticated`），平台据此实现「登录按钮 + 扫码」的可视化登录：
   助手脚本 `codebuddy_login.py` 拉起一次临时的 `--serve`，把授权链接渲染成二维码。
   登录 session 落盘在 `Data/Public/auth/*.info`（跟随真实 HOME，**不是**系统密钥环，
   也与 `CODEBUDDY_CONFIG_DIR` 无关）。无头部署仍可选用
   `CODEBUDDY_API_KEY`（并选对 `CODEBUDDY_INTERNET_ENVIRONMENT`）或 `CODEBUDDY_AUTH_TOKEN`。

---

## 2. 内核能力与协议

来源：npm 包 `@tencent-ai/codebuddy-code@2.185.0`（2026-11-17 发布，tgz 69MB / 解压 226MB），
文档随包分发在 `dist/web-ui/docs/cn/cli/*.md`，公开仓库 https://cnb.cool/codebuddy/codebuddy-code 。

### 2.1 安装与可执行文件

| 项 | 值 |
| --- | --- |
| npm 包 | `@tencent-ai/codebuddy-code` |
| bin | `codebuddy`（还有别名 `cbc` / `codebuddy-code`，以及辅助 `cbc-prewarm` / `codebuddy-lowmem`） |
| Node 要求 | 启动器硬检查 ≥ 18.20.8；官网要求 Node 22+ |
| 版本探测 | `codebuddy --version` |
| 更新 | `codebuddy update`（平台用 npm 走 provisioner 即可） |

### 2.2 headless 协议（接入主路径）

```bash
codebuddy -p "提示词" \
  --output-format stream-json --verbose --include-partial-messages \
  --permission-mode <mode>            # 非交互必须给权限策略，否则要授权的操作全部被拒
```

- 提示词可以从 **stdin** 进（`echo ... | codebuddy -p`），与平台 runner 现有的
  `Invocation.stdin` 用法一致。
- 输出是逐行 JSONL，事件形状与 Claude Code 相同：

| `type` | 内容 | 平台映射 |
| --- | --- | --- |
| `system` / `subtype:"init"` | `session_id`、`tools`、`mcp_servers`、`model`、`permissionMode` | `session` + `log` |
| `stream_event` | `content_block_delta`（`text_delta` / `thinking_delta`），**必须带 `--include-partial-messages`** | `text_delta` / `thinking_delta` |
| `assistant` | `content[]`（`text` / `tool_use` 块） | 补发全文（无增量时）+ `tool_call` |
| `user` | `content[]`（`tool_result` 块） | `tool_result` |
| `result` | `is_error`、`usage`、`duration_ms`、`num_turns`、`total_cost_usd` | `usage` + 运行结果 |
| `system` / `usage_update`、`task_*` | 每轮用量、后台任务事件 | 可先忽略（`log`） |

- 会话续接：`--resume <session-id>`（还有 `-c`、`--fork-session`、`--resume-create-missing`）。
- 其他可用旗标：`--system-prompt` / `--append-system-prompt`、`--allowedTools` /
  `--disallowedTools` / `--tools`、`--agents`、`--setting-sources`、`--settings`、
  `--mcp-config <file>` + `--strict-mcp-config`、`--max-turns`。
- 权限模式：`default` / `acceptEdits` / `auto` / `dontAsk` / `plan` / `bypassPermissions`；
  bypass 用 `-y`（`--dangerously-skip-permissions`）显式开启，root 下还需要**进程环境变量**
  `CODEBUDDY_IS_SANDBOX=1`（它只认进程 env，不认 settings 的 `env` 字段 —— 平台恰好是
  直接注入子进程 env，天然满足）。

### 2.3 配置目录与隔离

- 配置根：`~/.codebuddy`，环境变量 **`CODEBUDDY_CONFIG_DIR`** 可整体搬走
  （`resolveCliHomeDir()` 读它；skills / mcp / models.json / settings / plugins 全部跟随）。
- 结构：`settings.json`、`skills/<slug>/SKILL.md`、`.mcp.json`、`models.json`、
  `plugins/`、`projects/<hash>/<session>/`（会话记录）、`history.jsonl`。
- 项目级：工作目录下的 `.codebuddy/`（skills、settings.json、models.json）。
- **隔离目录 = `CODEBUDDY_CONFIG_DIR`**：与 Claude Code 的 `CLAUDE_CONFIG_DIR` 完全同构，
  平台现有的 agent-homes 物化机制直接复用，多轮会话也天然按对话隔离。

### 2.4 技能

- 用户级：`<config>/skills/`，项目级：`<workspace>/.codebuddy/skills/`，格式与 Claude Code
  相同（目录 + `SKILL.md` frontmatter）。
- 调用方式：内置 `Skill` 工具（参数 `skill` = 目录名），与 Claude Code 一致，
  `skill_invocation()` 直接照抄。
- 自带技能：npm 包内 `builtin/skills/`（当前只有 `mods-create` 一个，面向 Web UI 创作模式）。
  注意：`CODEBUDDY_BUILTIN_SKILLS_DIR` 只是**额外**扫描根，包内 fallback 永远会扫
  `builtin/skills`，所以**自带技能关不掉** —— 这是与 Claude Code（有
  `disableBundledSkills`）的已知差异，先接受，文档里注明。

### 2.5 MCP

- 用户级：`~/.codebuddy/.mcp.json`（推荐）/ `~/.codebuddy.json`（旧版）；
  项目级：`<workspace>/.mcp.json`。
- 严格仅分配：`--mcp-config <file> --strict-mcp-config`，与 Claude Code 同名同义，
  `mcp_mount()` 照抄 claude 适配器的写法。

### 2.6 鉴权与模型路由（平台适配的核心差异点）

| 路径 | 机制 | 平台对接方式 |
| --- | --- | --- |
| 官方（账号登录） | **没有 `codebuddy login` 子命令**（2.185.0 的 Commands 列表里没有）；交互式 TUI 里用斜杠命令 `/login`，选 Chinese / International / Enterprise / iOA 站点后浏览器授权。**无头等价物是 `--serve` 的本地 HTTP API**：`POST /api/v1/auth/account/login` 返回浏览器授权地址，`GET /api/v1/auth/account/status` 报告 `authenticated` / `loginPending`。session 落盘在 `<共享数据根>/Data/Public/auth/<hash>.info`（Linux `~/.local/share/CodeBuddyExtension/...`、macOS `~/Library/Application Support/...`、Windows `%LOCALAPPDATA%\\...`），**不是** `~/.codebuddy/.credentials.json`，也不在系统密钥环里 | `gateway_id=null`：不注入任何 env，CLI 自己读本机登录态；「登录」按钮由 `login_argv` 指向 `codebuddy_login.py` 助手（见 §3.6） |
| 官方（环境变量） | `CODEBUDDY_API_KEY` / `CODEBUDDY_AUTH_TOKEN` / `apiKeyHelper`（优先级 AUTH_TOKEN > apiKeyHelper > API_KEY）。**用 API Key 必须同时选对站点**：中国版 `CODEBUDDY_INTERNET_ENVIRONMENT=internal`、iOA=`ioa`、专享版=`cloudhosted`、私有化=`selfhosted`；不设则打国际站 `www.codebuddy.ai` | 走智能体的「环境变量」或适配器的「官方网络环境」字段；**不能**用 `CODEBUDDY_BASE_URL` 当平台网关（会被拼成 `<base>/v2/chat/completions`，自家协议） |
| 第三方模型 | `models.json`：`{models:[{id,url,apiKey,...}], availableModels}`；`url` 必须是**完整** `/chat/completions` 路径；`apiKey` 支持 `${ENV}` 引用；鉴权固定 `Authorization: Bearer` | `gateway_url + "/chat/completions"` 写成 `url`，密钥写 `${CODEBUDDY_API_KEY}`；`--model` 选注入的 id |

`models.json` 的读取路径：`$CODEBUDDY_CONFIG_DIR/models.json`（用户级）、
`<workspace>/.codebuddy/models.json`（项目级，优先）。**仅支持 OpenAI 兼容接口**
（文档明说；另有 `api: "openai-responses"` 变体）。Anthropic / Gemini 协议的网关接不了，
适配器要在构建时给出明确的中文报错。

其他有用的环境变量：`DISABLE_TELEMETRY` / `DISABLE_ERROR_REPORTING` /
`DISABLE_AUTOUPDATER`（无人值守场景建议默认关掉遥测与自动更新）。

---

## 3. 平台适配实现

新建 `backend/app/agents/builtin/codebuddy_cli.py`，骨架与 `claude_code.py` 对齐：

```
CodeBuddyCliAgent(AgentDefinition)
  id            = "codebuddy-cli"
  name          = "CodeBuddy CLI"
  vendor        = "Tencent"
  executable    = "codebuddy"
  logo          = "/icons/kernels/codebuddy.webp"    # 真彩色位图，logo_style = "image"
  runtime       = Runtime.CLI
  protocol      = "openai"          # 自定义网关只认 OpenAI 兼容
  api_key_env   = "CODEBUDDY_API_KEY"
  base_url_env  = None              # 故意不声明：CODEBUDDY_BASE_URL 语义与平台网关不匹配
  default_gateway = None            # 官方链路是「本机登录」，不播种官方网关（bootstrap 会跳过）
  default_model = "default-model"   # CodeBuddy 内置 Auto
  docs_url      = "https://cnb.cool/codebuddy/codebuddy-code"
  provisioner   = RuntimeProvisioner(
      manager="npm", package="@tencent-ai/codebuddy-code",
      install_argv=npm_install_argv("@tencent-ai/codebuddy-code", bin_name="codebuddy"),
      update_argv =npm_install_argv("@tencent-ai/codebuddy-code", bin_name="codebuddy", latest=True),
      uninstall_argv=npm_cli("uninstall", "-g", "@tencent-ai/codebuddy-code"),
      latest_argv=npm_cli("view", "@tencent-ai/codebuddy-code", "version"),
      prepare_argv=npm_prepare_argv("@tencent-ai/codebuddy-code"),
      homepage="https://cnb.cool/codebuddy/codebuddy-code",
  )
  capabilities  = Capabilities(streaming, sessions, native_system_prompt,
                               tools, mcp, permission_modes, working_directory, skills)
```

> `default_gateway=None` 的含义：启动播种不会为它建「官方网关 + 默认智能体」
> （`bootstrap.seed_if_empty` 对没有 `default_gateway` 的适配器直接跳过）。
> CodeBuddy 出现在「新建智能体」内核下拉里；用户要么用「官方默认」
> （回落到本机 `codebuddy login`），要么绑一个 OpenAI 协议的自定义网关。

### 3.1 config_fields（中文 label，前端 DynamicForm 自动渲染）

| key | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `permission_mode` | SELECT | `bypassPermissions` | default / acceptEdits / auto / dontAsk / plan / bypassPermissions。headless 下没有授权弹窗，默认给能真正干活的模式（help 里注明风险，与 claude 适配器的措辞一致） |
| `network_environment` | SELECT | 空（跟随内核默认 = 国际站） | 官方链路的站点选择 → `CODEBUDDY_INTERNET_ENVIRONMENT`：中国版 `internal`、iOA `ioa`、专享版 `cloudhosted`、私有化 `selfhosted`。只在**没绑自定义网关**时注入（第三方模型服务不需要） |
| `system_prompt_mode` | SELECT | `append` | append → `--append-system-prompt`；replace → `--system-prompt` |
| `partial_messages` | BOOLEAN | `True` | 控制 `--include-partial-messages` |
| `allowed_tools` / `disallowed_tools` | STRING_LIST | — | → `--allowedTools` / `--disallowedTools` |
| `extra_args` | STRING_LIST | — | 兜底旗标 |
| `env` | KEYVALUE | — | 框架已支持 |

### 3.2 build_invocation()

```python
argv = ["codebuddy", "-p", "--output-format", "stream-json", "--verbose"]
if partial_messages: argv.append("--include-partial-messages")
if ctx.model:        argv += ["--model", ctx.model]
mode = options.get("permission_mode") or "bypassPermissions"
argv += ["--permission-mode", mode]
if mode == "bypassPermissions": argv.append("--dangerously-skip-permissions")
if ctx.system_prompt:  # append / replace 两个旗标
if ctx.session_id:     argv += ["--resume", ctx.session_id]
if allowed_tools:      argv += ["--allowedTools", *as_list(...)]
if disallowed_tools:   argv += ["--disallowedTools", *as_list(...)]
argv += extra_args
# MCP 严格仅分配（家目录里物化好的那份）
if (Path(ctx.home_dir) / "mcp.json").is_file():
    argv += ["--mcp-config", str(home / "mcp.json"), "--strict-mcp-config"]

env = self.effective_env(ctx)          # CODEBUDDY_API_KEY（api_key_env）+ 用户自定义 env
# 官方站点的选择：只在「没绑自定义网关」时注入，第三方模型服务不需要它
if options.get("network_environment") and not (ctx.gateway_url and ctx.api_key):
    env.setdefault("CODEBUDDY_INTERNET_ENVIRONMENT", options["network_environment"])
if ctx.home_dir:
    env["CODEBUDDY_CONFIG_DIR"] = ctx.home_dir
if mode == "bypassPermissions" and _is_root():
    env.setdefault("CODEBUDDY_IS_SANDBOX", "1")   # root 逃生口，只认进程 env
env.setdefault("DISABLE_TELEMETRY", "1")
env.setdefault("DISABLE_ERROR_REPORTING", "1")
env.setdefault("DISABLE_AUTOUPDATER", "1")
env.setdefault("CI", "1")
# 自定义网关：写 <home>/models.json（OpenAI 兼容），而不是 CODEBUDDY_BASE_URL
self._sync_models_json(Path(ctx.home_dir), ctx)   # 见 3.3
return Invocation(argv=argv, env=env, cwd=ctx.workspace_dir, stdin=ctx.prompt)
```

### 3.3 网关路由：写 models.json 进隔离家目录

`ctx.gateway_url`（平台网关，OpenAI 协议约定形如 `https://host/v1`）+ `ctx.api_key` →

```json
{
  "models": [{
    "id": "<ctx.model>",
    "name": "<ctx.model>",
    "vendor": "custom",
    "url": "<gateway_url 去尾斜杠>/chat/completions",
    "apiKey": "${CODEBUDDY_API_KEY}",
    "supportsToolCall": true,
    "supportsImages": true
  }],
  "availableModels": ["<ctx.model>"]
}
```

- `apiKey` 用 `${CODEBUDDY_API_KEY}` 引用环境变量，明文密钥不落盘（包内文档明确支持）。
- CLI 侧 `normalizeChatCompletionsUrl()` 会自动归一化重复的 `/chat/completions` 后缀。
- `id` 直接取 `ctx.model`（平台模型白名单里的名字），`--model` 传同名 id。
- 写入位置：隔离家目录。`Invocation.files` 只会写进**运行目录**，而 `models.json`
  必须跟着 `CODEBUDDY_CONFIG_DIR` 走，所以适配器在 `build_invocation()` 里直接写
  `ctx.home_dir/models.json`（0600）。没有网关时**删除**该文件，否则切回官方链路
  还会继续打到上一次的网关上。
- 登录态：账号凭据在 `<config>/.credentials.json`。隔离家目录默认没有它，
  所以 `skill_mount()` 的 `carry` 会把 `.credentials.json` 与 `settings*.json`
  一起带进隔离目录 —— 否则「官方 = 本机登录」这条链路一进隔离目录就变成未登录。
- 网关协议不是 `openai` 时：构建阶段 `yield` 一条中文 `log`/`error`（「CodeBuddy 只支持
  OpenAI 兼容网关」），不要静默失败。

### 3.4 parse_line()

照抄 `claude_code.py` 的 `_dispatch`（`system/init` → session、`stream_event` → 增量、
`assistant` → 补发全文 + tool_call、`user` → tool_result、`result` → usage + is_error），
额外处理两个差异：

1. `result.total_cost_usd` 恒为 0（CodeBuddy 用积分计费）——照常透传即可，前端已按字段展示；
2. `system` / `usage_update`、`task_*` 这类本内核特有事件：第一版忽略（或 `log` 降级），
   不为新事件类型动铁律 5 的四处同步。

### 3.5 技能与 MCP

```python
def skill_roots(self):
    config = config_dir()                       # 尊重 CODEBUDDY_CONFIG_DIR（未设时 ~/.codebuddy）
    roots = [
        SkillRoot(str(config / "skills" / ".skillhub"), source_kind=MARKET_SOURCE),
        SkillRoot(str(config / "skills"), source_kind="user"),
    ]
    builtin = _builtin_skills_dir()             # 由可执行文件路径反推 npm 包根/builtin/skills
    if builtin:
        roots.append(SkillRoot(str(builtin), source_kind="system", installable=False))
    return roots

def discover_skills(self):  return scan_skill_roots(self.skill_roots(), type_id=self.id)

def skill_mount(self, ctx):
    return SkillMount(
        home_name="codebuddy-home",
        carry=[(str(config / "settings.json"), "settings.json"),     # 本机偏好跟进去
               (str(config / "settings.local.json"), "settings.local.json")],
        notes=[...],   # 工作目录 .codebuddy/skills 会被内核额外加载的提示
    )

def skill_invocation(self, tool_name, tool_input):
    if tool_name != "Skill": return None
    return str(tool_input.get("skill") or "").strip() or None

def discover_mcp_servers(self):   # ~/.codebuddy/.mcp.json（尊重 CONFIG_DIR）→ parse_json_servers
def mcp_mount(self, ctx):         # McpMount(home_name="codebuddy-home",
                                  #         files={"mcp.json": to_json_config(ctx.mcp_servers)})
```

注意两处架构守卫：

- 声明了 `skills=True` / `mcp=True` 就**必须**实现对应 mount（`test_architecture.py` 会拦）；
- `KERNEL_SKILL_MARKERS` 这类「内核路径知识只许出现在适配器里」的守卫同样适用。

### 3.6 probe / system_defaults / auth_status

- `probe()`：默认的「可执行文件 + 密钥」检查够用；可加一步
  `codebuddy --version` 成功即认为就绪。
- `system_defaults()`：只读 `model`（`CODEBUDDY_MODEL` > settings 的 `env` > settings 顶层），
  **不回填网关地址**：`CODEBUDDY_BASE_URL` 是内核自家协议的入口，当平台网关会打错端点。
- `auth_status()`：只做「配置层」判断，**不联网、不消耗积分**——
  1. 进程 env 或 `settings*.json` 的 `env` 里有 `CODEBUDDY_AUTH_TOKEN` / `CODEBUDDY_API_KEY`，
     或配置了 `apiKeyHelper` → `logged_in=None`（已配置，平台不校验有效性）；
  2. `Data/Public/auth/*.info` 存在**非空** session 文件 → `True`
     （这是内核真实的账号登录落盘位置；`.credentials.json` 只是插件 / MCP 的 OAuth 缓存，
     不能当成账号登录态）；
  3. 开了 `CODEBUDDY_CREDENTIALS_IN_MEMORY=1` → `None`（凭据只在进程内存，磁盘上看不到）；
  4. 都没有 → `False`，detail 里给出「点登录按钮扫码 / API Key + 站点 / AUTH_TOKEN」三条修法。
- `login_argv`：内核没有 `login` 子命令，但 `--serve` 暴露了登录 API，所以声明
  `[sys.executable, codebuddy_login.py]`。助手的工作流（见 §3.7）：
  起临时 `--serve`（回环 + `--auth none`）→ `POST /api/v1/auth/account/login` 拿 `authUrl`
  → `segno` 渲染二维码并打印 `QR_IMAGE=<png>`（平台通用登录协议）→ 轮询
  `/api/v1/auth/account/status` 直到 `authenticated`。站点选择：`CODEBUDDY_LOGIN_METHOD`
  > `CODEBUDDY_INTERNET_ENVIRONMENT` > `settings*.json` 的 `env` > 默认 `internal`。

### 3.7 可视化登录（`codebuddy_login.py`）

| 步骤 | 实现 |
| --- | --- |
| 起服务 | `codebuddy --serve --host 127.0.0.1 --port <空闲端口> --auth none`；只监听回环，登录结束即杀进程组 |
| 取链接 | `POST /api/v1/auth/account/login {"method": <站点>}` → `{"data": {"authUrl": ...}}` |
| 换账号 | 已有登录态时把 `/login` 改写成 `/login/switch-account`（国内站），让用户先登出浏览器里的旧登录；登录前后 uid 相同会明确告警 |
| 出二维码 | `segno.make(authUrl).save(png)`，打印 `QR_IMAGE=<绝对路径>`；`segno` 缺失时只打印链接，不中断登录 |
| 等授权 | 每 1.5s `GET /api/v1/auth/account/status`，`authenticated=true` 退出 0；`loginError` / 超时退出 1 |
| 收尾 | 杀 `--serve`、删二维码（短期凭据）与临时日志 |

> 为什么不用 TUI 的 `/login`：headless 没有 TTY，也驱动不了斜杠命令；`--serve` 的 HTTP API
> 是官方提供的公开入口（`docs/http-api.md`），比 PTY 注入稳定得多。

> **Windows 上的两个坑**（依据 2.186.0 包内代码与平台既有启动约定）：
>
> 1. **起服务必须与真实运行用同一套拼法**。npm 在 Windows 上给的是 `.cmd` 垫片，
>    `subprocess` 不能直接执行，得经 `cmd.exe /d /s /c`。平台里这条链路只有一个
>    既定形态：`runtime/runner.py:_spawn_argv` 用 `subprocess.list2cmdline` 把整条
>    命令拼成一个字符串再交给 `cmd`（单测钉着它）。登录助手若另写一套拼法，
>    引号 / 空格上就会出现两套行为——服务起不来，扫码自然永远完不成，对话只剩一句
>    「Authentication required」。故 `_serve_argv` 复用同一套写法。
> 2. **登录态落盘位置跟着「用户文件夹」走**。内核的 `FilePathServiceImpl.getBasePath()`
>    在 win32 上是 `os.homedir()` + `AppData\Local`，而 Windows 的「用户文件夹」
>    由 Known Folder API 解析。平台**不改** `USERPROFILE`，因此登录态与对话看到的是
>    同一份 —— 这一点与豆包相反：豆包要隔离 HOME，所以必须额外把真实数据目录接进去
>    （见 `docs/功能文档/doubao-cli.md` §2.7）。
>
> 另外，内核抛「未登录」时用的是一句英文（`Authentication required. Please use /login
> command to sign in to your account`）。平台在 `parse_line` 里把它翻成中文并指明
> 「内核管理 → CodeBuddy CLI → 登录」，其余报错原样透出（内核自己的信息通常更准）。

### 3.8 多账号：登录几个账号、随时切换（`codebuddy_accounts.py`）

内核是**单账号**模型：`FileAuthenticationStorage` 只保留一份 session
（`Data/Public/auth/*.info`），再登录另一个账号会直接覆盖。`/v2/plugin/accounts` 返回的
「多个账号」是同一登录下的个人 / 企业上下文，不是多个登录。平台侧用「账号槽位」补齐：

| 时机 | 动作 |
| --- | --- |
| 登录成功后 | 把内核写出的 session 文件**原样复制**到 `~/.codebuddy/platform-accounts/<槽位>.info`，元数据（uid / 昵称 / 企业 / 校验和）写 `index.json`；同一账号重复登录覆盖同一槽位 |
| 登录新账号前 | 先把当前账号归档，否则它的 session 会被这次登录覆盖 |
| 切换账号 | 把目标槽位的字节**原样写回**内核的 session 路径（0600、原子替换），并清掉 `*.logged-out` 登出标记 |
| 当前账号判定 | 比对 live session 文件与各槽位的 sha256 |
| 删除槽位 | 只删平台存档，不动当前登录态 |

session 文件是**本机密钥加密**的，平台不解析内容，只做字节复制 —— 同一台机器上切换没问题，
换机器 / 换 `HOME` 则不可移植。切换只影响**之后启动**的对话；正在跑的那一轮仍用原账号。

**「登录其他账号」为什么还可能登回原账号？** 浏览器 / 手机里已经有腾讯登录 cookie 时，
授权页会直接拿旧账号再授权一遍，CLI 收到的自然还是同一个账号。所以助手在「已有登录态」
时会把授权链接从 `/login` 改写成 `/login/switch-account`（国内站支持）：这个落地页上的
「登录其他账号」会先登出浏览器里的旧登录，再用同一个 `state` 回到登录页。同时助手会比对
登录前后的 uid：还是同一个账号时明确输出「这次授权的仍是当前账号，没有新增账号」，不再
假装新增成功。国际站是另一套登录前端，不做路径改写，只保留 uid 比对与提示。

**没扫码就显示登录成功？** `GET /api/v1/auth/account/status` 在**已有登录态**时会直接返回
`authenticated=true`（那是旧 session），它不会告诉调用方「新登录还没完成」。所以助手不以
状态接口为准，而是以 **session 文件内容是否变化** 作为「新登录已落盘」的判据：没扫完码
文件不变，就一直轮询到 5 分钟超时，并明确输出「没有检测到新的登录授权，当前账号保持不变」。
只有文件换成新 session、且状态接口也确认后，才算成功；这样也不会把后台 token 刷新误判成
一次新登录（发起后至少等 1.5 秒）。

框架侧是通用的：`AuthStatus.accounts`（`RuntimeAccount` 列表）由 `auth_status()` 返回，
适配器覆盖 `activate_account()` / `remove_account()` 即自动获得
`POST /runtimes/{id}/accounts/{账号}/activate` 与 `DELETE /runtimes/{id}/accounts/{账号}`，
内核详情里渲染成账号列表（桌面 + 移动端）。

---

## 4. 代码位置

| 文件 | 改动 |
| --- | --- |
| `backend/app/agents/builtin/codebuddy_cli.py` | 适配器（唯一的功能代码） |
| `backend/app/agents/builtin/codebuddy_login.py` | 可视化登录助手（`login_argv` 指向它，按 `QR_IMAGE=` 协议输出二维码） |
| `backend/app/agents/builtin/codebuddy_accounts.py` | 账号槽位：存档 / 切换 / 删除（多账号） |
| `backend/app/agents/base.py` | `RuntimeAccount`、`AuthStatus.accounts`、`activate_account()` / `remove_account()` / `supports_accounts()` |
| `backend/app/api/runtimes.py` | 账号切换 / 删除接口 |
| `backend/tests/test_adapters.py` | 注册表断言 + invocation/parse/权限/env/models.json/登录态用例 |
| `backend/tests/test_codebuddy_login.py` | 登录助手：站点选择、二维码、假服务主链路 |
| `backend/tests/test_codebuddy_accounts.py` | 账号槽位：存档、切换、登出标记、损坏索引 |
| `backend/tests/test_runtimes.py` | 账号切换接口（成功 / 未知账号 / 不支持的内核） |
| `frontend/src/lib/types.ts`、`lib/api.ts` | `RuntimeAccount` + 切换 / 删除接口 |
| `frontend/src/components/KernelManager.tsx`、`mobile/pages/MobileRuntimesPage.tsx` | 账号列表与切换按钮 |
| `backend/tests/test_architecture.py` | `BUILTIN_IDS` 加 `"codebuddy-cli"` |
| `backend/requirements.txt` | 加 `segno`（纯 Python 二维码，不依赖 Pillow） |
| `frontend/public/icons/kernels/codebuddy.webp` | 真彩色 Logo（`logo_style = "image"`，原色显示） |
| `README.md` 「都能接什么」表 | 加一行 `codebuddy-cli` |
| `docs/功能文档/codebuddy-cli.md` | 本文（接入说明） |

不需要改的：智能体表单 / 内核列表的数据来源（仍由后端 descriptor 驱动）、事件协议、
runner / skills / mcp 物化层、Docker 镜像（npm 内核走 provisioner；镜像自带 Node 24 满足要求）。
前端只多了一小块账号列表（`KernelManager.tsx` / `MobileRuntimesPage.tsx`），数据仍来自
`GET /runtimes/{id}/auth` 的 `accounts` 字段。

---

## 5. 风险与已知限制

1. **自带技能关不掉**：`builtin/skills`（当前仅 `mods-create`）永远会被加载。
   影响极小（面向 Web UI 创作模式），先接受并写进技能页说明。
2. **第三方网关只支持 OpenAI 协议**：Anthropic / Gemini 协议的网关接不了，
   构建时给明确报错；官方链路则完全不走平台网关（本机登录）。
3. **`-p` 必须有权限策略**：headless 下没有授权弹窗。默认 `bypassPermissions` + `-y`；
   root 环境自动补 `CODEBUDDY_IS_SANDBOX=1`（与 claude 适配器的 root 逃生口同构）。
4. **安装体积大**：tgz 69MB / 解压 226MB，npm 安装比其他内核慢，内核管理页的
   超时与回显沿用现有 provisioner 机制即可，但要预期「安装」这一步明显更久。
5. **登录态跟着 HOME 走，不跟 `CODEBUDDY_CONFIG_DIR` 走**：session 写在
   `<共享数据根>/Data/Public/auth/*.info`，所以一次扫码对这台机器上的所有对话生效；
   但换机器 / 换 HOME 就要重新登录。容器重建时只要 `$HOME`（`/app/data/home`）在卷上，
   登录态就保得住。需要无交互部署时仍可用 `CODEBUDDY_API_KEY` + 正确的
   `CODEBUDDY_INTERNET_ENVIRONMENT`（或 `CODEBUDDY_AUTH_TOKEN`），而不是绑 OpenAI 网关
   （那是另一条链路，计费与模型清单都不同）。
6. **登录二维码依赖 `segno`**：它是纯 Python 包（不拉 Pillow）；缺失时助手会退化成
   「只打印授权链接」，登录流程不中断，但界面上没有二维码。
7. **多账号是平台侧快照，不是内核原生能力**：切换需要在同一台机器、同一个 `HOME` 下
   （session 文件用本机密钥加密）。切换只影响之后启动的对话，正在跑的任务不会中途换号；
   长时间运行的服务建议在切换后确认当前账号（详情里会显示「当前」标记）。
8. **`--strict-mcp-config` 的语义以实测为准**：旗标在 CLI 参数解析里确认存在；
   「是否真的忽略 user/project 配置」需要在本机配置一个 MCP 服务器后验证它没被加载。
9. **没有「仅工作目录」档**：CodeBuddy 没有 Claude Code 的
   `blockReadsOutsideWorkingDirectories` 一类设置，硬造一个只会给出虚假的安全承诺，
   因此本适配器不声明 `file_access_scope` 字段。需要强隔离请在容器级做。

---

## 6. 已有能力与后续增强

### 6.1 已有能力

- **对话主链路**：`-p --output-format stream-json`、`--resume` 多轮续接、
  工具调用、流式输出；
- **内核管理**：安装 / 更新 / 卸载，登录状态展示；
- **可视化登录**：`codebuddy_login.py` 拉起临时 `--serve`，取授权链接渲染二维码，
  轮询登录状态；
- **多账号**：`codebuddy_accounts.py` 存档 / 切换 / 删除，切换只影响之后启动的对话；
- **技能与 MCP**：隔离目录 `CODEBUDDY_CONFIG_DIR` 下物化，只暴露分配给该智能体的技能 /
  MCP；`--strict-mcp-config` 阻止本机配置漏进来；
- **自定义网关**：写 `models.json`，只支持 OpenAI 兼容格式；协议不匹配时给明确报错；
- **用量**：usage 协议为 Anthropic 形状，跟请求协议（OpenAI Chat）不同，网关按
  `usage_protocol` 归一化。

### 6.2 后续增强（未实现，不影响当前接入）

- 长驻模式（`--input-format stream-json` + `stdin_hold`），省掉每次冷启动；
- `--json-schema` 结构化输出；
- 后台任务事件（`task_*`）映射成平台工具事件。
