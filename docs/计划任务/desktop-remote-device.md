# 桌面端免登录 + 连接远程设备设计方案

> 状态：**方案已成型，待开工**。两个前提待确认（见 §11）：远程设备形态、旧桌面数据是否多用户。
> 相关约束：`AGENTS.md`（铁律与分层）、`docs/架构/architecture.md`（运行时序）、
> `docs/说明/desktop.md`（桌面壳长期约定）。
> 需求来源：桌面端不需要注册与登录，但需要增加「连接远程设备」的能力。

---

## 0. 一句话结论

把需求拆成两条**互不混用**的链路：

1. **本机模式免登录**：桌面壳每次启动生成一个不可见的一次性本机令牌，后端照常开鉴权，
   自动准备一个「本机用户」作为数据归属。界面不再出现注册 / 登录 / 账号管理。
2. **连接远程设备**：远端 Coda 生成一次性配对码，桌面输入「地址 + 配对码」换取长效设备令牌，
   之后窗口**直接加载远端 Web UI**。SSE、附件、图片、下载全部同源，不做 API 代理。

**不改多用户体系本身**：网页 / Docker / 移动端的注册登录照旧，只让桌面本机形态绕开它。
**不新增依赖**：设备令牌复用现有 sha256 + 随机熵方案，令牌保存用 Electron 自带 `safeStorage`。

---

## 1. 需求与边界

### 1.1 用户看到的目标

| 场景 | 目标体验 |
| --- | --- |
| 首次安装桌面端 | 打开即是主界面，没有注册 / 登录页 |
| 日常使用本机 | 数据在本机 `userData/data` 里，不连外网也能用 |
| 连接远程设备 | 设置 / 托盘 / 标题栏入口 → 输入地址与配对码 → 进入远端 Coda |
| 多台远程设备 | 保存连接配置，随时切换 |
| 远端撤销设备 | 桌面侧提示「设备连接已失效」，可重新配对或切回本机 |

### 1.2 边界

- **远程设备 = 另一台运行 Coda 后端并对外提供 Web UI 的机器**（Docker / NAS / 服务器）。
  桌面本机后端仍固定监听 `127.0.0.1`，默认不作为远程主机。
- 桌面连桌面（桌面端作为被连接方）**不在本期范围**，需要额外开放监听地址与设备鉴权，
  单独立项（见 §11）。
- 网页、Docker、移动端 `/m/` 的行为不变；桌面端的「免登录」只作用于本机模式。

---

## 2. 现状盘点（实现前的事实基线）

| 现状 | 位置 | 对本需求的影响 |
| --- | --- | --- |
| 桌面壳启动后端未传鉴权 env，`CODA_AUTH_ENABLED` 走默认 `true` | `desktop/src/main/backend.ts` | 桌面首启必然落到登录 / 注册页 |
| `AuthGate` 在没有令牌且 `auth_enabled=true` 时渲染 `LoginScreen` | `frontend/src/components/AuthGate.tsx` | 本机免登录要绕过这条分支，但仍要有合法身份 |
| 后端固定监听 `127.0.0.1` | `desktop/src/main/backend.ts` | 远程模式必须连远端，桌面本机不能当被连接方 |
| 资产按 `owner_id` 归属；对话严格归属校验 | `backend/app/auth_deps.py`、`api/conversations.py` | 直接关鉴权会让老桌面用户的对话“变成无主” |
| 内置 MCP 按用户播种；`auth_enabled=false` 时走共享归属 | `backend/app/runtime/mcp.py` | 粗暴关鉴权会改变内置 MCP 的播种口径 |
| 前端令牌存 `localStorage['coda.auth.token']`，请求统一取 Bearer | `frontend/src/lib/auth.ts`、`lib/api.ts` | 桌面可以通过 preload 在页面脚本执行前注入令牌 |
| `<img>` 等资源靠查询参数带令牌 | `lib/api.ts`、`lib/avatar.ts`、`lib/agentAvatar.ts` | 远程 UI 同源加载时天然可用，不必改路径 |
| 桌面壳已用 `contextIsolation + sandbox + nodeIntegration=false` | `desktop/src/main/index.ts` | 加载远端页面有基础安全边界，但还需要 origin 白名单 |
| 窗口 `setWindowOpenHandler` 目前只允许 `127.0.0.1` | `desktop/src/main/index.ts` | 远程模式下要放行当前远端 origin，其余交系统浏览器 |

