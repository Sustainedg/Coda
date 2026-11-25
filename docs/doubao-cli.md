# 接入豆包 CLI 的方案

> 调研对象：字节跳动「豆包 CLI」（npm 包 `@bytedance/doubao-cli`，可执行文件 `doubao`）。
> 本文回答三件事：它是什么、平台怎么接、内核管理怎么换成「实打实的图标」。
> 结论全部来自**本机实装与实跑**（2.37.2，Linux x64），不是文档摘抄。

---

## 1. 结论先行

豆包 CLI 是**账号登录型**内核，不是 API-Key 型内核：

- **没有 API Key / 自定义网关**。它靠 `doubao login` 扫码登录，凭据落在
  `$XDG_DATA_HOME/doubao_cli/`（默认 `~/.local/share/doubao_cli/`），
  所以适配器不能走平台的「网关 + 密钥」通道，也不能播种「官方网关」。
- **headless 协议齐活**：`doubao -p - --output-format stream-json` 输出逐行 JSON，
  事件形状与 Claude Code **高度相似**（`system/init`、`stream_event`、`assistant`、
  `user`、`result`），会话续接靠 `--resume <会话 ID>`。
- **一条适配器文件即可接入**，不需要新框架能力、不需要新增事件类型。
- **两个硬约束**要提前认：① 安装时要下载 ~236 MB 原生二进制；② 只支持
  Windows x64 / macOS ARM64 / Linux x64，且 Linux 还依赖 `libgcrypt.so.20`。

「实打实的图标」部分：当前内核管理把单色 SVG 当遮罩染色，用户要的是**真彩色位图**
（像示例里那张 102×102 PNG）。方案是让 `logo` 支持位图（PNG/WebP），
前端按扩展名/声明选择 `<img>` 或遮罩，并把每个内核的真实品牌图**随仓库发布**，
不做运行时外链。

---

## 2. 调研到的事实（实测）

### 2.1 安装与可执行文件

| 项 | 值 |
| --- | --- |
| npm 包 | `@bytedance/doubao-cli` |
| bin | `doubao`（Node 启动器 `scripts/run.cjs`，真正的原生程序在数据目录里） |
| 版本 | 实测 `2.37.2`，`doubao --version` → `doubao 2.37.2` |
| Node 要求 | `>= 18`（package.json `engines`） |
| 支持平台 | `win32-x64` / `darwin-arm64` / `linux-x64`（**没有** macOS x64、Linux arm64） |
| 安装命令 | `npm install -g '@bytedance/doubao-cli' --registry=https://registry.npmjs.org/` |
| 更新 | `doubao update`，或重跑上面的 npm 安装 |
| 安装体积 | postinstall 下载原生包 **236 MB**，落盘在 `$XDG_DATA_HOME/doubao_cli/app/` |

> **实测坑 1**：原生二进制动态链接 `libgcrypt.so.20` / `libgnutls.so.30`。
> Debian trixie 的 `python:3.11-slim` 里没有 libgcrypt，启动直接报
> `error while loading shared libraries: libgcrypt.so.20`。
> Docker 镜像必须补 `libgcrypt20 libgnutls30`。
>
> **实测坑 2**：npm 11 会提示 postinstall「未在 allowScripts 覆盖范围内」。
> 本机实测脚本仍会执行；若目标环境配了 `ignore-scripts=true`，原生程序不会被下载，
> `doubao --version` 会报「已安装的运行时缺失或无效」。

### 2.2 登录（与平台现有内核最大的差异）

```bash
doubao login                  # 交互式二维码（TTY）
doubao login --qr=png         # 输出 QR_IMAGE=<临时图片路径>
doubao auth status [--verify] # 本地状态 / 在线验证
doubao logout
```

- **没有 API Key**，`api_key_env` / `base_url_env` 都必须为空。
- 登录态在 `$XDG_DATA_HOME/doubao_cli/`（`control/`、`doubao-cli-pc-login/` 等）。
- 本机沙箱里 `api-normal.doubao.com` 可访问（HTTP 200），但设备注册接口返回
  `auth_unavailable: 设备注册失败：stage=3 error=11`，**登录必须在真实网络环境验收**。
