# 用量归一化设计（已落地）

> 状态：**P1–P5 已交付**。已完成：
> 1. 审计表补上「缓存读 / 缓存写」两列（已通过前端 typecheck）；
> 2. 从本机真实运行数据抽出 Claude / CodeBuddy 的原始 usage fixture；
> 3. 输出本设计稿与单测矩阵；
> 4. 评审确认：Codex 只修新行、未采样本协议保持 pending、Custom CLI 默认 auto、
>    推理只记明细、raw_usage 结构化截断、网关记录 response_shape，
>    并预留双轨采集（agent_report / gateway_proxy）与请求级幂等去重。
>
> P1 已交付：`backend/app/agents/metering.py`（公共归一化核心）+ `backend/tests/test_usage_normalize.py`。
> **P1 不改任何现有调用点**，旧函数行为保持不变，确保线上零行为变化。
>
> P2 已交付：旧入口变薄包装 + 适配器 `usage_protocol` 声明 + 网关 `response_shape`。
> 验证：新增单测 87 passed；P2 接线主体验证时**全量后端 1108 passed / 1 skipped**；
> 最后的 pending 收口（Hermes / Antigravity 显式 unknown）后，关键链路 268 passed。
>
> P3 已交付：`usage_records` 新增原始留痕 / 归一化元数据 / 双轨采集列；
> `upstream_request_id` 唯一索引；网关后到可覆盖 Agent 自报（空值不覆盖有效计数）。
> 验证：P3 去重单测 9 passed；usage 相关 197 passed；
> DB / 迁移 / 运行时 / 网关相关 131 passed；前端 typecheck 通过。
>
> P4 已交付：Claude / CodeBuddy / DeepSeek / Hermes 改为**原样上报 usage**，
> 不再由适配器提前丢嵌套字段或翻译键名；`raw_usage` 现在能保住
> `cache_creation` / `output_tokens_details` / 驼峰 / Hermes 短字段。
>
> P5 已交付：前端 `summarizeUsage` 与后端 `app.agents.metering` 对齐；
> `Message.usage_protocol` 快照让 DeepSeek 等“分离式”不会在展示层被误判；
> 「输入」统一为**不含缓存**的普通输入；`total = 窗口占用 + 输出`；
> 用量页旧消息段 `_accumulate()` 也改用同一归一化核心。
> 前端 `normalizeUsage` 已对 13 个 fixture 做逐字段对账，全部一致。
>
> P2–P5 的**行为切换点**（只影响新写入的账本行，历史行不动）：
> - Codex：`input_tokens` 从 5 回落到 4、`cache_read_tokens` 保持 1，窗口 5，
>   不再把缓存重复计一遍；
> - Custom CLI 驼峰字段：从“认不出、记 unknown”变为正确归一化；
> - Anthropic / OpenAI Chat / Responses：四个计费字段语义不变，成本不变；
> - **Hermes / Antigravity**：显式 `unknown` → 新账本行保持 unknown（pending 阻塞），
>   拿到真实 `raw_usage` 验证后再放开；`Message.usage` 仍保留内核原始事件用于展示；
> - **双轨去重**：同一个 `upstream_request_id` 只留一行；网关后到会用一手数据覆盖自报。
>
> 关联文档：`docs/context-usage.md`（上下文口径）、`docs/usage-gateway.md`（账本口径）。

---

## 1. 目标与非目标

### 目标

- **一套字段表、一套判定规则**：适配器、账本、旧消息汇总、前端展示不再各自维护关键词列表。
- **协议优先**：先看来源/协议（Anthropic Messages / OpenAI Chat / OpenAI Responses / Gemini / 适配器声明的方言），字段名探测只做兜底。
- **六个标准字段**口径固定：
  - `input_tokens`：普通输入（**不含**缓存）
  - `cache_read_tokens`：缓存读
  - `cache_creation_tokens`：缓存写
  - `output_tokens`：输出
  - `reasoning_tokens`：推理/思考（**输出子集，只做明细，不重复计总量**）
  - `context_tokens`：窗口占用（输入侧**含**缓存）
  - `total_tokens`：派生展示值 = `context_tokens + output_tokens`
- **原始留痕**：账本行保留原始 usage payload（或关键原始字段）与解析协议，方便对账。
- **成本解耦**：token 归一化与成本计算分开；归一化只保证四个计费字段语义稳定，成本公式不因本次调整改变。
- **历史兼容**：老消息、老账本行缺字段不报错，不重算历史。

### 非目标

- 不在本次改动里重算历史成本。
- 不改变“上下文窗口快照 vs 整轮累计”的业务语义（这是调用方选择帧的问题，不是归一化的问题）。
- 不引入第三方 tokenizer；仍然只做 usage 计数归一化。

---

## 2. 现状：四套归一化，字段表和判定规则不一致

| # | 位置 | 用途 | 关键问题 |
|---|---|---|---|
| 1 | `backend/app/agents/builtin/util.py:84` `context_tokens()` | 适配器上报 `context.usage` | 判“分离式”的标记不含 `cache_write_input_tokens`；认驼峰 |
| 2 | `backend/app/runtime/usage_ledger.py:152` `normalize_usage()` | 账本 / 审计 / 用量页 | 判“分离式”的标记**含** `cache_write_input_tokens`；不认驼峰；不认 `reasoning_output_tokens` |
| 3 | `backend/app/api/usage.py:87` `_accumulate()` | 用量页里“账本启用前”的老消息 | 又一套标记（只认两个 `*_input_tokens`） |
| 4 | `frontend/src/lib/usage.ts:46` `summarizeUsage()` | 输入框下方消耗、导出、工单 | 输入不扣缓存；不认 `cache_write_*`；不认驼峰 |

