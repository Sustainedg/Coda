# 桌面版（Electron 壳）· 长期文档

> 目录 `desktop/` 是**物理隔离**的旁路模块：不 import 主项目代码、不进 `scripts/test.sh`、
> 构建 / 依赖 / CI 自成一体。`desktop/README.md` 保持自包含的入门说明；本文记录长期约定、
> CI/CD、壳更新通道、签名公证与内核安装通道的调研结论。

---

## 1. 运行形态

```
Electron 主进程（薄壳）
 ├─ 首启 / 整包升级物化：安装包内代码 → userData/releases/<版本>/ + current 软链（bootstrap.ts）
 ├─ 选端口：8010 优先，被占退随机（ports.ts）
 ├─ Python 运行时：**依赖在构建期就装进内嵌解释器**（prepare-python-deps.mjs），
 │   运行时按 requirements 的 sha256 指纹直接选用 —— 首启不建 venv、不跑 pip、不联网；
 │   指纹对不上（开发模式 / 目录损坏）才回退：离线 wheelhouse → venv → 在线 pip
 │   （pythonRuntime.ts，方案 A + C）
 ├─ Node 运行时：内嵌 Node + npm，全局前缀钉在 userData（runtimeEnv.ts，M4）
 ├─ spawn 后端：uvicorn --host 127.0.0.1 --app-dir releases/current/backend
 ├─ 健康检查门控 → loadURL(127.0.0.1:<port>)
 ├─ execv 看门狗：后端同 PID 换血后刷新窗口（兼容旧版 / 直接调用自更新 API）
 ├─ 壳自更新：electron-updater + 滚动 Release（updater.ts；托盘与「设置 → 关于」共用）
 └─ 托盘常驻：关窗 = 隐藏，后端继续跑（IM / 协作 / 生成不中断）
```

### 目录布局（userData，与 Docker 卷设计对齐）

| 目录 | 内容 | 删了会怎样 |
| --- | --- | --- |
| `data/` | `CODA_DATA_DIR`：SQLite、工作区、`secret.key`、内核隔离配置 | ⚠️ 数据全丢 |
| `python/` | **仅兜底路径**：内嵌解释器副本 + venv（正常安装走内置运行时，这里可能不存在） | 下次启动自动重建（离线 wheelhouse 装着；实在没有才联网） |
| `node/global/` | npm 全局安装前缀（内核 CLI 装在这里）+ `node/cache/` | 内核需要重装，不影响对话数据 |
| `releases/` | 后端版本目录 + `current` 指针（复用主项目自更新机制） | 回滚历史丢失，不影响运行 |

> 应用图标与托盘图标由 `desktop/scripts/gen-icons.py` 生成，渲染直接复用网页
> `scripts/gen_icons.py`（母版是用户提供的品牌图 `assets/brand/logo.png`）—— 桌面端与网页的图标是同一张图。
> 改了品牌图形后跑 `npm run gen:icons` 重新生成；托盘图三分：macOS 模板图（纯黑 + alpha，
> 系统按菜单栏明暗自动着色）、Windows 全彩品牌图 `tray-win.png`（Windows 不做模板着色，
> 单色剪影缩放后辨识度差）、Linux 品牌色剪影 `tray.png`。母版位图在 `assets/brand/`。

> 安装包卸载时 `deleteAppDataOnUninstall: false`：对话数据与内核安装都在 userData，卸载应用不该带走。

### 1.1 Windows 安装向导的品牌化

NSIS 的向导界面不是「配个主题」就能换皮肤的（MUI2 固定用系统对话框色），所以做法是
**只换 MUI 留出来的两张位图 + 补一页欢迎页**，剩下的交给系统：

| 资源 | 尺寸 | 出现位置 | 配置项 |
| --- | --- | --- | --- |
| `build/installerSidebar.bmp` | 164×314 | 欢迎页 / 完成页左侧竖图 | `nsis.installerSidebar` |
| `build/installerHeader.bmp` | 150×57 | 其余向导页右上角横幅 | `nsis.installerHeader` |
| `build/installer-branding.nsh` | — | 补一页 `MUI_PAGE_WELCOME` | `nsis.include` |
| 仓库根 `LICENSE` | — | 许可协议页（可滚动全文） | `nsis.license: ../LICENSE` |

