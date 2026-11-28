# MiniMax Code CLI 接入方案

> **状态：未实现 · 待实施（任务计划）**
> 调研对象：MiniMax Code CLI（npm `@minimax-ai/code`，bin `mcode`），文档 https://code.minimax.cn/docs/cli/quick-start
> 本文结论全部来自 2026-11-24 在本机的实测（mcode **0.7.9**），不是照抄文档；实测方式见第二节。

## 任务状态

| 项 | 值 |
| --- | --- |
| 状态 | 未实现，等待实施 |
| 前置阻塞 | 无硬阻塞。登录成功后的落盘形态需用真实账号补一次实测（见第七节） |
| 实施位置 | `backend/app/agents/builtin/minimax_cli.py`（新增） |
| 验收标准 | 内核管理能装/更/卸；登录扫码可用；新建 MiniMax 智能体能流式对话、工具调用、多轮续接；技能与 MCP 严格仅分配；`bash scripts/test.sh` 通过 |

---

## 一、结论先行

**MiniMax Code CLI 就是一个标准的 headless JSONL 内核，接入难度中等偏低，比 CodeBuddy 简单。**

它同时提供了我们需要的所有"接口面"：

| 能力 | mcode 的入口 | 平台怎么接 |
| --- | --- | --- |
| 无头执行 | `mcode exec --output-format stream-json` | `build_invocation()` 起子进程 |
| 会话续接 | `--session <id>` / `--continue` | `Capabilities.sessions=True` |
| 原生人格 | `--system-prompt-file` / `--append-system-prompt-file` | `native_system_prompt=True` |
| 工具调用 | `item.*` 事件里的 `tool_call` item | `tool_call` / `tool_result` 事件 |
| 推理过程 | `reasoning` item 的 `contentDelta` | `thinking.delta` |
| 权限 | `--permission smart\|full\|off` | `permission_modes=True` |
| 技能 | `<数据目录>/skills/<name>/SKILL.md` | `SkillMount` 直接复用 |
| MCP | `<数据目录>/mcp.json` | `McpMount.files` 直接复用 |
| 自定义网关 | `config.yaml` 里的 `custom_provider` | 写配置，不碰环境变量 |
| 官方登录 | `mcode login --no-browser`（设备码） | 登录助手渲染二维码 |

**两个必须注意的差异**（后面详述）：

1. **技能默认会"串味"**：它默认扫描 `~/.claude/skills`、`~/.codex/skills`、`~/.agents/skills` 和工作区的 `.minimax/skills`、`.claude/skills`、`.agents/skills`。必须在 `config.yaml` 里写 `skills.external.enabled: false`，否则违反铁律 13（严格仅分配）。**实测已确认可关闭**。
2. **登录态在数据目录里**：`<数据目录>/auth/...`。如果按对话隔离 `MINIMAX_DATA_DIR`，登录态会跟着"消失"。方案是把隔离目录里的 `auth` **软链**到真实数据目录（实测 CLI 会顺着软链读写）。

---

## 二、实测取证（本机 mcode 0.7.9）

### 2.1 安装

| 项 | 值 |
| --- | --- |
| npm 包 | `@minimax-ai/code` |
| bin | `mcode`（辅助 `mcode-tools`） |
| 版本 | 0.7.9（本次实测） |
| Node 要求 | `>=22.19 <23 \|\| >=24 <27`（package.json `engines`） |
| 解压体积 | **338 MB**（`releases/0.7.9/...`） |
| 官方安装器 | `curl -fsSL https://filecdn.minimax.chat/public/install.sh \| bash`，默认装到 `~/.minimax-code`，内部是 `releases/<版本>/` + `current` 软链 + `bin/mcode` |
| npm 安装 | `npm install -g @minimax-ai/code` |

**⚠️ 最大的安装坑：原生 SQLite。**

它依赖 `better-sqlite3@12.11.1`（native binding）。本机（Debian、Node 24 / ABI 137）直接 `npm install -g` 会失败：

```
[MCode] Native SQLite installation failed.
Reason: The native SQLite binding is missing...
node-gyp ... not found: make
```

两条可行路径，**都已实测**：

