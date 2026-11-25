# 对话成员（多用户共聊 + 成员智能体）设计方案

> 状态：**方案已确认，待开工**。本文档是唯一权威方案源，后续接力以本文档为准，
> 实现中若与本文冲突，先改文档再改代码。
> 相关约束：`AGENTS.md`（铁律与分层）、`docs/architecture.md`（现有运行时序）。
> 需求来源：对话页顶部最右侧增加「⋯」图标 → 展开成员侧边栏（头像 + 昵称）→
> 支持邀请用户与智能体加入对话；被邀请用户也能把自己的智能体添加进对话。

---

## 0. 一句话结论

在对话域（`api/conversations.py` + `runtime/service.py`）新增**成员关系**：两张引用表
（`conversation_participants` 用户成员、`conversation_agents` 成员智能体）+ `messages`
加一列 `author_user_id`；成员智能体的"发言方式"**完全复用现有 @ 召唤机制**
（`summon_profile_id`，`runtime/service.py:prepare_run` 已有完整实现），只放宽
"谁可以被召唤"的校验；前端两端（桌面 + 移动）顶栏加「⋯」按钮，弹成员面板。
**不新建旁路域、不新增依赖、不新增事件类型、不改适配器。**

---

## 1. 已确认的产品决策（评审结论，不要反复）

| # | 决策 | 说明 |
| --- | --- | --- |
| D1 | **邀请方式双轨**：V1 用户名直邀 + **V2 邀请链接一次做齐** | 邀请链接带过期时间与撤销；拥有者两种方式都能用 |
| D2 | 成员智能体发言 = **@ 召唤**（显式点将） | 复用 `summon_profile_id` 全套机制（独立隔离目录、不碰主场会话记忆）；不做"自动参与讨论"（那是协作域 `collab/` 的编排能力，对话域不重复造） |
| D3 | **移动端同步实现** | 两端功能对齐、交互各表（AGENTS.md §4.7） |
| D4 | 未读状态 V1 保持**对话级共享**（`last_read_at` 不按成员拆） | 多成员共享未读是已知妥协，列进 §10 后续优化 |
| D5 | 顶栏「⋯」与 AGENTS.md §4.7「不要再往顶栏塞 ⋯」的冲突 | 该条约束针对**会话级操作**（重新生成/删除，已收在左侧栏对话行）；「成员」是全新功能域且用户指令优先，放顶栏最右侧。**落地前必须先改规则**：同步更新 `AGENTS.md` §4.7 与 `docs/ai-coding-standard.md`，把「成员」明确列为顶栏按钮的例外，否则实现即违反硬约束；同时更新 `ChatPage.tsx` 顶栏注释，保持三处一致 |

---

## 2. 现状盘点（实现前必读的事实基线）

| 现状 | 位置 | 对本需求的影响 |
| --- | --- | --- |
| `Conversation` 单归属：`owner_id` 一个用户 + `agent_profile_id` 主场智能体快照 | `models.py` | 需引入成员关系表 |
| @ 召唤机制完整：召唤轮不续接主场会话、技能/MCP 物化到一次性隔离目录（`summon-` 前缀）、不用对话单独指定的模型 | `runtime/service.py:prepare_run`（约 L269-340） | 成员智能体发言**零新增机制**，只放宽召唤校验 |
| `messages.agent_profile_id` 已存在（@ 召唤轮记录作答者） | `models.py` | 多用户同聊还需 `author_user_id` 区分"谁发的" |
| 用户头像：`lib/avatar.ts` blob 加载 + `components/UserAvatar.tsx`（两端共用） | 前端 | 成员列表直接复用，零新资产 |
| 智能体头像：`components/AgentAvatar.tsx`（自定义上传 > 头像包 > 按 id 兜底的头像包；emoji 已下线） | 前端 | 同上 |
| `/api/auth/users` 列用户是**管理员专属** | `api/auth.py:list_users` | 邀请不能靠它——按用户名查 + 邀请链接，不泄漏用户清单 |
| `require_owned` / `owner_id_for` 归属校验体系 | `auth_deps.py` | 成员访问权在这之上扩展 |
| 桌面顶栏：`chat-topbar-actions`（分享 + 朗读） | `pages/ChatPage.tsx`（约 L990） | 「⋯」加在其右侧 |
| 移动顶栏：`MobileTopBar` 的 `actions` 插槽 | `mobile/pages/MobileChatPage.tsx`（约 L860） | 同上 |
| `MobileSheet`（底部弹层）、`Modal`（桌面弹窗） | 前端 | 邀请界面复用 |
| 未读：`last_read_at` 对话级单值 | `models.py` / `api/conversations.py:_is_unread` | 见 D4 |
| 对话列表 `_owned_query`：非管理员只看自己的 | `api/conversations.py` | **成员的对话列表要能看到被邀对话**（见 §4 L1） |

