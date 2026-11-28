# IM：在聊天软件里和智能体说话

- 入口：**每个智能体的设定页 → IM 标签**（桌面端 `/agents/{id}?tab=im`；移动端 `/m/agents/{id}?tab=im`；
  平台设置页不再有「IM」标签，`/m/im` 旧路由重定向到智能体列表）
- 代码：`backend/app/im/`（域）· `frontend/src/components/AgentImSettings.tsx`（两端共用的 IM 面板，
  挂在智能体设定页的「IM」标签里：桌面 `pages/AgentEditPage.tsx`，移动端 `mobile/pages/MobileAgentEditPage.tsx`）
- 参考实现：DS Bot（`Fei2411/DS-Bot`）的 IM 设置页与平台适配器 —— 只借鉴交互与协议思路，
  代码按本仓库的分层重写（Python · FastAPI · 无前端框架）。
- 相关文档：`AGENTS.md`（分层铁律与 §4.9 接入新平台）、`docs/架构/architecture.md`（IM 表结构与
  运行域交界）、`README.md`（平台一览与 API 速查）。

> **代码是唯一事实来源。** 本文描述的是当前工作区的实际行为；与旧说明冲突时，以代码为准。

---

## 0. 一分钟速查

| 你想知道 | 答案 |
| --- | --- |
| IM 是什么 | 把「聊天软件里的一个机器人」绑到一个智能体，用户在那边说话，就是对这个智能体说话 |
| 智能体会出现在成员名单里吗 | **不会**。IM 是**通道**，不是成员，不占协作岗位、不参与派活 |
| 需要公网地址 / 开放端口吗 | **不需要**。只建立客户端主动发起的连接（长轮询 / WebSocket / Socket Mode / Gateway） |
| 支持哪些平台 | Telegram、飞书 / Lark、钉钉、企业微信、QQ、Discord、Slack；微信个人号暂不支持 |
| 谁的话会进智能体 | 只有**配对过**的账号；群里还必须 **@ 机器人** |
| 回复发到哪 | 只发回**发起这一轮的那个聊天**；网页 / 手机 / 定时任务触发的轮次不会外发 |
| 回复是流式的吗 | **不是**。这一轮跑完才发一条最终消息；跑的过程中平台支持的话会持续显示「正在输入」 |
| 能发图片 / 文件给智能体吗 | 能。落到这条对话工作目录的 `attachments/`，路径并入这一轮的用户消息 |
| 过程内容（思考 / 工具 / 日志）会发到聊天里吗 | 默认**不发**；连接详情里可逐项打开，打开后以引用块附在正文之后 |
| 服务停了还能收消息吗 | 不能。只有服务运行时才收发；重启后自动重连，Telegram 从上次游标继续 |
| 一条聊天对应几条对话 | 固定一条（`im_chats.conversation_id`）；换绑智能体后才新开一条 |
| 同一聊天能并发跑吗 | 不能。同一聊天串行，上一条没跑完时先回「排在后面」 |
| 聊天里能改设置吗 | 不能。只支持 `/stop`、`/start`、`/help` 三个命令 |

---

## 1. 为什么，以及四条取舍

用户离开电脑时，智能体还在干活，但只有回到控制台才能找它。大多数人整天开着 Telegram、
飞书、钉钉这类聊天软件。IM 让用户在那里和某个智能体对话。

设计上坚持四件事：

1. **IM 是通道，不是成员。** 聊天软件里的机器人绑到一个智能体，不进智能体名单、不占权限。
   用户在 IM 里说的话就是用户的话，进那个智能体当前的那段对话。
2. **平台侧只往外连。** 只用长连接 / 长轮询（Telegram `getUpdates`、飞书 / 钉钉 / 企业微信 /
   Discord / Slack 的 WebSocket、QQ Gateway），不需要公网地址、不开放端口。
3. **只有服务运行时才收发消息。** 服务停了，平台会保留（或不保留）消息；能收到的等下次启动。
4. **回复只回来源。** 只有「从 IM 发起」的那一轮才把结果发回聊天软件；网页、手机、定时任务
   触发的结果不外发，避免把控制台里的工作内容泄漏到聊天软件。

---

## 2. 支持的平台

### 2.1 总表

| 平台 | 连接方式 | 要填的 | 私聊 | 群 | 卡片按钮 | 单条上限 | 附件下载 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Telegram | Bot API 长轮询 | Bot token | ✓ | @ 机器人 / 回复它 | ✓（适配器支持） | 4096 | 20 MB（Bot API 上限） |
| 飞书 / Lark | 长连接（protobuf 帧） | 版本、App ID、App Secret | ✓ | @ 机器人 | ✓ | 4000 | 图片 |
| 钉钉 | Stream 模式（WebSocket） | Client ID、Client Secret | ✓ | @ 机器人 | ➖ | 5000 | 仅文本 / 富文本 |
| 企业微信 | 智能机器人长连接 | Bot ID、Secret | ✓ | @ 机器人 | ✓ | 4000 | 文件 100 MB、图片 20 MB |
| QQ | 官方机器人 Gateway | App ID、App Secret | ✓ | @ 机器人 | ➖ | 2000 | 仅文本 |
| Discord | Gateway（WebSocket） | Bot token | ✓ | @ 机器人 | ✓ | 2000 | 有 |
| Slack | Socket Mode | App-Level Token、Bot Token | ✓ | @ 机器人 | ✓ | 4000 | 有 |
| 微信（个人号） | — | — | 暂不支持 | — | — | — | — |