调用链：

```
适配器 usage 事件 ──> Message.usage（下方消耗）
                  └─> service.consume_run ──> usage_ledger.normalize_usage ──> UsageRecord（账本审计 / 用量页）
gateway tee usage ─────────────────────────> usage_ledger.normalize_usage ──> UsageRecord
旧消息 usage ──────────────────────────────> api/usage.py._accumulate ─────> 用量页旧消息段
Message.usage ─────────────────────────────> frontend summarizeUsage ──────> 下方消耗
```

### 已确认的差异（不是猜测）

1. **Codex 缓存被重复计**：`codex._app_usage()`（`backend/app/agents/builtin/codex.py:867`）永远输出
   `cache_write_input_tokens` 键；账本看到这个词就判成“分离式”，把 `cached_input_tokens`
   又加一遍。实测数据 `inputTokens=5, cachedInputTokens=1, outputTokens=6, totalTokens=11`
   证明缓存是输入的子集；账本却会算出 `context=6`、用量页 `total=12`。
2. **嵌套字段被丢**：Claude/CodeBuddy 的 `result` 适配器只保留标量
   （`claude_code.py:737-742`、`codebuddy_cli.py:841-846`），会把
   `cache_creation.{ephemeral_1h,ephemeral_5m}` 和 `output_tokens_details.thinking_tokens`
   直接过滤掉；账本根本看不到。
3. **前端输入口径**：OpenAI 派 `prompt_tokens` 含缓存，前端“读取”不扣缓存，账本“输入”扣了，
   同一份数据两个数。
4. **推理字段**：`reasoning_output_tokens`（Codex）/ `thinking_tokens`（Anthropic）在账本里
   分别记 0 / 完全不识别。

---

## 3. 真实样本与缺口

### 3.1 真实样本（本机 `backend/data/coda.db`，只读抽取）

**Claude Code · `stream_event/message_delta`（真实帧）**

```json
{"input_tokens": 40683, "cache_creation_input_tokens": 0,
 "cache_read_input_tokens": 512, "output_tokens": 66}
```

**Claude Code · `result`（真实帧，含新字段）**

```json
{"input_tokens": 11479, "cache_creation_input_tokens": 0,
 "cache_read_input_tokens": 2688, "output_tokens": 134,
 "output_tokens_details": {"thinking_tokens": 80},
 "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0}}
```

**CodeBuddy · `result`（真实帧）**

```json
{"input_tokens": 16485, "cache_creation_input_tokens": 0,
 "cache_read_input_tokens": 246272, "output_tokens": 7375}
```

关键事实：

- `message_start.usage` 实测经常全 0；真实窗口占用看 `message_delta`。
- `result` 是整轮累计，不是单次请求窗口。
- Anthropic 输入与缓存分离；`cache_creation` 可能是嵌套字典；`thinking_tokens` 是输出子集。
- Codex 的 `totalTokens` 与 `inputTokens + outputTokens` 对齐，证明缓存包含在输入里。

### 3.2 缺口（必须补真实样本，不能靠字段名猜）

| 内核 | 当前本机样本 | 状态 |
|---|---|---|
| Claude Code | ✅ 真实 `trace_events.raw_lines` | 可定稿 |
| CodeBuddy | ✅ 真实 `messages.usage` | 可定稿 |
| Codex app-server | ❌ 只有 `test_adapters.py` 的协议字段 | **待补采** |
| Antigravity | ❌ 只有当前适配器的 `_tokens` 过滤规则 | **待补采** |
| Custom CLI | ❌ 取决于用户 CLI，字段不固定 | **待补采 + 显式配置** |
| DeepSeek Harness | ❌ 只有测试用例 | **待补采** |
| Hermes | ❌ 只有适配器映射表 | **待补采** |

补采建议（拿真实 payload，不落正文）：

```sql
-- 原生 message.usage（按内核分组）
select p.type_id, m.usage, count(*)
from messages m
join conversations c on c.id = m.conversation_id
left join agent_profiles p on p.id = c.agent_profile_id
where m.role = 'assistant' and m.usage is not null
group by p.type_id, m.usage;

-- 账本行（按来源/协议）
select agent_type_id, source, provider, input_tokens, output_tokens,
       cache_read_tokens, cache_creation_tokens, reasoning_tokens, context_tokens
from usage_records order by created_at desc limit 200;

-- 原始 CLI 帧（Codex / Antigravity / custom）
select payload from trace_events
where type = 'response' and payload like '%usage%'
order by created_at desc limit 50;
```

补采后只把 **raw usage** 放进 `backend/tests/fixtures/usage/`，不要把消息正文、密钥带进去。

---

## 4. 归一化设计

### 4.1 协议优先，字段名只兜底

新增一个中性模块，放在 `agents/` 叶子层（`backend/app/agents/metering.py`），
适配器与 runtime 都只依赖这个叶子：

```python
normalize_usage(
    raw: Mapping[str, Any] | None,
    *,
    protocol: str | None = None,      # 见下表；None = 走字段探测兜底
    already_normalized: bool = False, # 明确禁止二次归一化时使用
) -> dict[str, int | None]
```

协议枚举：

| protocol | 来源示例 | 判定 |
|---|---|---|
| `anthropic_messages` | Claude Code、CodeBuddy、Anthropic 网关 | 输入与缓存**分离** |
| `openai_chat` | OpenAI Chat Completions / 兼容端点 | 缓存**包含**在 `prompt_tokens` |
| `openai_responses` | OpenAI Responses、Codex app-server | 缓存**包含**在 `input_tokens` |
| `openai_compat_camel` | 自定义 CLI / 任意 OpenAI 兼容驼峰端点（兜底） | 缓存**包含**在 `inputTokens` |
| `gemini` | Gemini / Antigravity（待确认） | 缓存**包含**在 `promptTokenCount` |
| `deepseek_camel` | DeepSeek Harness（待确认） | 暂按**分离**，上线前用真实样本确认；仅显式协议启用 |
| `unknown` / `None` | 历史、自定义 CLI | 走 4.4 兜底 |