三张资源由 `desktop/scripts/gen-installer-assets.py` 生成（复用
`scripts/gen_icons.py` 的品牌母版渲染，母版位图 + Pillow），产物已入库，CI 不跑该脚本。改了品牌色 / 品牌图形时：

```bash
cd desktop && npm run gen:installer   # 与 npm run gen:icons 一起跑
```

**硬约束（改尺寸会静默裁切，不是报错）**：

- 侧栏必须正好 164×314、横幅必须正好 150×57 —— MUI 按像素原样贴；
- 位深必须 **24 位 BMP**（BGR、无 alpha、扫描线自下而上、每行 4 字节对齐）。
  32 位 BMP 的 alpha 在不同 Windows 版本上表现不一致，24 位是最稳的公共子集；
- 横幅底色必须**纯白**：MUI 把它贴在白色页头右上角，底色不白就能看到一块色块边界。

**为什么额外补欢迎页**：electron-builder 的向导模式默认从「安装选项」开始，
`MUI_WELCOMEFINISHPAGE_BITMAP` 就只在最后一页「完成」出现一次，首屏反而是通用模板。
`build/installer-branding.nsh` 里的 `customWelcomePage` 宏补上欢迎页，品牌图成为**第一屏**；
宏内先 `!insertmacro skipPageIfUpdated`，自动更新（`--updated`）时这一页自动跳过，
不给升级流程凭空多一次点击。

> **许可协议页**：`nsis.license` 指向仓库根的 `LICENSE`（**MIT**），安装器会自动多出一页
> 可滚动全文。权威来源只有这一份 —— `desktop/package.json` 的 SPDX 字段同步为 `"MIT"`，
> 不在 `desktop/` 下再维护副本。路径写 `../LICENSE` 是因为 `getResource` 从
> `buildResources`（`desktop/build`）解析不到时会回退到 `projectDir`（`desktop/`）。
> 换协议时只改根 `LICENSE` + `desktop/package.json` 的 SPDX 字段，别再改配置。
> 同一份文件还通过 `extraResources` 落到 `resources/LICENSE.txt`，满足 MIT
> 「分发副本保留版权声明」的要求。

---

## 2. 两条版本线 · 两条更新通道（最容易踩的坑）

| | 后端 | 桌面壳 |
| --- | --- | --- |
| 版本源 | `backend/app/__init__.py` 的 `__version__`（V0.1.x） | `desktop/package.json` 的 `version`（0.1.x，**与后端同号**，发布时 CI 双向校验） |
| 产物 | `coda.tar.gz` + `.sha256` | NSIS / DMG / zip + `latest*.yml` |
| 发布 tag | `v0.1.3`（普通 Release，含网页/后端源码） | 安装包**追加到同一个 `v0.1.3` Release**；另有 `desktop-latest`（**滚动**，prerelease，壳自更新读它） |
| 更新器 | `backend/app/selfupdate.py`，读 `/releases/latest` | `electron-updater`，Generic Provider 直读滚动 tag 下的 `latest*.yml` |
| 生效方式 | 下载 → `releases/<版本>/` → 切 current → `os.execv` 同 PID 重启 | 下载**完整安装包** → 重启 App（NSIS / Squirrel.Mac）；启动时把新安装包里的后端物化并切 current |
| 界面入口 | Docker / 网页部署：「设置 → 关于」 | 桌面端：「设置 → 关于」**与托盘菜单共用** electron-updater（不再走后端 tar.gz 通道） |

**为什么必须隔离**：两条通道都跑在 `Sustainedg/Coda`，而 GitHub 的 `/releases/latest`
是「全仓库最新一个非 prerelease 非 draft 的 Release」。如果壳的版本 Release 成了 latest：