「卡片按钮」表示**适配器具备渲染按钮的能力**。当前运行链路只外发正文，提问卡 / 审批卡还没有
接到 IM（见 §15）；按钮回调通道已经就绪，点一个 `data == "stop"` 的按钮等同于发送 `/stop`。

### 2.2 各平台细节

#### Telegram

- **凭据**：`token`（BotFather 给的机器人令牌，密钥字段）。
- **入站**：`getUpdates` 长轮询，一次挂 50 秒；只订阅 `message` 与 `callback_query`。
  update 游标存在 `im_connections.runtime.offset`，重启后从上次位置继续，不会把旧消息再回一遍。
- **@ 判定**：群消息里出现机器人 `@username`、`entities` 里有指向它的 mention，或回复了机器人
  自己发的消息，都算 @；处理时会把 `@username` 从正文里剥掉。
- **媒体**：照片取尺寸最大的一张；视频 / 动图 / 视频留言 / 文档 / 语音 / 音频取第一个。
  文件超过 Bot API 的 20 MB 不会下载，改为把一句说明交给模型。
- **出站**：Markdown 尽量转 Telegram HTML（`<b>` / `<i>` / `<code>` / `<pre>` / `<a>`）；
  解析实体失败时同一段文字退成纯文本再发一次。支持 `sendChatAction` 的「正在输入」。
- **按钮**：`inline_keyboard`（适配器支持，运行链路暂未产生按钮）。
- **注意**：token 在 URL 路径里（`/bot<token>/...`），所以 `main.py` 把 `httpx` 日志压到
  WARNING，异常文本统一走脱敏（见 §12）。`409` 通常表示这个机器人被 webhook 或另一个程序占用。

#### 飞书 / Lark

- **凭据**：`domain`（飞书 / Lark）、`app_id`、`app_secret`（密钥）。
- **连接**：先取 `tenant_access_token`，再建立长连接；帧是手写的 protobuf（varint +
  length-delimited），按服务端 `ClientConfig.PingInterval` 定时发 ping（缺省 90 秒）。
  分片消息按 `(sum, seq)` 拼回。
- **入站**：只处理 `im.message.receive_v1`；私聊是 `p2p`，群聊必须 @ 机器人
  （mention 的 `open_id` 等于机器人，或 key 为 `@_user_1`）。正文里的 `@_user_N` 会剥掉。
- **媒体**：图片通过消息资源接口下载；失败时不静默（纯图片消息正文本来就是空的，
  丢掉图等于整条消息消失），改为追加一句说明。
- **出站**：普通消息用 `text`；最后一段带按钮时用 `interactive` 卡片（最多 5 个按钮）。
- **注意**：长连接 URL 里带 `ticket` / `device_id`，异常文本必须先脱敏再落库。

#### 钉钉

- **凭据**：`client_id`、`client_secret`（密钥）。
- **连接**：走 Stream 模式。先 `POST /v1.0/gateway/connections/open` 拿 endpoint 与 ticket，
  再建 WebSocket；每帧按 `headers` 回 ack，10 分钟收不到任何帧就重连。
- **入站**：订阅 `/v1.0/im/bot/messages/get`。只处理文本 / 富文本；图片消息没有下载通道；
  私聊 key 是发送者 id，群聊 key 是 `conversationId`；群消息用 `isInAtList` 判断 @。
- **出站**：回调里带来 `sessionWebhook`（20 分钟内有效），优先走 webhook 被动回复；
  过期后改用机器人主动发送接口（私聊 `oToMessages/batchSend`，群聊 `groupMessages/send`）。
- **注意**：应用级 token 走 `oapi gettoken`，凭据放在 POST body 而不是 query；
  钉钉没有卡片按钮，提问 / 审批在 IM 里只能靠正文说明。

#### 企业微信

- **凭据**：`bot_id`、`secret`（密钥）。
- **连接**：连 `wss://openws.work.weixin.qq.com`，发 `aibot_subscribe` 订阅；按服务端节奏 ping。
  建连前 `check()` 会真的连一次并订阅，确认凭据可用。
- **入站**：`aibot_msg_callback` 处理消息，`aibot_event_callback` 处理卡片事件；
  `chattype == "single"` 是私聊（key 为 userid），否则群聊（key 为 chatid）。
  支持 image / file / video / mixed 消息；群消息用 `mention_robot` 判断 @。
- **媒体**：下载后用 AES-256-CBC 解密（密钥由平台给出，按 32 字节块补位）。上限：
  文件 100 MB、图片 20 MB。解密工具收在 `im/crypto.py`，适配器不直接依赖 `cryptography`。
- **出站**：有回调 `req_id` 时走 `aibot_respond_msg` 被动回复；否则走 `aibot_send_msg` 主动发送。
  最后一段带按钮时用 `template_card`（最多 10 个选项）。

#### QQ

- **凭据**：`app_id`、`app_secret`（密钥）。
- **连接**：官方机器人 Gateway。先 `POST https://bots.qq.com/app/getAppAccessToken` 换 token，
  再用 `op 2` identify（intents `1 << 25`，覆盖 C2C 与群 @ 消息）；有心跳与 resume。
- **入站**：`C2C_MESSAGE_CREATE` 是私聊，`GROUP_AT_MESSAGE_CREATE` 是群聊（平台只会投递
  @ 机器人的群消息，所以 `mentioned=True`）。只取文本，不下载附件。
- **出站**：优先发 markdown（`msg_type=2`）；平台以业务码 `40034090` 拒绝 markdown 时，
  同一段文字退成纯文本（`msg_type=0`）。被动回复带 `msg_id` + 递增 `msg_seq`。
