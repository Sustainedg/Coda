# 上下文计量与记录网关（功能文档）

> 状态：已实现（P0–P3 + 原生实时入账 + 原生压缩配置 + models.dev 目录价）
> 更新日期：2026-11-27
> 关联文档：`docs/architecture.md` §4.4 / §4.5、`docs/context-usage.md`、
> `docs/功能文档/上下文窗口与自动压缩.md`

本文是「计量 / 记录网关 / 外部 API / 成本」这条链路的**现状说明书**：已经做了什么、口径是
什么、怎么配置、还没做什么。设计过程中的取舍见 `docs/context-usage.md` 与代码注释。

---

## 1. 解决什么问题

改造前，平台只能看到**内核上报的二手数据**：CLI 拿 `base_url + api_key` 直连上游，平台
等 `stream-json` / `result` 事件上报 token。于是：

- 内核不报 → 只能按内容估算，窗口数字不准；
- 用量读 `messages.usage`，删对话 / 删消息用量就消失；
- 同一请求多次上报 / 重试无法请求级去重；
- 没有成本口径，也没有对外模型入口。

现在的目标是：**平台自己过磅（一手账本）+ 原生解析兜底 + 外部 API 计量**，并且**不重复记账**。

---

## 2. 总体架构

```text
[scoped] CLI ──► 本机记录网关 ──► 上游（同协议直通，平台注入密钥）
                    │ 响应终态读 usage
                    ▼
              usage_records（单一账本）
                    ▲
[native] 适配器解析内核事件 ──┘（运行中实时更新同一行）
[external] 外部客户端 ──► /api/model-gateway ──┘（origin=external, api_key_id）

用量页 / 上下文面板 / 审计 / 成本 / 配额 都读这个账本
```

三条链路的权威边界：

| 形态 | 请求走向 | 计量来源 |
| --- | --- | --- |
| **记录网关（scoped）** | CLI → 本机代理 → 上游 | 代理在响应终态读上游 `usage`（一手） |
| **原生（global）** | CLI 直连上游 | 适配器解析内核事件（二手兜底）；运行中实时入账 |
| **进程内 Harness** | 平台自己的 HTTP 客户端 | 本身就是一手（`source=harness`） |
| **外部 API** | 外部客户端 → `/api/model-gateway` | 代理读上游 `usage`，`origin=external` |

**互斥不双记**：一次 run 收尾按 `gateway > native > harness > none` 判权。已有 gateway 行时，
原生 `usage` 只做展示、不再入账；没有才把原生累计补进本 run 的唯一那一行；两边都没有就记
`unknown`，**不伪造 0**。

---

## 3. 计量口径

### 3.1 token 归一化

`runtime/usage_ledger.normalize_usage()` 把各家协议归一成六个字段：

| 协议 | 输入侧记法 | 窗口占用（`context_tokens`） |
| --- | --- | --- |
| Anthropic Messages | `input_tokens` 与缓存**分离** | `input + cache_read + cache_creation` |
| OpenAI Chat | `prompt_tokens` **包含**缓存 | `prompt_tokens` |
| OpenAI Responses | `input_tokens` **包含**缓存 | `input_tokens` |

- 输出：`output_tokens`；推理：`reasoning_tokens`（Responses 的 `output_tokens_details`）。
- **缺数据返回 `None`**，不写 0；`unknown` 是诚实的「未记录」。

### 3.2 上下文窗口

- 运行中：上下文面板优先读**当前 run 最新一条账本**的 `context_tokens`（网关请求终态即落账；
  原生 / harness 的 `context.usage` 也**实时入账**——每 run 一行、运行中更新、收尾补计费）。
- 运行结束：仍以 `messages.context_tokens` 快照为主（账本行用于校准），避免「最新请求 +
  其后消息」重复计算。
- 快照失效线：平台压缩时间、内核原生压缩时间、`session_id` 清空；比较的是**采集时刻**
  （`context_snapshot_at`），不是消息创建时刻。

### 3.3 账本启用边界

首次写入账本时在 `platform_settings` 记 `usage_ledger_started_at`，并用**本 run 的消息创建
时刻**对齐，避免边界轮被「旧消息段 + 账本段」各算一次。

