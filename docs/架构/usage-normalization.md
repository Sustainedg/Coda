# 用量归一化（架构说明）

> 本文说明平台如何把不同内核、不同协议上报的 token 用量，收敛成一套可比较、可计费、
> 可审计的口径。实现以 `backend/app/agents/metering.py` 为准；本文与代码冲突时以代码为准。
>
> 相关文档：`docs/功能文档/usage-gateway.md`（记录网关与账本）、
> `docs/功能文档/上下文窗口与自动压缩.md`（上下文窗口口径）。

---

## 1. 目标与非目标

### 1.1 目标

- **一套归一化核心**：适配器、运行层、网关、前端都指向同一份取数规则；
- **协议优先**：先知道「这是哪种 wire 协议」，再按协议取字段，不靠字段名猜；
- **原始留痕**：账本行保留原始 usage、协议、响应形状与来源，事后能复现；
- **跨来源幂等**：Agent 自报与网关旁路同时存在时，不重复记账；
- **缺失不伪造**：认不出的字段返回 `None`，调用方记 `unknown`，不写 0。

### 1.2 非目标

- 不统一各家「total_tokens」的含义，只做一致性校验；
- 不把 `reasoning_tokens` 加进总量（避免虚高）；
- 不回填历史 raw，也不重算历史成本；
- 不做跨协议代理翻译。

---

## 2. 协议优先，字段兜底

核心入口：

```python
normalize_usage(
    raw: Mapping[str, Any] | None,
    *,
    protocol: str | None = None,
    already_normalized: bool = False,
) -> dict[str, int | None]
```

协议枚举：

| protocol | 典型来源 | 缓存与输入的关系 |
| --- | --- | --- |
| `anthropic_messages` | Claude Code、CodeBuddy、Anthropic 网关 | 输入与缓存**分离** |
| `openai_chat` | OpenAI Chat Completions / 兼容端点 | 缓存**包含**在 `prompt_tokens` |
| `openai_responses` | OpenAI Responses、Codex app-server | 缓存**包含**在 `input_tokens` |
| `openai_compat_camel` | 自定义 CLI / 驼峰 OpenAI 兼容端点 | 缓存**包含**在 `inputTokens` |
| `gemini` | Gemini / Antigravity | 缓存**包含**在 `promptTokenCount` |
| `deepseek_camel` | DeepSeek Harness | 暂按**分离**，仅显式协议启用 |
| `unknown` / `None` | 历史数据、自定义 CLI | 走字段兜底 |

### 2.1 各协议取数规则

| 标准字段 | `anthropic_messages` | `openai_chat` | `openai_responses` | `openai_compat_camel` | `gemini` | `deepseek_camel` |
| --- | --- | --- | --- | --- | --- | --- |
| `input_tokens` | `input_tokens` | `prompt_tokens - cache_read` | `input_tokens - cache_read` | `inputTokens - cachedInputTokens` | `promptTokenCount - cachedContentTokenCount` | `inputTokens` |
| `cache_read_tokens` | `cache_read_input_tokens` | `prompt_tokens_details.cached_tokens` / `cached_tokens` | `input_tokens_details.cached_tokens` / `cached_input_tokens` / `cachedInputTokens` | `cachedInputTokens` / `cachedTokens` | `cachedContentTokenCount` | `cacheReadTokens` |
| `cache_creation_tokens` | `cache_creation_input_tokens`；为空则 `cache_creation.ephemeral_1h + ephemeral_5m` | `0` | `0` | `0` | `0` | `cacheWriteTokens` |
| `output_tokens` | `output_tokens` | `completion_tokens` | `output_tokens` | `outputTokens` | `candidatesTokenCount` | `outputTokens` |
| `reasoning_tokens` | `output_tokens_details.thinking_tokens` | `completion_tokens_details.reasoning_tokens` | `output_tokens_details.reasoning_tokens` / `reasoning_output_tokens` | `reasoningOutputTokens` / `reasoningTokens` | `thoughtsTokenCount` | `reasoningTokens` |
| `context_tokens` | `input + cache_read + cache_creation` | `prompt_tokens` | `input_tokens` | `inputTokens` | `promptTokenCount` | `input + cache_read + cache_creation` |
| `total_tokens`（派生） | `context + output` | `prompt_tokens + completion_tokens` | `input_tokens + output_tokens` | `inputTokens + outputTokens` | `promptTokenCount + candidatesTokenCount` | `context + output` |