- **注意**：被动回复有平台时限；`reply window` / `quota` / `超过` / `过期` 这类错误按永久失败处理，
  不重试，用户再发一句即可恢复。

#### Discord

- **凭据**：`token`（密钥）。
- **连接**：Gateway v10，intents 为 `guilds | guild messages | direct messages | message content`
  （`1<<0 | 1<<9 | 1<<12 | 1<<15`）；有心跳与 resume。
- **入站**：忽略机器人自己发的消息；私聊是 `guild_id is None`，群聊必须 mention 到机器人
  （`mentions` 里包含自己的 id），处理时剥掉 `<@id>` / `<@!id>`。附件按 URL 下载，
  按 `content_type` 分成图片 / 文件。
- **出站**：普通消息 + 最后一段最多 5 个按钮（`components`）；支持 typing。

#### Slack

- **凭据**：`app_token`（App-Level Token，`xapp-`）、`bot_token`（Bot Token，`xoxb-`），都是密钥。
- **连接**：Socket Mode。用 app token 调 `apps.connections.open` 拿 WSS URL；收到 `hello`
  后置为 ready；每个 `envelope_id` 必须先回 ack；`disconnect` 事件触发重连。
- **入站**：只处理 `app_mention`（群）与 `channel_type == "im"` 的 `message`（私聊）；
  忽略 `bot_id` 消息，`subtype` 只放行 `file_share`。附件用 `url_private` + Bot token 下载。
- **出站**：`chat.postMessage` 发送 `text` + Block Kit；最后一段带按钮时追加 `actions` 块
  （最多 5 个）。Markdown 的 `**粗体**` 会转成 Slack 的 mrkdwn `*粗体*`。

#### 微信个人号

- 只在平台清单里以 `available=false` 展示，写明「iLink 扫码暂未支持；企业场景请用企业微信」。
- 没有适配器，也不能添加。企业场景请使用企业微信。

---

## 3. 数据模型

IM 只有三张表，都不与 `Conversation` 合并。

### 3.1 `im_connections`：一条连接

| 字段 | 说明 |
| --- | --- |
| `id` | 连接 id |
| `platform` | 平台 id（telegram / feishu / dingtalk / wecom / qq / discord / slack） |
| `account_name` / `account_id` | 连接成功后平台返回的身份，仅用于展示 |
| `fields` | 非密钥字段（App ID、域名、Client ID…） |
| `secrets` | 密钥字段名 → 密文；明文只出现在运行时 |
| `agent_profile_id` | 绑定的智能体；所有私聊都进它，群聊没单独指定时也用它 |
| `enabled` | 是否启用；停用后不再连接 |
| `last_status` | `ready` / `connecting` / `retrying` / `error` / `off` |
| `last_error` | 最近一次失败原因（已脱敏） |
| `runtime` | 适配器的小状态（长轮询游标、上下文令牌…），重启后恢复 |
| `options` | 连接级「发什么」开关：`show_thinking` / `show_tools` / `show_logs` |
| `owner_id` | 所属用户；老库由 `bootstrap.backfill_ownership` 回填给首任管理员 |
| `created_at` / `updated_at` | 时间戳 |

`options`、`runtime`、`last_status`、`last_error` 是后加的可选列，登记在
`im/tables.py` 的 `ADDITIVE_COLUMNS`，由 `db.init_db` 用 `ALTER TABLE ... ADD COLUMN`
幂等补齐（`create_all` 不会给已存在的表加列）。老行的 `options` 一律按「都不发」处理。

### 3.2 `im_peers`：配对过的账号

| 字段 | 说明 |
| --- | --- |
| `connection_id` | 所属连接（`ON DELETE CASCADE`） |
| `sender_id` / `sender_name` | 平台侧发送者 id / 名字 |
| `state` | `pending`（等允许）/ `allowed`（已配对）/ `denied`（拒绝过） |
| `chat_key` / `chat_kind` / `chat_title` | 第一次发起请求时所在的聊天；允许后顺手建出这条聊天 |

配对粒度是 **「连接 + 发送者」**：同一个人在两条连接上要分别配对。配对与聊天无关 ——
同一个人在私聊和群里都算同一个人。

### 3.3 `im_chats`：连接见过的聊天

| 字段 | 说明 |
| --- | --- |
| `connection_id` | 所属连接（`ON DELETE CASCADE`） |
| `chat_key` | 平台侧会话标识；私聊与群各一套 |
| `kind` | `direct` / `group` |
| `title` | 群名 / 对方名字（平台给什么用什么） |
| `agent_profile_id` | 这个聊天单独绑的智能体；空 = 跟随连接的绑定 |
| `conversation_id` | 这个聊天对应的对话（一条聊天固定一条，`ON DELETE SET NULL`） |

三类数据都只是「连接登记」：**IM 不拥有对话**，消息最终写进对话域。

---

## 4. 分层与代码地图

```
app/im/api.py（路由：参数校验 / 归属过滤 / 状态码）
      │
im/service.py（连接登记表 · 配对 · 聊天绑定 · 发送队列 · 启停与重连）
      │                    │
im/bridge.py          im/platforms/*（各平台收发，叶子层）
（与对话域的唯一边界：  ↑ 只依赖 im/base.py、im/format.py、im/crypto.py、
  prepare_run/start_run）  标准库、httpx、websockets
      │
runtime/service.py（对话域：排队、执行、落库）
```