- 后端更新器会去那个 Release 里找 `coda.tar.gz` → 落空（「没有可安装的版本」）；
- 壳更新器（GitHub Provider 模式）会去后端 Release 里找 `latest.yml` → 404。

因此约定：

1. **壳的所有 Release 都是 prerelease**（`gh release create --prerelease`），GitHub 的
   `/releases/latest` 永远指向最后发的后端版本；
2. 壳更新用 **Generic Provider**（`electron-builder.yml` 的 `publish`），地址固定为
   `https://github.com/Sustainedg/Coda/releases/download/desktop-latest/`，与 latest 语义无关；
3. 滚动 tag `desktop-latest` 的资产由 CI **覆盖上传**（`gh release upload --clobber`）；
4. fork / 私有部署可用 `CODA_DESKTOP_UPDATE_URL` 覆盖更新源。

**桌面端只有一条更新通道**：托盘「检查桌面端更新…」与「设置 → 关于 → 检测更新」共用
`updater.ts` 的同一套状态机，下载的都是**完整安装包**（Windows 走 `.blockmap` 差分，
只传变化的数据块，但拼出来的仍是完整安装包）。新安装包自带的后端代码由 `bootstrap.ts`
在下次启动时物化成 `releases/<新版本>` 并切 `current`；若 `requirements.txt` 内容变了
（sha256 指纹不同），`pythonRuntime.ts` 会重装 venv 依赖。后端的「一键更新」
（`coda.tar.gz` + `os.execv`）只服务 Docker / 网页部署，桌面端界面不再暴露。

**macOS 前提**：Squirrel.Mac 只接受已签名应用。未签名构建里 `updater.ts` 会识别
（优先 `app.getCodeSigningInfo()`，Electron 33 没有该 API 时用 `codesign -dv` 探测
`Authority=Developer ID Application`）并降级为「请到发布页手动下载」，不会弹错误或崩溃。
Windows 的 NSIS 未签名也能更新（仅 SmartScreen 提示）。

---

## 3. CI/CD（`.github/workflows/desktop.yml`）

| 触发 | 行为 |
| --- | --- |
| PR / push（`desktop/**`、`backend/**`、`frontend/**` 有改动） | 双平台构建 + 打包版冒烟，不发布 |
| push tag `v*`（后端/网页发版） | 构建 + 冒烟，安装包追加到同一个 `v<版本>` Release，并刷新 `desktop-latest` |
| `workflow_dispatch` + 勾选 publish | 同上（不发 tag 也能发布） |

矩阵：

- `windows-latest`：`--win --x64`，下载 `win-x64` 的 Python / Node；
- `macos-latest`（arm64）：`--mac --arm64 --x64` 一次构建双架构。**必须放在同一次
  electron-builder 调用里**：`latest-mac.yml` 只有在同一次构建内才会合并两个架构的 zip
  条目（app-builder-lib 按同名 update-info 文件合并 `files`）；拆开调用会互相覆盖。
  因此 mac 任务要下载 `mac-arm64` 与 `mac-x64` 两份 Python / Node。

### 发版（与网页/后端同一个版本号、同一个 Release）

```bash
# 1) 改 backend/app/__init__.py 的 __version__（唯一版本源）与
#    desktop/package.json 的 version（去掉 V，例如 V0.1.3 ↔ 0.1.3）
# 2) 提交后跑 release.sh —— 它会校验两个版本号一致，并创建 v<版本> Release
bash scripts/release.sh
# 3) 推送 tag；桌面端 CI 会自动构建，把 Win/mac 安装包追加到同一个 Release，
#    并刷新滚动 Release desktop-latest（壳自更新用）
git push origin v0.1.3
```

### 冒烟（smoke）

打包产物以 `CODA_DESKTOP_SMOKE=1` 启动：主进程只跑「运行时 → 后端 → `/api/health`」，
不建窗口 / 托盘，就绪后打印 `[desktop] smoke ok version=… port=…` 并退出 0；失败退出 1，
10 分钟总看门狗退出 2。它验证的是**真实安装包**里的整条链路（内置运行时直接起 uvicorn，
依赖已预装、不联网），而不是源码模式 —— 首启慢 / 依赖缺失会在这一步暴露。

