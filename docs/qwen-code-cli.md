# 接入 Qwen Code CLI 的方案

> 调研对象：阿里 Qwen Code CLI（npm 包 `@qwen-code/qwen-code`，可执行文件 `qwen`）。
> 本文回答三件事：它是什么、平台怎么接、按什么顺序落地。
> 结论全部来自**本机实装与实跑**（0.28.4，Linux x64，假 OpenAI 服务回环实测），不是文档摘抄。

---

## 1. 结论先行

**Qwen Code 的 headless 协议与 Claude Code 几乎逐字对齐**（连 `control_request` /
`control_response` 都有），接入它 ≈ 复制一份 `claude_code.py` 适配器，改掉内核名、
配置目录、鉴权方式三件事。核心链路（`-p ""` + stdin、`--output-format stream-json` +
`--resume`、`--mcp-config`、`SKILL.md` 技能目录）全部现成，不需要任何新框架能力、
不需要新增事件类型。

它的独有价值是**多协议内核**：`AuthType` 枚举里 `openai` / `openai-responses` /
`anthropic` / `gemini` / `vertex-ai` / `qwen-oauth` / `qwen-subscription` 并存，
第三方模型直接用 `OPENAI_API_KEY` + `OPENAI_BASE_URL` + `OPENAI_MODEL` 三件套就能跑
（实测通过），不需要像 CodeBuddy 那样写 `models.json`。

主要差异有三点：

1. **配置目录没有 `QWEN_CONFIG_DIR`，只有 `QWEN_HOME`，且技能根有两个**。
   技能根是 `$QWEN_HOME/skills` **加上 `$HOME/.agents/skills`**（`SKILL_PROVIDER_CONFIG_DIRS =
   [".qwen", ".agents"]`，后者用 `os.homedir()` 解析）——只设 `QWEN_HOME` 隔离不住，
   本机 `~/.agents/skills` 的技能会漏进来（实测确认）。**解法是把 `HOME` 整个指向隔离目录**：
   `HOME=<隔离家>` 后，配置目录自动变成 `<隔离家>/.qwen`，`<隔离家>/.agents/skills` 也被隔离，
   实测泄漏消失。这是 Qwen 适配器与其它 npm 内核最大的不同。
2. **官方登录是标准 OAuth 设备码流程**（`qwen auth` 子命令已被移除，交互式走 TUI 的
   `/auth`）。设备码端点是公开常量（`chat.qwen.ai/api/v1/oauth2/device/code`，PKCE S256），
   助手脚本可以直接实现「发起设备码 → 出二维码 → 轮询 token → 凭据落盘」，
   比 Antigravity 的 60 秒 OOB 授权码窗口从容得多。凭据落在
   `getGlobalQwenDir()/oauth_creds.json`（跟随 HOME，不在系统密钥环）。
3. **`-m` 必须与注入的模型 id 一致**：模型名不在 `modelProviders` 里时，内核会把它当作
   官方 qwen 模型打到阿里云端点（实测 401 Incorrect API key）。自定义网关的模型要写进
   隔离目录的 `settings.json`（`modelProviders` + `model.name`），`--model` 传同一个 id。

---

## 2. 调研到的事实（实测）

来源：npm 包 `@qwen-code/qwen-code@0.28.4`（tgz 44MB / 解压 163MB，2026 年版本），
仓库 https://github.com/QwenLM/qwen-code ，官方文档 https://qwenlm.github.io/qwen-code-docs/ 。

### 2.1 安装与可执行文件

| 项 | 值 |
| --- | --- |
| npm 包 | `@qwen-code/qwen-code` |
| bin | `qwen`（入口 `cli-entry.js`，另有 `cli.js`） |
| Node 要求 | `engines: ">=22"`；启动时对 Node 24.0.0–24.22.1 打**内存警告**（建议 ≥26.12.1，见包内 issue #15165），不阻断运行。Docker 镜像现用 Node 24.22.3，会看到警告但一切正常 |
| 版本探测 | `qwen --version` → `0.28.4` |
| 安装体积 | tgz 44MB / 解压 163MB（CodeBuddy 的一半） |
| 原生依赖 | optionalDependencies：bun、node-pty、clipboard、sharp —— 装不上不阻断（警告「install scripts not yet covered by allowScripts」，实测无碍） |
| 更新 | `qwen update`，或 npm 重装 |

### 2.2 headless 协议（接入主路径）

```bash
qwen -p "" -o stream-json --approval-mode yolo
# 提示词从 stdin 进（与 gemini 适配器的 Invocation.stdin 用法一致）
```