### 4.2 各协议的取数规则

| 标准字段 | `anthropic_messages` | `openai_chat` | `openai_responses` | `openai_compat_camel` | `gemini` | `deepseek_camel`（待确认） |
|---|---|---|---|---|---|---|
| `input_tokens` | `input_tokens` | `prompt_tokens - cache_read` | `input_tokens - cache_read` | `inputTokens - cachedInputTokens` | `promptTokenCount - cachedContentTokenCount` | `inputTokens` |
| `cache_read_tokens` | `cache_read_input_tokens` | `prompt_tokens_details.cached_tokens` / `cached_tokens` | `input_tokens_details.cached_tokens` / `cached_input_tokens` / `cachedInputTokens` | `cachedInputTokens` / `cachedTokens` | `cachedContentTokenCount` | `cacheReadTokens` |
| `cache_creation_tokens` | `cache_creation_input_tokens`；为空则 `cache_creation.ephemeral_1h + ephemeral_5m` | `0` | `0` | `0` | `0` | `cacheWriteTokens` |
| `output_tokens` | `output_tokens` | `completion_tokens` | `output_tokens` | `outputTokens` | `candidatesTokenCount` | `outputTokens` |
| `reasoning_tokens` | `output_tokens_details.thinking_tokens` | `completion_tokens_details.reasoning_tokens` | `output_tokens_details.reasoning_tokens` / `reasoning_output_tokens` | `reasoningOutputTokens` / `reasoningTokens` | `thoughtsTokenCount` | `reasoningTokens` |
| `context_tokens` | `input + cache_read + cache_creation` | `prompt_tokens` | `input_tokens` | `inputTokens` | `promptTokenCount` | `input + cache_read + cache_creation` |
| `total_tokens`（派生） | `context + output` | `prompt_tokens + completion_tokens` | `input_tokens + output_tokens` | `inputTokens + outputTokens` | `promptTokenCount + candidatesTokenCount` | `context + output` |

硬规则：

1. **`cache_write_input_tokens` / `cacheWriteInputTokens` 永远不能单独作为“分离式”判定标记。**
   它只是一个缓存写数值的别名；是否包含在输入里由协议决定。
2. **`total_tokens` 不参与判定**。只用来做一致性校验：
   如果 `total_tokens != context + output`，记一条 warning（附协议与原始键），但**以派生值为准**。
   DeepSeek 的 `totalTokens=7` 只等于 `input+output`，与平台“含缓存的窗口口径”天然不同，属于预期差异。
3. **`reasoning_tokens` 不加进 `total_tokens`**：重复加会虚高。是否为输出子集要按协议确认——
   OpenAI Responses / Gemini / Anthropic thinking 是子集；但 DeepSeek 样例出现
   `reasoningTokens=5 > outputTokens=4`，说明它可能不是子集（或字段口径不同），
   这条在真实样本确认前**不能全局断言**，只记录明细、不参与总量。
4. **任何缺失/`None`/布尔/字符串字段按缺失处理**，不报错；一个字段都认不出时返回全 `None`
   （调用方记 unknown，不假装 0）。

### 4.3 协议从哪里来（不要靠字段猜）

| 场景 | 协议来源 |
|---|---|
| 记录网关 | `ProxyTarget.protocol` + `UsageAccumulator` 实际观察到的响应形状（`message_start` / chat chunk / `response.completed`） |
| OpenAI Harness | `AgentDefinition.protocol` + 实际请求形态（chat vs responses） |
| 原生适配器 | `AgentDefinition.usage_protocol`（新增 `ClassVar`，或 `usage_protocol_for(ctx)`） |
| Custom CLI | 新增配置字段 `usage_style`：`auto`（默认）/ `anthropic` / `openai_chat` / `openai_responses` / `gemini` |
| 历史/回填 | `None`（兜底） |

适配器建议值：

| 适配器 | `usage_protocol` |
|---|---|
| `claude-code` | `anthropic_messages` |
| `codebuddy-cli` | `anthropic_messages` |
| `doubao-cli` | `anthropic_messages`（当前不上报 usage，保留声明） |
| `codex` | `openai_responses` |
| `deepseek-harness` | `deepseek_camel`（待真实样本确认） |
| `openai-harness` | 按请求形态：`openai_chat` / `openai_responses` |
| `hermes` | `unknown`（待补采，禁止猜） |
| `antigravity-cli` | `unknown`（待补采；很可能是 `gemini`） |
| `custom-cli` | 用户配置，默认 `auto` |

### 4.4 `protocol=None` 时的兜底顺序

按顺序命中即停：

1. 有 `prompt_tokens` → `openai_chat`（Anthropic 不会发这个键）。
2. 有 `cache_read_input_tokens` 或 `cache_creation_input_tokens` → `anthropic_messages`。
3. 有 `input_tokens` 且（`input_tokens_details` 里带 `cached_tokens` **或**
   `output_tokens_details` 里带 `reasoning_tokens`）→ `openai_responses`。
   （注意：Anthropic 的 `output_tokens_details` 里是 `thinking_tokens`，不是 `reasoning_tokens`。）
