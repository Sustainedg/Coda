# MCP 任务进度卡 · 设计方案

> 状态：**方案**（未实施）
> 日期：2026-11-27
> 上游依据：[`docs/task-progress-card-design.md`](../task-progress-card-design.md)（围栏方案，已实施）
> 外部参考：Ekko Studio `docs/chat-chain-changes/2026-09-15-shared-mcp-task-plans.md`
> 目标读者：实施者（照此实现）、QA（照此验收）

---

## 0. 结论速览

| 决策点 | 结论 |
| --- | --- |
| 与现有围栏方案的关系 | **双轨，不替换**：MCP 内核走工具，其余内核继续走正文围栏 |
| 适用内核 | 已声明 `Capabilities.mcp = True` 的 5 个：Claude Code、Codex、CodeBuddy、Hermes、DeepSeek Harness |
| 触发方式 | 内置 MCP server 的工具 `update_task_card` |
| 身份绑定 | 每轮随机 `context_id` + 运行级令牌；**工具参数里没有 `conversation_id` / `run_id`** |
| 服务端权威 | 会话、运行、版本、归属全部由服务端按 `context_id` 解析，模型传了也不认 |
| 存储 | 复用 `task_cards` 表；P1 不加列、不新增表 |
| 幂等 | MCP 不按 `last_run_id` 跳过（一轮允许多次更新）；围栏保留 `last_run_id` |
| 写库顺序 | 先落库，再返回工具结果；失败返回 tool error |
| 生命周期 | `prepare_run` 建绑定，`consume_run` 的 `finally` 收口并吊销令牌 |
| 双写冲突 | 本轮有成功的 MCP 调用 → 忽略围栏块；围栏剥离仍然永远执行 |
| 新增依赖 | 无。MCP server 只用标准库，沿用 `scripts/mcp/*.py` 的 JSON-RPC 写法 |
| 事件协议 | 不新增事件类型；卡片仍走详情接口 + 3 秒轮询 |
| 客户端写入口 | 不提供；内部接口只认运行级令牌 |

**一句话**：MCP 只替换「模型把进度送进来」的通道，卡片语义、合并规则、存储、渲染全部复用现有实现。真正需要新写的是**每轮能力绑定**和**运行收尾**这两件事。

---

## 1. 背景

### 1.1 现状：围栏协议已经跑通

现有实现见 [`docs/task-progress-card-design.md`](../task-progress-card-design.md)，关键事实：

| 事实 | 位置 |
| --- | --- |
| 模型在正文末尾输出 `task_card` 围栏 JSON | `runtime/task_cards.py` 的 `render_notice` / `parse_task_card` |
| 服务端在运行收尾解析、合并、落库 | `runtime/service.py` 的 `consume_run` `finally`；`task_cards.apply_block` |
| 一个对话一张卡，`conversation_id` 唯一 | `models.TaskCard` |
| 二态 `done`，只允许 `false → true` | `task_cards._merge_update` |
| 卡片渲染在输入框上方，只读 | `frontend/src/components/TaskProgressCard.tsx` |
| 协议块从正文 / parts / 流式 / IM 四处剥离 | `task_cards.strip_task_card*`、`frontend/src/lib/taskCardFence.ts`、`im/bridge.py` |

围栏方案的优点是**所有内核都能用**；代价是解析容错代码量大（`task_cards.py` 802 行）、协议块可能短暂出现在流式输出里、模型写坏 JSON 时只能整块放弃。

### 1.2 Ekko Studio 的做法：shared MCP

Ekko 在 2026-09-15 补了 `shared-mcp-task-plans`，让 Hermes 和 14 个 coding agent 通过同一个托管 MCP server 上报进度：

- 托管 server `ekko-studio-interaction`，工具 `ekko_studio_update_plan`
- 每轮生成 `context_id`，拼进**本轮最新输入**（不碰 system prompt，保护缓存）
- 每轮签发 `studio_run_...` 令牌，写 `runtime/mcp-credentials/context-<digest>/auth.json`（0600）
- 工具调用只带 `context_id` + 计划内容；**session / run / revision / execution_state 由服务端解析**
- 先 commit 再 publish；轮末 `finish()` 失效 context，并把 `in_progress` 降回 `pending`
- Ekko Agent 仍用原生工具，与 shared MCP 互斥，避免同一轮两个计划工具

它证明了：**MCP 能覆盖外部 Agent，但前提是「能力句柄 + 服务端生命周期」，不是只挂一个工具。**

### 1.3 本方案要解决的问题

1. 让 5 个 MCP 内核用结构化工具更新卡片，去掉围栏 JSON 的解析风险；
2. 不破坏其余 9 个内核的围栏通道；
3. 不允许模型通过工具参数越权改别的对话；
4. 不新增事件类型、不新增依赖、不做破坏性数据库变更；
5. 一套合并 / 存储 / 渲染代码，两条输入通道共用。

---

## 2. 总体设计

### 2.1 双轨制