```bash
# A. 给 npm 指定 better-sqlite3 预编译二进制镜像（推荐，无需 build 工具链）
export npm_config_better_sqlite3_binary_host_mirror=https://npmmirror.com/mirrors/better-sqlite3
npm install -g @minimax-ai/code
# → Native SQLite check passed.

# B. 官方安装器 + 国内镜像（它会自己处理 Node 与预编译二进制）
MCODE_DOWNLOAD_MIRROR=cn bash install.sh
```

> 含义：**镜像里不能只装 npm 就完事**。要么给 provisioner 的安装命令注入上面这个环境变量，要么在 Dockerfile 里补 `build-essential python3`（体积大，不推荐）。官方安装器明确不支持 Alpine / musl，我们的 `python:3.11-slim`（Debian）没问题。

### 2.2 数据目录布局（`MINIMAX_DATA_DIR`）

```
<data-dir>/                      # 默认 ~/.minimax，兼容回退 MAVIS_DATA_DIR
├── config.yaml                  # 运行时配置（模型、provider、权限、技能开关…）
├── auth/
│   └── prod/cn/mcode-public/
│       ├── auth-state.json      # 登录状态：status=authenticated/authorizing/expired…
│       ├── auth.json            # 凭据（file 存储时）
│       └── auth.lock
├── skills/                      # ★ 用户级技能根（平台物化技能就放这里）
├── .builtin-skills/             # 内核自带技能（每次启动重建）
├── plugins/                     # 插件
├── mcp.json                     # ★ 用户级 MCP 配置
├── agents/                      # 子代理
├── v2/sqlite/                   # ★ Session 存储（sqlite）
└── bin / shims / cache / run / integrations
```

实测 `mcode provider list --json`、`mcode exec` 都会在数据目录里落 `config.yaml`。

### 2.3 headless 协议（实测）

```bash
mcode exec \
  --cwd <workspace> \
  --input -                      # 提示词走 stdin（也可用位置参数）
  --output-format stream-json \
  --permission smart \
  --prompt-mode coding
```

参数（`mcode exec --help` 实测）：

| 参数 | 说明 |
| --- | --- |
| `--input -` | 显式从 stdin 读；只能传 `-`，不能和位置参数同时用 |
| `--input-format text\|json` | 默认 text |
| `--cwd <path>` | 工作区 |
| `--file <path>` | 附件，可重复，最多 10 个 / 合计 100 MB |
| `--model <provider/model>` | 本次覆盖 |
| `--effort <level>` | 思考强度；档位由模型决定，非法档位**启动前**报错 |
| `--mode standard\|lightweight` | 上下文模式，只对新 Session 生效 |
| `--prompt-mode tui\|coding\|work` | 默认 tui；**续接时必须与会话保存的一致** |
| `--session <id>` / `--continue` | 续接 |
| `--permission smart\|full\|off` | `ask` 在 headless 直接拒绝（退出码 2） |
| `--timeout 30s\|2m` | 超时 |
| `--max-steps <n>` | 步数上限 |
| `--output-format text\|json\|stream-json` | 机器输出 |
| `--output-schema <schema>` | 结构化输出 |
| `-o, --output-last-message <path>` | 最终回答写文件 |
| `--system-prompt[-file]` / `--append-system-prompt[-file]` | **实测可用**，人格注入没问题 |

退出码（实测）：`0` 成功、`2` 参数/权限错误、`3` 配置错误（含未登录）、`4` 运行时失败、`6` 超时、`7` 超限、`130` 取消。

### 2.4 stream-json 事件流（实测样本）

```
{"type":"exec.started", "sessionId":"mvs_...", "turnId":"turn_..."}
{"type":"session.started", "sessionId":"mvs_..."}
{"type":"turn.started"}
{"type":"item.started","item":{"id":"<uuid>:message","type":"agent_message","contentDelta":"..."}}
{"type":"item.started","item":{"id":"<call-id>","type":"tool_call","toolCall":{"id":"call_abc","name":"bash","status":4}}}
{"type":"item.updated","item":{"id":"call_abc","type":"tool_call","toolCall":{"id":"call_abc","name":"bash","status":1,"input":{"command":"echo hi"}}}}
{"type":"item.updated","item":{"id":"call_abc","type":"tool_call","toolCall":{"id":"call_abc","name":"bash","status":2,"input":{...},"output":{"content":[{"type":"text","text":"hi\n"}],"details":{...}}}}}
{"type":"item.completed","item":{"id":"call_abc","type":"tool_call","toolCall":{...}}}
{"type":"item.started","item":{"id":"<uuid>:reasoning","type":"reasoning","contentDelta":"..."}}
{"type":"turn.completed","model":{...},"usage":{"inputTokens":32,"outputTokens":14,"totalTokens":46},"durationMs":1528}
{"type":"exec.completed","result":{...完整 ExecResult...}}
```

