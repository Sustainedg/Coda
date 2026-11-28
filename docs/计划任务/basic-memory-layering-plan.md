# Basic Memory 用户分层与智能体分层方案

> **状态：研究完成，待决策 / 待实施（任务计划）**
> 一句话结论：**三层都能做，其中两层不用改一行源码；只有「智能体多项目可见性」需要补约 30 行纯增量代码。**
> 核心纠偏：**多智能体共享一套记忆系统 = 一个用户、一个库、一份索引。索引不会 ×N。** 只有「不同用户」才需要独立数据库。
> 本文所有结论均基于 2026-11-28 对本机 basic-memory **1.0.0**（v0.1.10-alpha.4 schema）的实测 + 源码取证，未做无据推测。

---

## 〇、小白讲解（不需要懂代码也能看）

### 1. 先纠正一个容易搞混的概念

「多智能体共享一套记忆」和「每个智能体一套记忆」，是**两种完全不同的东西**：

- **共享一套** = 一个用户，一个记事本，几个助手都翻同一本。**写一遍，大家都能看见。**
- **各一套** = 几个助手各自一本记事本。想让他们看到同样的内容，就得**每人抄一遍**。

抄一遍这件事，只在第二种形态下发生。所以：

> **共享形态下，索引只有一份，和智能体数量无关。**
> 早期讨论中「N 个智能体索引 N 遍」的说法，是把「每智能体独立配置目录」这个方案的缺点，错安到了共享形态头上。此结论已作废。

### 2. 那真正要解决的是什么？

真正的问题是：**共享一个库，怎么让 A 智能体看不到 B 的私有笔记？**

就像公司只有一个文件柜，但不同部门能开的抽屉不一样。

Basic Memory 现在的做法只有一档：**要么全开，要么只开一个抽屉**（`--project` 参数）。
缺的是中间那档：**只开指定的几个抽屉**。

### 3. 三层分别对应现实里的什么

| 层 | 现实比喻 | 用什么实现 |
| --- | --- | --- |
| 👤 **用户层** | 不同的人，各有各的文件柜 | 每人一个独立数据库（真隔离） |
| 🤖 **智能体层** | 同一个人的几个助手，能开的抽屉不同 | 同一个库 + 抽屉可见性清单 |
| 📁 **项目层** | 抽屉本身（按主题分） | Basic Memory 原生 project |

---

## 一、结论速览

| 需求 | 能做到吗 | 要改源码吗 | 升级影响 |
| --- | --- | --- | --- |
| 多智能体共享一套记忆 | ✅ 现在就是 | ❌ 不用 | 无 |
| 用户分层（不同的人隔离） | ✅ | ❌ 不用 | 无 |
| 智能体独立知识库 | ✅ | ❌ 不用 | 无 |
| 共享知识库 | ✅ | ❌ 不用 | 无 |
| **智能体「看私有+共享」的精确可见性** | ⚠️ 缺一档 | ✅ **约 30 行** | 纯增量，冲突极小 |
| 系统设置可视化面板 | ✅ | ❌ 不用（外壳） | 无 |

---

## 二、现状取证

### 2.1 索引确实是「一份」

本机当前有 **4 个 MCP 进程**同时运行：

```
PID 134811 / 138792 / 138836 ...
→ 全部无 BASIC_MEMORY_CONFIG_DIR 覆盖
→ 全部指向同一个 /app/data/home/.basic-memory/memory.db
```

它们读同一个库，靠 WAL 模式支撑并发：

```python
# db.py:230
cursor.execute("PRAGMA journal_mode=WAL")
```

**结论：多进程共享单库在存储层、并发层都是通的，无需任何改造。**

### 2.2 数据库路径跟着配置目录走

```python
# config_models.py:960
@property
def app_database_path(self) -> Path:
    """Uses BASIC_MEMORY_CONFIG_DIR when set so each process/worktree can
    isolate both config and database state."""
    database_path = self.data_dir_path / APP_DATABASE_NAME
```

**结论：`BASIC_MEMORY_CONFIG_DIR` 是「换库」的唯一开关。它适合做用户隔离，不适合做智能体隔离。**

### 2.3 project 表没有任何归属字段

```sql
CREATE TABLE "project" (
    id, name, description, permalink, path,
    is_active, is_default, external_id,
    last_scan_timestamp, last_file_count, last_indexed_at,
    partition_position
    -- 没有 user_id / agent_id / owner_id / tenant_id
)
```