```
                    ┌─────────────────────────────────────────┐
                    │            task_cards（同一张表）          │
                    │  conversation_id 唯一 / items JSON /     │
                    │  version 乐观锁 / last_run_id             │
                    └───────────────▲───────────────▲─────────┘
                                    │               │
                  source="mcp"      │               │  source="fence"
                                    │               │
        ┌───────────────────────────┴──┐   ┌────────┴────────────────────┐
        │ 内置 MCP server               │   │ 正文 task_card 围栏           │
        │ coda-task-card               │   │ （现有解析 / 容错 / 剥离）      │
        │ update_task_card             │   │                              │
        └───────────────▲──────────────┘   └────────▲────────────────────┘
                        │                            │
        Capabilities.mcp = True            Capabilities.mcp = False
        （5 个内核）                        （其余内核）
```

两条通道最终都调用**同一个** `task_cards.apply_block`，只多传一个 `source`。合并语义、乐观锁、归属校验、`done` 单向规则一行都不重写。

### 2.2 一次 MCP 调用的完整时序

```
[prepare_run]
  ├─ 判定 definition.capabilities.mcp 且是主场运行
  ├─ 生成 context_id（uuid4）
  ├─ 选择 MCP 版协议说明，拼进 ctx.prompt 末尾
  ├─ prepare_mcp_home：把内置 server 追加进 ctx.mcp_servers
  │    └─ 写凭据文件 <data>/task-card-credentials/<conversation>/context-<hash>/auth.json (0600)
  ├─ 创建 assistant 占位消息
  ├─ registry.register → run
  └─ task_card_mcp.bind(context_id, conversation, owner, run, assistant_message)

[agent 运行中]
  └─ 调 update_task_card({context_id, action, title, items})
       ├─ MCP server 按 context_id 找到 auth.json
       ├─ POST /api/internal/task-cards/update
       │    Authorization: Bearer tc_...
       └─ 服务端：
            token → binding → 校验 context / 归属 / run 活跃
            → normalize_block → apply_block(source="mcp")
            → commit → 返回快照
       └─ 工具结果回给模型（含最新 version / items）

[consume_run finally]
  ├─ 消息落库
  ├─ 仍然 strip_task_card(raw_text) / strip_task_card_from_parts(parts)
  ├─ 若本轮没有成功的 MCP 调用 → 解析围栏并按 source="fence" 应用
  └─ task_card_mcp.finish(context_id, status) → 失效绑定、删凭据文件
```

### 2.3 复用清单

| 复用对象 | 位置 | 说明 |
| --- | --- | --- |
| 卡片表与合并 | `models.TaskCard`、`runtime/task_cards.py` | P1 不改表结构 |
| 乐观锁 / 唯一约束 | `apply_block` | 并发写入沿用现有重试 |
| 协议校验 | `normalize_block` | MCP 与围栏共用 2～20 项、120 字等约束 |
| 围栏剥离 | `strip_task_card*` | MCP 通道也保留，防模型两边都写 |
| 内置 MCP 声明 | `builtin_mcp.py`、`scripts/mcp/*.py` | 新增一个脚本，沿用 JSON-RPC 写法 |
| 每对话隔离目录 | `runtime/isolation.py` 的 `home_for` | 凭据文件放在对话作用域内 |
| 适配器物化 | `runtime/mcp.py` 的 `prepare_mcp_home` | 追加内部 server，不写数据库 |
| 运行注册表 | `runtime/manager.py` 的 `RunRegistry` | 校验 run 是否还活着 |

---

## 3. 内置 MCP server

### 3.1 形态

- 文件名：`scripts/mcp/task_card_mcp_server.py`
- 传输：stdio（与 `image_mcp_server.py` / `coda_admin_mcp_server.py` 一致）
- 协议版本：`2024-11-05`
- 进程模型：**每个对话复用同一个进程配置**，不按轮重启；凭据按 `context_id` 每次调用现读
- 只依赖标准库；stdout 只走 JSON-RPC，日志一律 stderr

不把 server 放进 `BUILTIN_SERVERS` 的用户可见目录：它是框架内部能力，不应出现在「MCP」页被分配 / 编辑。只在 `builtin_mcp.py` 里加脚本路径常量，供 `scripts/release.sh` 与 `docker/Dockerfile` 打包。

### 3.2 工具 schema

工具名：`update_task_card`
server 标识：`coda-task-card`

```json
{
  "name": "update_task_card",
  "description": "更新当前对话的任务进度卡。多步骤任务使用；闲聊和单步任务不要调用。只使用本轮输入给出的 context_id，不要复用历史。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "context_id": {
        "type": "string",
        "description": "本轮输入末尾 <task_card_context> 里的 context_id"
      },
      "action": { "enum": ["create", "update"] },
      "title": { "type": "string", "maxLength": 200 },
      "items": {
        "type": "array",
        "minItems": 2,
        "maxItems": 20,
        "items": {
          "type": "object",
          "properties": {
            "id": { "type": "string", "maxLength": 32 },
            "text": { "type": "string", "maxLength": 120 },
            "done": { "type": "boolean" }
          },
          "required": ["id", "text", "done"],
          "additionalProperties": false
        }
      }
    },
    "required": ["context_id", "action", "items"],
    "additionalProperties": false
  }
}
```

**刻意不出现的字段**：

- `conversation_id`、`session_id`：模型可以编造，绝不能作为定位依据；
- `run_id`、`revision`、`version`、`execution_state`：由服务端决定；
- `user_id`、`owner_id`：由令牌解出。