要点：

- **item 类型只有三种**：`agent_message`、`reasoning`、`tool_call`（另有框架自带的任务类 item，按 `tool_call` 处理）。
- **文本增量**读 `item.contentDelta`（`item.started` 或 `item.updated` 都可能有）；`item.completed` 里是全文 `item.content`。
- **工具调用**：`toolCall.status` 是数字（实测见过 4/5/1/2），**不要按数字硬编码**；按字段判断最稳：
  - 首次出现 `input` → 发一次 `tool_call`（去重靠 `toolCall.id`）；
  - 出现 `output` → 发 `tool_result`（`output.content[]` 里拼 `text`，`details.execution.status` 判断 `is_error`）。
- **用量**在 `turn.completed.usage`（`inputTokens/outputTokens/totalTokens`），`exec.completed.result` 里也有一份。
- **错误**在 `turn.failed`（`error.category` + `error.message`）。

### 2.5 会话续接（实测）

- `--session <id>` 可续，`sessionId` 形如 `mvs_<32hex>`。
- **Session 绑定工作目录**：换 `--cwd` 续接直接失败（`Session workspace does not match --cwd`）。平台每个对话工作目录稳定，天然满足。
- `--resume` 是兼容别名；`--session` / `--continue` / `--resume` 三者不能同时用。
- Session 存在 `v2/sqlite`，进程重启后仍在。

### 2.6 技能（实测，含"串味"验证）

- 用户级技能根：`<data-dir>/skills/<slug>/SKILL.md`（frontmatter `name`/`description`）。
- **默认还会扫描**（实测都会被加载）：
  - 工作区向上查找：`.minimax/skills`、`.claude/skills`、`.agents/skills`
  - 用户级：`~/.claude/skills`、`~/.codex/skills`、`~/.agents/skills`
- 关闭开关（实测有效）：`config.yaml` 里

```yaml
skills:
  external:
    enabled: false
    walkUp: false
```

  关闭后：`<data-dir>/skills` 里平台分配的技能**仍可见**，工作区 `.claude/skills` 里的"外人技能"**不再出现**。这正好满足铁律 13。
- 调用方式：内置工具 **`skill`**（小写），参数 `{"name": "<技能名>"}`。对应平台的 `skill_invocation()`。

### 2.7 MCP（实测）

- 用户级：`<data-dir>/mcp.json`（实测会真的拉起 stdio 服务器进程）
- 项目级：`<workspace>/.mcp.json`（实测也会拉起）
- 实测可用格式：

```json
{
  "mcpServers": {
    "probe": {"type": "stdio", "command": "python3", "args": ["-c", "print(1)"]}
  }
}
```

- MCP 工具**不进默认工具表**，走 `tool_search` 惰性发现 + `mcp_invoke` 调用（这是它自己的 PTC 设计，不是没加载）。
- **⚠️ 项目级 `.mcp.json` 无法关闭**：在 CLI 里没找到对应开关，headless 会把工作区已有的 `.mcp.json` 一并加载。这算一个隔离缺口（见第六节）。

### 2.8 鉴权与模型路由（实测）

| 路径 | 机制 | 实测结论 |
| --- | --- | --- |
| 官方 Token Plan | `mcode login --no-browser --region cn` | 输出**到 stderr**：`Open: https://account.minimax.cn/oauth-authorize?user_code=XXXX`、`Code: XXXX`、`Waiting for authorization…`；属于设备码流，无需本地回调端口 |
| 官方 API Key | `mcode provider set-minimax-key --api-key-env <名>` | 当前版本会把**明文 Key 写进** `config.yaml` 的 `minimax_api.apiKey`（`<名>` 只用于读取时解析，不是引用）。**但手写 `${ENV}` 可以生效**（见下） |
| 自定义 Provider | `mcode provider add --base-url ... --api-format ...` | 同样把明文 Key 写进 `custom_provider.<id>.options.apiKey` |