---

## 3. 数据模型（`backend/app/models.py`）

### 3.1 两张新引用表（`create_all` 自动建，老库无痛）

```python
class ConversationParticipant(SQLModel, table=True):
    """对话成员：除拥有者外被邀请进这条对话的用户。"""
    __tablename__ = "conversation_participants"

    conversation_id: str = Field(
        sa_column=Column(
            String(64), ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
        )
    )
    user_id: str = Field(
        sa_column=Column(
            String(64), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
        )
    )
    #: 谁邀请的（users.id；拥有者直邀时为拥有者 id）
    added_by: str | None = Field(default=None, sa_column=Column(String(64)))
    added_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime))


class ConversationAgent(SQLModel, table=True):
    """对话的成员智能体：被拉进这条对话的智能体（与主场的 agent_profile_id 相对）。

    任何成员都能添加**自己的**智能体；被添加的智能体可被本对话所有成员 @ 召唤。
    """
    __tablename__ = "conversation_agents"

    conversation_id: str = Field(
        sa_column=Column(
            String(64), ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
        )
    )
    agent_profile_id: str = Field(
        sa_column=Column(
            String(64), ForeignKey("agent_profiles.id", ondelete="RESTRICT"), primary_key=True
        )
    )
    #: 谁添加的（users.id）；移除权限 = 拥有者 / 管理员 / 添加者本人
    added_by_user_id: str | None = Field(default=None, sa_column=Column(String(64)))
    added_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime))
```

### 3.2 邀请链接（V2，一次做齐 —— D1）

```python
class ConversationInvite(SQLModel, table=True):
    """一条对话邀请链接。"""
    __tablename__ = "conversation_invites"

    id: str = Field(default_factory=new_id, primary_key=True)  # 邀请码（随机 32 hex）
    conversation_id: str = Field(..., ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    #: 创建者（users.id）；撤销 / 重新生成权限 = 拥有者 / 管理员 / 创建者
    created_by: str | None = Field(default=None, sa_column=Column(String(64)))
    expires_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime))
    #: 撤销时间；非空 = 已失效
    revoked_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime))
    #: 接受了邀请的用户（一人一码一用；复用需重新生成）
    accepted_by: str | None = Field(default=None, sa_column=Column(String(64)))
    accepted_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime))
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(UTCDateTime))
```

- **默认有效期 7 天**，创建时可指定（最长 30 天）；`expires_at` 为空 = 永不过期（不推荐，界面上默认给 7 天）。
- **一人一码一用**：`accepted_by` 非空即作废（防链接外泄后被反复使用）；拥有者可随时重新生成新码（旧的自动撤销）。
- 邀请码即主键（`new_id()`，256 位熵的 hex），URL 形如 `/chat/invite/<code>`（前端路由）。
- **安全**：码只出现在 URL 里（与 PAT 同级别机密度），接口永不回传全量列表给非拥有者；成员面板只显示"链接邀请"入口按钮，不显示历史码。

### 3.3 加列（登记 `ADDITIVE_COLUMNS`，铁律 9）

```python
"messages": {
    "agent_profile_id": "VARCHAR(64)",
    # 「这条消息由哪个用户发出」是后加的：为空 = 单用户时代的旧消息，
    # 界面回退显示为对话拥有者（兼容）。
    "author_user_id": "VARCHAR(64)",
},
```

---

## 4. 权限矩阵（后端访问控制核心）

新增依赖函数 `_accessible_conversation()`（`api/conversations.py`）：
`require_owned` 通过 **或** `user.id` 在 `conversation_participants` 里 **或** 管理员。
原 `_owned_conversation` 保留不动——管对话本体（改名/换智能体/删除）的接口继续用它。