`action` 的语义与围栏一致：`create` 首次建卡或重规划，`update` 只报变化项。服务端仍用 `_resolve_action` 兼容「有卡时缺省 action」的情况。

### 3.3 调用行为

1. 从工具参数取 `context_id`；
2. 计算 `<credentials_dir>/context-<sha256(context_id)[:16]>/auth.json`；
3. 文件不存在、内容不合法、`auth.json.context_id != 参数 context_id` → 返回 tool error；
4. 读 `token`，POST 内部接口，超时默认 10 秒；
5. 成功：把服务端返回的 `{ok, card}` 作为 JSON 文本返回给模型；
6. HTTP 4xx / 5xx：返回 `isError: true` 与可读原因，让模型决定是否重试；
7. 不把 token 写进 stderr / 返回值。

### 3.4 错误语义

| 情况 | 工具返回 | 模型可见 |
| --- | --- | --- |
| 凭据文件缺失 | `isError: true` | 「当前轮的任务卡凭据不可用，请重新开始一轮」 |
| context 过期 / run 已结束 | `isError: true` | 「任务卡上下文已失效」 |
| 参数不合法（项数、长度） | `isError: true` | 具体字段错误 |
| 写库冲突重试后失败 | `isError: true` | 「进度未保存，可重试」 |
| 网络 / 超时 | `isError: true` | 「平台暂时不可达」 |

模型拿到错误后可以重试或退回正文说明；**不向用户弹错误气泡**，与现有「后台增强失败只记日志」的约定一致。

### 3.5 打包

- `backend/app/builtin_mcp.py` 增加 `TASK_CARD_SCRIPT = _ROOT / "scripts" / "mcp" / "task_card_mcp_server.py"`
- `scripts/release.sh`、`docker/Dockerfile` 把新脚本加入打包清单
- 不新增 pip 依赖

---

## 4. 身份与授权（本方案的核心）

### 4.1 每轮 `context_id`

- 在 `prepare_run` 里生成：`uuid4().hex`（或 `secrets.token_hex(16)`）
- 只在「主场运行 + `persist_session` + `Capabilities.mcp`」时生成
- 不写入数据库；只存在于：
  1. 本轮 `ctx.prompt` 的 `<task_card_context>` 段；
  2. 进程内 binding 表；
  3. 凭据文件内的 `context_id` 字段。
- 轮末立即失效；历史对话里出现过的旧 `context_id` 不再可用。

**它不是 `conversation_id`**，也不是 `plan_id`：没有可推导关系，不能拿它反查别的会话。

### 4.2 凭据文件

目录（对话作用域，天然隔离）：

```
<data_dir>/task-card-credentials/<conversation_id>/
    context-<sha256(context_id)[:16]>/
        auth.json        # 0600
```

`auth.json`：

```json
{
  "token": "tc_<43 位 base64url>",
  "context_id": "<本轮 context_id>"
}
```

- 目录 `0700`，文件 `0600`
- 令牌 256 位随机熵，前缀 `tc_`，与用户 PAT 完全隔离
- 令牌哈希只存内存 binding；文件里存明文（MCP server 要读）
- `finish()` 删除整个 `context-<hash>` 目录；删除失败也不影响内存失效
- 路径只由 `sha256(context_id)` 拼成，**不使用模型传入的原始字符串做路径**

### 4.3 进程内 binding

新增 `backend/app/runtime/task_card_mcp.py`：

```python
@dataclass
class TaskCardContext:
    context_id: str
    conversation_id: str
    owner_id: str | None
    run_id: str
    anchor_message_id: str | None
    token_hash: str
    active: bool = True
    mcp_used: bool = False
    calls: int = 0
    created_at: float = 0.0
    last_call_at: float = 0.0

_contexts: dict[str, TaskCardContext] = {}
_by_token: dict[str, str] = {}   # token_hash -> context_id
```

接口：

| 函数 | 作用 |
| --- | --- |
| `begin(...) -> (context_id, credentials_dir)` | 生成 context、令牌，写凭据文件，登记 binding |
| `bind(context_id, conversation_id, owner_id, run_id, anchor_message_id)` | 运行注册后补齐归属 |
| `authenticate(authorization: str) -> TaskCardContext \| None` | 校验 Bearer 令牌，返回活跃 binding |
| `mark_used(context_id)` | 记录本轮用过 MCP，供收尾判断双写 |
| `finish(context_id, status)` | 标记失效、删除令牌索引与凭据目录 |
| `render_notice(context_id, card) -> str` | 生成 MCP 版提示词段 |

**多进程限制**：binding 在进程内存。当前服务按单进程运行；若未来启用多 worker，必须把 binding 与令牌哈希迁到共享存储（数据库表或 Redis），否则 MCP 请求可能落到另一个 worker 而 401。这个约束写进代码注释与部署文档。

### 4.4 内部接口与校验顺序

路由：`POST /api/internal/task-cards/update`，`include_in_schema=False`（参照现有记录网关的内部路由）。

请求体：

```json
{
  "context_id": "...",
  "action": "create",
  "title": "...",
  "items": [{"id": "t1", "text": "...", "done": true}]
}
```

校验顺序（任何一步失败都返回 4xx，且不写库）：