### 本地打包

```bash
cd desktop
node scripts/fetch-python.mjs        # 按当前平台下载（产物进 build/python-runtime/<arch>/）
node scripts/prepare-python-deps.mjs # 依赖预装进该解释器 + 下载离线 wheelhouse（需联网）
node scripts/fetch-node.mjs          # 同上（build/node-runtime/<arch>/）
node scripts/prepare-backend.mjs
npm run compile && npx electron-builder --publish never
```

发版只在 CI 做（覆盖上传滚动 Release），不要本地 `--publish always`。

---

## 4. 签名与公证

> 当前公开构建仍是 **unsigned**：Windows SmartScreen 会拦（README 引导「仍要运行」）。
> macOS 走 **ad-hoc 签名兜底**（见 4.1）：用户首次打开会在「隐私与安全性」里点「仍要打开」，
> 不会再看到「已损坏」。配置证书后下面的接线自动生效，不需要改代码。

### 4.1 ad-hoc 兜底（没有证书时的 macOS 行为）

`scripts/sign-runtimes.cjs` 在 `afterPack` 阶段判断：找不到 Developer ID Application 身份时，
对整包执行 `codesign --force --deep --sign -` 并 `--verify --deep --strict` 自检。

原因：electron-builder 打包时会改名应用、改 `Info.plist`、换图标，**Electron 自带签名随之失效**。
不重签的 `.app` 下载后带 quarantine，Gatekeeper 判的是「**已损坏**」——这个状态在
系统设置里**没有**「仍要打开」的入口（macOS 15 起连右键「打开」的绕过也没了），
用户只能靠终端自救。ad-hoc 签名让包重新自洽，Gatekeeper 降级为「**未识别开发者**」，
「仍要打开」按钮就会出现。

> 注意：ad-hoc 签名**不解决**"双击直接打开"。那仍然需要 Developer ID + 公证（4.3）。
> 签名自检失败会让构建**失败**（而不是静默发出一份坏包），这是故意的。

### 4.2 CI Secrets 清单

| Secret | 平台 | 用途 |
| --- | --- | --- |
| `CSC_LINK` | Win + mac | 证书文件（`.pfx` / `.p12` 的 base64，或可访问的路径）。electron-builder 通用约定 |
| `CSC_KEY_PASSWORD` | Win + mac | 证书密码 |
| `APPLE_ID` | mac | Apple 开发者账号 |
| `APPLE_APP_SPECIFIC_PASSWORD` | mac | App 专用密码（不是登录密码） |
| `APPLE_TEAM_ID` | mac | 团队 ID |

另可用 `CSC_NAME` 显式指定钥匙串里的签名身份（默认取第一个 `Developer ID Application`）。

### 4.3 Windows

拿到代码签名证书（OV/EV）→ 导出 `.pfx` → base64 后放进 `CSC_LINK`，密码进
`CSC_KEY_PASSWORD`。electron-builder 自动签 `Coda-<版本>-win-x64.exe`。
Azure Trusted Signing 属于另一条路（`win.azureSignOptions`），本文不展开。

> NSIS 固定为「仅当前用户」安装：`perMachine: false` + `allowElevation: false`，
> 并由 `installer-branding.nsh` 的 `customInstallMode` 跳过「安装范围」页。
> 原因有两条：① electron-updater 的静默更新要求安装目录对当前用户可写，
> 装到 `Program Files`（machine 级）会需要提权、静默更新直接失败；
> ② 向导里若允许再选「所有用户」，会触发 UAC 二次启动 —— 第一个安装窗口关闭、
> 提权后的新窗口从头再走一遍欢迎页与许可协议页，用户会误以为安装器重开了。

### 4.4 macOS

1. Apple Developer Program → 创建 **Developer ID Application** 证书 → 导出 `.p12`；
2. `.p12` base64 → `CSC_LINK`，密码 → `CSC_KEY_PASSWORD`；再配 `APPLE_ID` /
   `APPLE_APP_SPECIFIC_PASSWORD` / `APPLE_TEAM_ID`；