| 操作 | 拥有者 | 参与成员 | 管理员 |
| --- | :-: | :-: | :-: |
| 看消息 / 发消息 / 传附件 / 订阅流 / 标已读 | ✅ | ✅ | ✅ |
| @ 召唤成员智能体 | ✅ | ✅ | ✅ |
| 把**自己的**智能体加进对话 | ✅ | ✅ | ✅ |
| 退出对话（移除自己） | — | ✅ | — |
| 用户名直邀 / 生成 / 撤销邀请链接 | ✅ | ❌ | ✅ |
| 移除成员 / 移除别人添加的成员智能体 | ✅ | ❌ | ✅ |
| 移除**自己添加的**成员智能体 | ✅ | ✅ | ✅ |
| 改标题 / 换智能体 / 换模型 / 换工作目录 / 删对话 | ✅ | ❌ | ✅ |

补充规则：
- **L1（对话列表）**：`list_conversations` 的过滤改为「自己的 ∪ 被邀的」（成员视角带 `role: "member"` 标记，拥有者带 `role: "owner"`）；管理员仍看全部。成员看到的对话行不可删除/归档（前端按 role 隐藏按钮）。
  > 注意：本方案开工前，**当前代码已把对话内容收紧为「仅本人可见，管理员也不豁免」**（`require_owned_strict`）。实施成员功能时，「管理员/成员能看到哪些对话」要按本方案的可见性规则重新定义，不能直接沿用现有的 owner-only 检查。
- **L2（召唤校验放宽）**：`send_message` 里召唤校验从「必须是自己的」改为「**自己的 或 已在 `conversation_agents` 表里**」；其余召唤逻辑（`prepare_run` 内部）**零改动**。
- **L3（成员智能体归属）**：添加成员智能体时 `require_owned(agent, user)`——只能添加自己的；别人的智能体只有拥有者/管理员能拉进来（拥有者添加时同样过 `require_owned`，即拥有者也只能拉自己的，除非是管理员）。**删除智能体时**：`conversation_agents` 行随对话 CASCADE；智能体本体软删除（`deleted_at`）后成员面板显示"已删除"灰态、不可召唤（召唤校验含 `deleted_at is None`）。
- **L4（被移除后）**：成员被移除 → 立即失去访问（`_accessible_conversation` 查表即失效，无需清 token）。
- **L5（禁用用户）**：被邀请用户 `is_active=False` 后自动失去访问权（`current_user` 层已有校验，成员表行保留、界面灰态）。
- **L6（邀请码安全）**：兑换接口 `POST /api/conversations/invites/{code}/accept` **无需预先鉴权通过归属**（登录即可兑换）；兑换时校验：存在 / 未撤销 / 未过期 / 未被接受 / 不在自己拥有（自己拥有自己的对话无意义）→ 写入 `conversation_participants` 并回填 `accepted_by`。失败一律 404 + 中文原因（不泄漏码是否存在）。

---

## 5. API 设计（挂在现有 `conversations` 路由下，`api/conversations.py`）

| 方法与路径 | 请求体 | 说明 | 权限 |
| --- | --- | --- | --- |
| `GET /api/conversations/{id}/members` | — | `{users: [...], agents: [...]}`（含头像标识、role、added_by、added_at） | 访问权（§4） |
| `POST /api/conversations/{id}/members` | `{username: str}` | 用户名直邀；回 404「用户不存在」（防枚举） | 拥有者/管理员 |
| `DELETE /api/conversations/{id}/members/{user_id}` | — | 移除成员；本人可退（`user_id == me`） | 拥有者/管理员/本人 |
| `POST /api/conversations/{id}/members/agents` | `{agent_profile_id: str}` | 添加成员智能体（校验自己的 + 未软删 + enabled） | 任何成员（自己的） |
| `DELETE /api/conversations/{id}/members/agents/{agent_id}` | — | 移除成员智能体 | 拥有者/管理员/添加者 |
| `POST /api/conversations/{id}/invites` | `{expires_hours?: int}` | 生成邀请链接（默认 168h=7天，上限 720h=30天，`null`=永久） | 拥有者/管理员 |
| `GET /api/conversations/{id}/invites` | — | 本对话的邀请码列表（含状态：待使用/已接受/已过期/已撤销；**只回 id 与状态，用于撤销按钮**） | 拥有者/管理员 |
| `DELETE /api/conversations/{id}/invites/{invite_id}` | — | 撤销 | 拥有者/管理员/创建者 |
| `POST /api/conversations/invites/{code}/accept` | — | 兑换邀请码（登录即可）→ 返回对话 id，前端跳转 | 任何登录用户 |