- `-p/--prompt` 已标记 deprecated（推荐位置参数），但 `-p ""` + stdin 实测可用，
  与平台 runner 现状对齐，先沿用。
- 输出逐行 JSONL，事件形状与 Claude Code 相同：

| `type` | 内容 | 平台映射 |
| --- | --- | --- |
| `system` / `subtype:"init"` | `session_id`、`tools`、`mcp_servers`、`model`、`permission_mode`、`slash_commands`、`qwen_code_version` | `session` + `log` |
| `stream_event` | `content_block_delta`（`text_delta` / `input_json_delta`）+ `message_start/stop` + **`goal_state`**（Qwen 特有） | `text_delta` / `thinking_delta`；其余忽略 |
| `assistant` | `message.content[]`（`text` / `tool_use` 块，带 `usage`） | 补发全文 + `tool_call` |
| `user` | `message.content[]`（`tool_result` 块，`tool_use_id` / `is_error` / `content`） | `tool_result` |
| `result` | `subtype:"success"`、`is_error`、`duration_ms`、`num_turns`、`result`、`usage`、`permission_denials`；出错时 `error.message` | `usage` + 运行结果 |

- 会话续接：`--resume <session-id>`（实测有效）；另有 `-c`（最近一次）、`--session-id`（指定新 id）、`--fork-session`。
- 会话落盘：`<配置目录>/projects/<工作目录哈希>/chats/<session>.jsonl` —— 在隔离目录内，多轮会话天然按对话隔离。
- 其他可用旗标：`--system-prompt` / `--append-system-prompt`（原生支持，实测 `--system-prompt` 是**替换式**且生效）、
  `--allowed-tools`（array，bypass 确认）、`--mcp-config`（JSON 字符串或文件路径）、
  `--allowed-mcp-server-names`、`--disable-mcp-server`、`--json-schema`（headless 结构化输出）、
  `--json-file` / `--json-fd`（双输出模式：TUI 照常渲染，JSONL 写另一条流）、
  `--sandbox` / `--sandbox-image`、`--worktree <slug>`、`--bare`（跳过隐式发现）、`--safe-mode`（禁用一切自定义）。
- 权限模式：`--approval-mode plan | default | auto-edit | auto | yolo`（`-y` 是 `yolo` 的别名，
  **与 `--approval-mode` 同用会报错**，二选一）。headless 无授权弹窗，默认 `yolo`。

### 2.3 配置目录与隔离（本方案核心差异点）

- 全局配置目录：`getGlobalQwenDir()` = **`QWEN_HOME`**（直接作为配置根，与 Gemini 的
  `GEMINI_CLI_HOME`「主目录下还有一层 .gemini」不同），未设时 `~/.qwen`。
- 结构：`settings.json`、`skills/`、`oauth_creds.json`、`oauth_creds.lock`、
  `projects/<hash>/chats/`（会话）、`usage/`（用量 JSONL）、`memories/`（Auto-Memory）、
  `output-language.md`、`extensions/`、`tmp/`、`debug/`。
- 项目级：工作目录下的 `.qwen/`（`settings.json`、`skills/`、`PROJECT_SUMMARY.md`）。
- **技能根有两个**：`SKILL_PROVIDER_CONFIG_DIRS = [".qwen", ".agents"]`，
  解析时 `.qwen` 用 `getGlobalQwenDir()/skills`（尊重 QWEN_HOME），
  `.agents` 用 `os.homedir()/.agents/skills`（**只认 HOME**）。实测：只设 `QWEN_HOME`
  时 `~/.agents/skills/leak-test` 照样加载；设 `HOME=<隔离目录>` 后消失。
- **隔离方案 = 设 `HOME`**（而不是 QWEN_HOME）：配置目录变成 `<HOME>/.qwen`，
  技能的第二个根也落进隔离目录，`oauth_creds.json` 同样跟随。实测布局：

```
<隔离家>/
  .qwen/
    settings.json          # 平台写：鉴权 / 网关 / 技能开关
    skills/<slug>/SKILL.md # 分配到的技能
    oauth_creds.json       # 登录态（跟随 HOME）
    projects/…/chats/…     # 会话记录
```

- 内置 Auto-Memory 会自动建 `memories/` 并往 system prompt 注入记忆目录说明，
  `output-language.md` 也是自动生成的——这是 Qwen 的默认行为，不改。

### 2.4 技能

- 用户级：`<配置目录>/skills/<slug>/SKILL.md`，格式与 Claude Code 相同
  （frontmatter：`name` / `description` / `allowedTools` / `argument-hint`）。