| 文件 | 职责 |
| --- | --- |
| `api.py` | HTTP 接口：平台清单、连接增删改查、重试、配对、聊天绑定；只做校验、归属过滤、状态码 |
| `service.py` | 唯一编排层：连接登记表、启停与重连、配对判断、每聊天串行锁、发送队列、状态落库 |
| `bridge.py` | 与对话域的唯一交界：把 IM 消息写成普通用户消息，跑完把回复带回来 |
| `base.py` | 平台元数据 `PLATFORMS`、消息 / 动作 / 按钮数据类、`ConnectionBase`、重连与脱敏工具 |
| `platforms/*.py` | 各平台适配器，叶子节点 |
| `format.py` | Markdown → Telegram HTML / 纯文本、按平台上限切分 |
| `crypto.py` | 企业微信媒体 AES-256-CBC 解密（`cryptography` 只在这里出现） |
| `tables.py` / `schemas.py` | 数据表与请求 / 响应契约 |

四条硬约束（`backend/tests/test_im_architecture.py` 会拦住违规）：

1. **适配器是叶子节点**：只能依赖 `im/base.py`、`im/format.py`、`im/crypto.py`、标准库、
   `httpx`、`websockets`；不得 import `app.api.*` / `app.runtime.*` / `app.db` / `app.models`
   / `app.collab.*` 等上层或存储模块。
2. **框架层不为具体平台开分支**：`service.py`、`api.py` 里不得出现硬编码的平台 id；
   「某个平台怎么收发消息」是适配器的知识。
3. **IM 碰运行层只允许经 `bridge.py`**：`service.py` 自己 import `app.runtime.*` 会让取消 /
   落库语义在两处各写一份，日后必然漂移。
4. **平台清单是唯一来源**：名字、图标、字段、是否支持群 / 按钮、设置步骤都在
   `base.PLATFORMS`；前端只渲染，不按平台 id 写映射。

---

## 5. 连接生命周期

### 5.1 状态机

| 状态 | 含义 | 显示 |
| --- | --- | --- |
| `connecting` | 正在建立连接 / 握手 | 连接中 |
| `ready` | 已连上，可以收发 | 已连接 |
| `retrying` | 掉线或建连失败，按退避重连 | 重试中 |
| `error` | 永久失败（凭据被拒、平台不支持等），不会再自动重连 | 连不上 |
| `off` | 已停用 / 已关闭 | 未启用 |

- 接口层读状态时，**活着的连接以运行时为准**，否则看库里的 `last_status`。
- 状态会落库（`last_status` / `last_error`），设置页每 5 秒轮询一次。
- 相同 `(state, error)` 不重复写库，避免高频轮询把库写爆。

### 5.2 启动、同步与关闭

- 应用启动：`main.lifespan` 里异步 `im_manager.start_all()`，只连 `enabled=True` 的连接。
  单条连不上只记状态，**不拦服务启动**（IM 是旁路）。
- 设置页新增 / 修改 / 重试：接口调用 `manager().sync(connection_id)` 立刻按库里最新状态
  决定该连接该不该活着，不需要重启服务。`sync` 会处理四种情况：停用 → 停掉；平台没有适配器
  → 停掉并记 `error`；凭据不完整 → 停掉并记 `error`；否则重启这条连接。
- 移除连接：先 `stop()` 关掉运行时，再删库里的行（`api.delete_connection`）。
- 应用关闭：`im_manager.stop_all()` 停掉所有连接，并取消、等待入站任务、状态落库任务与
  节流的运行状态任务真正退出。
- 建连前会先放一个占位句柄：此时发送会明确报「这条连接还没建立起来」，而不是悄悄丢消息。

### 5.3 重连与退避

- 断线重连的等待序列：`0.5, 1, 2, 5, 10, 20, 30` 秒，之后按 30 秒固定节奏重试。
- `AdapterError.permanent=True`（凭据被拒、超出回复窗口、平台明确不支持）→ 停在 `error`，
  把原因显示给用户；其余异常 → `retrying` 后继续重试。
- 每次重连都会推进 `generation`：`stop()` / 换代之后，旧连接的回调一律作废，避免旧读循环
  把状态又写回去。
- 连接级游标（如 Telegram 的 `offset`）由 `RuntimeState` 管理，变化后节流 0.4 秒落库到
  `im_connections.runtime`；重启后原样传回适配器。

---

## 6. 添加连接、配对与聊天绑定

### 6.1 添加：先核对，再保存

1. 选平台 → 表单由 `PLATFORMS` 元数据生成（字段、是否密钥、下拉选项、设置步骤、后台链接）。
2. 填字段 → 选智能体 → 点「连接」。
3. 后端按平台元数据**白名单**规整字段（未知键一律丢掉），再调适配器的 `check(runtime_fields(row))`
   向平台核对一次凭据。
4. **核对不通过不保存**：接口返回 422 和脱敏后的原因；核对通过才落库，并记下平台返回的
   `account_id` / `account_name`。
5. 落库后立刻 `sync()` 起连接。

`available=false` 的平台（微信个人号）会被 422 拒绝；平台没有适配器也拒绝。

### 6.2 凭据怎么存

- 密钥字段按字段加密落库（`app.security.encrypt`，Fernet；密钥来自 `CODA_SECRET_KEY`，
  未配置时用 `data/secret.key`，权限 0600）。
- 接口**永远只回「哪些字段已填」**（`secret_fields: ["token", ...]`），不返回明文、不返回掩码值。
- 更新连接时，密钥字段留空 = 保留旧值；只有真的填了新值才覆盖。
- 移除连接时，`secrets` 随行一起删除。

### 6.3 配对：只认允许过的账号