4. 有驼峰 `inputTokens` → `openai_compat_camel`，按包含式处理：`input = inputTokens - cachedInputTokens`。
5. 只有裸 `input_tokens` / `output_tokens` → **默认包含式**（`context = input_tokens`），
   同时如果存在 `cached_input_tokens` / `cached_tokens`，从中扣掉并单列。
6. 一个都认不出 → 全 `None`。

> 为什么第 5 条默认包含式：Anthropic 的分离式一定伴随 `cache_*_input_tokens` 标记；
> 裸 `input_tokens` 更像 OpenAI Responses / Codex。这样不会再把 Codex 的缓存加两遍。

### 4.5 单次窗口 vs 整轮累计：归一化不负责选帧

- `context.usage` 只喂**单次请求**的帧（Claude 的 `message_delta` / Harness 的每步 `prompt_tokens`）。
- 计费 `usage` 喂**整轮累计**帧（Claude 的 `result` / Harness 的 totals）。
- 同一套归一化函数，**由调用方决定喂哪一帧**；不要把 `result` 当窗口，也不要把 `message_delta` 当账单。

---

## 5. 原始字段留痕与数据库变更

### 5.1 新增列（P3 已落地）

`usage_records`：

| 列 | 类型 | 说明 |
|---|---|---|
| `raw_usage` | JSON NULL | 原始 usage payload（不含正文/密钥），上限 16KB；超长时写结构化截断占位对象 |
| `usage_protocol` | VARCHAR(32) NULL | 实际使用的协议，便于复现 |
| `normalizer_version` | VARCHAR(16) NULL | 归一化规则版本（首版 `v1`） |
| `telemetry_source` | VARCHAR(24) NULL | `agent_report`（Agent 自报） / `gateway_proxy`（网关旁路抓取） |
| `response_shape` | VARCHAR(24) NULL | `anthropic_messages` / `chat` / `responses` / `gemini`，区分 Chat Completions 与 Responses |
| `upstream_request_id` | VARCHAR(120) NULL | 上游响应 ID（`req_...` / `chatcmpl-...` / `message.id`），可选唯一键，用于跨来源幂等去重 |

`messages`（P5 已落地）：

| 列 | 类型 | 说明 |
|---|---|---|
| `usage_protocol` | VARCHAR(32) NULL | 这条 usage 快照的协议；前端/导出按同一口径归一化。老行留空 = 字段兜底 |

- 老行全部为 `NULL`：接口返回 `null`，前端显示“—”，不报错。
- 补列方式沿用 `models.py` 的 `ADDITIVE_COLUMNS`，不重建表；
  `init_db` 在 SQLite 老库上额外执行 `CREATE UNIQUE INDEX IF NOT EXISTS`。
  非 SQLite 老库：`create_all` 不会给已存在的表补索引，需要手工执行等价 DDL
  （本项目默认 SQLite，先按这个边界落地）。
- 新行由 `build_record()` 统一写入 `normalizer_version="v1"`；
  老行保持 `NULL`，以此区分“新规则 / 历史行为”。
- `raw_usage` 只存 usage 相关对象；若超过 16KB，**不硬切字符串**，改写结构化占位对象：
  ```json
  {
    "_truncated": true,
    "_original_bytes": 23456,
    "_keys": ["cache_read_input_tokens", "input_tokens", "..."],
    "usage": {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 3}
  }
  ```
  占位对象本身必须是合法 JSON；关键字段按白名单保留，原始大小与顶层键留痕。
- **不回填历史 raw**（拿不到就是拿不到），也不重算历史成本。

### 5.2 适配器从“先归一化”改为“交原始帧”（P4 已落地）

改造前：DeepSeek / Hermes 先翻译字段再发 `usage` 事件；Claude / CodeBuddy 的
`_from_result` 只留数值标量，嵌套的 `cache_creation` / `output_tokens_details`
在适配器里就丢了。

改造后：

- 新增 `usage.raw` 事件 + `events.usage_raw(counters)`：原样上报一份 usage 字典，
  保留嵌套 / 驼峰 / 短字段；与数值计费的 `usage` 事件分开，保持事件协议契约清晰；
- Claude / CodeBuddy：`_from_result` 直接 `yield usage_raw(payload["usage"])`；
- DeepSeek：`yield usage_raw(payload["usage"])`，`context.usage` 用
  `context_tokens(raw, protocol=deepseek_camel)` 计算；
- Hermes：`yield usage_raw(payload["tokens"])`（协议仍 `unknown`，账本 pending 阻塞）；
- 协议仍由 `AgentDefinition.usage_protocol` / `usage_protocol_for(ctx)` 提供，
  事件 payload 不掺协议键，运行层统一归一化。

### 5.3 双轨采集与跨来源幂等去重（架构预留）

平台同时存在两条来源：

| 来源 | `telemetry_source` | 场景 |
|---|---|---|
| Agent 自报 | `agent_report` | 直连模式的 Claude Code / CodeBuddy 等，适配器解析内核原生事件；平台内进程内 Harness 也归这一类（非旁路） |
| 网关旁路 | `gateway_proxy` | 无自报能力的 Agent 走记录网关，从上游响应 tee 出 usage |

**问题**：同一笔请求可能既经过网关、又触发 Agent 自报。如果两条都写账本，就会重复记账。

**设计**：

1. 账本行增加 `telemetry_source` 标记，审计里能看出这笔数从哪来。
2. 增加 `upstream_request_id`（上游响应 ID：Anthropic `message.id` / OpenAI `resp_...` /
   Chat `chatcmpl-...`）。提取优先级：
   - 响应体顶层 `id`
   - `message.id` / `response.id`
   - 网关 `UsageAccumulator.request_id` 已解析的值
   - 都没有才回退到 `<run_id>:<seq>`（现有机制）
