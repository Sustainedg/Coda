# 技能机制与功能手册

> 适用对象：想搞清楚「技能怎么编目、怎么分配、怎么生效、怎么管理」的使用者，以及要改技能代码的工程师。
>
> 事实基线：Coda `main` 分支，提交 `2b23ded`（技能物化快速路径 / 硬链接 / 跨内核批量挂载之后的完整形态）。
> **代码是唯一事实来源**；本文与代码冲突时，以代码为准。
>
> 相关文档：`docs/architecture.md` §5（技能编目 / 分配 / 物化的架构视角）、`AGENTS.md` §3.8（技能契约铁律）、
> `docs/collab-mechanism.md`（协作域如何用节点级技能覆盖）。

---

## 0. 一分钟速查

| 你想知道 | 答案 |
| --- | --- |
| 技能是什么 | 一个装着 `SKILL.md` 的文件夹：教智能体「什么时候、怎么做某件事」的说明书 |
| 智能体怎么获得技能 | **只有一条路**：在智能体配置里勾选（`agent_skills` 分配表）。没分配就绝对用不到（「严格仅分配」） |
| 技能文件存在哪 | 共享技能留在内核原生目录；用户技能（导入 / 市场 / 挂载）统一放 `<数据目录>/skill-libraries/<用户id>/<slug>/`，**一份技能只存一份文件** |
| 多个内核能共用一份技能吗 | 能。「挂载到别的内核」只是登记一条引用（`.mounts.json`），不复制文件；市场更新一次全部内核同时生效 |
| 运行时技能在哪 | 每轮运行物化到 `agent-homes/<对话id>/<内核家目录>/skills/`，用完的隔离目录，内核通过环境变量（如 `CLAUDE_CONFIG_DIR`）指过去 |
| 取消分配后技能会消失吗 | 会，下一轮立刻消失（物化集合变了必触发重建） |
| 每轮都重抄一遍文件吗 | 不。集合与内容都没变时走快速路径直接复用；变了（更新 / 取消 / 清单坏了）才全量重建 |
| 物化是复制还是链接 | 同盘优先**硬链接**（零拷贝 inode 共享），不支持时自动降级普通复制 |
| 能删内核自带技能吗 | 不能。`system` / `plugin` 来源只读；个人 / 市场技能才能删（市场卸载走技能页） |
| 技能市场是什么 | 腾讯 SkillHub（skillhub.cn）：在市场页浏览、装进自己的技能库，与导入完全同一套管理 |
| 别人能看到我的技能吗 | 不能。用户技能按 `owner_id` 隔离；管理员可以看全部并带归属用户名 |
| 给技能加了前缀后内核调不到？ | 物化时会同步改写副本的 frontmatter `name`（Claude Code 按目录名调，Hermes 按 name 找），源目录不改 |

---

## 1. 全景：一份技能的一生

```
内核原生目录 / 用户技能库 / 技能市场
 │  ① discover_skills() 只读扫描（适配器声明 skill_roots）
 ▼
统一编目（skills 表，按 type_id × slug × owner 一行）
 │  ② 智能体配置页勾选（agent_skills 多对多分配）
 ▼
运行时物化（runtime/skills.materialize）
 │  ③ 每轮运行：期望集合 == 上轮 manifest → 复用；否则 rmtree + 重建（硬链接优先）
 ▼
agent-homes/<对话id>/<home>/skills/<slug>/
 │  ④ 适配器把 ctx.home_dir 写成内核的环境变量（CLAUDE_CONFIG_DIR / CODEX_HOME…）
 ▼
内核在自己的隔离目录里看到「恰好等于分配集合」的技能
```

四个环节各答一个问题：**编目**回答「有哪些技能」、**分配**回答「谁能用」、
**物化**回答「这次运行拿哪些」、**隔离目录**回答「内核从哪读」。

---

## 2. 概念与数据模型

### 2.1 技能的形状

一个技能 = 一个目录，里面必须有 `SKILL.md`（frontmatter 元数据 + 正文说明书），可选地
带 `scripts/`、`references/` 等附属文件。frontmatter 常见字段：`name`、`description`、
`version`、`author`、`allowed-tools`。目录名（slug）就是内核**调用**技能时用的名字。

### 2.2 两类技能、五个来源

| 类别 | 来源（`source_kind`） | 归谁管 | 能删吗 |
| --- | --- | --- | --- |
| 共享技能 | `system`（内核自带） | 内核自己 | ❌ 只读 |
| 共享技能 | `plugin`（已安装插件里的） | 插件管理器 | ❌ 整包卸载走插件管理器 |
| 共享技能 | `user` / `project`（宿主机扫到的） | 宿主机主人 | ✅（可导出 / 挂载） |
| 用户技能库 | `user`（导入的）、`market`（市场装的） | 当前用户 | ✅ / 卸载 |
| 用户技能库 | 挂载到别的内核的引用 | 当前用户 | ✅（摘引用） |