- 一个账号第一次发消息（私聊，或在群里 @ 机器人）：记一条 `pending`，设置页出现「谁想连接」，
  带平台上的名字。
- 没配对的账号：机器人只回**一次**「已向 Coda 发出连接请求，请在电脑上对应智能体的『设定 → IM』标签里允许」；
  再说也不重复回。
- 点「允许」→ `allowed`，并顺手建出那条聊天；点「拒绝」→ `denied`，之后它发消息只回
  「这个账号的连接请求已被拒绝」。
- 群里没 @ 机器人的消息**完全忽略**：不建配对请求、不打扰、也不进智能体。
- 配对过的账号可以在连接详情里移除，之后它的消息不再进智能体（聊天行保留）。
- 移除连接会一起删掉该连接下的配对与聊天绑定。

### 6.4 聊天绑定与智能体覆盖

- 一条连接绑一个智能体：所有私聊都进它。
- 群聊可以在连接详情里**单独指定**智能体；界面只给群聊露出这个下拉，接口对私聊也接受
  （传 `null` 表示跟随连接）。
- 优先级：`im_chats.agent_profile_id` > `im_connections.agent_profile_id`。
- 连接被停用 / 智能体被删除时，消息会收到明确提示（「还没绑定智能体」/「指定的智能体不存在」），
  不会静默失败。

### 6.5 停用、重试、移除

- **停用**：`enabled=false`，连接立即关闭，设置页状态变「未启用」；重新启用会重新建连。
- **重试**：只在状态卡在 `error` 时出现，效果是 `sync()` 重启这条连接。
- **移除**：停掉连接，删除配对账号、聊天绑定与连接本身；凭据随之消失。
- 设置页每 5 秒轮询连接与配对请求，掉线 / 重连不需要手动刷新。

---

## 7. 入站：消息怎么进智能体

### 7.1 处理顺序

```
适配器读循环
  └─ handlers.on_message(message)
       └─ service 丢到后台任务（读循环不能等一轮跑完）
            ├─ 1. 找连接；不存在就丢弃
            ├─ 2. 查配对状态：不是 allowed → 建 pending / 回拒绝提示 → 结束
            ├─ 3. 群消息且没 @ 机器人 → 完全忽略
            ├─ 4. 命令：/stop、/start、/help → 处理并结束
            ├─ 5. 正文、图片、文件全空 → 忽略
            ├─ 6. 找 / 建这条聊天（im_chats）
            ├─ 7. 平台不支持群聊（微信）→ 回「这个平台只支持私聊」
            ├─ 8. 没有可用智能体 → 回「还没绑定智能体」
            ├─ 9. 每聊天一把锁：上一条还在跑就先回「排在后面」
            └─ 10. bridge.dispatch_message → 跑完把回复发回来源聊天
```

关键点：

- **读循环绝不等一轮运行**：一轮可能跑很久；等它会把同一个机器人上其它聊天的消息、
  以及 `/stop` 都堵住。`on_message` 只把活丢到后台，排队交给每聊天一把锁。
- **锁的键是「连接 + 聊天」**，不是「连接」：同一个机器人上的不同聊天可以各跑各的。
- 上一条还在跑时，用户会先收到一句「上一条还在处理，这条排在后面。」，然后这条消息等锁。
- 运行过程中，平台支持的话会持续发「正在输入」（每 4 秒一次；目前 Telegram / Discord 实现）。

### 7.2 命令

命令只匹配**整条消息**（大小写不敏感，允许带 `@机器人名` 后缀）：

| 命令 / 情况 | 行为 |
| --- | --- |
| `/stop` | 停下这个聊天正在跑的那一轮；回「已停下。」或「现在没有在做的活。」 |
| `/start`、`/help` | 回一句「你好，我是 <智能体名>。直接说话就行，/stop 可以停下正在做的事。」 |
| 未绑定智能体 | 回「这个机器人还没绑定智能体，请在设置里选一个。」 |

命令同样要求**已配对**；没配对的账号发 `/stop` 只会触发配对请求。

### 7.3 附件

- 一条消息最多处理 **8 个**附件（`MAX_ATTACHMENTS`），单个上限 **8 MB**
  （`attachments.MAX_ATTACHMENT_BYTES`）。
- 图片与文件统一写进这条对话工作目录的 `attachments/`；提示词里附一份相对路径清单。
- 只有附件没有正文时，正文写成「（收到附件，请查看）」。
- 附件过大 / 写入失败：直接把原因发回聊天，这条消息不进智能体。
- 平台侧的下载上限见 §2.1；平台下载失败时，适配器会把一句说明并入正文交给模型，
  而不是让整条消息消失。

### 7.4 失败怎么告诉用户

- 一轮失败（`ImReplyError`，如智能体已停用 / 附件过大）：把原因发回聊天。
- 其它异常：回「这一轮没能完成：<脱敏原因>」，并记一条 warning 日志。
- 一轮被取消且没有任何正文：回「（这一轮被中断了）」。
- 发送本身失败不会拖垮连接：只记 warning，后续消息继续处理。

---

## 8. 与对话域的桥（`bridge.py`）

这是 IM 与对话域的**唯一交界**。`service.py` 不 import `app.runtime.*`。

### 8.1 一条聊天 → 一条对话

- 先按 `(connection_id, chat_key)` 找 `im_chats`，没有就建。
- `ensure_conversation` 的规则：
  - 已有对话、未归档、且 `agent_profile_id` 与当前目标智能体一致 → 继续用；
  - 智能体变了 / 旧对话已归档 → 另开一条新对话（`Conversation` 是智能体快照，
    旧对话仍属于旧智能体，沿用只会把新的一轮带偏）；
  - 新对话标题：`<平台名> · <聊天标题或 chat_key>`，截断到 120 字符；
  - 新对话的 `owner_id` 继承连接的所有者。