**结论：「用户分层」在数据模型里完全缺失，只能靠「独立数据库」来实现。**

### 2.4 底层早已支持多项目，只是没开放

```python
# repository/search_scope.py:19
@dataclass(frozen=True)
class ProjectScope:
    project_ids: tuple[int, ...]        # ← 天然是多项目集合

    @classmethod
    def of(cls, project_ids: Iterable[int]) -> "ProjectScope":
        return cls(tuple(sorted(set(project_ids))))
```

搜索服务按 scope 执行：

```python
# services/scoped_search_service.py:50
async def search(self, query, *, limit, offset):
    """One ranking over every project in scope."""
```

MCP 工具层也已暴露：

```python
# mcp/tools/ranked_search.py:180
projects: Annotated[StringList, Field(description="Search these projects together.")]
all_projects: Annotated[bool, ...]
```

**结论：「看多个项目」的能力是现成的，缺的只是「配置入口」。**

### 2.5 唯一的多项目缺口：约束只有单数

```python
# project_resolver.py:48
constrained_project=os.environ.get("BASIC_MEMORY_MCP_PROJECT")

# resolve() 中：
if self.constrained_project:
    return ResolvedProject(project=self.constrained_project, ...)   # 直接返回，无视一切
```

优先级链：

```
1. ENV_CONSTRAINT: BASIC_MEMORY_MCP_PROJECT   ← 永远赢
2. EXPLICIT: project 参数
3. DEFAULT: config 里的 default_project
```

全库搜索复数版本：

```bash
grep -rn "MCP_PROJECTS\|constrained_projects"  →  (空)
```

**结论：只有单项目约束，没有多项目约束。这是本方案唯一必须动代码的点。**

### 2.6 单项目约束会连带锁死搜索

```python
# mcp/tools/search.py:319
if not _matches_constrained_project(item, constrained_project):
    continue          # 不在约束内的项目，直接跳过
```

一旦设了 `--project`，`all_projects=true` 会被砍成只剩一个项目。

| 需求 | 用 `--project` | 结果 |
| --- | --- | --- |
| 只看私有库 | ✅ 完美 | 但看不到共享库 |
| 私有 + 共享 | ❌ 锁死 | 只能单选 |
| 私有 + 共享（严格） | ❌ 做不到 | — |

---

## 三、目标架构

### 3.1 整体形态

```
/data/users/
├── alice/                              ← 👤 用户层：独立库
│   └── .basic-memory/
│       ├── config.json                 ← 项目注册表
│       └── memory.db                   ← 唯一索引（1 份）
│
│   ┌──────────┬──────────┬──────────┐
│   │ agent A  │ agent B  │ agent C  │  ← 🤖 智能体层：同库，可见性不同
│   └──────────┴──────────┴──────────┘
│        ↓          ↓          ↓
│   ┌────────────────────────────────┐
│   │  📁 项目层                      │
│   │  alice-private / team-shared    │
│   │  proj-bot / writer-drafts       │
│   └────────────────────────────────┘
│
└── bob/                                ← 另一个用户：另一个库
    └── .basic-memory/
        └── memory.db
```

### 3.2 索引成本

| 场景 | 索引份数 |
| --- | --- |
| 同一用户、10 个智能体、3 个项目 | **1 份** ✅ |
| 2 个用户、各 10 个智能体 | 2 份（每人一份，合理） |
| 10 个用户 | 10 份（隔离的必要代价） |

### 3.3 各层机制

| 层 | 隔离对象 | 机制 | 改代码 |
| --- | --- | --- | --- |
| 用户层 | 不同的人 | `BASIC_MEMORY_CONFIG_DIR` 独立库 | ❌ |
| 智能体层 | 同一人的不同 agent | 同库 + 项目可见性清单 | ✅ 约 30 行 |
| 项目层 | 知识主题 | 原生 `bm project` | ❌ |

---

## 四、实施方案

### 阶段 P0：验证（不改代码，先证明能走通）

目标：用现有能力拼出「多 agent 共享一个库 + 不同可见性」，看边界在哪。