### 2.2 硬规则

1. `cache_write_input_tokens` / `cacheWriteInputTokens` **不能单独作为「分离式」判定标记**；
   它只是缓存写的别名，是否包含在输入里由协议决定。
2. `total_tokens` 不参与判定，只做一致性校验；与派生值不一致时记 warning，但**以派生值为准**。
3. `reasoning_tokens` 不加入 `total_tokens`；是否为输出子集按协议确认，未确认前只记明细。
4. 缺失、`None`、布尔、字符串一律按缺失处理；一个字段都认不出时返回全 `None`。

### 2.3 协议从哪里来

| 场景 | 协议来源 |
| --- | --- |
| 记录网关 | `ProxyTarget.protocol` + `UsageAccumulator` 实际观察到的响应形状 |
| OpenAI Harness | `AgentDefinition.protocol` + 实际请求形态（chat / responses） |
| 原生适配器 | `AgentDefinition.usage_protocol` 或 `usage_protocol_for(ctx)` |
| Custom CLI | 配置字段 `usage_style`：`auto` / `anthropic` / `openai_chat` / `openai_responses` / `gemini` |
| 历史 / 回填 | `None`，走兜底 |

推荐声明：

| 适配器 | `usage_protocol` |
| --- | --- |
| `claude-code` | `anthropic_messages` |
| `codebuddy-cli` | `anthropic_messages` |
| `doubao-cli` | `anthropic_messages`（当前不上报 usage，保留声明） |
| `codex` | `openai_responses` |
| `deepseek-harness` | `deepseek_camel` |
| `openai-harness` | 按请求形态：`openai_chat` / `openai_responses` |
| `hermes` | `unknown` |
| `antigravity-cli` | `unknown` |
| `custom-cli` | 用户配置，默认 `auto` |

### 2.4 `protocol=None` 的兜底顺序

按顺序命中即停：

1. 有 `prompt_tokens` → `openai_chat`；
2. 有 `cache_read_input_tokens` 或 `cache_creation_input_tokens` → `anthropic_messages`；
3. 有 `input_tokens` 且带 `cached_tokens` 或 `reasoning_tokens` → `openai_responses`；
4. 有驼峰 `inputTokens` → `openai_compat_camel`，按包含式扣缓存；
5. 只有裸 `input_tokens` / `output_tokens` → 默认包含式；
6. 一个都认不出 → 全 `None`。

> 第 5 条默认包含式：Anthropic 的分离式一定伴随 `cache_*_input_tokens` 标记；
> 裸 `input_tokens` 更像 OpenAI Responses / Codex，默认包含可避免缓存加两遍。

### 2.5 单次窗口 vs 整轮累计

归一化函数不负责选帧，由调用方决定：

- `context.usage` 只喂**单次请求**的帧（Claude 的 `message_delta`、Harness 的每步
  `prompt_tokens`）；
- 计费 `usage` 喂**整轮累计**帧（Claude 的 `result`、Harness 的 totals）。

同一套函数，两种用途；不要把 `result` 当窗口，也不要把 `message_delta` 当账单。

---

## 3. 原始留痕与数据模型

### 3.1 `usage_records` 新增列

| 列 | 说明 |
| --- | --- |
| `raw_usage` | 原始 usage payload（不含正文 / 密钥），上限 16KB；超长写结构化截断占位对象 |
| `usage_protocol` | 实际使用的协议 |
| `normalizer_version` | 归一化规则版本，首版 `v1` |
| `telemetry_source` | `agent_report`（Agent 自报）/ `gateway_proxy`（网关旁路） |
| `response_shape` | `anthropic_messages` / `chat` / `responses` / `gemini` |
| `upstream_request_id` | 上游响应 id（`req_...` / `chatcmpl-...` / `message.id`），用于跨来源幂等 |

`raw_usage` 超过 16KB 时不硬切字符串，改写结构化占位对象，保留关键字段、原始大小与顶层键：

```json
{
  "_truncated": true,
  "_original_bytes": 23456,
  "_keys": ["cache_read_input_tokens", "input_tokens", "..."],
  "usage": {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 3}
}
```

### 3.2 `messages.usage_protocol`