- 项目级：`<工作目录>/.qwen/skills/`。
- 调用：内置 `skill` 工具（**小写**，参数 `skill` = 技能名）——Claude 是 `Skill` 大写，注意。
- **自带技能可以关掉**（比 CodeBuddy 强）：settings 里
  `"skills": {"disabledLevels": ["bundled", "project"]}` 实测把 18 个 bundled 技能
  （review / batch / simplify / stuck / loop / new-app / dataviz…）与项目级技能全部关掉；
  `disabledLevels` 支持 `project / user / extension / bundled` 四档，UNION 合并。
- bundled 技能目录在 npm 包内 `bundled/`（17 个目录，每个一个 SKILL.md）。

### 2.5 MCP

- 配置来源：用户级 `<配置目录>/settings.json` 的 `mcpServers`、项目级
  `<工作目录>/.qwen/settings.json` 的 `mcpServers`（实测两者都会加载）。
- 严格仅分配：**没有 `--strict-mcp-config`**（Claude Code 同名旗标不存在），
  用 `--mcp-config <file> --allowed-mcp-server-names <分配到的名字…>` 达成：
  实测只传 allowed 名单时，用户级配置里的其它服务器**不出现在** `init.mcp_servers`，
  工具也不会注册。
- `--disable-mcp-server <name>` 实测**不生效**（init 列表里仍显示）——不要用它做隔离。
- `qwen mcp add/remove/list` 子命令存在（管理本机配置），平台不用它，直接物化文件。

### 2.6 鉴权与模型路由（本方案的核心差异点）

`AuthType` 枚举（包内 `chunk-EW6HVTD5.js`）：
`openai` / `openai-responses` / `anthropic` / `gemini` / `vertex-ai` / `qwen-oauth` / `qwen-subscription`。

| 路径 | 机制 | 平台对接方式 |
| --- | --- | --- |
| **第三方 / 平台网关（openai）** | `OPENAI_API_KEY` + `OPENAI_BASE_URL` + `OPENAI_MODEL` 三件套**无需 settings 即可推断 auth type**（实测 init 成功）；也可写 `settings.json`：`security.auth.selectedType: "openai"` + `modelProviders.openai[]`（`{id, name, baseUrl, envKey, generationConfig}`）+ `model.name`。baseUrl 是**字面拼接**：给 `http://h/v1` 打 `/v1/chat/completions`，给 `http://h` 打 `/chat/completions`（实测），不自动补 `/v1` | `ctx.gateway_url + "/chat/completions"` 直接可用（平台网关约定形如 `https://host/v1`）；密钥走 `api_key_env = "OPENAI_API_KEY"` 注入进程 env；`--model` 传与 `model.name` / 注入 id 一致的名字 |
| 官方订阅（qwen-oauth） | OAuth **设备码流程**（PKCE S256）：`POST https://chat.qwen.ai/api/v1/oauth2/device/code`（form：`client_id=f0304373b74a44d2b584a3fb70ca9e56`、`scope=openid profile email model.completion`、`code_challenge`、`code_challenge_method=S256`）→ `verification_uri_complete`；轮询 `POST .../oauth2/token`（form：`grant_type=urn:ietf:params:oauth:grant-type:device_code`、`client_id`、`device_code`、`code_verifier`，pending 时 `status=pending`）。凭据 `{access_token, token_type, refresh_token, resource_url, expiry_date}` 落 `<配置目录>/oauth_creds.json` | `gateway_id=null`：不注入 env，CLI 自己读本机登录态；「登录」按钮由 `login_argv` 指向 `qwen_login.py` 助手（见 §3.6） |
| 官方 API Key | `QWEN_API_KEY` / `QWEN_BASE_URL` **单独设置不管用**（实测报 "No auth type is selected"）——需要 auth type 才生效。百炼 Coding Plan 另有 `BAILIAN_CODING_PLAN_API_KEY`（中国站 `https://coding.dashscope.aliyuncs.com/v1` / 国际站 `coding-intl...`） | 走智能体的「环境变量」并**同时**配 `OPENAI_MODEL`，或让用户绑平台网关；不播种官方网关 |
| Anthropic / Gemini 协议网关 | `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL`、`GEMINI_API_KEY` 等（包内 `PREFLIGHT_ENV_KEYS`） | P3 再考虑；第一版只做 openai 协议网关 + 官方 OAuth 两条链路 |

- **模型优先级**：`-m <cli 参数>` > settings 的 `model.name`；`-m` 给了不在
  `modelProviders` 里的名字时**不会报错**，而是按官方 qwen 模型打到阿里云端点
  （实测 401 Incorrect API key）——适配器必须保证注入的 id 与 `--model` 一致。
