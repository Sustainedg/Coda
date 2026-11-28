# 智能体为核心的记忆方案

> **状态：研究完成，待决策 / 待实施（任务待办）**
> 一句话结论：**能做到，而且不用改内核一行代码。**
> 关键动作：把「记忆目录」的锚点从**对话 id** 换成**智能体 id**，并把记忆从内核配置目录里「拔出来」，变成平台自己的一等公民。
> 前置方案：`docs/计划任务/claude-native-memory-plan.md`（那份解决「跨对话」，这份解决「跨内核 + 以智能体为核心」）。
> 本文所有结论均基于 2026-11-24 对本机 Claude Code **2.1.327** 的实测（假端点抓请求体）+ 源码取证。

---

## 〇、小白讲解（不需要懂代码也能看）

### 1. 记忆到底存在哪？

把「内核」想成一辆车：Claude Code 是一辆车，Codex 是另一辆车，豆包 CLI 又是另一辆。
「智能体」是司机——同一个司机今天开这辆、明天开那辆。

现在 Claude Code 这辆车自带一个**后备箱**（原生记忆功能），司机的记事本放在后备箱里。

问题出在 Coda 现在的设计：**每开一段路就换一辆新车，后备箱也跟着换新的**。
于是司机在这段路上记的东西，下一段路就找不到了。

> 准确说：Coda 给每条**对话**分配一套独立的配置目录（`agent-homes/<对话 id>/claude-home`），
> 记忆默认就落在这套目录里。所以现状不是「内核级共享」，而是**对话级隔离**。

### 2. 那为什么不能直接把后备箱做大？

因为**不同车的后备箱格式不一样**：

- Claude Code 的记忆是 `MEMORY.md` 索引 + 一条事实一个 `.md` 文件；
- CodeBuddy 有自己的一套（`$HOME/agent-memory/<id>/MEMORY.md`）；
- Codex、Gemini、豆包、DeepSeek harness 压根**没有自动记忆**这个后备箱，只有 `AGENTS.md` / `GEMINI.md` 这类「说明书」。

所以「同一个智能体换内核还保留记忆」这件事，**光靠内核自带的后备箱做不到**。

### 3. 正确做法：给司机一个「云盘」

平台在数据目录里给每个智能体开一个云盘：

```text
<数据目录>/agent-memory/<用户 id>/<智能体 id>/
├── MEMORY.md        # 目录（一行一条）
└── xxx.md           # 每条事实一个文件
```

- 开 Claude Code 这辆车时，把云盘**挂到它的后备箱**（一个环境变量指过去）→ 它原生读写，体验不变；
- 开 Codex 这种没有后备箱的车时，平台**把云盘里的目录念给司机听**（注入到提示词里）；
- 换车？云盘还在，因为它挂在「司机」身上，不是挂在「车」身上。

这就是「**智能体为核心的记忆**」：记忆跟着智能体走，不跟着内核走，也不跟着对话走。

### 4. 智能体上那个「记忆设置」是什么？

就像手机里的「照片存本地 / 存云盘」，给智能体加一个三选一：

| 选项 | 大白话 | 适合谁 |
|---|---|---|
| 关闭 | 不记事 | 临时工、一次性任务 |
| 跟随内核（现状） | 记事本放车里，下车就没了 | 不介意失忆的短对话 |
| 智能体记忆 | 记事本放云盘，换车也在 | 长期用的「员工」 |

⚠️ 注意：**只有「智能体记忆」能跨内核**；「内核原生」最多做到跨对话，换内核一定丢（因为新内核不认识旧格式）。

---

## 一、先把三个作用域拆开

项目里其实有三个不同的「记忆归属」，现在被一句「内核级共享」糊在一起了：

| 作用域 | 代码里的锚点 | 现状 |
|---|---|---|
| 对话 | `Conversation.id` | 隔离家目录 `agent-homes/<conversation.id>/<home_name>`（`runtime/isolation.py:20`） |
| 内核 | `AgentProfile.type_id` | 只是「用哪个适配器」，本身不存记忆 |
| 智能体 | `AgentProfile.id` | 稳定身份。**换内核 = 改 `type_id`，`id` 不变**（`api/agents.py:347`） |

关键事实：

- 对话可以中途换智能体（`api/conversations.py:357-371`），换完会清 `session_id`；
- 智能体可以换内核（`api/agents.py:381-384`），换完会丢弃不匹配的技能 / MCP，但 `id` 不变；
- 隔离家目录是**按对话**切的（`skills.py:770`、`mcp.py:1044`），所以原生记忆天然是「对话级」。