- 扫码是**设备级**登录：一台机器一个账号，平台多用户会共享同一份登录态。
  这一点要写进文档，不能假装它是按用户隔离的。

### 2.3 headless 协议（接入主路径）

```bash
doubao -C <工作目录> --output-format stream-json -p -
# 提示词走 stdin（-p - 要求非空管道输入），避免命令行长度与引号问题
```

`--output-format` 只对 `-p` / `--list` / `model list` 有效。实测参数解析通过
（未登录时报 `login_required`，说明旗标都认得）。事件从 CLI 的
`entry-cli-chat/main.js` 里逐条核对过：

| 事件（每行一个 JSON） | 关键字段 | 平台映射 |
| --- | --- | --- |
| `system` / `subtype:"init"` | `session_id` | `session` |
| `stream_event` | `event.content_block_delta.delta.{type:"text_delta",text}` | `text_delta` |
| `assistant` | `message.{id,role,content:[{type:"text",text}]}` | 全文补发（**需去重**） |
| `assistant` | `content:[{type:"tool_use",id,name,input,input_status}]` | `tool_call` |
| `user` | `content:[{type:"tool_result",tool_use_id,content,isError,content_status}]` | `tool_result` |
| `result` | `subtype:"success"\|"error_during_execution"`、`is_error`、`result.content`、`errors`、`code`、`log_id`、`permission_denials` | `error` / 运行结果 |

去重规则（照抄 Claude 适配器的 `partial` 思路，但按 `message_id` 记账）：

1. `stream_event` 的 `text_delta` 逐字发；记下 `message_id`。
2. 同一 `message_id` 的 `assistant` 全文**不再发**（否则界面每轮重复一遍）。
3. 没有收到过增量的 `assistant`（工具轮、极短回复）才补发全文。
4. `tool_use` 按 `id` 去重；`input_status` 不是 `available` 时先别发，等 `result`
   兜底，避免同一次工具调用在界面上出现两次。
5. **没有 token 用量**：`result` 里没有 `usage`，适配器不要伪造 `usage` 事件。

`-p -` 的输入上限：管道 ≤ 1 MiB，最终提示词 ≤ 262144 个码点；超了 CLI 会直接报错。

### 2.4 会话、目录、权限、模型

| 能力 | 旗标 | 说明 |
| --- | --- | --- |
| 会话续接 | `--resume <会话 ID>` + `-p` | 会话 ID 就是 `session_id`（conversationId） |
| 列出会话 | `doubao --list [--json]` | 可选，平台不需要 |
| 工作目录 | `-C <目录>` | **必须放在 `-p` 之前**；目录必须存在 |
| 模型 | `--model <ID 或名称>` | 名称必须能唯一匹配；默认 Auto |
| 推理强度 | `--effort none\|minimal\|low\|medium\|high\|xhigh\|max` | 以模型支持为准 |
| 权限 | `--permission-mode ask_always\|ask_risky\|always_allow` | 与下一项互斥 |
| 无限制 | `--dangerously-bypass-approvals-and-sandbox` | 请求不受限的网络与文件访问 |
| 模型清单 | `doubao model list --json` | 需要登录；平台可留空由用户手填 |

> headless 下没有授权弹窗：`ask_always` / `ask_risky` 遇到需要确认的操作大概率被拒
> （最终在 `result.permission_denials` 里体现）。默认给 `always_allow`，
> 配置项 help 里写明风险；想更严的用户自己切 `ask_risky`。

### 2.5 技能与人格

- 豆包 CLI 会读**当前工作目录**的 `AGENTS.md`。
- 技能根（官方文档）：
  - 用户级：`~/Doubao/skills`、`~/.agents/skills`、`~/.codex/skills`、
    `~/.workbuddy/skills`、`~/.claude/skills`、`~/.trae/skills`、`~/.gemini/skills`
  - 工作区级：`<cwd>/.doubao/skills`、`<cwd>/.agents/skills`、`<cwd>/.codex/skills` 等
