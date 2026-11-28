# Claude Code 原生记忆跨会话共享方案

> **状态：未实现 · 待实施（任务计划）**
> 结论：能做到。原生记忆系统本身是活的，只是被 Coda 的「一对话一隔离目录」设计切成了「对话内记忆」。
> 本文所有结论均来自 2026-11-24 对本机（Claude Code 2.1.327）的实测与源码取证，未做无据推测。

## 任务状态

| 项 | 值 |
| --- | --- |
| 状态 | 未实现，等待决策与实施 |
| 前置阻塞 | 第五节第 3 条「记忆召回时机」尚未验证，先做对照实验再动手 |
| 实施位置 | `backend/app/agents/builtin/claude_code.py` 的 `skill_mount()` |
| 验收标准 | 新开会话能召回上一对话写入的记忆；`bash scripts/test.sh` 通过 |

---

## 一、一句话结论

Claude Code 2.1.327 自带文件型持久记忆（auto-memory），在本机的 Coda 环境里**确实在运行、确实写出了记忆文件**；
但它的落脚点跟着 `CLAUDE_CONFIG_DIR` 走，而 Coda 把这个变量指向「每对话一个」的隔离目录，于是记忆各存各的，跨对话读不到。
内核留了 `autoMemoryDirectory` / `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` 两个官方覆盖口，指到共享目录即可打通。

---

## 二、现状取证

### 2.1 原生记忆在本机真实跑过

全盘只找到一个真正落了记忆的 home：

```
/app/data/agent-homes/09156a8573f34ef9a781eda94a870278/claude-home/
  projects/-vol1-1000-----python----Bot/memory/
    ├── MEMORY.md                 # 索引
    └── secret-code-word.md       # 一条 user 类型记忆
```

`MEMORY.md` 是索引，每条一行：

```markdown
# Memory Index

- [Secret code word](secret-code-word.md) — 用户设定的暗号是「香蕉」
```

单条记忆是一个文件一个事实，带 frontmatter：

```markdown
---
name: secret-code-word
description: 用户设置的暗号是「香蕉」
metadata:
  node_type: memory
  type: user
  originSessionId: dba80f37-7949-4a44-b14c-9ce5e94adaea
  modified: 2026-11-21T14:02:21.288Z
---

用户设定了暗号：**香蕉**。用于确认身份或作为约定标记。
```

回溯来源：写入者是会话 `dba80f37-7949-4a44-b14c-9ce5e94adaea`，`entrypoint: sdk-cli`（无头模式），
`cwd: /vol1/1000/同步库/python/项目/Bot/backend`，14:02:21 调了两次 `Write`（先写笔记、后写索引）。

**两个要点**

1. **无头 `-p` 模式照样触发记忆抽取**，不用担心「headless 不跑记忆」。
2. **记忆按 git 根归档，不按 cwd 归档**。那次会话 cwd 在 `Bot/backend`，记忆却落在 `Bot/memory/`——
   内核会先按 git 根 / 工作副本根归一化（源码 `defaultKey()`：`working_copy` → `git` → `project_root`）。

### 2.2 记忆类型与写入机制

- 类型只有四种：`user` / `feedback` / `project` / `reference`。`feedback` 与 `project` 还要求补 **Why:** 和 **How to apply:** 两行
- 写入靠一个**记忆抽取子代理**（内部标识 `memextract`），每轮对话后运行，提示词明确约束「只看最近若干条消息，不许 grep 源码去验证」
- 无内容可存时输出 `Nothing to save.`，不解释
- 读取时 `MEMORY.md` 索引**整份进上下文**，正文按 `description` 判断相关性再按需读；索引扫描上限 200 条
- 后台整合任务叫 **auto-dream**（设置项 `autoDreamEnabled`），跨会话去重合并
- 用户明说「记住这个」→ 立即存；说「忘掉」→ 找到条目删掉

### 2.3 为什么跨对话读不到

链路是这样的：

```
backend/app/runtime/isolation.py:20   home_for(scope_id, home_name)
    → agent-homes/<对话 id>/claude-home      ← 一对话一个家

backend/app/runtime/skills.py:770        home = home_for(scope_id or conversation.id, ...)
backend/app/agents/builtin/claude_code.py:433
    carry=[(str(root/name), name) for name in _SYSTEM_SETTINGS_FILES]   ← 把真配置搬进这个家

backend/app/agents/builtin/claude_code.py:524
    env["CLAUDE_CONFIG_DIR"] = ctx.home_dir   ← 内核的「家」被换到这个隔离目录
```