3. 构建时的顺序（配置见 `electron-builder.yml`）：
   - `hardenedRuntime: true` + `build/entitlements.mac.plist`（JIT / 可执行内存 /
     禁用库校验 / 本机网络服务 —— 内嵌 Python、Node 作为子进程运行的既定组合）；
   - `afterPack: scripts/sign-runtimes.cjs`：**逐个签名内嵌 Python / Node 里的 Mach-O**。
     electron-builder 虽然会扫包内二进制，但按「内容像不像二进制」启发式判定（会把 `.pyc`
     之类也当二进制丢给 `codesign`），所以用 `mac.signIgnore` 把两份运行时交给专用脚本：
     只签 `file` 认定的 Mach-O，并统一套 runtime entitlements。漏签一个 dylib，公证就整包失败。
     **没有证书时同一钩子改走整包 ad-hoc 签名**（见 4.1），两条分支互斥；
   - 正常签名（electron-builder，已跳过上面两个目录）；
   - `afterSign: scripts/notarize.cjs`：提交 Apple 公证（未配置凭据时直接跳过）。
4. 验证：

```bash
codesign --verify --deep --strict --verbose=2 "Coda.app"
spctl --assess --type execute --verbose "Coda.app"   # 应显示 accepted / notarized
xcrun stapler validate "Coda.app"                    # 确认已装订
```

签名生效后，macOS 自动更新随之可用（`updater.ts` 会自动识别签名状态）。

### 4.5 安装包瘦身（构建期，三平台通用）

桌面端业务代码只占安装包 ~1%，大头是随身携带的三个运行时（Electron / 内嵌 Python /
内嵌 Node）。瘦身分三层，全部发生在构建期，用户侧无感知、无运行时开销：

| 层 | 时机 | 内容 | 收益（解压态） |
| --- | --- | --- | --- |
| 目录剔除 | fetch 脚本（`fetch-node.mjs` / `fetch-python.mjs`） | Node 的 `include/`（C/C++ 头文件，桌面端不编译原生扩展）、npm 文档；Python 的 tcl/tk、terminfo、idlelib/turtledemo/lib2to3/distutils 等开发工具模块 | ~80 MB |
| 符号剥离 | `trim-runtimes.cjs`（afterPack 钩子，仅 Linux） | 官方 Node 二进制与 libpython 携带的 `.symtab` / DWARF 调试段（运行时不加载；崩溃符号化用构建机上的原始发行包） | ~35 MB |
| 语言包裁剪 | `electron-builder.yml` 顶层 `electronLanguages` | Chromium 界面语言包 55 种 → 中英两种。注意 mac 目录名是下划线（`zh_CN.lproj`）而 Linux/Windows 是连字符（`zh-CN.pak`），两种写法都要配 | ~38 MB |

**不能删的东西**（删了会破坏既定链路）：

- `ensurepip` / `pip` / `setuptools`：兜底路径（`pythonRuntime.ts` 方案 C）靠
  `python -m venv` 建环境，venv 默认经 ensurepip 装 pip；
- npm 本体：内核 provision 走 `npm i -g`（§5 的既定通道）；
- `wheelhouse/`：方案 C 的离线兜底依赖（mac 启用公证后如有未签名 Mach-O 报错，
  见 §6 的处理口径）。

**接线顺序**（`electron-builder.yml` 的 `afterPack` 只接受单个函数，多钩子由
`pack-after-hooks.cjs` 串联）：先 `trim-runtimes`（改文件内容）→ 再
`sign-runtimes`（macOS 逐个签 Mach-O）。先签后改会让签名失效，顺序是硬约束。

---

## 5. 内核安装通道（M4 调研结论）

### 5.1 问题

「设置 → 内核管理」安装 CLI 内核时跑的是 `npm install -g`，内核本身也是 Node 程序。
桌面用户机器上**可能没有 Node**；而且：