- 二进制里确认存在的路径串：`.agents/skills`、`.claude/skills`、`.codex/skills`、
  `.gemini/skills`、`.trae/skills`、`.workbuddy/skills`。

**人格注入不能用 `AGENTS.md`**：它只认 `<cwd>/AGENTS.md`，而工作目录是用户的项目目录
（本仓库自己就有 `AGENTS.md`），写进去等于改用户仓库。第一版**把人格拼到提示词最前面**
（`native_system_prompt=False`），安全、无副作用。

**技能严格仅分配**：把子进程 `HOME` 指到平台物化好的隔离家目录，只往
`<隔离 HOME>/.agents/skills/` 里放分配到的技能；其余 `~/.claude/skills` 等
相对 HOME 的根自然为空。工作区里用户自带的 `.claude/skills` 等仍会被额外加载，
按 Claude 适配器的做法给一条 `notes` 提示。

### 2.6 MCP

文档没有 MCP 配置说明；二进制里找不到 `mcpServers` / `.mcp.json` / `settings.json`
这类本地配置串（只有 `mcp_tool_block` 这种**输出渲染**类型）。
结论：**第一版 `capabilities.mcp = False`**，不写虚假的 mount。云端 MCP 由豆包账号侧管理，
平台侧不接管。

### 2.7 配置目录与隔离（实测）

| 环境变量 | 作用 |
| --- | --- |
| `XDG_DATA_HOME` | 决定 `doubao_cli/app`（原生程序）与登录态、控制通道；也决定设备身份存储 `mssdk` 的落点 |
| `HOME` | 决定 `~/Doubao/skills`、`~/.agents/skills` 等技能扫描根；macOS 上还决定 `Application Support` 数据根 |
| `XDG_CONFIG_HOME` / `XDG_CACHE_HOME` | 常规缓存/配置 |

实测结论：

- 只改 `XDG_DATA_HOME` → 找不到已安装的运行时（app 目录跟着走）。
  把 app 目录软链过去可以跑通。
- `HOME=<隔离目录>` + `XDG_DATA_HOME=<真实数据目录>` → 正常启动，
  技能根全部落到隔离目录。**这是推荐组合**：
  - `HOME = ctx.home_dir` —— 技能严格仅分配
  - `XDG_DATA_HOME` 显式指向服务进程的真实数据目录 —— 复用已安装的原生程序、登录态与设备身份
- macOS 没有 `XDG_DATA_HOME` 机制（`runtime.cjs` 写死按 `os.homedir()/Library/Application Support`
  解析，而 Node 的 `os.homedir()` 认 `HOME`）：隔离 HOME 里必须把
  `Application Support/doubao_cli`（原生程序 + 登录态）**和 `Application Support/mssdk`
  （设备身份的「安全存储」，字节设备指纹 SDK 的数据根）都软链到真实目录。
  只链 `doubao_cli` 漏掉 `mssdk` 时，每次运行都会在隔离目录重建设备身份，
  报 `auth_unavailable: 无法访问用于保存设备身份的安全存储`，对话直接起不来
  —— macOS 真机踩过的坑，适配器 `_link_macos_data` 两个目录一起链。
- 控制通道在 `/tmp/dcli-<uid>`，实例锁 `doubao-cli-instance.lock` 在数据目录。
  **并发运行是否互相打架必须实测**（协作域会并行跑多个智能体）。

### 2.8 环境依赖与平台限制

- Linux 需要 `libgcrypt20`、`libgnutls30`（以及它们的传递依赖）。
- macOS 只有 ARM64 包；Intel Mac 与 Linux arm64 不支持，适配器要在可用性探测里
  给出明确中文原因，而不是让用户对着「安装成功但跑不起来」发呆。
- 安装下载 236 MB，`provision_timeout_seconds` 默认 900 秒，慢网下要留意。
- `doubao update` 会尝试停掉 `--service-start` 服务；平台用的是 `-p` 一次性模式，
  不冲突，但**必须保证没有残留实例**。

---

## 3. 适配器设计

新建 `backend/app/agents/builtin/doubao_cli.py`。