用户技能库的行带 `owner_id`，标识是 `<type_id>:lib:<owner 或 local>:<slug>`；
共享技能是 `<type_id>:<slug>`。不同用户导同名技能互不冲突。

### 2.3 数据表

| 表 | 作用 |
| --- | --- |
| `skills` | 统一编目：每「内核 × slug × 归属」一行，`path` 指向唯一实体目录，`fingerprint` 记内容指纹，`available` 标记这一轮扫没扫到 |
| `agent_skills` | 分配表（多对多）：智能体 → 技能，带 `position` 排序；「严格仅分配」的唯一事实源 |
| `skill-libraries/<owner>/.mounts.json` | 引用登记：`{"<slug>": ["claude-code", …]}`，磁盘上的跨内核共享凭据 |

同一份用户技能实体被 N 个内核引用时，编目展开成 N 行（每内核一行），**所有行共享同一个 `path`**。

---

## 3. 编目：适配器声明，框架统一扫

适配器用 `skill_roots()` 声明「本内核的技能根在哪」（`SkillRoot(path, source_kind, installable,
max_depth, namespace)`），框架 `scan_skill_roots()` 统一扫描。框架不认识任何具体内核、不硬编码
任何路径（AGENTS.md 铁律 13，有架构守卫测试）。

重扫（`POST /api/skills/rescan`）以磁盘为准，三种情况三种处理：

| 磁盘情况 | 处理 | 分配 |
| --- | --- | --- |
| 目录确实没了、技能根还在 | 删编目行 + 解除分配 | 清掉（幽灵引用不留） |
| 目录还在、这轮没扫到 | `available=False`「未扫描到」 | **保留**（防扫描落空清光分配） |
| 连技能根都不在（卷没挂上） | 同上，按「无法判断」 | **保留** |

旧布局 `<owner>/<type_id>/<slug>` 升级时自动迁移成新布局（同 slug 合并引用，内容冲突时
市场份优先、另一份挪 `.conflicts/`）。

同名技能靠 `SkillRoot.namespace` 加前缀共存（两个插件各带 `access` → `telegram-access` /
`discord-access`），原始目录名存 `extra["original_slug"]`。内核按**目录名**调用，前缀
同时也是调用名。

插件技能只认「已安装清单」（Claude Code 的 `installed_plugins.json` → `installPath`），
市场目录的克隆只是货架、不算已安装；Hermes 靠 `skills/.bundled_manifest` 区分自带与用户技能
（清单缺失退回「全按用户技能」）。

---

## 4. 分配：智能体能用什么，只看这一张表

`agent_skills` 是智能体技能的**唯一来源**（`PUT /api/agents/{id}/skills` 整表替换）。服务端
三层兜底校验：

1. 技能存在；
2. `type_id` 与智能体内核一致（跨内核用「挂载」而不是直接分配）；
3. 用户技能必须归属本人（别人的技能 422）。

协作域有节点级覆盖：`NodeSpec.skill_ids` 非 `None` 时以它为准（`None` = 跟随智能体配置），
安全边界与默认路径完全一致（同内核、可用、归属校验，见 `runtime/skills.skills_for_agent`）。

换内核时丢弃不匹配的分配；复制智能体时分配跟着走；软删除智能体不再算「使用中」但分配保留
（历史对话仍能复现技能）。

---

## 5. 物化：把「分配的」变成「内核看得见的」

### 5.1 隔离目录为什么长这样

物化产物放在 `<数据目录>/agent-homes/<对话id>/<home_name>/skills/`（`home_name` 由适配器的
`skill_mount()` 声明，如 `claude-home` / `codex-home`）。**每对话一个目录**而不是内核全局目录，
原因有三：分配按智能体隔离（全局目录会让所有智能体共享）、不污染本机内核配置、取消分配
能立刻生效。

隔离目录放在数据目录而不是临时目录：内核的**会话记录**也写在自己的配置目录下，每次运行
换目录会让多轮对话无法续接。

技能与 MCP 共用同一个「家」：技能物化先定下 `home_dir`，MCP 物化写进同一个目录
（顺序不能反）。

### 5.2 materialize() 的两条路径

```
materialize(home, mount, specs)
 │
 ├─ 快速路径：_reusable_skill_tree() 三重比对
 │    ① manifest 的 skills_subdir 与本轮一致
 │    ② 期望条目（slug + 源路径 + 逐文件 size/mtime 签名）== manifest 记录
 │    ③ 磁盘上每个技能目录都还在
 │    → 跳过重建直接复用；carry / patch 照做（内核配置可能在轮外被改）
 │
 └─ 慢路径（全量重建）
      rmtree 受管技能目录 → 逐技能链接优先复制 → 写新 manifest
```