同时：
- `GET /conversations/{id}` 详情响应**顺带 `members`**（同 `GET .../members` 的内容）——前端轮询自动同步成员变化，成员头像渲染不用额外请求。
- `list_conversations` 响应项加 `role: "owner" | "member" | null`（管理员看别人的为 null）。
- `MessageRead` 加 `author_user_id`（`schemas.py` + 前端 `types.ts` 同步）。
- `schemas.py` 新增：`ConversationMemberRead`、`ConversationAgentRead`、`MemberInviteIn`、`InviteCreateIn`、`InviteRead`。

### 5.1 成员智能体被召唤的链路（唯一动运行层的地方）

`api/conversations.py:send_message` 现有校验（约 L472-479）：

```python
# 现状：召唤目标必须是"自己的"
require_owned(summon, user)
# 改为：自己的 或 本对话的成员智能体
if not _is_member_agent(session, conversation_id, summon.id) :
    require_owned(summon, user)
```

`runtime/service.py` **不改**（`prepare_run` 的召唤分支已完整：独立隔离目录、不写回 session、忽略对话模型）。这是本方案对运行层的唯一侵入点，且是 API 层的校验放宽，符合分层。

---

## 6. 前端 —— 桌面端（`pages/ChatPage.tsx` + `index.css`）

1. **顶栏按钮**：`chat-topbar-actions` 最右加 `<MoreIcon size={18} />`（已有图标，`Icons.tsx:216`），`aria-label="对话成员"`。同步更新顶栏注释（D5）。
2. **成员抽屉**：新建 `components/ConversationMembersPanel.tsx`（**纯展示**，两端复用成员行），桌面端以右侧 overlay 抽屉呈现（新 CSS：`chat-members-drawer`，宽 ~300px、遮罩点击关闭、配色全走现有 CSS 变量，深浅主题自动适配）。
   - 头部：对话标题 + 成员数。
   - 用户区：拥有者置顶（`pill brand` 标「拥有者」）→ 成员（`pill` 标「成员」）；每行 `UserAvatar` + 显示名 + `@username`；拥有者视角下成员行尾有移除按钮（✕）。
   - 智能体区：主场智能体置顶（标「主场」）→ 成员智能体（标「成员」）；每行 `AgentAvatar` + 名字 + 内核名；添加者/拥有者可移除。
3. **邀请 Modal**（复用 `Modal` + `agent-pick` 样式）两个分区：
   - **邀请用户**：用户名输入框 + 「邀请」按钮；下方「生成邀请链接」按钮 → 展示 `origin + /chat/invite/<code>` + 复制按钮（`navigator.clipboard`，失败提示手动复制）。
   - **添加智能体**：列出**自己的**智能体（`api.listAgents()` 已按归属过滤），点选即加，已加的标「已添加」。
4. **@ 浮层合并**：`mentionCandidates`（ChatPage L598）合并 `members.agents`：`enabled`、排除主场、按 id 去重（自己的智能体可能与成员智能体重叠）。
5. **消息作者头像**：`assistantIdentity` 旁边加 `userIdentity`——按 `message.author_user_id` 从 `members.users` 查身份，`MessageView` 的 `user` prop 传对应 `AuthUser` 片段；author 为空（老消息/拥有者发的）→ 传拥有者身份。注意：`MessageView` 现按 `role === 'user'` 画 `UserAvatar`，多成员后**所有 user 气泡都带作者身份**（成员 B 的消息不再是"我"）。
6. **兑换页路由**：`App.tsx` 加 `/chat/invite/:code` 路由 → 轻量组件 `InviteLanding`（登录态校验 → 调 accept → 成功跳 `/chat/{conversation_id}`，失败显示中文原因 + 「去登录/返回」）。

## 7. 前端 —— 移动端（`mobile/pages/MobileChatPage.tsx` + `mobile.css`）

两端功能对齐、交互各表（D3）：