| 步骤 | 动作 | 验收标准 |
| --- | --- | --- |
| P0-1 | 新建 2 个测试项目（`test-private`、`test-shared`） | `bm project list` 能看到 |
| P0-2 | 起 2 个 MCP 进程，一个 `--project test-private`，一个不约束 | 两者都能启动 |
| P0-3 | 用不约束的那个跑 `bm_search(projects=["test-private","test-shared"])` | 能同时命中两个库 |
| P0-4 | 用 `--project` 的那个跑同样搜索 | 确认只有 1 个项目的结果 |
| P0-5 | 两个进程同时写同一个项目 | 确认 WAL 下无写冲突报错 |

**产出**：一份实测记录，确认「不改代码能走多远」。

### 阶段 P1：最小可用（不改代码，靠纪律）

适用：对越界不敏感的日常场景。

| 步骤 | 动作 |
| --- | --- |
| P1-1 | 所有 agent 都不设 `--project`，共享同一个库 |
| P1-2 | 每个 agent 的 `default_project` 设成自己的私有库 |
| P1-3 | 在 MCP server instructions 里明确要求「搜索时必须传 `projects=[...]`」 |
| P1-4 | 在 system prompt 里写清楚本 agent 可见的项目清单 |

**风险**：靠模型自觉，不是代码强制。模型偷懒就会越界。

### 阶段 P2：补复数约束（唯一需要改代码的地方）

#### 改动点 1：新增环境变量读取

```python
# project_resolver.py，在 from_env 附近新增

@classmethod
def from_env(cls, default_project: Optional[str] = None) -> "ProjectResolver":
    return cls(
        default_project=default_project,
        constrained_project=os.environ.get("BASIC_MEMORY_MCP_PROJECT"),
        # 新增：复数约束，逗号分隔
        constrained_projects=_parse_project_list(
            os.environ.get("BASIC_MEMORY_MCP_PROJECTS")
        ),
    )
```

#### 改动点 2：构造 ProjectScope 时使用复数清单

在 `ProjectScope.single(...)` 的调用点改为：

```python
if resolver.constrained_projects:
    scope = ProjectScope.of([
        project_id_of(name) for name in resolver.constrained_projects
    ])
else:
    scope = ProjectScope.single(project_id)
```

#### 改动点 3：搜索侧放行多个项目

```python
# mcp/tools/search.py:272 附近
def _matches_constrained_project(project, constrained):
    if isinstance(constrained, (list, tuple, set)):
        return any(_names_project(...) for c in constrained)
    return _names_project(project, constrained)
```

#### 升级友好性评估

| 维度 | 评估 |
| --- | --- |
| 改动性质 | 纯增量，不改任何现有行为 |
| 涉及文件 | 约 3 个 |
| 代码行数 | 约 30 行 |
| 与上游冲突概率 | 低（新增分支，不重写逻辑） |
| 建议方式 | **不要 fork**，改为向上游提 PR；或维护一个 patch 文件，升级后重放 |

> ⚠️ **如果必须 fork**，记得上游 `config_models.py` 里有 `extra="ignore"`——自定义配置字段会被静默丢弃，不报错。所以私有配置要存在**你自己的库**里，别塞进 `config.json`。

### 阶段 P3：系统设置面板（外壳，不碰源码）

#### 可用接口

| 接口 | 用途 | 特点 |
| --- | --- | --- |
| `bm config list --json` | 全量配置枚举 | **带 `source` 字段**（file/env/default） |
| `bm config get <key>` | 读单个值 | 会显示被哪个环境变量覆盖 |
| `bm config set <key> <val>` | 写值 | 走 Pydantic 校验，写错直接拒绝 |
| `bm config unset <key>` | 恢复默认 | — |
| `bm doctor` | 健康检查 | — |
| `bm inspect` | 检视检索投影 | — |

`source` 字段是这套 UI 的核心价值：

| source | UI 表现 |
| --- | --- |
| `file` | 可编辑 ✅ |
| `env` | **置灰 + 提示「由环境变量控制」** ⚠️ |
| `default` | 可编辑，显示「未设置」 |

#### UI 结构建议

```
┌─ 记忆系统设置 ──────────────────────────┐
│                                          │
│  [嵌入模型]                               │
│   Provider:  [fastembed ▾] / [openai ▾]  │
│   API Base:  [__________________]        │
│   API Key:   [••••••••] (脱敏显示)        │
│   Model:     [__________________]        │
│   Dimensions:[____]                      │
│   ⚠️ 修改后需重启 MCP 生效                │
│   [保存并重启]                            │
│                                          │
│  [智能体可见性]                            │
│   agent: coco                            │
│     ☑ coco-private    ☑ team-shared      │
│     ☐ proj-bot        ☐ writer-drafts    │
│   [保存]                                  │
│                                          │
│  [存储状态]                               │
│   数据库: sqlite  ·  向量: sqlite-vec     │
│   实体 20 · 观测 3 · 关系 44 · 向量 224     │
└──────────────────────────────────────────┘
```