1. `Authorization: Bearer tc_...` 存在且格式正确；
2. 令牌哈希命中 binding；
3. `binding.active == True`；
4. `payload.context_id == binding.context_id`；
5. `binding.run_id` 仍能在 `RunRegistry.get()` 找到且未结束；
6. 请求体只允许 `context_id/action/title/items` 四个字段；出现 `conversation_id`、`run_id`、`version` 等一律 422；
7. `normalize_block` 校验 2～20 项、文本长度、ID 合法；
8. 调用 `apply_block(conversation_id=binding.conversation_id, anchor_message_id=binding.anchor_message_id, block=..., run_id=binding.run_id, source="mcp")`；
9. `mark_used`，返回最新快照。

**归属校验不依赖请求体**：`conversation_id` 只来自 binding；`owner_id` 只用于日志与断言。

### 4.5 威胁模型

| 威胁 | 结果 | 缓解 |
| --- | --- | --- |
| 模型编造 `context_id` | 404/409，不写库 | 随机 128 位 + 进程内表 |
| 模型传别人的 `conversation_id` | 422，不写库 | 字段白名单，直接拒绝 |
| 模型重放历史 `context_id` | 409，不写库 | 轮末 `finish` 失效 |
| 拿到旧令牌重放 | 401 | 令牌随 binding 失效 |
| 跨对话读取凭据文件 | 需要文件系统权限；agent 工作目录与配置目录按对话隔离 | `home_for(conversation_id, ...)` + 0600 |
| prompt injection 让 agent 乱报进度 | 只能改本对话的卡 | 卡片非权威数据；正文与消息不受影响 |
| 令牌泄漏到日志 | 不会 | 只记录 `context_id[:8]` 与 conversation_id，绝不记录 token |

**注意**：如果内核本身有 shell 权限，理论上可以读自己对话目录下的凭据文件；但它读不到别的对话的目录（隔离目录按对话切分），且令牌只对本次运行有效。这个风险与「agent 能改自己工作目录」同级，可接受。

### 4.6 与 Ekko 的对应关系

| Ekko | 本方案 |
| --- | --- |
| `context_id`（每轮 UUID） | `context_id`（每轮 UUID） |
| `studio_run_...` 令牌 + `auth.json` | `tc_...` 令牌 + `auth.json` |
| `TaskPlanRuns` binding | `task_card_mcp` binding |
| `/api/studio/task-plans/update` | `/api/internal/task-cards/update` |
| 服务端解析 session / run / revision | 服务端解析 conversation / run / version |
| `finish()` 降级 `in_progress` | P1 只失效 binding；P2 可加执行状态 |

---

## 5. 生命周期

### 5.1 轮次开始（`prepare_run`）

顺序固定：

1. `load_profile` 得到 `definition`；
2. 判定 `mcp_task_card = persist_session and not summon_profile_id and definition.capabilities.mcp`；
3. `mcp_task_card` 为真时：
   - `context_id, credentials_dir = task_card_mcp.begin(conversation.id, owner_id)`
   - 用 `task_card_mcp.render_notice(context_id, card)` 生成提示词段
   - 否则沿用现有 `_task_card_notice`
4. 拼 `prompt_text`，参与上下文估算（与现有顺序一致，不能后加）；
5. `prepare_mcp_home(..., task_card_context_id=context_id)` 追加内部 server；
6. 创建 assistant 占位消息；
7. `registry.register` 得到 `run`；
8. `task_card_mcp.bind(context_id, conversation.id, owner_id, run.id, assistant_message.id)`；
9. 启动 agent。

**为什么先 `begin` 后 `bind`**：提示词与凭据文件在 `prepare_mcp_home` 之前就要有；`run_id` 只有 `registry.register` 之后才有。中间这段窗口没有 agent 在跑，不可能收到工具调用。

### 5.2 轮次结束（`consume_run` 的 `finally`）

在消息 `commit` 之后、`registry.finish` 之前：

```python
if mcp_task_context_id:
    await task_card_service.finish_task_card_context(
        mcp_task_context_id,
        status="complete" if status == "complete" else ("interrupted" if status == "cancelled" else "failed"),
    )
```

`finish` 做三件事：

1. `binding.active = False`，从 `_by_token` 删除；
2. 删除凭据目录；
3. P1 不改卡片；P2 可在这里把 `execution_state` 落库。

**双写判定**：

```python
if committed and status == "complete" and prepared.persist_session:
    if mcp_task_context_id and task_card_mcp.was_used(mcp_task_context_id):
        logger.info("本轮已通过 MCP 更新任务卡，忽略正文围栏块")
    else:
        block, parse_error = parse_task_card(raw_text)
        if block:
            await apply_block(..., source="fence")
```

`strip_task_card` / `strip_task_card_from_parts` 仍然**无条件执行**，防止模型两边都写时协议 JSON 泄漏。

### 5.3 失败 / 取消 / 中断

| 状态 | 卡片行为 |
| --- | --- |
| `complete` | MCP 已写则保留；未写则解析围栏兜底 |
| `error` | 不应用围栏；MCP 已写的保留（完成项是真的完成了） |
| `cancelled` | 同上；P1 不标「中断」，P2 可标 `interrupted` |
| 进程被杀 | binding 随进程消失，令牌自动失效；卡片停在最后一次已提交状态 |

