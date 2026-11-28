# 协作波次并行机制

> 适用对象：想把「多岗位同时干活」用起来、或要改并行调度代码的人。
>
> 事实基线：`backend/app/collab/engine.py`（`_pick_wave` / `_run_wave`）、`tickets.py`（`tickets` 数组）、
> `schemas.py`（`BudgetSpec.max_parallel`）。**代码是唯一事实来源**；本文讲清动机、规则与边界。
>
> 相关文档：`docs/计划任务/collaboration-design.md` §3.4（设计基线）、`docs/功能文档/collab-mechanism.md`（协作全貌）、
> `docs/功能文档/collab-guide.html`（使用者图文版）。

---

## 0. 一分钟速查

| 你想知道 | 答案 |
| --- | --- |
| 并行开了吗 | 默认**没开**。预算里 `max_parallel=1` 是完全串行，与过去行为一字不差 |
| 怎么开 | 协作组预算「并行上限」设为 2~8；运行中也可 `POST /runs/{id}/budget` 调整后点「继续」 |
| 什么能并行 | **只有**「同一次 split 拆出的兄弟工单」且「持有者岗位互不相同」的一批 |
| 什么永远不并行 | 同一岗位（一个岗位一个对话通道）；返工环、审查链、跨层工单 |
| 一次能拆几张 | `tickets` 数组最多 8 张（`MAX_SPLIT_TICKETS`），每张可自带 `assignee` 分给不同岗位 |
| 并行了账会乱吗 | 不会。序号 / 步数在数据库层原子计数，用量在波次收口后单点聚合 |
| 并行时取消会怎样 | 和串行一样：`/cancel` 杀掉这一波全部子进程，工单状态原样保留可「继续」 |
| 典型收益 | 「领导者 + 2 平级成员」拆两张、并行度 2 → 墙钟时间约砍半 |

---

## 1. 为什么当初是串行，为什么现在是波次

一期把引擎做成**绝对串行**，是三笔算好的账，不是偷懒：

1. **可解释性**：同一时刻只有一个步骤在跑，「谁在等谁」永远说得清，时间线一条线看完；
2. **取消语义**：`cancel` 一刀下去，不会有「停了一半、另一个还在写文件」的中间态；
3. **共享目录竞态**：所有岗位共用同一个工作目录，两个写者同时改一个文件就是互相覆盖。

但「领导者 + N 个平级成员」恰好是这笔账亏得最狠的形状：领导者 `split` 拆出的子工单
**互相之间零依赖**，理论上 N 倍速，实际却在排队。而真正天生串行的部分
（`submit → review → rework` 的返工环、同一节点的多张工单）一点没省。

波次并行（wave scheduling）就是把「不该串的」切开、把「必须串的」留下：

```
领导者 handoff ──▶ CTO 一次 split 拆两张（se1 / se2 各一张）
                        │
                        ▼
        ┌─────────── 波次调度（max_parallel=2）───────────┐
        │   [se1]《实现主题开关》        [se2]《实现持久化》  │  ← 同波并行，各开独立会话
        └──────────────────┬──────────────────┘           │  事件按 step_id 分流给前端
                           ▼ join：全部收尾                │
        CTO 汇总（父工单 rollup 回到拆解者手里）            │
                           ▼                              │
        CEO 收尾汇报 → 运行 done                           │
```

**join 是天然同步屏障**：父工单汇总（`_rollup_parents`）、收尾汇报（`_hand_back_to_entry`）
只在波次边界跑——这些逻辑一行没改，串行时代的语义原样保留。

---

## 2. 调度规则：`_pick_wave` 挑一波

每一轮循环，引擎从就绪队列（状态 ∈ `open / working / review / rework / escalated`，按
`(深度, created_at)` 排序）取工单。`max_parallel > 1` 时按下面的规则挑出**一波**：

1. **第一张工单定基调**：它在，它的同父兄弟才有资格进这一波；
2. **持有者岗位互不相同**：一个岗位一个对话通道（与入口聊天通道同一约束）。两张工单
   派给同一个岗位时，第二张排队等下一波；
3. **数量 ≤ `max_parallel`**：预算里的并行上限（1~8）是硬顶；
4. **保守截断**：就绪队列里遇到「不同父」的工单就停——它与后续工单的依赖关系未知，
   宁可不并。

`max_parallel=1`（默认）时 `_pick_wave` 永远只返回一张，走**与过去完全相同的串行路径**
——不开新会话、不多一层包装，既有 32 个引擎用例就是这条路径的回归线。

## 3. 协议：一次拆多张（`tickets` 数组）

