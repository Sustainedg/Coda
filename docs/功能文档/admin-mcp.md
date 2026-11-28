# 系统管理 MCP 服务器

`scripts/mcp/coda_admin_mcp_server.py` 是平台自带的「平台通用」MCP 服务器：启动时会自动播种到「MCP」页（每个用户一条「系统管理」，只在该用户自己的列表里出现），把它分配给某个智能体后，**在对话里就能让 AI 直接管理这个平台** —— 查看与创建智能体、编排协作组、安装 / 删除技能、增删改 MCP 服务器。

它调用的是平台自己的 REST API，权限完全等同于**配置里那个令牌的主人**：PAT 属于谁，工具就只能看到 / 动到谁的资产（管理员的全局可见）。平台不因它放宽任何校验。

## 1. 零配置：平台自动填写地址与令牌

启动播种后，「MCP」页会出现一条「系统管理」（来源显示「平台内置」），**地址与令牌已经自动填好**，
不需要你去「账号」页生成令牌，也不需要手填平台地址。连接方式（启动命令 / 脚本参数）同样由平台维护。

* **平台地址**：默认按 `http://127.0.0.1:<CODA_PORT>` 填写；部署在反向代理或容器网络后面时，
  用 `CODA_PUBLIC_BASE_URL=https://你的域名` 指定 MCP 子进程实际能访问到的地址。
* **平台令牌**：启动 / 注册时自动生成一份**系统管理专用**的长效令牌（`pat_` 前缀），
  与「设置 → 账号」里你自己的个人访问令牌**互不影响**。平台只存哈希，明文加密写进这条内置配置。
  令牌失效时**当场补发**一份新的：用户的凭据一变（重新生成 / 吊销 PAT、改口令、管理员重置口令），
  或配置里的令牌被删改，平台立刻换一份新的写回配置 —— 不用等平台重启，系统管理 MCP 不会中途 401。
  仍然有效的令牌不会被多此一举地轮换（重新生成 PAT 不会把系统管理 MCP 一起掀了）。

## 2. 权限模式

「MCP」页点「系统管理」→「编辑」，只需要关心权限模式（地址与令牌显示为「平台托管」，只读）。

默认 `CODA_MODE=ro`（只读，推荐）：需要写操作时再改成 `rw`。不想让任何智能体用它时，
停用即可；内置服务器不提供删除，下次启动也不会重复播种。

| 环境变量 | 说明 |
| --- | --- |
| `CODA_BASE_URL` | 平台地址（平台自动填写；带不带 `/api` 都可以，自动归一） |
| `CODA_TOKEN` | 系统管理专用令牌（平台自动生成并加密保存） |
| `CODA_MODE` | `ro`（内置默认，只读——写操作直接拒绝，连平台都不碰）/ `rw`（可读写） |
| `CODA_TIMEOUT` | 单次请求超时秒数（默认 60） |

配置值经平台加密落库，运行时由内核注入进程，不会出现在日志或对话里。

## 3. 工具清单（22 个）

| 域 | 工具 |
| --- | --- |
| 智能体 | `agent_list` · `agent_get` · `agent_create` · `agent_update` · `agent_set_skills` · `agent_set_mcp` · `agent_delete` |
| 协作组 | `team_list` · `team_get` · `team_create` · `team_update` · `team_delete` |
| 技能 | `skill_list` · `skill_get` · `skill_rescan` · `skill_delete` |
| MCP | `mcp_list` · `mcp_create` · `mcp_update` · `mcp_delete` |
| 辅助 | `agent_types_list` · `gateway_list`（创建智能体前先查 type_id / gateway_id） |

设计约定：

- **写操作都要 `confirm: true`** —— 删除类破坏性动作必须是模型的显式决定，不能顺嘴一说就执行；
- **列表只回摘要**（id / 名字 / 状态），详情按需再查 —— 防一次调用把几十 KB 灌进上下文；
- `skill_get` 的 SKILL.md 正文超 2000 字截断；
- 401 时错误信息会提示：内置服务器由平台自动补发令牌（令牌失效时当场换新，不必重启平台）。

## 4. 安全注意

- 这个服务器等于把你的账号权限交给对话里的 AI。**只分配给你信任的智能体**（平台"严格仅分配"机制是第一道闸门，`CODA_MODE=ro` 是第二道）；
- 别让它把挂在自己身上的这个 MCP 服务器解除分配（`agent_set_mcp` 的工具描述里已提示）；
- 系统管理专用令牌由平台维护；如需轮换，删除这条内置配置的令牌值后保存即可 —— 平台当场补发一份新的，不必重启。

## 5. 测试

`backend/tests/test_coda_admin_mcp.py`（配置归一 / ro 拦截 / confirm 拦截 / 假平台端到端），PAT 机制在 `backend/tests/test_pat.py`。

令牌联动在 `backend/tests/test_pat.py`：

| 用例 | 验证什么 |
| --- | --- |
| `test_pat_revoked_on_admin_password_reset` | 管理员重置口令：旧的内置令牌立即失效，新的当场补发并可用 |
| `test_builtin_mcp_token_refreshed_when_user_pat_rotates` | 用户重新生成 PAT：内置 MCP 里那份自动换成新的，立刻可用 |
| `test_builtin_mcp_token_kept_when_still_valid` | 令牌还有效时不多此一举地轮换（重新生成 PAT 不掀掉系统管理 MCP） |
| `test_builtin_mcp_token_reissued_on_edit_without_restart` | 删掉令牌值后保存：当场补发，不必重启平台 |