用量页按此分段：

1. `created_at >= usage_ledger_started_at` → 读 `usage_records`；
2. `created_at < usage_ledger_started_at` → 读 `messages`（归到 `legacy`）；
3. 两段相加；同一笔不重复。

---

## 4. 数据模型

```text
usage_records(id, owner_id, conversation_id, run_id, request_id, source, origin,
              api_key_id, agent_type_id, agent_profile_id, model, provider, base_url,
              input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
              reasoning_tokens, context_tokens, api_calls, api_duration_ms,
              cost_usd, cost_source, cost_pricing JSON, is_estimated,
              request_started_at, created_at)
              # UNIQUE(run_id, source, request_id) 去重（INSERT OR IGNORE）
              # 缺数据 = NULL；cost_source = reported / estimated / unknown

api_keys(id, owner_id, name, gateway_id→gateways, key_mask, key_hash UNIQUE,
         enabled, revoked_at, last_used_at,
         daily/monthly_request_limit, daily/monthly_token_limit, created_at, updated_at)
         # 只存 sha256；明文 cosk_… 只返回一次；配额为空/0 = 不限制

usage_pricing(id, owner_id, model_pattern, input/output/cache_read/cache_creation_per_million,
              enabled, created_at, updated_at)
              # 手工价（美元/百万 token）；owner_id 为空 = 平台级
```

- `source`：`gateway` / `native` / `harness` / `legacy`。
- `origin`：`internal` / `external`。
- `cost_pricing`：成本依据（手工价 id / 目录模型 + 快照哈希 + 费率）。
- 新表由 `create_all` 自动建；后加列（`api_key_id`、`cost_pricing`）登记在
  `models.ADDITIVE_COLUMNS["usage_records"]`，旧库启动自动补列。

---

## 5. 记录网关（scoped）

**代码**：`runtime/recording_gateway.py`（纯逻辑）、`api/usage_gateway.py`（路由）、
`api/_forward.py`（流式转发）、`runtime/gateway.py:bind_recording()`。

**启用条件**（`service._recording_gateway_binding`）：

1. `CODA_RECORDING_GATEWAY` 不是 `off`（默认 `off`）；
2. 适配器有 `base_url_env` 注入缝；
3. `auto` 模式还要求适配器声明 `Capabilities.recording_gateway`（目前 Claude Code、Codex）；
4. 绑定的是**自定义网关**（`kind="custom"`，平台持有上游密钥，`inject` 模式）。

官方链路、无注入缝的内核一律**回退直连**，由原生解析兜底。

**流程**：

1. 每个 run 生成 256-bit 一次性 token（TTL 2h，run 结束吊销），注册上游地址 / 密钥 / 归属；
2. CLI 的 `base_url` 指向 `/api/internal/usage-gateway/<token>`，`api_key` 换成代理 token；
3. 路由校验**回环来源 + 有效 token**，同协议转发、tee 响应流、解析 usage；
4. 响应终态（正常 / 取消 / 断开 / 上游异常）立即写账本；凭证不落日志、不落正文。

**安全**：只收 loopback；上游密钥只存在服务端内存；不记录请求 / 响应正文；子进程 env 里
只有代理 token。转发失败返回 502，但**已测用量保留**。

**不做**：跨协议翻译、请求体改写、工具执行、公网暴露。

---

## 6. 原生实时入账

**代码**：`usage_ledger.record_native_snapshot_isolated()` + `service.consume_run()`。

- 收到原生 `context.usage` 事件（且不是 record 模式）就写账：
  - **每个 run 一行**（`request_id = <run_id>:native`），运行中不断更新 `context_tokens`；
  - 独立会话提交，面板下一次轮询即可见；
  - 同值重复上报**节流**，不重复写库；
  - 收尾 `finalize=True` 把整轮计费计数补进同一行并计算成本，**不新增行**。
- record 模式下原生事件只做展示，不写原生行（gateway 是权威源）。
- 认证源判定 `resolve_authority` 识别 `gateway / native / harness`。

---

## 7. 上下文压缩

### 7.1 平台压缩（每轮开始前）