---

## 3. 关键决策（评审基线，不要反复）

| # | 决策 | 说明 |
| --- | --- | --- |
| D1 | **不全局关闭鉴权** | 桌面本机用「不可见本机会话令牌」；后端仍要求 Bearer，loopback 接口不被同机进程裸调 |
| D2 | **自动准备本机 owner** | 空库自动建内部管理员「本机」；有用户时取最早启用的管理员。老数据继续按原归属可见 |
| D3 | **远程模式直接加载远端 UI** | 不把本地 UI 的 API Base 指向远端，也不在壳内反代。避开 CORS、SSE、附件相对路径、下载与版本兼容的一串坑 |
| D4 | **设备配对码优先，令牌粘贴兜底** | 主路径是远端生成一次性配对码；高级用户仍可直接粘贴 PAT / 设备令牌 |
| D5 | **远端仍保留多用户体系** | 配对码由远端已登录用户生成；桌面端只是把「登录」换成「配对」 |
| D6 | **本机模式隐藏账号入口** | 桌面本机不显示「账号 / 用户 / 退出登录」；移动端与网页保持不变 |
| D7 | **不新增依赖** | 设备令牌复用 `secrets` + `hashlib`；桌面令牌保存用 Electron `safeStorage` |

---

## 4. 本机模式：无感登录

### 4.1 启动链路

```
Electron 主进程
  ├─ 随机生成 32 字节本机会话令牌（每次启动重新生成，不落盘）
  ├─ spawn 本地 uvicorn，附加 env：
  │    CODA_DESKTOP_LOCAL=1
  │    CODA_DESKTOP_SESSION_TOKEN=<令牌>
  │    CODA_REGISTRATION_OPEN=false
  ├─ 健康检查
  └─ loadURL(127.0.0.1:<port>)
       └─ preload 向主进程同步取「当前 origin 对应令牌」
            └─ localStorage['coda.auth.token'] = <令牌>
                 └─ AuthGate 带令牌请求 /api/auth/me → 直接进入
```

### 4.2 后端改动

**配置**（`backend/app/settings.py`）：

```python
desktop_local: bool = False
desktop_session_token: str | None = None
```

**本机 owner**（`backend/app/bootstrap.py` 新增 `ensure_desktop_owner()`）：

- 仅 `desktop_local=1` 时执行；
- 有用户：取最早启用的管理员；
- 空库：自动创建内部管理员（显示名「本机」，口令为随机不可用值）；
- 幂等，重启不会重复创建；
- 在启动时序里放在 `seed_if_empty` 之前、`backfill_ownership` 之前，
  让播种出来的网关 / 智能体也能回填到本机 owner 名下。

**鉴权依赖**（`backend/app/auth_deps.py`）：

```python
# 常量时间比较，命中后返回本机 owner（没有用户时回退 _SYSTEM_USER）
if settings.desktop_session_token and secrets.compare_digest(token, settings.desktop_session_token):
    return await local_owner(session)
```

`owner_id_for()` 同步走本机 owner，保证资产归属与旧数据一致。

### 4.3 前端表现

- `AuthConfig` 增加 `desktop_local: bool`；
- 桌面本机模式：
  - 不显示注册 / 登录页（preload 已注入令牌）；
  - 隐藏「设置 → 账号 / 用户」；
  - 侧边栏不显示「退出登录」，底部显示「本机」；
- 移动端、网页、Docker 部署完全不变。

### 4.4 为什么不用 `CODA_AUTH_ENABLED=false`

| 直接关鉴权的后果 | 本方案如何避免 |
| --- | --- |
| 老桌面库里 `owner_id` 指向真实用户的对话，被 `require_owned_strict` 判成无主 | 自动准备本机 owner，按原归属继续可见 |
| `127.0.0.1` 对同机其他进程可达，接口变成无鉴权 | 仍校验本机会话令牌 |
| 内置 MCP 的「按用户播种」口径被改成共享 | 鉴权保持开启，播种逻辑不动 |

---

## 5. 远程设备：配对 + 直接加载远端 UI