### 3.1 元数据

```python
@register_agent
class DoubaoCliAgent(AgentDefinition):
    id = "doubao-cli"
    name = "豆包 CLI"
    vendor = "字节跳动"
    description = "豆包的终端智能体。扫码登录后可直接读写文件、执行命令、调用本地工具。"
    runtime = Runtime.CLI
    executable = "doubao"
    docs_url = "https://www.doubao.com/work/docs/zh-cn/doubao-cli"
    logo = "/icons/kernels/doubao.png"       # 真彩色位图（见 §5）
    logo_style = "image"
    api_key_env = None                        # 账号登录，没有 API Key
    base_url_env = None
    default_gateway = None                    # bootstrap 因此跳过官方网关播种
    default_model = None                      # Auto
    protocol = "openai"                       # 仅占位，不参与路由
    capabilities = Capabilities(
        streaming=True,
        sessions=True,
        native_system_prompt=False,           # 人格拼进提示词
        tools=True,
        mcp=False,                            # 无本地 MCP 配置
        permission_modes=True,
        working_directory=True,
        skills=True,
    )
    provisioner = RuntimeProvisioner(
        manager="npm",
        package="@bytedance/doubao-cli",
        install_argv=npm_install_argv(
            "@bytedance/doubao-cli", bin_name="doubao",
            registry="https://registry.npmjs.org/",
        ),
        update_argv=npm_install_argv(
            "@bytedance/doubao-cli", bin_name="doubao", latest=True,
            registry="https://registry.npmjs.org/",
        ),
        uninstall_argv=npm_cli("uninstall", "-g", "@bytedance/doubao-cli"),
        latest_argv=npm_cli("view", "@bytedance/doubao-cli", "version"),
        prepare_argv=npm_prepare_argv("@bytedance/doubao-cli"),
        homepage="https://www.doubao.com/work/docs/zh-cn/doubao-cli",
        note="仅支持 Windows x64 / macOS ARM64 / Linux x64；Linux 需要 libgcrypt20。",
    )
```

`npm_install_argv` 需要加一个可选 `registry` 参数（官方命令明确带 `--registry`，
自建镜像上可能没有这个包）。这是 `util.py` 的小改动，所有 npm 内核共用。

### 3.2 config_fields

| key | 类型 | 默认 | 链路 |
| --- | --- | --- | --- |
| `permission_mode` | SELECT | `always_allow` | `--permission-mode`；help 写明无人值守风险 |
| `bypass_sandbox` | BOOLEAN | `false` | `--dangerously-bypass-approvals-and-sandbox`（与上一项互斥） |
| `effort` | SELECT | 空 | `--effort`；空 = 用模型默认 |
| `extra_args` | STRING_LIST | — | 兜底旗标 |
| `env` | KEYVALUE | — | 框架已支持 |

模型不单独做 ConfigField：智能体的「模型」字段在「官方默认」下就是自由输入，
直接映射到 `--model`。help 里提示可用 `doubao model list` 查看。

### 3.3 build_invocation

```python
argv = ["doubao", "-C", str(ctx.workspace_dir or ".")]
argv += ["--output-format", "stream-json"]
if ctx.model:
    argv += ["--model", ctx.model]
if options.get("effort"):
    argv += ["--effort", options["effort"]]
if options.get("bypass_sandbox"):
    argv += ["--dangerously-bypass-approvals-and-sandbox"]
else:
    argv += ["--permission-mode", options.get("permission_mode") or "always_allow"]
if ctx.session_id:
    argv += ["--resume", ctx.session_id]
argv += [str(a) for a in as_list(options.get("extra_args"))]
argv += ["-p", "-"]                                   # 提示词走 stdin

prompt = ctx.prompt
if ctx.system_prompt:                                  # 人格拼最前面
    prompt = f"{ctx.system_prompt}\n\n---\n\n{prompt}"

env = self.effective_env(ctx)
env.setdefault("CI", "1")
env.setdefault("NO_COLOR", "1")
env.setdefault("DOUBAO_CLI_NO_UPDATE_CHECK", "1")      # 无人值守不弹更新
if ctx.home_dir:
    real_home = os.environ.get("HOME", "")
    env["HOME"] = ctx.home_dir                          # 技能根隔离
    # app 与登录态留在真实数据目录
    env.setdefault(
        "XDG_DATA_HOME",
        os.environ.get("XDG_DATA_HOME") or str(Path(real_home) / ".local" / "share"),
    )
    # HOME 换了之后 runner 补的是隔离目录下的 .local/bin；真实安装目录要自己保住
    real_bin = str(Path(real_home) / ".local" / "bin")
    env["PATH"] = f"{real_bin}:{os.environ.get('PATH', '')}"
return Invocation(argv=argv, env=env, cwd=ctx.workspace_dir, stdin=prompt)
```