与现有围栏方案一致：**失败回合绝不把未完成项标成完成**。

### 5.4 进程重启

P1 没有持久化的「运行中」状态，所以重启不需要恢复逻辑：

- 内存 binding 消失 → 旧令牌全部失效；
- 卡片在 SQLite 里，重新打开对话照常读回；
- 残留的凭据目录在下次同对话运行时按同名覆盖；建议启动时做一次清理（可选）。

P2 若引入 `execution_state`，再按 Ekko 的做法：启动时把孤儿 `running` 标 `interrupted`。

### 5.5 不适用的场景

以下场景**不注入**任务卡 MCP，继续走围栏或不建卡：

- `summon_profile_id` 非空（@ 召唤轮）；
- 协作域运行（`collab/execution.py` 的 `prepare_mcp_home` 不传 `task_card_context_id`）；
- 自动起标题（`titles.py`）；
- `persist_session == False` 的运行。

---

## 6. 存储与合并

### 6.1 P1 不改表

复用 `task_cards`：

| 列 | 用途 |
| --- | --- |
| `conversation_id` 唯一 | 一个对话一张卡 |
| `items` JSON | `id/text/done/done_at/updated_at` |
| `version` | 乐观锁 + 前端比较 |
| `last_run_id` | **仅围栏通道**幂等 |
| `anchor_message_id` | 接口兼容；MCP `create` 时写本轮占位消息 id |

不需要 `ADDITIVE_COLUMNS`，因为不加列。

### 6.2 `apply_block` 扩展

```python
async def apply_block(
    conversation_id: str,
    anchor_message_id: str | None,
    block: dict[str, Any],
    run_id: str,
    *,
    source: str = "fence",   # "fence" | "mcp"
) -> TaskCard | None:
```

行为差异只有一处：

```python
if source == "fence" and card.last_run_id == run_id:
    return card          # 围栏：同一轮重复应用幂等
# source == "mcp"：不按 run 跳过，一轮允许多次更新
```

其余不变：唯一约束、乐观锁、`changed == False` 不 bump、异常只记日志。

### 6.3 幂等策略

| 通道 | 幂等键 | 说明 |
| --- | --- | --- |
| 围栏 | `last_run_id` | 一轮只解析一次；重试 / 重放跳过 |
| MCP | `context_id + 内容` | 不按 run 跳过；重复提交同一内容时 `merge_block` 返回 `changed=False`，不 bump version |

**不要复用 `last_run_id` 做 MCP 幂等**：MCP 一轮内会调用多次（建计划、完成第一步、完成第二步），按 run 跳过会把第二次以后的更新全部吞掉。这是本方案最容易踩的坑。

并发场景：两个运行同时对同一对话写卡，乐观锁 `WHERE version = :v` 重试最多 3 次；`done` 单向合并保证不会互相回退。仍失败只记日志，不影响消息。

### 6.4 合并语义不变

- `create`：无卡新建；有卡按 `id` 重规划，`done = old.done or new.done`，已完成项不丢；
- `update`：无卡忽略；未知 `id` 忽略；`true → false` 忽略；重复 `id` 以最后一次为准；
- 有效项不足 2 / 超过 20 → 整块忽略；
- 全部无变化 → 不写库、不 bump。

这些规则在 `_merge_create` / `_merge_update` 里已经实现，MCP 通道直接复用。

### 6.5 双写优先级

1. 本轮 `mcp_used == True` → 收尾不解析围栏；
2. 本轮 `mcp_used == False` → 解析围栏兜底；
3. 无论哪条通道，正文与 parts 里的围栏块永远剥离；
4. 两个通道都失败 → 卡片保持上一次已提交状态，不猜测、不修补。

### 6.6 P2（可选，不在本方案范围）

若要让用户看到「进行中 / 已中断」，再单独设计：

- `items` 增加 `status: pending|in_progress|completed`；
- `task_cards` 增加 `execution_state`、`context_id` 列，登记 `ADDITIVE_COLUMNS`；
- 前端卡片、围栏协议、IM 文案同步；
- 启动时标记孤儿 `running`。

这会把二态协议改成三态，影响面远大于本方案，建议另开文档。

---

## 7. 提示词注入

### 7.1 两种通知

**MCP 版**（`task_card_mcp.render_notice`）：

```text
【任务进度卡片协议 · MCP】
当本轮确实在推进一件需要多步完成的工作时，调用 coda-task-card 的 update_task_card 工具：
- 首次给出计划：{"context_id":"<本轮 context_id>","action":"create","title":"一句话任务名","items":[{"id":"t1","text":"步骤一","done":false},{"id":"t2","text":"步骤二","done":false}]}
- 后续轮次只报告状态变化：{"context_id":"<本轮 context_id>","action":"update","items":[{"id":"t1","done":true}]}
规则：只使用本轮输入给出的 context_id，不要复用历史；items 数量 2~20；id 短且稳定；只把确实完成的项标 done=true；闲聊、问答、单步指令不要调用。不要再输出 task_card 围栏。
```

有卡时把「当前卡片」逐项列出（与现有 `render_notice` 一致），让模型知道哪些已完成、不要重复报。

**围栏版**：现有 `task_cards.render_notice`，不改。

### 7.2 注入位置