`prepare_run` / `regenerate_last` 在发送前估算；达到阈值就把更早的消息折叠成摘要、清
`session_id` 重开会话。估算与压缩决策共用 `runtime/context.py` 的唯一入口。

### 7.2 原生压缩配置（下发给内核）

平台把**生效上限 / 触发阈值**写进 `AgentContext.context_limit_tokens / context_trigger_tokens`，
适配器据此生成内核自己的压缩配置：

| 内核 | 下发方式 |
| --- | --- |
| Claude Code | `CLAUDE_CODE_AUTO_COMPACT_WINDOW` + `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`（百分比按 100K–1M 夹取后的窗口算；用户显式 `--autocompact` 时不覆盖） |
| Codex | `-c model_context_window=<上限>` + `-c model_auto_compact_token_limit=<阈值>`（Codex 自己在 90% 内再夹取） |

这样「平台压缩」和「内核原生压缩」共用同一阈值，不会出现「面板说没超、内核却提前压」。

### 7.3 内核原生压缩事件

内核自己压缩（如 Claude Code auto-compact）时上报 `context.compacted`；运行层记下
`context_kernel_compacted_at`，让更早的快照失效；若带 `post_tokens`，它直接成为最新快照。

---

## 8. 成本

**代码**：`runtime/usage_ledger.apply_cost()`、`app/model_catalog.py`。

优先级：**手工价 > models.dev 目录价 > unknown**。

- 手工价：按 owner（个人 + 平台级）与**最长前缀**匹配；
- 目录价：按 provider/model（含 provider 别名、`provider/model` 前缀写法）匹配；不认识的
  中转**不继承**官方价；
- 两种价都记 `cost_source=estimated`，并把依据写进 `cost_pricing`；`reported` 留给上游明确
  报价（目前未接入）；
- **只对新写入行计价，不重算历史**；目录刷新 / 改价都不会回改旧行；
- 只有窗口快照、没有任何计费计数的行不算成本（避免“假装 0”）。

**目录刷新**：启动先读磁盘快照（`<data_dir>/models/models.dev.json`），后台从 models.dev 拉
一次；15s 超时、20MiB 上限、原子替换、失败保留旧快照，不阻塞启动。

**覆盖率**：用量页返回 `cost_usd`（没有已定价行时为 `null`）与 `cost_coverage`
（reported / estimated / unknown 行数）。

---

## 9. 外部模型 API

**代码**：`app/api_keys.py`（签发/校验/配额）、`api/api_keys.py`（管理接口）、
`api/model_gateway.py`（外部路由）、`runtime/recording_gateway.py:persist_usage()`。

- **API Key**：`cosk_` 前缀，**只存 sha256**，明文只在签发时返回一次；列表只给掩码；
  吊销保留审计行；必须绑定**自定义网关**。
- **入口**：`/api/model-gateway/<path>`（不进公开 OpenAPI）。同协议直通（Anthropic Messages
  / OpenAI Chat / Responses），平台用绑定网关的上游密钥 `inject` 转发。
- **记账**：`origin=external` + `api_key_id`，按 Key 归属。
- **配额**：按 Key 的日 / 月**请求数**与 **token 数**，转发前按已用量判，超限 429（不预扣）。
- **非推理请求**（`/models` 等）：只转发、不记账、不占配额。
- **安全**：`?api_key=` 只用于鉴权，转发前从查询串剔除；客户端带来的 key 不透传上游。

> **网络暴露提醒**：默认部署脚本 / Docker **监听 `0.0.0.0`**，`/api/model-gateway/...` 在服务
> 可达的网络里就能被访问，唯一的门是 `cosk_…` 密钥。只给本机用请设 `CODA_HOST=127.0.0.1`
> （Docker 映射写成 `127.0.0.1:8010:8010`）；对外开放请加反向代理 + TLS + 来源限制，并保持
> `CODA_AUTH_ENABLED=true`。

---

## 10. 审计与回填

- `GET /api/usage/records`：账本明细分页（`run_id` / `conversation_id` / `source` 过滤）。
  管理员看全部，普通用户只看自己。