- macOS 上全局 npm 前缀默认是 `/usr/local` / 只读的 `.app` 内部 → `EACCES`；
- Windows 上没有 `sh`，原来的包装命令 `["sh", "-c", …]` 直接「未找到命令 sh」；
- Windows 上 npm 是 `npm.cmd`，`CreateProcess` 不认识 `.cmd`，直连也会失败；
- 裸 `claude` / `codex` 在 Windows 上是 `.cmd` 垫片，执行层 `create_subprocess_exec` 同样跑不起来。

### 5.2 原生二进制通道调研（2026-11 实测 GitHub Releases）

| 内核 | 存在的原生资产 | 体积（单平台） | Windows 覆盖 |
| --- | --- | --- | --- |
| Claude Code | `claude-win32-x64.zip`、`claude-darwin-arm64.tar.gz`、`claude-darwin-x64.tar.gz`、linux 四份 | 158–173 MB | ✅ |
| Codex | `codex-x86_64-pc-windows-msvc.exe.zip`、`codex-*-apple-darwin.tar.gz`、linux musl | 115–215 MB | ✅ |
| Gemini CLI | 只有 `gemini-darwin-{arm64,x64}-unsigned.zip` + `gemini-cli-bundle.zip` | 21–38 MB | ❌ 没有 Windows 版 |

结论：

- 三个内核的 npm 包里装的**就是同一份原生二进制**（体积大头一致），直接走原生通道不省空间；
- Gemini CLI 缺 Windows 原生资产，原生通道无法覆盖全部内核；
- 官方自更新口径仍是 `npm i -g` / 内核自己的 update 命令，绕开 npm 会丢这条升级路径。

**因此选型：内嵌 Node + npm**（与内嵌 Python 同一思路），既有 provisioner 声明原样工作，
一个通道覆盖三个内核；原生二进制通道留作后续（若将来 Gemini 补齐 Windows 资产，可在
适配器里为个别内核声明下载源，框架侧只执行声明）。

### 5.3 落地内容

- `scripts/fetch-node.mjs`：下载官方 Node 24 LTS（与 `docker/Dockerfile` 同版本）、
  校验 `SHASUMS256.txt`、按架构落到 `build/node-runtime/<arch>/`；
- `src/main/runtimeEnv.ts`：把「内置 Node 目录 + 托管 npm 全局 bin」前置进 PATH，
  并把 `npm_config_prefix` / `npm_config_cache` 钉到 userData；后端进程继承这份环境，
  provisioning 与内核执行自动生效；
- `backend/app/agents/builtin/util.py`：npm 包装脚本改用 **Node 内联脚本**（三平台同一套
  逻辑）；`npm_cli()` 负责卸载 / 查询版本（Windows 经 `cmd.exe /c npm`）；
- `backend/app/runtime/runner.py`：Windows 上把 `.cmd` / `.bat` 垫片交给 `cmd.exe /d /s /c`
  启动，真正的 `.exe` 换成解析后的绝对路径；
- `backend/app/runtime/processes.py`：结束内核统一走这里 —— Windows 上用
  `taskkill /T /F` 连进程树一起杀（`.cmd` 垫片的 pid 是 cmd.exe，只杀它会留下孤儿 node）；
- 桌面版 npm 全局前缀是**App 私有的**（`userData/node/global`），不与用户自己
  `%APPDATA%\npm` / `/usr/local` 里的同名命令抢位置。

> Hermes Agent 走官方 `curl | bash` 安装脚本，Windows 天然不可用（上游也不提供 Windows
> 安装方式）；在 Windows 上点安装会明确报「未找到命令 sh」，不会静默失败。

---

## 6. 已知限制

- **未签名构建**：Windows SmartScreen 拦截；macOS 走 ad-hoc 兜底，首次打开需在
  「隐私与安全性」点「仍要打开」，且 macOS 自动更新不可用（Squirrel.Mac 只认正式签名）；
- **mac Intel**：构建双架构，但 CI 冒烟只跑 arm64（`macos-latest` 是 arm64 runner）；
- **Windows npm 内核**：`.cmd` 垫片已处理；若某个内核的 Windows 包不是 Node 程序
  （未来变化），需要按新通道重新评估；