### 5.1 总体流程

```
远端 Coda（Docker / NAS）
  └─ 用户登录 Web UI → 设置 → 设备连接 → 生成配对码
        （显示地址、一次性码、有效期）

桌面端
  └─ 托盘 / 标题栏 / 账号菜单 → 连接远程设备
        → 输入地址 + 配对码
        → GET  /api/device/info        确认「这确实是个 Coda」
        → POST /api/device/pair        换取 dev_ 长效令牌
        → 保存连接配置（令牌用 safeStorage 加密）
        → loadURL(远端地址)
             └─ preload 按 remote origin 注入令牌
                  └─ 远端 AuthGate 直接进入主界面
```

### 5.2 远端数据模型

新表由 `create_all` 自动建，老库无痛：

```text
device_links
  id
  owner_id            # 可空；远端鉴权关闭时为 NULL
  name                # 设备名（桌面计算机名 / 用户可改）
  platform            # win32 / darwin / linux
  token_hash          # sha256，唯一索引；明文只在配对响应出现一次
  created_at
  last_seen_at
  revoked_at

device_pairing_codes
  id
  owner_id            # 可空
  code_hash           # sha256，唯一索引
  created_at
  expires_at
  used_at
```

### 5.3 远端接口

| 接口 | 鉴权 | 作用 |
| --- | --- | --- |
| `GET /api/device/info` | 公开 | 返回 `{app, version, auth_enabled, pairing_supported}`，桌面用来校验目标身份 |
| `POST /api/device/pairing-codes` | 登录用户 | 生成一次性配对码；TTL 建议 5 分钟 |
| `POST /api/device/pair` | 公开 + 限流 | 校验配对码，签发 `dev_` 令牌与设备记录 |
| `GET /api/device/links` | 登录用户 | 列出已连接设备 |
| `DELETE /api/device/links/{id}` | 登录用户 | 吊销设备令牌 |

`current_user` 新增 `dev_` 分支：

- 按 `token_hash` 查 `device_links`；
- `revoked_at` 非空 → 401；
- 按 `owner_id` 返回用户；为空返回 `_SYSTEM_USER`；
- `last_seen_at` 做节流更新（例如 5 分钟一次），避免每请求写库；
- 在 `request.state` 标记令牌类型，供敏感接口做限制。

### 5.4 桌面连接管理器

本地后端常驻，新增 `/connect` 独立页面（不进主壳、不依赖 AuthGate）：

- 远程地址输入 + 配对码输入；
- 「测试连接」：校验 `/api/device/info`、版本、鉴权形态；
- 配对成功保存 profile；
- profile 列表：本机 / 多台远程设备；切换、改名、删除；
- 状态提示：连不上 / 不是 Coda / 版本过旧 / 令牌失效 / 需要配对。

入口：

| 入口 | 说明 |
| --- | --- |
| 托盘菜单 | 「连接远程设备…」「切回本机」；远程模式下常驻 |
| 标题栏 | 桌面壳注入的「设备」小按钮，打开连接管理器 |
| 设置页 | 桌面本机的「设备连接」标签 |
| 账号菜单 | 「连接远程设备…」 |

### 5.5 切换语义

- 连接远程：主窗口 `loadURL(remote)`；本地后端**继续运行**，用于连接管理器与切回本机；
- 切回本机：主窗口 `loadURL(127.0.0.1:<port>)`；
- 下次启动：默认进入本机；若上次是远程模式，可尝试健康检查后自动重连，失败则回本机并提示；
- 本地后端的版本看门狗在远程模式下**不刷新窗口**，避免远端 UI 被“后端自更新”误伤。

---

## 6. 为什么不做「本地 UI + 远程 API 代理」

| 代理方案要处理的问题 | 直接加载远端 UI |
| --- | --- |
| API Base 动态化、每个请求带远端令牌 | 远端同源，天然带令牌 |
| CORS、预检、`allow_credentials` | 不需要 |
| SSE / gzip / 心跳逐块透传 | 远端自己处理 |
| `<img src>` / 附件 / 下载链接的相对路径与令牌查询参数 | 同源路径，零改动 |
| 远端与本地前端版本错位 | 远端自带完整前端 |
| 代理层的超时、断线、重连、上传体积 | 不引入代理层 |