- 标题还会参与对话域的自动起标题：`prepared.title_pending` 时调度 `titles.schedule`。

### 8.2 消息怎么变成一轮运行

```python
SendMessage(content=..., attachments=[相对路径...])
prepared = await prepare_run(session, conversation.id, payload)
start_run(prepared)
```

- 与网页 / 手机发消息**同一条运行链路**：排队、取消、工具调用、落库、事件协议全部一致。
- IM 这一层不知道任何智能体、内核、网关的细节。
- `dispatch_message` 跑完返回一个 `ReplySink`（待发送的文本 + 这一轮的终态），由 `service`
  发回来源聊天。

### 8.3 取消

`bridge.cancel_conversation(conversation_id)` 调 `registry.cancel_for_conversation`。
`/stop` 只经这个函数碰运行层，和 `dispatch_message` 同一条边界。

---

## 9. 出站：什么发回聊天软件

### 9.1 默认只发正文

- 默认只发**这一轮运行的正文**：`message.delta` 拼起来的文本。
- 只发给**发起这一轮的那个聊天**；别的来源触发的轮次不外发。

### 9.2 过程内容是连接级开关

在连接详情里逐条开关，默认全关：

| 键 | 内容 | 呈现 |
| --- | --- | --- |
| `show_thinking` | 思考过程 | `> 💭 …` |
| `show_tools` | 工具调用 / 结果 | `> 🔧 调用 <工具名>`、`> ↩️ <输出>`、`> ⚠️ <错误>` |
| `show_logs` | 日志 | `> · <info>` / `> ⚠️ <warn/error>` |

- 打开后，过程内容以引用块附在正文之后，与回答明确分开；没有正文时它就是全部内容。
- 每条过程内容会压成一行并截断到 **400 字符**（`TRACE_LINE_LIMIT`），避免一个工具输出
  把整条消息撑爆；正文仍按平台上限完整发送。
- 开关是**连接级**的：同一个平台可以接两条连接，一条安静、一条详细。
- 新增开关要在 `base.OPTION_KEYS` 登记；接口与两端界面都按它读写，未声明的键不会进库。

### 9.3 切分与格式

- 单条长度上限按平台切分：Telegram 4096、Discord 2000、QQ 2000、Slack 4000、
  飞书 / 企业微信 4000、钉钉 5000。
- 通用切分尽量在换行 / 空格处断开；Telegram 额外做代码块感知切分，单块超限时按行切开并
  各自补回围栏，其它平台用通用切分（优先保持段落完整，不专门处理围栏）。
- 各平台的格式转换：
  - Telegram：Markdown → HTML，解析失败退纯文本；
  - Slack：`**粗体**` → `*粗体*`；
  - 其它平台：发 Markdown 原文或平台自己的富文本结构。
- 空正文（这一轮没有任何可发内容）不发消息。

### 9.4 发送队列与重试

- 每条连接一个发送队列，**同一聊天按顺序发**；不同聊天互不阻塞。
- 每次发送最多尝试 **4 次**：平台给了 `Retry-After` 就按它等，否则按重连退避序列等。
- 平台明确拒绝（凭据失效、超出回复窗口）：**不重试**，把原因记下来。
- 网络类失败尝试 4 次仍失败：同样记原因；**一条发不出去的消息不会卡住后面的**。
- 连接关闭时，排队中的与正在发送的消息都会明确失败（409），调用方不会悬空等待。

---

## 10. HTTP 接口