- 官方 OAuth 可用模型：包内 `QWEN_OAUTH_MODELS` 目前是 `coder-model`（Qwen 3.7 Max）；
  订阅型默认 `qwen3.8-max`，openai 型默认 `qwen3.7-max`（`MAINLINE_CODER_MODEL`）。
- 登录态判断：`oauth_creds.json` 存在且有 `access_token` / `refresh_token` → 已登录
  （还有 `oauth_creds.lock` 防并发刷新）。

### 2.7 平台网关写法（settings.json 注入）

与 CodeBuddy 的 `models.json` 不同，Qwen 的自定义 provider 走 **settings.json**：

```json
{
  "security": {"auth": {"selectedType": "openai"}},
  "modelProviders": {
    "openai": [{
      "id": "<ctx.model>",
      "name": "<ctx.model>",
      "baseUrl": "<ctx.gateway_url>/chat/completions",
      "envKey": "OPENAI_API_KEY",
      "generationConfig": {"contextWindowSize": 100000}
    }]
  },
  "model": {"name": "<ctx.model>"},
  "skills": {"disabledLevels": ["bundled", "project"]}
}
```

- `envKey: "OPENAI_API_KEY"`：密钥不落盘，通过进程 env 注入（包内 `readSettingsEnv`
  也支持 settings 顶层 `env` 字段，但走进程 env 更干净）。
- 写入位置：隔离目录的 `.qwen/settings.json`（跟随 HOME）。`Invocation.files` 只写
  运行目录，所以这份配置必须由框架的 `skill_mount().patch` / `mcp_mount().files`
  机制物化进隔离家目录（见 §3.5）。
- baseUrl 拼接规则（实测）：平台网关 `https://host/v1` → 直接拼 `/chat/completions`。
  若网关地址本身以 `/chat/completions` 结尾，内核**不会**去重（与 CodeBuddy 的
  normalizeChatCompletionsUrl 不同）——适配器构建时先去尾。

---

## 3. 适配器设计

新建 `backend/app/agents/builtin/qwen_cli.py`，骨架与 `claude_code.py` 对齐：

```
QwenCliAgent(AgentDefinition)
  id            = "qwen-cli"
  name          = "Qwen Code"
  vendor        = "Alibaba"
  executable    = "qwen"
  logo          = "/icons/kernels/qwen.webp"      # 真彩色位图，logo_style = "image"
  runtime       = Runtime.CLI
  protocol      = "openai"           # 第一版自定义网关只接 OpenAI 兼容
  api_key_env   = "OPENAI_API_KEY"   # 三件套之一，密钥经框架合并进子进程 env
  base_url_env  = None               # 故意不声明：OPENAI_BASE_URL 属于「官方直连」语义，
                                     # 平台网关走 settings.json 的 modelProviders，不混用
  default_gateway = None             # 官方链路是「本机设备码登录」，不播种官方网关
  default_model = "coder-model"      # OAuth 链路的默认模型
  docs_url      = "https://qwenlm.github.io/qwen-code-docs/"
  provisioner   = RuntimeProvisioner(
      manager="npm", package="@qwen-code/qwen-code",
      install_argv=npm_install_argv("@qwen-code/qwen-code", bin_name="qwen"),
      update_argv =npm_install_argv("@qwen-code/qwen-code", bin_name="qwen", latest=True),
      uninstall_argv=npm_cli("uninstall", "-g", "@qwen-code/qwen-code"),
      latest_argv=npm_cli("view", "@qwen-code/qwen-code", "version"),
      prepare_argv=npm_prepare_argv("@qwen-code/qwen-code"),
      homepage="https://github.com/QwenLM/qwen-code",
      note="登录：点「登录」会出现设备码授权链接与二维码，浏览器打开 chat.qwen.ai "
           "登录 Qwen 账号并确认配对码即可（登录态保存在服务所在机器上）。"
           "自定义网关走 OpenAI 兼容协议。",
      login_argv=[sys.executable or "python3", str(_LOGIN_HELPER)],
  )
  capabilities  = Capabilities(streaming, sessions, native_system_prompt,
                               tools, mcp, permission_modes, working_directory, skills)
```

> `default_gateway=None` 的含义与 CodeBuddy 相同：启动播种跳过，Qwen 出现在
> 「新建智能体」内核下拉里；用户要么用「官方默认」（本机设备码登录），
> 要么绑一个 OpenAI 协议的自定义网关。