3. **幂等去重键**：
   ```
   upstream_request_id（唯一索引）  当上游 ID 存在时
   (run_id, source, request_id)     始终保留，作为老机制 / 无上游 ID 时的兜底
   ```
   同一笔请求先到先写；后到的 `INSERT ... ON CONFLICT DO NOTHING` 忽略。
4. 权威源优先级保持不变：`gateway_proxy` > `agent_report`（网关是一手 tee，Agent 自报是二手）。
   若先写的是 `agent_report`、后到 `gateway_proxy`，`_promote_to_gateway()` 按
   `upstream_request_id` 找到原行，把 token / 来源 / 协议 / raw / 成本换成网关一手数据；
   身份与时间列（`id` / `run_id` / `request_id` / `created_at`）保持第一次观察到的值。
   **护栏**：网关一个计费计数都没解析出来时，不拿空值覆盖自报的有效数据。
5. 没有上游 ID 的老内核 / 老数据：继续用现有 `(run_id, source, request_id)`，行为不变；
   新唯一索引允许多个 `NULL`，老行不参与跨来源去重。
6. **粒度说明（重要）**：当前只有网关写**逐请求行**并填 `upstream_request_id`；
   native / harness 仍是**一个 run 一行**的聚合行（`request_id=run:native`），
   **不填**这个键——因为拿某个单请求的 ID 去撞整轮聚合行，会把“整轮累计”误覆盖成
   “单次请求”。今天的双记保护仍由 `resolve_authority()`（gateway > native）负责；
   等未来 native 改成逐请求入账，再填 `upstream_request_id`，唯一索引与
   `_promote_to_gateway()` 已经就位。

> **P3 已落地**：表结构、唯一索引、`record()` 的冲突处理与
> `_promote_to_gateway()` 都已接好。老行 `upstream_request_id=NULL` 不参与新唯一约束
> （SQLite / PostgreSQL 唯一索引允许多个 NULL）；身份 / 时间列保持第一次观察到的值，
> 只把 token、来源、协议、成本等字段换成网关一手数据。

---

## 6. 成本 / 计费解耦

### 6.1 现状

`usage_ledger._cost_from_rates()`（`usage_ledger.py:380`）只吃四个计费字段：

```
input_tokens × 输入单价
+ output_tokens × 输出单价
+ cache_read_tokens × 缓存读单价
+ cache_creation_tokens × 缓存写单价
```

`context_tokens` 与 `reasoning_tokens` **不参与**成本。这本身就是解耦的，本次不改公式。

### 6.2 本次调整对成本的影响

- Anthropic / OpenAI Chat / OpenAI Responses：四个计费字段语义不变 → **成本不变**。
- Codex：修复“缓存重复计”后，`input_tokens` 会从 5 变 4、`cache_read_tokens` 保持 1；
  即从“6 个 token 计费”回到“5 个 token 计费”。**这是修正，不是回归**。
  - **已拍板：只影响新写入的账本行；历史行保持原值，不重算。**
  - 新老行的区分靠 `normalizer_version`（老行 NULL / `v0`，新行 `v1`）。
- 推理 token：当前按**只记明细、不重复计费**处理。为兼容不同厂商，协议级配置项
  `reasoning_in_output` 预留如下：

  | 协议 | `reasoning_in_output` | 含义 |
  |---|---|---|
  | `anthropic_messages` | `True` | `thinking_tokens` 是输出子集 |
  | `openai_chat` | `True` | `completion_tokens_details.reasoning_tokens` 是输出子集 |
  | `openai_responses` | `True` | `output_tokens_details.reasoning_tokens` 是输出子集 |
  | `openai_compat_camel` | `True` | 兼容端点默认按子集处理 |
  | `gemini` | `True` | `thoughtsTokenCount` 是输出子集 |
  | `deepseek_camel` | `False` | 样例 `reasoningTokens=5 > outputTokens=4`，暂按“独立”处理，待真实样本确认 |
  | `unknown` | `None` | 未知，不参与计费推断 |

  计费层后续按这个开关决定“是否把 reasoning 再加一笔”；默认仍不加。
  若未来要单独计价，必须新增 `reasoning_per_million`，不能偷偷并进输出。

### 6.3 成本 golden test

每个 fixture 配一张固定费率表，断言：

1. 归一化后的四个计费字段与期望一致；
2. `_cost_from_rates()` 结果与 golden 值一致；
3. 本次改动前后，Anthropic/OpenAI 样例的成本金额**逐位相等**。

---

## 7. 前端统一

### 7.1 已做（本轮）

- 审计表补 `缓存读` / `缓存写` 两列（`ExternalApiPanel.tsx`）。
- 「输入」「窗口」加 tooltip 澄清口径。

### 7.2 P5 已落地

1. **下方「本对话消耗」**
   - 「读取」已改为 **「输入」**，tooltip 写明“普通输入，不含缓存”；
   - 缓存读 / 缓存写单列；「总计」= 窗口占用（输入含缓存）+ 输出；
   - 整条用量栏 tooltip：“累计消耗 ≠ 当前上下文窗口”；
   - 移动端同步改为「输入」，两栏网格保持不变。
2. **`summarizeUsage()`**
   - 新增 `normalizeUsage(raw, protocol?)`：有 `Message.usage_protocol` 时按协议算，
     为空时按后端同一顺序兜底：
     `prompt_tokens → Anthropic 缓存标记 → Gemini → input_tokens →
     驼峰 inputTokens → Hermes 短字段`；
   - `deepseek_camel` 按“分离式”算（input 不扣缓存），不会像 auto 那样误判成包含式；
   - 流式那一轮还没落库、没有可信的协议快照：走字段兜底；
     落库后以消息上的 `usage_protocol` 为准（前端不按 `type_id` 硬编码协议）；
   - 「输入」= 普通输入（不含缓存）；
   - 不再用 `total_tokens` 猜分项（与后端一致）。