全部挂在 `/api` 下，路由前缀 `/im`。除特别说明外都要求登录；非管理员只能看到 / 操作
自己名下的连接（`owner_id`），否则按 404 处理（不暴露「存在但无权」）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/im/platforms` | 可连接平台清单（添加表单由它生成） |
| GET | `/api/im/connections` | 列出连接（含状态、配对 / 聊天计数、已填密钥字段名） |
| POST | `/api/im/connections` | 新建连接；**保存前先向平台核对凭据** |
| PATCH | `/api/im/connections/{id}` | 改绑定智能体 / 字段 / 开关 / 启停；显式 `null` 表示解绑 |
| DELETE | `/api/im/connections/{id}` | 移除连接（连同配对与聊天绑定） |
| POST | `/api/im/connections/{id}/retry` | 重新连一次（状态卡在 error 时用） |
| GET | `/api/im/peers` | 配对请求与已配对账号；可按 `state` / `connection_id` 过滤 |
| POST | `/api/im/connections/{id}/peers/{peer_id}` | 允许 / 拒绝一个配对请求；允许时顺手建聊天 |
| DELETE | `/api/im/connections/{id}/peers/{peer_id}` | 移除一个已配对账号 |
| GET | `/api/im/connections/{id}/chats` | 这条连接见过的聊天 |
| PATCH | `/api/im/connections/{id}/chats/{chat_id}` | 给聊天单独指定智能体；传 `null` 跟随连接 |
| DELETE | `/api/im/connections/{id}/chats/{chat_id}` | 移除聊天绑定（不删对话本身） |

接口层只做参数校验、归属过滤与状态码；连接的启停与消息路由都在 `service.py`。

主要错误码：

- `401`：未登录 / 令牌失效；
- `404`：连接 / 配对 / 聊天不存在，或不属于当前用户；
- `422`：平台不支持 / 凭据没填全 / 平台核对没通过 / 智能体不存在或无权。

> `409`（连接已断开、这条回复没发出去）是 IM 服务层 `ImError` 的语义，出现在发送队列与
> `/stop` 这类内部路径上，不是这些 HTTP 接口直接返回的状态码。

---

## 11. 前端

### 11.1 桌面端

- 每个智能体设定页的「IM」标签（`frontend/src/pages/AgentEditPage.tsx` → `components/AgentImSettings.tsx`，`/agents/{id}?tab=im`；设置页不再有 IM 标签）。
- 打开时并发拉取：平台清单、连接、配对请求、智能体列表；然后逐个拉每条连接的聊天。
- 连接状态是活的：每 5 秒轮询连接与配对请求；轮询失败只提示一次，不反复弹窗。
- 页面结构：
  - 「谁想连接」：`pending` 配对请求，带平台名、私聊 / 群聊标记、允许 / 拒绝；
  - 「我的 IM 机器人」：每条连接一张卡，显示平台、账号名、状态、绑定的智能体、配对 / 聊天计数、
    待允许数、已填密钥数；可停用 / 启用、详情、重试（仅 error）、移除；
  - 详情里：改智能体、三个过程开关、已配对账号（可移除）、聊天列表（群聊可单独改智能体）、
    移除聊天绑定；
  - 平台网格：可用平台点「连接」打开添加弹层；不可用平台置灰并显示原因。
- 添加弹层：表单完全由平台元数据生成（下拉 / 文本框 / 密码框），顶部展示设置步骤与后台链接；
  保存前由后端核对，失败原因显示在弹层里。

### 11.2 移动端

- `frontend/src/mobile/pages/MobileAgentEditPage.tsx`，路由 `/m/agents/{id}`，入口在智能体列表的「设定」；
  旧路由 `/m/im` 重定向到智能体列表。
- 功能与桌面端对齐，交互按触屏重做：列表 + 底部弹层。
- 配对请求放最上面（「现在要动的东西」）；连接详情同样可以改智能体、开关过程内容、
  管理配对账号与聊天。

### 11.3 类型与 API 客户端

- 类型：`frontend/src/lib/types.ts` 的 `ImPlatform` / `ImConnection` / `ImPeer` / `ImChat`。
- 请求：`frontend/src/lib/api.ts` 的 `listImPlatforms` / `listImConnections` /
  `createImConnection` / `updateImConnection` / `deleteImConnection` / `retryImConnection` /
  `listImPeers` / `answerImPeer` / `removeImPeer` / `listImChats` / `updateImChat` / `removeImChat`。

---

## 12. 安全与归属

### 12.1 凭据

- 加密落库（Fernet），接口只回字段名；明文只出现在运行时内存里。
- 字段读写按 `PLATFORMS` 白名单：未知字段直接丢弃，防止把任意内容写进库。
- 更新时密钥留空 = 保留旧值，避免「改个智能体却把 token 清空」。

### 12.2 日志与异常脱敏

- `main.py` 把 `httpx` 日志压到 WARNING：Telegram 的 token 在 URL 路径里，钉钉 / 飞书的凭据
  也曾以 query 形式出现，INFO 级别会把完整 URL 打进日志。
- 所有会落库或回给前端的异常文本都走 `scrub()`：
  - query 里的 `ticket` / `device_id` / `access_token` / `appsecret` / `secret` → `***`；
  - 形如 `scheme://<用户名>:<口令>@host` 的 userinfo → `***@`；
  - `/bot<id>:<token>/` 路径 → `/bot***/`。
- `ConnectionBase.status()` 在回调前再脱敏一次；`reason_of()` 统一截断到 300 字符，
  空文本回落到异常类名。

### 12.3 归属

- 连接有 `owner_id`；管理员可看全部，普通用户只看自己的。
- 配对与聊天通过所属连接间接归属。
- 鉴权关闭期间创建的连接 `owner_id` 为空；打开鉴权后 `backfill_ownership` 会回填给
  首任管理员，不会出现「永远看不到、也没人能接管」的连接。

### 12.4 不把敏感内容带回聊天

- 只有 IM 发起的轮次才外发；网页 / 手机 / 定时任务的结果不会进聊天软件。
- 过程内容默认关闭；打开后只是引用块里的截断文本，不是完整控制台输出。
- 聊天软件里不能填密钥、改设置；配对与绑定只能在控制台操作。

---

## 13. 测试与架构守卫

### 13.1 `backend/tests/test_im.py`

用假适配器（`FakeAdapter` / `FakeHandle`）替换真实平台，验证框架侧行为，不连外网：

- 平台元数据形状、可用平台都有适配器；
- Telegram HTML / 纯文本对齐、切分不超限、代码围栏保持完整；
- 连接 CRUD：密钥只回字段名、保存前核对、核对失败不落库、不可用平台拒绝；
- 配对：没配对的账号只回一次请求、允许后消息进智能体、拒绝后不转发；
- 群消息：没 @ 机器人就完全忽略（配对前 / 配对后都一样）；
- 失败要给原因而不是静默；`/stop` 能停掉这个聊天正在跑的那一轮；
- 换绑智能体另开新对话；
- 过程内容默认关闭、按连接开关生效、压平并截断到 400 字符、部分更新只改传入的键；
- 脱敏：异常文本里的 ticket / token / bot 路径不泄漏；状态上报前也脱敏；
- 发送队列关闭时正在发的 future 会被收尾；主动停机不会被误报成掉线。

### 13.2 `backend/tests/test_im_architecture.py`