- **内嵌运行时签名**：有证书时逐个签（公证要求）；无证书时整包 ad-hoc 签，二者不会同时发生；
- **安装包体积**：为换取首启零联网，Python 依赖与离线 wheelhouse 随包分发（压缩后约
  +20–30MB）；后续整包升级走 `.blockmap` 差分，只传变化块，不会每次都全量下发；
  另有构建期瘦身（详见 §4.5）：剔除 Node 头文件 / tcl·tk / terminfo 等运行时用不到的
  内容、剥离 Linux 二进制调试段、Chromium 语言包只留中英，解压态约省 150MB；
- **wheelhouse 与公证**：wheel 里含 Mach-O（`.so`），当前构建未签名；将来启用 Developer ID
  公证时，如果 notarytool 报「whl 内有未签名二进制」，把 Mac 包的 wheelhouse 去掉即可
  （方案 A 已覆盖正常路径，C 只是兜底）；
- 桌面版**不承诺 Linux**（electron-builder 的 AppImage 只是顺带产物）。

## 7. 排障

正常安装**首启不联网**：依赖在构建期已装进内嵌解释器（方案 A），指纹对不上才回退到
离线 wheelhouse（方案 C），最后才是在线 pip。真走到在线这一步时，桌面版也认
`CODA_PIP_INDEX_URL`（镜像源）与 `CODA_INSTALL_PROXY`（安装期代理），
语义与 Docker 一致、只作用于 pip 子进程。

| 症状 | 先看 |
| --- | --- |
| 启动报 `EPERM: operation not permitted, symlink 'V…' -> …\current` | **旧壳的 bug**（v0.1.3 及以前用 `fs.symlinkSync(..., 'dir')`，Windows 普通用户没权限）：升级到改用 junction 的修复版即可。旧包自救：开启「开发者模式」或以管理员启动一次，也可手动<br>`mklink /J "%APPDATA%\Coda\releases\current" "%APPDATA%\Coda\releases\V0.1.3"` |
| 首启仍停在「安装后端依赖」（正常应秒开）/ 报 `ProxyError ... Cannot connect to proxy`（10053/10060） | 先看启动日志里的 `Python 运行时：bundled` 还是 `venv`：`venv` 说明内置依赖指纹没对上（开发模式 / resources 被改），走了兜底。若在在线安装阶段失败：Windows 系统代理残留（代理软件退出后注册表设置还在），关闭「设置 → 网络和 Internet → 代理」或启动代理软件；应用会自动绕过连不上的系统代理重试一次。仍失败看 `%APPDATA%\Coda\install.log`，按上面两个环境变量配镜像 / 代理。`userData/python/venv` 可整目录删掉重建（对话数据不受影响） |
| 启动日志出现 `无法创建 current 目录链接` | 文件系统不支持重解析点（FAT/exFAT、部分网络盘）或被安全软件拦截：应用会降级为直接从版本目录运行（后端一键更新不可用，桌面端本来走整包更新），不影响使用 |
| 内核装了但启动报「未安装或不在 PATH」 | 是否用的是打包版（`resources/node-runtime` 是否存在）；`userData/node/global/bin` 是否在 PATH |
| 壳更新提示 404 / 找不到 latest.yml | 滚动 Release `desktop-latest` 是否还在、是否被误设成非 prerelease；`CODA_DESKTOP_UPDATE_URL` 是否被覆盖 |
| macOS 公证失败 | 日志里搜 `sign-runtimes`；确认内嵌运行时里没有漏签的 Mach-O；检查 entitlements |
| macOS 打开报「已损坏」 | 先看构建日志有没有 `[sign-runtimes] ad-hoc 签名完成`。旧包自救：<br>`sudo xattr -rd com.apple.quarantine /Applications/Coda.app`<br>仍报损坏再 `sudo codesign --force --deep --sign - /Applications/Coda.app` |

---

## 8. Windows 托盘实机验证清单

