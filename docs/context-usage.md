# 上下文用量按钮与自动压缩

输入框上方新增一枚「上下文」按钮，展示**已用 / 上限 / 百分比**；点开是详情面板
（当前已用、可配置上限、触发阈值、压缩策略、最近一次压缩状态），上限与阈值可在面板
里修改并持久化。用量达到阈值时，下一轮发送前会自动把更早的历史折叠成摘要。

## 估算口径（唯一一份）

| 内容 | 估算规则 |
| --- | --- |
| 中日韩文字（含全角标点、假名、谚文） | 1 字 ≈ 1 token |
| 其余字符 | 4 字符 ≈ 1 token |
| 每条消息 | 额外 4 tokens（角色 / 分隔开销） |

实现只有一处：`backend/app/runtime/context.py` 的 `estimate_tokens` / `estimate_usage`。
界面展示（`GET /api/conversations/{id}/context`）、阈值判断、压缩决策（`prepare_run`）
全部调用它，前端不另算一套。

> 这是**近似估算**，不是计费口径。各内核上报的 `usage` 才是真实消耗（用量页），
> 但「下一轮还没发出去的上下文会有多大」只能按正文估算。

默认值：上限 **128000** tokens，触发阈值 = 上限的 **80%**。

## 配置持久化

沿用仓库现有的键值配置表 `platform_settings`（与标题设置同一套读写约定）：

- 键：`context_settings:<owner_id>`（鉴权关闭 / 单用户部署时 owner 为空）
- 值：`{"limit_tokens": 128000, "threshold_ratio": 0.8}`
- 键不存在或损坏 = 回到默认值（日志留痕，不让坏数据弄崩界面）

接口：

- `GET /api/system/context`：读当前配置
- `PUT /api/system/context`：保存；`conversation_id` 可选，带了就校验「上限不得低于
  该对话当前已用量」
- `GET /api/conversations/{id}/context`：用量 + 阈值 + 压缩状态（按钮的数据源）

校验（前后端各一道，服务端为准，非法输入 422 且不落盘）：

- 上限必须是正整数、≥ 1000、且不低于当前已用量；
- 阈值（百分比）必须满足 `0 < 阈值 ≤ 100%`（换算成比例后自然满足「阈值 ≤ 上限」）。

## 自动压缩

触发点：`prepare_run`（以及 `regenerate_last`）在构建历史之前估算
「系统提示 + 历史 + 本轮提示」。达到阈值就压缩：

1. **保留系统提示**（不动 `AgentContext.system_prompt`）；
2. **保留最近若干轮**：按「上限的一半」从最新一条往前收，最多 8 条、至少 2 条；
3. **更早的历史折叠成摘要**：一行一条（用户/智能体 + 首 80 字），摘要最多占上限的
   1/5；原文一条不删，仍完整保存在 `messages` 表里（对话记录 / 导出不受影响）；
4. 压缩后清掉内核侧 `session_id`：session 型内核无法在自己的面板里裁剪上下文，
   这一轮改用「摘要 + 最近若干轮」注入；下一轮内核会建立新会话。
5. 失败（例如最近两条本身就超过目标）**不改任何已存上下文**，本轮沿用原始历史，
   并通过运行日志 + 按钮状态给出「压缩失败，已保留原上下文」的提示。

压缩是确定性的裁剪 + 摘要，不调模型、不阻塞用户继续输入。同一对话有
`begin_compaction` 占位，避免重复触发。最近一次压缩的状态（进行中 / 成功 / 失败）
通过 `GET /api/conversations/{id}/context` 返回，按钮上显示徽标、面板里显示详情。

## 改动文件

后端：

- `backend/app/runtime/context.py`（新增）：估算口径、配置读写与校验、压缩、状态；
- `backend/app/models.py`：`Conversation` 增加 `context_summary` /
  `context_compacted_count` / `context_compacted_at`（含旧库补列声明）；
- `backend/app/runtime/service.py`：`prepare_run` / `regenerate_last` 接入压缩，
  运行日志推送压缩结果；
- `backend/app/api/conversations.py`：`GET /conversations/{id}/context`；
  `/clear` 与删除对话时清理压缩状态；
- `backend/app/api/system.py`：`GET/PUT /system/context`；
- `backend/tests/test_context.py`（新增）：估算、配置持久化与校验、自动压缩成功 /
  失败兜底、清除记忆。

前端：

- `frontend/src/components/ContextMeter.tsx`（新增）：按钮 + 详情面板；
- `frontend/src/lib/contextUsage.ts`（新增）：取数 / 校验 / 展示格式化；
- `frontend/src/lib/api.ts`、`frontend/src/lib/types.ts`：接口与类型；
- `frontend/src/pages/ChatPage.tsx`、`frontend/src/mobile/pages/MobileChatPage.tsx`：
  接入输入框上方；
- `frontend/src/index.css`、`frontend/src/mobile/mobile.css`：两端样式（共享段逐字一致；
  移动端在共享段之后补了一条「面板输入框 16px」的微调，避免 iOS 聚焦时强制放大页面）。

## 手动验证

1. 打开任意对话，输入框上方出现「上下文 x/y · z%」按钮；消息变化后数字跟着变。
2. 点开按钮：能看到当前已用、可配置上限、触发阈值（tokens + %）、压缩策略与估算口径。
3. 把上限改成 20000、阈值 80% 保存 → 刷新页面，数值仍是 20000 / 80%。
4. 非法输入各试一次：`abc`、`0`、`-1`、比当前已用更小的上限 → 面板内出现中文提示，
   刷新后配置未变。
5. 自动压缩：新建一条对话，把上限设为 4000、阈值 80%，连续发送几条较长的消息，
   直到按钮百分比越过 80%；再发下一条 → 运行日志出现「上下文已自动压缩：N 条较早
   消息折叠为摘要」，按钮出现「已压缩」徽标，对话可继续。
6. 失败兜底（自动化测试覆盖）：`backend/tests/test_context.py`
   `test_compaction_failure_keeps_original_context` 断言压缩抛错时消息一条不少、
   状态为 `error`、本轮照常完成。