> 结论：要做智能体级记忆，不是把作用域「放大」，而是**换锚点**：`conversation.id` → `agent_profile.id`。

---

## 二、实测结论（前置方案的阻塞项已清除）

### 实验方法

用一个假的 Anthropic `/v1/messages` 端点把请求体全量落盘，按 Coda 的真实调用方式跑：

- `CLAUDE_CONFIG_DIR` 指向隔离目录；
- `claude -p --output-format stream-json` 无头模式；
- 记忆目录放在工作目录之外；
- 分别测试 `autoMemoryDirectory`、`CLAUDE_COWORK_MEMORY_PATH_OVERRIDE`、`autoMemoryEnabled` 与 `workspace_only`。

### 结果 1：无头 `-p` 模式真的会召回记忆 ✅

请求体第一条 user message 里确实有：

```text
<system-reminder>
Contents of /tmp/.../memory/MEMORY.md (user's auto-memory, persists across conversations):

# Memory Index
- [Secret code word](secret-code-word.md) — 用户设定的暗号是「西瓜」
</system-reminder>
```

**只把索引进上下文，正文按需读**，和前置方案里描述的机制一致。
→ 前置方案 §5.3「召回时机未验证」**可以划掉**。

### 结果 2：`--resume` 续接会重新读，且「变了才提醒」 ✅

同一会话第二轮请求里出现：

```text
Instruction files were re-read when this session started;
these differ from their earlier copies — ...
Contents of .../MEMORY.md ...
```

→ 别的对话写了新记忆，下一轮就能看到。**一条长对话不会永远失忆。**

### 结果 3：`workspace_only` 下记忆目录照样能读写 ✅

把 `blockReadsOutsideWorkingDirectories`、`blockFileToolReadsOutsideWorkingDirectories`、`blockWritesOutsideWorkingDirectories` 全打开，记忆目录放在工作目录之外：

- 读：`MEMORY.md` 正常注入；
- 写：让模型调 `Write` 往记忆目录写 `probe.md`，**成功落盘**。

源码里的豁免文案也找到了：`auto memory files are allowed for writing`。
⚠️ **只对 `.md` 生效**——写别的扩展名会被拦，所以记忆条目必须保持 Markdown。

→ 前置方案 §5.1「写入权限风险」**可以划掉**。

### 结果 4：其它关键事实

| 结论 | 证据 |
|---|---|
| `autoMemoryDirectory` 写进隔离 home 的 `settings.json` 生效 | 它落在 userSettings 位置 |
| `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` 优先级最高，且不依赖 settings | 空 settings + env 也能注入 |
| 设了 override 就是**扁平目录**，不再拼 `projects/<key>/memory/` | 注入路径直接是目录下的 `MEMORY.md` |
| `autoMemoryEnabled: false` 是真正的总开关 | 读、写、注入全部为零 |
| 记忆目录不存在时内核会自己建 | 实测可直接写 |
| `_patch_json` 是顶层键合并 | 只覆盖同名键，不会冲掉用户 carry 进来的其它配置（`runtime/skills.py:881`） |

### 仍未验证（实施前要补）

1. **并发写 `MEMORY.md`**：同一智能体两条对话同时跑，各自「读-改-写」索引，可能丢一行。
2. **CodeBuddy 的真实记忆路径**：源码显示它在 `$HOME/agent-memory/<id>/MEMORY.md`，而 Coda **不改 HOME**（`runtime/runner.py:250` 直接继承 `os.environ`）。如果它的自动记忆默认开启，可能跨对话甚至跨用户共享——**接入前必须单独实测**，必要时用 `CODEBUDDY_CODE_DISABLE_AUTO_MEMORY=1` 关掉。

---

## 三、方案对比

| | 方案 A：原生目录按智能体切 | 方案 B：平台托管智能体记忆 | 方案 C：混合（推荐） |
|---|---|---|---|
| 做法 | `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE=<data>/agent-memory/<agent_id>/` | 平台存 markdown，框架注入 prompt；写回走 MCP / 后台抽取 | 平台目录是唯一真身；Claude 用原生读写，其它内核注入 |
| 跨对话 | ✅ | ✅ | ✅ |
| 换内核 | ❌（新内核不认这个格式） | ✅ | ✅ |
| 实现量 | 最小（一个环境变量 + 一个配置项） | 中（注入 + 写回 + 锁） | 中（A 的适配器部分 + B 的注入部分） |
| 自动记忆抽取 | ✅ 内核自带 | 要自己实现 | Claude 原生；其它另说 |
| 并发写索引 | ⚠️ 两条对话同写一个 `MEMORY.md` | 平台可加锁 / 重建 | ⚠️ / ✅ 混合 |
| 适合 | 只求「同 agent 跨对话」 | 只求「跨内核」 | 你的真实诉求 |