代价是「在 Electron 壳里加载远端代码」，用 §8 的 origin 白名单收住。

---

## 7. 前端与桌面壳改动清单

### 7.1 后端

| 文件 | 改动 |
| --- | --- |
| `backend/app/settings.py` | `desktop_local`、`desktop_session_token` |
| `backend/app/bootstrap.py` | `ensure_desktop_owner()`，启动时序与回填 |
| `backend/app/auth.py` | `dev_` 令牌生成 / 哈希 / 前缀常量 |
| `backend/app/auth_deps.py` | 本机令牌分支、设备令牌分支、`owner_id_for` |
| `backend/app/models.py` | `DeviceLink`、`DevicePairingCode` |
| `backend/app/api/devices.py` | 设备信息、配对码、配对、列表、吊销 |
| `backend/app/api/auth.py` | `AuthConfig` 增加 `desktop_local` |
| `backend/app/api/__init__.py` | 挂载 devices 路由 |
| `backend/tests/` | 本机令牌、设备配对、吊销、归属、限流、架构守卫 |

### 7.2 前端

| 文件 | 改动 |
| --- | --- |
| `frontend/src/lib/desktop.ts` | 连接管理桥类型 |
| `frontend/src/lib/types.ts` | `AuthConfig.desktop_local`、设备与连接类型 |
| `frontend/src/components/AuthGate.tsx` | 桌面本机 / 远程状态；远端 401 显示连接失效而非登录页 |
| `frontend/src/App.tsx` | 桌面模式隐藏退出登录；账号菜单加「连接远程设备…」 |
| `frontend/src/pages/SettingsPage.tsx` | 桌面隐藏账号 / 用户；新增「设备连接」标签 |
| `frontend/src/components/DeviceConnectionsPanel.tsx` | 设备列表 / 配对 / 切换 |
| `frontend/src/pages/ConnectPage.tsx` | `/connect` 独立连接管理器 |
| `frontend/src/main.tsx` | `/connect` 不进主壳 / AuthGate |
| `frontend/src/index.css` | 连接卡片与设备状态样式 |

### 7.3 桌面壳

| 文件 | 改动 |
| --- | --- |
| `desktop/src/main/session.ts`（新） | 本机令牌生成、按 origin 发令牌 |
| `desktop/src/main/connections.ts`（新） | profile 存储、测试、配对、连接、吊销、切换 |
| `desktop/src/main/backend.ts` | spawn 时传 `CODA_DESKTOP_LOCAL` / `CODA_DESKTOP_SESSION_TOKEN` / `CODA_REGISTRATION_OPEN=false` |
| `desktop/src/main/index.ts` | 启动目标选择、远程加载、导航守卫、托盘 / 标题栏入口、连接管理器窗口 |
| `desktop/src/preload/index.ts` | 令牌注入、connection 桥、updates 仅本机 origin 暴露 |
| `desktop/README.md`、`docs/说明/desktop.md` | 更新架构与安全约定 |

---

## 8. 安全边界

1. **IPC 按 origin 白名单**
   - `desktop-window`、`desktop-session:sync`、连接管理 IPC 只接受本机 origin + 当前远程 origin；
   - 任意网页拿不到本机令牌。

2. **导航守卫**
   - 新增 `will-navigate`：只允许本地 origin / 当前远程 origin 在壳内导航，其余交给系统浏览器；
   - `setWindowOpenHandler` 白名单同步加入当前远程 origin。

3. **preload 能力分级**
   - `updates` 只在本机 origin 暴露；
   - `connection` 可暴露，但只返回非敏感信息；令牌走内部同步 IPC，不暴露给页面；
   - 保持 `nodeIntegration=false`、`contextIsolation=true`、`sandbox=true`。

4. **令牌保存**
   - 设备令牌用 Electron `safeStorage` 加密后写 `userData/desktop-connections.json`（0600）；
   - Linux 上 `safeStorage` 不可用时降级为 0600 明文并明确提示；
   - 日志、异常、URL 里都不出现令牌。

5. **传输安全**
   - 局域网 HTTP 可用但提示「明文传输，建议 HTTPS / Tailscale / WireGuard」；
   - 公网地址强制要求 HTTPS；
   - 不默认忽略自签名证书。