- `prepare_run`：在 `build_context` 之前、`prompt_text` 拼装时注入；与现有 `_task_card_notice` 同一位置，保证参与上下文估算。
- `regenerate_last`：同样处理。
- 注入到 `ctx.prompt`（每轮必发的新输入），**不注入 `ctx.system_prompt`**：session 型内核会缓存 system prompt，变化的 `context_id` 放进去会破坏缓存；Ekko 也是这么做的。

### 7.3 注入条件矩阵

| 有卡 | 用户正文像多步任务 | MCP 内核 | 注入 |
| --- | --- | --- | --- |
| 有 | 任意 | 是 | MCP 协议 + 当前卡片 |
| 有 | 任意 | 否 | 围栏协议 + 当前卡片 |
| 无 | 是 | 是 | MCP 建卡协议 |
| 无 | 是 | 否 | 围栏建卡协议 |
| 无 | 否 | 任意 | 不注入 |

「像多步任务」仍用现有 `looks_like_task_request` 硬闸门，MCP 通道不放松：工具调用也可能被模型滥用，服务端闸门是第二层保险。

### 7.4 token 开销

- 无卡 + 闲聊：0；
- MCP 建卡：一段协议 + schema（由 MCP 客户端注入工具定义），比围栏示例短；
- 有卡：当前卡片逐项列出，最多 20 行，量级可控。

---

## 8. 适配器接入

### 8.1 支持矩阵（以 `Capabilities.mcp` 为准）

| 适配器 | `mcp` | `mcp_mount` | 本方案 |
| --- | --- | --- | --- |
| `claude_code.py` | ✅ | ✅ | 注入内置 server |
| `codex.py` | ✅ | ✅ | 注入内置 server |
| `codebuddy_cli.py` | ✅ | ✅ | 注入内置 server |
| `hermes_agent.py` | ✅ | ✅ | 注入内置 server |
| `deepseek_harness.py` | ✅ | ✅ | 注入内置 server |
| 其余 9 个 | ❌ | ❌ | 继续围栏 |

新增内核只要实现 `mcp_mount` 并声明 `mcp=True`，自动获得 MCP 通道；不需要在 `service.py` 里加分支。

### 8.2 注入点

在 `runtime/mcp.py` 的 `prepare_mcp_home` 增加可选参数：

```python
async def prepare_mcp_home(
    session, definition, profile, conversation, ctx,
    *, scope_id=None, override_ids=None,
    task_card_context_id: str | None = None,
) -> list[str]:
```

当 `task_card_context_id` 非空且 `definition.capabilities.mcp`：

```python
ctx.mcp_servers.append(McpServerSpec(
    name="任务进度卡",
    slug="coda-task-card",
    description="平台内置：更新当前对话的任务进度卡",
    transport="stdio",
    command=sys.executable or "python3",
    args=[str(TASK_CARD_SCRIPT)],
    env={
        "CODA_TASK_CARD_CREDENTIALS_DIR": str(credentials_dir),
        "CODA_TASK_CARD_API": f"{base_url}/api/internal/task-cards/update",
        "CODA_TASK_CARD_TIMEOUT": "10",
    },
))
```

要点：

- 只在 `service.prepare_run` / `regenerate_last` 传 `task_card_context_id`，`titles.py` / `collab/execution.py` 不传；
- 内部 server 只存在于隔离配置里，不写 `mcp_servers` 表，不出现在 MCP 管理页；
- `env` 里只有目录、API 地址、超时，**没有令牌**；令牌在每次调用时从文件读；
- 5 个适配器的 `mcp_mount` 不需要逐个改：它们都序列化 `ctx.mcp_servers`，追加的 spec 自动生效。

### 8.3 权限预允许

Claude Code 默认会对 MCP 工具逐次询问权限。必须在挂载配置里把**这一个工具**加入 allow 列表，至少包括：

- Claude Code：`permissions.allow` 增加 `mcp__coda-task-card__update_task_card`；
- Codex：按现有 `approval_policy` / `sandbox` 配置，确保该工具不被逐次拦截；
- CodeBuddy / Hermes / DSH：按各自「工具自动批准」配置处理。

**只允许这一个工具**，不要放开整个 MCP server，也不要用 `bypassPermissions` 图省事。

### 8.4 非 MCP 内核

- 不注入内部 server；
- 继续现有围栏协议；
- 如果模型在 MCP 内核上仍然输出围栏（提示词没遵守），收尾逻辑按 §6.5 处理：MCP 已用则忽略，未用则兜底。

---

## 9. 前端与 IM

### 9.1 卡片渲染

不变：`TaskProgressCard` 仍在 `Composer` 的 `header` 槽，只读，默认收起，折叠状态存 `localStorage`。MCP 更新会 bump `version`，现有 3 秒轮询的 `sameTaskCard` 比较会触发重渲染。

### 9.2 流式输出

- MCP 通道不产生正文协议块，实时流天然干净；
- 仍保留 `stripTaskCardFence`，处理模型两边都写的情况；
- 不新增事件类型。

### 9.3 工具轨迹去噪

MCP 工具调用会以 `tool_call` / `tool_result` part 出现在时间线里，用户会同时看到「工具卡 + 任务卡」。建议：