并行需要输入来源——一张一张拆，兄弟工单永远只会先后就绪。所以 `delegate split`
的工单块支持数组写法：

````
```work_order
{
  "action": "delegate",
  "mode": "split",
  "assignee": "se1",
  "tickets": [
    {"title": "实现主题开关", "assignee": "se1", "goal": "…", "acceptance": ["…"]},
    {"title": "实现持久化",   "assignee": "se2", "goal": "…", "acceptance": ["…"]}
  ]
}
```
````

要点：

* **单张写法完全兼容**：`"ticket": {...}` 照旧，两种形态都归一到 `order.tickets`；
* **每张可自带 `assignee`**：不写就用外层的 `assignee`（全部给同一人）；
  要分岗并行就在每张里写自己的目标——注意每张的 `assignee` 都必须在派发者的
  `assign` 出边白名单里，否则整单拒绝；
* **上限 8 张**（`MAX_SPLIT_TICKETS`）：再多说明该先问用户怎么分工；
* **护栏一条不少**：拆解深度（≤5 层）、近似同名去重（同父下同名/高度相似子工单 ≤2 张）、
  缺标题拦截——多张拆分**逐张**过护栏，任何一张不合规**整单拒绝**，绝不留半截状态；
* 提示词（`render_protocol_guide`）会教模型这套写法，并强调「只在确实能并行分工时才拆多张，
  先后依赖的活分几轮拆」。

## 4. 并发安全：三处「不丢账」

并行分支各自开**独立的数据库会话**（`AsyncSession` 不能并发共享），事件经队列实时汇流
（信封自带 `step_id`，前端按它分流）。真正容易踩的坑是计数与聚合，三处都有结构性保证：

### 4.1 步骤序号：原子取号

串行时代「查最大值 + 1」就够；并行后两个步骤可能同时读到同一个最大值、各自 +1 落库，
时间线顺序就毁了。现在 `collab_runs.step_seq` 是个计数列，取号是一条原子语句：

```sql
UPDATE collab_runs SET step_seq = step_seq + 1 WHERE id = ? RETURNING step_seq
```

SQLite 的写锁保证这条语句的原子性（WAL 模式下写锁只持有这一条语句的时间）。
旧库升级由 `ADDITIVE_COLUMNS` 幂等补列（默认 0），`prepare_turn` 里对老数据做一次
`max(sequence)` 对齐，并行取号不会与历史序号撞车。

### 4.2 步数：原子自增

`steps_used` 同样走 `UPDATE ... SET steps_used = steps_used + 1`，不是「读出来 +1 再写回」。

### 4.3 用量：单点聚合，只写一列

`usage` 是 JSON 列，没法用 SQL 合并。规则是：**每一步只落自己的 `collab_steps` 行**，
`run.usage` 的聚合由波次 join 之后的主循环串行做（`_accumulate_run_usage`）。

聚合时有一个反直觉的坑：**不能用 ORM 整对象写回**。`expire_on_commit=False` 意味着
本会话 identity map 里的 run 对象不会因别的会话的 UPDATE 而刷新——整对象写回会把
并行会话刚自增的 `steps_used` 用旧值盖掉（实测抓到过：7 步只记 6 步）。所以聚合
**只 UPDATE `usage` 一列**（Core 语句），`_finalize` 收尾前也用 `populate_existing`
强制重读最新行。

## 5. 失败、取消与恢复

| 场景 | 行为 |
| --- | --- |
| 波内某步解析/执行失败 | 该步照串行语义挂起（`blocked` + 显式原因）；**同波其余步骤让它们跑完**——半途强杀已在执行的子进程会留下「工单还是 working、步骤还是 streaming」的脏状态，比等它跑完更糟。波次结束后主循环按停止信号收尾本轮 |
| 波内异常（数据库等） | `drive_one` 捕获后转成 `stopped = (ERROR, 可读原因)`，经 `_finalize` 发 `collab.notice`，绝不静默 |
| 用户点「停止」 | 与串行完全一致：`cancel_run` 杀掉这一波**全部**子进程句柄，工单原样保留，可「继续」 |
| 服务重启 | 启动恢复（`recover_interrupted`）统一收尾残留 `streaming` 步骤——多个并行残留一样收，无需特判 |
| 预算超限 | 波次边界检查，与串行同一套提示（「提高预算」后 `/resume`） |

## 6. 前端：多路流式输出

SSE 事件流在并行时会交织，但**信封协议一字未改**——每条 `collab.step.*` 都带 `step_id`，
前端按它分流（`collabStream.ts` 的 `live` 从单数改成数组，`step.started` 不再顶掉前一个、
`step.finished` 按 `step_id` 收账）：