要点：

- `-C` 放最前（CLI 帮助里明确「必须放在业务参数之前」）。
- `-p -` 走 stdin，规避 Windows 32K 命令行上限与引号转义。
- 只设 `HOME`、不设 `XDG_DATA_HOME` 会找不到原生程序；两个都要显式给。

### 3.4 parse_line

```python
def parse_line(self, line, run):
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
    yield from self._dispatch(payload, run)

def _dispatch(self, payload, run):
    kind = payload.get("type")
    sid = payload.get("session_id")
    if sid and sid != run.state.get("session"):
        run.state["session"] = sid
        yield session(sid)

    if kind == "stream_event":
        event = payload.get("event") or {}
        if event.get("type") == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                run.state.setdefault("streamed", set()).add(payload.get("message_id"))
                yield text_delta(delta["text"])
    elif kind == "assistant":
        yield from self._from_assistant(payload.get("message") or {}, run)
    elif kind == "user":
        yield from self._from_tool_results(payload.get("message") or {})
    elif kind == "result":
        yield from self._from_result(payload, run)
```

`_from_assistant` 按 `message_id` 判断是否已经流过增量；`_from_tool_results` 读
`isError`（注意是驼峰）；`_from_result` 只在 `is_error` 时发 `error`，
并把 `permission_denials` 用 `log(..., "warn")` 回显（否则用户只看到任务没做完）。

### 3.5 技能隔离

```python
def skill_roots(self):
    home = Path.home()
    return [
        SkillRoot(str(home / ".agents" / "skills"), source_kind="user"),
        SkillRoot(str(home / "Doubao" / "skills"), source_kind="user"),
    ]

def skill_mount(self, ctx):
    notes = []
    if ctx.workspace_dir:
        for name in (".agents", ".claude", ".codex", ".gemini", ".trae", ".workbuddy"):
            extra = Path(ctx.workspace_dir) / name / "skills"
            if extra.is_dir() and any(extra.iterdir()):
                notes.append(f"工作目录自带的 {extra} 会被内核额外加载，这些技能不在分配范围内")
    return SkillMount(
        home_name="doubao-home",
        skills_subdir=".agents/skills",     # 与 HOME 覆盖配合：只暴露分配到的技能
        notes=notes,
    )
```

> `skills_subdir` 会以 `home / mount.skills_subdir` 的方式拼接，`".agents/skills"`
> 这种带层级的相对路径可以直接用（`mkdir(parents=True)` 会建出两级目录）。
> 框架语义不用改。

### 3.6 可用性与连通性测试

- 继承默认 `availability()`：`shutil.which("doubao")` + `doubao --version`，无需登录。
- 覆盖 `probe()`：依次跑 `doubao --version` 与 `doubao auth status`（**不要**用
  `--verify`，它会联网，慢且受代理影响），未登录时明确返回
  「CLI 已安装，但未登录：请在宿主机终端执行 `doubao login`」。
- 不新增「登录」按钮前，内核详情页靠 `note` + 连通性测试告诉用户下一步。

---

## 4. 登录在平台侧怎么落地

登录是二维码交互，平台现在没有「内核登录」这个概念。建议分两步：

### P0（本方案必做）：宿主机手工登录 + 状态可见