**结论：选 C。** 平台目录做真身，Claude 原生读写，其它内核只读注入。这样既不丢原生体验，又能跨内核。

### 目标形态

```text
<数据目录>/agent-memory/<owner_id>/<agent_id>/
├── MEMORY.md          # 索引（一行一条，Claude 兼容格式）
├── user-role.md       # 一条事实一个文件
└── feedback-xxx.md
```

- **Claude Code**：用 `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` 指过去 → 原生读 + 原生抽取 + 原生 auto-dream，**零额外代码**；
- **其它内核**：框架在 `prepare_run` 里把索引（必要时加 top-N 条目正文）拼进 prompt；
- **写回**：Claude 直接写进这个目录；其它内核先做「只读注入」，写回二期再上；
- **换内核**：目录键是 `agent_id`，换 `type_id` 不动它，记忆天然跟着智能体走。

⚠️ 一个必须提前想清楚的点：`workspace_only` 下，非原生内核**读得到注入的索引，但读不到单条正文**（单条文件在工作目录之外）。所以给这类内核不能只注入索引，要么注入 top-N 正文，要么用 MCP 记忆工具（MCP 进程自己的权限不受工作目录限制）。

---

## 四、智能体上的「记忆设置」怎么放

### 不要只做成 ConfigField

`ConfigField` 是**适配器声明的、存在 `profile.options` 里的 per-kernel 选项**：

- 换内核时 `options` 会被新适配器的 `coerce_options()` 重新规整；未知键虽然会保留（`agents/base.py:496-512` 的 `setdefault`），但**前端只渲染当前适配器声明过的字段**——换到 Codex 后，这个开关就从界面上「消失」了；
- 而且「用哪种记忆」是跨内核的平台语义，塞进「适配器设置」概念上就错位。

### 推荐：平台级智能体字段

和 `system_prompt`、`gateway_id`、`group_name`、技能 / MCP 分配同级：

```python
# models.py AgentProfile 新增
memory_mode: str = "conversation"   # off | conversation | agent
```

> 关于 AGENTS.md 铁律 4（可配置项必须是 `ConfigField`）：那条约束的对象是「某个内核自己的适配器选项」。
> `system_prompt`、`gateway`、`model`、`group_name` 以及技能 / MCP 分配都是平台级字段，不是 `ConfigField`，这是现成的先例。记忆模式同理。

### UI（智能体编辑弹窗，新增「记忆」区块）

```text
记忆
  ○ 关闭
  ○ 跟随内核 · 每条对话各自记忆（现状）
  ○ 智能体记忆 · 跨对话、跨内核共享，换内核不丢
```

---

## 五、代码落点（按项目铁律）

### 1. 契约层（`agents/`）

新增一个和 `SkillMount` / `McpMount` 同构的契约，别让框架知道 Claude 的变量名：

```python
# agents/base.py
@dataclass(slots=True)
class AgentContext:
    ...
    #: 平台算好的「智能体记忆目录」；None = 不启用
    memory_dir: str | None = None

@dataclass(slots=True)
class Capabilities:
    ...
    #: 声明「我能原生消费 memory_dir」（Claude Code = True）
    native_memory: bool = False
```

```python
# agents/memory.py（新文件，照 skills.py / mcp.py 的形状）
@dataclass(slots=True)
class MemoryMount:
    env: dict[str, str] = field(default_factory=dict)
    settings_patch: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
```

适配器只加一个可选钩子：

```python
# claude_code.py
def memory_mount(self, ctx: AgentContext) -> MemoryMount | None:
    if not ctx.memory_dir:
        return None
    return MemoryMount(
        env={"CLAUDE_COWORK_MEMORY_PATH_OVERRIDE": ctx.memory_dir},
    )
```

框架层永远不出现 `CLAUDE_CONFIG_DIR` / `autoMemoryDirectory` 这些字符串，`test_architecture.py` 的守卫测试继续守得住。

### 2. 框架层（`runtime/memory.py`，新文件）