**关键发现：`config.yaml` 支持 `${ENV}` 插值**（实测）：

```yaml
custom_provider:
  platform:
    options:
      apiKey: "${MCODE_PROVIDER_API_KEY}"   # 未设置该变量时启动即报错，说明确实解析了
```

这意味着**平台密钥不必落盘**：写变量引用 + 通过子进程 env 注入明文即可（符合铁律 6）。

**端点拼接实测**（决定了平台网关怎么配）：

| `api` 格式 | 请求路径 | 结论 |
| --- | --- | --- |
| `openai-completions` | `POST <base>/chat/completions` | **base 必须自带 `/v1`**；不会自动补 |
| `anthropic-messages` | `POST <base>/v1/messages` | 会自动补 `/v1`；base 带不带 `/v1` 都行；鉴权头 `x-api-key` + `anthropic-version` |
| `google-generative-ai` | 文档：只填域名时自动补 `/v1beta` | 未实测 |
| 附加探测 | `POST <base>/responses/input_tokens` | token 计数用；**404 也能正常跑通**（实测） |

平台网关 `protocol` 是 `openai / anthropic / gemini`，正好一一映射：

| 平台 protocol | mcode `api` | base_url 处理 |
| --- | --- | --- |
| `openai` | `openai-completions` | 确保以 `/v1` 结尾 |
| `anthropic` | `anthropic-messages` | 原样 |
| `gemini` | `google-generative-ai` | 原样 |

---

## 三、适配器设计

新建 `backend/app/agents/builtin/minimax_cli.py`，骨架与 `codebuddy_cli.py` / `doubao_cli.py` 对齐。

```python
@register_agent
class MinimaxCliAgent(AgentDefinition):
    id = "minimax-cli"
    name = "MiniMax Code CLI"
    vendor = "MiniMax"
    description = "MiniMax 的终端智能体。支持设备码登录、原生人格、技能与 MCP，"
                  "输出 stream-json 事件流。"
    runtime = Runtime.CLI
    executable = "mcode"
    logo = "/icons/kernels/minimax.webp"
    logo_style = "image"
    protocol = "openai"
    api_key_env = "MCODE_PROVIDER_API_KEY"   # 配置里用 ${MCODE_PROVIDER_API_KEY} 引用
    base_url_env = None                      # 网关走 config.yaml，不走环境变量
    default_gateway = None                   # 官方链路 = 本机登录，不播种官方网关
    default_model = "MiniMax-M3.1-Flash"
    default_models = ["MiniMax-M3.1-Flash", "MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.7-highspeed"]
    docs_url = "https://code.minimax.cn/docs/cli/quick-start"
    capabilities = Capabilities(
        streaming=True, sessions=True, native_system_prompt=True,
        tools=True, mcp=True, permission_modes=True, working_directory=True, skills=True,
    )
```

### 3.1 config_fields

| key | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `permission` | SELECT | `smart` | `smart`（Auto，常规自动）/ `full`（跳过确认）/ `off`。headless 没有 `ask` |
| `prompt_mode` | SELECT | `coding` | `tui` / `coding` / `work`。**续接时必须保持不变**，否则会话报错 |
| `context_mode` | SELECT | `standard` | `standard` / `lightweight`，只对新会话生效 |
| `effort` | SELECT | 空 | 空=跟随模型；low/medium/high/xhigh/max。不支持的模型会启动前报错 |
| `max_steps` | NUMBER | 空 | → `--max-steps` |
| `append_system_prompt` | BOOLEAN | `True` | True=`--append-system-prompt-file`；False=`--system-prompt-file` |
| `extra_args` | STRING_LIST | — | 兜底旗标 |
| `env` | KEYVALUE | — | 框架已支持 |

> 不提供 `timeout`，因为框架层已经有统一的运行超时；多加一层容易互相顶替（铁律 10）。需要时用 `extra_args` 显式传。

### 3.2 build_invocation()