- 文档与内核详情提示：在宿主机（或容器内）执行 `doubao login`，扫码一次即可。
- 「连通性测试」检查 `doubao auth status`，未登录给明确中文提示。
- 这是与 CodeBuddy / Claude Code 等账号型内核一致的做法，改动最小。

### P2（已随首版实现）：内核管理里「一键登录」

复用安装日志那套 SSE：

1. 给 `RuntimeProvisioner` 增加可选 `login_argv`，`Action` 加 `"login"`；
2. 后端启动 `doubao login --qr=png`，从输出里截 `QR_IMAGE=<路径>`；
3. **不要**把服务器临时路径直接给浏览器：后端读取图片（校验 PNG 魔数、≤2 MB），
   以 `data:` URL 形式塞进 `provision.output` 事件；前端 `<img>` 直接显示；
4. 轮询 / 等待命令退出后自动跑 `doubao auth status` 回报结果；
5. 仅管理员可触发（与安装 / 卸载同权限）。

> 这一步会动 `provisioning.Action` / API 路由 / 桌面与移动两套前端，
> 建议独立排期，别和适配器首版混在一起。

---

## 5. 内核管理换成「实打实的图标」

### 5.1 现状与问题

- `AgentDefinition.logo` 只允许 `/icons/kernels/*.svg`，前端
  `KernelLogo.tsx` 用 `mask-image` + `background-color: currentColor`
  把图形染成文字色 —— 结果是**单色剪影**，不是品牌图标。
- 守卫测试（`backend/tests/test_frontend_assets.py`）把这条约定写死了：
  必须以 `.svg` 结尾、必须有 `viewBox="0 0 24 24"`、必须有 `<path>`。
- 用户要的是「实打实的图标」：真彩色、位图（PNG/WebP），例如那张
  `102×102`、带透明通道的 PNG。单色遮罩满足不了。

### 5.2 契约扩展（向后兼容）

`AgentDefinition` 增加一个可选的展示样式声明：

```python
#: Logo 的渲染方式：mask = 单色 SVG 当遮罩染色（现状）；image = 真彩色位图/彩色 SVG
logo_style: ClassVar[str] = "mask"
```

- 旧内核不改，继续走 `mask`。
- 新内核（含豆包）用 `logo_style = "image"` + `/icons/kernels/doubao.webp`。
- 约定：`logo` 指向 `/icons/kernels/` 下的静态资源；`.svg` 默认 `mask`，
  `.png/.webp` 默认 `image`（显式声明优先）。
- `descriptor()` / `api/runtimes.py` 把 `logo_style` 一起下发；
  前端 `AgentRuntime` 类型加 `logo_style?: 'mask' | 'image'`。

### 5.3 前端渲染

`KernelLogo.tsx` 改成双形态：

```tsx
export function KernelLogo({ logo, style }: { logo?: string | null; style?: 'mask' | 'image' | null }) {
  const [broken, setBroken] = useState(false)
  useEffect(() => setBroken(false), [logo])
  if (!logo) return null
  const image = (style ?? (/\.(png|webp|jpe?g)$/i.test(logo) ? 'image' : 'mask')) === 'image'
  if (image) {
    if (broken) return null // 或退回到传入的 fallbackLogo
    return <img className="kernel-logo kernel-logo-image" src={logo} alt="" loading="lazy" onError={() => setBroken(true)} />
  }
  return <span className="kernel-logo" aria-hidden="true"
    style={{ WebkitMaskImage: `url(${logo})`, maskImage: `url(${logo})` }} />
}
```

CSS（桌面 `index.css` 与移动端 `mobile.css` 各一份）：

```css
.kernel-logo { width: 24px; height: 24px; flex: none; }        /* 统一尺寸 */
.kernel-logo-image {
  object-fit: contain;
  background: transparent;      /* 覆盖 .kernel-logo 的 currentColor 背景 */
  mask: none;
  border-radius: 6px;           /* 透明图也有个柔和边界 */
}
```

- 深色 / 浅色主题都直接显示品牌原色，不再染色；透明底图标不会糊。
- 图片加载失败 → 回退到中性图标或文字首字（与技能市场 `SkillIcon` 同一套思路），
  界面里不出现裂图。
