# 智能体核心记忆方案（用户偏好 + 核心记忆）

> 状态：**方案，尚未开工**（2026-11-28 已按代码评审意见修订）。
> 事实基线：Coda `main` 分支当前工作区。**代码是唯一事实来源**；本文与代码冲突时以代码为准。
> 相关文档：`AGENTS.md`（铁律与分层）、`docs/功能文档/上下文窗口与自动压缩.md`（上下文口径）、
> `docs/计划任务/skills-versioning-plan.md`（本文档结构参照）。
> 同目录相关方案（互不冲突，见 §7）：`claude-native-memory-plan.md`（内核原生记忆）、
> `agent-centric-memory-plan.md`（记忆锚点以智能体为核心）、`basic-memory-layering-plan.md`（MCP 记忆库分层）——
> 那三份讨论「内核自己的记忆文件怎么跨对话」，本方案讨论「平台侧手动维护的记忆怎么注入」，是两条独立的线。

---

## 0. 一句话结论

给 `AgentProfile` 增加「核心记忆」与「用户偏好」两个纯文本字段，在设定页手动编辑；
运行时由**一个统一拼装函数**把它们拼进 system prompt 的最前方（人格之前），
**六个**现有拼装点全部改为调用它。**不新增事件类型、不新增依赖、不改任何适配器**；
网页对话、@ 召唤、协作节点、IM 四条链路经同一函数自动生效。
AI 自动总结记忆（openclaw 式）留作后续演进——**这两个字段本身就是接口**，无需现在埋钩子。

---

## 1. 需求与现状

### 1.1 三点需求

| # | 需求 | 现状 | 本方案动作 |
| --- | --- | --- | --- |
| N1 | 智能体自己的**身份**（人格） | ✅ 已有：`AgentProfile.system_prompt`（`models.py:252`），设定页「人格」编辑框 | 不动 |
| N2 | **用户偏好**（跨对话记住用户的习惯喜好） | ❌ 无 | 新增 `user_preferences` 字段 + 编辑框 |
| N3 | **核心记忆**（跨对话记住重要事实） | ❌ 无 | 新增 `core_memory` 字段 + 编辑框 |

三个字段都是**手动维护**：用户写什么，智能体就记什么。不做自动提取、
不让 AI 自己写入（那是后续演进，见 §5）。

### 1.2 现有「记忆」机制盘点（为什么不重复造轮子）

| 机制 | 位置 | 作用域 | 与本方案的关系 |
| --- | --- | --- | --- |
| 内核会话续接 `session_id` | `Conversation.session_id`（`models.py:483`） | **单条对话** | 无冲突：会话记的是这条对话内部的往来 |
| 上下文压缩摘要 `context_summary` | `Conversation.context_summary`（`models.py:494`） | **单条对话** | 无冲突：超限时折叠早期消息的摘要 |
| 跨对话的智能体级记忆 | **不存在** | — | **本方案补的就是这个缺口** |

关键区分：内核侧的「会话记忆」只到单条对话为止，换一条对话、换一个协作节点就没了。
本方案是**智能体级**的：跟着 `AgentProfile` 走，该智能体的所有对话、协作工单、IM
会话都带着同一份记忆。

### 1.3 现有 system prompt 拼装点盘点（全部要收敛）

`with_notice(profile.system_prompt, credentials)`（人格 + 凭据说明）目前有 **6 处**消费点：

| 调用点 | 位置 | 链路 | 角色说明 |
| --- | --- | --- | --- |
| ① `build_context` | `runtime/service.py:220` | 网页对话 / @ 召唤 / 重生成 / IM（`im/bridge.py` 无独立拼装点，全部经 `prepare_run`） | **唯一真正进 `AgentContext` 的拼装**，内核实际收到的人格文本由它决定 |
| ② `prepare_run` 内估算 | `runtime/service.py:619` | 正常发送 | 喂给 `_auto_compact_context` 的**压缩估算入口**（不直接进 `AgentContext`） |
| ③ `regenerate_last` 内估算 | `runtime/service.py:1305` | 重生成链路 | 同 ②，重跑一轮的压缩估算入口 |
| ④ 协作节点 | `collab/execution.py:309` | 协作域 | 人格由这里拼（`collab/prompt.py` 明确不拼人格） |
| ⑤ 上下文面板估算 | `api/conversations.py:298` | 面板展示 | 只传 `profile.system_prompt`（无凭据块，现状即如此） |
| ⑥ 上限校验估算 | `api/system.py:205-209`（`set_context_settings`） | 「上限不能低于当前已用量」校验 | 同 ⑤ 的口径，防止用户把上限设到低于真实已用 |