- `agent_memory_dir(profile)` → `<data>/agent-memory/<owner>/<agent>/`；
- `ensure_agent_memory(...)`：建目录、`0700`；
- `read_index(...)` / `render_block(...)`：读 `MEMORY.md`，做长度上限（Claude 自己也是 200 行 / 25KB 截断）；
- `prepare_memory(session, definition, profile, conversation, ctx)`：
  - `off` → 不做任何事（必要时给适配器 `autoMemoryEnabled: false`）；
  - `conversation` → 完全不动，保持现状；
  - `agent` + `definition.capabilities.native_memory` → 设置 `ctx.memory_dir`，按 `memory_mount()` 物化，**不注入 prompt**（避免双份）；
  - `agent` + 非原生内核 → 把索引 / 条目注入 `ctx.prompt` 或 `ctx.system_prompt`；
- `rebuild_index(...)`：从 `*.md` 重建 `MEMORY.md`，兜底并发写丢条目；
- 清理 / 导入 / 导出接口。

### 3. 编排层（`runtime/service.py`）

在 `prepare_run()` 里，**放在 `prepare_skill_home` / `prepare_mcp_home` 之后**（那时 `ctx.home_dir` 才定下来），`_inject_task_card_notice` 之前：

```python
memory_warnings = await prepare_memory(session, definition, profile, conversation, ctx)
...
if persist_session:
    await _inject_task_card_notice(...)
```

注入 prompt 的顺序要排在 `transcript_prompt` / `prior_context_prompt` 之后，否则会被覆盖。

### 4. 适配器首期只改 Claude

```python
# claude_code.py build_invocation()
if ctx.memory_dir:
    env["CLAUDE_COWORK_MEMORY_PATH_OVERRIDE"] = ctx.memory_dir
```

用 env 而不是 `autoMemoryDirectory` 的原因：优先级最高、不依赖 settings 合并、不会覆盖用户自己 carry 进来的同名配置。

### 5. 数据库 / API / 前端

- `models.py`：`AgentProfile` 加 `memory_mode`，登记到 `ADDITIVE_COLUMNS`（`"memory_mode": "VARCHAR(16) NOT NULL DEFAULT 'conversation'"`），老库自动补列，符合铁律 9；
- `schemas.py`：`AgentProfileUpdate` / `AgentRead` 加字段，API 校验枚举；
- `api/agents.py`：克隆默认**不复制**记忆（或提供勾选），删除时默认保留（软删，对话还引用着）；
- 前端 `AgentsPage.tsx`：加平台级「记忆」区块（不是在 `DynamicForm` 里）。

---

## 六、风险清单

| 风险 | 说明 | 对策 |
|---|---|---|
| **并发写 `MEMORY.md`** | 同一智能体两条对话同时跑，各自读-改-写索引，可能丢一行 | 平台在运行前 / 后 `rebuild_index()`；或写回走平台加锁；或每对话一个子目录 + 平台合并 |
| **跨内核格式** | Claude 的 frontmatter 和 CodeBuddy 的不一样 | 以 Claude 格式为中性格式；平台只读写 `MEMORY.md` + 单事实 `.md` |
| **CodeBuddy 的 HOME 级记忆** | 源码显示在 `$HOME/agent-memory/<id>`，而 Coda 不改 HOME，可能跨对话 / 跨用户共享 | 接入前实测；按对话 / 按 agent 隔离，或直接 `CODEBUDDY_CODE_DISABLE_AUTO_MEMORY=1` |
| **多用户隔离** | 记忆必然跨对话，不能跨用户 | 目录按 `owner_id/agent_id`；权限 `0700`；接口校验 `require_owned` |
| **协作域 C5「不共享记忆」** | `collab/execution.py:311` 明确 `session_id=None`、`history=[]` | 协作步骤默认**不读不写**智能体记忆；要读做成节点级开关 |
| **@ 召唤** | 召唤轮用一次性 home | 读：可以（是它自己的记忆）；写：默认禁止，避免污染 |
| **自动起标题** | `titles.py` 自己构造 `AgentContext` | 不注入记忆，避免标题被记忆带偏，也省 token |
| **token 预算** | 索引每轮都进上下文 | 索引上限（200 行 / 25KB），正文按需；非原生内核只注入 top-N |
| **记忆里出现密钥** | 用户可能让智能体记住 token | 复用 `credentials.scrub`；导入 / 写入前过滤；不要把记忆内容写进事件与日志 |
| **删除 / 换内核** | 删智能体是软删；换内核不重置 `conversation.session_id`（但新 home 名字不同，`note_fresh_home` 会兜底清掉） | 删除保留记忆目录；提供「清空记忆」「导出记忆」；换内核时提示「原生记忆不会自动迁移」 |