3. **用量页旧消息段**
   - `api/usage.py._accumulate()` 改调 `usage_ledger.normalize_usage()`；
     删掉第三套字段表，旧消息与账本共用同一口径。
4. **缓存命中率分母**
   - 统一为 `input + cache_read + cache_creation`（input 已不含缓存）；
   - 前端不再自行判断记法。

---

## 8. 单测矩阵（fixture 已落盘）

fixture 目录：`backend/tests/fixtures/usage/`；清单：`manifest.json`（含来源与期望值）。

| # | fixture | protocol | 覆盖点 | 期望要点 |
|---|---|---|---|---|
| T1 | `anthropic_message_delta_real.json` | `anthropic_messages` | 真实单次窗口 | input 40683 / cache 512 / context 41195 |
| T2 | `anthropic_result_thinking_real.json` | `anthropic_messages` | thinking / 嵌套 cache_creation | reasoning 80；context 14167 |
| T3 | `anthropic_cache_creation_nested.json` | `anthropic_messages` | 嵌套缓存写回退 | cache_creation 500；context 1500 |
| T4 | `codebuddy_result_usage_real.json` | `anthropic_messages` | 真实整轮累计 | cache_read 246272；context 262757 |
| T5 | `openai_chat_cached.json` | `openai_chat` | 包含式 | input 700 / cache 300 / context 1000 |
| T6 | `openai_responses_reasoning.json` | `openai_responses` | 包含式 + 推理 | input 600 / reasoning 20 / context 800 |
| T7 | `codex_app_server_snake.json` | `openai_responses` | **缓存重复计回归** | input 4 / cache 1 / context 5 / total 11 |
| T8 | `codex_app_server_camel.json` | `openai_responses` | 驼峰字段 | 同 T7 |
| T9 | `custom_cli_camel.json` | `None` | 驼峰兜底 | input 600 / cache 400 / context 1000 |
| T10 | `deepseek_step_end.json` | `deepseek_camel` | 待确认的分离式 | context 6；provider total 7 ≠ 派生 10 |
| T11 | `gemini_usage_metadata.json` | `gemini` | Gemini 包含式 | input 700 / cache 300 / context 1000 |
| T12 | `legacy_anthropic_no_cache.json` | `anthropic_messages` | 历史缺缓存字段 | cache 0；不报错 |
| T13 | `legacy_openai_bare.json` | `None` | 历史裸 prompt_tokens | input 42 / context 42 |
| T14 | 空 / `None` / 字符串 / `{}` | 任意 | 无计数 | 全 `None`，不是 0 |
| T15 | `{"input_tokens": true}` | 任意 | 布尔过滤 | 全 `None` |
| T16 | `{"prompt_tokens":1000,"cached_tokens":300,"cache_read_input_tokens":50}` | `None` | 冲突字段 | `prompt_tokens` 优先 → openai_chat |
| T17 | `{"input_tokens":5,"cached_input_tokens":1,"cache_write_input_tokens":0}` | `None` | 兜底不得误判 | input 4 / cache 1 / context 5 |
| T18 | 已归一化字典 | `already_normalized=True` | 防二次归一化 | 原样返回 |
| T19 | 老账本行（新列全 NULL） | — | 历史兼容 | API 返回 null，不报错 |
| T20 | 成本 golden | — | 四个计费字段不变 | 金额逐位相等（Codex 除外，按修复后） |
| T21 | 全 fixture | — | 不变量 | `context = input + cache_read + cache_creation`；`total = context + output`；`reasoning ≤ output` **只在已知子集协议**（openai_responses / gemini / anthropic thinking）断言，DeepSeek 记为告警不阻断 |
| T22 | 网关三种响应形状 | 形状探测 | `message_start` / chat chunk / `response.completed` | 选对协议 |
| T23 | Codex raw + `protocol=anthropic_messages` | 协议优先 | 错误协议不得被字段自动纠正 | 按调用方传的协议算（并在测试里标注这是误用） |
| T24 | `antigravity_result_stats_pending.json` / `hermes_result_tokens_pending.json` | `unknown` | **阻塞** | 真实样本补采后再补期望值 |
| T25 | 超过 16KB 的 raw_usage | — | 结构化截断 | `_truncated=true`、`_original_bytes`、`_keys`、`usage` 白名单；占位对象可 JSON 序列化 |
| T26 | `rules_for()` | 各协议 | 计费开关 | `reasoning_in_output` 按协议表返回；DeepSeek 为 `False`，unknown 为 `None` |
| T27 | 上游响应 ID | — | 双轨幂等预留 | `req_...` / `chatcmpl-...` 清洗后 ≤120 字符；非法值返回 `None` |
| T28 | 响应形状常量 | — | Chat vs Responses | `anthropic_messages / chat / responses / gemini` 常量齐全 |
| T29 | `usage_records` 老行 | — | 双轨/留痕历史兼容 | `telemetry_source` / `response_shape` / `upstream_request_id` 为 NULL 时不报错、不参与新唯一约束 |

> T22 建议做成 `pytest.mark.parametrize`；T24 用 `pytest.mark.xfail(strict=True)` 或 `skip`，
> 避免“没确认就上线”。

---

## 9. 兼容与迁移

1. 新列可空，老数据不迁移、不报错。
2. `normalize_usage` 保持返回原来的 6 个键，新增 `protocol`/`total` 通过独立函数或 dataclass 提供，
   避免现有 `counts["input_tokens"]` 调用点全改。