**为什么 ②③⑤⑥ 四个「估算点」也必须换**：估算与实际发送共用同一份口径是
`runtime/context.py:estimate_conversation` 文档里写死的约定。漏掉任何一个就会出现
「面板说没超、压缩却触发了」或「上限校验放行了低于真实已用的值」这类自相矛盾——
恰好是本方案要消灭的静默分叉。

**不在覆盖范围（有意不带记忆，写明防误改）**：`runtime/titles.py:333`（自动起标题的
one-shot，`system_prompt=None`）、`api/agents.py` 连通性测试（`system_prompt=None`）——
两者都是与「带记忆对话」无关的旁路任务，带记忆反而污染标题/探测。

只改其中一处的后果：**网页有记忆、协作没记忆**，或上下文面板 / 上限校验的 token 数是假的
（估算没算记忆块）。因此必须收敛成同一个函数，六处全部替换。

---

## 2. 设计

### 2.1 数据模型

`AgentProfile` 新增两列（都可空字符串 = 没写，注入时整个块省略）：

```python
#: 用户偏好：跨对话记住的用户习惯与喜好（手动维护）。
user_preferences: str = Field(default="", sa_column=Column(Text, nullable=False))
#: 核心记忆：跨对话记住的重要事实（手动维护；后续演进为 AI 自动总结，见方案 §5）。
core_memory: str = Field(default="", sa_column=Column(Text, nullable=False))
```

* 登记进 `models.py:43` 的 `ADDITIVE_COLUMNS`（铁律 9：老库加列必须登记、
  老行容忍缺省）：
  `"agent_profiles": { ..., "user_preferences": "TEXT NOT NULL DEFAULT ''", "core_memory": "TEXT NOT NULL DEFAULT ''" }`
* `schemas.py` 三处同步：`AgentProfileCreate`（`schemas.py:154` 附近）、
  `AgentProfileUpdate`（`:178` 附近）、`AgentProfileRead`（`:224` 附近）——
  按现有 `system_prompt` 字段的透传方式原样加两个即可。
  **注意**：`AgentProfileUpdate` 里不加 `max_length`——软上限走「前端计数 + 保存时后端警告」
  （见 §2.6），Pydantic 的 `max_length` 会直接 422 拦截，与 D4「只警告不拦截」矛盾。
  语义沿用现有约定：**Update 里 `None` = 不改，`""` = 清空**（`update_agent` 用
  `model_dump(exclude_unset=True)`，老前端不带这两个字段 PATCH 不会误清——见 §2.7）。
* **`api/agents.py` 必须改三处**（原稿「不需要动」是错的，2026-11-28 评审纠正）：
  1. `_to_read`（`api/agents.py:148-163`）是**手工逐字段**构造 `AgentProfileRead` 的，
     不是靠 `from_attributes` 自动转——schemas 加了字段它也不会自动回传。
     不补这两行的事故链条：读取接口不回传记忆 → 前端 `useState(agent.core_memory ?? '')`
     初始化成空串 → 编辑页是**整包 PATCH**（`AgentEditPage.tsx:604` 把所有字段一起提交）→
     **用户改个名字保存，记忆被静默清空**。这是本方案最大的一个隐性数据丢失坑。
  2. 克隆智能体 `duplicate_agent`（`api/agents.py:620` 起，`clone = AgentProfile(...)` 在
     `:640`）同样是手工挑字段：不加 `core_memory=profile.core_memory,
     user_preferences=profile.user_preferences` 两行，副本没记忆且无任何提示。
  3. （核对项）`create_agent`（`:328` 附近）经 `AgentProfileCreate` → 手工构造
     `AgentProfile`，需要带上 `core_memory` / `user_preferences` 的透传——创建表单
     也要能直接写记忆。

### 2.2 注入格式与顺序

最终拼出的 system prompt（每个块非空才出现，空字段整块省略）：

```
【核心记忆】
<core_memory>

【用户偏好】
<user_preferences>

<人格 system_prompt 原文>

<凭据说明块（现有 with_notice 产物）>
```

* 核心记忆放**最前面**（需求原文：「放进最前面的 system 注入层」）；
* 块标题用「【核心记忆】/【用户偏好】」，与压缩摘要的 `SUMMARY_HEADER`
  （`runtime/context.py:81` 的「【较早对话摘要…】」）同一风格，模型好分辨；