```python
def build_invocation(self, ctx: AgentContext) -> Invocation:
    home = Path(ctx.home_dir or (Path.home() / ".minimax-cli-home"))
    home.mkdir(parents=True, exist_ok=True)
    options = ctx.options

    # 1) 写 config.yaml：模型路由 + 关掉外部技能 + 默认技能根
    self._write_config(home, ctx)

    # 2) 登录态：把隔离目录的 auth 软链到真实数据目录
    self._link_auth(home)

    argv = [
        self.executable or "mcode", "exec",
        "--cwd", ctx.workspace_dir or os.getcwd(),
        "--input", "-",
        "--output-format", "stream-json",
        "--permission", options.get("permission") or "smart",
        "--prompt-mode", options.get("prompt_mode") or "coding",
    ]
    mode = options.get("context_mode")
    if mode and mode != "standard":
        argv += ["--mode", str(mode)]
    if options.get("effort"):
        argv += ["--effort", str(options["effort"])]
    if options.get("max_steps"):
        argv += ["--max-steps", str(int(options["max_steps"]))]
    if ctx.session_id:
        argv += ["--session", ctx.session_id]

    # 3) 人格：写进隔离目录，用文件旗标（避免 argv 过长）
    if ctx.system_prompt:
        persona = home / "persona.md"
        persona.write_text(ctx.system_prompt, encoding="utf-8")
        os.chmod(persona, 0o600)
        flag = "--append-system-prompt-file" if options.get("append_system_prompt", True) \
               else "--system-prompt-file"
        argv += [flag, str(persona)]

    argv += [str(a) for a in as_list(options.get("extra_args") or [])]

    env = self.effective_env(ctx)          # 注入 MCODE_PROVIDER_API_KEY
    env["MINIMAX_DATA_DIR"] = str(home)    # 数据目录隔离（会话 / 技能 / MCP / 配置）
    env.setdefault("MCODE_DISABLE_TELEMETRY", "1")
    env.setdefault("CI", "1")
    env.setdefault("NO_COLOR", "1")

    return Invocation(argv=argv, env=env, cwd=ctx.workspace_dir, stdin=ctx.prompt or "")
```

### 3.3 写 config.yaml

```python
def _write_config(self, home: Path, ctx: AgentContext) -> None:
    config: dict[str, Any] = {
        "logLevel": "info",
        # ★ 铁律 13：只认 <home>/skills 里平台分配的技能
        "skills": {"external": {"enabled": False, "walkUp": False}},
    }
    model = ctx.model or self.default_model

    if ctx.gateway_url and ctx.api_key:
        # 自定义网关：base 归一化 + 平台密钥只写引用，明文走 env
        base = ctx.gateway_url.rstrip("/")
        api = _API_FORMAT[ctx.protocol]           # openai/anthropic/gemini → 对应格式
        if api == "openai-completions" and not base.endswith("/v1"):
            base += "/v1"
        config["defaultModel"] = f"custom_provider:platform/{model}"
        config["custom_provider"] = {
            "platform": {
                "name": "platform", "kind": "custom", "enabled": True, "api": api,
                "options": {
                    "apiKey": "${MCODE_PROVIDER_API_KEY}",
                    "baseURL": base, "authMode": "api-key",
                },
                "models": {model: {"reasoning": True}},
            }
        }
    else:
        # 官方链路：本机登录态
        config["defaultModel"] = f"minimax/{model}"
        config["minimaxModelSource"] = "token_plan"

    tmp = home / "config.yaml.tmp"
    tmp.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, home / "config.yaml")
```

> 为什么不用 `mcode provider add`：它会做一次"ping"非流式请求来验证，网关不支持非流式就直接失败；还会把明文 Key 写盘。直接写配置文件更可控、可复现。

### 3.4 登录态：软链而不是拷贝

`MINIMAX_DATA_DIR` 隔离后，登录态在 `<home>/auth/...` 里是空的。做法：

```python
def _link_auth(self, home: Path) -> None:
    real = Path(os.environ.get("MINIMAX_DATA_DIR") or Path.home() / ".minimax")
    target = home / "auth"
    if target.is_symlink() or target.exists():
        return
    source = real / "auth"
    source.mkdir(parents=True, exist_ok=True)
    try:
        target.symlink_to(source, target_is_directory=True)
    except OSError:
        pass    # 尽力而为；失败只意味着需要重新登录
```