- 在 `frontend/src/components/MessageView.tsx` 的 `groupTimeline` 里，按工具名过滤内部工具：
  - 名称包含 `coda-task-card` 且以 `update_task_card` 结尾（兼容 `mcp__coda-task-card__update_task_card`、`coda-task-card__update_task_card` 等前缀差异）；
- 过滤只影响展示，不改事件、不改落库；
- 工具报错也不额外弹泡，模型自己会看到 `isError`。

### 9.4 IM

`im/bridge.py` 的 `_collect` 继续调用 `strip_task_card`；MCP 通道没有正文块，无需改。若模型兜底输出围栏，仍会被剥掉。

---

## 10. 安全与边界

| 项 | 要求 |
| --- | --- |
| 令牌 | 每轮随机 256 位，前缀 `tc_`，只存哈希在内存；不写日志 |
| 文件权限 | 目录 0700，`auth.json` 0600；轮末删除 |
| 路径 | 只用 `sha256(context_id)` 拼路径，禁止原始字符串拼接 |
| 请求体 | 字段白名单；出现 `conversation_id/run_id/version` 直接 422 |
| 限流 | 每 context 最多 120 次调用；超限 429（防模型死循环） |
| 载荷 | 2～20 项、120 字、标题 200 字；与围栏一致 |
| 归属 | `conversation_id` 只来自 binding；`owner_id` 只用于断言与日志 |
| 客户端入口 | 不新增任何面向浏览器的卡片写接口；内部接口 `include_in_schema=False` |
| 失败 | 只记 `logger.warning`，不弹用户错误；消息落库不受影响 |

部署约束：**单进程**。多 worker 需要把 binding 与令牌哈希迁到共享存储，或改用粘性会话；这一点必须写进部署文档与代码注释。

---

## 11. 实施子任务

> 统一验收：`bash scripts/test.sh`（后端 pytest + 前端 tsc）、`npm --prefix frontend run build`。下表「验收」列的是额外针对性验证。

| # | 子任务 | 改动文件 | 完成标准 | 验收 | 依赖 |
| --- | --- | --- | --- | --- | --- |
| S1 | 每轮 binding 与令牌 | 新建 `backend/app/runtime/task_card_mcp.py` | `begin/bind/authenticate/mark_used/finish`；凭据文件 0600；轮末删除；单测覆盖过期、错 profile、错 context、重放 | `pytest tests/test_task_card_mcp.py` | 无 |
| S2 | 内置 MCP server | 新建 `scripts/mcp/task_card_mcp_server.py`；`builtin_mcp.py` 加路径常量 | `initialize` / `tools/list` / `tools/call`；按 context 读凭据；错误走 `isError`；只用标准库 | 子进程测试：正常调用、缺文件、坏 JSON、HTTP 4xx、超时 | S1 的接口契约 |
| S3 | 内部接口 | 新建 `backend/app/api/task_cards_internal.py`；`api/__init__.py` 注册 | Bearer 校验、字段白名单、run 活跃校验、调用 `apply_block(source="mcp")`、返回快照 | API 测试：401/409/422/200；跨对话拒绝 | S1 |
| S4 | `apply_block` 扩展 | `backend/app/runtime/task_cards.py` | 增加 `source` 参数；围栏保留 `last_run_id`；MCP 不按 run 跳过；合并语义不变 | 单测：一轮多次 MCP 更新全部生效；围栏重放仍幂等 | 无（可与 S1 并行） |
| S5 | 提示词与运行接线 | `backend/app/runtime/service.py` | `prepare_run` / `regenerate_last` 生成 context、选通知、传 `prepare_mcp_home`、运行注册后 `bind`、`finally` 里 `finish` | 单测：MCP 内核含 `context_id`、非 MCP 不含；失败轮不写卡；MCP 用过则忽略围栏 | S1、S4 |
| S6 | MCP 物化注入 | `backend/app/runtime/mcp.py` | `prepare_mcp_home` 接受 `task_card_context_id`，追加内部 spec；不写库、不出现在管理页 | 适配器测试：5 个内核配置含 `coda-task-card`；titles/collab 不含 | S1、S5 |
| S7 | 适配器权限 | 5 个适配器的 `mcp_mount` / 配置生成 | 仅预允许 `update_task_card`；不放开整个 server | 检查生成的 Claude/Codex/CodeBuddy/Hermes/DSH 配置 | S6 |
| S8 | 前端工具去噪 | `frontend/src/components/MessageView.tsx`（或 `groupTimeline`） | 隐藏内部工具调用；不影响其它工具 | `typecheck` + 手测：只看到任务卡，不看到工具卡 | 无（可并行） |
| S9 | 测试补齐 | `tests/test_task_cards.py`、新建 `tests/test_task_card_mcp.py`、`tests/test_adapters.py` | 覆盖 §12 验收清单 | `pytest` 全绿 | S1-S8 |
| S10 | 打包 | `scripts/release.sh`、`docker/Dockerfile` | 新脚本随包分发 | 构建产物内含 `task_card_mcp_server.py` | S2 |
| S11 | 文档同步 | `docs/architecture.md`、`docs/task-progress-card-design.md`、本文档 | 架构文档补数据流与内部接口；围栏文档加「MCP 通道」链接 | 评审 | 全部 |