而内核的默认记忆路径是 `~/.claude/projects/<sanitized-cwd>/memory/`。
`~/.claude` 跟着 `CLAUDE_CONFIG_DIR` 一换，记忆就跟着进了 `agent-homes/<对话 id>/.../memory/`。

**结果**：A 对话的记忆躺在 A 的家，B 对话进的是 B 的家，翻遍抽屉也找不到。
auto-dream 的整合范围同样按 home 隔离，连合并都不会跨对话发生。

### 2.4 各内核的记忆机制并不通用

同一个 `settings.json` 只对 Claude Code 生效，各内核各回各家：

| 内核 | 配置目录 | 隔离变量 |
| --- | --- | --- |
| Claude Code | `~/.claude` | `CLAUDE_CONFIG_DIR` |
| CodeBuddy | `~/.codebuddy` | `CODEBUDDY_CONFIG_DIR` |
| Codex | `~/.codex` | `CODEX_HOME` |
| Gemini CLI | `~/.gemini` | `GEMINI_CLI_HOME` |
| Hermes | — | `HERMES_HOME` |
| 豆包 CLI | — | 直接换 `HOME` |
| DeepSeek harness | — | `DSH_HOME` / `DSH_AGENTS_HOME` |

**只有 Claude Code 认 `autoMemoryDirectory`**。写进 `~/.claude/settings.json` 的东西，Codex / Gemini 根本看不见。
所以「改全局配置会影响所有内核」这个说法是错的——影响面是**所有 Claude Code 智能体的所有对话**。

---

## 三、三个覆盖开关的真实语义

从内核源码取到的原文与优先级：

| 开关 | 位置 | 生效优先级 | 语义 |
| --- | --- | --- | --- |
| `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` | 环境变量 | **最高** | 直接指定记忆目录，不做路径展开 |
| `autoMemoryDirectory` | settings.json | 次之 | 指定记忆目录，支持 `~/` 展开 |
| `CLAUDE_CODE_REMOTE_MEMORY_DIR` | 环境变量 | — | 换的是**基准目录**，仍会拼 `projects/<key>/memory/` 子层 |
| （默认） | — | 兜底 | `<CONFIG_DIR>/projects/<sanitized-cwd>/memory/` |

源码解析顺序（`resolveEntry()`）：

```
Sd()          = CLAUDE_COWORK_MEMORY_PATH_OVERRIDE   → 命中即返回
SE().dir      = autoMemoryDirectory（settings）      → 命中即返回
否则          → <CONFIG_DIR>/projects/<key>/memory/
```

### ⚠️ 关键差异：设了 `autoMemoryDirectory` 就是**扁平目录**

设了 `autoMemoryDirectory`（或 env override）之后，内核**不再拼 `projects/<key>/memory/` 这层子目录**，
直接把该目录当记忆根。含义是：

- ✅ 所有对话共享同一个 `MEMORY.md` 索引 → 跨对话、跨项目都能读到
- ⚠️ **跨项目也会共享**，不同项目的记忆混在一个索引里
- ⚠️ 索引是单文件，多个会话并发写索引存在互相覆盖风险

如果不想要跨项目混合，就用 `CLAUDE_CODE_REMOTE_MEMORY_DIR`（保留 `projects/<key>/memory/` 分层），
但这样只能跨**同一项目**的对话共享。

### 生效来源限制

`autoMemoryDirectory` 只在 **userSettings / policySettings / flagSettings** 生效；
写进项目里 check-in 的 `.claude/settings.json`（projectSettings）会被**出于安全忽略**。

Coda 的 carry 机制把真实的 `~/.claude/settings.json` 搬进隔离 home 后，它落在 `<home>/settings.json`，
对内核而言正是 **userSettings** 位置 → **方案 A 可行**。

---

## 四、两个方案

### 方案 A：零代码，改用户级配置

新建 `/app/data/home/.claude/settings.json`：

```json
{
  "autoMemoryDirectory": "/vol1/1000/同步库/coco-memory"
}
```

- **生效范围**：本机所有 Claude Code 智能体的所有对话（即刻全量生效）
- **优点**：不动代码、不用重启平台、随时可回滚（删掉这个键即恢复）
- **缺点**：粒度粗，无法只给某一个智能体开；无法按项目隔离
- **注意**：目录必须真实存在且可写；`~` 会展开到 `/app/data/home`，指向全机共享路径