实测：CLI 会顺着软链读 `auth-state.json` 并写 `auth.lock`。软链的好处是**刷新 token 会写回真实目录**，所有对话共享最新登录态；拷贝则会把刷新结果困在各自的隔离目录里。

> 存储可能是 OS keyring（`storeKind: "os-keyring"`）。Linux 容器里实测是 `file`。macOS / Windows 若走 keyring，同一个用户下仍然可读，不影响。

### 3.5 parse_line()

```python
def parse_line(self, line: str, run: AgentRun) -> Iterable[AgentEvent]:
    line = line.strip()
    if not line:
        return
    if not line.startswith("{"):
        yield log(line, "debug")
        return
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        yield log(line, "debug")
        return

    kind = payload.get("type")
    if kind == "session.started" or payload.get("sessionId"):
        sid = payload.get("sessionId")
        if sid and sid != run.state.get("session_id"):
            run.state["session_id"] = sid
            yield session(sid)
    if kind in ("item.started", "item.updated", "item.completed"):
        yield from self._parse_item(payload.get("item") or {}, run)
    elif kind == "turn.completed":
        u = payload.get("usage") or {}
        if u:
            yield usage(input_tokens=u.get("inputTokens"), output_tokens=u.get("outputTokens"),
                        total_tokens=u.get("totalTokens"))
    elif kind == "turn.failed":
        err = payload.get("error") or {}
        yield error(err.get("message") or "MiniMax Code 报告了一个错误")
    elif kind == "exec.completed":
        result = payload.get("result") or {}
        if result.get("status") not in (None, "succeeded"):
            yield error(f"任务未成功：{result.get('status')}")
```

`_parse_item()` 的关键规则：

- `agent_message`：有 `contentDelta` → `text_delta`，并记 `run.state["text_emitted"]`；没有增量但 `item.completed` 有 `content` 且**从未**发过增量 → 补发全文。
- `reasoning`：`contentDelta` → `thinking_delta`。
- `tool_call`：用 `toolCall.id` 去重；
  - 首次拿到 `input` → `tool_call(name, call_id, **input)`
  - 首次拿到 `output` → `tool_result(call_id, 拼 text, is_error=details.execution.status != "succeeded")`
- 其它 item 类型 → `log(..., "debug")`，**不要新增事件类型**（铁律 5）。

### 3.6 技能与 MCP

```python
def skill_roots(self):
    data = _data_dir()                      # 真实数据目录（~/.minimax）
    return [
        SkillRoot(str(data / "skills" / ".skillhub"), source_kind=MARKET_SOURCE),
        SkillRoot(str(data / "skills"), source_kind="user"),
    ]

def skill_mount(self, ctx):
    return SkillMount(
        home_name="minimax-home",
        skills_subdir="skills",             # 正好是 mcode 的用户级技能根
        notes=["MiniMax 只加载平台分配的技能；外部 .claude/.codex 技能已关闭"],
    )

def skill_invocation(self, tool_name, tool_input):
    if tool_name != "skill":
        return None
    return str(tool_input.get("name") or "").strip() or None

def discover_mcp_servers(self):
    return parse_json_servers(_read_json(_data_dir() / "mcp.json"), type_id=self.id)

def mcp_mount(self, ctx):
    return McpMount(home_name="minimax-home",
                    files={"mcp.json": to_json_servers(ctx.mcp_servers)})
```

> `skill_mount` 与 `mcp_mount` 的 `home_name` 必须一致（框架用同一个"家"）；`skills_subdir="skills"` 正好对上 mcode 的技能根，不需要额外 carry/patch。

### 3.7 登录助手（`minimax_login.py`）

`mcode login --no-browser` 是**设备码流**，比 CodeBuddy 简单得多：

| 步骤 | 实现 |
| --- | --- |
| 起进程 | `mcode login --no-browser --region <cn\|en>`，`MINIMAX_DATA_DIR` 指向真实数据目录 |
| 取链接 | 从 **stderr** 读 `Open: <url>`；同时读 `Code: <code>` |
| 出二维码 | `segno.make(url).save(png)`，打印 `QR_IMAGE=<绝对路径>`（平台通用协议） |
| 展示码 | 把 `Code: XXXX` 原样透传成日志行（用户手动输入时可用） |
| 等授权 | 子进程退出即结束；退出 0 = 成功 |
| 收尾 | 删二维码（短期凭据），杀进程组 |