3. `raw_usage` 只在账本写入时存；消息侧继续用 `Message.usage` 做展示快照。
4. 历史成本不重算；Codex 修复只对新行生效（待你确认）。
5. 前端对 `raw_usage` / `usage_protocol` 全部按可选字段处理。

---

## 10. 实施顺序（确认后）

1. **P1 归一化核心（本轮）**：新增 `backend/app/agents/metering.py` + 单测（T1–T21、T25–T28）。
   **不改任何现有调用点**；旧函数行为保持不变，确保线上零行为变化。
   同时落 `reasoning_in_output` 协议规则、结构化截断 helper、遥测常量与上游 ID 清洗函数。
2. **P2 协议接线（已交付）**：网关/harness/原生适配器传 `protocol`；旧函数改为薄包装
   （行为切换点）；补 T22–T23。
   - `usage_ledger.normalize_usage()` / `window_tokens()` → 调 `app.agents.metering`，只保留原 6 键；
   - `agents/builtin/util.context_tokens()` → 同一核心的薄包装，适配器按协议传参；
   - `AgentDefinition.usage_protocol` + `usage_protocol_for(ctx)`；Custom CLI 新增
     `usage_style`（auto / anthropic / openai_chat / openai_responses / gemini / deepseek）；
   - `UsageAccumulator.response_shape` 从实际响应识别 `anthropic_messages / chat / responses`，
     `persist_usage` 用它选协议。
3. **P3 原始留痕与双轨（已交付）**：加 `raw_usage` / `usage_protocol` /
   `normalizer_version` / `telemetry_source` / `response_shape` /
   `upstream_request_id` 列；唯一索引与“网关后到覆盖自报”的幂等逻辑；补 T19、T29。
   - `build_record()` 统一截断 raw_usage、推导 telemetry_source / response_shape；
   - `record()` 任一唯一约束冲突后调 `_promote_to_gateway()`；
   - `record_native_snapshot_isolated()` 在创建 / 更新两处都写留痕与协议；
   - 网关 `persist_usage()` 写入 raw / protocol / shape / upstream_id；
   - `api/usage._record_to_dict()` 与前端 `UsageRecordRow` 增加对应可选字段。
4. **P4 适配器改发原始帧（已交付）**：新增 `usage.raw` 事件 + `events.usage_raw()`；
   Claude / CodeBuddy 的 `_from_result` 保留完整 usage；
   DeepSeek / Hermes 直接上报原始 `usage` / `tokens`；
   Custom CLI 的 `usage_style` 已在 P2 落地。
5. **P5 前端对齐（已交付）**：`Message` 增加 `usage_protocol` 快照；
   `summarizeUsage` 新增 `normalizeUsage(raw, protocol?)` 并按后端规则识别；
   下方消耗 / 移动端 / 协作面板统一为「输入（不含缓存）」；用量页旧消息段改调
   `usage_ledger.normalize_usage()`；缓存命中率分母统一。
6. **P6 清理**：`util.context_tokens` / `usage_ledger.normalize_usage` / `api/usage._accumulate`
   的重复字段表已删除；前端仍保留一份展示用归一化（无协议上下文字段，只能按字段兜底），
   后续若要彻底单一实现，可让消息序列化直接下发 `usage_normalized`。

---

## 11. 已拍板结论与剩余风险

### 已拍板

1. **Codex 历史账**：**不重算历史，只修新行**；靠 `normalizer_version` 区分新老版本。
2. **未采到样本的协议**：全部保持 `pending` 阻塞，拿到真实 `raw_usage` 验证后再放开。
3. **Custom CLI 协议**：默认 `auto` + UI 支持手动覆盖。
4. **推理 token**：当前只记明细、不重复计费；计费层新增协议级 `reasoning_in_output`
   配置项（见 6.2），为后续厂商差异留口子。
5. **raw_usage 体积**：16KB 上限；超长写结构化截断占位对象，不硬切字符串。
6. **网关响应形状**：新增 `response_shape`（`chat` / `responses` / `anthropic_messages` / `gemini`）。
7. **双轨采集**：新增 `telemetry_source`（`agent_report` / `gateway_proxy`）与
   `upstream_request_id`，按上游响应 ID 幂等去重；网关一手优先。

### 剩余风险

1. **Antigravity / Hermes / DeepSeek / Custom CLI 真实样本缺口**：相关规则 `pending`，
   在补采前不放开主链路；这是已知阻塞，不是遗漏。
2. **DeepSeek reasoning > output**：样例显示反思 token 可能不是输出子集，暂按
   `reasoning_in_output=False`，真实样本确认后再调整。
3. **双轨去重边界**：没有上游响应 ID 的内核仍走 `(run_id, source, request_id)`；
   可能出现“同一请求两个 request_id”的老式重复，需要在线上观察数据再决定是否回填。
4. **上游 ID 复用风险**：`upstream_request_id` 全局唯一依赖供应商不复用响应 ID。
   若发现某些中转层跨 run 复用，需把唯一键降级为 `(upstream_request_id, run_id)`
   或加时间窗；P3 先按全局唯一落地，观察后再调。

---

## 12. 本轮已落盘的文件

- `backend/app/agents/metering.py`（P1 归一化核心：协议优先、字段兜底、结构化截断、遥测常量）
- `backend/tests/test_usage_normalize.py`（P1 单测：66 passed）
- `backend/tests/test_usage_wiring.py`（P2 接线单测：22 passed）
- `backend/tests/test_usage_ledger_dedup.py`（P3 双轨去重单测：9 passed）
- `backend/tests/fixtures/usage/*.json`（15 个原始 payload）
- `backend/tests/fixtures/usage/manifest.json`（来源、协议、期望值、阻塞项）
- `backend/app/runtime/usage_ledger.py`（薄包装 + 原始留痕 + 双轨幂等去重）
- `backend/app/runtime/recording_gateway.py`（`response_shape` 识别 + 协议/raw/upstream_id 传参）
- `backend/app/runtime/service.py`（收尾按适配器协议归一化 + raw/协议落账 +
  `Message.usage_protocol` 快照）