### 方案 B：改一行代码，做成平台配置项

在 `backend/app/agents/builtin/claude_code.py` 的 `skill_mount()` 里给 `settings_patch` 补一个键：

```python
settings_patch: dict[str, Any] = {
    "enabledPlugins": {...},
    "disableBundledSkills": True,
    "skillOverrides": {...},
    # 新增：把自动记忆指向平台共享目录，实现跨对话共享
    "autoMemoryDirectory": str(shared_memory_dir),
}
```

- **生效范围**：可精确到「这个智能体开不开」，且只影响 Claude Code
- **合规性**：按 AGENTS.md 铁律 4「所有可配置项必须声明为 `ConfigField`」，
  正式实现应做成**智能体页面上的配置项**（在 `claude_code.py` 的 `ConfigField` 列表里声明），
  而不是硬编码路径
- **合并安全**：`_patch_json()`（`backend/app/runtime/skills.py:881`）是**逐键合并**语义，
  只覆盖同名键，不会冲掉 carry 进来的用户配置 → 用户的 `autoMemoryDirectory` 与平台配置可共存

### 不改代码的第三条路

继续用已经挂着的 **Basic-Memory MCP**。它天生跨会话（独立知识库 `~/.basic-memory/memory.db`，
笔记实体落在 `/vol1/1000/同步库/记忆库`），代价是不属于 Claude Code 原生那套 frontmatter 与自动抽取机制。

---

## 五、风险与待验证项

以下几条**尚未实测**，实施前必须验证：

1. **写入权限**：`workspace_only` 模式会开 `blockWritesOutsideWorkingDirectories`，
   共享目录在工作目录之外时可能被拦。内核源码里有 `auto memory files are allowed for writing` 的豁免文案，
   但豁免是否覆盖「目录在工作目录之外」这一情形，**没有实测证据**。
   → 验证方法：设好目录后跑一次真实对话，看记忆文件是否落盘。
2. **并发写索引**：多对话同时触发抽取时，`MEMORY.md` 单文件索引可能被互相覆盖。
   → 若确有冲突，需改成按项目分层（用 `CLAUDE_CODE_REMOTE_MEMORY_DIR`）或加锁。
3. **记忆召回时机**：本机所有会话记录中**未出现任何 `# Memory` 注入段**（全盘 grep `MEMORY.md` / `# Memory` / `Memory Index` 均 0 命中）。
   唯一那次写入的会话，其记忆落在 `Bot/memory/`，而同 home 的另一个会话 `03418b18` 虽与写入时刻重叠
   （14:10 的请求在 14:02 写入之后），请求体里**同样没有记忆段**。
   → 这说明**注入可能只发生在会话启动时**（启动早于记忆写入 → 拿不到），
   也可能是无头模式根本不注入。**这一条直接决定方案能否兑现，必须先做一次「写入 → 新开会话 → 问它记了什么」的对照实验。**
4. **记忆目录位置**：共享目录建议放在 `backend/data/` 之外（用户真实数据目录 `backend/data/` 受 `.gitignore` 忽略），
   且要考虑备份策略。

---

## 六、实施步骤（建议顺序）

1. **先做对照实验**（不动任何配置）——
   用当前隔离 home 手工塞一条 `MEMORY.md` + 一条笔记，再新开一个对话问「你记得什么」，
   验证第 5 节第 3 条：记忆到底会不会被召回。**这一步的结果决定方案是否值得做。**
2. 若是「会话启动时注入」，则确认平台侧 `--resume` 续接的对话是否也能拿到（同一会话内续接天然共享 home）。
3. 选定方案 A 或 B。B 需按 AGENTS.md 铁律 4 声明 `ConfigField`，并在智能体页面暴露开关。
4. 补测试：断言物化后 `<home>/settings.json` 里 `autoMemoryDirectory` 仍在（不被 `_patch_json` 冲掉），
   且用户 carry 进来的同名键不被静默覆盖。
5. 按 AGENTS.md 铁律 8 验证：`bash scripts/test.sh`（后端 pytest + 前端 `tsc --noEmit` + `npm run build`）。

---

## 关联

- `AGENTS.md` —— 方案 B 涉及改代码，必须先读 AGENTS.md 全文
- 旧 MEMORY.md 归档笔记 —— 本库曾归档过一份工作区记忆索引，与本文讨论的原生记忆机制是两套东西
- `docs/README.md` —— 项目文档索引