* **进度看板**：多个岗位可以同时显示「实时输出中」（`buildCollabProgress` 接受数组）；
* **岗位抽屉**：`liveOfNode(live, node.key)` 取本岗位的那一路，互不串台；
* **主时间线**：领导者只认自己岗位的流（`leaderLive`），其他岗位的在旁边显示。

没有新增事件类型，所以不涉及事件协议的四处同步义务——这是当初选「step_id 分流」
而不是「新事件」的直接原因。

## 7. 怎么用（运维视角）

1. **开并行**：协作组设置 → 预算与上限 → 「并行上限（1=串行）」设为 2~8。两端（桌面/移动）表单都有；
2. **运行中调整**：运行挂起（`waiting_user`）时 `POST /runs/{id}/budget {"max_parallel": 2}`，
   再点「继续」即从下一波起生效（正在跑的这一波不受影响）；
3. **让模型拆多张**：不用教——提示词已内置；只要图里画了多个平级实施者、且协调者有指向它们的
   `assign` 出边，它就可以一次拆多张分头干；
4. **看效果**：协作进度页两个岗位同时「实时输出中」、时间线上两条步骤的 `sequence` 连续不重号。

**典型收益**：`max_parallel=2` + 两张兄弟工单，理论墙钟减半；N 个平级成员则受限于
「岗位互不相同」——图里有几个能同时接活的岗位，并行度实际上限就是几。

## 8. 边界与不做什么

* **不跨层并行**：父与子、爷与孙不同波（依赖方向不明，宁可不并）；
* **不并行写同一目录的写者**：write 权限的岗位之间可以并行（它们改的文件由拆单者规划），
  但**越权审计的指纹比对在并行下可能互相污染**（只读节点旁边的写者会弄脏快照）——
  这是已知边界，强隔离需沙箱（见 `collab-mechanism.md` §8）；
* **不保证波次大小**：岗位互异 + 兄弟约束意味着很多时候一波就一张（串行路径）；
* **不上executor 池**：每个分支一个 `asyncio.Task` + 独立会话，规模上限 8，没必要上线程池。

## 9. 测试与验证

`backend/tests/test_collab_parallel.py` 四个用例（用替身智能体的 gate 闸门做**确定性**并发验证）：

| 用例 | 断言 |
| --- | --- |
| 兄弟工单真并发 | 两张 gate 都卡住时，se1/se2 的 prompt 都已写入、两张工单都 streaming；跑完后时间线 7 条、序号 1..7 连续、用量 6 步一分不少 |
| 同岗位不并发 | 两张都派给 se1 时，se1 被卡住期间**只有一个** prompt（第二张排队） |
| 默认串行零回归 | `max_parallel=1` 时 se2 的 prompt 在 se1 完成前绝不出现 |
| 运行中调并行度 | 挂起 → `/budget` 调到 2 → 回答提问 → 兄弟工单同波并行 |

回归线：既有引擎 32 用例（串行路径）+ 守卫 7 + 全量 `bash scripts/test.sh`（732 passed）。

## 10. 代码地图

| 位置 | 职责 |
| --- | --- |
| `engine.py` `_pick_wave` | 挑波：兄弟 + 岗位互异 + 数量上限；`max_parallel=1` 直接返回单张 |
| `engine.py` `_run_wave` / `drive_one` | 后台任务并行推进；每步独立 `SessionLocal`；事件入队实时汇流；`WaveState` 带回用量/停止信号 |
| `engine.py` `_next_sequence` | `step_seq` 原子取号（`UPDATE ... RETURNING`） |
| `engine.py` `_accumulate_run_usage` | 波次 join 后单点聚合；**只写 `usage` 一列** |
| `engine.py` `_apply_action`（DELEGATE/SPLIT） | 多张拆分：逐张护栏、逐张建子工单、每张可自带 assignee |
| `tickets.py` | `WorkOrder.tickets` 解析（数组/单张归一）、逐张校验、协议指南教学 |
| `schemas.py` | `BudgetSpec.max_parallel`（默认 1，1~8）、`RunBudgetPatch.max_parallel` |
| `tables.py` | `collab_runs.step_seq` 计数列（登记 `ADDITIVE_COLUMNS`） |
| `api.py` `/budget` | 运行中调并行度（随时可调，含调回 1） |
| 前端 `collabStream.ts` | `live` 数组化 + `liveOfNode` / `latestLive`；按 `step_id` 分流收账 |
| 前端 `collabProgress.ts` | 多岗位同时「实时输出中」 |
| 两端预算表单 | 「并行上限（1=串行）」数字字段，`clampBudget` 按 1~8 夹紧 |