### 3.1 config_fields（中文 label，前端 DynamicForm 自动渲染）

| key | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `approval_mode` | SELECT | `yolo` | plan / default / auto-edit / auto / yolo。headless 下没有授权弹窗，默认给能真正干活的档；帮助文案注明风险（与 claude 适配器措辞一致） |
| `system_prompt_mode` | SELECT | `replace` | replace → `--system-prompt`；append → `--append-system-prompt`。内核原生支持，不需要 Gemini 式的文件注入 |
| `partial_messages` | BOOLEAN | `True` | 控制 `--include-partial-messages` |
| `allowed_tools` | STRING_LIST | — | → `--allowed-tools`（可重复 / 逗号分隔，内核自己拆） |
| `extra_args` | STRING_LIST | — | 兜底旗标 |
| `env` | KEYVALUE | — | 框架已支持 |

### 3.2 build_invocation()

```python
argv = ["qwen", "-p", "", "--output-format", "stream-json"]
mode = options.get("approval_mode") or "yolo"
argv += ["--approval-mode", mode]           # -y 与 --approval-mode 同用会报错，只走后者
if partial_messages: argv.append("--include-partial-messages")
if ctx.model: argv += ["-m", ctx.model]
if ctx.system_prompt:
    argv += ["--system-prompt" if replace else "--append-system-prompt", ctx.system_prompt]
if ctx.session_id: argv += ["--resume", ctx.session_id]
if allowed_tools: argv += ["--allowed-tools", *as_list(...)]
# MCP 严格仅分配：分配到的服务器物化成文件 + 白名单收窄
if ctx.mcp_servers:
    argv += ["--mcp-config", str(Path(ctx.home_dir) / ".." / "mcp.json"),   # 见 3.5
             "--allowed-mcp-server-names", *[s.slug for s in ctx.mcp_servers]]
argv += extra_args

env = self.effective_env(ctx)               # OPENAI_API_KEY（api_key_env）+ 用户自定义 env
env.setdefault("QWEN_CODE_SUPPRESS_YOLO_WARNING", "1")   # 关掉 stderr 的 yolo 警告
env.setdefault("CI", "1")
if ctx.home_dir:
    env["HOME"] = ctx.home_dir              # 隔离键：Qwen 的技能第二根与凭据都认 HOME
    env.pop("QWEN_HOME", None)              # 不设 QWEN_HOME，让配置目录 = HOME/.qwen
return Invocation(argv=argv, env=env, cwd=ctx.workspace_dir, stdin=ctx.prompt)
```

要点：

- **`HOME` 是隔离键**（QWEN_HOME 不够，见 §2.3）；`effective_env` 之后覆盖。
- 隔离目录里没有本机 `oauth_creds.json`：**不 carry**。官方登录态要进隔离目录，
  由登录助手显式写入「平台统一的登录家」（见 §3.6），多对话共用一份；
  这与 CodeBuddy「session 跟 HOME 走」的实际行为一致。
- `-p ""` 的 deprecated 警告会打到 stderr，解析器照常忽略非 JSON 行。
- `--mcp-config` 接受文件路径；`--allowed-mcp-server-names` 是数组旗标，逐个传。

### 3.3 网关路由：patch 隔离目录的 settings.json

`ctx.gateway_url`（OpenAI 协议约定形如 `https://host/v1`）+ `ctx.api_key` 时，
适配器在 `skill_mount().patch` / `mcp_mount()` 里准备一份 `settings.json` 内容
（物化机制见 §3.5）：

- `security.auth.selectedType = "openai"`；
- `modelProviders.openai[0]`：`id`/`name` = `ctx.model`，`baseUrl` = 网关地址
  先去掉 `/chat/completions` 尾缀再拼回去，`envKey = "OPENAI_API_KEY"`；
- `model.name = ctx.model`；
- `skills.disabledLevels = ["bundled", "project"]`（严格仅分配的一部分）。

没有网关（官方 OAuth 链路）时**不写**这些键，只保留 `skills` 开关 —— 内核回落到
本机登录态与官方模型。网关协议不是 `openai` 时：构建阶段 `yield` 一条中文
`log`/`error`（「Qwen Code 适配器第一版只支持 OpenAI 兼容网关」），不要静默失败。

### 3.4 parse_line()

照抄 `claude_code.py` 的 `_dispatch`（`system/init` → session、`stream_event` → 增量、
`assistant` → 补发全文 + tool_call、`user` → tool_result、`result` → usage + is_error），
额外处理两个差异：