`.skills-manifest.json` 记在隔离目录根部：`{version, skills_subdir, entries: {slug:
{source, files: {相对路径: [size, mtime_ns]}}}}`。

设计语义的关键不变量：**取消分配 → 期望集合必变 → 必触发重建 → 技能立即消失**。
清单读不出来（损坏 / 版本不认识）一律当「变了」处理，兜底永远安全。有任何「问题技能」
（目录名不安全 / 源没了 / 超体积上限）也放弃快速路径——用户每轮都该看到它们的告警。

### 5.3 硬链接优先（零拷贝）

技能库与 agent-homes 同在数据目录下（同盘），慢路径复制优先 `os.link`：

- inode 共享、零拷贝；
- 链接不可用（跨盘 / Windows 未提权 / 权限不足）自动降级 `shutil.copy2`，产物内容一致；
- 目录符号链接按指向复制（与 `copytree(symlinks=False)` 语义对齐），已访问解析路径集合防成环；
- **会被框架改写的副本先断链**：frontmatter 同步的 `SKILL.md` 改写前 `_break_hard_link()`
  （rename 出去再 copy 回来拿独立 inode），否则共享 inode 会把只读的源目录一起改掉；
- `carry` 配置文件**始终普通复制**：它随后会被 `_patch_json` 补丁改写，链接会改坏用户真实配置。

市场更新（先 rmtree 源目录再重写）不影响已物化副本持有的旧 inode；内容偏旧由 manifest 的
mtime 签名捕捉，下一轮自动重建拿到新内容——两道保险咬合。

### 5.4 物化触发点

| 场景 | 位置 | 隔离作用域 |
| --- | --- | --- |
| 普通对话 | `runtime/service.py:414` | 对话 id |
| 重生成 | `runtime/service.py:791` | 对话 id |
| 召唤轮 | `runtime/service.py`（summon） | 一次性 `summon-<uuid>` |
| 协作步骤 / 聊天 | `collab/execution.py:329` | 运行 × 智能体 × 通道 |
| 标题生成 | `runtime/titles.py:296` | `<智能体id>-title` |

### 5.5 适配器的四件事（声明式，框架照单执行）

`SkillMount(home_name, skills_subdir, carry, patch, notes)`：

- `home_name`：隔离目录名（技能与 MCP 必须一致）；
- `carry`：携带的本机配置文件（如 Codex 的 `auth.json` 保登录）；
- `patch`：配置文件补丁（如 Claude Code 关掉 bundled 技能）；
- 适配器在 `build_invocation()` 里把 `ctx.home_dir` 写进内核的环境变量。

物化后 `name != 源目录名` 的副本会同步改写 frontmatter `name`（只改副本，源只读）。

---

## 6. 用户技能库：一份实体 + N 个引用

```
<数据目录>/skill-libraries/<owner_id>/
 ├─ <slug>/                    # 技能实体（一份）
 ├─ <slug>/.skillhub.json      # 市场安装标记（市场技能才有）
 └─ .mounts.json               # {"<slug>": ["claude-code", "codex", …]}
```

三个动作只动「目录 / 引用登记」，不碰智能体分配：

| 动作 | API | 磁盘动作 |
| --- | --- | --- |
| 导入 zip | `POST /api/skills/import` | 解压进自己技能库 + 挂到指定内核 |
| 导出 zip | `GET /api/skills/{id}/export` | 只读打包，顶层目录名用 slug |
| 挂载到别的内核 | `POST /api/skills/{id}/copy` | 共享技能第一次收进库；之后**只登记引用** |
| 挂载到全部内核 | `POST /api/skills/{id}/copy-all` | 批量：逐内核尽力挂载，冲突的跳过并汇报 |

删除语义（`DELETE /api/skills/{id}`）：先摘本内核引用、断本内核分配；还有别的内核在用就
保留实体；最后一个引用消失才删目录与全部编目行。市场卸载（`POST /api/skillhub/uninstall`）
同一套规则（`remaining_kernels` 说明保留情况）。

同名护栏：同内核同 slug 已有技能时导入 / 挂载都拒绝（物化会撞名）；内容一致时幂等成功。
所有磁盘操作（导出 / 复制 / 删除）先验 `_is_declared_skill_dir()`：数据库路径被改坏也
只能碰到声明过的技能根内部。

---

## 7. 技能市场（SkillHub）

市场页浏览腾讯 SkillHub（skillhub.cn）的技能目录，安装时下载 zip → 落盘
`skill-libraries/<owner>/<slug>` → 写市场标记 → 登记引用 → 重扫。与导入完全同一套管理：
可卸载、可多内核共享、更新覆盖唯一实体后所有挂载内核同时生效。