- `backend/app/models.py`（`UsageRecord` 6 个新列 + `upstream_request_id` 唯一索引；
  `Message.usage_protocol`）
- `backend/app/schemas.py`（`MessageRead.usage_protocol`）
- `backend/app/db.py`（老 SQLite 库补唯一索引）
- `backend/app/api/usage.py`（审计明细返回新字段）
- `backend/app/agents/base.py`（`usage_protocol` / `usage_protocol_for`）
- `backend/app/agents/events.py`（`usage_raw()`：原样上报 usage）
- `backend/app/agents/builtin/{claude_code,codebuddy_cli,deepseek_harness,hermes_agent}.py`
  （P4：原样上报，不再提前丢嵌套 / 翻译键名）
- `backend/app/agents/builtin/{codex,openai_harness,doubao_cli,custom_cli}.py`
  （协议声明；Custom CLI `usage_style`）
- `backend/app/agents/builtin/util.py`（`context_tokens` 薄包装）
- `frontend/src/lib/types.ts` / `frontend/src/components/ExternalApiPanel.tsx`
  （审计字段类型 + 来源 pill tooltip + 缓存列）
- `frontend/src/lib/usage.ts`（P5：`normalizeUsage()` 与后端同一口径）
- `frontend/src/pages/ChatPage.tsx` / `frontend/src/mobile/pages/MobileChatPage.tsx` /
  `frontend/src/components/CollabRunsPanel.tsx` / `frontend/src/lib/conversationExport.ts`
  （「输入」标签、缓存读写分列、tooltip）
- `docs/usage-normalization-design.md`（本文件）

### P1–P5 验证结果

```text
P1 新单测 tests/test_usage_normalize.py                 66 passed
P2 新单测 tests/test_usage_wiring.py                    22 passed
P3 新单测 tests/test_usage_ledger_dedup.py               9 passed
P4/P5 后 usage / adapter / API 相关                      319 passed
全量后端首轮                                           1120 passed, 1 failed（协作事件守卫未同步 usage.raw，已修）, 1 skipped
全量后端复跑（scripts/test.sh）                          1121 passed, 1 skipped
前端 typecheck / npm run build                          通过
前端 normalizeUsage / summarizeUsage 对 13 个 fixture    13/13 逐字段一致
```

P1 未改任何现有调用点；P2 完成接线与行为切换；P3 完成留痕与双轨；
P4 完成适配器原始帧；P5 完成前端与旧消息段口径统一：

- `usage_ledger.normalize_usage()` / `window_tokens()` 改为薄包装，保留原 6 键；
- `agents/builtin/util.context_tokens()` 改为薄包装，适配器按协议传 `protocol`；
- 适配器新增 `usage_protocol` / `usage_protocol_for(ctx)`；Custom CLI 新增 `usage_style`；
- 网关 `UsageAccumulator.response_shape` 按实际响应识别协议；
- `usage_records` 新增 `raw_usage` / `usage_protocol` / `normalizer_version` /
  `telemetry_source` / `response_shape` / `upstream_request_id`；
- `upstream_request_id` 唯一索引 + `_promote_to_gateway()` 实现跨来源幂等；
- Claude / CodeBuddy / DeepSeek / Hermes 改为 `usage.raw` 原样上报；
- `api/usage.py._accumulate()` 与前端 `summarizeUsage()` 都改用同一归一化口径。

**历史账不重算**：只影响新写入的账本行；老行保持原值，
`normalizer_version=NULL`（老）/ `v1`（新）用于区分。

---

## 13. 编码规范复核（收尾）

按 `AGENTS.md` 铁律与 `docs/ai-coding-standard.md` 逐条复核后，额外修正了三处：

1. **铁律 1（前端不得按 `type_id` 写分支）**：
   删除了前端 `AGENT_TYPE_PROTOCOLS` / `protocolForAgentType` 映射；
   流式那一轮没有协议快照时走字段兜底，落库后以 `Message.usage_protocol` 为准。
2. **铁律 2（适配器是叶子）**：
   归一化核心从 `backend/app/metering/usage.py` 移到
   `backend/app/agents/metering.py`；适配器只 `from ..metering import ...`，
   runtime 从 `app.agents.metering` 导入；架构守卫把该文件纳入检查。
   同时把 `agents/metering.py` 补进 `AGENTS.md` 的适配器允许依赖清单与分层树。`
3. **事件协议（铁律 5）**：
   新增独立事件 `usage.raw` 承载原始小票，`usage` 保持“任意数值键”的计费契约；
   已同步 `events.py` → `service.consume_run` → 前端 `types.ts` / `applyEvent`
   → `docs/architecture.md` / `docs/ai-coding-standard.md` / `test_architecture.py`；
   另同步了硬编码事件集合的协作守卫 `tests/test_collab_architecture.py`。
   前端 `usage` 类型同步改为 `Record<string, unknown>`（原始小票可能含嵌套）。

验证：
- `scripts/test.sh`（pytest + 前端 typecheck）复跑全绿：**1121 passed, 1 skipped**；
- `npm run build` 通过；
- 在**现有 `backend/data/coda.db` 的快照副本**上跑 `init_db`：20 个对话 / 128 条消息 /
  12 行账本数量不变，老行新列全为 NULL，唯一索引创建成功，历史对话与消息可读。