1. `stream_event` 里混有 `goal_state`、`message_start` / `message_stop` 等非
   `content_block_*` 事件：只处理 `content_block_delta`（`text_delta` /
   `thinking_delta`；`input_json_delta` 属于工具参数，由 `assistant` 补发，忽略），
   其余丢弃。
2. `result.usage` 字段名与 Claude 一致（`input_tokens` / `output_tokens` /
   `cache_read_input_tokens` / `total_tokens`），照常透传。

### 3.5 技能与 MCP

```python
def skill_roots(self):
    config = _qwen_config_dir()          # $QWEN_HOME 或 ~/.qwen（读本机真实配置时用）
    return [
        SkillRoot(str(config / "skills" / ".skillhub"), source_kind=MARKET_SOURCE),
        SkillRoot(str(config / "skills"), source_kind="user"),
        SkillRoot(str(Path.home() / ".agents" / "skills"), source_kind="user"),
    ]

def skill_mount(self, ctx):
    return SkillMount(
        home_name="qwen-home",            # 物化目录 = <对话技能主目录>/qwen-home
        skills_subdir=".qwen/skills",     # 分配到的技能 → <家>/.qwen/skills
        patch={"settings.json": {...}},   # 见 3.3：写入 <家>/.qwen/settings.json
        notes=[...],                      # 工作目录 .qwen/skills 已被 disabledLevels 挡住的说明
    )
```

**home 目录的层级换算**：框架把隔离家物化在 `<agent-homes>/<对话 id>/qwen-home`，
适配器把 `ctx.home_dir` 作为 **HOME** 注入，内核自己的配置目录就是
`<ctx.home_dir>/.qwen`。因此：

- `SkillMount.skills_subdir = ".qwen/skills"`（技能落 `<家>/.qwen/skills`）；
- `McpMount.files = {"mcp.json": to_json_config(ctx.mcp_servers)}` 写在家根
  （`build_invocation` 里以 `<家>/mcp.json` 传给 `--mcp-config`）；
- `patch={"settings.json": ...}` —— 需要确认框架的 patch 目标是
  `<家>/<文件名>` 还是 `<家>/.qwen/settings.json`：**落地时二选一**，
  若框架只支持家根下的文件名，就把 patch 键写成 `".qwen/settings.json"`
  （与 `skills_subdir` 同理；`runtime/skills.py` 的物化逻辑支持子目录路径）。
- `skill_invocation`：工具名是 `skill`（**小写**）：

```python
def skill_invocation(self, tool_name, tool_input):
    if tool_name != "skill":
        return None
    return str(tool_input.get("skill") or "").strip() or None
```

- `discover_mcp_servers()`：读 `<配置目录>/settings.json` 的 `mcpServers`
  （尊重 QWEN_HOME / HOME，尽力而为）→ `parse_json_servers`。
- 架构守卫：`KERNEL_SKILL_MARKERS` 需要加 `".qwen/skills"` 与 `"QWEN_HOME"`；
  `BUILTIN_IDS` 加 `"qwen-cli"`。

### 3.6 设备码登录助手（`qwen_login.py`）

官方 OAuth 是**设备码流程**，助手不需要拉起内核进程，直接 HTTP 就能完成：

| 步骤 | 实现 |
| --- | --- |
| 发起 | `POST https://chat.qwen.ai/api/v1/oauth2/device/code`（form：`client_id`、`scope`、`code_challenge`（PKCE S256，`hashlib.sha256 + base64url`）、`code_challenge_method=S256`）→ `device_code` / `user_code` / `verification_uri` / `verification_uri_complete` / `interval` / `expires_in` |
| 出码 | `segno.make(verification_uri_complete).save(png)`，打印 `QR_IMAGE=<绝对路径>`；同时打印 `INPUT_PROMPT=` 不需要——设备码**无需回填任何东西**，用户在浏览器里确认配对码即可（比 Antigravity 的 OOB 体验好） |
| 轮询 | 每 `interval` 秒 `POST .../oauth2/token`（form：`grant_type=urn:ietf:params:oauth:grant-type:device_code`、`client_id`、`device_code`、`code_verifier`）；`status=pending` 继续等；`access_token` 到手即成功退出 0 |
| 落盘 | 把 `{access_token, token_type, refresh_token, resource_url, expiry_date}` 写进**平台统一的登录家**（见下），0600 |
| 收尾 | 删二维码（短期凭据）与临时文件；超时 / `access_denied` / `expired_token` 退出 1 |