`provisioner.login_argv = [sys.executable, str(_LOGIN_HELPER)]`。

### 3.8 auth_status()

不联网、不消耗额度，只读文件：

```python
def auth_status(self):
    base = _data_dir() / "auth"
    for state in base.glob("*/"):                       # prod/test/dev
        for path in state.glob("*/mcode-public/auth-state.json"):
            try:
                data = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            status = data.get("status")
            if status == "authenticated":
                return AuthStatus(required=True, logged_in=True,
                                  detail=f"已登录（{data.get('region')}）")
            if status in ("expired", "error", "logout_pending"):
                return AuthStatus(required=True, logged_in=False,
                                  detail="登录已过期：点「登录」重新授权")
    return AuthStatus(required=True, logged_in=False,
                      detail="未登录：点「登录」扫码，或在本机执行 mcode login")
```

真实数据目录取 `MINIMAX_DATA_DIR`（若设置）否则 `~/.minimax`；**注意不要用隔离目录**。

### 3.9 账号槽位（P3 可选）

mcode 是**单账号**模型：`auth/prod/<env>/<region>/mcode-public/` 只有一份 `auth-state.json`。多账号可以照搬 `codebuddy_accounts.py` 的思路：登录后把该目录归档成槽位，切换时写回。第一版不做。

---

## 四、需要动的文件

| 文件 | 改动 |
| --- | --- |
| `backend/app/agents/builtin/minimax_cli.py` | **新增**适配器（唯一的功能代码） |
| `backend/app/agents/builtin/minimax_login.py` | **新增**登录助手（设备码 → 二维码） |
| `backend/app/agents/builtin/util.py` | `npm_install_argv()` 增加可选 `env` 参数（给 better-sqlite3 预编译镜像），通用能力，不含内核知识 |
| `backend/tests/test_adapters.py` | 注册表断言 + invocation / parse / 权限 / 技能 / MCP / 配置写盘用例 |
| `backend/tests/test_minimax_login.py` | 登录助手：假 `mcode` 脚本输出 `Open:`/`Code:` → 二维码 |
| `backend/tests/test_architecture.py` | `BUILTIN_IDS` 加 `"minimax-cli"` |
| `frontend/public/icons/kernels/minimax.webp` | **新增**真彩色 Logo（`logo_style="image"`） |
| `README.md`「都能接什么」表 | 加一行 `minimax-cli` |
| `backend/app/bootstrap.py` | （可选）`STARTER_PERSONAS` 加一条 |
| `docs/计划任务/minimax-code-cli-plan.md` | 本方案的仓库版 |

不需要改：事件协议、runner、技能/MCP 物化层、前端表单（仍由 descriptor 驱动）。

---

## 五、落地顺序

### P0 — 跑通官方链路（1 个适配器 + 测试）

1. 适配器骨架：`build_invocation`（config.yaml + 软链 auth + 人格文件）、`parse_line`、权限、env。
2. 安装：验证 `npm_install_argv` 带 better-sqlite3 镜像环境变量能装上；镜像里 Node 24 + Debian 已验证。
3. `auth_status()` + 登录助手（`mcode login --no-browser` → 二维码）。
4. Logo、README、`test_architecture.py` 注册表、`test_adapters.py` 用真实 stream-json 样本做解析用例。

**验收**：内核管理里点「安装」成功、点「登录」出二维码；扫码后新建 MiniMax 智能体 → 流式回复、工具调用、多轮续接全部可用。

### P1 — 技能与 MCP 严格仅分配

1. `skill_mount(skills_subdir="skills")` + `config.yaml` 的 `skills.external.enabled: false`。
2. `mcp_mount(files={"mcp.json": ...})`；`discover_mcp_servers` 读真实数据目录。
3. **实测**：分配技能 A → 只有 A 可见；工作区放一个 `.claude/skills/foreign` → 不出现；分配 MCP → 进程被拉起。

### P2 — 自定义网关

1. `_write_config()` 按 `protocol` 映射 `api`，base URL 归一化（openai 补 `/v1`）。
2. `apiKey: "${MCODE_PROVIDER_API_KEY}"`，密钥只在子进程 env 里。
3. **实测**：绑一个 OpenAI 兼容网关 → 选中模型 → 对话走网关、usage 正常回报；Anthropic 协议网关也验一次。