> 托盘左键唤回 / 右键退出的交互依赖真实 Windows 系统托盘，CI 与 Linux 环境都无法取证：
> 仓库对桌面壳只有静态检查（`npm run typecheck` / `npm run compile`），CI 的打包版冒烟
> 以 `CODA_DESKTOP_SMOKE=1` 运行、**不建窗口也不建托盘**（见 `desktop/src/main/index.ts`
> 的 `smokeMode` 分支）。下面这份清单**逐条待实机执行**，每条给出「操作步骤 → 期望现象 →
> 判定标准」，在 Windows 上装好最新构建（或 `cd desktop && npm run dev`）后照单打勾；
> 任何一条不过，按 §7 排障。托盘图标可能折叠在任务栏右下角「^」里。

### 8.1 左键唤回（绝不退出）

| # | 操作步骤 | 期望现象 | 判定标准 |
| --- | --- | --- | --- |
| 1 | 主窗口正常显示且在最前时，左键单击托盘图标 | 窗口保持打开，仍置前并获得焦点 | 连续快速单击 5 次：始终只有一个主窗口、无新增进程（单实例锁生效），窗口不闪退、不退出 |
| 2 | 主窗口已显示但被其他窗口完全遮挡时，左键单击托盘图标 | 主窗口跳到所有窗口之上并聚焦 | 能直接用键盘输入（输入框有光标）；不需要再点任务栏 |
| 3 | 最小化主窗口后，左键单击托盘图标 | 窗口从最小化**还原**并聚焦 | 窗口恢复原尺寸/最大化状态；任务栏按钮恢复非按下态 |
| 4 | 点窗口 ×（隐藏到托盘）后，左键单击托盘图标 | 窗口重新**显示**并聚焦 | 托盘图标仍在（关窗 ≠ 退出）；后端仍在跑（见 8.3 #2） |

### 8.2 右键菜单与「退出」（单击一次即完全退出）

| # | 操作步骤 | 期望现象 | 判定标准 |
| --- | --- | --- | --- |
| 5 | 右键单击托盘图标 | 弹出菜单：`Coda <版本>`（置灰）/ 打开主界面 / 检查桌面端更新… / 退出 | 一次右键只弹**一个**菜单（不闪两次）；右键不会唤回/隐藏主窗口 |
| 6 | 快速连续右键两次（间隔 <250ms） | 第二次不重复弹菜单；等 >250ms 再右键，菜单照常弹出 | 去重是自愈式的：不出现「菜单再也不弹」或菜单卡住 |
| 7 | 菜单里左键单击「退出」**一次** | 菜单关闭，托盘图标**立即**消失，应用开始退出 | 只点一次就生效（不需要点第二次）；窗口随之关闭 |
| 8 | 退出完成后检查进程与端口 | 任务管理器无 `Coda.exe`（含后端 python 子进程）；`netstat -ano` 中无 8010 端口的 LISTEN 行 | 进程不驻留；再次启动应用一切正常 |

### 8.3 关窗 ≠ 退出的既有语义（回归检查）

| # | 操作步骤 | 期望现象 | 判定标准 |
| --- | --- | --- | --- |
| 1 | 点窗口 × | 窗口隐藏，托盘图标仍在 | 不弹退出确认、进程不退出 |
| 2 | 关窗后浏览器访问 `http://127.0.0.1:8010/api/health` | 返回 200 | 后端随托盘常驻继续运行 |
| 3 | 右键菜单「打开主界面」 | 窗口重新显示并聚焦 | 与左键唤回一致（同走 `showMainWindow()`） |

### 8.4 托盘图标（全彩修复的回归检查）

| # | 操作步骤 | 期望现象 | 判定标准 |
| --- | --- | --- | --- |
| 1 | 100% 缩放观察托盘图标 | 全彩品牌图，与网页 / 应用图标同色系 | 不是单色剪影、不是一团纯色 |
| 2 | 系统缩放分别设为 125% / 150% / 200% 各观察一次 | 图标边缘清晰、可辨认 | 高分屏下不糊边（`tray-win@2x.png` 生效的直观证据） |