---

## 七、待办清单

### P1（最小可用）

- [ ] `AgentProfile` 新增 `memory_mode` 字段 + `ADDITIVE_COLUMNS` 登记
- [ ] 新增 `runtime/memory.py`：目录计算、`ensure`、读索引、注入
- [ ] `prepare_run` 接入（放在技能 / MCP 物化之后）
- [ ] 智能体编辑页新增「记忆」区块（关闭 / 跟随内核 / 智能体记忆）
- [ ] 单测：目录按 `owner/agent` 切分、`off` 不注入、路径不越界
- [ ] 验收：A 对话让智能体记住一件事 → B 对话（同一智能体）问它，答得出来
- [ ] 验收：`workspace_only` 下同样成立
- [ ] `bash scripts/test.sh` 通过

### P2（Claude 原生读写映射）

- [ ] `Capabilities.native_memory` + `MemoryMount` 契约
- [ ] Claude 适配器实现 `memory_mount()`，用 `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE`
- [ ] `agent` 模式下原生内核与 prompt 注入**互斥**
- [ ] 验收：Claude 对话写入直接落在平台目录；换到 Codex 后仍能通过注入召回
- [ ] 补测试：物化后 `<home>/settings.json` 不被 `_patch_json` 冲掉（如用 settings 方案）

### P3（其它内核写回，二选一）

- [ ] 方案 1：内置 MCP「记忆」服务器（`builtin_mcp.py` 加一条，`scripts/mcp/` 加脚本，env 按智能体注入 `CODA_MEMORY_DIR`）——**推荐**
- [ ] 方案 2：后台抽取任务（照 `runtime/titles.py` 的 `schedule()` 模式，一轮结束后跑一次抽取）
- [ ] 验收：Codex / Gemini 对话里说「记住」，下一轮能召回

### P4（治理）

- [ ] 从旧对话 `agent-homes/*/.../memory/` 一键导入
- [ ] 清空 / 导出记忆
- [ ] 并发写保护 + 定期 `rebuild_index`
- [ ] 审计：记录读写来源（哪条对话、哪个内核）
- [ ] 文档：`docs/architecture.md` 补记忆作用域与数据流

---

## 八、几个需要拍板的决策

1. **默认值**：`memory_mode` 默认用 `conversation`（保持现状）还是 `agent`（新智能体默认共享）？
   → 建议 `conversation`，老用户无感；新建智能体时 UI 默认选「智能体记忆」。
2. **是否覆盖用户的 `autoMemoryEnabled`**：如果用户自己在真实 `settings.json` 里关了自动记忆，平台还要强制打开吗？
   → 建议尊重用户设置，给出告警；或者智能体页显式勾选时再覆盖。
3. **记忆是否按工作目录再分层**：同一个智能体服务多个项目时，要不要 `agent_id/project_key/`？
   → 建议首期不做（用户要的就是 agent 级）；需要时作为高级选项。
4. **协作域要不要读智能体记忆**：C5 说的是「智能体之间不共享」，智能体自己的记忆不违反字面，但会破坏可复现性。
   → 建议默认不读不写。
5. **克隆智能体是否复制记忆**：→ 建议不复制，提供「复制记忆」显式操作。

---

## 九、一句话回答三个问题

1. **能做到智能体为核心的记忆吗？** 能。把记忆目录的键从「对话 id」换成「智能体 id」，并把它从内核配置目录里独立出来。
2. **智能体上能加记忆设置吗？** 能，但要做成**平台级字段 + 独立 UI 区块**，不要塞进 `ConfigField`——否则换内核后这个开关会从界面上消失。
3. **能选「内核原生记忆」还是「智能体记忆」吗？** 能。但要说清楚：**只有「智能体记忆」能跨内核**；「内核原生记忆」跨对话可以，跨内核不行，因为 Codex / Gemini / 豆包根本没有这套子系统。

---

## 关联

- `docs/计划任务/claude-native-memory-plan.md` —— 前置研究：原生记忆怎么跨对话共享（本文的实测把它的两个阻塞项清掉了）
- `AGENTS.md` —— 本方案涉及后端 / 前端改动，动手前先读 AGENTS.md 全文
- `docs/README.md` —— 项目文档索引