- `GET /api/usage/records/export.csv`：CSV 导出（最多 5 万行，带 BOM）。
- `scripts/backfill_usage_records.py`：把**账本启用前**的历史 `messages.usage` 补成
  `source=legacy` 的账本行；默认 dry-run，`--apply` 才写；幂等；回填后把启用边界前移。
- `bootstrap.backfill_ownership`：鉴权开启时把老账本行 / API Key 的 `owner_id` 回填给管理员
  （`usage_pricing` 的 `owner_id=NULL` 是平台级语义，**不回填**）。

---

## 11. 配置项

| 环境变量 / 设置 | 默认 | 说明 |
| --- | --- | --- |
| `CODA_RECORDING_GATEWAY` | `off` | `off` / `auto` / `on` 记录网关灰度 |
| `CODA_RECORDING_GATEWAY_BASE` | `http://127.0.0.1:<port>` | 代理对外地址（Docker / 端口转发时覆盖） |
| `CODA_MODELS_DEV_URL` | `https://models.dev/api.json` | 目录价来源；留空 = 不下载，只用磁盘快照 |
| 上下文设置（`platform_settings`） | 上限 128k、阈值 80%、`limit_mode=auto` | 自动模式跟随内核真实窗口 |

---

## 12. 接口清单

| 接口 | 作用 |
| --- | --- |
| `GET /api/usage/overview` | 分段汇总 + `by_source` + `cost_usd` / `cost_coverage` |
| `GET /api/usage/records` | 账本明细（分页 / 过滤） |
| `GET /api/usage/records/export.csv` | 账本 CSV 导出 |
| `GET/POST/DELETE /api/usage/pricing` | 手工单价 |
| `POST/GET/PATCH/DELETE /api/api-keys` | 外部 API Key 签发 / 列表 / 调参 / 吊销 |
| `/api/model-gateway/<path>` | 外部模型入口（API Key 鉴权，不进公开文档） |
| `/api/internal/usage-gateway/<token>/<path>` | 记录网关内部转发（一次性 token + 回环） |
| `GET /api/conversations/{id}/context` | 上下文面板（运行中账本快照优先） |

---

