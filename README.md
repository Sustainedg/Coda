<div align="center">

<img src="./frontend/public/icons/icon-512.png" width="128" alt="Coda Logo" />

# Coda

**把 AI 编程内核，变成你的聊天同事。** 🐝

Claude Code · Codex · Antigravity CLI · 任意 OpenAI 兼容端点 ——
配上网关、人格、技能与 MCP，在浏览器里像发消息一样使唤它们；
多智能体「工单」协作看得见，手机 PWA 与 7 大 IM 平台随时在线。

[🚀 快速开始](#-快速开始) · [✨ 核心特性](#-核心特性) · [🔌 都能接什么](#-都能接什么) · [🔐 安全](#-你的密钥安全吗) · [❓ 常见问题](#-常见问题) · [⭐ Star 曲线](#-star-曲线)

![Release](https://img.shields.io/github/v/release/Sustainedg/Coda?style=for-the-badge&color=6b4cf0)
![Stars](https://img.shields.io/github/stars/Sustainedg/Coda?style=for-the-badge&color=e053d6&label=%E2%AD%90%20Stars)
![Forks](https://img.shields.io/github/forks/Sustainedg/Coda?style=for-the-badge&color=9d7bff)
![Issues](https://img.shields.io/github/issues/Sustainedg/Coda?style=for-the-badge&color=ffd980)

![Python 3.11+](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![React 18](https://img.shields.io/badge/React-18-61DAFB?style=for-the-badge&logo=react&logoColor=black)
![TypeScript](https://img.shields.io/badge/TypeScript-5-3178C6?style=for-the-badge&logo=typescript&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-aiosqlite-003B57?style=for-the-badge&logo=sqlite&logoColor=white)

</div>

---

## 🤔 这是为谁准备的

| 你是不是也这样？ | Coda 的回答 |
| --- | --- |
| 😩 **内核很强，但终端很烦** —— Claude Code 每次都要开终端、敲命令、记参数 | 在浏览器里像发消息一样用它，流式回复、工具调用、推理过程实时展开 |
| 🔑 **手里一把 API Key** —— 散落在各处 `.bashrc`，谁都能看 | 网关统一加密保管，智能体只拿得到掩码，日志自动脱敏 |
| 🤝 **想让它们搭伙干活** —— 一个写代码、一个做审查，全靠自己人肉接力 | 可视化编排「工单」协作：CEO 派活、员工执行、状态机与护栏兜底 |
| 📱 **人不在电脑前** —— 智能体跑着，我却要回控制台才能看 | 独立移动端 PWA + Telegram / 飞书 / 钉钉等 7 大 IM，在哪都能喊它 |

---

## ✨ 核心特性

| 🧩 内核即智能体 | 🔑 密钥统一管家 | 🗣️ 多智能体协作 |
| --- | --- | --- |
| 把 Claude Code / Codex / Antigravity CLI 配成一个个「员工」：头像、人格、技能、MCP 各自定义 | 网关是 API 密钥的唯一来源：Fernet 加密落库、接口只回掩码、日志自动脱敏 | 可视化编排协作组：工单在岗位间接力，「办公室实况」页面实时看到谁在忙、谁卡住、谁在等你 |
| 🧰 技能与 MCP 编目 | 🎛️ 对话式系统管理 | 📱 双端在线 |
| 各内核的技能与 MCP 服务器统一编目，「严格仅分配」才生效，不给就绝不冒用 | 平台自带「系统管理」MCP + 个人访问令牌，智能体在对话里就能帮你管智能体 / 技能 / MCP | 主站之外物理独立的 `/m/` 移动端 PWA；IM 连接只用长连接，**不需要公网地址** |
| 📊 用量透明 | 🔌 新智能体是插件 | 🐳 部署即所得 |
| token 消耗按日期 / 智能体 / 对话汇总，花了多少一目了然 | 加一个文件即可接入新智能体，表单、SSE 流、会话记忆、权限全部由框架统一处理 | 镜像「构建什么就运行什么」，界面一键升级，**不用重建容器** |

> 🖼️ 下面这张是协作进度页「办公室实况」的底图 —— 每个在岗智能体都是坐在工位上的小人，姿势跟着真实状态走（打盹 / 敲键盘 / 举手提问 / 出错 / 收工）：

<div align="center">

<img src="./frontend/public/office/iso-office.svg" width="800" alt="Coda 办公室实况底图" />

</div>

---

## 🚀 快速开始

**最快的一条路**（源码运行，一条命令起服务）：

```bash
git clone https://github.com/Sustainedg/Coda.git
cd Coda

./scripts/serve.sh            # → http://<本机IP>:8010
```

自动建 venv、构建前端，UI 与 API 由后端统一托管，浏览器打开就能用。

**前后端分离开发**（热更新）：`./scripts/dev.sh` → API `:8000` + Web UI `:5173`

<details>
<summary><b>🐳 用 Docker 跑（点开看完整步骤）</b></summary>

镜像里封装好 Python 3.11 / Node 24 / git，后端与前端都在构建时打进镜像
（「构建什么就运行什么」；前端由镜像内的 node 阶段现构建，不需要本地先打包）：

```bash
git clone https://github.com/Sustainedg/Coda.git
cd Coda

docker compose -f docker/docker-compose.yml up -d --build
# → http://<本机IP>:8010
```

改端口：`CODA_HTTP_PORT=8080 docker compose -f docker/docker-compose.yml up -d`

**两个卷各管什么**（数据安全必读）：

| 卷 | 挂载点 | 内容 | 删了会怎样 |
| --- | --- | --- | --- |
| `coda-app` | `/app` | 版本目录 `releases/` + Python venv | 下次启动从镜像自动重建（**离线秒级**），对话数据不受影响 |
| `coda-data` | `/app/data` | SQLite、工作区、`secret.key`、内核 HOME | ⚠️ **数据全丢**（对话、智能体、凭据、登录态），无法恢复 |

💡 只清理应用代码时删 `coda-app` 就好，**千万别连 `coda-data` 一起删**。

**国内网络加速**（首启空卷要装 Python 依赖，直连 PyPI 实测只有 ~15KB/s，二十多分钟）：

```yaml
environment:
  # 方式一：国内镜像源（不需要代理，实测 17–24 秒装完）
  CODA_PIP_INDEX_URL: "https://pypi.tuna.tsinghua.edu.cn/simple"
  # 方式二：安装期代理（pip 也会用）
  CODA_INSTALL_PROXY: "http://<proxy-host>:<port>"
```

两个都配也行（代理优先）。**真实地址请写在你自己的部署 compose 里，不要提交进仓库。**

**让智能体读写宿主机项目** —— 容器内外必须用同一个路径，否则对话里记录的工作目录在容器内不存在：

```bash
HOST_PROJECT_DIR=/path/to/projects \
  docker compose -f docker/docker-compose.yml up -d
```

（前提是 `docker/docker-compose.yml` 里那行 `volumes` 已取消注释。）

</details>

### 首次启动会发生什么

- 为本机**已安装**的智能体类型各自动创建一条官方网关，并配套一个起始智能体；
- 数据全部落在 `backend/data/`（SQLite + 每个对话独立的工作目录），不碰别的任何地方；
- 默认开启多用户鉴权与注册；`CODA_ADMIN_USERNAME` / `CODA_ADMIN_PASSWORD` 可指定首任管理员，不填则**第一个注册的账号自动成为管理员**。

<details>
<summary><b>全部环境变量一览（都可选）</b></summary>

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `CODA_HOST` | `0.0.0.0` | 监听地址 |
| `CODA_PORT` | `8010` | 监听端口 |
| `CODA_PUBLIC_BASE_URL` | 按端口推导 | 平台对外地址（系统管理 MCP 自动填写）；反向代理后面请填实际可访问地址 |
| `CODA_DATA_DIR` | `backend/data` | 数据目录 |
| `CODA_AUTH_ENABLED` | `true` | 多用户鉴权 |
| `CODA_REGISTRATION_OPEN` | `true` | 开放注册 |
| `CODA_ADMIN_USERNAME` | — | 首任管理员用户名（首启有效）|
| `CODA_ADMIN_PASSWORD` | — | 首任管理员密码（首启有效）|
| `CODA_RUN_TIMEOUT_SECONDS` | 不限时 | 运行超时护栏（普通对话默认不限时，可随时中断）|
| `CODA_SECRET_KEY` | 自动生成 | 密钥加密密钥；未设置时自动生成 `data/secret.key`（0600）|

完整清单见 [`backend/.env.example`](./backend/.env.example) 与 [`docker/docker-compose.yml`](./docker/docker-compose.yml)。

</details>

---

## 🧭 上手只要五步

0. **设置 → 内核管理** —— 按需安装内核（Claude Code / Codex / Antigravity CLI / Hermes / DeepSeek Harness / 豆包 CLI）。镜像与首次启动都**不预装**内核，不装就没有可用的智能体类型（为什么？见 [FAQ](#-常见问题)）；豆包这类账号型内核在详情里点「登录」扫码即可。
1. **设置 → 自定义网关** —— 新建网关（名称、地址、协议、鉴权、API 密钥），点「获取模型」拿到可选模型并勾选
2. **智能体 → 新建** —— 选内核类型、选网关、定默认模型，写头像与系统提示词，按需勾选技能 / MCP，点「连通性测试」确认一次通过
3. **对话 → 新建对话** —— 选择智能体开始聊天；工具调用、推理过程、token 用量实时展开
4. **设置 → IM**（可选）—— 把智能体连到 Telegram / 飞书 / 钉钉 / 企业微信 / QQ / Discord / Slack，配对账号后在聊天软件里直接对话

> 💡 想看多智能体怎么「搭伙干活」？打开 [`docs/collab-guide.html`](./docs/collab-guide.html)（浏览器直接打开的图文版），一分钟看懂工单协作。

---

## 🔌 都能接什么

### 内核 / 模型端

| 适配器 | 接的是什么 | 会话续接 | 原生人格 | 一句话 |
| --- | --- | --- | --- | --- |
| `claude-code` | CLI `claude` | ✅ `--session-id` / `--resume` | ✅ | 权限模式、MCP、工具白名单全支持 |
| `codex` | CLI `codex` | ✅ `exec resume` | ➖ 注入到 prompt | `-c model_providers.*` 指向自定义网关 |
| `antigravity-cli` | CLI `agy` | ✅ `--conversation` | ➖ 注入到 prompt | Google Antigravity CLI：OAuth 账号登录（内核详情里点「登录」出授权链接 / 二维码，粘贴授权码完成）；无 API Key，不接管网关；v1 暂不接 MCP |
| `codebuddy-cli` | CLI `codebuddy` | ✅ `--resume` | ✅ | 腾讯 CodeBuddy Code：内核详情里点「登录」出授权二维码（`--serve` API），支持**多账号存档与切换**；也支持环境变量凭据；自定义网关走 `models.json`（仅 OpenAI 兼容） |
| `doubao-cli` | CLI `doubao` | ✅ `--resume` | ➖ 注入到 prompt | 豆包 CLI：扫码登录（无 API Key），内核详情里直接出二维码 |
| `deepseek-harness` | CLI `dsh` | ✅ `--session-id` | ✅ 写入 `AGENTS.md` | DeepSeek Harness headless：JSON 事件、沙箱权限、技能与 MCP |
| `openai-harness` | 进程内 API | ✅ | ✅ | 任意 OpenAI 兼容端点：OpenRouter、vLLM、Ollama…（不再出现在新建入口） |
| `custom-cli` | 任意 CLI 命令 | 可声明 | ➖ | **零代码**接入私有 CLI 智能体（不再出现在新建入口） |

> `openai-harness` 与 `custom-cli` 是逃生舱模板：存量智能体照旧可编辑、可运行，
> 但「新建智能体」的内核下拉只列正式内核，不再推荐这两个模板。
>
> DeepSeek Harness（`dsh`）要求 **Node.js ≥ 24.9**：Docker 镜像与桌面版已内置 Node 24，
> 源码直接运行时请先确认本机 Node 版本。
>
> CodeBuddy CLI（`codebuddy`）要求 **Node.js ≥ 18.20.8**（官网建议 22+）：Docker 镜像与
> 桌面版已满足；源码直接运行时请先确认版本。它的自定义网关只支持 OpenAI 兼容接口，
> 会写成隔离目录里的 `models.json`。

### IM 平台（只用长连接 / 长轮询，不开公网端口）

| 平台 | 私聊 | 群聊 | 平台 | 私聊 | 群聊 |
| --- | :---: | :---: | --- | :---: | :---: |
| Telegram | ✓ | @ 机器人 | Discord | ✓ | @ 机器人 |
| 飞书 / Lark | ✓ | @ 机器人 | Slack | ✓ | @ 机器人 |
| 钉钉 | ✓ | @ 机器人 | QQ（官方机器人） | ✓ | @ 机器人 |
| 企业微信 | ✓ | @ 机器人 | 微信个人号 | *即将支持* | — |

---

## 🔐 你的密钥安全吗

这是 Coda 最认真的一块，逐条说清楚：

- **密钥加密落库**：API 密钥与凭据用 Fernet 加密存储，接口只回掩码（`sk-a…9f2c`）；加密密钥来自 `CODA_SECRET_KEY`，未设置时自动生成 `data/secret.key`（权限 0600）
- **凭据只以环境变量注入**：模型只知道变量名（如 `$GITHUB_TOKEN`），值不进提示词、日志与接口响应；运行层逐条事件擦除凭据值
- **日志自动脱敏**所有出现在 env 中的凭据
- **路径护栏**：API 型智能体的工具被限制在工作目录内，`../` 逃逸会被拒绝
- **超时与取消**：普通对话默认不限时，可随时中断（消息标记 `interrupted`）；需要护栏的部署可设 `CODA_RUN_TIMEOUT_SECONDS`

> ⚠️ `bypassPermissions` / `danger-full-access` 等无人值守模式，请只在沙箱里使用。

---

## 📦 数据与升级

**数据主权在你手里**：所有数据（对话、智能体、凭据、用量）都落在本地 SQLite —— `backend/data/`（Docker 里是 `coda-data` 卷）。没有云端，没有遥测，备份这一个目录就等于备份了全部。

| 部署方式 | 升级动作 |
| --- | --- |
| 源码运行 | `git pull` →（前端有变动时）`(cd frontend && npm install && npm run build)` → `./scripts/serve.sh` 重启 |
| Docker | 界面「**设置 → 关于 → 检测更新**」一键升级：下载 Release → 解到 `releases/<版本>/` → 原子切换 → 原地重启，**不需要重建镜像**。只有改了 `Dockerfile` / `entrypoint.sh` / 系统依赖时才 `up -d --build` |

---

## ❓ 常见问题

<details>
<summary><b>为什么镜像里不预装内核？</b></summary>

刻意的设计，三个理由：内核更新频繁，烘进镜像等于每次都要重建；内核要装进 `$HOME`（即 `/app/data/home`）才能**保住登录态**，容器重建后不用重新登录；不预装还能让镜像保持小巧。

**部署完成后**：打开「设置 → 内核管理」按需点「安装」（需要管理员账号）。装好的内核落在 `/app/data/home/.local`，跟着 `coda-data` 卷走，容器重建不丢。

想让容器启动时自动补装缺失内核？把 `CODA_SKIP_KERNEL_INSTALL` 改成 `"0"`（网络差时会拖慢启动，默认跳过保持快速可预测）。
</details>

<details>
<summary><b>Docker 首次启动装依赖特别慢？</b></summary>

直连 PyPI 实测只有 ~15KB/s。给 compose 配上 `CODA_PIP_INDEX_URL: "https://pypi.tuna.tsinghua.edu.cn/simple"`（实测 17–24 秒装完）或 `CODA_INSTALL_PROXY` 安装期代理。内核安装（npm / curl 脚本）同理建议配好出口。
</details>

<details>
<summary><b>怎么让智能体访问我宿主机上的项目文件？</b></summary>

容器内外必须用**同一个路径**挂载（`HOST_PROJECT_DIR=/path/to/projects`），否则对话里记录的工作目录在容器内不存在。源码运行没有这个问题，直接就是宿主机路径。
</details>

<details>
<summary><b>升级会丢数据吗？</b></summary>

不会。Docker 升级走界面「检测更新」，数据在 `coda-data` 卷里纹丝不动；源码升级只要不动 `backend/data/` 目录就没影响。**唯一会丢数据的操作**是把 `coda-data` 卷删了 —— 那是唯一存放业务数据的地方。
</details>

<details>
<summary><b>忘记管理员密码 / 谁是管理员？</b></summary>

不设 `CODA_ADMIN_USERNAME` 时，**第一个注册的账号自动成为管理员**。多用户鉴权与开放注册默认开启，可以在环境变量里调整。
</details>

---

## 🛠️ 给开发者

<details>
<summary><b>架构：新增一个智能体不需要碰前端 / 流式 / 数据库</b></summary>

```
frontend (React + TS + Vite)
   │  REST /api/*            SSE  POST /api/conversations/{id}/messages
   ▼
FastAPI  ── api/          agents · gateways · conversations · runs · system
         ── runtime/      service(编排+持久化) · runner(执行) · manager(取消/追踪)
         ── agents/       ★ 适配器插件层（唯一需要扩展的地方）
         ── models/db      SQLite + SQLModel
```

所有智能体输出统一翻译成**规范化事件协议**，前端与持久化只认这一套：

```
run.started   message.delta   thinking.delta   tool.call   tool.result
usage         session         log              error       run.finished
```

易变的部分（各内核的调用方式与输出格式）被压缩进一个极薄插件层，稳定部分（流式、会话记忆、持久化、表单、取消与超时）全沉淀在框架。深挖见 [`docs/architecture.md`](./docs/architecture.md)。

</details>

<details>
<summary><b>接入一个新智能体的两种方式</b></summary>

**方式 A：写一个适配器**（推荐，能力完整）—— 在 `backend/app/agents/builtin/` 新建模块：

```python
from ..base import AgentContext, AgentDefinition, Capabilities, ConfigField, FieldType, Invocation, Runtime
from ..events import AgentEvent, session, text_delta, usage
from ..registry import register_agent

@register_agent
class AcmeAgent(AgentDefinition):
    id = "acme"
    name = "Acme 智能体"
    runtime = Runtime.CLI
    executable = "acme"
    capabilities = Capabilities(streaming=True, sessions=True, native_system_prompt=True, tools=True)

    def build_invocation(self, ctx: AgentContext) -> Invocation:
        argv = ["acme", "run", "--json"]
        if ctx.model:         argv += ["--model", ctx.model]
        if ctx.session_id:    argv += ["--resume", ctx.session_id]
        if ctx.system_prompt: argv += ["--system-prompt", ctx.system_prompt]
        return Invocation(argv=argv, env=self.effective_env(ctx),
                          cwd=ctx.workspace_dir, stdin=ctx.prompt)

    def parse_line(self, line: str, run):
        payload = json.loads(line)                     # 按自己的输出格式解析
        if payload.get("session"): yield session(payload["session"])
        if payload.get("text"):    yield text_delta(payload["text"])
```

重启后即可在「智能体配置」里看到它，表单由 `config_fields` 自动生成。**API 型智能体**改为实现 `async def stream(self, ctx)`，参考 `openai_harness.py`。

**方式 B：自定义 CLI（零代码）** —— 用内置的 `custom-cli` 适配器声明一个任意命令：

```jsonc
{
  "command": "aider",
  "args": ["--model", "{model}", "--message", "{prompt}"],
  "prompt_mode": "arg",
  "output_format": "jsonl",
  "text_path": "message.content",      // JSON 取值路径 → 文本增量
  "session_path": "session_id",        // 取到即启用多轮
  "tool_path": "tool.name",            // 工具调用
  "usage_path": "usage"
}
```

占位符：`{prompt} {model} {gateway} {api_key} {workspace} {session} {agent}`。

</details>

<details>
<summary><b>HTTP API 摘要</b></summary>

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/agent-types` | 所有适配器及其 config schema、安装状态 |
| GET/POST | `/api/agents` | 列出 / 创建智能体 |
| PATCH/DELETE | `/api/agents/{id}` | 更新 / 删除（软删除：历史对话保留） |
| POST | `/api/agents/{id}/test` | 连通性探测（适配器 / 运行时 / 网关三段检查） |
| PUT | `/api/agents/{id}/skills` · `/mcp-servers` | 设置技能 / MCP 分配 |
| GET/POST | `/api/gateways` | 列出 / 创建网关 |
| POST | `/api/gateways/probe` | 按填写的地址 / 协议 / 鉴权 / 密钥**获取模型**（只探测，不落库） |
| GET | `/api/runtimes` | 各运行时的安装状态、版本与维护方式 |
| POST | `/api/runtimes/{id}/install` · `/update` · `/uninstall` | **SSE** 安装 / 更新 / 卸载 |
| GET/POST | `/api/conversations` | 对话管理 |
| POST | `/api/conversations/{id}/messages` | **SSE** 发送消息并流式接收回复；请求体带 `summon_profile_id` 时为 **@ 召唤**：被召唤的智能体只回答这一轮（共享对话历史，不共享主场会话 / 技能 / MCP） |
| POST | `/api/conversations/{id}/regenerate` · `/cancel` | 重跑最后一轮 / 中断运行 |
| GET | `/api/system/overview` · `/api/runs` | 运行状态与实时任务 |
| GET | `/api/usage/overview` | 用量汇总：token 消耗与技能分配 |
| POST/GET/DELETE | `/api/auth/me/pat` | 个人访问令牌（PAT）：生成（明文只回一次）/ 查状态 / 吊销 |
| GET/POST | `/api/im/connections` | 列出 / 新建 IM 连接（保存前先向平台核对凭据） |
| GET/POST | `/api/im/peers` | 等待允许的配对请求与已配对账号 |

交互式文档：启动后访问 `http://127.0.0.1:8010/docs`。

</details>

<details>
<summary><b>目录结构</b></summary>

```
backend/
  app/agents/            适配器层（base · events · registry · builtin/*）
  app/runtime/           service（编排/持久化）· runner（执行）· manager（运行表）·
                         provisioning（运行时的安装 / 更新 / 卸载）
  app/im/                IM 域：base（平台元数据/契约）· platforms/*（各平台收发）·
                         service（连接登记表/配对/发送队列）· bridge（与对话域的唯一交界）
  app/api/               路由 · SSE
  app/models.py db.py    数据模型与引擎
  tests/                 适配器解析、API 契约、harness 工具循环、架构守卫
frontend/
  src/pages/             Chat · Agents · Runtimes · Skills · MCP · Usage · Settings
  src/components/        DynamicForm（由 schema 生成表单）· MessageView · Modal
  src/mobile/            移动端（PWA）
docs/
  architecture.md             一次运行的完整时序、事件协议、扩展点
  im.md                       IM：支持哪些平台、配对与消息路由、分层
  ai-coding-standard.md       编码规范详版
  context-usage.md            上下文用量按钮与自动压缩
  collaboration-design.md     多智能体协作设计详版
  collab-mechanism.md        协作机制与功能手册
  collab-guide.html           协作机制图文版（浏览器直接打开）
```

</details>

<details>
<summary><b>参与开发与发版</b></summary>

改代码前先读 [`AGENTS.md`](./AGENTS.md)（分层铁律与扩展手册），需要理由与边界时读 [`docs/ai-coding-standard.md`](./docs/ai-coding-standard.md)。分层约束由 `backend/tests/test_architecture.py` 自动校验。

```bash
./scripts/test.sh     # pytest（含架构守卫）+ 前端 typecheck
```

**自己发版**：改 `backend/app/__init__.py` 的 `__version__` → 提交 → `bash scripts/release.sh`（构建前端、打 `coda.tar.gz` + `.sha256`、`gh release create`）。tag 必须与 `__version__` 一致（`v0.2` ↔ `V0.2`），脚本会校验。
</details>

---

## 🤝 更多文档

| 想了解 | 去哪看 |
| --- | --- |
| 平台运行的完整时序与扩展点 | [`docs/architecture.md`](./docs/architecture.md) |
| 多智能体协作怎么跑、能做什么 | [`docs/collab-mechanism.md`](./docs/collab-mechanism.md) · [`docs/collab-guide.html`](./docs/collab-guide.html)（图文版） |
| IM 平台、配对与消息路由 | [`docs/im.md`](./docs/im.md) |
| 系统管理 MCP 与个人访问令牌 | [`docs/admin-mcp.md`](./docs/admin-mcp.md) |
| 编码规范与工程铁律 | [`AGENTS.md`](./AGENTS.md) · [`docs/ai-coding-standard.md`](./docs/ai-coding-standard.md) |

---

## 📄 开源协议

本项目采用 [MIT License](./LICENSE) 开源 —— 可以自由使用、修改、分发与商用，保留版权声明即可。

> 第三方组件（Electron、Python、Node.js、各 AI 内核的 CLI 等）遵循各自的许可，见其发行包内的 LICENSE。
> Windows 安装向导里的「许可协议」页展示的就是根目录这份 `LICENSE`。

---

## ⭐ Star 曲线

<p align="center">
  <a href="https://star-history.com/#Sustainedg/Coda&Date">
    <img src="https://api.star-history.com/svg?repos=Sustainedg/Coda&type=Date" alt="Star History Chart" width="720" />
  </a>
</p>

> 这条曲线每往上拐一次，就有一个人把智能体们安顿进了同一个家 🏠
>
> 如果 Coda 也帮到了你，欢迎[点个 Star](https://github.com/Sustainedg/Coda/stargazers) —— 让这条线有理由继续上扬 ✨