### P3（可选）— 增强

- 账号槽位 / 多账号切换（照搬 codebuddy）。
- `--max-steps` + `--timeout` 做成智能体配置项。
- `--output-schema` 的结构化输出。
- 把 `task_*` 类 item 映射成工具事件（第一版先 `log`）。

---

## 六、风险与已知限制

1. **原生 SQLite 安装**：`better-sqlite3` 必须拿到预编译二进制。不设镜像、容器里又没 `make/g++` 时会失败。**P0 必须验证**镜像内的安装路径。
2. **项目级 `.mcp.json` 无法关闭**：headless 会连带加载工作区里已有的 `.mcp.json`。平台默认给每个对话分配空沙箱，所以只有"用户把工作目录指向真实项目且该项目有 `.mcp.json`"时才受影响。文档里要写明。
3. **`--prompt-mode` 续接必须一致**：如果以后把它做成可变配置项，中途改值会让旧会话续接失败。实现上应在会话首个事件后把 mode 记进 `run.state`/会话元数据，或干脆固定不变。
4. **`--effort` 档位由模型决定**：没有枚举命令，非法值启动前报错（退出码非 0）。UI 上给常见档位，并允许留空。
5. **登录态与数据目录绑定**：靠软链共享。若未来内核改成 keyring，软链不生效，需要改成"登录后把 auth 目录内容带回隔离家"或直接不隔离数据目录。
6. **安装体积大**：338 MB，比 Claude Code / Gemini 都大。provisioner 的超时回显要预期"安装明显更慢"。
7. **流事件 schema 有版本号**（`schemaVersion: 1`）：升级 mcode 大版本时要跑一次真实样本回归。
8. **官方链路的额度/计费**：走 Token Plan 时平台看不到"积分余额"，`/usage` 只在 TUI 里。不做展示，避免误导。
9. **Alpine / musl 不支持**：官方安装器明确排除；镜像保持 Debian slim。
10. **子代理 / 插件**：`agents/`、`plugins/` 目录第一版不接，避免"顺手带进"未分配能力。

---

## 七、待验证清单（实施前/实施中）

1. **真实登录落盘**：用真实账号跑一次 `mcode login --no-browser`，确认成功后 `auth/prod/cn/mcode-public/auth-state.json` 的 `status` 变成 `authenticated`、进程退出码 0。**这是 `auth_status()` 与登录助手的前提。**
2. **软链 + 真实 token 刷新**：登录后跑一次任务，确认 `auth-state.json` 的 `expiresAtMs` / `generation` 会被刷新（验证软链写回）。
3. **技能严格仅分配**：已在本次实测中验证"关 external 后平台技能仍可见、外部技能消失"；P1 再在真实平台流程里复验一次。
4. **MCP 拉起**：本次已用进程副作用验证 stdio server 被启动；P1 再验证平台分配的服务器（含 url 型）能被 `tool_search` 发现。
5. **网关三种协议**：openai / anthropic 已实测路径拼接；gemini 未测（平台网关较少用，P2 补）。
6. **Node 24 + better-sqlite3 预编译**：已实测可装；需在目标 Docker 镜像（`python:3.11-slim` + 官方 Node 24.22.3）里再跑一次。

---

## 八、一句话总结

> mcode 的接口面和我们现有的 CLI 适配器几乎一一对应：`exec --output-format stream-json` + `--session` + `--append-system-prompt-file` + 数据目录隔离。真正要小心的是三件事——**原生 SQLite 的安装方式**、**默认会串味的技能扫描**、**登录态跟着数据目录走**。把这三处按本文处理掉，剩下就是照抄 `codebuddy_cli.py` 的骨架。

---

## 关联

- `docs/功能文档/codebuddy-cli.md` —— 同为"账号型 + 自定义网关"的 CLI 内核，骨架直接复用
- `docs/功能文档/doubao-cli.md` —— 账号型内核的登录态探测与登录助手写法
- `docs/架构/architecture.md` —— 适配器分层与事件协议
- `docs/README.md` —— 项目文档索引

- Qoder CLI 接入方案
- opencode v2 接入方案