## 13. 测试

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_usage_ledger.py` | 归一化、窗口口径、去重、权威源、成本、回填 |
| `tests/test_recording_gateway.py` | token 生命周期、URL 拼接、转发头、SSE 解析、运行中实时入账 |
| `tests/test_gateway_binding.py` | direct / record / 回退 / 官方 vs 自定义 |
| `tests/test_model_gateway.py` | 外部鉴权、密钥注入、外部记账、配额 429、非推理请求 |
| `tests/test_api_keys.py` | 签发只回一次明文、掩码、吊销、配额校验 |
| `tests/test_model_catalog.py` | 目录解析、别名/前缀查价、坏数据保留旧快照 |
| `tests/test_native_compaction.py` | Claude/Codex 原生压缩配置与窗口夹取 |
| `tests/test_usage.py` / `test_context.py` | 用量页分段/成本覆盖率、上下文账本分支 |

完成定义：`bash scripts/test.sh` 全绿；前端改动时 `npm run build` 全绿。

---

## 14. 未完成 / 已知边界（重要）

以下都是**有意留白**或明确未做，不要误以为已覆盖：

1. **官方直连的 passthrough 代理未做**：官方 OAuth / 内核自带登录仍走原生解析，不走记录网关。
   （计划中的 B 阶段；需处理「CLI 不接受自定义 base_url」并自动回退。）
2. **不做跨协议翻译**：只支持同协议直通（Anthropic Messages / OpenAI Chat / Responses）。
   OpenAI 客户端 → Anthropic 上游这类场景不支持；Gemini 协议也不在代理范围内。
3. **有注入缝但未声明能力的内核**（如 DeepSeek Harness）暂未接入记录网关；**没有
   `base_url_env` 的内核**（CodeBuddy / Doubao / Hermes / Antigravity / Custom CLI）无法通过
   通用方式代理，只能原生 / 估算（除非做适配器专属注入）。
4. **内部 run 没有配额 / 限流**：配额只存在于外部 API Key 路由；记录网关转发内部流量时不做
   额度判断。
5. **外部 API 只有请求数 / token 配额，没有 USD 预算**：不像对照实现那样按「已花费美元 +
   未定价请求门禁」拦截；也没有 Key 级模型白名单。
6. **目录价是参考估算**：按 provider/model 精确/别名/前缀匹配；模型短别名（如 `sonnet`）可能
   查不到完整模型 id；不继承未知中转的官方价；不重算历史。没有上下文分档 / 推理单独计价。
7. **平台不做运行中自动压缩**：平台压缩只在每轮开始前触发；运行中的压缩交给内核自己的
   auto-compact（由 7.2 的配置驱动）。不做「按账本水位在流中途改上下文」。
8. **不是逐 token 实时计量**：记录网关在请求终态落账；原生按事件（每请求/步）更新，不是
   每个 token 一次。
9. **审计视图没有筛选条**：后端 `/usage/records` 支持过滤，前端只展示最近 20 条 + 导出。
10. **移动端没有外部 API / 单价 / 审计面板**：这些低频管理动作只在桌面端设置页。
11. **公网安全需自行补齐**：默认监听 `0.0.0.0`；TLS、反向代理、来源限制、限流不在本项目内。
12. **回填只补账本启用前的通用历史**：没有对照实现那种按内核逐轮修复（Codex rollout 修复）
    的离线工具。
13. **流式上游错误未逐块擦除密钥**：非流式错误体已读出来擦掉上游密钥；`text/event-stream`
    的错误片段无法跨块可靠替换，保持原样转发（上游在 SSE 错误里回显密钥属于极少数情况）。

---

## 15. 与 ekko-studio 的差异（速查）

| 维度 | 本项目 | ekko-studio（0.8.32） |
| --- | --- | --- |
| scoped 网关 | Claude Code / Codex，同协议 | 多内核 + **跨协议翻译**（Anthropic ↔ OpenAI Responses ↔ Gemini） |
| 原生计量 | Claude/Codex/Harness；其余估算 | 7 种内核逐家权威边界 + 修复工具 |
| 上下文 | 平台压缩 + Claude/Codex 原生配置 | 6 种内核原生压缩配置生成 + 版本化估算 |
| 成本 | 手工价 > 目录价，coverage | 目录 + 分档 + 缓存/推理价 + coverage |
| 外部 API | 请求数/token 配额 | USD 预算 + 未定价门禁 + 模型白名单 |
| 实时用量 | 每 run 单行实时更新 | 逐 model_call 落账 + 时间窗 live activity |

差异不是“谁对谁错”：本项目是本地优先、Python 栈、同协议优先；对照实现是多租户产品级。
上面的「未完成」清单就是若要继续对齐时的路线图。

---

## 16. 相关文件

```text
backend/app/models.py                         # UsageRecord / ApiKey / UsagePricing / ADDITIVE_COLUMNS
backend/app/runtime/usage_ledger.py           # 归一化、去重、权威源、成本、原生实时行
backend/app/runtime/recording_gateway.py      # 一次性 token、转发、tee、记账
backend/app/runtime/gateway.py                # GatewayBinding direct / record
backend/app/runtime/context.py                # 上下文口径、运行中账本快照
backend/app/runtime/service.py                # 运行层接线、原生实时入账、原生压缩策略
backend/app/api/usage.py                      # 用量总览 / 明细 / 导出 / 单价
backend/app/api/usage_gateway.py              # 内部记录网关路由
backend/app/api/model_gateway.py              # 外部模型网关路由
backend/app/api/api_keys.py                   # 外部 API Key 管理接口
backend/app/api/_forward.py                   # 两条网关共用的流式转发
backend/app/api_keys.py                       # Key 签发 / 哈希 / 掩码 / 配额判定
backend/app/model_catalog.py                  # models.dev 目录价
backend/app/agents/builtin/util.py            # 原生压缩配置纯函数
backend/app/agents/builtin/claude_code.py     # Claude 压缩环境变量
backend/app/agents/builtin/codex.py           # Codex -c 压缩覆盖
scripts/backfill_usage_records.py             # 历史回填（默认 dry-run）
frontend/src/components/UsageReport.tsx       # 用量页（来源 / 成本）
frontend/src/components/ExternalApiPanel.tsx  # 外部 API 设置面板
```
