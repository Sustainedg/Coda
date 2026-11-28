# 文档索引

> 本目录按「这份文档回答什么问题」分四类，而不是按写作时间堆平。
> 需要快速定位时先看这里；引用文档时优先用本页给出的路径。

---

## 1. 架构（`架构/`）

讲**系统为什么这样分层、一次运行怎么走、扩展点在哪**。改框架层、加事件、加适配器之前先读。

| 文档 | 回答什么 |
| --- | --- |
| [`architecture.md`](架构/architecture.md) | 一次运行的完整时序、事件协议、适配器契约、数据模型、扩展点、移动端与协作呈现层 |
| [`ai-coding-standard.md`](架构/ai-coding-standard.md) | 分层铁律的理由、契约详解、扩展决策树、前端规范、评审清单 |
| [`usage-normalization.md`](架构/usage-normalization.md) | 各协议 token 用量如何归一化、原始留痕、双轨采集与跨来源幂等 |

---

## 2. 功能文档（`功能文档/`）

讲**这个功能是什么、用户看到什么、怎么用、边界在哪**。面向使用者与维护者。

| 文档 | 回答什么 |
| --- | --- |
| [`上下文窗口与自动压缩.md`](功能文档/上下文窗口与自动压缩.md) | 上下文按钮、计量口径、平台压缩、与内核原生压缩的协同 |
| [`usage-gateway.md`](功能文档/usage-gateway.md) | 记录网关、一手账本、外部模型 API、成本与审计 |
| [`任务进度卡.md`](功能文档/任务进度卡.md) | 多步骤任务的进度展示、围栏 / MCP / 内核任务三通道、存储与合并 |
| [`轨迹调试视图.md`](功能文档/轨迹调试视图.md) | 调试开关、五类轨迹事件、网关采集、脱敏、体积裁剪 |
| [`im.md`](功能文档/im.md) | 聊天软件接入：平台、配对、消息路由、出站与安全 |
| [`admin-mcp.md`](功能文档/admin-mcp.md) | 系统管理 MCP：零配置、权限、工具清单 |
| [`skills-mechanism.md`](功能文档/skills-mechanism.md) | 技能编目、分配、物化、用户技能库与技能市场 |
| [`collab-mechanism.md`](功能文档/collab-mechanism.md) | 多智能体协作：概念、工单协议、运行生命周期、API 与前端 |
| [`collab-parallel.md`](功能文档/collab-parallel.md) | 协作波次并行：调度规则、并发安全、失败恢复 |
| [`collab-guide.html`](功能文档/collab-guide.html) | 协作机制图文版（浏览器直接打开） |
| [`codebuddy-cli.md`](功能文档/codebuddy-cli.md) | CodeBuddy CLI 接入说明 |
| [`doubao-cli.md`](功能文档/doubao-cli.md) | 豆包 CLI 接入说明 |

---

## 3. 说明（`说明/`）

讲**实现细节、长期约定、验证记录**。不是面向新用户的主线阅读材料。

| 文档 | 回答什么 |
| --- | --- |
| [`context-usage.md`](说明/context-usage.md) | 上下文计量与压缩的实现口径、逐内核对接细节 |
| [`desktop.md`](说明/desktop.md) | 桌面版（Electron 壳）的长期约定、更新通道、签名公证、内核安装通道 |
| [`kernel-icon-perf.md`](说明/kernel-icon-perf.md) | 内核图标加载耗时的验证记录与根因 |

---

## 4. 计划任务（`计划任务/`）

只放**尚未完成**的方案与调研。已完成的方案应改写进 `功能文档/` 或 `架构/`，不在这里留痕。

| 文档 | 状态 |
| --- | --- |
| [`agent-core-memory-plan.md`](计划任务/agent-core-memory-plan.md) | 智能体核心记忆（用户偏好 + 核心记忆）方案，尚未开工 |
| [`agent-centric-memory-plan.md`](计划任务/agent-centric-memory-plan.md) | 智能体为核心的记忆方案（记忆目录按智能体切 + 跨内核），待决策 / 实施 |
| [`claude-native-memory-plan.md`](计划任务/claude-native-memory-plan.md) | Claude Code 原生记忆跨会话共享方案，待实施 |
| [`basic-memory-embedding-plan.md`](计划任务/basic-memory-embedding-plan.md) | Basic Memory 中文语义检索失效，切换多语言 / API embedding，待选服务商 |
| [`basic-memory-layering-plan.md`](计划任务/basic-memory-layering-plan.md) | Basic Memory 用户 / 智能体 / 项目三层隔离方案，待决策 |
| [`collaboration-design.md`](计划任务/collaboration-design.md) | 协作域设计基线；主体已实现，仍有少量打磨项 |
| [`conversation-members.md`](计划任务/conversation-members.md) | 对话成员（多用户共聊 + 成员智能体）方案，待开工 |
| [`desktop-remote-device.md`](计划任务/desktop-remote-device.md) | 桌面端免登录 + 连接远程设备方案，待开工 |
| [`skills-versioning-plan.md`](计划任务/skills-versioning-plan.md) | 技能版本管理方案，待开工 |
| [`qwen-code-cli.md`](计划任务/qwen-code-cli.md) | Qwen Code CLI 接入方案，适配器尚未落地 |
| [`minimax-code-cli-plan.md`](计划任务/minimax-code-cli-plan.md) | MiniMax Code CLI 接入方案，适配器尚未落地 |
| [`统一内容存储设计方案.md`](计划任务/统一内容存储设计方案.md) | 内容库（按 owner 隔离 + 内容寻址）方案，待产品拍板「项目」边界 |
| [`统一凭据池与轮询层方案.md`](计划任务/统一凭据池与轮询层方案.md) | 内核级账号池 + 网关级密钥池、选号策略、健康冷却与跨内核接入方案，待开工 |

---

## 5. 阅读顺序建议

- **第一次接手**：`AGENTS.md` → `架构/architecture.md` → `架构/ai-coding-standard.md`；
- **改一个功能**：先读对应的 `功能文档/`，再看 `架构/` 里的事件表与扩展点；
- **排障**：`功能文档/轨迹调试视图.md` → 对应功能的「排错」章节；
- **写新方案**：放 `计划任务/`；完成后改写并迁入 `功能文档/` 或 `架构/`。

---

## 6. 维护约定

1. **代码是唯一事实来源**。文档与代码冲突时，先改代码或先改文档，别让两边长期不一致。
2. **已完成的计划不留痕**：不写「已实施」「P1 / M2 / S3」「本轮」「待验收」这类过程话术，
   直接按功能 / 架构 / 说明的口吻描述现状。
3. **路径跟着文档走**：移动文档时同步更新 `AGENTS.md`、`README.md`、代码注释与测试
   docstring 里的引用。
4. **新文档先归类**：功能进 `功能文档/`，架构进 `架构/`，实现细节与长期约定进 `说明/`，
   未完成方案进 `计划任务/`。
5. **HTML 说明页**（如 `collab-guide.html`）保持单文件、无外部依赖，浏览器直接打开。