#### 升级友好性铁律

| 铁律 | 理由 |
| --- | --- |
| **只碰标准 key** | 上游不会删自己的 key |
| **按前缀动态发现字段** | `[k for k in cfg if k.startswith("semantic_embedding_")]`，升级新增字段自动出现 |
| **维护字段白名单 + 降级** | 上游改过名（`cloud_sync_path` → `local_sync_path`），读不到就隐藏，别崩 |
| **未知字段折叠展示** | 升级后新 key 归入「未识别」组 |
| **敏感字段后端脱敏** | `config.json` 权限 0600，含 `cloud_api_key` |
| **必须有重启按钮** | MCP 只在启动时读配置 |
| **绝不改源码** | 一旦 fork，升级从「一行命令」变成「rebase 噩梦」 |

升级命令只有一条：

```bash
uv tool upgrade basic-memory
```

---

## 五、风险清单

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| **路径即权限** | 两个用户 config 指向同一 `path` 就失去隔离 | 用户层配 OS 权限（不同 UID）+ 路径隔离 |
| **共享库写冲突** | 多 agent 同时写同一项目 | WAL 支持并发读单写；跨进程写建议串行化 |
| **共享库重复索引** | 仅在「每智能体独立 CONFIG_DIR」时发生 | **不要用 CONFIG_DIR 做智能体隔离**，用项目可见性 |
| **约束靠提示词** | P1 方案靠模型自觉 | 敏感场景必须走 P2 代码强制 |
| **上游改名** | 配置字段可能改名 | UI 做能力探测，不硬编码 |
| **embedding 中文失效** | 当前 `bge-small-en-v1.5` 是纯英文模型 | 见 `docs/计划任务/basic-memory-embedding-plan.md` |
| **改配置不重启无效** | MCP 启动时读一次 | UI 提供重启按钮 |

---

## 六、待办清单

### P0（验证，优先做）

- [ ] 新建 `test-private` / `test-shared` 两个测试项目
- [ ] 起 2 个 MCP 进程对比约束与非约束行为
- [ ] 验证 `projects=[...]` 多项目搜索实际效果
- [ ] 验证多进程写同项目的并发表现

### P1（不改代码的最小可用）

- [ ] 所有 agent 共享同一 `memory.db`
- [ ] 每个 agent 设独立 `default_project`
- [ ] 在 server instructions 里写入可见性纪律

### P2（补复数约束）

- [ ] 实现 `BASIC_MEMORY_MCP_PROJECTS`
- [ ] 改造 `ProjectScope` 构造点
- [ ] 改造 `_matches_constrained_project` 支持列表
- [ ] 向上游提 PR，或维护 patch 文件
- [ ] 回归测试：单项目约束行为不变

### P3（系统设置面板）

- [ ] 后端封装 `bm config list/get/set/unset`
- [ ] 实现 `source` 字段驱动的置灰逻辑
- [ ] 实现敏感字段脱敏
- [ ] 实现「保存并重启」
- [ ] 按前缀动态发现字段，避免硬编码

---

## 七、决策记录

| 日期 | 决策 | 理由 |
| --- | --- | --- |
| 2026-11-28 | 作废「N 个智能体索引 N 遍」的说法 | 该问题只存在于「每智能体独立 CONFIG_DIR」方案，不适用于共享形态 |
| 2026-11-28 | 智能体分层用「同库 + 项目可见性」，不用独立库 | 共享形态下索引只该有一份 |
| 2026-11-28 | 用户分层用 `CONFIG_DIR` 独立库 | 不同用户本就该隔离，重复索引是必要代价 |
| 2026-11-28 | 系统设置走外壳模式，不改源码 | 保证 `uv tool upgrade` 零痛 |

---

## 八、关联文档

- `docs/计划任务/agent-centric-memory-plan.md` —— 解决内核级记忆共享（Claude Code / Codex 等）
- `docs/计划任务/claude-native-memory-plan.md` —— 解决对话级隔离
- 本文 —— 解决 Basic Memory 这一层的用户与智能体分层