- 手机端的内核卡片同步改（`MobileRuntimesPage.tsx` 已用同一个组件，只改样式）。

### 5.4 资产从哪来、怎么进仓库

原则：**随仓库发布、离线可用、运行时不外链**。示例里的 CDN URL 只作为下载源，
不允许前端直接引用（桌面版断网、CSP、隐私与引用稳定性都不可控）。

1. 每个内核一张**真彩色、透明底、正方形**的源图（≥ 256×256，PNG/WebP）。
2. 用脚本归一化（Pillow 已是仓库既有工具链）：
   - 裁掉透明边距，居中补成正方形；
   - 缩放到 128×128，导出 PNG（透明底）；
   - 输出到 `frontend/public/icons/kernels/<id>.png`。
   本张图按同流程人工处理；后续要批量做，再补 `scripts/normalize_kernel_icons.py`
   （与 `gen_avatar_pack.py` 同风格）。
3. 同目录新增 `SOURCES.md`：记录每张图的来源 URL、抓取日期、授权/商标说明。
4. 位图加载失败时界面**直接隐藏**该内核的 Logo（不拿旧单色 SVG 兜底，
   避免显示一个与品牌不符的图形）；品牌图形用 `logo_style` 声明，不做隐式猜测。

豆包这一张已经拿到官方素材：`豆包工作` 头图（349×93 PNG）左侧的品牌标记，
裁成 93×93 后归一化即可，不需要手绘、也不需要 AI 生成。

### 5.5 测试与文档同步

- `test_frontend_assets.py`：
  - 允许 `.svg` / `.png` / `.webp`；
  - SVG 仍校验 `viewBox="0 0 24 24"` 与 `<path>`；
  - 位图校验魔数（PNG/WebP）、尺寸 ≤ 512、文件 ≤ 256 KB；
  - 断言 `KernelLogo` 同时包含 `maskImage` 与 `<img`，防止有人把其中一种形态删掉。
- 文档三处要一起改：
  - `AGENTS.md`（§ 内核 Logo 约定）
  - `docs/architecture.md`（4.1 运行时管理）
  - `docs/ai-coding-standard.md`（6.1 适配器骨架里的 `logo` 说明）
- `README.md` 的「都能接什么」表加一行 `doubao-cli`。

---

## 6. 需要动的文件

| 文件 | 改动 |
| --- | --- |
| `backend/app/agents/builtin/doubao_cli.py` | **新增**适配器 |
| `backend/app/agents/builtin/util.py` | `npm_install_argv(..., registry=None)` |
| `backend/app/agents/base.py` | `AuthStatus`、`logo_style`、`auth_status()`、`RuntimeProvisioner.login_argv` |
| `backend/app/runtime/provisioning.py` | `login` 动作、`QR_IMAGE=` 解析、`provision.qr` 事件 |
| `backend/app/api/runtimes.py` | `logo_style` 下发、`GET /auth`、`POST /login` |
| `frontend/src/lib/types.ts` / `api.ts` | `RuntimeAction` 加 `login`、登录状态与二维码事件 |
| `frontend/src/components/KernelLogo.tsx` | 双形态（mask / img） |
| `frontend/src/components/KernelManager.tsx` | 登录按钮、登录状态、二维码弹层 |
| `frontend/src/mobile/pages/MobileRuntimesPage.tsx` | 移动端同上 |
| `frontend/src/index.css`、`frontend/src/mobile/mobile.css` | `.kernel-logo-image`、`.login-qr` |
| `frontend/public/icons/kernels/doubao.png` + `SOURCES.md` | **新增**真实品牌图 |
| `backend/tests/test_doubao_cli.py` | 启动参数、事件解析、登录态、登录二维码 |
| `backend/tests/test_architecture.py` / `test_frontend_assets.py` / `test_api.py` | 注册表、图标双形态、内置内核计数 |
| `docker/Dockerfile` | 补 `libgcrypt20 libgnutls30` |
| `README.md` / `AGENTS.md` / `docs/architecture.md` / `docs/ai-coding-standard.md` | 内核表与图标约定 |
| `docs/doubao-cli.md` | 本文（调研依据） |