**登录家与隔离目录的关系**：凭据落在 `getGlobalQwenDir()/oauth_creds.json`，
而隔离运行时 `HOME=<隔离家>`，所以内核读的是 `<隔离家>/.qwen/oauth_creds.json`。
登录助手需要把凭据同时写两处：

1. 宿主机真实位置（`~/.qwen/oauth_creds.json`）——让本机裸跑内核也能用；
2. 每个隔离家目录的 `.qwen/oauth_creds.json`——但隔离家按对话建、运行前才存在，
   助手写不到「未来的家」。

所以采用 **carry**：`skill_mount().carry = [(登录家凭据, ".qwen/oauth_creds.json")]`
——框架物化隔离家时把宿主机凭据复制进去，官方链路一进隔离目录就是已登录状态。
助手只写宿主机真实位置这一处。登录前需要配置 `QWEN_HOME` 吗？不需要：
助手直接对宿主机 `~/.qwen` 操作，`auth_status()` 也读同一处（尊重 QWEN_HOME）。

> **设备码端点在沙箱内无法实测**（本机无外网），上面的请求参数是从包内实现
> 抄录的；落地 P0 时先用 curl 过一遍真实端点，确认响应字段
> （`verification_uri_complete` 是否存在、`interval` 默认值）再写死。

### 3.7 probe / system_defaults / auth_status

- `probe()`：默认的「可执行文件 + 密钥」检查够用；可加 `qwen --version` 成功即就绪。
- `system_defaults()`：读本机真实配置（`$QWEN_HOME` 或 `~/.qwen` 的 `settings.json`）：
  `model.name` → `model`；`modelProviders.openai[0].baseUrl` + `envKey` 对应的
  进程 env → `gateway_url` / `api_key`。**不回填**官方 OAuth 的链路（那是本机登录态，
  不是网关）。
- `auth_status()`：只做配置层判断，不联网：
  1. 进程 env 或智能体凭据里有 `OPENAI_API_KEY` → `logged_in=None`（已配置密钥，
     平台不校验有效性）；
  2. `<配置目录>/oauth_creds.json` 有 `access_token` / `refresh_token` → `True`，
     detail 带上 `expiry_date` 是否过期提示；
  3. 都没有 → `False`，detail 给出「点登录按钮完成设备码授权 / 绑 OpenAI 兼容网关 /
     填 OPENAI_API_KEY 三件套」三条修法。

### 3.8 多账号（P3，可选）

Qwen 是单账号模型（`oauth_creds.json` 一份），平台侧「账号槽位」方案与
`codebuddy_accounts.py` 同构：登录成功后把凭据文件复制进槽位、切换时原样写回。
凭据是明文 JSON（不像 CodeBuddy 有本机密钥加密），**槽位文件必须 0600 且放在
数据目录内**（受 `app/security.py` 管辖，不进 git）。第一版不做，保留扩展点。

---

## 4. 需要动的文件

| 文件 | 改动 |
| --- | --- |
| `backend/app/agents/builtin/qwen_cli.py` | **新增**适配器（唯一的功能代码） |
| `backend/app/agents/builtin/qwen_login.py` | **新增**设备码登录助手（`login_argv` 指向它，输出 `QR_IMAGE=`） |
| `backend/tests/test_adapters.py` | 注册表断言 + invocation/parse/权限/env/settings 注入用例（用真实 stream-json 样本） |
| `backend/tests/test_architecture.py` | `BUILTIN_IDS` 加 `"qwen-cli"`；`KERNEL_SKILL_MARKERS` 加 `".qwen/skills"` / `"QWEN_HOME"` |
| `backend/tests/test_qwen_login.py` | 登录助手：PKCE、二维码、假服务主链路 |
| `backend/requirements.txt` | 不动（`segno` 已在 CodeBuddy 登录时引入，复用） |
| `frontend/public/icons/kernels/qwen.webp` | **新增**真彩色 Logo（`logo_style = "image"`），来源登记进 `SOURCES.md` |
| `README.md` 「都能接什么」表 | 加一行 `qwen-cli` |
| `docs/qwen-code-cli.md` | 本方案（调研依据） |

不需要改的：事件协议、runner / skills / mcp 物化层、Docker 镜像（Node 24.22.3 满足
`>=22`，会有内存警告但不阻断；若介意可在镜像注释里记一笔）、前端（DynamicForm /
内核列表仍由 descriptor 驱动）。

---

## 5. 风险与已知限制