分层守卫，失败通常意味着有人在框架层为某个平台开分支，或适配器反向依赖了上层：

- 平台适配器不依赖禁止的上层模块；
- `service.py` / `api.py` 不硬编码平台 id；
- 只有 `bridge.py` 碰 `app.runtime.*`；
- 前端 IM 组件不按平台 id 写映射；
- 每个可用平台都有适配器；
- 平台元数据有名字、字段 label 是中文、可用平台有字段；
- 过程开关只在 `base.py` 声明一次，脏数据按「都不发」；
- `im_connections` 的后加列都登记在 `ADDITIVE_COLUMNS`。

### 13.3 其它

- `backend/tests/test_ownership.py`：IM 连接的归属过滤与老库回填。
- `backend/tests/test_settings_page.py`：IM 收在智能体设定页的「IM」标签（`AgentImSettings`），设置页不再有 IM 标签。

---

## 14. 接入一个新平台

1. 在 `backend/app/im/base.py` 的 `PLATFORMS` 加一条 `Platform(...)` 元数据：
   id、名字、图标、字段（label 用中文、密钥字段标 `secret`）、是否支持群 / 按钮、
   设置步骤、后台链接。设置页的表单由它生成，**不要改前端**。
2. 在 `backend/app/im/platforms/` 新建模块，实现：
   - `check(fields)`：先向平台核对一次凭据，返回 `Account`；
   - `connect(fields, handlers, runtime)`：返回一个 `ConnectionBase` 子类；
   - 子类实现 `send(chat, message)`，按需覆盖 `typing()` / `open()`。
3. 在 `platforms/__init__.py` 的 `ADAPTERS` 注册。
4. 长连接 / 长轮询的游标放进 `RuntimeState`（框架会节流落库到 `im_connections.runtime`），
   不要自己写文件。
5. 只使用客户端主动发起的连接（长轮询 / WebSocket / Socket Mode / Gateway）；
   **不接需要公网回调地址的接口**。
6. 加测试：参考 `tests/test_im.py`（假适配器验证框架行为）与
   `tests/test_im_architecture.py`；可用平台必须在 `ADAPTERS` 里。

新增平台的完整检查清单见 `AGENTS.md` §4.9。

---

## 15. 不做什么，以及已知限制

### 15.1 明确不做

- IM 机器人不进智能体名单，不派活（那是协作域的事）。
- 不让没配对的人和智能体说话；不把群里其他人的话转给智能体。
- 不在聊天软件里填密钥、改设置。
- 不接需要公网回调地址的接口。
- 不把网页 / 手机 / 定时任务的结果外发到聊天软件。

### 15.2 已知限制

- **微信个人号暂不支持**（扫码依赖未公开的第三方协议）；企业场景请用企业微信。
- **只有服务运行时才收发**：电脑关机或睡眠时收不到；服务重启后连接自动重连。
- **回复不是流式的**：这一轮跑完才发最终消息，长任务期间只有「正在输入」。生成的图片 / 文件
  留在工作目录，不会自动发回聊天软件。
- **单进程假设**：连接登记表在内存里。不要用多 worker / 多副本同时跑同一个数据库，
  否则多个进程会各自长轮询，消息可能重复处理。
- **被动回复窗口**：QQ / 钉钉 / 企业微信的被动回复有平台时限，超时后用户再发一句即可恢复。
- **钉钉 / QQ 没有卡片按钮**：提问 / 审批用正文说明；当前运行链路本身也只外发正文，
  提问卡 / 审批卡还没有接到 IM（按钮回调通道已就绪，`data == "stop"` 等同 `/stop`）。
- **过程内容是连接级开关**，不是全局：同一个平台可以接两条连接，一条安静、一条详细。
- **附件上限**：一条消息最多 8 个、单个 8 MB；平台自身的下载上限可能更低
  （Telegram 20 MB，企业微信文件 100 MB / 图片 20 MB）。钉钉只处理文本 / 富文本，QQ 只取文本。
- **群聊只看 @**：群里其他人的消息不会进智能体。Slack 适配器只处理 `app_mention` 与私聊
  `message`；Discord 与其它平台在服务层统一过滤。
- **配对按「连接 + 发送者」**：同一个人在两条连接上要分别配对。
- **聊天绑定界面只给群聊露出**：接口对私聊也接受单独指定，但桌面 / 移动界面只对群聊显示下拉。

### 15.3 排错

| 现象 | 先看什么 |
| --- | --- |
| 状态一直是「连接中」 | 平台是否可达；网络代理；长连接是否被企业网络拦 |
| 状态「连不上」 | 连接卡上的 `error` 文案（已脱敏）；凭据是否过期 / 权限是否开通 |
| Telegram `409` | 这个机器人是否被 webhook 或另一个程序占用 |
| 发消息没反应 | 账号是否已配对；群里是否 @ 了机器人；服务是否在运行；智能体是否已停用 / 删除 |
| 收到「已发出连接请求」 | 去对应智能体的「设定 → IM」标签点「允许」 |
| 收到「上一条还在处理」 | 同一聊天串行，等这一轮结束；需要停就发 `/stop` |
| 消息发出去了但智能体没收到 | 看服务端 warning 日志；确认消息不是纯空文本且附件没有超限 |
| 聊天里只有正文，没有思考 / 工具 | 过程开关默认关闭；在连接详情里逐项打开 |
| 换绑智能体后上下文断了 | 预期行为：一条聊天固定一条对话，换绑会新开一条 |
| 重启后重复收到旧消息 | 目前只有 Telegram 把 update 游标落库；其它平台是否重放取决于平台自身 |