装到别的内核时：目标实体已存在就是**更新**（覆盖一次全部生效）；库里已有同 slug 的个人
技能（非市场）时拒绝，不能悄悄覆盖用户自己的东西。

---

## 8. API 全表

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/skills` | 列表（共享 + 自己；管理员全部；支持 `q` / `source_kind` / `available` / `type_id` 过滤） |
| POST | `/api/skills/rescan` | 重扫（返回 added / updated / missing / removed / unassigned / total） |
| POST | `/api/skills/import` | 导入 zip（base64） |
| GET | `/api/skills/{id}` | 详情（正文 / 文件清单 / 使用它的智能体） |
| GET | `/api/skills/{id}/export` | 导出 zip |
| POST | `/api/skills/{id}/copy` | 挂载到指定内核 |
| POST | `/api/skills/{id}/copy-all` | 挂载到全部支持技能的内核（冲突跳过并汇报） |
| DELETE | `/api/skills/{id}` | 删除 / 卸载（`remaining_kernels` 汇报保留情况） |
| PUT | `/api/agents/{id}/skills` | 整表替换智能体的分配 |
| POST | `/api/skillhub/…` | 市场目录 / 安装 / 卸载（见 `api/skillhub.py`） |

多用户归属：列表 / 详情 / 分配 / 删除全链路按 `owner_id` 过滤（`_visible` / `_can_manage`）；
管理员可看全部并带归属用户名，但对话内容不豁免（那是另一条规则）。

---

## 9. 排错手册

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| 技能页显示「未扫描到」 | 目录还在但这轮没扫到（扫描失败 / 内核没装好） | 分配不受影响；点「重新扫描」 |
| 分配了技能但智能体用不到 | ① 内核没实现 `skill_mount`；② 物化告警被忽略（看对话里的 log 事件） | 检查内核是否声明 `Capabilities.skills`；看告警 |
| 挂载报「同名技能」 | 目标内核已有同 slug 的共享 / 个人技能 | 删掉或改名后再挂；内容一致时重试会幂等成功 |
| 删不掉的技能 | `system` / `plugin` 来源只读 | 插件技能走插件管理器整包卸载 |
| 删除报「目录删除失败」 | 磁盘权限 | 按提示检查权限；编目行还在（不留半截状态） |
| 市场更新后行为没变 | 旧 inode 被副本持有 | 下一轮物化自动重建；实在不行删对话隔离目录 |
| 加前缀的技能内核调不到 | frontmatter `name` 没同步 | 物化会自动同步副本；确认没绕过平台直接跑内核 |
| 同名技能互相覆盖只剩一个 | 没加 `namespace` 的根 | 适配器声明根时给会重名的来源加前缀 |

---

## 10. 代码地图与扩展指引

| 环节 | 位置 |
| --- | --- |
| 技能契约（SkillSpec / SkillMount / SkillRoot / 扫描） | `backend/app/agents/skills.py` |
| 编目 / 分配 / 物化 / 用户库 / 市场安装 | `backend/app/runtime/skills.py` |
| 隔离目录工具 | `backend/app/runtime/isolation.py` |
| 接口层 | `backend/app/api/skills.py`、`api/skillhub.py` |
| 市场下载 / 解压 | `backend/app/skillhub.py` |
| 测试 | `backend/tests/test_skills.py`（71 个用例） |

**让新内核支持技能**（AGENTS.md §4.6 四步）：

1. `capabilities` 声明 `skills=True`；
2. 实现 `skill_roots()`（会重名的来源加 `namespace`）；
3. 实现 `skill_mount(ctx)` 并在 `build_invocation()` 里把 `ctx.home_dir` 写进内核配置目录
   环境变量；
4. 加测试（解析 / 物化 / 隔离变量，参照 `test_skills.py`）。

**写测试时的两个坑**（来自实测）：

- 不要用 mtime 断言「发生了重建」——本机文件系统时间粒度粗，重建后 mtime 可能不变；
  用「塞残留文件看是否被清」观测；
- 共享技能挂进库时**源内核不登记引用**（它用原生目录），幂等检查要从库行出发。

---

## 11. 验证记录（本次核对）

- `pytest backend/tests/test_skills.py`：71 个用例全绿（含快速路径 5 例、硬链接 3 例、
  符号链接回归 1 例、批量挂载 1 例）；
- 后端全量 `pytest tests`：880 passed + 1 skipped；
- 前端 `tsc --noEmit` + `npm run build` 通过；
- 本文所述行为均有对应代码坐标与测试用例支撑（提交 `926b871` / `22e024c` / `2b23ded`）。