1. **技能第二根认 HOME 不认 QWEN_HOME**：只设 `QWEN_HOME` 隔离不住 `~/.agents/skills`。
   方案用 `HOME` 整体隔离解决；副作用是子进程看到的家目录变了——git / npm 缓存等
   会落到隔离家（`.gitconfig` 不在就不会有），隔离家在数据目录卷内，可接受。
   需要留意 runner 其它子进程若共享 env，不要把 HOME 泄漏出去（`Invocation.env`
   是逐次运行的，天然隔离）。
2. **没有 `--strict-mcp-config`**：MCP 隔离靠 `--allowed-mcp-server-names` 白名单。
   用户级 / 项目级配置里的其它服务器不会进 `init.mcp_servers`（实测），但
   `--disable-mcp-server` 旗标无效（实测），文档里写清楚别用错。
3. **`-m` 跑偏风险**：模型名不在注入的 `modelProviders` 里时会静默打到阿里云官方端点。
   适配器保证 `settings.modelProviders[].id` 与 `--model` 同源（都取 `ctx.model`）；
   若用户手改 `extra_args` 里的 `-m`，属用户自己绕过。
4. **设备码端点未经真实外网验证**：沙箱无外网，请求参数抄自包内实现；
   P0 落地时先 curl 一遍真实端点再写死字段名。
5. **Node 版本警告**：Node 24.22.x 会有「高临时内存占用」警告（建议 ≥26.12.1），
   不阻断；镜像升级 Node 到 26 前先在 README 记一笔。
6. **Auto-Memory 与 output-language 自动生成**：内核会在配置目录写 `memories/` 与
   `output-language.md` 并注入 system prompt。第一版接受（这是它的产品行为）；
   若后续要关，研究 settings 的 memory 配置项再动。
7. **`-p` deprecated**：`-p ""` + stdin 现在可用，未来移除时改用位置参数
   （`qwen "<提示>" -o stream-json`），stdin 仍会拼接。
8. **官方 OAuth 的模型清单窄**：包内 `QWEN_OAUTH_MODELS` 目前只有 `coder-model`
   （Qwen 3.7 Max）；订阅型 `qwen3.8-max`。默认模型按 `coder-model` 声明，
   用户可在智能体里改。
9. **登录态跟着隔离家走**：`oauth_creds.json` 在 `<家>/.qwen/` 下，由 carry 从宿主机
   复制进来；宿主机重新登录后，**存量对话**的隔离家不会自动更新（框架每次运行重建
   物化目录，carry 会拿到新凭据——以实测为准）。
10. **包体积与冷启动**：163MB 解压、首次启动要加载大量 chunk，进程冷启动比
    Gemini CLI 慢；`qwen serve` / `qwen daemon` 提供长驻模式（P3 可选，类似 codex
    app-server 的 `stdin_hold` 思路，另需要 HTTP + SSE 客户端，成本高，先不做）。

---

## 6. 落地顺序

**P0 — 跑通官方链路（2 个文件 + 测试）**
适配器骨架（invocation / parse_line / HOME 隔离 / env）、Logo、README 表格、
`test_adapters.py` 用真实 stream-json 样本的解析用例 + `test_architecture.py` 注册表用例。
设备码登录助手（`qwen_login.py`）：真实端点 curl 验证 → PKCE → 二维码 → 轮询落盘。
验收：**不碰终端**，在「内核管理」点「登录」完成设备码授权后，平台新建 Qwen 智能体
→ 对话、流式、工具调用、多轮续接全部可用；「内核管理」页能安装 / 更新 / 卸载并显示登录状态。

**P1 — 技能与 MCP（严格仅分配）**
`skill_roots / discover_skills / skill_mount / skill_invocation` 与
`discover_mcp_servers / mcp_mount`；实测两项：技能分配 → 只有分配的技能出现在
`init.slash_commands`（bundled / project 已被 `disabledLevels` 关掉）；
MCP 分配 → `--allowed-mcp-server-names` 下本机配置不漏进 `init.mcp_servers`。

**P2 — 自定义网关（settings.json 路由）**
`build_invocation` / `skill_mount` 写 `settings.json`（modelProviders + model.name +
auth type）+ 协议校验报错 + `system_defaults()` 读本机配置。
验收：绑一个 OpenAI 兼容网关（如 OpenRouter）→ `--model` 选中注入的 id →
对话走网关密钥，`result.usage` 正常回报，**且没有任何请求打到阿里云端点**
（用网关侧日志确认 `-m` 没有跑偏）。

**P3（可选）— 增强项**
多账号槽位（同 `codebuddy_accounts` 思路）、Anthropic / Gemini 协议网关、
`--json-schema` 结构化输出、`qwen serve` 长驻模式、用量 JSONL
（`<配置目录>/usage/`）的对账展示。