1. 顶栏 `actions` 最右加三点图标（`m-icon-btn`）。
2. 成员面板用 `MobileSheet` 弹出（复用 §6 的 `ConversationMembersPanel` 成员行，样式在 `mobile.css` 另写一份，配色走变量）。
3. 邀请界面同样分"用户 / 智能体"两段（`MobileSheet` 内表单式交互，触屏友好）。
4. 兑换页：`MobileApp.tsx` 加 `/m/chat/invite/:code` 路由（PWA 深链可刷新，注意 §8 验证项）。
5. `types.ts` / `api.ts` 两端共用一份（规范），新增类型与接口调用见 §5。

## 8. 测试与验收（完成定义）

```bash
bash scripts/test.sh          # pytest + 前端 tsc 必须全绿
cd frontend && npm run build  # 前端改完必 build + 重启服务自测
```

后端测试（`backend/tests/test_conversation_members.py` 新建，参照 `test_api.py` 风格）：

| 用例 | 断言 |
| --- | --- |
| 拥有者用户名直邀 | 201，成员表落行，`added_by` 正确 |
| 成员直邀（越权） | 403 |
| 用户名不存在 | 404「用户不存在」 |
| 成员发消息 / 传附件 / 订阅流 | 200 |
| 非成员访问 | 404 |
| 成员添加自己的智能体 | 201 |
| 成员添加别人的智能体 | 404 |
| 拥有者添加别人的智能体（非管理员） | 404 |
| @ 召唤成员智能体（成员视角） | 200，`messages.agent_profile_id` = 被召唤者 |
| @ 召唤非成员非自己的智能体 | 422 |
| 召唤已软删的成员智能体 | 422 |
| 移除成员后访问 | 404 |
| 成员退出（删自己） | 204 |
| 邀请链接：生成/过期/撤销/一码一用/兑换跳转 | 各状态码与落库 |
| 兑换自己的对话的码 | 422（无意义） |
| 老对话无成员 | 照常访问（兼容） |
| `messages.author_user_id` 落库 | 成员 B 发的消息 author = B |
| 对话列表：成员视角 | 含被邀对话 + `role: "member"` |

手工端到端：
- A 邀 B（用户名 + 链接两种）→ B 发消息（B 的头像）→ B 拉自己的智能体 → A @ 它 → 回复正常 → A 移除 B → B 404。
- 移动端同样流程过一遍；`/m/chat/invite/<code>` 深链刷新可用。
- 移动端 PWA：改了外壳相关文件需把 `public/m/sw.js` 缓存名版本 +1（本次预计只动 `mobile.css`/页面 JS，**若 vite 构建产物名变化也需检查**——按 AGENTS.md §4.7 执行）。

## 9. 实施里程碑（接力时按此推进，完成一项勾一项）

- [ ] **M1 后端数据层**：`models.py` 三张表 + `ADDITIVE_COLUMNS` 加列；`db.init_db` 自动生效（无需注册，`models.ADDITIVE_COLUMNS` 已并入）。验证：老库启动无错。
- [ ] **M2 后端 API**：§5 全部接口 + `_accessible_conversation` + `send_message` 召唤校验放宽 + 详情/列表响应扩展。验证：pytest 新用例全绿。
- [ ] **M3 桌面端**：顶栏按钮 + 成员抽屉 + 邀请 Modal + @ 合并 + 作者头像 + 兑换路由。验证：tsc + build + 手工自测。
- [ ] **M4 移动端**：同 M3 的移动版。验证：tsc + build + 深链自测。
- [ ] **M5 收尾**：更新 `AGENTS.md` §4.7 与 `docs/ai-coding-standard.md` 的顶栏按钮规则（D5，必须先改规则再落地）、`ChatPage.tsx` 顶栏注释、`docs/architecture.md` 若涉及（预计不涉及事件表）、全量 `bash scripts/test.sh` + `npm run build` + 重启自测。

每个里程碑完成后：`bash scripts/test.sh` 全绿才进下一个；前端里程碑另跑 `npm run build`。

## 10. 明确不做（后续优化方向）

- 未读按成员拆分（现为对话级共享，D4）。
- 成员加入/退出的 `system` 消息时间线提示（`MessageView` 已支持 system 角色，随时可加）。
- 成员权限分级（如"只读成员"）。
- 邀请链接批量导入 / 邮件通知（email 字段已预留但通知系统不存在）。
- 成员智能体"自动参与讨论"（属协作域编排，D2）。