消息行的 usage 快照也记录协议，前端 / 导出按同一口径归一化。老行为 `NULL`，
接口返回 `null`，前端显示「—」，不报错。

补列沿用 `ADDITIVE_COLUMNS`；SQLite 老库额外执行
`CREATE UNIQUE INDEX IF NOT EXISTS`。非 SQLite 老库需要手工执行等价 DDL。

### 3.3 适配器改交原始帧

改造前，部分适配器先翻译字段再发 `usage` 事件，嵌套的 `cache_creation` /
`output_tokens_details` 在适配器里就丢了。现在：

- 新增 `usage.raw` 事件，原样上报一份 usage 字典，保留嵌套 / 驼峰 / 短字段；
- 数值计费的 `usage` 事件保留，协议不写进事件 payload；
- Claude / CodeBuddy / DeepSeek / Hermes 直接上报原始 usage；
- 运行层统一归一化，适配器不各自维护一套字段表。

---

## 4. 双轨采集与跨来源幂等

平台同时存在两条来源：

| 来源 | `telemetry_source` | 场景 |
| --- | --- | --- |
| Agent 自报 | `agent_report` | 直连模式的 Claude Code / CodeBuddy；进程内 Harness |
| 网关旁路 | `gateway_proxy` | 无自报能力的 Agent 走记录网关，从上游响应 tee |

同一笔请求可能既经过网关、又触发 Agent 自报，必须去重：

1. 账本行带 `telemetry_source`，审计能看出数据来源；
2. 带 `upstream_request_id`：提取优先级为响应体顶层 `id` → `message.id` / `response.id`
   → 网关 `UsageAccumulator.request_id` → 回退 `<run_id>:<seq>`；
3. 幂等键：
   - 有上游 ID 时用 `upstream_request_id` 唯一索引；
   - 始终保留 `(run_id, source, request_id)` 作为兜底；
4. 权威源优先级：`gateway_proxy` > `agent_report`。若先写自报、后到网关，
   `_promote_to_gateway()` 按 `upstream_request_id` 找到原行，把 token / 来源 / 协议 /
   raw / 成本换成网关一手数据；身份与时间列保持第一次观察值；
5. 护栏：网关一个计费计数都没解析出来时，不拿空值覆盖自报的有效数据；
6. 当前只有网关写逐请求行并填 `upstream_request_id`；native / harness 仍是每 run 一行的
   聚合行（`request_id=run:native`），**不填**该键，避免把整轮累计误覆盖成单次请求。
   今天的双记保护仍由 `resolve_authority()`（gateway > native）负责。

---

## 5. 成本与计费解耦

- 归一化只负责 token 口径，不负责价格；
- 成本由 `runtime/usage_ledger.apply_cost` 在写入后计算，只对新行生效，不重算历史；
- 价格优先级：**手工价 > `models.dev` 目录价 > unknown**；
- `UsageRecord.cost_pricing` 记录依据（手工 / 目录 / unknown）；
- 用量页返回 `cost_usd` / `cost_coverage`（reported / estimated / unknown 行数）。

---

## 6. 前端口径

- `frontend/src/lib/usage.ts` 的 `summarizeUsage()` 与后端 `agents.metering` 对齐；
- 消息行的 `usage_protocol` 一并下发；老消息没有协议时走字段兜底；
- 用量页与上下文面板展示同一套「输入 / 缓存读 / 缓存写 / 输出 / 推理」字段，
  不各自另算一套。

---

## 7. 测试与边界

### 7.1 测试

- `backend/tests/test_usage_normalize.py`：各协议取数、兜底顺序、缓存关系、缺失字段；
- `backend/tests/test_usage_wiring.py`：适配器 / 网关接线与协议传递；
- `backend/tests/test_usage_ledger_dedup.py`：跨来源去重与权威源升级；
- `backend/tests/test_usage.py`：用量接口与成本展示。

### 7.2 已知边界

- `deepseek_camel` 仍待真实样本确认是否分离式；未确认前只按显式协议启用；
- `reasoning_tokens` 是否为输出子集因协议而异，不全局断言；
- 历史 raw 不回填，历史成本不重算；
- 非 SQLite 老库的唯一索引需要手工迁移；
- native / harness 仍是每 run 一行的聚合行，逐请求去重要等未来改造。