不需要改：事件协议、runner / skills / mcp 物化层、表单渲染（全部由 descriptor 驱动）。

---

## 7. 风险与已知限制

1. **登录态是设备级、不是用户级**：平台多用户共享同一份豆包登录。
   文档要写清楚；需要按用户隔离时只能一机一账号，成本另算。
2. **登录无法在 CI / 无网环境完成**：必须人工扫码；平台已在内核详情弹层提供二维码登录，
   但扫码这一步只能在有真实账号与网络的环境里完成。
3. **并发未知**：`doubao-cli-instance.lock` 与 `/tmp/dcli-<uid>` 控制通道是否允许
   并行 `-p` 进程尚未实测。协作域会并行跑多个智能体，**这是 P0 验收项**；
   如果互斥，需要加进程内/文件级串行锁，并在文档里写明并发上限。
4. **平台限制**：Intel Mac、Linux arm64 安装不了；适配器要在可用性/连通性测试里说人话。
5. **Linux 系统依赖**：缺 `libgcrypt20` 时 `--version` 都跑不起来；
   Docker 必须补包，降级方案是提示用户改用 custom-cli。
6. **安装体积与时长**：236 MB 原生包，首次安装明显更久；900 秒总超时一般够，
   慢网要预期失败并重试。
7. **没有 token 用量**：界面上的用量统计对该内核恒为空，不要伪造。
8. **人格只能拼提示词**：`AGENTS.md` 会写进用户项目，第一版不碰；
   若后续要原生人格，需要确认 CLI 是否有系统提示词参数/环境变量。
9. **MCP 不支持**：不要声明 `mcp=True`，否则架构守卫会要求一个并不存在的配置面。
10. **商标与授权**：品牌图随仓库分发前，在 `SOURCES.md` 记录来源与使用范围；
    示例 CDN 图只做下载源，不做运行时热链。

---

## 8. 落地顺序与验收

**P0 — 跑通 + 图标（1 个适配器 + 图标链路）**

- 适配器骨架：元数据、`build_invocation`、`parse_line`、技能隔离、`probe`；
- `logo_style` 契约 + `KernelLogo` 双形态 + 豆包真实图标；
- Docker 补库；README / 架构文档更新；
- 测试：真实 `stream-json` 样本解析、注册表、图标守卫。
- 验收：宿主机 `doubao login` 后，平台新建豆包智能体 → 流式回复、工具调用、
  多轮续接正常；内核管理能安装 / 更新 / 卸载；图标显示为真彩色。

**P1 — 并发与隔离实测**

- 两个以上对话同时运行，确认实例锁 / 控制通道不冲突；
- 技能分配：只看到分配到的技能，工作区自带技能有告警；
- 平台不支持架构上的中文报错。

**P2 — 一键登录（可选）**

- `RuntimeProvisioner.login_argv` + `Action("login")` + SSE 二维码（data URL）；
- 管理员权限、失败重试、登录后自动 `auth status`。

**P3 — 增强（可选）**

- 若 `doubao` 提供系统提示词/技能开关的环境变量，改成原生人格注入；
- `--list` / `--export` 做成「导入历史会话」；
- 用量统计：等 CLI 输出 token 用量后再接 `usage` 事件。

---

## 9. 待实测清单（P0 必须补齐）

- [ ] `doubao -p - --output-format stream-json` 登录后的真实事件样本（把 5 类事件各留一条）。
- [ ] `message.delta` 与 `message.completed` 的去重：确认没有丢字/重字。
- [ ] `--resume` 第二轮是否只发增量、历史是否自动带上。
- [ ] 两个并发 `-p` 进程是否互斥。
- [ ] `HOME` 覆盖 + `XDG_DATA_HOME` 指向真实目录时，登录态是否正常复用。
- [ ] `--permission-mode always_allow` 在无人值守下不会卡住等输入。
- [ ] `-p` 超长提示词（>262144 码点）的实际报错形态。