**并行说明**：S1 / S2 / S4 / S8 可并行起步；S3 依赖 S1；S5 依赖 S1+S4；S6 依赖 S5；S7 依赖 S6；S9 / S10 / S11 收尾。

**开关**：建议加 `CODA_TASK_CARD_MCP`（默认 `0`，灰度打开）。关闭时：不注入 server、不生成 context、不挂内部路由，行为与现在完全一致。回滚只需关开关。

---

## 12. 验收清单

1. MCP 内核（Claude Code）执行多步任务：模型调用 `update_task_card`，卡片出现，正文无协议 JSON。
2. 同一轮连续三次更新（create → 完成 t1 → 完成 t2）：三次都生效，`version` 递增，没有因 `last_run_id` 被吞。
3. 非 MCP 内核（如豆包 CLI）：仍走围栏，卡片正常。
4. MCP 内核模型同时输出围栏块：MCP 生效，围栏被剥离且不重复写卡。
5. 轮末重放旧 `context_id`：409，不写库。
6. 用 A 对话的 `context_id` 从 B 对话调用：409/404，不写库。
7. 请求体带 `conversation_id`：422，不写库。
8. 运行失败 / 取消：不新增完成项；已完成的保留。
9. 刷新页面、重启后端：卡片内容与勾选状态不变。
10. 3 秒轮询能看到 MCP 中途更新；不整页闪烁。
11. 工具轨迹里看不到内部工具卡（或只看到折叠的一条）。
12. IM 通道正文不含任何协议 JSON。
13. `bash scripts/test.sh`、`npm --prefix frontend run build` 通过。
14. 关闭 `CODA_TASK_CARD_MCP` 后，现有围栏测试全部通过。

---

## 13. 风险与应对

| 风险 | 应对 |
| --- | --- |
| 多 worker 下 binding 不共享 | 明确单进程部署；需要多 worker 时迁 DB / Redis，或在网关层做粘性 |
| 内核长驻进程缓存旧工具列表 | 文档提示：启用后重启内核；MCP server 本身按 context 现读凭据，不需要重启 |
| 权限弹窗打断 | 5 个适配器分别预允许 `update_task_card`，只放一个工具 |
| 模型不调用工具 | 提示词明确「多步任务必须调用」；保留围栏兜底；不保证 100% |
| 模型两边都写 | MCP 优先 + 无条件剥离；`mcp_used` 标记 |
| MCP server 崩溃 | 工具返回错误；本轮若无 MCP 调用则围栏兜底；不影响消息 |
| 凭据文件残留 | 轮末删除；启动时可选清理超过 24 小时的目录 |
| 写库冲突 | 沿用乐观锁重试 3 次；失败只记日志 |
| 提示词变长 | 只在有卡 / 像任务时注入；最多 20 行 |
| 工具轨迹噪声 | 前端按名称过滤内部工具 |
| 未来新增 MCP 内核 | 只要 `mcp=True` + `mcp_mount`，自动接入，不改 `service.py` |

---

## 14. 明确不做

- 不替换围栏方案；9 个非 MCP 内核继续用围栏。
- 不新增事件类型；不碰 `agents/events.py` / `frontend/src/lib/agentStream.ts`。
- 不新增依赖；MCP server 只用标准库。
- 不新增面向浏览器的卡片写接口；内部接口只认运行级令牌。
- 不引入三态 `in_progress` / `execution_state`（P2 另开文档）。
- 不做多卡片 / 历史版本 / 手动编辑。
- 不把内部 server 放进「MCP」管理页，不允许用户分配 / 编辑。
- 不改 `ConversationDetail` 响应结构；卡片仍走 `task_card` 字段。

---

## 附录 A：新增 / 修改文件索引

| 文件 | 类型 | 作用 |
| --- | --- | --- |
| `backend/app/runtime/task_card_mcp.py` | 新建 | context / 令牌 / 凭据 / binding / finish |
| `backend/app/runtime/task_cards.py` | 修改 | `apply_block` 增加 `source`；其余不变 |
| `backend/app/runtime/mcp.py` | 修改 | `prepare_mcp_home` 追加内部 server |
| `backend/app/runtime/service.py` | 修改 | `prepare_run` / `regenerate_last` / `consume_run` 接线 |
| `backend/app/api/task_cards_internal.py` | 新建 | `POST /api/internal/task-cards/update` |
| `backend/app/api/__init__.py` | 修改 | 注册内部路由（`include_in_schema=False`） |
| `backend/app/builtin_mcp.py` | 修改 | `TASK_CARD_SCRIPT` 路径常量 |
| `scripts/mcp/task_card_mcp_server.py` | 新建 | stdio MCP server |
| `scripts/release.sh` / `docker/Dockerfile` | 修改 | 打包新脚本 |
| `frontend/src/components/MessageView.tsx` | 修改 | 过滤内部工具轨迹 |
| `backend/tests/test_task_card_mcp.py` | 新建 | binding / 令牌 / 接口 / server 测试 |
| `backend/tests/test_task_cards.py` | 修改 | `apply_block(source=...)` 用例 |
| `backend/tests/test_adapters.py` | 修改 | 5 个内核注入断言 |
| `docs/architecture.md` | 修改 | 数据流与内部接口 |
| `docs/task-progress-card-design.md` | 修改 | 加「MCP 通道」链接 |