6. **配对码**
   - 短 TTL、单次使用、按 IP 限流；
   - 生成配对码需要远端已登录用户，避免局域网内任意人配对。

7. **设备令牌权限**
   - 设备令牌不能创建新的设备令牌 / 配对码 / PAT，避免令牌被盗后横向续命；
   - 远端设置页提供吊销入口，吊销即时生效。

---

## 9. 分阶段实施

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| **M1 本机免登录** | 本机令牌 + 自动 owner + preload 注入 + 隐藏账号入口 | 全新桌面首启无登录；老桌面库数据仍可见；不带令牌访问本机 API 返回 401 |
| **M2 远程基础连接** | `/api/device/info`；桌面地址 + 令牌手动连接；直接加载远端 UI | 输入地址 + 令牌能进远端；切回本机正常；令牌失效显示连接错误 |
| **M3 配对与设备管理** | 配对码 / 设备列表 / 吊销；连接管理器；托盘 + 标题栏入口 | 远端生成码 → 桌面配对进入；远端吊销后桌面报错；可管理多台设备 |
| **M4 可选增强** | QR 码配对（`coda://` 协议）、mDNS 发现、桌面端作为远程主机 | 按需 |

验证命令：

```bash
bash scripts/test.sh                      # 后端 pytest + 前端 typecheck
cd frontend && npm run build              # 前端构建
cd desktop && npm run typecheck           # 桌面壳类型检查
```

桌面 smoke 模式可加一条断言：用本机令牌请求 `/api/auth/me` 必须 200，
把「免登录链路」纳入 CI。

---

## 10. 验收清单

- [ ] 全新桌面首次启动，不出现注册 / 登录页；
- [ ] 老桌面库升级后，原对话 / 智能体 / 网关仍可见、可继续使用；
- [ ] 不带本机令牌访问 `127.0.0.1` 的受保护接口返回 401；
- [ ] 桌面设置隐藏账号 / 用户 / 退出登录；
- [ ] 远端生成配对码 → 桌面配对 → 进入远端主界面；
- [ ] 远端吊销设备后，桌面收到 401 并显示「设备连接失效」；
- [ ] 远程模式切回本机，本机数据与登录态不受影响；
- [ ] 远程页面无法读取本机令牌，也无法让壳内导航到白名单外站点；
- [ ] `bash scripts/test.sh`、前端 build、桌面 typecheck 全部通过。

---

## 11. 风险与待确认

| # | 事项 | 说明 / 建议 |
| --- | --- | --- |
| R1 | **远程设备形态待确认** | 本文按「另一台 Docker / NAS / 服务器上的 Coda」设计。若是**桌面连桌面**，需要额外实现「本机允许远程连接」：桌面后端监听地址可配置、启用设备鉴权，并单独立项评估安全前提 |
| R2 | **旧桌面库可能多用户** | 若检测到多个账号，本机免登录只能选一个默认用户，其余账号数据不可见。建议在设置里提供一次「选择本机默认用户」，选完再进入免登录模式 |
| R3 | **远端版本过旧** | 远端没有 `/api/device/info` / 配对接口时，桌面给出明确提示；不要靠猜接口是否存在 |
| R4 | **远端前端版本兼容** | 直接加载远端 UI 时，壳只依赖 preload 桥；旧版远端前端仍可通过 localStorage 令牌正常登录，但可能显示「退出登录」，需靠壳级入口兜底 |
| R5 | **启动自动重连** | 建议默认「上次远程 + 可达则重连，否则回本机」，避免用户断网时卡在启动页 |
| R6 | **Linux safeStorage** | 无系统密钥环时降级明文保存（0600）并提示，不静默 |
| R7 | **本机令牌在进程 env 中** | 同一 OS 用户可读 `/proc/<pid>/environ`。这是同用户信任边界内的妥协；若需更强隔离，可改为一次性文件描述符或命名管道 |

---

## 12. 不在本期范围

- 桌面端作为远程主机（监听局域网 / 公网）；
- 远程设备自动发现（mDNS / 广播）；
- 远程设备之间的数据同步、迁移、共享对话；
- 远端 Web UI 的多用户权限模型改造；
- 删除后端注册 / 登录接口（网页与移动端仍需要）。