* 人格的 `system_prompt_mode`（replace / append，见 `claude_code.py:545`）不受影响：
  记忆块是 `ctx.system_prompt` 的一部分，跟随用户已选的模式。

### 2.3 统一拼装函数

新模块 `backend/app/persona.py`（与 `attachments.py` 同级的「只有一份实现」模式，
对应 AGENTS.md §4.4「附件能力只有一份」的同款约定）：

```python
def compose_system_prompt(
    profile: AgentProfile | None,
    credentials: dict[str, str] | None,
) -> str | None:
    """核心记忆 + 用户偏好 + 人格 + 凭据说明，按 §2.2 顺序拼装；全空返回 None。

    ``profile=None`` 时直接返回 None（估算点的对话可能已换绑智能体，现状就是
    ``profile.system_prompt if profile else None``，保持同款容忍）。
    """
```

* `credentials=None` 时跳过凭据块（⑤⑥ 估算点现状就不带凭据块，保持原精度）；
* 内部复用现有 `credentials.with_notice`（`credentials.py:100`），不重写凭据逻辑；
* ①②③④⑤⑥ 六处全部改为调它，替换后 grep `with_notice(` 应只剩 `persona.py` 与
  `credentials.py` 自身；
* `persona.py` 是**顶层叶子模块**（与 `attachments.py` / `credentials.py` 同层），
  协作域 import 它不违反分层守卫（`tests/test_collab_architecture.py` 只拦
  api / runtime / agents 前缀），但 AGENTS.md §2 的协作域白名单文字与分层图需要
  同步提一句（见 §3.4）。

### 2.4 为什么不用动适配器（铁律 1/2/3 自检）

记忆拼进 `AgentContext.system_prompt` 后，一切走现有能力声明机制：

* `native_system_prompt=True` 的内核（Claude Code / CodeBuddy）：记忆随
  `--append-system-prompt` 每轮重传；
* `native_system_prompt=False` 的内核（Codex / Antigravity / harness 类）：
  `runtime/service.py:677` 已有的 `transcript_prompt` 折叠路径自动把记忆折进
  正文，同样不用改；
* 协作域 `collab/execution.py:310-313` 的折叠逻辑同理。

**任何适配器零改动**；也不在 runtime 里为具体 `type_id` 开小灶。

### 2.5 记忆生效时机（一个被评审否掉的坑，记录在案）

曾有方案提议「改记忆时把相关对话的 `session_id` 置空，防止内核重放旧版 system prompt」。
**核实代码后否掉**：Claude Code 适配器已用 `_SNAPSHOT_OFF_ARGS`
（`claude_code.py:123-130`，`--system-prompt-snapshot off`）显式关闭系统提示快照，
每轮按智能体**当前**人格重拼重传；CodeBuddy 同款（`codebuddy_cli.py:673-682`）；
非原生 system prompt 内核每轮把人格折进 prompt 更新发送。**结论：改记忆下一轮
立刻生效，不需要任何会话失效处理。**

### 2.6 记忆长度预算

* 字段是 `Text`，物理上不限长，但记忆每轮都占上下文：建议软上限
  `MAX_MEMORY_CHARS = 4000`（每字段，常量放 `persona.py`）；
* 超限时**不拦截保存**（用户自己的智能体自己负责），只在拼装时打一条
  `logger.warning`；
* 设定页编辑框旁显示**字符数**（纯前端本地计数，不叫 tokens）；
  上下文面板的 token 数因为估算改用拼装后的完整 prompt（⑤），自动包含记忆块，
  **不另起一套 token 估算**（铁律：禁止各处各算一套口径）。

---

## 3. 改动清单

### 3.1 后端

| 文件 | 改动 |
| --- | --- |
| `backend/app/models.py` | `AgentProfile` 加 2 列；`ADDITIVE_COLUMNS` 登记 2 行 |
| `backend/app/persona.py` | **新建**：`compose_system_prompt` + `MAX_MEMORY_CHARS` |
| `backend/app/schemas.py` | Create / Update / Read 各加 2 字段 |
| `backend/app/runtime/service.py` | ①②③ 三处改为 `compose_system_prompt(profile, credentials)` |
| `backend/app/collab/execution.py` | ④ 一处替换 |
| `backend/app/api/conversations.py` | ⑤ 改为 `compose_system_prompt(profile, None)` |

### 3.2 前端（桌面 + 移动两套都要，铁律 8）

