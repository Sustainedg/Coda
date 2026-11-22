# 接入 CodeBuddy CLI 的方案

> 调研对象：腾讯 CodeBuddy Code CLI（`@tencent-ai/codebuddy-code`）。
> 本文回答三件事：它是什么、平台怎么接、按什么顺序落地。

---

## 1. 结论先行

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
2. **官方登录的形态**：没有 `codebuddy login` 子命令，交互登录只在 TUI 的 `/login` 里，
   token 存**系统密钥环**（容器里通常没有）。无头部署要用 `CODEBUDDY_API_KEY`
   （并选对 `CODEBUDDY_INTERNET_ENVIRONMENT`）或 `CODEBUDDY_AUTH_TOKEN`。

---

## 2. 调研到的事实（证据都在包内）

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

### 2.6 鉴权与模型路由（本方案的核心差异点）

| 路径 | 机制 | 平台对接方式 |
| --- | --- | --- |
| 官方（账号登录） | **没有 `codebuddy login` 子命令**（2.185.0 的 Commands 列表里没有）；交互式 TUI 里用斜杠命令 `/login`，选 Chinese / International / Enterprise / iOA 站点后浏览器授权。token 由内核交给**系统密钥环**（macOS Keychain、Linux 上 GNOME Keyring / KWallet、Windows 凭据管理器），不是 `~/.codebuddy/.credentials.json` | `gateway_id=null`：不注入任何 env，CLI 自己读本机登录态 |
| 官方（环境变量） | `CODEBUDDY_API_KEY` / `CODEBUDDY_AUTH_TOKEN` / `apiKeyHelper`（优先级 AUTH_TOKEN > apiKeyHelper > API_KEY）。**用 API Key 必须同时选对站点**：中国版 `CODEBUDDY_INTERNET_ENVIRONMENT=internal`、iOA=`ioa`、专享版=`cloudhosted`、私有化=`selfhosted`；不设则打国际站 `www.codebuddy.ai` | 走智能体的「环境变量」或适配器的「官方网络环境」字段；**不能**用 `CODEBUDDY_BASE_URL` 当平台网关（会被拼成 `<base>/v2/chat/completions`，自家协议） |
| 第三方模型 | `models.json`：`{models:[{id,url,apiKey,...}], availableModels}`；`url` 必须是**完整** `/chat/completions` 路径；`apiKey` 支持 `${ENV}` 引用；鉴权固定 `Authorization: Bearer` | `gateway_url + "/chat/completions"` 写成 `url`，密钥写 `${CODEBUDDY_API_KEY}`；`--model` 选注入的 id |

`models.json` 的读取路径：`$CODEBUDDY_CONFIG_DIR/models.json`（用户级）、
`<workspace>/.codebuddy/models.json`（项目级，优先）。**仅支持 OpenAI 兼容接口**
（文档明说；另有 `api: "openai-responses"` 变体）。Anthropic / Gemini 协议的网关接不了，
适配器要在构建时给出明确的中文报错。

其他有用的环境变量：`DISABLE_TELEMETRY` / `DISABLE_ERROR_REPORTING` /
`DISABLE_AUTOUPDATER`（无人值守场景建议默认关掉遥测与自动更新）。

---

## 3. 适配器设计

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
  2. 配置目录里存在**非空**的凭据缓存（`.credentials.json` / `credentials.json`）→ `True`；
  3. 都没有 → `False`，detail 里给出「TUI `/login` / API Key + 站点 / AUTH_TOKEN」三条修法。
- 内核**没有** `login` 子命令，所以不声明 `login_argv`：平台不提供「登录」按钮，
  只展示登录状态（`supports_auth()` 让内核管理卡片显示这一行）。

---

## 4. 需要动的文件

| 文件 | 改动 |
| --- | --- |
| `backend/app/agents/builtin/codebuddy_cli.py` | **新增**适配器（唯一的功能代码） |
| `backend/tests/test_adapters.py` | 注册表断言 + invocation/parse/权限/env/models.json 用例（样本用包内文档里的真实输出形状） |
| `backend/tests/test_architecture.py` | `BUILTIN_IDS` 加 `"codebuddy-cli"` |
| `frontend/public/icons/kernels/codebuddy.webp` | **新增**真彩色 Logo（`logo_style = "image"`，原色显示） |
| `README.md` 「都能接什么」表 | 加一行 `codebuddy-cli` |
| `docs/codebuddy-cli.md` | 本方案（调研依据） |

不需要改的：`frontend` 表单/内核列表（全部由后端 descriptor 驱动）、事件协议、
runner / skills / mcp 物化层、Docker 镜像（npm 内核走 provisioner；镜像自带 Node 24 满足要求）。

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
5. **登录态不可移植**：官方链路依赖服务所在机器上的一次交互式 `/login`，
   而 token 由内核交给**系统密钥环**——容器 / 无桌面环境里通常没有密钥环，
   交互登录自然也无法长期保存。这类部署应引导用户改用环境变量凭据
   （`CODEBUDDY_API_KEY` + 正确的 `CODEBUDDY_INTERNET_ENVIRONMENT`，或 `CODEBUDDY_AUTH_TOKEN`），
   而不是绑一个 OpenAI 网关（那是另一条链路，计费与模型清单都不同）。
   隔离家目录会尽力携带 `settings*.json` 与凭据缓存；密钥环不随 `CODEBUDDY_CONFIG_DIR` 隔离。
6. **`--strict-mcp-config` 的语义以实测为准**：旗标在 CLI 参数解析里确认存在，
   但「是否真的忽略 user/project 配置」要在 P1 验收时实测一次（拿一个本机配置的
   MCP 服务器验证它没被加载）。
7. **没有「仅工作目录」档**：CodeBuddy 没有 Claude Code 的
   `blockReadsOutsideWorkingDirectories` 一类设置，硬造一个只会给出虚假的安全承诺，
   因此本适配器不声明 `file_access_scope` 字段。需要强隔离请在容器级做。

---

## 6. 落地顺序

**P0 — 跑通官方链路（1 个文件 + 测试）**
适配器骨架（invocation / parse_line / 权限 / root 逃生口 / env）、Logo、README 表格、
`test_adapters.py` 用真实 stream-json 样本的解析用例 + `test_architecture.py` 注册表用例。
验收：本机（或容器内）完成官方授权后，平台里新建 CodeBuddy 智能体 → 对话、流式、
工具调用、多轮续接全部可用；`内核管理` 页能安装 / 更新 / 卸载，并显示登录状态。

**P1 — 技能与 MCP（与 claude 同构）**
`skill_roots / discover_skills / skill_mount / skill_invocation` 与
`discover_mcp_servers / mcp_mount`，隔离目录 `CODEBUDDY_CONFIG_DIR` 实测：
技能分配 → 只有分配的技能可见；MCP 分配 → `--strict-mcp-config` 下本机配置不漏进来。

**P2 — 自定义网关（models.json 路由）**
`build_invocation` 写 `models.json` + 协议校验报错 + `system_defaults()` 读本机配置。
验收：绑一个 OpenAI 兼容网关（如 OpenRouter）→ `--model` 选中注入的模型 →
对话走网关密钥，`usage` 事件正常回报。

**P3（可选）— 增强项**
长驻模式（`--input-format stream-json` + `stdin_hold`，像 codex app-server 那样常驻
进程省掉每次冷启动）、`--json-schema` 结构化输出、后台任务事件（`task_*`）映射成
平台的工具事件。