| 文件 | 改动 |
| --- | --- |
| `frontend/src/pages/AgentEditPage.tsx` | 创建表单（`:257` 附近）与编辑表单（`:533` 附近）各加「核心记忆」「用户偏好」两个多行编辑框 + 字符数提示，位置在「人格」框下方 |
| `frontend/src/mobile/pages/MobileAgentEditPage.tsx` | 同步加两个字段 |
| `frontend/src/lib/types.ts` | 智能体类型加 2 字段（若类型从 `AgentProfileRead` 手工同步） |

文案提示示例：「这段记忆会注入该智能体所有对话的最前方（协作与 IM 同样生效）。」

### 3.3 测试

| 层 | 用例 |
| --- | --- |
| 后端单测 | `persona` 拼装：全空 → None；只写核心记忆 → 只出该块；顺序断言（核心记忆在人格之前）；凭据块在最尾；超软上限打日志不抛错 |
| 后端单测 | `ADDITIVE_COLUMNS` 兼容：老库（无新列）启动补列后行为正常（沿用现有 bootstrap 测试模式） |
| 后端回归 | 现有 `runtime` / `collab` 相关测试全绿（拼装点替换不改变无记忆智能体的输出——空字段时产物与旧 `with_notice` 完全一致） |
| 前端 | `tsc --noEmit` + `npm run build`；`bash scripts/test.sh` 一次跑完 |

---

## 4. 关键决策记录

| # | 决策 | 理由 |
| --- | --- | --- |
| D1 | 记忆存 `AgentProfile` 两列，不建独立 `agent_memories` 表 | 一期只有手动编辑，结构化条目（置顶/归档/来源）是阶段三的需求，现在建表是过度设计 |
| D2 | 五个拼装点收敛到 `persona.compose_system_prompt` | 防「网页有记忆、协作没记忆」与「面板 token 假数」两类静默分叉 |
| D3 | 改记忆不失效 `session_id` | 快照机制已被 `_SNAPSHOT_OFF_ARGS` 关闭（§2.5），失效处理是多余且扰动的 |
| D4 | 超软上限只警告不拦截 | 记忆是用户自己写的自己用；平台负责提醒不负责代办 |
| D5 | 编辑框显示字符数而非 token 数 | 避免前端复制一套 token 估算口径；真实占用由上下文面板（后端口径）呈现 |
| D6 | 协作节点带各自智能体的记忆 | 协作 C5「不共享记忆」指不共享**对话历史**；智能体自带的人格与记忆本来就该跟人走，与 C5 不冲突 |

---

## 5. 后续演进（本方案只留接口，不写代码）

需求方明确：AI 自动总结记忆是**后面**做的事。届时如何接入：

* **阶段二 · 后台提取器**：每轮对话收尾后跑一次 one-shot 总结，往这两个字段
  **追加 / 修订**内容。实现直接照抄 `runtime/titles.py` 的成熟模式（独立会话、
  超时放弃、失败只记日志、绝不影响正常对话）。提取结果建议先进「待确认」状态，
  防对话内容投毒（用户对话里随口说的「记住我的密码」不该被写进核心记忆）。
* **阶段三 · memory MCP（openclaw 式自管理）**：智能体在对话里通过工具自己
  读写记忆。实现直接照抄 `runtime/task_card_mcp.py` 的令牌模式（每轮随机
  `context_id`、0600 凭据文件、内核隔离目录、轮末吊销）。届时可能引入
  `agent_memories` 结构化表，与本方案的两列并存或迁移，另行评审。
* **为什么说「接口已留好」**：这两个字段的语义是「该智能体的长期事实」，
  写入方从「用户手填」演进到「提取器代写」再到「AI 自写」，字段本身不变。
  一期唯一要守的约定：**文档与代码注释里写明这两个字段未来会有程序化写入方**，
  不要在 UI 或 API 上假设「只能人工修改」。

---

## 6. 验收标准（Definition of Done，对照 AGENTS.md §8）

- [ ] 老库升级：启动自动补两列，现有智能体行为与升级前逐字节一致（空记忆 = 旧行为）
- [ ] 设定页（桌面 + 移动）可编辑两字段，字符数提示可见
- [ ] 写了记忆的智能体：网页新对话第一轮就带着记忆（看内核收到的 system prompt 日志/轨迹）
- [ ] 同一智能体被 @ 召唤、进协作节点、走 IM，三处都带记忆
- [ ] 上下文面板 token 数在加记忆后可见增长（口径含记忆块）
- [ ] 协作域 `collab/prompt.py`、`im/bridge.py`、全部适配器 `grep` 零改动
- [ ] `bash scripts/test.sh` 全绿（后端 pytest + 前端 tsc + build）
